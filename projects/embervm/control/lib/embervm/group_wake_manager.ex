defmodule Embervm.GroupWakeManager do
  @moduledoc """
  The composite-group wake brain (R5, Task 7): the group counterpart of
  `Embervm.StatefulManager`, and this rung's headline verb. An inbound TCP
  connection to a sleeping (banked, or never-yet-booted) composite workload arrives
  at `Embervm.TcpActivator`, which resolves the workload from the LOCAL accept port
  (the entry listener port IS the workload identity at L4, decision 5) and calls
  `wake/3` here. This module SINGLE-FLIGHTS the wake, publishes the fresh entry
  endpoint via `Embervm.EndpointPublisher`, and hands the woken `{ip, port}` back to
  every parked connection so the activator splices bytes to the group's entry
  member. Every SUBSEQUENT connection reaches the entry member node-Envoy-direct
  with zero control-plane involvement (the same off-hit-path shape as serving +
  stateful).

  ## why a separate single-flight brain, not the per-instance GroupManager

  `Embervm.GroupManager` is ONE process per LIVE group instance (its ordered
  create/relight/bank sequence is heavy per-instance work). But the SINGLE-FLIGHT
  of a wake burst is a per-WORKLOAD concern: N concurrent connections to a banked
  workload must produce exactly ONE relight sequence and N spliced sessions. So the
  single-flight lives HERE (one process, a `waking` map keyed by workload, exactly
  `StatefulManager.waking`), and the heavy per-instance relight/create sequence is
  delegated to a GroupManager child the wake spawns (via
  `Embervm.GroupManager.Supervisor`). The first connection kicks one wake worker;
  every concurrent connection parks; when the wake completes, every parked caller is
  resolved to the same entry endpoint.

  ## relight vs create vs fresh (decision 3 + decision 8)

  The wake DECISION reads GroupStore facts purely (ETS reads only, mirroring
  `StatefulManager.plan_wake/2`):

    * a BANKED instance with a COMPLETE set (a bundle for every member) RELIGHTS:
      the GroupManager child resumes the whole snapshot set in role order. ANY
      member relight failure (including the clock-resync FAILED_PRECONDITION,
      decision 7) aborts the relight, evicts the set, and falls back to a full
      FRESH boot INSIDE THE SAME single-flight (`group_fresh_booted{reason}`), with
      the parked connection held across the fallback up to `wakeTimeoutSeconds`
      (the caller blocks on the one wake worker the whole time);
    * a BANKED instance with a PARTIAL set FRESH-BOOTS straight away (the set can
      never relight whole); the partial set is evicted eagerly on the NodeStatus
      sweep (`evict_partial_sets`), so by wake time a partial banked instance has
      already had its `set_id` cleared and is treated as no-set;
    * NO instance at all (scale-to-zero from birth, or a destroyed/evicted-then-
      swept instance) is a full group CREATE (CreateGroupNetwork + role-ordered
      fresh start), the Task 6 create path unchanged.

  All three recover through the SAME connection path (decision 8: for the life of
  the CR, the activator is the fallback), single-flighted so N connections yield one
  wake sequence.

  ## restart adoption (the #3517 lesson, generalized to a set)

  `reconcile/1` (boot + periodic timer) reconciles the `GroupStore` projection
  against every node's reported `group_networks` + `group_member_vms` +
  `group_bundle_sets` (mirrors `StatefulManager.reconcile/1`, adapted for the
  set-shaped, network-anchored composite):

    * an instance whose members are node-reported LIVE (by vm_id) is a live group:
      `adopt_state(:running)` + rebind each member's endpoint/health from node
      truth, respawn its GroupManager child so a later bank/relight has an owner,
      and re-derive the entry endpoint. A control-plane restart with a live group
      therefore republishes the IDENTICAL endpoint WITHOUT touching any VM;
    * an instance with NO live members but a node-reported COMPLETE bundle set heals
      to `:banked` (the set survived the CP restart);
    * an instance with NO live members and NO (or a PARTIAL) bundle set, whose node
      is still reporting, is a daemon-restart casualty: resolved to fresh-bootable
      (its set is evicted, it stays a banked-with-no-set row the next wake fresh-
      boots) rather than failed;
    * `banking`/`relighting`/`fresh_booting` LIMBO is healed the same way (from node
      truth, not the durable log, which never recorded the transient state);
    * PARTIAL sets are evicted eagerly (`GroupStore.evict_partial_sets/2`) from the
      same node-reported bundle-set facts every sweep.

  An in-flight wake for a workload is left untouched by a periodic reconcile
  (mirroring `StatefulManager.adopt_one/4`'s `waking` guard): the wake owns that
  workload's transition. After every reconcile pass, `EndpointPublisher.publish/1`
  re-derives the L4 fan-out so a control-plane restart with a live group
  republishes the identical entry endpoint.
  """

  use GenServer
  require Logger

  alias Embervm.{EndpointPublisher, GroupManager, GroupState, GroupStore, NodeCapacity, WorkloadCatalog}

  # Wake-rate + parked-cap defaults mirror the R4 stateful values (a composite group
  # is a group-level singleton owned by one principal, so per-workload == per-
  # principal, same reasoning as StatefulManager).
  @default_wake_max 10
  @default_wake_window_ms 60_000
  @default_park_cap 16

  # -- Client API ------------------------------------------------------------

  @spec start_link(keyword()) :: GenServer.on_start()
  def start_link(opts) do
    case Keyword.get(opts, :name, __MODULE__) do
      nil -> GenServer.start_link(__MODULE__, opts)
      name -> GenServer.start_link(__MODULE__, opts, name: name)
    end
  end

  @doc """
  Handles one activator connect for a composite `workload` on behalf of `principal`
  (the workload's synthesized owner). Blocks (`:infinity`) until the group is woken
  and returns `{:ok, %{ip, port}}` for the `Embervm.TcpActivator` to splice the
  parked connection to, OR a denial (`{:error, {:wake_rate, _}}`,
  `{:error, {:park_full, _}}`, `{:error, {:wake_failed, r}}`, or
  `{:error, {:unknown_workload}}`). N concurrent connections for one workload
  produce exactly ONE wake sequence and N resolved callers (single-flight).
  """
  @spec wake(GenServer.server(), String.t(), String.t()) :: {:ok, map()} | {:error, term()}
  def wake(server \\ __MODULE__, workload, principal) do
    GenServer.call(server, {:wake, workload, principal}, :infinity)
  end

  @doc """
  Runs one adoption reconcile synchronously (boot continue + periodic sweep run the
  same code) and returns after it completes. Reconciles the GroupStore projection
  against every node's reported group inventory (live members, bundle sets,
  networks), evicts partial sets, re-derives + re-pushes the L4 fan-out. Tests drive
  adoption deterministically through this.
  """
  @spec reconcile(GenServer.server()) :: :ok
  def reconcile(server \\ __MODULE__) do
    GenServer.call(server, :reconcile, :infinity)
  end

  # -- GenServer callbacks ---------------------------------------------------

  @impl true
  def init(opts) do
    state = %{
      store: Keyword.get(opts, :store, GroupStore),
      publisher: Keyword.get(opts, :publisher, EndpointPublisher),
      capacity_table: Keyword.get(opts, :capacity_table, NodeCapacity.table()),
      catalog_table: Keyword.get(opts, :catalog_table, WorkloadCatalog.table()),
      clock: Keyword.get(opts, :clock, &default_clock/0),
      tenant: Keyword.get(opts, :tenant, "homelab"),
      # The create + relight-wake entrypoints. Injected in tests as a fake module
      # implementing create_group/2 + wake_group/1 (or a per-workload scripted fake).
      # Production drives Embervm.GroupManager.Supervisor.
      supervisor_mod: Keyword.get(opts, :supervisor_mod, GroupManager.Supervisor),
      # workload -> [{from, principal}] parked behind an in-flight wake (single-flight).
      waking: %{},
      # principal -> [wake timestamps within the window] (sliding-window rate limit).
      wake_events: %{},
      wake_max: Keyword.get(opts, :wake_max, @default_wake_max),
      wake_window_ms: Keyword.get(opts, :wake_window_ms, @default_wake_window_ms),
      park_cap: Keyword.get(opts, :park_cap, @default_park_cap),
      reconcile_interval_ms: Keyword.get(opts, :reconcile_interval_ms, 0)
    }

    if state.reconcile_interval_ms > 0 do
      {:ok, state, {:continue, :boot}}
    else
      {:ok, state}
    end
  end

  @impl true
  def handle_continue(:boot, state) do
    state = do_reconcile(state)
    schedule(:reconcile, state.reconcile_interval_ms)
    {:noreply, state}
  end

  @impl true
  def handle_call({:wake, workload, principal}, from, state) do
    handle_wake(state, workload, principal, from)
  end

  def handle_call(:reconcile, _from, state) do
    {:reply, :ok, do_reconcile(state)}
  end

  # The async wake worker finished: resolve every parked caller for the workload.
  @impl true
  def handle_info({:wake_done, workload, outcome}, state) do
    {:noreply, finish_wake(state, workload, outcome)}
  end

  def handle_info(:reconcile, state) do
    state = do_reconcile(state)
    schedule(:reconcile, state.reconcile_interval_ms)
    {:noreply, state}
  end

  def handle_info(_msg, state), do: {:noreply, state}

  # -- wake handling ---------------------------------------------------------

  defp handle_wake(state, workload, principal, from) do
    # Straggler: the group came up between the node Envoy's miss and this call (a
    # race with a just-published wake). Resolve to the live entry endpoint directly.
    case GroupStore.entry_endpoint(state.store, workload) do
      %{ip: ip, port: port} when is_binary(ip) and ip != "" and is_integer(port) ->
        {:reply, {:ok, %{ip: ip, port: port}}, state}

      _ ->
        handle_cold_wake(state, workload, principal, from)
    end
  end

  defp handle_cold_wake(state, workload, principal, from) do
    cond do
      not composite_workload?(state, workload) ->
        {:reply, {:error, {:unknown_workload}}, state}

      # Already a wake in flight for this workload: park behind it (single-flight),
      # subject to the parked-connection cap.
      Map.has_key?(state.waking, workload) ->
        park_behind_wake(state, workload, principal, from)

      # First miss: apply the per-workload wake-rate limit, then park + kick ONE
      # wake worker.
      wake_allowed?(state, principal) ->
        state = record_wake(state, principal)
        state = %{state | waking: Map.put(state.waking, workload, [{from, principal}])}
        {:noreply, start_wake(state, workload)}

      true ->
        audit_denial(principal, workload, :wake_rate)
        {:reply, {:error, {:wake_rate, "per-workload wake-rate limit exceeded"}}, state}
    end
  end

  defp park_behind_wake(state, workload, principal, from) do
    waiters = Map.get(state.waking, workload, [])

    if length(waiters) >= state.park_cap do
      audit_denial(principal, workload, :park_full)
      {:reply, {:error, {:park_full, "parked-connection cap exceeded for workload"}}, state}
    else
      {:noreply, %{state | waking: Map.put(state.waking, workload, waiters ++ [{from, principal}])}}
    end
  end

  # -- wake worker -----------------------------------------------------------

  # Kick ONE async wake for a workload. The relight-vs-create DECISION is read HERE
  # (a pure GroupStore fact read); the heavy sequence (relight or create) runs in a
  # spawned worker so a multi-second group boot never head-of-line-blocks another
  # workload's wake. The worker ALWAYS reports a {:wake_done}, even if it crashes, so
  # a parked :infinity caller never blocks forever.
  defp start_wake(state, workload) do
    owner = self()
    plan = plan_wake(state, workload)

    spawn_wake(owner, workload, fn -> run_wake(state, workload, plan) end)
    state
  end

  defp spawn_wake(owner, workload, fun) do
    spawn(fn ->
      outcome =
        try do
          fun.()
        rescue
          e -> {:error, {:wake_crashed, e}}
        catch
          kind, reason -> {:error, {:wake_crashed, {kind, reason}}}
        end

      send(owner, {:wake_done, workload, outcome})
    end)
  end

  # PURE (GroupStore reads only): a banked instance whose set is COMPLETE (its set_id
  # is still stamped, meaning the eager partial-set sweep did not clear it) relights;
  # a banked instance with NO set_id (a partial set the sweep evicted, or a fresh-
  # bootable casualty) fresh-boots; NO instance at all creates.
  defp plan_wake(state, workload) do
    case banked_instance(state, workload) do
      nil -> :create
      %{instance_id: instance_id, set_id: set_id} when is_binary(set_id) and set_id != "" -> {:relight, instance_id}
      %{instance_id: instance_id} -> {:fresh, instance_id}
    end
  end

  # Drive the actual wake sequence in the worker. For a relight/fresh the wake spawns
  # a GroupManager child bound to the EXISTING banked instance and calls wake_group/1
  # (which relights, or on any member failure evicts + fresh-boots in the SAME call,
  # holding the caller). For a create it drives the Task 6 create path unchanged.
  defp run_wake(state, workload, plan) do
    case plan do
      :create ->
        case state.supervisor_mod.create_group(workload) do
          {:ok, endpoint} -> {:ok, endpoint}
          {:error, reason} -> {:error, {:wake_failed, reason}}
        end

      {:relight, instance_id} ->
        run_group_wake(state, workload, instance_id)

      {:fresh, instance_id} ->
        run_group_wake(state, workload, instance_id)
    end
  end

  # Spawn (or reuse) the GroupManager child for the banked instance and drive its
  # wake_group sequence. wake_group returns {:ok, endpoint, outcome} (relit or fresh-
  # fallback) or {:error, reason}. The child is bound in the supervisor under the
  # workload registry key, so a concurrent wake worker cannot spawn a second.
  defp run_group_wake(state, workload, instance_id) do
    case state.supervisor_mod.wake_group(workload, instance_id) do
      {:ok, endpoint, _outcome} -> {:ok, endpoint}
      {:ok, endpoint} -> {:ok, endpoint}
      {:error, reason} -> {:error, {:wake_failed, reason}}
    end
  end

  # -- finish wake (serialized) ----------------------------------------------

  defp finish_wake(state, workload, outcome) do
    {waiters, state} = pop_waiters(state, workload)

    reply =
      case outcome do
        {:ok, %{ip: _ip, port: _port} = endpoint} -> {:ok, endpoint}
        {:error, {:wake_failed, _} = reason} -> {:error, reason}
        {:error, reason} -> {:error, {:wake_failed, reason}}
      end

    case reply do
      {:ok, _} ->
        Logger.info("embervm group woken", workload: workload)

      {:error, reason} ->
        Logger.warning("embervm group wake failed", workload: workload, reason: inspect(reason))
    end

    # The publish already happened inside the GroupManager wake sequence; re-derive
    # once more here so a straggler that connects right after resolves cleanly.
    EndpointPublisher.publish(state.publisher)
    reply_all(waiters, reply)
    state
  end

  defp pop_waiters(state, workload) do
    waiters = Map.get(state.waking, workload, [])
    {waiters, %{state | waking: Map.delete(state.waking, workload)}}
  end

  defp reply_all(waiters, reply) do
    for {from, _principal} <- waiters, do: GenServer.reply(from, reply)
    :ok
  end

  # -- adoption --------------------------------------------------------------

  # Reconcile the GroupStore projection against every node's reported group
  # inventory, evict partial sets, then re-derive + re-push. Mirrors
  # StatefulManager.do_reconcile/1; see the moduledoc's adoption section.
  defp do_reconcile(state) do
    facts = NodeCapacity.all(state.capacity_table)
    live_members = index_group_members(facts)
    bundle_sets = index_group_bundle_sets(facts)

    state =
      GroupStore.all(state.store)
      |> Enum.reject(&GroupState.terminal?(&1.state))
      |> Enum.reduce(state, fn instance, acc ->
        adopt_one(acc, instance, live_members, bundle_sets)
      end)

    # Evict PARTIAL banked sets eagerly from the node-reported bundle-set inventory
    # (the primitive Task 6 built): for every banked instance the node reports a set
    # for, is every member's bundle present? A missing member clears the set_id so
    # the next wake fresh-boots.
    reported_sets = reported_member_sets(bundle_sets)
    _ = GroupStore.evict_partial_sets(state.store, reported_sets)

    EndpointPublisher.publish(state.publisher)
    state
  end

  # instance_id -> [%{member_name, vm_id, ip, healthy}] of the node-reported LIVE
  # member VMs for that group.
  defp index_group_members(facts) do
    for f <- facts, m <- Map.get(f, :group_member_vms, []) || [], reduce: %{} do
      acc ->
        entry = %{
          node_id: f.configured_id,
          member_name: m.member_name,
          vm_id: m.vm_id,
          ip: m.ip,
          healthy: Map.get(m, :healthy, true)
        }

        Map.update(acc, m.group_instance_id, [entry], &[entry | &1])
    end
  end

  # instance_id -> %{set_id, members: MapSet of member_names} of the node-reported
  # bundle set for that group (the daemon reports refs grouped by set dir; it makes
  # no completeness judgment, the CP does).
  defp index_group_bundle_sets(facts) do
    for f <- facts, s <- Map.get(f, :group_bundle_sets, []) || [], into: %{} do
      names = for member <- Map.get(s, :members, []) || [], into: MapSet.new(), do: member.member_name
      {s.group_instance_id, %{set_id: s.set_id, members: names, node_id: f.configured_id}}
    end
  end

  # The {instance_id -> MapSet of member_names} shape GroupStore.evict_partial_sets/2
  # consumes (a banked instance ABSENT here reports no set -> partial -> evicted).
  defp reported_member_sets(bundle_sets) do
    Map.new(bundle_sets, fn {instance_id, %{members: names}} -> {instance_id, names} end)
  end

  defp adopt_one(state, instance, live_members, bundle_sets) do
    cond do
      # An in-flight wake owns the workload's transition; never touch it.
      Map.has_key?(state.waking, instance.workload) ->
        state

      # Live members reported for this instance -> a live group: adopt to running,
      # rebind member endpoints/health, respawn the GroupManager owner, re-derive
      # the entry endpoint. Never touches a VM.
      Map.has_key?(live_members, instance.instance_id) ->
        adopt_live(state, instance, Map.fetch!(live_members, instance.instance_id))

      # No live members, but a COMPLETE reported bundle set -> heal to banked.
      complete_set?(state, instance, bundle_sets) ->
        heal_to_banked(state, instance)

      # No live members and no complete set, but the instance's node is still
      # reporting -> a daemon-restart casualty: resolve to fresh-bootable (evict any
      # stale set, leave the banked-with-no-set row the next wake fresh-boots).
      node_reporting?(state, instance.node_id) ->
        resolve_casualty(state, instance)

      true ->
        state
    end
  end

  # Adopt a node-reported live group WITHOUT touching a VM: force the ETS state to
  # running, rebind every reported member's live facts + health, respawn the
  # GroupManager owner in the adopted state, and re-derive the entry endpoint from
  # the entry member's node-reported ip (the CP-derived DNAT port stays authoritative
  # for the published endpoint; adoption keeps the SAME entry endpoint the pre-restart
  # publish recorded, so the republish is identical).
  defp adopt_live(state, instance, members) do
    GroupStore.adopt_state(state.store, instance.instance_id, :running)

    for m <- members do
      _ = GroupStore.set_member_health(state.store, instance.instance_id, m.member_name, m.healthy)
    end

    _ = adopt_owner(state, instance)
    state
  end

  defp heal_to_banked(state, %{state: :banked}), do: state

  defp heal_to_banked(state, instance) do
    GroupStore.adopt_state(state.store, instance.instance_id, :banked)
    Logger.info("embervm group adopted (banked)", instance_id: instance.instance_id)
    state
  end

  # A daemon-restart casualty (no live members, no complete set): its warmth is gone.
  # Evict any stale set so its set_id is cleared, and adopt it to banked-with-no-set
  # (the next wake fresh-boots). We do NOT fail it: a composite instance with no VMs
  # and no set is exactly a scale-to-zero group awaiting a fresh wake, not a terminal
  # failure (banked cannot fail; the group is recoverable through the connection path).
  defp resolve_casualty(state, %{state: :banked, set_id: nil}), do: state

  defp resolve_casualty(state, instance) do
    _ = GroupStore.evict_set(state.store, instance.instance_id, "adoption_casualty")
    GroupStore.adopt_state(state.store, instance.instance_id, :banked)
    Logger.info("embervm group adopted (fresh-bootable casualty)", instance_id: instance.instance_id)
    state
  end

  # Respawn the GroupManager owner for an adopted-live instance so a later
  # bank/relight has a process. Best-effort: a supervisor without an adopt seam (the
  # test fakes) is a clean no-op. The child is bound under the workload registry key,
  # so a second reconcile finds it already-started and leaves it.
  defp adopt_owner(state, instance) do
    if function_exported?(state.supervisor_mod, :adopt_group, 2) do
      state.supervisor_mod.adopt_group(instance.workload, instance.instance_id)
    else
      :ok
    end
  end

  # A banked set is COMPLETE iff the node reports a bundle for every member the group
  # expects (each member row's name present in the reported set).
  defp complete_set?(state, instance, bundle_sets) do
    case Map.get(bundle_sets, instance.instance_id) do
      %{members: reported} ->
        expected = expected_member_names(state, instance)
        expected != MapSet.new() and MapSet.subset?(expected, reported)

      _ ->
        false
    end
  end

  defp expected_member_names(state, instance) do
    for m <- GroupStore.members(state.store, instance.instance_id),
        into: MapSet.new(),
        do: m.member_name
  end

  defp node_reporting?(state, node_id) when is_binary(node_id) do
    match?({:ok, _}, NodeCapacity.fetch(state.capacity_table, node_id))
  end

  defp node_reporting?(_state, _node_id), do: false

  # -- wake-rate limit -------------------------------------------------------

  defp wake_allowed?(%{wake_max: max}, _principal) when not is_integer(max) or max <= 0, do: true

  defp wake_allowed?(state, principal) do
    now = state.clock.()
    length(recent_wakes(state, principal, now)) < state.wake_max
  end

  defp record_wake(state, principal) do
    now = state.clock.()
    recent = recent_wakes(state, principal, now)
    %{state | wake_events: Map.put(state.wake_events, principal, [now | recent])}
  end

  defp recent_wakes(state, principal, now) do
    cutoff = now - state.wake_window_ms

    state.wake_events
    |> Map.get(principal, [])
    |> Enum.filter(&(&1 > cutoff))
  end

  # -- catalog helpers -------------------------------------------------------

  defp composite_workload?(state, workload) do
    match?({:ok, %{class: "composite"}}, WorkloadCatalog.fetch(state.catalog_table, workload))
  end

  defp banked_instance(state, workload) do
    GroupStore.list(state.store, workload)
    |> Enum.find(&(&1.state == :banked))
  end

  # -- misc ------------------------------------------------------------------

  defp audit_denial(principal, workload, reason) do
    Embervm.Metering.record_denial(principal, workload, reason)
  end

  defp schedule(msg, interval_ms) when interval_ms > 0 do
    Process.send_after(self(), msg, interval_ms)
  end

  defp schedule(_msg, _interval), do: :ok

  defp default_clock, do: System.system_time(:millisecond)
end
