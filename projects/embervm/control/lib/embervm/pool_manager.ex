defmodule Embervm.PoolManager do
  @moduledoc """
  Keeps the primed-VM pool refilled so the dispatcher has a warm VM to Assign.
  Where `Embervm.Dispatcher` owns the hot path (pop a VM, Assign it), this owns
  the slow background loop that Primes VMs to replace the ones assignment
  destroys, so the two are separate failure domains and the refill loop never
  slows a dispatch decision.

  ## the refill policy (floor-first, then proportional)

  On each tick, for every FRESH, non-draining node with a ready base for a
  workload, it computes how many more VMs to Prime and allocates the node's
  remaining live-VM budget in two phases:

    1. FLOOR first: bring every workload up to its `concurrency.floor` of primed
       VMs. Under a scarce budget the floor deficits are filled round-robin, one
       VM at a time across workloads, so no single workload's floor monopolizes
       the budget.
    2. SURPLUS next: whatever budget remains is split proportional to each
       workload's current queue depth, priming beyond the floor for the workloads
       under load, each capped at its own depth (no point priming more VMs than
       there are queued tasks).

  Because phase 1 (all floors) completes before phase 2 (any surplus), one
  workload's burst can NEVER consume the budget another workload's floor needs:
  the surplus phase only ever spends what is left after every floor is funded.
  That is the floor-isolation property the acceptance test asserts.

  ## backpressure

  Primes are heavy (restore + health-gate), so concurrent primes are capped
  (`max_concurrent_primes`) and every in-flight prime counts against both the cap
  and the node budget, so a slow daemon does not get a growing pile of concurrent
  restores. `free_primed_slots` from the daemon's own heartbeat is the authority
  for "how many are already parked"; in-flight primes this loop has issued but the
  daemon has not yet reported are added so a burst of ticks does not over-prime.

  ## destroy-on-assign

  The daemon destroys a VM on Assign (single-use), which drops `free_primed_slots`
  and is reflected on the next heartbeat; the loop then re-primes toward the
  floor. The DISPATCH-side accounting (a vm_id leaves the dispatcher's inventory
  the instant it is committed) is what prevents double-assign; this loop only
  needs the heartbeat count to know how many to replace.

  ## status.primedFloorSatisfied

  This loop owns `status.primedFloorSatisfied`: it is true for a workload once the
  node's `free_primed_slots >= floor`. Ownership is split by key exactly like the
  BaseBuilder/watcher split: the watcher writes `observedGeneration`, the
  BaseBuilder writes `conditions`/`snapshotRef`/`snapshotDigest`, and this writes
  only `primedFloorSatisfied`, so the three merge-patches never clobber. Status is
  written only when the flag flips, not every tick.

  ## deferred

  Base turnover on a digest change (proactively Destroy old-base primed VMs and
  re-prime from the new base, per the BaseBuilder's `superseded_refs` seam) is a
  documented follow-on: the node contract reports a slot COUNT, not per-VM base
  identity, so the control plane cannot today target the old-base VMs specifically.
  R0 primes zero VMs (empty `EMBERVM_NODED_IMAGES`), so there is nothing to turn
  over yet; when guest images land (Task 14) the turnover either drains via natural
  assignment or gains a per-VM base tag on the node contract.
  """

  use GenServer
  require Logger

  alias Embervm.{NodeCapacity, WorkloadCatalog}
  alias Embervm.Node.V1.{PrimeRequest, PrimeResponse, Trace}

  @refill_interval_ms 1_000
  @max_concurrent_primes 4
  @stale_after_ms 15_000
  @depth_table :embervm_queue_depth

  # -- Client API ------------------------------------------------------------

  @spec start_link(keyword()) :: GenServer.on_start()
  def start_link(opts) do
    case Keyword.get(opts, :name, __MODULE__) do
      nil -> GenServer.start_link(__MODULE__, opts)
      name -> GenServer.start_link(__MODULE__, opts, name: name)
    end
  end

  @doc """
  Runs one refill pass synchronously (the same code the periodic tick runs) and
  returns after the Prime workers it decides on are spawned. Tests drive refill
  deterministically through this with `start_refill: false`; production fires it
  on the timer every #{@refill_interval_ms}ms.
  """
  @spec refill(GenServer.server()) :: :ok
  def refill(server \\ __MODULE__) do
    GenServer.call(server, :refill)
  end

  @doc "A snapshot of in-flight primes and floor-satisfaction, for tests/ops."
  @spec stats(GenServer.server()) :: map()
  def stats(server \\ __MODULE__) do
    GenServer.call(server, :stats)
  end

  # -- GenServer callbacks ---------------------------------------------------

  @impl true
  def init(opts) do
    state = %{
      capacity_table: Keyword.get(opts, :capacity_table, NodeCapacity.table()),
      catalog_table: Keyword.get(opts, :catalog_table, WorkloadCatalog.table()),
      depth_table: Keyword.get(opts, :depth_table, @depth_table),
      dispatcher: Keyword.get(opts, :dispatcher, Embervm.Dispatcher),
      clock: Keyword.get(opts, :clock, &default_mono/0),
      channel_fun: Keyword.get(opts, :channel_fun, &Embervm.NodeChannel.get/1),
      invalidate_fun: Keyword.get(opts, :invalidate_fun, &Embervm.NodeChannel.invalidate/2),
      prime_fun: Keyword.get(opts, :prime_fun, &default_prime/2),
      deposit_fun: Keyword.get(opts, :deposit_fun, &Embervm.Dispatcher.deposit/4),
      status_writer: Keyword.get(opts, :status_writer, &Embervm.K8s.patch_workload_status/3),
      refill_interval_ms: Keyword.get(opts, :refill_interval_ms, @refill_interval_ms),
      max_concurrent_primes: Keyword.get(opts, :max_concurrent_primes, @max_concurrent_primes),
      stale_after_ms: Keyword.get(opts, :stale_after_ms, @stale_after_ms),
      # Dynamic.
      inflight_primes: %{},
      prime_workers: %{},
      floor_satisfied: %{}
    }

    if Keyword.get(opts, :start_refill, true) do
      schedule_refill(state)
    end

    {:ok, state}
  end

  @impl true
  def handle_call(:refill, _from, state), do: {:reply, :ok, do_refill(state)}

  def handle_call(:stats, _from, state) do
    {:reply,
     %{
       inflight_primes: state.inflight_primes,
       floor_satisfied: state.floor_satisfied,
       prime_workers: map_size(state.prime_workers)
     }, state}
  end

  @impl true
  def handle_info(:refill, state) do
    state = do_refill(state)
    schedule_refill(state)
    {:noreply, state}
  end

  # A prime worker reported. On success, deposit the vm_id into the dispatcher's
  # inventory; either way release its in-flight-prime slot.
  def handle_info({:prime_result, pid, node_id, wl, result}, state) do
    case Map.pop(state.prime_workers, pid) do
      {nil, _} ->
        {:noreply, state}

      {meta, workers} ->
        Process.demonitor(meta.ref, [:flush])

        case result do
          {:ok, %PrimeResponse{vm_id: vm_id}} when is_binary(vm_id) and vm_id != "" ->
            _ = safe(fn -> state.deposit_fun.(state.dispatcher, node_id, wl, vm_id) end)

          {:error, reason} ->
            Logger.warning("embervm pool: Prime for #{wl} on #{node_id} failed: #{inspect(reason)}")

          _ ->
            :ok
        end

        {:noreply, %{state | prime_workers: workers} |> dec_prime(node_id, wl)}
    end
  end

  # A prime worker died without reporting: release its slot (it always sends a
  # result in the normal path, so this is an abnormal exit).
  def handle_info({:DOWN, _ref, :process, pid, reason}, state) do
    case Map.pop(state.prime_workers, pid) do
      {nil, _} ->
        {:noreply, state}

      {meta, workers} ->
        Logger.warning("embervm pool: prime worker for #{meta.workload} died: #{inspect(reason)}")
        {:noreply, %{state | prime_workers: workers} |> dec_prime(meta.node_id, meta.workload)}
    end
  end

  def handle_info(_msg, state), do: {:noreply, state}

  @impl true
  def terminate(_reason, state) do
    for {pid, _meta} <- state.prime_workers, do: Process.exit(pid, :shutdown)
    :ok
  end

  # -- refill ----------------------------------------------------------------

  defp do_refill(state) do
    now = state.clock.()

    NodeCapacity.all(state.capacity_table)
    |> Enum.filter(fn f -> not stale?(f, now, state.stale_after_ms) and not draining?(f) end)
    |> Enum.reduce(state, fn facts, acc -> refill_node(acc, facts) end)
  end

  defp refill_node(state, facts) do
    # Dial the SPECIFIC instance this capacity fact belongs to (its instance_id,
    # `"node/pod_uid"`), not the bare node name (instance-key unification PR-B0a):
    # under brick co-location the node-name channel alias resolves to an arbitrary
    # sibling that never adopted this instance's base, so a Prime against the alias
    # fails "unknown snapshot_ref" and loops at the refill cadence. Fall back to the
    # node name for a legacy/single-instance fact (no instance_id), unchanged. This
    # key is also the per-instance bookkeeping key (inflight primes, floor status)
    # so two co-located instances track independently.
    node_id = dial_id(facts)
    workloads = ready_workloads(state, facts)

    # Node live-VM budget minus what this loop already has in flight for the node.
    node_inflight = node_inflight_primes(state, node_id)
    budget = max(0, Map.get(facts, :max_live_vms, 0) - Map.get(facts, :live_vms, 0) - node_inflight)

    # And the global concurrency cap on primes.
    global_room = max(0, state.max_concurrent_primes - map_size(state.prime_workers))
    budget = min(budget, global_room)

    state = update_floor_status(state, facts, workloads)

    if budget <= 0 or workloads == [] do
      state
    else
      allocation = plan_primes(state, node_id, workloads, budget)
      spawn_primes(state, node_id, allocation)
    end
  end

  # Workloads this node has a ready base for, paired with their catalog entry
  # (floor, namespace, name) and the node's current free primed slots.
  defp ready_workloads(state, facts) do
    for {wl, wc} <- facts.workloads || %{},
        base_ready?(wc),
        {:ok, entry} <- [WorkloadCatalog.fetch(state.catalog_table, wl)] do
      %{
        workload: wl,
        namespace: entry.namespace,
        floor: entry.floor,
        snapshot_ref: Map.get(wc, :snapshot_ref),
        free: Map.get(wc, :free_primed_slots, 0)
      }
    end
  end

  # Decide how many VMs to prime per workload within `budget`: floors first
  # (round-robin fair under scarcity), then surplus proportional to queue depth.
  defp plan_primes(state, node_id, workloads, budget) do
    floor_deficits =
      for w <- workloads, into: %{} do
        have = w.free + inflight_for(state, node_id, w.workload)
        {w.workload, max(0, w.floor - have)}
      end

    floor_alloc = allocate_round_robin(floor_deficits, budget)
    remaining = budget - map_sum(floor_alloc)

    depths =
      for w <- workloads, into: %{} do
        {w.workload, queue_depth(state, w.workload)}
      end

    surplus_alloc = allocate_proportional(depths, remaining)

    for w <- workloads,
        n = Map.get(floor_alloc, w.workload, 0) + Map.get(surplus_alloc, w.workload, 0),
        n > 0 do
      %{workload: w.workload, snapshot_ref: w.snapshot_ref, count: n}
    end
  end

  defp spawn_primes(state, node_id, allocation) do
    Enum.reduce(allocation, state, fn %{workload: wl, snapshot_ref: ref, count: n}, acc ->
      Enum.reduce(1..n//1, acc, fn _i, acc2 -> start_prime_worker(acc2, node_id, wl, ref) end)
    end)
  end

  defp start_prime_worker(state, node_id, wl, snapshot_ref) do
    owner = self()
    channel_fun = state.channel_fun
    invalidate_fun = state.invalidate_fun
    prime_fun = state.prime_fun

    {pid, ref} =
      spawn_monitor(fn ->
        result =
          case channel_fun.(node_id) do
            {:ok, channel} ->
              req = %PrimeRequest{trace: %Trace{workload: wl}, snapshot_ref: snapshot_ref || ""}

              res =
                try do
                  prime_fun.(channel, req)
                catch
                  kind, reason -> {:error, {kind, reason}}
                end

              # A transport death (a replaced noded pod's broken connection, wrapped
              # by the Mint adapter as an RPCError) must tear the cached channel down
              # so the NEXT refill tick re-dials; otherwise every Prime reuses the dead
              # channel and the node wedges (Embervm.NodeChannel.transport_dead?/1).
              maybe_invalidate_prime(res, invalidate_fun, node_id, channel)
              res

            {:error, reason} ->
              {:error, {:connect, reason}}
          end

        send(owner, {:prime_result, self(), node_id, wl, result})
      end)

    meta = %{ref: ref, node_id: node_id, workload: wl}

    %{state | prime_workers: Map.put(state.prime_workers, pid, meta)}
    |> inc_prime(node_id, wl)
  end

  # -- primedFloorSatisfied status -------------------------------------------

  defp update_floor_status(state, _facts, workloads) do
    Enum.reduce(workloads, state, fn w, acc ->
      satisfied = w.free >= w.floor

      if Map.get(acc.floor_satisfied, w.workload) == satisfied do
        acc
      else
        _ = safe(fn -> write_floor_status(acc, w.namespace, w.workload, satisfied) end)
        %{acc | floor_satisfied: Map.put(acc.floor_satisfied, w.workload, satisfied)}
      end
    end)
  end

  defp write_floor_status(state, namespace, name, satisfied) do
    case state.status_writer.(namespace, name, %{"primedFloorSatisfied" => satisfied}) do
      :ok ->
        :ok

      {:error, reason} ->
        Logger.warning(
          "embervm pool: primedFloorSatisfied patch failed for #{namespace}/#{name}: #{inspect(reason)}"
        )
    end
  end

  # -- allocators ------------------------------------------------------------

  # Distribute `budget` units across the demands one at a time, round-robin, so no
  # demand is filled at another's expense under scarcity. Returns wl -> count.
  defp allocate_round_robin(demands, budget) do
    keys = demands |> Enum.filter(fn {_k, v} -> v > 0 end) |> Enum.map(&elem(&1, 0))
    do_round_robin(keys, demands, budget, Map.new(keys, &{&1, 0}))
  end

  defp do_round_robin([], _demands, _budget, acc), do: acc
  defp do_round_robin(_keys, _demands, 0, acc), do: acc

  defp do_round_robin(keys, demands, budget, acc) do
    {acc, budget, granted} =
      Enum.reduce(keys, {acc, budget, false}, fn k, {acc, budget, granted} ->
        cond do
          budget <= 0 -> {acc, budget, granted}
          Map.get(acc, k) >= Map.get(demands, k) -> {acc, budget, granted}
          true -> {Map.update!(acc, k, &(&1 + 1)), budget - 1, true}
        end
      end)

    # Stop when a full pass grants nothing (every demand met) or budget is gone.
    if granted and budget > 0, do: do_round_robin(keys, demands, budget, acc), else: acc
  end

  # Split `budget` proportional to weights, each capped at its own weight (never
  # prime more than there are queued tasks). Largest-remainder rounding, then any
  # leftover from caps is redistributed round-robin.
  defp allocate_proportional(_weights, budget) when budget <= 0, do: %{}

  defp allocate_proportional(weights, budget) do
    positive = Enum.filter(weights, fn {_k, w} -> w > 0 end)
    total = positive |> Enum.map(&elem(&1, 1)) |> Enum.sum()

    if total == 0 do
      %{}
    else
      base =
        for {k, w} <- positive, into: %{} do
          share = min(w, div(budget * w, total))
          {k, share}
        end

      leftover = budget - map_sum(base)
      demands = Map.new(positive)

      # Redistribute rounding/cap leftover round-robin, respecting each cap.
      residual_demands =
        for {k, w} <- positive, into: %{}, do: {k, w - Map.get(base, k, 0)}

      extra = allocate_round_robin(residual_demands, leftover)
      Map.merge(base, extra, fn _k, a, b -> a + b end) |> Map.take(Map.keys(demands))
    end
  end

  # -- in-flight prime accounting --------------------------------------------

  defp inc_prime(state, node_id, wl) do
    %{state | inflight_primes: Map.update(state.inflight_primes, {node_id, wl}, 1, &(&1 + 1))}
  end

  defp dec_prime(state, node_id, wl) do
    key = {node_id, wl}

    case Map.get(state.inflight_primes, key, 0) do
      n when n <= 1 -> %{state | inflight_primes: Map.delete(state.inflight_primes, key)}
      n -> %{state | inflight_primes: Map.put(state.inflight_primes, key, n - 1)}
    end
  end

  defp inflight_for(state, node_id, wl), do: Map.get(state.inflight_primes, {node_id, wl}, 0)

  defp node_inflight_primes(state, node_id) do
    state.inflight_primes
    |> Enum.filter(fn {{n, _wl}, _v} -> n == node_id end)
    |> Enum.map(&elem(&1, 1))
    |> Enum.sum()
  end

  # -- helpers ---------------------------------------------------------------

  defp base_ready?(wc) do
    ready =
      case Map.get(wc, :base_state) do
        :BASE_BUILD_STATE_READY -> true
        3 -> true
        _ -> false
      end

    ref = Map.get(wc, :snapshot_ref)
    ready and is_binary(ref) and ref != ""
  end

  defp stale?(f, now, stale_after), do: now - Map.get(f, :updated_at, 0) > stale_after
  defp draining?(f), do: Map.get(f, :draining, false) == true

  # The dial/bookkeeping key for a capacity fact: its instance_id (`"node/pod_uid"`)
  # when present, else the bare node name (a legacy/single-instance fact without the
  # field still resolves via the node-name alias, unchanged behaviour).
  defp dial_id(facts) do
    case Map.get(facts, :instance_id) do
      id when is_binary(id) and id != "" -> id
      _ -> facts.configured_id
    end
  end

  defp queue_depth(state, wl) do
    if :ets.whereis(state.depth_table) == :undefined do
      0
    else
      state.depth_table
      |> :ets.match_object({{wl, :_}, :_})
      |> Enum.map(fn {_key, n} -> n end)
      |> Enum.sum()
    end
  end

  defp map_sum(map), do: map |> Map.values() |> Enum.sum()

  defp schedule_refill(state) do
    Process.send_after(self(), :refill, state.refill_interval_ms)
    state
  end

  defp safe(fun) do
    try do
      fun.()
    rescue
      _ -> :error
    catch
      _, _ -> :error
    end
  end

  defp default_mono, do: System.monotonic_time(:millisecond)

  defp default_prime(channel, %PrimeRequest{} = req) do
    Embervm.Node.V1.NodeService.Stub.prime(channel, req)
  end

  # Tear the shared channel down when a Prime failed because the channel's transport
  # is dead, so the next refill tick re-dials to the (Service-DNS) node address. A
  # server status leaves the channel up; only transport_dead?/1 shapes invalidate.
  defp maybe_invalidate_prime({:error, reason}, invalidate_fun, node_id, channel) do
    if Embervm.NodeChannel.transport_dead?(reason), do: invalidate_fun.(node_id, channel)
    :ok
  end

  defp maybe_invalidate_prime(_result, _invalidate_fun, _node_id, _channel), do: :ok
end
