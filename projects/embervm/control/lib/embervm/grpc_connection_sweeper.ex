defmodule Embervm.GrpcConnectionSweeper do
  @moduledoc """
  Periodic sweeper for leaked gRPC.Client.Connection orchestrators that hold
  dead addresses or are wedged in initialization, unable to be disconnected via
  GenServer.call.

  ## Root cause: dual leaks

  `GRPC.Client.Connection` orchestrators are supervised under
  `GRPC.Client.Supervisor` (a DynamicSupervisor) but are NOT linked to their
  callers. Two leaks occur when an instance expires:

  1. **Responsive but stale**: an orchestrator holds a dead address and retries
     re-establishment forever on a hardcoded backoff (max 120s). Some holder
     dropped the channel without disconnecting (e.g. a streamer hard-killed
     before it could disconnect).

  2. **Wedged in init**: an orchestrator stuck in `:proc_lib.sync_start/2` never
     completes init and never enters its GenServer receive loop. A
     `GenServer.call` based disconnect cannot reach it; messages queue and are
     never read.

  Both are reaped by exit signal rather than GenServer call: calling
  `DynamicSupervisor.terminate_child/2` kills the process by exit, which works
  for wedged-in-init orchestrators that are unreachable via message passing.

  ## Correctness: duplicates on LIVE addresses are legitimate

  Several components dial independently (NodeRegistry streamer, NodeChannel hot
  path, BaseBuilder), and every `GRPC.Stub.connect` mints its own orchestrator
  because `name` defaults to `make_ref()`. Multiple orchestrators per live
  address is expected. The sweeper's predicate is "target is not in the live
  address set", never "more than one per address". Getting that backwards would
  tear down healthy connections on every sweep.

  ## Fail-safe: empty or unavailable keep-set aborts the sweep

  If the NodeRegistry is down or returns empty, the sweeper must reap NOTHING.
  At control plane boot the registry is seeded empty, so an empty keep-set is
  a routine case, not an error. Treating it as "reap everything" would destroy
  the entire gRPC connection pool and break dispatch. The sentinel value
  `:unavailable` tracks this distinct state; the sweep is skipped entirely
  when the keep-set cannot be determined with certainty.

  ## Address normalization

  The registry stores addresses like `"10.42.3.34:9090"`, while the
  orchestrator's state target reads `"ipv4:10.42.3.34:9090"`. Normalization
  must handle both formats defensively before comparison.

  ## Strike-based confirmation: calibrated on false-positive patterns

  Live logs from 46 sweeps of the control plane showed every observed false positive
  (transient unreadable processes) persisted exactly ONE sweep, while genuine leaks
  persisted 42 or more sweeps: 8 pids in 1 sweep (false positives), gap, then 1+2+2+2+1
  pids spread across 42-46 consecutive sweeps. The threshold N=3 sits inside a 40-sweep
  gap, so every real leak is caught within the first 3 sweeps of its life.

  Strike counts require N=3 consecutive NON-ABORTED classifications; aborted sweeps
  (registry down, empty keep-set) neither grant nor reset strikes. With sweep interval 60s,
  confirmation window is 3 consecutive NON-ABORTED sweeps, so up to 3 minutes
  if the registry stays healthy. An aborted sweep (registry down, empty keep-set)
  neither grants nor resets strikes, so a child flagged as reapable, then
  invisible for several sweeps due to registry outage, is not delayed further
  once the registry recovers.

  The sweeper requires strikes on both branches: unreadable (one-second :sys.get_state
  timeout) and target-not-in-live-set, so a connection mid-TCP-establish cannot be
  killed by a transiently unreadable state.
  """

  use GenServer
  require Logger

  # Default sweep interval: 60s, balanced against the backoff_max of 120s.
  # One sweep every 60s gives a detection window of up to 120s + 60s.
  @sweep_interval_ms 60_000
  @wedged_strikes_required 3

  # Short timeout for reading a child's state via :sys.get_state. The
  # wedged-in-init processes time out here, which is exactly what we need to
  # identify them as reapable if they are old enough. Busy processes should
  # respond within this window.
  @state_read_timeout_ms 1_000

  # -- Client API

  @spec start_link(keyword()) :: GenServer.on_start()
  def start_link(opts) do
    case Keyword.get(opts, :name, __MODULE__) do
      nil -> GenServer.start_link(__MODULE__, opts)
      name -> GenServer.start_link(__MODULE__, opts, name: name)
    end
  end

  @doc """
  Run one sweep synchronously and return the list of {pid, reason} entries
  that meet the reap threshold. This is a REAL sweep: it advances strike counts
  and terminates children when the gate is enabled and threshold is met. It is
  not a read-only preview. Operators wanting to preview the plan should
  read the dry-run logs instead.
  """
  @spec sweep_now(GenServer.server()) :: [{pid(), String.t()}]
  def sweep_now(server \\ __MODULE__) do
    GenServer.call(server, :sweep_now)
  end

  # -- GenServer callbacks

  @impl true
  def init(opts) do
    # Injected seams (defaults are the real NodeRegistry, real supervisor
    # enumeration, and real termination). Tests inject fakes.
    status_fun = Keyword.get(opts, :status_fun, &Embervm.NodeRegistry.status/0)
    supervisor = Keyword.get(opts, :supervisor, GRPC.Client.Supervisor)
    terminate_child_fun = Keyword.get(opts, :terminate_child_fun, &DynamicSupervisor.terminate_child/2)

    # 0 disables the timer (unit-test default); production uses the module
    # default.
    sweep_interval_ms = Keyword.get(opts, :sweep_interval_ms, @sweep_interval_ms)
    strikes_required = Keyword.get(opts, :strikes_required, @wedged_strikes_required)

    # Destructive gate: false (default, ships inert) means the sweep logs a
    # dry-run plan but deletes NOTHING. true means it fires the terminations.
    # Read from EMBERVM_GRPC_CONNECTION_SWEEP_ENABLED so it flips via deploy
    # values, no code change.
    sweep_enabled = Keyword.get(opts, :sweep_enabled, false)

    state = %{
      status_fun: status_fun,
      supervisor: supervisor,
      terminate_child_fun: terminate_child_fun,
      sweep_interval_ms: sweep_interval_ms,
      sweep_enabled: sweep_enabled,
      strikes_required: strikes_required,
      strikes: %{}
    }

    schedule_sweep(state)
    {:ok, state}
  end

  @impl true
  def handle_call(:sweep_now, _from, state) do
    {reaped, new_state} = sweep(state)
    {:reply, reaped, new_state}
  end

  @impl true
  def handle_info(:sweep, state) do
    {_reaped, new_state} = sweep(state)
    schedule_sweep(new_state)
    {:noreply, new_state}
  end

  def handle_info(_msg, state), do: {:noreply, state}

  # -- Sweep implementation

  # Return either {:ok, set} with populated live addresses, or :unavailable if
  # the registry cannot be reached or the set is empty. These MUST stay distinct:
  # an empty set means "the registry told us there are zero live instances right
  # now", while :unavailable means "we cannot ask the registry at all". Both
  # are fail-safe cases (abort the sweep), but for different reasons and with
  # different logging.
  defp sweep(state) do
    keep_set = get_live_addresses(state)

    case keep_set do
      :unavailable ->
        Logger.info("embervm grpc connection sweeper: skipping sweep (keep-set unavailable, likely at boot)")
        {[], state}

      {:ok, set} ->
        if MapSet.size(set) == 0 do
          Logger.info("embervm grpc connection sweeper: skipping sweep (no live addresses in keep-set)")
          {[], state}
        else
          classify_and_reap(state, set)
        end
    end
  end

  # Classify all children against the live keep-set and reap those whose
  # targets are not in the set.
  defp classify_and_reap(state, keep_set) do
    children = DynamicSupervisor.which_children(state.supervisor)

    classifications = Enum.map(children, &classify_child(&1, keep_set, state))

    strikes =
      classifications
      |> Enum.reduce(state.strikes, fn
        {pid, :keep}, strikes -> Map.delete(strikes, pid)
        {pid, _reason}, strikes -> Map.update(strikes, pid, 1, &(&1 + 1))
      end)
      |> Map.take(Enum.map(children, fn {:undefined, pid, _type, _modules} -> pid end))

    reapable =
      classifications
      |> Enum.filter(&should_reap?/1)
      |> Enum.map(fn {pid, reason} ->
        strike_count = Map.fetch!(strikes, pid)

        strike_reason =
          if strike_count >= state.strikes_required do
            "#{reason} (reapable for #{strike_count} consecutive sweeps, threshold #{state.strikes_required})"
          else
            "#{reason} (strike #{strike_count} of #{state.strikes_required})"
          end

        {pid, strike_reason}
      end)

    reaped =
      reapable
      |> Enum.filter(fn {pid, _reason} -> Map.fetch!(strikes, pid) >= state.strikes_required end)
      |> Enum.map(fn {pid, reason} -> maybe_reap(state, pid, reason) end)
      |> Enum.filter(&(not is_nil(&1)))

    new_state = %{state | strikes: strikes}
    log_sweep(new_state, reaped, reapable, children, keep_set)
    {reaped, new_state}
  end

  # Classify a child as reapable or keep-able. Returns {pid, reason} where
  # reason describes why it should be reaped, or {pid, :keep}.
  defp classify_child({:undefined, pid, :worker, _modules}, keep_set, _state) do
    # Try to read the child's state to determine its target address
    case read_child_state(pid) do
      {:ok, target} when is_binary(target) and target != "" ->
        # Successfully read a target; check against keep-set
        normalized_target = normalize_address(target)

        if address_in_set?(normalized_target, keep_set) do
          {pid, :keep}
        else
          {pid, "target #{inspect(normalized_target)} not in live set"}
        end

      :timeout ->
        # State read timed out; check if the process looks wedged in init
        if process_looks_wedged?(pid) do
          {pid, "unreadable and wedged (likely stuck in proc_lib.sync_start)"}
        else
          {pid, :keep}
        end

      _other ->
        # Cannot determine state or target; err on the side of caution
        {pid, :keep}
    end
  end

  # Read a child's current state via :sys.get_state with a short timeout.
  # Returns {:ok, target} on success (target is extracted from the state),
  # :timeout if the call times out (usually indicates a wedged init), or :error
  # on any other failure.
  #
  # The connection state struct's fields vary by grpc library version. We try
  # multiple approaches:
  # 1. Check if the state is a map with a :target key (custom state)
  # 2. Check if it's a GRPC.Client.Connection struct (extract target)
  # 3. Try various field names that might contain target info
  #
  # This defensive approach ensures we handle different library versions and
  # state shapes gracefully.
  defp read_child_state(pid) do
    try do
      state = :sys.get_state(pid, @state_read_timeout_ms)
      extract_target(state)
    rescue
      _e -> :error
    catch
      :exit, {:timeout, _} -> :timeout
      :exit, _reason -> :error
    end
  end

  # Extract the target address from a connection state. Try multiple field
  # names to be defensive against library version differences.
  defp extract_target(state) when is_map(state) do
    # Try various field names that might contain the target
    case state do
      %{target: target} when is_binary(target) ->
        {:ok, target}

      %{resolver_target: target} when is_binary(target) ->
        {:ok, target}

      %{address: target} when is_binary(target) ->
        {:ok, target}

      _ ->
        :error
    end
  end

  defp extract_target(_state), do: :error

  # Determine if a process looks wedged in initialization. A process stuck in
  # :proc_lib.sync_start has never served anyone and holds no useful
  # connection, so it is safe to reap if:
  # - it shows :proc_lib.sync_start in its current function, OR
  # - it is very new and unreadable (may still be initializing)
  defp process_looks_wedged?(pid) do
    # Note: the reductions < 100 fallback is unstable under repeated :sys.get_state
    # probing; each probe delivers a message that increments reductions, eventually
    # crossing 100 and flipping classification to :keep, which resets strikes. Only
    # the proc_lib.sync_start arm is stable across extended observation. Real leaks
    # in live logs stay flagged 42+ sweeps, indicating they are stuck in sync_start,
    # not riding the reductions fallback.
    case :erlang.process_info(pid, [:current_function, :reductions]) do
      [{:current_function, {:proc_lib, :sync_start, 2}}, {:reductions, _}] ->
        # Definitively stuck in sync_start init
        true

      [{:current_function, _}, {:reductions, reductions}] ->
        # Check age via reductions as a proxy: a process with very few
        # reductions that is still unreadable may be stuck. This is best-effort;
        # we rely primarily on the :proc_lib.sync_start check above.
        # Treat reductions < 100 as "very new, probably still in init".
        reductions < 100

      :undefined ->
        # Process just died; safe to keep (it is gone anyway)
        false

      _other ->
        # Cannot determine; err on the side of caution
        false
    end
  end

  # Predicate: should this child be reaped?
  defp should_reap?({_pid, :keep}), do: false
  defp should_reap?({_pid, _reason}), do: true

  # Maybe reap a child. If the gate is enabled, terminate it; always return
  # {pid, reason} for logging.
  defp maybe_reap(state, pid, reason) do
    if state.sweep_enabled do
      try do
        state.terminate_child_fun.(state.supervisor, pid)
      rescue
        _e -> :ok
      catch
        :exit, _reason -> :ok
      end
    end

    {pid, reason}
  end

  # Get the live address set from NodeRegistry. Returns either {:ok, set} with
  # a MapSet of normalized addresses, or :unavailable if the registry cannot be
  # reached. The two cases MUST be distinct: {:ok, empty_set} means "the
  # registry told us there are zero live instances right now" (routine at boot
  # under dial-home), while :unavailable means "we cannot ask the registry at
  # all". Both abort the sweep, but for different reasons and logging.
  defp get_live_addresses(state) do
    try do
      status = state.status_fun.()

      addresses =
        status
        |> Map.values()
        |> Enum.map(& &1[:address])
        |> Enum.filter(&is_binary/1)
        |> Enum.map(&normalize_address/1)
        |> Enum.reject(&(&1 == ""))
        |> MapSet.new()

      {:ok, addresses}
    rescue
      _e ->
        # Registry unavailable or error
        :unavailable
    catch
      :exit, _reason ->
        # Registry process died or timed out
        :unavailable
    end
  end

  # Normalize an address to a canonical form for comparison. The registry
  # stores `"10.42.3.34:9090"` but the orchestrator state reads
  # `"ipv4:10.42.3.34:9090"`. Strip the `"ipv4:"` prefix if present, leaving
  # other addresses unchanged.
  defp normalize_address(addr) when is_binary(addr) do
    case String.split(addr, ":", parts: 2) do
      ["ipv4", rest] -> rest
      _other -> addr
    end
  end

  defp normalize_address(_addr), do: ""

  # Check if an address is in the keep-set (already normalized).
  defp address_in_set?(addr, set) when is_binary(addr) and addr != "" do
    MapSet.member?(set, addr)
  end

  defp address_in_set?(_addr, _set), do: false

  # Log the sweep results. Every pass ends with a heartbeat line so "swept and
  # found nothing" is distinguishable from "never ran" (#4419: the armed
  # sweeper used to be silent until it killed something). Pids accumulating
  # strikes are listed in BOTH modes so a kill is visible before it lands.
  defp log_sweep(state, reaped, reapable, children, keep_set) do
    prefix = log_prefix(state)

    pending =
      Enum.filter(reapable, fn {pid, _reason} ->
        Map.fetch!(state.strikes, pid) < state.strikes_required
      end)

    Logger.info(
      "#{prefix}: pass complete (children=#{length(children)}, keep_set=#{MapSet.size(keep_set)}, " <>
        "reaped=#{length(reaped)}, pending=#{length(pending)})"
    )

    if reaped != [] do
      verb = if state.sweep_enabled, do: "reaped", else: "WOULD reap"
      Logger.info("#{prefix}: #{verb} #{length(reaped)} orchestrator(s)")
      Enum.each(reaped, fn {pid, reason} -> Logger.info("  pid #{inspect(pid)}: #{reason}") end)
    end

    if pending != [] do
      Logger.info("#{prefix}: #{length(pending)} orchestrator(s) accumulating strikes")
      Enum.each(pending, fn {pid, reason} -> Logger.info("  pid #{inspect(pid)}: #{reason}") end)
    end

    :ok
  end

  defp log_prefix(%{sweep_enabled: true}), do: "embervm grpc connection sweeper"
  defp log_prefix(_state), do: "embervm grpc connection sweeper (DRY RUN, gate off)"

  defp schedule_sweep(%{sweep_interval_ms: ms}) when ms > 0 do
    Process.send_after(self(), :sweep, ms)
    :ok
  end

  defp schedule_sweep(_state), do: :ok
end
