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

  # Tracer.with_span/set_attributes are OpenTelemetry.Tracer MACROS, required (not
  # aliased as a runtime dep). Matches the stateful/serving/session manager idiom.
  require OpenTelemetry.Tracer, as: Tracer
  import Bitwise

  alias Embervm.{EndpointPublisher, GroupManager, GroupState, GroupStore, NodeCapacity, WorkloadCatalog}

  alias Embervm.Node.V1.{ArtifactRef, RestoreArtifactRequest, StopGroupMemberRequest, Trace}

  # Wake-rate + parked-cap defaults mirror the R4 stateful values (a composite group
  # is a group-level singleton owned by one principal, so per-workload == per-
  # principal, same reasoning as StatefulManager).
  @default_wake_max 10
  @default_wake_window_ms 60_000
  @default_park_cap 16

  # Wake-worker bound (R6, Task 10). A wedged member boot (the R5 drill: a member
  # that never opens its port, chaining `:infinity` waits) must NOT pin `waking`
  # forever, starving `adopt_one` and overflowing the park. So the WORKER is bounded
  # by the workload's `wakeTimeoutSeconds` plus this margin: on the bound the wake is
  # FAILED (single-flight released, parked callers erred/re-parked, the banked set
  # left re-wakeable), NOT held. The parked caller's own `:infinity` GenServer.call is
  # untouched: a caller waits as long as it chooses, the bound is on the wake it waits
  # behind. The margin covers the legitimate relight/fresh sequence beyond the guest's
  # own readiness budget (network setup, the create fallback). Default group
  # wakeTimeoutSeconds is 120 (scratch-k8s 180), so the bound defaults to ~135s.
  @default_wake_timeout_margin_ms 15_000
  # Fallback wakeTimeoutSeconds when the catalog entry carries none (matches
  # WorkloadWatcher's @group_defaults.wake_timeout_seconds).
  @default_wake_timeout_seconds 120

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
      # The teardown seam for a wake that expired at the bound (and for a dead
      # create found by adoption): force-rolls the workload's live instance so the
      # singleton gate releases and the NEXT wake can create afresh. Without this a
      # bound-expired create leaves a live-forever instance every later wake
      # bounces off (:already_live), a permanent wedge only an operator's forced
      # roll could clear. Injected in tests as a module implementing force_roll/1.
      sweeper_mod: Keyword.get(opts, :sweeper_mod, Embervm.GroupSweeper),
      # Restore-on-miss seams (R6, Task 8). Unlike serving/stateful this module owns
      # no dispatch channel of its own (the heavy relight is delegated to a
      # GroupManager child), so the restore-before-relight RPC dials its own channel
      # through the shared NodeChannel. restore_artifact_fun issues RestoreArtifact for
      # a complete exported GROUP_SET; op_log records the :artifact_restored audit. All
      # injected for tests; production dials the real NodeService stub.
      channel_fun: Keyword.get(opts, :channel_fun, &Embervm.NodeChannel.get/1),
      invalidate_fun: Keyword.get(opts, :invalidate_fun, &Embervm.NodeChannel.invalidate/2),
      restore_artifact_fun: Keyword.get(opts, :restore_artifact_fun, &default_restore_artifact/2),
      stop_group_member_fun:
        Keyword.get(opts, :stop_group_member_fun, &default_stop_group_member/2),
      op_log: Keyword.get(opts, :op_log, Embervm.OpLog.SQLite),
      # The backend module dispatched below, threaded alongside :op_log (the
      # server address) so a non-default backend never requires editing this
      # module. Defaults to the same SQLite module :op_log defaults to.
      op_log_mod: Keyword.get(opts, :op_log_mod, Embervm.OpLog.SQLite),
      # ADR embervm/014 decision 5: node-confirmed destroy config plumbing.
      node_confirmed_destroy: Keyword.get(opts, :node_confirmed_destroy, false),
      destroying_alarm_ms: Keyword.get(opts, :destroying_alarm_ms, 300_000),
      orphan_grace_ms: Keyword.get(opts, :orphan_grace_ms, 60_000),
      # Ids already alarmed for being stuck in destroying (log once per id, not per tick).
      destroying_alarmed: MapSet.new(),
      # The composite supernet + DNAT port base + control-plane pod IP, the SAME
      # shared values that feed the GroupManager (and noded's CompositeSupernet /
      # ServingPortBase). Adoption re-derives the entry DNAT endpoint `{pod_ip,
      # port_base + host_offset(entry ip, supernet)}` from these so a CP restart
      # republishes the IDENTICAL endpoint the live publish recorded (the op-log
      # rebuild reconstructs only the fallback {tap ip, guest port}). Defaulted to the
      # group_manager_defaults app-env so production and the GroupManager agree.
      supernet: Keyword.get(opts, :supernet, group_default(:supernet, "10.101.0.0/16")),
      port_base: Keyword.get(opts, :port_base, group_default(:port_base, 30_000)),
      pod_ip: Keyword.get(opts, :pod_ip, group_default(:pod_ip, nil)),
      # workload -> [{from, principal}] parked behind an in-flight wake (single-flight).
      waking: %{},
      # workload -> monotonic ms the in-flight wake started (Task 10). Feeds the
      # adoption self-recovery: a workload still `waking` past 2 * wakeTimeoutSeconds
      # is a wedged wake whose worker never reported, recovered as a casualty rather
      # than skipped forever.
      wake_started: %{},
      # principal -> [wake timestamps within the window] (sliding-window rate limit).
      wake_events: %{},
      wake_max: Keyword.get(opts, :wake_max, @default_wake_max),
      wake_window_ms: Keyword.get(opts, :wake_window_ms, @default_wake_window_ms),
      park_cap: Keyword.get(opts, :park_cap, @default_park_cap),
      # Wake-worker bound (Task 10). Derived per-wake from the workload's
      # wakeTimeoutSeconds + this margin, unless `wake_bound_ms` overrides it outright
      # (tests inject a tiny bound so a never-ready wake fails deterministically). The
      # monotonic clock the bound + the adoption stuck-check read is injectable too
      # (tests drive it with a fake counter).
      wake_timeout_margin_ms: Keyword.get(opts, :wake_timeout_margin_ms, @default_wake_timeout_margin_ms),
      wake_bound_ms: Keyword.get(opts, :wake_bound_ms, nil),
      mono_clock: Keyword.get(opts, :mono_clock, &default_mono/0),
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

  # The wake-worker bound (Task 10) elapsed. If the wake for THIS workload is still
  # in flight (the worker never reported a {:wake_done}, the wedged-boot case), fail
  # it: finish_wake releases single-flight and errs the parked callers, leaving the
  # banked set re-wakeable. A stale timer for a wake that already finished (or a newer
  # wake replaced it) is a no-op: `waking` no longer has the workload, so finish_wake
  # pops an empty waiter list and touches nothing.
  def handle_info({:wake_timeout, workload}, state) do
    if Map.has_key?(state.waking, workload) do
      Logger.warning("embervm group wake timed out at bound", workload: workload)
      state = finish_wake(state, workload, {:error, {:wake_failed, :wake_timeout}})
      # Tear the expired wake's instance down (async: force_roll drives noded RPCs
      # and must not block this manager). The create/relight worker behind it may
      # still be hung inside a member start; rolling the instance terminal releases
      # the singleton so the next wake can create afresh instead of bouncing off
      # :already_live forever, and the store's publish guard keeps the zombie
      # worker from publishing onto the rolled instance if it ever returns.
      teardown_expired_wake(state, workload)
      {:noreply, state}
    else
      {:noreply, state}
    end
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
      log_park_full(state, workload, length(waiters))
      {:reply, {:error, {:park_full, "parked-connection cap exceeded for workload"}}, state}
    else
      {:noreply, %{state | waking: Map.put(state.waking, workload, waiters ++ [{from, principal}])}}
    end
  end

  # Log a park overflow as STRUCTURED warning (Task 10) so the Task 11 alert can match
  # `park_full` with the oldest-waiter age: a park filling up means the in-flight wake
  # is not draining, the exact R5 symptom. The oldest-waiter age is how long the wake
  # this park sits behind has been in flight (mono now - wake_started); 0 when no start
  # was recorded. The alert is NOT implemented here (PR-7); this only emits the signal.
  defp log_park_full(state, workload, depth) do
    Logger.warning("embervm group park_full",
      event: :park_full,
      workload: workload,
      park_depth: depth,
      oldest_waiter_age_ms: oldest_waiter_age_ms(state, workload)
    )
  end

  defp oldest_waiter_age_ms(state, workload) do
    case Map.get(state.wake_started, workload) do
      started when is_integer(started) -> max(state.mono_clock.() - started, 0)
      _ -> 0
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
    # Stamp the wake's start (for the adoption stuck-check) and arm the wake-worker
    # bound: if the worker has not reported a {:wake_done} by then, {:wake_timeout}
    # fails the wake so single-flight releases (Task 10).
    schedule_wake_timeout(workload, wake_bound_ms(state, workload))
    %{state | wake_started: Map.put(state.wake_started, workload, state.mono_clock.())}
  end

  # The per-wake worker bound in ms: the explicit `wake_bound_ms` override (tests) or
  # the workload's wakeTimeoutSeconds + margin.
  defp wake_bound_ms(%{wake_bound_ms: ms}, _workload) when is_integer(ms) and ms > 0, do: ms

  defp wake_bound_ms(state, workload) do
    wake_timeout_seconds(state, workload) * 1_000 + state.wake_timeout_margin_ms
  end

  # The workload's wakeTimeoutSeconds from the catalog (group config), defaulting to
  # @default_wake_timeout_seconds when the entry carries none.
  defp wake_timeout_seconds(state, workload) do
    case WorkloadCatalog.fetch(state.catalog_table, workload) do
      {:ok, %{group: %{wake_timeout_seconds: secs}}} when is_integer(secs) and secs > 0 -> secs
      _ -> @default_wake_timeout_seconds
    end
  end

  defp schedule_wake_timeout(workload, bound_ms) when is_integer(bound_ms) and bound_ms > 0 do
    Process.send_after(self(), {:wake_timeout, workload}, bound_ms)
  end

  defp schedule_wake_timeout(_workload, _bound_ms), do: :ok

  # Force-roll a workload's live instance after its wake expired at the bound (or
  # adoption found a dead create). Async and best-effort: the sweeper drives noded
  # StopGroupMember/DeleteGroupNetwork RPCs, so it must not run on this manager's
  # process; a crash in the spawned task only means the wedge waits for the next
  # adoption pass (which retries via the same seam).
  defp teardown_expired_wake(state, workload) do
    sweeper = state.sweeper_mod

    spawn(fn ->
      try do
        result = sweeper.force_roll(workload)
        Logger.warning("embervm group expired wake torn down", workload: workload, result: inspect(result))
      rescue
        e -> Logger.error("embervm group expired-wake teardown failed", workload: workload, error: inspect(e))
      catch
        kind, reason ->
          Logger.error("embervm group expired-wake teardown failed", workload: workload, error: inspect({kind, reason}))
      end
    end)

    :ok
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
        # Restore-on-miss (R6, Task 8): a COMPLETE exported set whose local bundles
        # are gone is restored FIRST, then the delegated relight resumes it warm. A
        # partial local set that is NOT fully exported, or an unreachable store, skips
        # the restore and the GroupManager wake evicts + fresh-boots as it does today
        # (fail-open warmth, standing decision 7). The restore runs inside this wake
        # worker so single-flight/park semantics are unchanged.
        _ = maybe_restore_set(state, workload, instance_id)
        run_group_wake(state, workload, instance_id)

      {:fresh, instance_id} ->
        run_group_wake(state, workload, instance_id)
    end
  end

  # -- restore-on-miss (R6, Task 8) ------------------------------------------

  # Restore the whole exported GROUP_SET for a banked instance when its local
  # bundles are missing but the store holds a complete set and the store is
  # reachable. Best-effort: any gap (no set_id, set already local, store
  # unreachable, or not exported) is a clean skip that leaves the relight to the
  # existing GroupManager fresh-fallback path. The restore RPC failing degrades the
  # same way.
  defp maybe_restore_set(state, workload, instance_id) do
    # The group instance_id IS the group_instance_id the node keys its bundle sets by
    # (GroupStore rows carry no separate group_instance_id column; the instance id is
    # that identity, mirroring how do_reconcile indexes sets by group_instance_id).
    # OPTIMISTIC restore-on-miss (R6, Task 8, option b): the CP tracks no remote
    # inventory, so on a TRUE local miss (the node no longer reports the set's member
    # bundles) with a reachable store it ATTEMPTS the restore; the daemon fails closed
    # (FAILED_PRECONDITION) if no store copy exists and the delegated relight then
    # evicts + fresh-boots as it does today. store_reachable == false never blocks the
    # wake, it only withholds the restore (fail-open warmth, standing decision 7).
    with {:ok, %{node_id: node_id, set_id: set_id}}
         when is_binary(node_id) and is_binary(set_id) and set_id != "" <-
           GroupStore.get(state.store, instance_id),
         false <- set_local?(state, node_id, instance_id),
         true <- store_reachable?(state, node_id),
         # Instance selection (Step 4): the restore must land the set on the SAME
         # instance the delegated GroupManager boot dials, since bundle sets are
         # per-instance on disk (PR-2.5). Resolve the boot's instance with the
         # IDENTICAL WakeInstance.select the Supervisor's anchor_instance uses (same
         # table, workload key, and need_mib), so both pick the same dial_id (the set
         # is not local here, so warmth matches nothing and both take the deterministic
         # cold pick). A no-eligible-instance result skips the restore (the delegated
         # wake fails/fresh-boots as it would without a restore).
         {:ok, dial_id} <- select_restore_instance(state, node_id, workload) do
      restore_set(state, dial_id, node_id, workload, set_id)
      :ok
    else
      _ -> :ok
    end
  end

  # Resolve the instance the delegated boot will dial (see maybe_restore_set), keyed
  # identically to Embervm.GroupManager.Supervisor.anchor_instance: the workload as
  # the deterministic choose key and the group's total member memory as need_mib. The
  # set is absent locally on this path, so there is no warmth owner to prefer.
  defp select_restore_instance(state, node_id, workload) do
    Embervm.WakeInstance.select(node_id,
      table: state.capacity_table,
      workload: workload,
      need_mib: group_mem_mib(state, workload)
    )
  end

  # The group's total member memory (MiB), mirroring
  # Embervm.GroupManager.Supervisor.group_mem_mib so the restore-instance pick matches
  # the boot-instance pick. 512 default per member and as the floor.
  defp group_mem_mib(state, workload) do
    members =
      case WorkloadCatalog.fetch(state.catalog_table, workload) do
        {:ok, %{group: %{members: members}}} when is_list(members) -> members
        _ -> []
      end

    case Enum.reduce(members, 0, fn m, acc -> acc + (Map.get(m, :mem_mib) || 512) end) do
      0 -> 512
      total -> total
    end
  end

  # Whether the anchor node still reports this group's bundle SET with member
  # bundles present on LOCAL disk (a reported set whose members list is non-empty).
  # True -> the local set is present, no restore needed. A locally-absent set (no
  # reported set for the group, or one reported with an empty member list) reads as
  # NOT local, the restore-on-miss case.
  defp set_local?(state, node_id, group_instance_id) do
    case NodeCapacity.fetch(state.capacity_table, node_id) do
      {:ok, fact} ->
        fact
        |> Map.get(:group_bundle_sets, [])
        |> Enum.any?(fn s ->
          s.group_instance_id == group_instance_id and Map.get(s, :members, []) != []
        end)

      :error ->
        false
    end
  end

  # The anchor node's latest object-store reachability verdict (R6). Absent/false
  # reads as NOT reachable, so no restore is attempted (the wake degrades to the
  # delegated relight's fresh-fallback); never blocks the local-state wake.
  defp store_reachable?(state, node_id) do
    case NodeCapacity.fetch(state.capacity_table, node_id) do
      {:ok, fact} -> Map.get(fact, :store_reachable, false) == true
      :error -> false
    end
  end

  # `dial_id` is the boot's selected instance (Step 4): restore the set onto it, not
  # the node-name alias. `node_id` is kept for the vendor stamp only (see the
  # stateful/serving restore notes: RestoreVendor keys on the node name, and the
  # vendor is identical across a node's instances).
  defp restore_set(state, dial_id, node_id, workload, set_id) do
    ref = %ArtifactRef{kind: :ARTIFACT_KIND_GROUP_SET, workload: workload, ref: set_id}
    req = %RestoreArtifactRequest{artifact: ref, trace: %Trace{workload: workload}}

    case safe_restore_artifact(state, dial_id, node_id, req) do
      {:ok, resp} ->
        record_restore(state, workload, set_id, resp)
        :ok

      other ->
        Logger.warning("embervm group: set restore-on-miss failed, degrading to fresh",
          workload: workload,
          set_id: set_id,
          reason: inspect(other)
        )

        :error
    end
  end

  # Dial the restore on `dial_id` (the boot's instance, Step 4) but stamp the vendor
  # off `node_id` (the node-name anchor RestoreVendor can resolve). Transport-death
  # invalidation is keyed on `dial_id` (the channel we actually dialled).
  defp safe_restore_artifact(state, dial_id, node_id, %RestoreArtifactRequest{artifact: ref} = req) do
    req = Embervm.RestoreVendor.stamp(state.capacity_table, node_id, req)

    with {:ok, channel} <- safe_channel(state.channel_fun, dial_id) do
      # The `artifact_restore` span (Task 11): a child span around the
      # RestoreArtifact RPC (the restore-on-miss read path). A group set is always
      # kind GROUP_SET; bytes-moved/skipped stamped from the response.
      Tracer.with_span "embervm.artifact_restore",
                       %{
                         attributes: %{
                           "ember.workload" => ref.workload,
                           "ember.artifact_kind" => "group_set",
                           "ember.artifact_ref" => ref.ref
                         }
                       } do
        result = restore_rpc(state, dial_id, channel, req)
        stamp_restore_span(result)
        result
      end
    end
  end

  # The RestoreArtifact RPC with transport-death channel invalidation. Extracted so
  # the `artifact_restore` span wraps exactly the call and its result. Invalidates by
  # `dial_id` (the instance the channel was dialled on, Step 4).
  defp restore_rpc(state, dial_id, channel, req) do
    try do
      case state.restore_artifact_fun.(channel, req) do
        {:error, reason} = err ->
          if Embervm.NodeChannel.transport_dead?(reason) do
            _ = state.invalidate_fun.(dial_id, channel)
          end

          err

        other ->
          other
      end
    rescue
      e -> {:error, {:restore_artifact_raised, e}}
    catch
      :exit, reason ->
        _ = state.invalidate_fun.(dial_id, channel)
        {:error, {:restore_artifact_raised, {:exit, reason}}}

      kind, reason ->
        {:error, {:restore_artifact_raised, {kind, reason}}}
    end
  end

  # Stamp bytes-moved/skipped onto the current `artifact_restore` span from a
  # successful RestoreArtifact response. A failure leaves only the identity attrs.
  defp stamp_restore_span({:ok, resp}) do
    Tracer.set_attributes(%{
      "ember.bytes_moved" => Map.get(resp, :bytes_moved, 0),
      "ember.skipped" => Map.get(resp, :skipped, false)
    })
  end

  defp stamp_restore_span(_other), do: :ok

  defp safe_channel(channel_fun, node_id) do
    channel_fun.(node_id)
  rescue
    e -> {:error, {:channel_raised, e}}
  catch
    kind, reason -> {:error, {:channel_raised, {kind, reason}}}
  end

  # Append the audit-only :artifact_restored op (no projection table). Best-effort:
  # an append failure must never fail the wake (the restored bytes on disk are the
  # durable state, not this audit row).
  defp record_restore(state, workload, set_id, resp) do
    op = %Embervm.OpLog.Op{
      kind: :artifact_restored,
      tenant: state.tenant,
      principal: wake_principal(workload),
      workload: workload,
      ts: state.clock.(),
      payload: %{
        kind: "group_set",
        ref: set_id,
        bytes_moved: Map.get(resp, :bytes_moved, 0),
        skipped: Map.get(resp, :skipped, false)
      }
    }

    _ = state.op_log_mod.append(state.op_log, op)
    :ok
  rescue
    e ->
      Logger.warning("embervm group: artifact_restored append raised", workload: workload, error: inspect(e))
      :ok
  end

  defp default_restore_artifact(channel, req) do
    Embervm.Node.V1.NodeService.Stub.restore_artifact(channel, req)
  end

  defp default_stop_group_member(channel, req) do
    Embervm.Node.V1.NodeService.Stub.stop_group_member(channel, req)
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
    {waiters, %{state | waking: Map.delete(state.waking, workload), wake_started: Map.delete(state.wake_started, workload)}}
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

    state =
      if state.node_confirmed_destroy do
        state
        |> redrive_destroying(live_members)
        |> destroy_orphan_group_vms(facts)
      else
        state
      end

    EndpointPublisher.publish(state.publisher)
    state
  end

  # A destroying group is re-driven only while its owner reports. Reported members
  # get another real StopGroupMember(DESTROY); authoritative absence from a reporting
  # owner confirms the teardown. A missing owner report leaves the group destroying.
  defp redrive_destroying(state, live_members) do
    now = state.clock.()

    destroying =
      GroupStore.all(state.store)
      |> Enum.filter(&(&1.state == :destroying))

    # Prune the alarmed set to instances still destroying (see session_manager).
    still = MapSet.new(destroying, & &1.instance_id)
    state = %{state | destroying_alarmed: MapSet.intersection(state.destroying_alarmed, still)}

    Enum.reduce(destroying, state, fn instance, acc ->
      acc = maybe_alarm_destroying(acc, instance, now)
      redrive_one_destroying(acc, instance, live_members)
    end)
  end

  # Alarm ONCE per stuck group (dedup via destroying_alarmed): every-tick logging
  # would flood SigNoz for a genuinely stuck destroy. Returns the updated state.
  defp maybe_alarm_destroying(state, instance, now) do
    id = instance.instance_id
    elapsed = now - instance.updated_at

    if elapsed > state.destroying_alarm_ms and not MapSet.member?(state.destroying_alarmed, id) do
      Logger.error("embervm group stuck in destroying",
        instance_id: id,
        workload: instance.workload,
        elapsed_ms: elapsed,
        alarm_threshold_ms: state.destroying_alarm_ms
      )

      %{state | destroying_alarmed: MapSet.put(state.destroying_alarmed, id)}
    else
      state
    end
  end

  defp redrive_one_destroying(state, instance, live_members) do
    case Map.get(live_members, instance.instance_id) do
      members when is_list(members) ->
        confirmations =
          Enum.map(members, fn member ->
            if is_binary(member.node_id) and is_binary(member.vm_id) do
              stop_group_member_destroy_confirmed(
                state,
                instance.instance_id,
                instance.workload,
                member
              )
            else
              true
            end
          end)

        if Enum.all?(confirmations) do
          record_group_destroyed(state, instance)
        else
          state
        end

      nil ->
        if node_reporting?(state, instance.node_id) do
          record_group_destroyed(state, instance)
        else
          state
        end
    end
  end

  defp record_group_destroyed(state, instance) do
    _ =
      GroupStore.transition(
        state.store,
        instance.instance_id,
        :destroy,
        :group_destroyed,
        %{reason: :destroyed},
        %{}
      )

    Logger.info("embervm group destroyed (reconcile-confirmed)",
      instance_id: instance.instance_id,
      workload: instance.workload
    )

    state
  end

  # A node-reported group member VM whose group_instance_id has no control-plane
  # row is an orphan. Group writes are synchronous, so there is no async adoption
  # discriminator: issue a plain node-confirmed destroy and never create a row.
  defp destroy_orphan_group_vms(state, facts) do
    for fact <- facts, member <- Map.get(fact, :group_member_vms, []) || [], reduce: state do
      acc ->
        cond do
          # ADR embervm/018 Phase 3: an origin-ACTIVATOR member belongs to a
          # node-relit group adopt_one heals on this same pass. Skip it explicitly
          # as a belt-and-suspenders guard for the adoption/orphan ordering window:
          # never destroy a live node-relit group member, let adoption retry.
          activator_origin?(member) ->
            acc

          GroupStore.get(acc.store, member.group_instance_id) == :error ->
            confirmed =
              if is_binary(fact.configured_id) and is_binary(member.vm_id) do
                stop_group_member_destroy_confirmed(
                  acc,
                  member.group_instance_id,
                  nil,
                  %{
                    node_id: fact.configured_id,
                    vm_id: member.vm_id,
                    member_name: member.member_name
                  }
                )
              else
                true
              end

            Logger.warning("embervm orphan group member vm destroyed",
              group_instance_id: member.group_instance_id,
              member_name: member.member_name,
              vm_id: member.vm_id,
              node_id: fact.configured_id,
              teardown_confirmed: confirmed
            )

            acc

          true ->
            acc
        end
    end
  end

  # True when a node-reported group member was relit by the brick's composite
  # activator. A pre-018 daemon reports nil / UNSPECIFIED, the CP-issued default.
  defp activator_origin?(member), do: Map.get(member, :origin) == :INSTANCE_ORIGIN_ACTIVATOR

  defp stop_group_member_destroy_confirmed(
         state,
         group_instance_id,
         workload,
         %{node_id: node_id, vm_id: vm_id, member_name: member_name}
       ) do
    req = %StopGroupMemberRequest{
      trace: %Trace{workload: workload},
      vm_id: vm_id,
      mode: :STOP_GROUP_MEMBER_MODE_DESTROY,
      set_id: "",
      member_name: member_name
    }

    dial_id =
      Embervm.WakeInstance.dial_for_group(
        state.capacity_table,
        node_id,
        group_instance_id
      )

    with {:ok, channel} <- safe_channel(state.channel_fun, dial_id) do
      try do
        match?({:ok, %{teardown_confirmed: true}}, state.stop_group_member_fun.(channel, req))
      rescue
        _ -> false
      catch
        _, _ -> false
      end
    else
      _ -> false
    end
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
          healthy: Map.get(m, :healthy, true),
          origin: Map.get(m, :origin)
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
      # A destroying instance (ADR embervm/014 decision 5) is mid node-confirmed
      # teardown; adoption must NOT re-adopt it even though the node still reports its
      # live members (the teardown RPCs are in flight). Keying off the CP state, not
      # the node report, is the fix for the TLC NoDestroyBeforeConfirm violation
      # modeled in adoption.tla. redrive_destroying (gated) owns the transition.
      instance.state == :destroying ->
        state

      # A wake stuck waking past 2 * wakeTimeoutSeconds (Task 10): the worker never
      # reported and the {:wake_timeout} timer is lost (a CP restart drops the timer
      # but the projection is rebuilt, so `waking` is empty on boot; this covers the
      # in-process wedge a timer somehow missed). Recover it as a casualty: drop the
      # stale waking bookkeeping, then fall through to the normal casualty/heal logic
      # below rather than skipping it forever. A wake within the bound still OWNS its
      # transition and is skipped (the next case).
      Map.has_key?(state.waking, instance.workload) and wake_stuck?(state, instance.workload) ->
        Logger.warning("embervm group wake stuck past bound, recovering as casualty",
          workload: instance.workload,
          instance_id: instance.instance_id
        )

        state = clear_stuck_wake(state, instance.workload)
        adopt_one(state, instance, live_members, bundle_sets)

      # An in-flight wake (within the bound) owns the workload's transition; never
      # touch it.
      Map.has_key?(state.waking, instance.workload) ->
        state

      # A `banking` instance is MID-BANK: the sweeper owns its transition. The
      # members are still node-reported live for most of the bank (they are
      # paused+snapshotted one by one), so without this skip the adopt_live
      # branch below force-flips banking -> running mid-bank and the sweeper's
      # bank_ready record then dies on {:illegal_transition, :running,
      # :bank_ready}, failing the instance AFTER noded already destroyed the
      # VMs (the 2026-07-19 bank_record_failed wedge). A bank that truly died
      # mid-flight resolves on a LATER pass: once the node stops reporting the
      # members, the complete-set/casualty branches below handle it.
      instance.state == :banking ->
        state

      # A `creating` instance with NO in-flight wake is a dead create: its owning
      # worker is gone (a CP restart mid-create, or a bound-expired wake whose
      # teardown was lost). It can never finish, and adopting whatever members DID
      # start to `running` would publish a partial group (the 1-of-3-members zombie
      # from the R6 Gate-1 drill). Roll it terminal (destroys the reported members
      # + network, releases the singleton) so the next connection creates afresh.
      instance.state == :creating ->
        Logger.warning("embervm group adoption: dead create, rolling terminal",
          workload: instance.workload,
          instance_id: instance.instance_id
        )

        teardown_expired_wake(state, instance.workload)
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
  # running, rebind every reported member's health, re-derive + force the DNAT entry
  # endpoint from the entry member's NODE-REPORTED ip (the same `{pod_ip, port_base +
  # host_offset(entry ip, supernet)}` math the live publish used), and respawn the
  # GroupManager owner. Forcing the DNAT endpoint is load-bearing: the op-log rebuild
  # reconstructs only the FALLBACK `{entry tap ip, entry guest port}`, so without this
  # a CP restart would republish a DIFFERENT endpoint. With it the republish is
  # byte-identical to the pre-restart snapshot.
  defp adopt_live(state, instance, members) do
    GroupStore.adopt_state(state.store, instance.instance_id, :running)

    for m <- members do
      _ = GroupStore.set_member_health(state.store, instance.instance_id, m.member_name, m.healthy)
    end

    _ = adopt_entry_endpoint(state, instance, members)
    _ = adopt_owner(state, instance)
    state
  end

  # Re-derive the DNAT entry endpoint's PORT from the ENTRY member's node-reported
  # ip (vm_port = port_base + host_offset(ip, supernet), the exact derivation
  # GroupManager.entry_vm_port uses) and force it into the instance, so the
  # republish equals the pre-restart publish.
  #
  # The endpoint HOST prefers the instance's already-recorded entry_ip: the publish
  # that recorded it carried the DAEMON's endpoint projection (the noded pod IP,
  # where the DNAT actually lives), and adoption must not clobber it. state.pod_ip
  # (this control plane's OWN pod IP) remains only as the legacy fallback for an
  # instance that was never published; for a split noded deployment it is known-
  # unroutable (the F-bug), but adoption preserves the old shape rather than
  # publishing nothing. A missing entry member ip, an ip outside the supernet, or
  # no host at all leaves the endpoint untouched (the fallback rebuild stands;
  # adoption never publishes a bad endpoint).
  defp adopt_entry_endpoint(state, instance, members) do
    recorded_host = Map.get(instance, :entry_ip)

    with entry_name when is_binary(entry_name) <- instance.entry_member,
         %{ip: entry_ip} when is_binary(entry_ip) and entry_ip != "" <-
           Enum.find(members, &(&1.member_name == entry_name)),
         {:ok, offset} <- host_offset(state.supernet, entry_ip),
         host when is_binary(host) and host != "" <-
           first_present([recorded_host, state.pod_ip, entry_ip]) do
      vm_port = state.port_base + offset

      if vm_port >= 1 and vm_port <= 65_535 do
        GroupStore.adopt_endpoint(state.store, instance.instance_id, host, vm_port)
      else
        :ok
      end
    else
      _ -> :ok
    end
  end

  defp first_present(candidates) do
    Enum.find(candidates, fn v -> is_binary(v) and v != "" end)
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

  # -- stuck-wake recovery (Task 10) -----------------------------------------

  # A workload is stuck iff its in-flight wake has been waking past 2 *
  # wakeTimeoutSeconds. The bound itself (start_wake's {:wake_timeout} timer) is the
  # primary release; this is the backstop for a wedge the timer missed (or a CP that
  # somehow rebuilt `waking`). No start timestamp recorded (never happens on the wake
  # path) reads as NOT stuck, so adoption defers to the timer.
  defp wake_stuck?(state, workload) do
    case Map.get(state.wake_started, workload) do
      started when is_integer(started) ->
        state.mono_clock.() - started >= 2 * wake_timeout_seconds(state, workload) * 1_000

      _ ->
        false
    end
  end

  # Drop the stale waking bookkeeping for a stuck wake and err its parked callers so
  # they stop blocking (their own retry re-enters the wake path once the workload is
  # recovered to a re-wakeable row). Single-flight is released; the reconcile then
  # heals the workload's state through the normal casualty/heal path.
  defp clear_stuck_wake(state, workload) do
    {waiters, state} = pop_waiters(state, workload)
    reply_all(waiters, {:error, {:wake_failed, :wake_stuck}})
    state
  end

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

  # The op-log owner attribution for a group's restore audit, matching
  # GroupSweeper.usage_principal/1 (a composite group is a singleton owned by one
  # principal, so per-workload == per-principal).
  defp wake_principal(workload), do: "system:group:#{workload}"

  defp schedule(msg, interval_ms) when interval_ms > 0 do
    Process.send_after(self(), msg, interval_ms)
  end

  defp schedule(_msg, _interval), do: :ok

  # Read a shared group default (supernet / port_base / pod_ip) from the app-env the
  # GroupManager.Supervisor also reads, so adoption's DNAT re-derivation uses the SAME
  # values the live publish did. Tests inject these directly via opts.
  defp group_default(key, fallback) do
    :embervm
    |> Application.get_env(:group_manager_defaults, [])
    |> Keyword.get(key, fallback)
  end

  # host_offset within the supernet: the ip's host part masked by the supernet prefix,
  # the EXACT derivation GroupManager.host_offset uses (kept in lockstep with noded's
  # serving/net.go). v1 supernets are /16, so the offset is (third_octet << 8) |
  # fourth_octet. Errors when the ip is not inside the supernet.
  defp host_offset(supernet_cidr, ip) do
    with [net, prefix_str] <- String.split(supernet_cidr, "/"),
         {prefix, ""} <- Integer.parse(prefix_str),
         {:ok, net_int} <- ip_to_int(net),
         {:ok, ip_int} <- ip_to_int(ip) do
      mask = mask_for(prefix)

      if band(net_int, mask) == band(ip_int, mask) do
        {:ok, ip_int |> band(bnot(mask)) |> band(0xFFFFFFFF)}
      else
        {:error, {:ip_outside_supernet, ip, supernet_cidr}}
      end
    else
      _ -> {:error, {:bad_supernet, supernet_cidr, ip}}
    end
  end

  defp ip_to_int(ip) do
    case ip |> String.split(".") |> Enum.map(&Integer.parse/1) do
      [{a, ""}, {b, ""}, {c, ""}, {d, ""}] -> {:ok, bsl(a, 24) + bsl(b, 16) + bsl(c, 8) + d}
      _ -> :error
    end
  end

  defp mask_for(prefix), do: band(bsl(0xFFFFFFFF, 32 - prefix), 0xFFFFFFFF)

  defp default_clock, do: System.system_time(:millisecond)

  # Monotonic ms for the wake-worker bound + the adoption stuck-check (never wall
  # time: the bound is a duration, immune to clock steps). Tests inject a fake.
  defp default_mono, do: System.monotonic_time(:millisecond)
end
