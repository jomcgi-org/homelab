defmodule Embervm.GroupSweeper do
  @moduledoc """
  The composite-group lifecycle-economics loop (R5, Task 8): the group
  generalization of `Embervm.StatefulSweeper`, and the piece that makes an idle
  composite cluster actually cost ONE bundle set on disk rather than N running
  member VMs forever. Like stateful/serving a group instance has NO per-instance
  supervised economic process (it is a set of ETS rows in `Embervm.GroupStore`; the
  `Embervm.GroupManager` owns the create/wake/bank sequence but not the timer), so
  this ONE process drives every economic decision on a timer: it scrapes each node
  Envoy's L4 connection counters for the group's ENTRY listener to detect idleness,
  banks idle groups (unpublish then drive `GroupManager.bank_group`), expires
  over-lifetime ones (destroy the group + its network), GCs stale banked sets
  (warmth-only: an expired set IS the instance's end), and answers the forced-roll
  management verb.

  ## the idle signal: the entry listener's L4 cx counts PLUS live activator splices

  A composite group contributes exactly ONE L4 entry endpoint (the entry member's
  DNAT projection), fronted by a node Envoy `tcp_proxy` listener named
  `group-<entry.listenPort>` (Task 4's `stat_prefix`). So the Envoy idle signal is
  that ONE listener's per-connection stats, exactly as stateful reads `state-<port>`:

    * `downstream_cx_active`: currently-open connections to the entry. MUST be zero
      before a bank is considered (never sever a live connection to bank).
    * `downstream_cx_total`: the cumulative connection count; a zero delta across
      `idleBankSeconds` means no NEW connection arrived either.

  Intra-group traffic (member-to-member chatter on the private /24 bridge) is
  invisible to Envoy BY CONSTRUCTION (it never crosses the entry listener), and
  correctly does NOT count as activity: the entry-listener cx counts are the right
  (and only) Envoy signal.

  BUT the entry listener's cx counts miss one thing the control plane itself owns: a
  long-lived session SPLICED by `Embervm.TcpActivator` during a wake. The activator
  dials the entry member directly and byte-pumps for the life of the session; that
  connection never re-enters the node Envoy's entry-listener `cx_active` counter, so
  a group with a live splice can read `cx_active == 0` and LOOK idle. So the GROUP
  idle predicate ADDs a third clause the stateful one lacks: zero live activator
  splices (`Embervm.ActivatorSplices.live?/2`, the control plane's own count of
  in-flight splices for this workload). A group is idle-bankable iff
  `cx_active == 0` AND the `cx_total` delta is `0` AND there are zero live activator
  splices.

  ## degraded groups are EXCLUDED from banking (decision 11), visibly

  A group carrying a `degraded_member` flag (a member fell unhealthy while the group
  stayed up) is NEVER banked: a bank snapshots the WHOLE set, but a degraded group is
  missing a live member, so a bank would either write a PARTIAL set (violating the
  all-or-nothing set invariant, decision 3) or capture a broken group. The exclusion
  is a LOGGED, VISIBLE fact (a `bank_excluded_degraded` log line per idle-but-degraded
  group), and the group stays live until it is HEALED (a forced roll, or a member
  recovering) or EXPIRED (max-lifetime). It is exactly the convergence lever the
  forced roll below exists to pull.

  ## fail-open for warmth (ADR embervm/001), exactly as stateful/serving

  A scrape failure yields no reading for that node this tick: idle detection is
  suppressed for every composite workload on that node, never banking on missing or
  stale stats. Lifetime/TTL GC still run (they read the durable store). A fresh
  sweeper's first tick only baselines (no prior reading to delta against).

  ## the bank sequence (no drain window, standing decision 7)

  Like stateful, an L4 group connection has no natural completion boundary, so the
  precondition IS the wait: banking only begins once `cx_active` has read zero for
  the whole idle window AND no activator splice is live. The sequence:

    1. mark the instance `running -> banking` via the GroupManager's bank drive is
       preceded by an UNPUBLISH here: the sweeper drops the entry endpoint
       (`GroupStore` bank_ready drops it, but the entry must leave the fan-out BEFORE
       the members are paused so a racing connection parks on the activator, not a
       dead socket). Concretely the sweeper re-publishes with the group still
       `running` but flagged banking so `EndpointPublisher` swaps in the activator;
       the actual member pause/snapshot/destroy + the atomic `group_banked` run in
       `GroupManager.bank_group`;
    2. RECHECK the freshest `cx_active` + the splice count: if a connection or splice
       raced in, ABORT (never begin the bank) and leave the group live;
    3. drive `GroupManager.bank_group` (pause all, snapshot all to a shared set_id,
       atomic `group_banked`, destroy all), off this process's own call (it blocks
       until the whole set banks or aborts, which is acceptable at the sweep cadence
       and keeps the durable append serialized). On a clean bank the group is
       `banked` with the set on disk; on an abort the group is back `running` and we
       re-publish so the still-live group re-enters the fan-out.

  ## max-lifetime: destroy the group AND its network (decision 8)

  A group older than `maxLifetimeSeconds` must go so a stale image lineage cannot
  squat. A live one waits for `cx_active == 0` up to a capped patience window
  (`EMBERVM_GROUP_LIFETIME_DRAIN_MAX_MS`, default 1 hour) then destroys anyway
  (unlike stateful there is no durable volume to recover against, so the honest thing
  is a hard cap then a fresh boot on the next connection). Destroying a group tears
  down every member VM AND deletes the private /24 network (the bridge leaks
  otherwise). A banked over-lifetime instance is destroyed immediately (it holds no
  VMs; its set is evicted as part of the terminal).

  ## banked-TTL GC: warmth-only, the set IS the instance's end

  A banked set untouched (`last_active_at`, else `updated_at`) longer than
  `bankedTtlSeconds` is GC'd. Unlike R4 stateful (where an evicted bundle leaves the
  durable VOLUME as a floor, so the instance survives the eviction), a composite
  group has NO volume floor: an expired set IS the instance's end. So banked-TTL here
  DESTROYS the instance (`group_destroyed{reason: expired}`) and best-effort evicts
  each member's bundle, rather than leaving a banked-with-no-set row. The next
  connection is a full fresh CREATE.

  ## forced roll: the convergence + degraded-recovery lever

  `DELETE /v1/groups/{workload}/instance` (management auth) drives `force_roll/2`:
  destroy every live member + delete the network + EVICT the banked set, but KEEP the
  workload DEFINITION (the CR / catalog entry). The next connection fresh-boots a new
  environment on the CURRENT images. This is both how an operator rolls a group onto
  new images AND how a stuck degraded group is recovered (a degraded group is never
  banked, so the roll is the only way to shed it short of lifetime expiry).
  """

  use GenServer
  require Logger

  require OpenTelemetry.Tracer, as: Tracer

  alias Embervm.{ActivatorSplices, GroupState, GroupStore, NodeCapacity, WorkloadCatalog}
  alias Embervm.OpLog.Op

  alias Embervm.Node.V1.{
    ArtifactRef,
    DeleteGroupNetworkRequest,
    EvictArtifactRequest,
    EvictSnapshotRequest,
    StopGroupMemberRequest,
    Trace
  }

  # The capped patience window for a live over-lifetime group to drain (entry
  # cx_active reach zero) before it is destroyed anyway (decision 8). One hour
  # default, mirroring StatefulSweeper's lifetime_drain_max_ms.
  @default_lifetime_drain_max_ms 3_600_000

  # -- Client API ------------------------------------------------------------

  @spec start_link(keyword()) :: GenServer.on_start()
  def start_link(opts) do
    case Keyword.get(opts, :name, __MODULE__) do
      nil -> GenServer.start_link(__MODULE__, opts)
      name -> GenServer.start_link(__MODULE__, opts, name: name)
    end
  end

  @doc """
  Runs one full sweep synchronously (scrape + group_stats + idle-bank + recheck +
  lifetime GC + banked-TTL GC) and returns after it completes. Tests drive the
  economics deterministically through this (with an injected clock + stats seam)
  instead of waiting on the timer.
  """
  @spec sweep(GenServer.server()) :: :ok
  def sweep(server \\ __MODULE__) do
    GenServer.call(server, :sweep, :infinity)
  end

  @doc """
  Forced roll (management verb behind `DELETE /v1/groups/{workload}/instance`):
  destroy every live member VM of the workload's group, delete its private network,
  and evict its banked set, KEEPING the workload definition so the next connection
  fresh-boots a new environment on the current images. Synchronous; returns
  `%{destroyed: n, evicted: m}` (n = live instances torn down, m = banked sets
  evicted; each 0 or 1, the class is a group-level singleton). The convergence +
  degraded-recovery lever.
  """
  @spec force_roll(GenServer.server(), String.t()) :: %{destroyed: non_neg_integer(), evicted: non_neg_integer()}
  def force_roll(server \\ __MODULE__, workload) do
    GenServer.call(server, {:force_roll, workload}, :infinity)
  end

  # -- GenServer callbacks ---------------------------------------------------

  @impl true
  def init(opts) do
    state = %{
      store: Keyword.get(opts, :store, GroupStore),
      publisher: Keyword.get(opts, :publisher, Embervm.EndpointPublisher),
      capacity_table: Keyword.get(opts, :capacity_table, NodeCapacity.table()),
      catalog_table: Keyword.get(opts, :catalog_table, WorkloadCatalog.table()),
      op_log: Keyword.get(opts, :op_log, Embervm.OpLog.SQLite),
      # The backend module dispatched below, threaded alongside :op_log (the
      # server address) so a non-default backend never requires editing this
      # module. Defaults to the same SQLite module :op_log defaults to.
      op_log_mod: Keyword.get(opts, :op_log_mod, Embervm.OpLog.SQLite),
      clock: Keyword.get(opts, :clock, &default_clock/0),
      tenant: Keyword.get(opts, :tenant, "homelab"),
      # The node Envoy stats scrape seam (same shape + endpoint as stateful): (url) ->
      # {:ok, %{stat_prefix => %{active, total}}} | {:error, reason}. Composite entry
      # listeners live on the SAME node Envoy admin the serving/stateful scrape reaches.
      scrape_fun: Keyword.get(opts, :scrape_fun, &default_scrape/1),
      stats_base: Keyword.get(opts, :stats_base, nil),
      # The activator live-splice table name: ActivatorSplices.live?(table, workload)
      # is the third idle clause (zero live splices). nil reads no-splice (a sweeper
      # wired without an activator never blocks banking on a signal it does not track).
      splices_table: Keyword.get(opts, :splices_table, ActivatorSplices),
      # The whole-set bank drive seam: (workload, instance_id) -> {:ok, %{set_id,
      # banked, pause_spread_ms}} | {:error, reason}. Production drives
      # GroupManager.Supervisor.bank_group/2 (the per-instance manager pauses/snapshots/
      # destroys the members and records the set atomically); tests inject a recorder.
      bank_fun: Keyword.get(opts, :bank_fun, &default_bank/2),
      # The daemon group-verb seams for lifetime/forced-roll destroys (a banked group
      # has no live GroupManager process, so the sweeper destroys directly, mirroring
      # StatefulSweeper). Injected for tests; production dials the real NodeService stub.
      channel_fun: Keyword.get(opts, :channel_fun, &Embervm.NodeChannel.get/1),
      stop_group_member_fun: Keyword.get(opts, :stop_group_member_fun, &default_stop_group_member/2),
      delete_group_network_fun:
        Keyword.get(opts, :delete_group_network_fun, &default_delete_group_network/2),
      evict_snapshot_fun: Keyword.get(opts, :evict_snapshot_fun, &default_evict_snapshot/2),
      # Remote artifact eviction seam (R6, Task 9): (channel, %EvictArtifactRequest{})
      # -> {:ok, %EvictArtifactResponse{}} | {:error, _}. Fired alongside the local
      # per-member EvictSnapshot so the store copy of the whole banked SET is dropped
      # on the same trigger (banked TTL, forced roll). Injected for tests; production
      # dials the real NodeService stub.
      evict_artifact_fun: Keyword.get(opts, :evict_artifact_fun, &default_evict_artifact/2),
      # status.group {state, members{live,degraded}, setId, subnetCidr} + groupSummary
      # writer (Task 9). Defaults to the K8s merge-patch on the workload status
      # subresource, the SAME seam StatefulSweeper.status_writer uses; tests inject a
      # recorder. Disjoint status keys (group/groupSummary) from
      # status.stateful/serving/sessions and the watcher, so the merge-patch never
      # clobbers another writer.
      status_writer: Keyword.get(opts, :status_writer, &Embervm.K8s.patch_workload_status/3),
      # workload -> last-written {state, live, degraded, set_id, subnet_cidr} tuple, so
      # the sweep patches only a workload whose values changed (debounce: at most one
      # API call per composite workload per sweep, never per transition). Mirrors
      # StatefulSweeper.stateful_status_written.
      group_status_written: %{},
      lifetime_drain_max_ms: Keyword.get(opts, :lifetime_drain_max_ms, @default_lifetime_drain_max_ms),
      # workload -> the ms it was first observed idle this run (cx_active == 0 AND flat
      # cx_total delta AND no live splice), so idleBankSeconds measures from the first
      # idle tick. Any activity resets it.
      idle_since: %{},
      # workload -> the ms live-seconds were last charged, so each tick bills exactly
      # the elapsed window (now - last) x per-member compute (no double-count, no gap).
      # A workload first seen live baselines here and bills from the NEXT tick.
      last_charged_at: %{},
      # workload -> the ms an over-lifetime LIVE group was first seen both
      # over-lifetime and with an active entry connection (the drain patience baseline).
      lifetime_drain_since: %{},
      # The prior tick's per-node reading: node_id -> %{stat_prefix => %{active, total}}.
      last_stats: %{},
      # This tick's per-node reading (freshest), consulted by the recheck + lifetime pass.
      current_stats: %{},
      sweep_interval_ms: Keyword.get(opts, :sweep_interval_ms, 0)
    }

    if state.sweep_interval_ms > 0 do
      schedule(:sweep, state.sweep_interval_ms)
    end

    {:ok, state}
  end

  @impl true
  def handle_call(:sweep, _from, state) do
    {:reply, :ok, do_sweep(state)}
  end

  def handle_call({:force_roll, workload}, _from, state) do
    {reply, state} = do_force_roll(state, workload)
    {:reply, reply, state}
  end

  def handle_call({:drain_node, node_id}, _from, state) do
    {count, state} = drain_bank_node(state, node_id)
    {:reply, count, state}
  end

  @impl true
  def handle_info(:sweep, state) do
    state = do_sweep(state)
    schedule(:sweep, state.sweep_interval_ms)
    {:noreply, state}
  end

  def handle_info(_msg, state), do: {:noreply, state}

  # -- the sweep -------------------------------------------------------------

  # One tick: (1) scrape every composite-capable node's entry-listener stats, append
  # group_stats + touch activity; (2) idle-bank: unpublish + recheck + bank drive for
  # groups confirmed idle past idleBankSeconds (degraded groups EXCLUDED); (3)
  # max-lifetime expiry (drain-then-destroy, capped patience; destroy group + network);
  # (4) banked-TTL GC (warmth-only terminal).
  defp do_sweep(state) do
    now = state.clock.()

    Tracer.with_span "embervm.group.stats_sweep", %{attributes: %{"ember.tenant" => state.tenant}} do
      state
      |> scrape_and_record(now)
      |> charge_live_seconds(now)
      |> idle_bank_pass(now)
      |> sweep_lifetime(now)
      |> sweep_banked_ttl(now)
      |> gc_orphan_networks()
      |> write_group_status()
    end
  end

  # -- orphan group-network GC -------------------------------------------------

  # Delete node-held group networks whose instance is terminal or absent. A
  # teardown that dies before DeleteGroupNetwork (a failed bank, a dead channel
  # mid-forced-roll, a noded that restarted and re-adopted its on-disk record)
  # leaves the bridge record squatting the composite CIDR, and every later
  # create fails with "cidr overlaps existing group" until an operator deletes
  # the record by hand - three times on 2026-07-18/19 alone. Best-effort: an
  # RPC failure just leaves the orphan for the next sweep.
  defp gc_orphan_networks(state) do
    for fact <- NodeCapacity.all(state.capacity_table),
        net <- Map.get(fact, :group_networks, []) || [] do
      instance_id = Map.get(net, :group_instance_id)

      orphaned? =
        case GroupStore.get(state.store, instance_id) do
          {:ok, instance} -> GroupState.terminal?(instance.state)
          # GroupStore.get returns a BARE :error (not {:error, _}) when the instance
          # is absent from the store (see get_view/2, and group_manager.ex which
          # matches :error the same way). An absent instance IS orphaned: its network
          # record outlived its group, which is exactly what this GC collects. The old
          # {:error, _} clause matched neither {:ok,_} nor a bare :error, so the sweep
          # crashed with a CaseClauseError PRECISELY when there was an orphan to clean,
          # crash-looping the sweeper every tick and letting bridge records pile up
          # ("cidr overlaps existing group").
          :error -> true
        end

      if orphaned? and is_binary(instance_id) and instance_id != "" do
        Logger.warning("embervm group: GC deleting orphan group network",
          instance_id: instance_id,
          node_id: fact.configured_id,
          cidr: Map.get(net, :cidr)
        )

        _ = delete_network(state, %{instance_id: instance_id, node_id: fact.configured_id})
      end
    end

    state
  end

  # -- status.group (Task 9) --------------------------------------------------

  # Write status.group {state, members{live,degraded}, setId, subnetCidr} +
  # groupSummary for every composite-class workload, DEBOUNCED: only patch a workload
  # whose values changed since the last write. Runs on the sweep tick (not per
  # transition), so the K8s API is touched at most once per composite workload per
  # sweep. Disjoint status keys (group/groupSummary) from the other writers, so the
  # merge-patch never clobbers another. Mirrors StatefulSweeper.write_stateful_status/1.
  defp write_group_status(state) do
    composite_workloads_with_ns(state)
    |> Enum.reduce(state, fn %{workload: workload, namespace: namespace}, acc ->
      fields = group_status_fields(acc, workload)
      key = {fields.state, fields.live, fields.degraded, fields.set_id, fields.subnet_cidr}

      if Map.get(acc.group_status_written, workload) == key do
        acc
      else
        _ = patch_group_status(acc, namespace, workload, fields)
        %{acc | group_status_written: Map.put(acc.group_status_written, workload, key)}
      end
    end)
  end

  # Every cataloged composite-class workload with its namespace, for the status write.
  # A catalog with no composite workloads yields [], a no-op on non-composite clusters.
  # Mirrors StatefulSweeper.stateful_workloads_with_ns/1.
  defp composite_workloads_with_ns(state) do
    for name <- WorkloadCatalog.all_names(state.catalog_table),
        {:ok, %{class: "composite", namespace: namespace}} <- [WorkloadCatalog.fetch(state.catalog_table, name)],
        is_binary(namespace) do
      %{workload: name, namespace: namespace}
    end
  end

  # Reads the primary (non-terminal) instance's group facts from GroupStore: the FSM
  # state string (with `degraded` derived from the degraded_member flag on a running
  # group, and `expired` folded into `destroyed` per the CRD contract), the live /
  # degraded member health counts (from the instance's member rows), the banked
  # set_id (present only while banked), and the group's private subnet. Every field
  # defaults to a concrete value ("" for strings, 0 for counts) when absent, so the
  # status-patch map never carries a nil (the router's `:json.encode` nil trap: OTP
  # renders Elixir nil as the STRING "nil"). WARMTH-ONLY (the CRD contract): reflects
  # live state, evaporates on destroy/TTL/fresh-boot.
  defp group_status_fields(state, workload) do
    instances = GroupStore.list(state.store, workload)
    primary = Enum.find(instances, &(not GroupState.terminal?(&1.state)))

    case primary do
      nil ->
        %{state: "", live: 0, degraded: 0, set_id: "", subnet_cidr: ""}

      instance ->
        {live, degraded} = member_health_counts(state, instance)

        %{
          state: group_state_string(instance),
          live: live,
          degraded: degraded,
          set_id: instance.set_id || "",
          subnet_cidr: instance.subnet_cidr || ""
        }
    end
  end

  # The status `state` string: `degraded` when a running instance carries a
  # degraded_member flag (the CRD names this as its own status state, distinct from the
  # FSM's `running`), else the FSM state. `banking`/`relighting`/`fresh_booting` are
  # transient ETS-only states the projection never persists, but a mid-transition read
  # could surface one; they map through Atom.to_string unchanged (all valid CRD status
  # enum values). A never-yet-booted workload has no instance and reads "" (handled by
  # the nil branch above).
  defp group_state_string(%{state: :running, degraded_member: dm}) when is_binary(dm), do: "degraded"
  defp group_state_string(%{state: fsm_state}), do: Atom.to_string(fsm_state)

  # Live vs degraded member counts across the instance's member rows. A member is live
  # iff it holds a live vm_id AND is healthy; degraded iff it holds a live vm_id but is
  # unhealthy (a member that fell over while the group stayed up). Members with no live
  # vm_id (a banked instance cleared them) count as neither, so a banked group reads
  # 0/0, consistent with its "" set-of-live-VMs.
  defp member_health_counts(state, instance) do
    GroupStore.members(state.store, instance.instance_id)
    |> Enum.filter(fn m -> is_binary(m.vm_id) and m.vm_id != "" end)
    |> Enum.reduce({0, 0}, fn m, {live, degraded} ->
      if m.healthy == false, do: {live, degraded + 1}, else: {live + 1, degraded}
    end)
  end

  defp patch_group_status(state, namespace, name, fields) do
    status_map = %{
      "group" => %{
        "state" => fields.state,
        "members" => %{"live" => fields.live, "degraded" => fields.degraded},
        "setId" => fields.set_id,
        "subnetCidr" => fields.subnet_cidr
      },
      "groupSummary" => "#{fields.state} #{fields.live}/#{fields.degraded}"
    }

    case state.status_writer.(namespace, name, status_map) do
      :ok ->
        :ok

      {:error, reason} ->
        # Visibility-only: a status-write failure must never crash the sweep.
        Logger.warning("embervm group status patch failed for #{namespace}/#{name}: #{inspect(reason)}")
    end
  rescue
    e -> Logger.warning("embervm group status patch raised for #{namespace}/#{name}: #{inspect(e)}")
  catch
    _, _ -> :ok
  end

  # -- usage: per-member live-seconds (decision 9) ---------------------------

  # Charge every LIVE (non-terminal, non-banked) composite group its compute usage for
  # the elapsed window: a composite bills PER-MEMBER live-seconds (a 3-member group
  # bills 3 VMs' worth), unlike serving/stateful which count connection/request units.
  # The group_stats projection expects `%{usage: %{vcpu_seconds, gb_seconds},
  # member_count}` and multiplies, so the op carries ONE member's live-seconds for the
  # window plus the member count. A workload first seen live baselines its clock this
  # tick (bills from the NEXT tick, never a lump-sum from creation). A workload with no
  # live instance drops its clock so a later boot re-baselines.
  defp charge_live_seconds(state, now) do
    live_workloads =
      GroupStore.all(state.store)
      |> Enum.filter(&GroupState.live?(&1.state))
      |> Enum.map(& &1.workload)
      |> Enum.uniq()

    live_set = MapSet.new(live_workloads)

    state =
      Enum.reduce(live_workloads, state, fn workload, acc ->
        charge_one_workload(acc, workload, now)
      end)

    # Prune the charge clock for workloads no longer live (a re-boot re-baselines).
    %{state | last_charged_at: Map.take(state.last_charged_at, MapSet.to_list(live_set))}
  end

  defp charge_one_workload(state, workload, now) do
    case Map.get(state.last_charged_at, workload) do
      nil ->
        # First tick a live workload is seen: baseline only (bill from next tick).
        %{state | last_charged_at: Map.put(state.last_charged_at, workload, now)}

      last ->
        window_ms = max(now - last, 0)

        if window_ms > 0 do
          _ = append_group_stats(state, workload, window_ms, now)
        end

        %{state | last_charged_at: Map.put(state.last_charged_at, workload, now)}
    end
  end

  # -- stats scrape + activity -----------------------------------------------

  # Scrape each composite-capable node's Envoy stats keyed by listener stat_prefix
  # (group-<listen_port>), map each prefix back to its workload via the catalog, and:
  # touch last_active_at + reset the idle clock on cx activity (a new connection, an
  # open connection, or a live activator splice), else mark the idle baseline. Usage
  # billing is a SEPARATE per-tick live-seconds charge (charge_live_seconds/2), not tied
  # to the cx delta. A failed node scrape drops its prior reading (fail-open).
  defp scrape_and_record(state, now) do
    nodes = group_nodes(state)

    {state, last_stats, current_stats} =
      Enum.reduce(nodes, {state, state.last_stats, %{}}, fn node, {state_acc, last_acc, current_acc} ->
        case scrape_node(state_acc, node) do
          {:ok, reading} ->
            prior = Map.get(state_acc.last_stats, node.configured_id)
            state_acc = record_reading(state_acc, node, prior, reading, now)
            {state_acc, Map.put(last_acc, node.configured_id, reading), Map.put(current_acc, node.configured_id, reading)}

          {:error, reason} ->
            Logger.debug("embervm group: stats scrape failed",
              node_id: node.configured_id,
              reason: inspect(reason)
            )

            {state_acc, Map.delete(last_acc, node.configured_id), current_acc}
        end
      end)

    scraped_ids = MapSet.new(nodes, & &1.configured_id)
    last_stats = Map.take(last_stats, MapSet.to_list(scraped_ids))

    %{state | last_stats: last_stats, current_stats: current_stats}
  end

  defp record_reading(state, _node, nil, _reading, _now), do: state

  defp record_reading(state, node, prior, reading, now) do
    Enum.reduce(reading, state, fn {stat_prefix, %{active: active, total: total}}, acc ->
      workload = workload_of_stat_prefix(acc, stat_prefix)
      prior_entry = Map.get(prior, stat_prefix)
      record_one_reading(acc, node, workload, prior_entry, active, total, now)
    end)
  end

  defp record_one_reading(state, _node, nil, _prior, _active, _total, _now), do: state
  defp record_one_reading(state, _node, _workload, nil, _active, _total, _now), do: state

  defp record_one_reading(state, _node, workload, %{total: prior_total}, active, total, now) do
    delta = total - prior_total
    splice_live? = ActivatorSplices.live?(state.splices_table, workload)

    cond do
      delta < 0 ->
        # Counter reset (Envoy restarted): re-baseline, reset the idle clock.
        clear_idle_since(state, workload)

      delta > 0 or active > 0 or splice_live? ->
        # Activity: a new connection (delta > 0), an open Envoy connection (active > 0),
        # OR a live activator splice (invisible to Envoy but still activity). Touch
        # active + clear the idle clock either way. (Usage billing is a SEPARATE
        # per-tick live-seconds charge, see charge_live_seconds/2: a composite bills
        # compute-time per member, not connection count, so the cx delta here is ONLY
        # the activity/idle signal.)
        touch_workload_active(state, workload, now)
        clear_idle_since(state, workload)

      true ->
        # active == 0, delta == 0, AND no live splice: confirmed idle this tick.
        mark_idle_since(state, workload, now)
    end
  end

  defp mark_idle_since(state, workload, now) do
    if Map.has_key?(state.idle_since, workload) do
      state
    else
      %{state | idle_since: Map.put(state.idle_since, workload, now)}
    end
  end

  defp clear_idle_since(state, workload) do
    %{state | idle_since: Map.delete(state.idle_since, workload)}
  end

  # Append a group_stats op billing the WHOLE group's compute for `window_ms`: the sum
  # over every member of {vcpus, mem_gib} x window_seconds, carried as the op's `usage`
  # with `member_count: 1` (the projection multiplies usage x member_count, so passing
  # the already-summed group total with count 1 accrues exactly the group's true
  # per-window compute, heterogeneous member sizes included). group_instance_id is nil
  # (workload-scoped, like stateful_stats). Best-effort: a usage-op failure must never
  # wedge the sweep. A workload whose catalog group has no members bills nothing.
  defp append_group_stats(state, workload, window_ms, now) do
    case group_cfg(state, workload) do
      {:ok, cfg} ->
        window_s = window_ms / 1000
        {vcpu_seconds, gb_seconds} = group_compute_seconds(cfg, window_s)

        if vcpu_seconds > 0 or gb_seconds > 0 do
          op = %Op{
            kind: :group_stats,
            tenant: state.tenant,
            principal: usage_principal(workload),
            workload: workload,
            group_instance_id: nil,
            ts: now,
            payload: %{usage: %{vcpu_seconds: vcpu_seconds, gb_seconds: gb_seconds}, member_count: 1}
          }

          _ = state.op_log_mod.append(state.op_log, op)
        end

        :ok

      :error ->
        :ok
    end
  rescue
    e ->
      Logger.warning("embervm group: group_stats append raised", workload: workload, error: inspect(e))
      :ok
  end

  # The group's summed compute for a window: over every EXPANDED member (replicas
  # counted), window_seconds x {vcpus, mem_gib}. Mirrors the member_plan expansion
  # (replicas > 1 -> N copies), so a 2-replica member bills 2 VMs' worth. Defaults a
  # missing vcpus to 1 and mem to 512 MiB (the GroupManager StartGroupMember defaults),
  # so an under-specified member still bills a sane floor.
  defp group_compute_seconds(cfg, window_s) do
    members = Map.get(cfg, :members, []) || []

    Enum.reduce(members, {0.0, 0.0}, fn member, {vcpu_acc, gb_acc} ->
      replicas = member_replicas(member)
      vcpus = (Map.get(member, :vcpus) || 1) * replicas
      mem_gib = (Map.get(member, :mem_mib) || 512) / 1024 * replicas
      {vcpu_acc + vcpus * window_s, gb_acc + mem_gib * window_s}
    end)
  end

  defp member_replicas(member) do
    case Map.get(member, :replicas) do
      n when is_integer(n) and n > 0 -> n
      _ -> 1
    end
  end

  defp touch_workload_active(state, workload, now) do
    GroupStore.list(state.store, workload)
    |> Enum.filter(&GroupState.live?(&1.state))
    |> Enum.each(fn instance ->
      _ = GroupStore.touch_active(state.store, instance.instance_id, now)
    end)
  end

  # -- idle-bank pass ---------------------------------------------------------

  # For every RUNNING group whose workload has been confirmed idle for at least
  # idleBankSeconds AND is NOT degraded: begin the bank. A degraded group is EXCLUDED
  # (decision 11) with a visible log line. A workload with no confirmed-idle reading
  # this tick (a scrape failed, first reading, activity, or a live splice) is
  # untouched: fail-open for warmth.
  defp idle_bank_pass(state, now) do
    GroupStore.all(state.store)
    |> Enum.filter(&(&1.state == :running))
    |> Enum.reduce(state, fn instance, acc -> maybe_begin_bank(acc, instance, now) end)
  end

  defp maybe_begin_bank(state, instance, now) do
    with {:ok, cfg} <- group_cfg(state, instance.workload),
         since when is_integer(since) <- Map.get(state.idle_since, instance.workload) do
      idle_ms = cfg.idle_bank_seconds * 1000

      cond do
        now - since < idle_ms ->
          state

        degraded?(instance) ->
          # A degraded group is EXCLUDED from banking (decision 11): a partial set
          # would violate all-or-nothing. Make the exclusion a visible, logged fact;
          # the group stays live until healed (forced roll / member recovery) or
          # expired (max-lifetime).
          Logger.info("embervm group: bank excluded, group degraded",
            workload: instance.workload,
            instance_id: instance.instance_id,
            degraded_member: instance.degraded_member
          )

          state

        true ->
          begin_bank(state, instance)
      end
    else
      _ -> state
    end
  end

  # Begin the bank (mirrors StatefulSweeper's unpublish-then-recheck-then-bank):
  # move running -> banking (which drops the entry endpoint from the fan-out, since
  # entry_endpoint/2 only serves a `running` group) and install the activator in the
  # SAME publish, so a racing connection parks on the activator, not a VM about to be
  # paused (decision 7). Then RECHECK the freshest cx_active + splice count: a race
  # ABORTS (banking -> running) and republishes; otherwise drive the whole-set bank.
  defp begin_bank(state, instance) do
    Tracer.with_span "embervm.group.drain",
                     %{attributes: %{"ember.workload" => instance.workload, "ember.instance_id" => instance.instance_id}} do
      begin_bank_body(state, instance)
    end
  end

  defp begin_bank_body(state, instance) do
    case GroupStore.mark(state.store, instance.instance_id, :bank) do
      {:ok, _} ->
        # The endpoint left the fan-out (banking != running); install the activator.
        Embervm.EndpointPublisher.publish(state.publisher)
        recheck_and_bank(state, instance)

      {:error, reason} ->
        Logger.warning("embervm group: bank-drain mark failed",
          workload: instance.workload,
          instance_id: instance.instance_id,
          reason: inspect(reason)
        )

        state
    end
  end

  # The decision-7 recheck: re-scrape the node NOW and re-read the splice count to
  # catch a connection/splice that opened since the tick-start idle scan. A race
  # ABORTS (banking -> running) and republishes so the still-live group re-enters the
  # fan-out; otherwise drive the whole-set bank via the GroupManager.
  defp recheck_and_bank(state, instance) do
    state = refresh_current_stats(state, instance.node_id)

    if connection_raced_in?(state, instance) or ActivatorSplices.live?(state.splices_table, instance.workload) do
      abort_bank(state, instance, :recheck_active)
    else
      drive_bank(state, instance)
    end
  end

  # A racing connection/splice: abort the bank (banking -> running) and republish so
  # the still-live group re-enters the fan-out, never severing a live connection.
  defp abort_bank(state, instance, reason) do
    _ = GroupStore.mark(state.store, instance.instance_id, :bank_abort)
    Embervm.EndpointPublisher.publish(state.publisher)

    Logger.info("embervm group: bank aborted, republished",
      workload: instance.workload,
      instance_id: instance.instance_id,
      reason: reason
    )

    state
  end

  # Drive the whole-set bank via the GroupManager (StopGroupMember BANK every member
  # under a shared set_id, atomic group_banked). The instance is already `banking`
  # (begin_bank_body moved it there). A clean bank leaves it `banked` with the entry
  # endpoint dropped (the manager re-published the activator swap); an abort/failure
  # returns it to `running` (the manager's bank_abort), so re-derive the fan-out so
  # the live group is reachable again.
  @doc """
  Force-bank every running group on a draining node (R6, ADR embervm/009).

  Called by the DrainCoordinator on the drain edge. Banks each `:running` group as a
  unit (the all-members-or-none bundle-set contract) via the existing drive_bank
  path, so a routine roll never destroys a banked group. Unlike the idle pass it
  ignores idle age; degraded groups still bank (a preempted node's members are lost
  either way, so a best-effort set is strictly better than none). Returns the count
  of groups whose bank was driven.
  """
  @spec drain_node(GenServer.server(), String.t()) :: non_neg_integer()
  def drain_node(server \\ __MODULE__, node_id) do
    GenServer.call(server, {:drain_node, node_id}, :infinity)
  end

  defp drain_bank_node(state, node_id) do
    instances =
      GroupStore.all(state.store)
      |> Enum.filter(&(&1.state == :running and &1.node_id == node_id))

    state = Enum.reduce(instances, state, fn instance, acc -> drive_bank(acc, instance) end)
    {length(instances), state}
  end

  defp drive_bank(state, instance) do
    case state.bank_fun.(instance.workload, instance.instance_id) do
      {:ok, %{set_id: set_id, pause_spread_ms: spread}} ->
        Logger.info("embervm group banked",
          workload: instance.workload,
          instance_id: instance.instance_id,
          set_id: set_id,
          pause_spread_ms: spread
        )

        state

      {:error, reason} ->
        Logger.warning("embervm group: bank drive failed, group left live",
          workload: instance.workload,
          instance_id: instance.instance_id,
          reason: inspect(reason)
        )

        Embervm.EndpointPublisher.publish(state.publisher)
        state
    end
  end

  defp refresh_current_stats(state, node_id) do
    case scrape_node(state, %{configured_id: node_id}) do
      {:ok, reading} -> %{state | current_stats: Map.put(state.current_stats, node_id, reading)}
      {:error, _reason} -> state
    end
  end

  # True when the instance's node's freshest reading reports cx_active > 0 for its
  # entry listener. No fresh reading fails OPEN toward proceeding (the idle
  # confirmation that got here already required cx_active == 0 on the last successful
  # scrape, and the splice-count recheck is a separate independent guard).
  defp connection_raced_in?(state, instance) do
    with {:ok, entry} <- Map.fetch(state.current_stats, instance.node_id),
         {:ok, cfg} <- group_cfg(state, instance.workload),
         {:ok, %{active: active}} <- Map.fetch(entry, stat_prefix(cfg.entry.listen_port)) do
      active > 0
    else
      _ -> false
    end
  end

  # -- max-lifetime expiry -----------------------------------------------------

  # A group older than maxLifetimeSeconds is destroyed. A banked one is destroyed
  # immediately (it holds no VMs; its set is evicted as part of the terminal). A live
  # one waits for entry cx_active == 0 up to the capped patience window (decision 8),
  # tracked from the first tick it was BOTH over-lifetime and active; once the window
  # elapses, destroy anyway (a fresh boot on the next connection covers it, there is
  # no volume floor to recover against).
  defp sweep_lifetime(state, now) do
    GroupStore.all(state.store)
    |> Enum.reject(&GroupState.terminal?(&1.state))
    |> Enum.filter(&over_lifetime?(state, &1, now))
    |> Enum.reduce(state, fn instance, acc -> expire_instance(acc, instance, now) end)
  end

  defp over_lifetime?(state, instance, now) do
    case group_cfg(state, instance.workload) do
      {:ok, cfg} ->
        max_ms = cfg.max_lifetime_seconds * 1000
        is_integer(instance.created_at) and now - instance.created_at >= max_ms

      :error ->
        false
    end
  end

  defp expire_instance(state, %{state: :banked} = instance, _now) do
    state = clear_lifetime_drain_since(state, instance.workload)
    destroy_banked(state, instance, "expired")
  end

  defp expire_instance(state, instance, now) do
    if instance_active?(state, instance) do
      lifetime_drain_wait(state, instance, now)
    else
      state = clear_lifetime_drain_since(state, instance.workload)
      destroy_live_group(state, instance, "lifetime")
    end
  end

  defp lifetime_drain_wait(state, instance, now) do
    since = Map.get(state.lifetime_drain_since, instance.workload, now)
    state = %{state | lifetime_drain_since: Map.put(state.lifetime_drain_since, instance.workload, since)}

    if now - since >= state.lifetime_drain_max_ms do
      destroy_live_group(clear_lifetime_drain_since(state, instance.workload), instance, "lifetime")
    else
      state
    end
  end

  defp clear_lifetime_drain_since(state, workload) do
    %{state | lifetime_drain_since: Map.delete(state.lifetime_drain_since, workload)}
  end

  # Whether the instance's entry listener currently reports an open connection, from
  # THIS tick's freshest reading, OR a live activator splice (a spliced session is an
  # open connection Envoy cannot see). Falls back to "not active" when there is no
  # fresh reading (which only delays a destroy the patience window still forces).
  defp instance_active?(state, instance) do
    envoy_active =
      with {:ok, entry} <- Map.fetch(state.current_stats, instance.node_id),
           {:ok, cfg} <- group_cfg(state, instance.workload),
           {:ok, %{active: active}} <- Map.fetch(entry, stat_prefix(cfg.entry.listen_port)) do
        active > 0
      else
        _ -> false
      end

    envoy_active or ActivatorSplices.live?(state.splices_table, instance.workload)
  end

  # -- banked-TTL GC -----------------------------------------------------------

  # A banked set untouched (last_active_at, else updated_at) longer than
  # bankedTtlSeconds is GC'd. Warmth-only terminal: an expired set IS the instance's
  # end (no volume floor), so this DESTROYS the instance (group_destroyed{reason:
  # expired}) and best-effort evicts each member's bundle.
  defp sweep_banked_ttl(state, now) do
    GroupStore.all(state.store)
    |> Enum.filter(&(&1.state == :banked))
    |> Enum.reduce(state, fn instance, acc ->
      case group_cfg(acc, instance.workload) do
        {:ok, cfg} ->
          ttl_ms = cfg.banked_ttl_seconds * 1000
          last = instance.last_active_at || instance.updated_at || 0

          if now - last >= ttl_ms do
            destroy_banked(acc, instance, "expired")
          else
            acc
          end

        :error ->
          acc
      end
    end)
  end

  # -- forced roll -----------------------------------------------------------

  # Destroy every live member + delete the network + evict the banked set, KEEPING the
  # workload definition so the next connection fresh-boots on the current images. A
  # live group is torn down (StopGroupMember DESTROY per member, DeleteGroupNetwork,
  # group_destroyed); a banked one has its set evicted + the instance destroyed. The
  # publisher re-derives once at the end (the activator swap for the now-gone group).
  defp do_force_roll(state, workload) do
    # The `forced_roll` root span (Task 9): a ROOT span around the whole operator-
    # override roll (no caller trace; a management-API/timer-driven destroy), bounding
    # every member DESTROY + network delete + set eviction so a slow or stuck forced
    # roll is visible. Mirrors StatefulManager.do_destroy_instance's forced_roll span.
    Tracer.with_span "embervm.group.forced_roll", %{attributes: %{"ember.workload" => workload}} do
      instances = GroupStore.list(state.store, workload)

      {state, destroyed, evicted} =
        Enum.reduce(instances, {state, 0, 0}, fn instance, {acc, d, e} ->
          cond do
            GroupState.terminal?(instance.state) ->
              {acc, d, e}

            instance.state == :banked ->
              {destroy_banked(acc, instance, "forced_roll"), d, e + 1}

            true ->
              {destroy_live_group(acc, instance, "forced_roll"), d + 1, e}
          end
        end)

      Embervm.EndpointPublisher.publish(state.publisher)
      Logger.info("embervm group: forced roll", workload: workload, destroyed: destroyed, evicted: evicted)
      {%{destroyed: destroyed, evicted: evicted}, state}
    end
  end

  # -- destroy / evict RPCs + durable ops --------------------------------------

  # Destroy a LIVE group: unpublish the entry endpoint (if running), StopGroupMember
  # DESTROY every live member, DeleteGroupNetwork, then the durable
  # group_destroyed{reason}. The publisher re-derive is batched by the caller for the
  # forced-roll path; a lifetime destroy re-derives here (a single instance) so the
  # activator swap lands.
  defp destroy_live_group(state, instance, reason) do
    # The terminal group_destroyed clears the entry endpoint (the store's
    # post_transition_endpoint nils it on a terminal), and the re-publish below swaps
    # in the activator, so a racing connection parks rather than hitting a dead socket.
    _ = destroy_all_members(state, instance)
    _ = delete_network(state, instance)

    _ =
      GroupStore.transition(
        state.store,
        instance.instance_id,
        :destroy,
        :group_destroyed,
        %{reason: reason},
        %{}
      )

    Embervm.EndpointPublisher.publish(state.publisher)

    Logger.info("embervm group destroyed",
      workload: instance.workload,
      instance_id: instance.instance_id,
      reason: reason
    )

    state
  end

  # Destroy a BANKED instance: best-effort EvictSnapshot each member's bundle (the set
  # on disk), delete the network (the on-disk network record persists while the
  # instance does, so a terminal destroy reclaims it), then the durable
  # group_destroyed{reason}. Warmth-only terminal: the set IS the instance's end.
  defp destroy_banked(state, instance, reason) do
    _ = evict_member_bundles(state, instance)
    # R6, Task 9: drop the store copy of the whole banked SET (EvictArtifact,
    # remote=true, kind GROUP_SET) alongside the local per-member eviction, on the
    # same trigger. The set is the export/evict unit; a single remote evict covers
    # every member. Best-effort and idempotent on the daemon.
    _ = evict_remote_set(state, instance)
    _ = delete_network(state, instance)

    _ =
      GroupStore.transition(
        state.store,
        instance.instance_id,
        :destroy,
        :group_destroyed,
        %{reason: reason},
        %{}
      )

    Logger.info("embervm group banked set evicted (destroyed)",
      workload: instance.workload,
      instance_id: instance.instance_id,
      reason: reason
    )

    state
  end

  defp destroy_all_members(state, instance) do
    GroupStore.members(state.store, instance.instance_id)
    |> Enum.filter(fn m -> is_binary(m.vm_id) and m.vm_id != "" end)
    |> Enum.each(fn m -> _ = stop_group_member_destroy(state, instance, m) end)

    :ok
  end

  # StopGroupMember DESTROY dials the INSTANCE's node (a composite group is anchored to
  # one node; members carry no per-member node_id).
  defp stop_group_member_destroy(state, instance, member) do
    req = %StopGroupMemberRequest{
      trace: %Trace{workload: nil},
      vm_id: member.vm_id,
      mode: :STOP_GROUP_MEMBER_MODE_DESTROY,
      set_id: "",
      member_name: member.member_name
    }

    with {:ok, channel} <- safe_channel(state, dial_for_group(state, instance)) do
      try do
        state.stop_group_member_fun.(channel, req)
      rescue
        _ -> :error
      catch
        _, _ -> :error
      end
    end

    :ok
  end

  # Evict every member's banked bundle (best-effort; the daemon's own bundle GC
  # reclaims a missed one). Reads each member row's snapshot_ref (the banked set).
  defp evict_member_bundles(state, instance) do
    GroupStore.members(state.store, instance.instance_id)
    |> Enum.each(fn m ->
      case m.snapshot_ref do
        ref when is_binary(ref) and ref != "" -> _ = evict_snapshot(state, instance, ref)
        _ -> :ok
      end
    end)

    :ok
  end

  defp delete_network(state, instance) do
    req = %DeleteGroupNetworkRequest{trace: %Trace{workload: nil}, group_instance_id: instance.instance_id}

    with {:ok, channel} <- safe_channel(state, dial_for_group(state, instance)) do
      try do
        state.delete_group_network_fun.(channel, req)
      rescue
        _ -> :error
      catch
        _, _ -> :error
      end
    end

    :ok
  end

  defp evict_snapshot(state, instance, ref) do
    req = %EvictSnapshotRequest{trace: %Trace{workload: nil}, snapshot_ref: ref}

    with {:ok, channel} <- safe_channel(state, dial_for_group(state, instance)) do
      try do
        state.evict_snapshot_fun.(channel, req)
      rescue
        _ -> :error
      catch
        _, _ -> :error
      end
    end

    :ok
  end

  # R6, Task 9: drop the store copy of a banked GROUP_SET (EvictArtifact,
  # remote=true) alongside the local per-member eviction. ref is the set_id (the set
  # is the export/evict unit). Best-effort; a set with no set_id (a partial set
  # already cleared) or no node is a clean no-op.
  defp evict_remote_set(state, %{node_id: node_id, set_id: set_id, workload: workload} = instance)
       when is_binary(node_id) and is_binary(set_id) and set_id != "" do
    artifact = %ArtifactRef{kind: :ARTIFACT_KIND_GROUP_SET, workload: workload, ref: set_id}
    req = %EvictArtifactRequest{artifact: artifact, remote: true, trace: %Trace{workload: workload}}

    with {:ok, channel} <- safe_channel(state, dial_for_group(state, instance)) do
      try do
        state.evict_artifact_fun.(channel, req)
      rescue
        _ -> :error
      catch
        _, _ -> :error
      end
    end

    :ok
  end

  defp evict_remote_set(_state, _instance), do: :ok

  # -- stats scrape helpers -----------------------------------------------------

  # Composite groups run on the same node Envoy as serving/stateful, so reuse the
  # serving-capable predicate: any node reporting a serving_subnet_cidr is scraped.
  defp group_nodes(state) do
    NodeCapacity.all(state.capacity_table)
    |> Enum.filter(&group_capable?/1)
  end

  defp group_capable?(fact) do
    cidr = Map.get(fact, :serving_subnet_cidr)
    is_binary(cidr) and cidr != ""
  end

  defp scrape_node(%{stats_base: nil}, _node), do: {:error, :no_stats_base}

  defp scrape_node(state, _node) do
    state.scrape_fun.(stats_url(state.stats_base))
  end

  defp stats_url(base), do: String.trim_trailing(base, "/") <> "/stats?format=json"

  # -- catalog + instance helpers -----------------------------------------------

  defp group_cfg(state, workload) do
    case WorkloadCatalog.fetch(state.catalog_table, workload) do
      {:ok, %{class: "composite", group: cfg}} when is_map(cfg) -> {:ok, cfg}
      _ -> :error
    end
  end

  # The entry listener stat_prefix for a workload's entry.listen_port
  # (group-<listenPort>, Task 4's stat_prefix wiring), the key the scrape reading is
  # keyed by (mirrors stateful's state-<port>).
  @stat_prefix "group-"
  defp stat_prefix(listen_port) when is_integer(listen_port), do: @stat_prefix <> Integer.to_string(listen_port)

  # Map a scraped stat_prefix back to its owning workload via the catalog: the inverse
  # of stat_prefix/1, found by matching every composite catalog entry's
  # entry.listen_port.
  defp workload_of_stat_prefix(state, @stat_prefix <> port_str) do
    case Integer.parse(port_str) do
      {port, ""} -> find_workload_by_listen_port(state, port)
      _ -> nil
    end
  end

  defp workload_of_stat_prefix(_state, _other), do: nil

  defp find_workload_by_listen_port(state, port) do
    WorkloadCatalog.all_names(state.catalog_table)
    |> Enum.find_value(fn name ->
      case WorkloadCatalog.fetch(state.catalog_table, name) do
        {:ok, %{class: "composite", group: %{entry: %{listen_port: ^port}}}} -> name
        _ -> nil
      end
    end)
  end

  # A group is degraded when it carries a dead-member flag (decision 11 exclusion).
  defp degraded?(instance), do: is_binary(instance.degraded_member)

  defp usage_principal(workload), do: "system:group:#{workload}"

  # -- daemon + bank seams -------------------------------------------------

  defp safe_channel(state, node_id) do
    state.channel_fun.(node_id)
  rescue
    e -> {:error, {:channel_raised, e}}
  catch
    kind, reason -> {:error, {:channel_raised, {kind, reason}}}
  end

  # The channel key for a group teardown dial (StopGroupMember / DeleteGroupNetwork /
  # EvictSnapshot / EvictArtifact): the OWNING instance on the group's node reporting
  # this group_instance_id (live group_member_vms or banked group_bundle_sets), else
  # the node name. Instance-key unification (PR-B0b): a group's members are all
  # co-located on ONE instance, so a teardown against the co-located node-name alias
  # misroutes to an arbitrary sibling brick. Fail-open to the instance's node_id
  # preserves single-instance behaviour exactly.
  defp dial_for_group(state, %{instance_id: instance_id, node_id: node_id}) do
    Embervm.WakeInstance.dial_for_group(state.capacity_table, node_id, instance_id)
  end

  defp default_bank(workload, instance_id) do
    Embervm.GroupManager.Supervisor.bank_group(workload, instance_id)
  end

  defp default_stop_group_member(channel, req) do
    Embervm.Node.V1.NodeService.Stub.stop_group_member(channel, req)
  end

  defp default_delete_group_network(channel, req) do
    Embervm.Node.V1.NodeService.Stub.delete_group_network(channel, req)
  end

  defp default_evict_snapshot(channel, req) do
    Embervm.Node.V1.NodeService.Stub.evict_snapshot(channel, req)
  end

  defp default_evict_artifact(channel, req) do
    Embervm.Node.V1.NodeService.Stub.evict_artifact(channel, req)
  end

  # Production scrape: identical to StatefulSweeper's (GET /stats?format=json over the
  # shared Finch pool, parse tcp.<prefix>.downstream_cx_{active,total}). Composite
  # entry listeners are named group-<port> but share the tcp.* stat namespace, so the
  # SAME parser keeps both stateful (state-<port>) and group (group-<port>) prefixes.
  defp default_scrape(url) do
    req = Finch.build(:get, url)

    case Finch.request(req, Embervm.Finch, receive_timeout: 3_000) do
      {:ok, %Finch.Response{status: status, body: body}} when status in 200..299 ->
        parse_stats(body)

      {:ok, %Finch.Response{status: status}} ->
        {:error, {:stats_status, status}}

      {:error, reason} ->
        {:error, reason}
    end
  end

  @tcp_stat_prefix "tcp."
  @cx_active_suffix ".downstream_cx_active"
  @cx_total_suffix ".downstream_cx_total"

  @doc """
  Parse an Envoy `/stats?format=json` body into `%{stat_prefix => %{active: n, total:
  n}}`, keeping only `tcp.<prefix>.downstream_cx_{active,total}` counters (identical
  to `StatefulSweeper.parse_stats/1`; the group entry listener shares the tcp.* stat
  namespace). `@doc false` so the parse path is exercised directly in tests.
  """
  @doc false
  @spec parse_stats(binary()) :: {:ok, %{optional(String.t()) => %{active: integer(), total: integer()}}} | {:error, term()}
  def parse_stats(body) do
    case Jason.decode(body) do
      {:ok, %{"stats" => stats}} when is_list(stats) ->
        reading = Enum.reduce(stats, %{}, fn stat, acc -> fold_tcp_stat(stat, acc) end)
        {:ok, reading}

      _ ->
        {:error, :unparseable_stats}
    end
  rescue
    e -> {:error, {:stats_parse_raised, e}}
  end

  defp fold_tcp_stat(%{"name" => @tcp_stat_prefix <> rest, "value" => value}, acc) when is_integer(value) do
    cond do
      String.ends_with?(rest, @cx_active_suffix) ->
        prefix = String.replace_suffix(rest, @cx_active_suffix, "")
        put_field(acc, prefix, :active, value)

      String.ends_with?(rest, @cx_total_suffix) ->
        prefix = String.replace_suffix(rest, @cx_total_suffix, "")
        put_field(acc, prefix, :total, value)

      true ->
        acc
    end
  end

  defp fold_tcp_stat(_stat, acc), do: acc

  defp put_field(acc, prefix, field, value) do
    entry = Map.get(acc, prefix, %{active: 0, total: 0})
    Map.put(acc, prefix, Map.put(entry, field, value))
  end

  defp schedule(msg, interval_ms) when interval_ms > 0 do
    Process.send_after(self(), msg, interval_ms)
  end

  defp schedule(_msg, _interval), do: :ok

  defp default_clock, do: System.system_time(:millisecond)
end
