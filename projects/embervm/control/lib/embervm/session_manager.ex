defmodule Embervm.SessionManager do
  @moduledoc """
  The session lifecycle brain (R2, Task 6): create-from-primed-pool, destroy, and
  the placement seam, sitting between the front-end router and the per-session
  `Embervm.Session` processes. The router NEVER computes where a session lives or
  reserves capacity; it calls this module, which owns those decisions (the R2
  held invariant: the invocation front-end is split from placement).

  ## create (assignment from the primed pool)

  Create rides the R0 primed-pool machinery, exactly like a task's warm dispatch:

    1. reserve CAPACITY, fail-closed and in order (this GenServer serializes
       creates so two concurrent creates cannot both slip past `maxSessions`):
       the per-workload live+banked count against `session.maxSessions`, the live
       count against `concurrency.cap`, and the principal's daily quota;
    2. pick a node (`Embervm.SessionPlacement` lands in PR-4; PR-3 uses a minimal
       ready-node-with-budget choice inline, noted below);
    3. CLAIM a primed pristine VM from the dispatcher's inventory for the workload
       (principal-bound at claim, the task-class assignment moment), or `Prime` one
       on a pool miss;
    4. append `session_created` (write-through, mints the id + token) and start the
       session process under the DynamicSupervisor.

  A create denial is a structured 429/403 with a distinguishable reason and an
  audited op append (D12.2 cadence), returned to the router to shape the response.

  ## invoke routing

  `invoke/3` resolves the session by id, then:

    * a LIVE session with a running process: forward to it (`Embervm.Session.invoke`),
      which serializes FIFO and returns the guest response verbatim;
    * a `creating`/`banking`/`relighting` session: PARK the caller (PR-4 relight
      routing; PR-3 sessions are created directly `running`, so this is a rare
      transient window). PR-3 returns `{:error, {:not_ready, state}}` which the
      router 409s, rather than block, since no bank/relight path exists yet to
      resolve it (noted);
    * a TERMINAL session: `{:error, {:gone, reason}}` (the router 410s with the
      recorded terminal reason);
    * an unknown session: `{:error, :not_found}`.

  ## destroy

  `destroy/2` transitions the session to `destroyed`: it stops the session process
  (which owns the VM) and, if the session held a banked snapshot, records the
  intent to evict it (the EvictSnapshot RPC lands in PR-4; PR-3 destroys a LIVE
  session's VM via its process). Idempotent-ish: destroying an already-terminal
  session is a no-op success.
  """

  use GenServer
  require Logger

  alias Embervm.{NodeCapacity, SessionStore, WorkloadCatalog}
  alias Embervm.Node.V1.{PrimeRequest, PrimeResponse, Trace}

  @registry Embervm.SessionRegistry

  # -- Client API ------------------------------------------------------------

  @spec start_link(keyword()) :: GenServer.on_start()
  def start_link(opts) do
    case Keyword.get(opts, :name, __MODULE__) do
      nil -> GenServer.start_link(__MODULE__, opts)
      name -> GenServer.start_link(__MODULE__, opts, name: name)
    end
  end

  @doc """
  Creates a session for `workload` on behalf of `principal`. Returns
  `{:ok, %{session_id, token, expires_at, base_digest}}` (the token is returned
  ONCE, here), or `{:error, {:denied, reason}}` for a capacity/quota denial
  (`:session_cap`, `:workload_cap`, `:quota`, `:no_capacity`, `:unknown_workload`,
  `:not_session_class`) that the router maps to a 429/403.
  """
  @spec create(GenServer.server(), String.t(), String.t()) :: {:ok, map()} | {:error, term()}
  def create(server \\ __MODULE__, workload, principal) do
    GenServer.call(server, {:create, workload, principal})
  end

  @doc """
  Routes one invoke to a session. `req` is the task-envelope map
  (`%{method, path, headers, body}`). Returns the session process's result, or a
  routing error (`{:not_ready, state}`, `{:gone, reason}`, `:not_found`).
  """
  @spec invoke(GenServer.server(), String.t(), map()) :: {:ok, map()} | {:error, term()}
  def invoke(server \\ __MODULE__, session_id, req) do
    GenServer.call(server, {:route_invoke, session_id, req}, :infinity)
  end

  @doc """
  Destroys a session (`* -> destroyed`): stops its process/VM and records
  `session_destroyed`. Returns `{:ok, session}`, `{:error, :not_found}`, or
  `{:ok, :already_terminal}` for an already-terminal session.
  """
  @spec destroy(GenServer.server(), String.t()) :: {:ok, term()} | {:error, term()}
  def destroy(server \\ __MODULE__, session_id) do
    GenServer.call(server, {:destroy, session_id})
  end

  # -- GenServer callbacks ---------------------------------------------------

  @impl true
  def init(opts) do
    state = %{
      session_store: Keyword.get(opts, :session_store, SessionStore),
      dispatcher: Keyword.get(opts, :dispatcher, Embervm.Dispatcher),
      supervisor: Keyword.get(opts, :supervisor, Embervm.SessionSupervisor),
      registry: Keyword.get(opts, :registry, @registry),
      capacity_table: Keyword.get(opts, :capacity_table, NodeCapacity.table()),
      catalog_table: Keyword.get(opts, :catalog_table, WorkloadCatalog.table()),
      clock: Keyword.get(opts, :clock, &default_clock/0),
      quota_table: Keyword.get(opts, :quota_table, Embervm.Metering.table()),
      quota_config: Keyword.get(opts, :quota_config, Embervm.Metering.quota_config()),
      # Injected for tests; production uses the real claim/prime/channel seams.
      claim_fun: Keyword.get(opts, :claim_fun, &default_claim/3),
      channel_fun: Keyword.get(opts, :channel_fun, &Embervm.NodeChannel.get/1),
      prime_fun: Keyword.get(opts, :prime_fun, &default_prime/2),
      # Extra opts threaded into every started Embervm.Session (the daemon seams),
      # so a test can inject a fake session_assign into the spawned process.
      session_opts: Keyword.get(opts, :session_opts, []),
      tenant: Keyword.get(opts, :tenant, "homelab")
    }

    {:ok, state}
  end

  @impl true
  def handle_call({:create, workload, principal}, _from, state) do
    {:reply, do_create(state, workload, principal), state}
  end

  def handle_call({:route_invoke, session_id, req}, from, state) do
    # Resolve the route on this process (fast), but do NOT block it on the invoke:
    # the session process's own FIFO parks the caller. So we reply the pid to the
    # caller path via a spawned forwarder, keeping the manager responsive. Simpler:
    # forward synchronously from a short task so the manager is not the bottleneck.
    route = resolve_route(state, session_id)

    case route do
      {:live, pid} ->
        # Forward off the manager so a long guest round-trip does not serialize other
        # sessions' routing through this one GenServer.
        _ = spawn_forward(pid, req, from)
        {:noreply, state}

      {:error, _reason} = error ->
        {:reply, error, state}
    end
  end

  def handle_call({:destroy, session_id}, _from, state) do
    {:reply, do_destroy(state, session_id), state}
  end

  # -- create ----------------------------------------------------------------

  defp do_create(state, workload, principal) do
    with {:ok, entry} <- fetch_session_workload(state, workload),
         :ok <- check_session_cap(state, workload, entry),
         :ok <- check_workload_cap(state, workload, entry),
         :ok <- check_quota(state, principal),
         {:ok, node_id, snapshot_ref} <- pick_node(state, workload),
         {:ok, vm_id} <- claim_or_prime(state, node_id, workload, snapshot_ref) do
      register_and_start(state, workload, principal, entry, node_id, vm_id)
    else
      {:error, reason} ->
        audit_denial(state, principal, workload, reason)
        {:error, {:denied, reason}}
    end
  end

  defp fetch_session_workload(state, workload) do
    case WorkloadCatalog.fetch(state.catalog_table, workload) do
      {:ok, %{class: "session", session: session} = entry} when is_map(session) ->
        {:ok, entry}

      {:ok, _entry} ->
        {:error, :not_session_class}

      :error ->
        {:error, :unknown_workload}
    end
  end

  # live + banked < session.maxSessions (both hold a resource: a VM or disk).
  defp check_session_cap(state, workload, %{session: session}) do
    counts = SessionStore.counts(state.session_store, workload)

    if counts.live + counts.banked < session.max_sessions do
      :ok
    else
      {:error, :session_cap}
    end
  end

  # live session VMs < concurrency.cap (banked sessions do not hold a live VM).
  defp check_workload_cap(state, workload, %{cap: cap}) do
    counts = SessionStore.counts(state.session_store, workload)

    if counts.live < cap, do: :ok, else: {:error, :workload_cap}
  end

  defp check_quota(state, principal) do
    if Embervm.Metering.within_quota?(principal, state.clock.(), state.quota_config, state.quota_table) do
      :ok
    else
      {:error, :quota}
    end
  end

  # PR-3 placement: the first fresh, non-draining, ready node with live-VM budget.
  # `Embervm.SessionPlacement` (PR-4, Task 8) replaces this with rendezvous hashing
  # over the ready node list; the interface (workload -> {node_id, snapshot_ref}) is
  # the same, so that swap does not touch this module's create flow.
  defp pick_node(state, workload) do
    NodeCapacity.all(state.capacity_table)
    |> Enum.find_value({:error, :no_capacity}, fn f ->
      wc = Map.get(f.workloads || %{}, workload)

      cond do
        wc == nil -> false
        not base_ready?(wc) -> false
        not has_budget?(f) -> false
        true -> {:ok, f.configured_id, Map.get(wc, :snapshot_ref)}
      end
    end)
  end

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

  defp has_budget?(f) do
    max = Map.get(f, :max_live_vms, 0)
    max > 0 and Map.get(f, :live_vms, 0) < max
  end

  # Claim a primed VM from the dispatcher's inventory, or Prime one on a miss (the
  # dispatcher's own miss path, run inline here since create latency is not the
  # per-invoke hot path). A Prime failure is a create denial, not a crash.
  defp claim_or_prime(state, node_id, workload, snapshot_ref) do
    case state.claim_fun.(state.dispatcher, node_id, workload) do
      {:ok, vm_id} ->
        {:ok, vm_id}

      :miss ->
        prime(state, node_id, snapshot_ref)
    end
  end

  defp prime(state, node_id, snapshot_ref) do
    with {:ok, channel} <- state.channel_fun.(node_id),
         {:ok, %PrimeResponse{vm_id: vm_id}} when is_binary(vm_id) and vm_id != "" <-
           safe_prime(state, channel, snapshot_ref) do
      {:ok, vm_id}
    else
      _ -> {:error, :no_capacity}
    end
  end

  defp safe_prime(state, channel, snapshot_ref) do
    req = %PrimeRequest{trace: %Trace{}, snapshot_ref: snapshot_ref || ""}
    state.prime_fun.(channel, req)
  rescue
    _ -> {:error, :prime_crashed}
  catch
    _, _ -> {:error, :prime_crashed}
  end

  # Append session_created (mints id + token), then start the session process. The
  # store write is write-through and MUST land before the process exists so a crash
  # between the two heals to a durable-but-processless session (which adoption in
  # PR-4 rebinds). If the process fails to start, the durable session is orphaned as
  # a live-but-unrouted row; PR-4 adoption rebinds it from NodeStatus, so we surface
  # the create as failed rather than leave the caller a token to a dead process.
  defp register_and_start(state, workload, principal, entry, node_id, vm_id) do
    attrs = %{
      tenant: state.tenant,
      principal: principal,
      workload: workload,
      node_id: node_id,
      vm_id: vm_id,
      base_snapshot_ref: Map.get(entry, :image_ref),
      base_digest: base_digest(entry),
      expires_at: state.clock.() + entry.session.max_lifetime_seconds * 1000
    }

    case SessionStore.create(state.session_store, attrs) do
      {:ok, %{session_id: session_id} = created} ->
        case start_session_process(state, session_id, workload, principal, entry, node_id, vm_id) do
          {:ok, _pid} ->
            {:ok, created}

          {:error, reason} ->
            Logger.error("embervm session: process start failed for #{session_id}: #{inspect(reason)}")
            {:error, {:denied, :process_start_failed}}
        end

      {:error, reason} ->
        {:error, {:denied, {:store, reason}}}
    end
  end

  defp start_session_process(state, session_id, workload, principal, entry, node_id, vm_id) do
    child_opts =
      [
        session_id: session_id,
        workload: workload,
        principal: principal,
        node_id: node_id,
        vm_id: vm_id,
        queue_cap: entry.session.invoke_queue_cap,
        timeout_ms: entry.timeout_ms,
        session_store: state.session_store,
        # Register under the session id so the router/manager resolve the pid.
        name: {:via, Registry, {state.registry, session_id}}
      ] ++ state.session_opts

    spec = %{
      id: session_id,
      start: {Embervm.Session, :start_link, [child_opts]},
      restart: :transient,
      type: :worker
    }

    DynamicSupervisor.start_child(state.supervisor, spec)
  end

  # -- invoke routing --------------------------------------------------------

  defp resolve_route(state, session_id) do
    case SessionStore.get(state.session_store, session_id) do
      {:ok, %{state: :running} = _session} ->
        case Registry.lookup(state.registry, session_id) do
          [{pid, _}] -> {:live, pid}
          # Running per the durable store but no process: a restart-limbo the PR-4
          # adoption sweep rebinds. PR-3 has no adoption, so surface a transient
          # error rather than pretend the VM is reachable.
          [] -> {:error, {:not_ready, :running}}
        end

      {:ok, %{state: session_state}} ->
        cond do
          session_state in [:creating, :banking, :relighting] -> {:error, {:not_ready, session_state}}
          # terminal: 410 with the recorded reason.
          true -> {:error, {:gone, terminal_reason(state, session_id, session_state)}}
        end

      :error ->
        {:error, :not_found}
    end
  end

  defp terminal_reason(state, session_id, session_state) do
    case SessionStore.get(state.session_store, session_id) do
      {:ok, %{terminal_reason: reason}} when is_binary(reason) -> reason
      _ -> to_string(session_state)
    end
  end

  # Forward an invoke to the session process off the manager, replying to the
  # original caller. A crash forwarding (process died mid-route) becomes an error
  # reply rather than a hung caller.
  defp spawn_forward(pid, req, from) do
    spawn(fn ->
      reply =
        try do
          Embervm.Session.invoke(pid, req)
        catch
          :exit, reason -> {:error, {:session_down, reason}}
        end

      GenServer.reply(from, reply)
    end)
  end

  # -- destroy ---------------------------------------------------------------

  defp do_destroy(state, session_id) do
    case SessionStore.get(state.session_store, session_id) do
      {:ok, %{state: session_state}} when session_state in [:expired, :evicted, :destroyed, :failed] ->
        {:ok, :already_terminal}

      {:ok, session} ->
        destroy_live(state, session)

      :error ->
        {:error, :not_found}
    end
  end

  # Stop the session process (which owns and, on terminate, releases the VM), then
  # record session_destroyed. A live session has a process; a banked one does not
  # (its VM is already gone, only the snapshot remains, whose eviction is PR-4).
  defp destroy_live(state, session) do
    _ = stop_session_process(state, session.session_id, session)

    SessionStore.transition(
      state.session_store,
      session.session_id,
      :destroy,
      :session_destroyed,
      %{reason: :destroyed},
      %{}
    )
  end

  # Terminate the session process and destroy its VM. We destroy the VM HERE (not in
  # the process's terminate, which may not run on a hard kill) so a destroy always
  # tears down the guest even if the process is wedged.
  defp stop_session_process(state, session_id, session) do
    case Registry.lookup(state.registry, session_id) do
      [{pid, _}] ->
        _ = DynamicSupervisor.terminate_child(state.supervisor, pid)
        destroy_vm(state, session)

      [] ->
        # No process (already stopped or never started): still best-effort destroy
        # the VM from the durable residency, so a destroy of a limbo session frees it.
        destroy_vm(state, session)
    end
  end

  defp destroy_vm(state, %{node_id: node_id, vm_id: vm_id}) when is_binary(node_id) and is_binary(vm_id) do
    with {:ok, channel} <- state.channel_fun.(node_id) do
      opts = state.session_opts

      destroy_fun =
        Keyword.get(opts, :destroy_fun, fn ch, id ->
          Embervm.Node.V1.NodeService.Stub.destroy(ch, %Embervm.Node.V1.DestroyRequest{vm_id: id})
        end)

      try do
        destroy_fun.(channel, vm_id)
      rescue
        _ -> :error
      catch
        _, _ -> :error
      end
    end

    :ok
  end

  defp destroy_vm(_state, _session), do: :ok

  # -- helpers ---------------------------------------------------------------

  # Audit a create denial as one op-log append (D12.2 cadence). Capacity/quota
  # denials are principal-attributable and request-bounded, so they are appended
  # (unlike per-tick dispatch saturation). Reuses the metering denial reason space.
  defp audit_denial(_state, principal, workload, reason) do
    metering_reason =
      case reason do
        :quota -> :quota
        other -> other
      end

    Embervm.Metering.record_denial(principal, workload, metering_reason)
    :ok
  end

  defp base_digest(entry) do
    # PR-3 has no BaseBuilder-resolved digest wired into the catalog entry for the
    # session flow; use the image ref as the birth-base identity placeholder. PR-4
    # threads the resolved base_digest from status once relight lineage needs it.
    Map.get(entry, :image_ref) || ""
  end

  defp default_claim(dispatcher, node_id, workload) do
    Embervm.Dispatcher.claim(dispatcher, node_id, workload)
  end

  defp default_prime(channel, %PrimeRequest{} = req) do
    Embervm.Node.V1.NodeService.Stub.prime(channel, req)
  end

  defp default_clock, do: System.system_time(:millisecond)
end
