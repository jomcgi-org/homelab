defmodule Embervm.ServingSweeper do
  @moduledoc """
  The serving lifecycle-economics loop (R3, Task 9): the counterpart to the R2
  session idle-bank / TTL-GC sweep, adapted to the fact that serving instances have
  NO per-instance process (they are ETS rows in `Embervm.ServingStore`, not a
  supervised `Embervm.Session`). So this ONE process drives every economic decision
  on a timer: it scrapes each node Envoy's request counters to detect idleness,
  banks idle instances (drain-before-bank), expires over-lifetime ones, GCs stale
  banked snapshots, and answers the forced-roll management verb.

  ## the idle signal (standing decision 9)

  Serving is off the hit path, so the control plane cannot see per-request activity
  directly. Instead, a low-cadence scrape reads each node Envoy's per-cluster
  `cluster.serve|<workload>.upstream_rq_total` counter (the node Envoy is the only
  component on the hit path, and it counts every proxied request). A tick computes
  the DELTA against the prior tick's reading: a non-zero delta means the workload
  served traffic in the window, so its live instances are marked active
  (`last_active_at` touched) and a `serving_stats` op is appended (usage per
  D-R3.2.1: request-count only). An instance with ZERO delta across `idleBankSeconds`
  is idle and a bank candidate.

  Because the counter is per-CLUSTER (per-workload), not per-instance, the activity
  signal is workload-wide: a workload with N live instances that served ANY request
  marks all N active. v1 does not attribute requests to individual instances (the
  node Envoy load-balances across them and its per-endpoint stats are not scraped);
  that is acceptable because the bank decision is "scale the workload down toward
  minInstances when the WHOLE workload is idle", not "bank the specific cold
  instance".

  ## fail-open for warmth (ADR embervm/001 posture)

  A scrape failure (timeout, non-200, unparseable body) yields NO reading for that
  node this tick. Idle detection is then SUPPRESSED for every workload on that node:
  an instance is NEVER banked on missing or stale stats (banking a warm instance
  because we could not confirm it was busy is the expensive mistake). The tick still
  runs lifetime/TTL GC (those read the durable store, not stats) and simply skips the
  idle pass, retrying the scrape next cadence. A fresh sweeper has no prior reading
  either, so its FIRST tick establishes the baseline and banks nothing (same code
  path as a scrape failure): idleness needs two consecutive successful readings.

  ## drain-before-bank (standing decision 5), timer-driven

  Banking a live serving instance must not drop in-flight requests, so the sequence
  is: `unpublish` (reason `:bank`, EDS update pulls the endpoint; if this empties the
  cluster the SAME update installs the activator fallback, so scale-to-zero is never
  a 503 window) STRICTLY precedes `StopServing(BANK)`. Between them the instance
  drains for `drainSeconds` of wall-clock (a request already routed there finishes).
  This process must not BLOCK for drainSeconds (it would stall every other decision),
  so the drain is timer-driven: unpublishing arms a `{:bank_drained, id}` timer for
  `drainSeconds`; when it fires, the bank is admitted (per-node cap) and the
  `StopServing(BANK)` RPC runs in a spawned worker whose `{:bank_done}` completes the
  durable `serving_banked` op on this serialized process (the session bank shape).

  The drain-for-bank vs drain-for-health distinction is carried by `drain_reason` on
  the ETS row (`ServingStore.unpublish(reason: :bank)` stamps `:bank`), which
  `Embervm.ServingHealth` reads so a health sweep during the drain window does NOT
  republish the instance the bank is removing.

  ## reaching the node Envoy stats

  The node Envoy admin (`/stats`) binds loopback only, so PR-3's chart adds a
  path-restricted stats listener on `servingEnvoy.statsPort` (proxying ONLY `/stats`
  to the loopback admin, 403 for everything else) exposed on the `embervm-serving`
  Service. This process GETs `http://<serving-svc>:<statsPort>/stats?format=json` over
  the shared Finch pool. No new k8s RBAC: it is an HTTP GET to a pod, not an API
  call. Placement iterates serving-capable `Embervm.NodeCapacity` facts (mirroring the
  EndpointPublisher's per-node keying); v1 has one serving node reached via the one
  serving Service DNS name (`stats_base`).

  ## does serving participate in base-refcounting?

  No (D-R3.3.1, resolved in Task 9): a serving instance cold-boots from a rootfs
  IMAGE (`img.RootfsPath`, resolved via the node's image table, D-R3.4.2), never from
  a `BuildBase` base SNAPSHOT, and once banked rides its OWN per-instance serving
  snapshot. So base eviction (`Embervm.BaseBuilder.evictable?/1`) can never remove
  anything a live serving instance needs, and this sweeper reports NO `:serving`
  base-ref count (there is nothing to hold a superseded base alive for). The inert
  `:serving` term threaded through `merge_refcounts` stays a no-op by design.
  """

  use GenServer
  require Logger

  # Tracer.with_span/set_attributes are OpenTelemetry.Tracer MACROS, so the module
  # must be required even though it is called fully-qualified via the alias.
  require OpenTelemetry.Tracer, as: Tracer

  alias Embervm.{NodeCapacity, ServingState, ServingStore, WorkloadCatalog}
  alias Embervm.OpLog.Op

  alias Embervm.Node.V1.{ArtifactRef, EvictArtifactRequest, StopServingRequest, StopServingResponse, Trace}

  # Per-node concurrent-bank cap, shared-in-spirit with sessions (banking writes
  # GiBs, so serialize per node): the node daemon also refuses a second concurrent
  # bank, but capping here avoids even issuing the RPC. Default 1.
  @default_bank_concurrency 1

  # -- Client API ------------------------------------------------------------

  @spec start_link(keyword()) :: GenServer.on_start()
  def start_link(opts) do
    case Keyword.get(opts, :name, __MODULE__) do
      nil -> GenServer.start_link(__MODULE__, opts)
      name -> GenServer.start_link(__MODULE__, opts, name: name)
    end
  end

  @doc """
  Runs one full sweep synchronously (scrape + serving_stats + idle-bank arming +
  lifetime/TTL GC) and returns after it completes. Tests drive the economics
  deterministically through this (with an injected clock + stats seam) instead of
  waiting on the timer.
  """
  @spec sweep(GenServer.server()) :: :ok
  def sweep(server \\ __MODULE__) do
    GenServer.call(server, :sweep, :infinity)
  end

  @doc """
  Forced roll (management verb behind `DELETE /v1/serving/{workload}/instances`):
  drains and DESTROYS every live instance of `workload` (and evicts its banked
  snapshots) so the next miss cold-creates on the CURRENT base. Synchronous;
  returns `%{destroyed: n, evicted: m}`. Unlike an idle bank this does NOT snapshot
  (the point is to discard the old base lineage), and it does NOT wait `drainSeconds`
  (a forced roll is an operator override accepting the in-flight-drop, mirroring a
  task-class destroy). The last unpublish still installs the activator atomically, so
  the workload stays reachable (a miss cold-starts it).
  """
  @spec force_roll(GenServer.server(), String.t()) :: %{destroyed: non_neg_integer(), evicted: non_neg_integer()}
  def force_roll(server \\ __MODULE__, workload) do
    GenServer.call(server, {:force_roll, workload}, :infinity)
  end

  # -- GenServer callbacks ---------------------------------------------------

  @impl true
  def init(opts) do
    op_log_mod = Keyword.get(opts, :op_log_mod, Embervm.OpLog.SQLite)
    op_log = Keyword.get(opts, :op_log, op_log_mod)

    state = %{
      store: Keyword.get(opts, :store, ServingStore),
      publisher: Keyword.get(opts, :publisher, Embervm.EndpointPublisher),
      capacity_table: Keyword.get(opts, :capacity_table, NodeCapacity.table()),
      catalog_table: Keyword.get(opts, :catalog_table, WorkloadCatalog.table()),
      op_log: op_log,
      # The backend module dispatched below, threaded alongside :op_log (the
      # server address) so a non-default backend never requires editing this
      # module. Defaults to the same SQLite module :op_log defaults to.
      op_log_mod: op_log_mod,
      clock: Keyword.get(opts, :clock, &default_clock/0),
      tenant: Keyword.get(opts, :tenant, "homelab"),
      # The node Envoy stats scrape seam: (stats_url) -> {:ok, %{cluster_name => rq_total}}
      # | {:error, reason}. Production GETs /stats?format=json over Finch and parses
      # the per-cluster upstream_rq_total counters; tests inject scripted readings.
      scrape_fun: Keyword.get(opts, :scrape_fun, &default_scrape/1),
      # The base URL of the serving Service's stats port (http://<svc>:<statsPort>).
      # nil disables the scrape (no serving node wired): every tick then fails open,
      # so no idle-bank decision ever runs, exactly the safe default.
      stats_base: Keyword.get(opts, :stats_base, nil),
      # The StopServing daemon seam (injected for tests; production dials the real
      # NodeService stub over the shared NodeChannel).
      channel_fun: Keyword.get(opts, :channel_fun, &Embervm.NodeChannel.get/1),
      stop_serving_fun: Keyword.get(opts, :stop_serving_fun, &default_stop_serving/2),
      # Remote artifact eviction seam (R6, Task 9): (channel, %EvictArtifactRequest{})
      # -> {:ok, %EvictArtifactResponse{}} | {:error, _}. Fired alongside the durable
      # serving_evicted transition so the store copy of a banked SERVING bundle is
      # dropped on the same triggers (banked TTL, stale lineage, forced roll). v1 issues
      # no local EvictSnapshot RPC here (the activator's adoption reconcile evicts the
      # node-local orphan separately), but the STORE copy needs an explicit drop since
      # nothing else targets it. Injected for tests; production dials the real
      # NodeService stub.
      evict_artifact_fun: Keyword.get(opts, :evict_artifact_fun, &default_evict_artifact/2),
      # Per-node concurrent-bank cap: node_id -> banks in flight. A node at its cap
      # defers new banks to a later tick (the idle instance stays live, re-evaluated).
      bank_concurrency: Keyword.get(opts, :bank_concurrency, @default_bank_concurrency),
      bank_inflight: %{},
      # instance_id -> %{node_id, vm_id, workload} for a bank whose drain timer is
      # running (unpublished, waiting drainSeconds) or whose StopServing is in flight.
      # A candidate already here is not re-drained; a wake mid-bank is the activator's
      # concern (a miss cold-creates a NEW instance, R2 no-cancel).
      draining: %{},
      # The prior tick's per-node stats reading: node_id -> %{cluster_name => rq_total}.
      # A delta needs two consecutive successful readings; a failed scrape drops the
      # node's prior reading so the next success re-establishes a baseline (never a
      # delta against a stale pre-failure number).
      last_stats: %{},
      # The set of workloads confirmed idle on the last scrape (two consecutive
      # readings, zero delta): the only workloads the idle pass may bank. Empty until
      # the first sweep populates it.
      confirmed_idle: MapSet.new(),
      # A timer seam so tests fire the drain deadline deterministically. Production
      # uses Process.send_after; a test injects a function that records the delay and
      # lets the test send {:bank_drained, id} by hand.
      timer_fun: Keyword.get(opts, :timer_fun, &default_timer/2),
      # ADR embervm/014 decision 5: node-confirmed destroy config plumbing.
      node_confirmed_destroy: Keyword.get(opts, :node_confirmed_destroy, false),
      destroying_alarm_ms: Keyword.get(opts, :destroying_alarm_ms, 300_000),
      orphan_grace_ms: Keyword.get(opts, :orphan_grace_ms, 60_000),
      # status.serving {live,banked,published} writer (Task 10). Defaults to the K8s
      # merge-patch on the workload status subresource (the session writer's seam);
      # tests inject a recorder. Disjoint status keys from status.sessions and the
      # watcher, so the merge-patch never clobbers another writer.
      status_writer: Keyword.get(opts, :status_writer, &Embervm.K8s.patch_workload_status/3),
      # workload -> last-written {live,banked,published} triple, so the sweep patches
      # only a workload whose counts changed (debounce: at most one API call per
      # serving workload per sweep, never per transition).
      serving_status_written: %{},
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

  # A drain timer fired: the instance has drained for drainSeconds. Admit the bank
  # (per-node cap) and spawn the StopServing(BANK) worker.
  def handle_info({:bank_drained, instance_id}, state) do
    {:noreply, on_drain_deadline(state, instance_id)}
  end

  # The async StopServing(BANK) worker finished: complete the durable transition +
  # inflight release on this serialized process.
  def handle_info({:bank_done, instance_id, node_id, outcome}, state) do
    {:noreply, finish_bank(state, instance_id, node_id, outcome)}
  end

  def handle_info(_msg, state), do: {:noreply, state}

  # -- the sweep -------------------------------------------------------------

  # One tick: (1) scrape every serving node, append serving_stats + touch activity
  # for workloads with a non-zero delta; (2) arm idle-bank drains for instances idle
  # past idleBankSeconds AND above minInstances (only when the node's scrape
  # succeeded this tick: fail-open); (3) expire over-lifetime instances and GC stale
  # banked snapshots (store reads, no stats dependency).
  defp do_sweep(state) do
    now = state.clock.()

    # The `stats_sweep` lifecycle span (Task 10): a ROOT span per sweep tick (timer-
    # driven, no caller trace to nest under, like the session create/bank spans). It
    # bounds the stats scrape + idle-bank arming + lifetime/TTL GC so a slow or
    # error-prone tick is visible; it does NOT touch the hit path.
    Tracer.with_span "embervm.serving.stats_sweep", %{attributes: %{"ember.tenant" => state.tenant}} do
      state
      |> scrape_and_record(now)
      |> arm_idle_banks(now)
      |> sweep_lifetime(now)
      |> sweep_banked_ttl(now)
      |> sweep_stale_lineage(now)
      |> write_serving_status()
    end
  end

  # -- status.serving counts (Task 10) ---------------------------------------

  # Write status.serving {live,banked,published} + servingSummary for every serving-
  # class workload, DEBOUNCED: only patch a workload whose counts triple changed
  # since its last write. Runs on the sweep tick (not per transition), so the K8s
  # API is touched at most once per serving workload per sweep. Disjoint status keys
  # (serving/servingSummary), so the merge-patch never clobbers the status.sessions
  # or watcher writers.
  defp write_serving_status(state) do
    serving_workloads_with_ns(state)
    |> Enum.reduce(state, fn %{workload: workload, namespace: namespace}, acc ->
      counts = serving_counts(acc, workload)
      triple = {counts.live, counts.banked, counts.published}

      if Map.get(acc.serving_status_written, workload) == triple do
        acc
      else
        _ = patch_serving_status(acc, namespace, workload, counts)
        %{acc | serving_status_written: Map.put(acc.serving_status_written, workload, triple)}
      end
    end)
  end

  # live+banked from the maintained per-workload counter; published is the healthy-
  # published set (the fan-out fact), which is not in the counter. Both are O(few)
  # reads on the sweep tick.
  defp serving_counts(state, workload) do
    base = ServingStore.counts(state.store, workload)
    published = length(ServingStore.published_endpoints(state.store, workload))
    %{live: base.live, banked: base.banked, published: published}
  end

  # Every cataloged serving-class workload with its namespace, for the status write.
  # A catalog with no serving workloads yields [], a no-op on session/task clusters.
  defp serving_workloads_with_ns(state) do
    for name <- WorkloadCatalog.all_names(state.catalog_table),
        {:ok, %{class: "serving", namespace: namespace}} <- [WorkloadCatalog.fetch(state.catalog_table, name)],
        is_binary(namespace) do
      %{workload: name, namespace: namespace}
    end
  end

  defp patch_serving_status(state, namespace, name, counts) do
    status_map = %{
      "serving" => %{"live" => counts.live, "banked" => counts.banked, "published" => counts.published},
      "servingSummary" => "#{counts.live}/#{counts.banked}/#{counts.published}"
    }

    case state.status_writer.(namespace, name, status_map) do
      :ok ->
        :ok

      {:error, reason} ->
        # Visibility-only: a status-write failure must never crash the sweep.
        Logger.warning("embervm serving status patch failed for #{namespace}/#{name}: #{inspect(reason)}")
    end
  rescue
    e -> Logger.warning("embervm serving status patch raised for #{namespace}/#{name}: #{inspect(e)}")
  catch
    _, _ -> :ok
  end

  # -- stats scrape + activity -----------------------------------------------

  # Scrape each serving-capable node's Envoy stats, compute per-workload rq deltas,
  # and for a non-zero delta append serving_stats (usage) + touch last_active_at on
  # that workload's live instances. Returns state with last_stats updated (a failed
  # node's prior reading dropped, so no delta against a stale number) and the set of
  # workloads with a CONFIRMED idle reading this tick (present in BOTH the prior and
  # current reading with a zero delta), which is the ONLY set the idle pass may bank.
  # A workload with no prior reading (first successful scrape, or the tick after a
  # scrape failure) is NOT confirmed-idle: idleness requires two consecutive readings,
  # so the first reading only baselines and banks nothing (fail-open for warmth).
  defp scrape_and_record(state, now) do
    nodes = serving_nodes(state)

    {last_stats, confirmed_idle} =
      Enum.reduce(nodes, {state.last_stats, MapSet.new()}, fn node, {stats_acc, idle_acc} ->
        case scrape_node(state, node) do
          {:ok, reading} ->
            prior = Map.get(state.last_stats, node.configured_id)
            idle = record_deltas(state, node, prior, reading, now)
            {Map.put(stats_acc, node.configured_id, reading), MapSet.union(idle_acc, idle)}

          {:error, reason} ->
            # Fail-open: drop the prior reading (so the next success re-baselines) and
            # confirm NOTHING idle for this node. Idle detection is suppressed for its
            # workloads this tick (never bank on a failed scrape).
            Logger.debug("embervm serving: stats scrape failed",
              node_id: node.configured_id,
              reason: inspect(reason)
            )

            {Map.delete(stats_acc, node.configured_id), idle_acc}
        end
      end)

    # A node that VANISHED from the capacity table (no longer serving-capable) leaves
    # a stale last_stats entry; prune to the currently-scraped set so a re-added node
    # re-baselines rather than delta-ing against an old number.
    scraped_ids = MapSet.new(nodes, & &1.configured_id)
    last_stats = Map.take(last_stats, MapSet.to_list(scraped_ids))

    Map.merge(state, %{last_stats: last_stats, confirmed_idle: confirmed_idle})
  end

  # For each workload cluster on the node, delta rq_total against the prior reading.
  # A positive delta: append serving_stats (charges usage.request_count, D-R3.2.1)
  # and touch every live instance of the workload active. A ZERO delta AGAINST A
  # PRIOR READING confirms the workload idle this tick (added to the returned set,
  # the only set the idle pass may bank). No prior reading (first tick or
  # post-failure) => no delta computed => nothing active AND nothing confirmed idle
  # (baseline established, banks nothing: idleness needs two consecutive readings).
  # A cluster present in the prior reading but ABSENT now (or vice versa) is treated
  # as neither active nor confirmed-idle (a reset/rename; re-baseline next tick).
  defp record_deltas(_state, _node, nil, _reading, _now), do: MapSet.new()

  defp record_deltas(state, node, prior, reading, now) do
    Enum.reduce(reading, MapSet.new(), fn {cluster_name, total}, confirmed_idle ->
      workload = workload_of_cluster(cluster_name)
      prior_total = Map.get(prior, cluster_name)

      record_one_delta(state, node, workload, prior_total, total, now, confirmed_idle)
    end)
  end

  # One cluster's delta classification. `prior_total` nil = this cluster had no prior
  # reading (first appearance): baseline only. A positive delta charges usage + marks
  # active; a zero delta against a prior reading confirms idle; a negative delta
  # (counter reset) re-baselines without an idle claim.
  defp record_one_delta(_state, _node, workload, _prior, _total, _now, acc) when not is_binary(workload),
    do: acc

  defp record_one_delta(_state, _node, _workload, nil, _total, _now, acc), do: acc

  defp record_one_delta(state, node, workload, prior_total, total, now, acc) do
    delta = total - prior_total

    cond do
      delta > 0 ->
        append_serving_stats(state, node, workload, delta, now)
        touch_workload_active(state, workload, now)
        acc

      delta == 0 ->
        # Two consecutive readings, no traffic: the workload is confirmed idle. The
        # idle pass then checks each instance against idleBankSeconds.
        MapSet.put(acc, workload)

      true ->
        # A negative delta (Envoy restarted, counter reset): re-baseline, do not
        # treat as idle (we cannot tell how much traffic the reset hid).
        acc
    end
  end

  # Append a serving_stats op (per-cluster request-count usage, D-R3.2.1). principal/
  # tenant ride the op's own fields (the workload owner), never a join: serving_stats
  # is per-workload, not per-instance, so serving_instance_id is nil. A best-effort
  # append: a usage-op failure must never wedge the sweep.
  defp append_serving_stats(state, _node, workload, delta, now) do
    op = %Op{
      kind: :serving_stats,
      tenant: state.tenant,
      principal: usage_principal(workload),
      workload: workload,
      serving_instance_id: nil,
      ts: now,
      payload: %{rq_delta: delta, window_ms: nil}
    }

    _ = state.op_log_mod.append(state.op_log, op)
    :ok
  rescue
    e ->
      Logger.warning("embervm serving: serving_stats append raised", workload: workload, error: inspect(e))
      :ok
  end

  # Touch every LIVE (non-terminal, non-banked) instance of the workload active. A
  # banked instance has no live traffic; a terminal one is gone.
  defp touch_workload_active(state, workload, now) do
    ServingStore.list(state.store, workload)
    |> Enum.filter(&live_instance?/1)
    |> Enum.each(fn instance ->
      _ = ServingStore.touch_active(state.store, instance.instance_id, now)
    end)
  end

  # -- idle-bank arming ------------------------------------------------------

  # Arm a drain-before-bank for every instance that is idle past idleBankSeconds AND
  # above the workload's minInstances floor, in a workload CONFIRMED idle this tick
  # (present in two consecutive readings with a zero rq delta). A workload not in
  # confirmed_idle (a scrape failed, a first reading, or it served traffic) is never
  # banked this tick: fail-open for warmth. `last_active_at` (touched on the last
  # active tick, else the created time as the initial baseline) sets the per-instance
  # idle age against idleBankSeconds.
  defp arm_idle_banks(state, now) do
    ServingStore.all(state.store)
    |> Enum.filter(&(&1.state == :published))
    |> Enum.reject(&Map.has_key?(state.draining, &1.instance_id))
    |> Enum.filter(&MapSet.member?(state.confirmed_idle, &1.workload))
    |> group_by_workload()
    |> Enum.reduce(state, fn {workload, instances}, acc ->
      arm_workload_idle_banks(acc, workload, instances, now)
    end)
  end

  # For one workload: the instances idle past idleBankSeconds are bank candidates,
  # but the workload must keep at least minInstances live. Bank the COLDEST-first
  # excess (idle instances beyond the floor), never dropping below minInstances.
  defp arm_workload_idle_banks(state, workload, instances, now) do
    case serving_cfg(state, workload) do
      {:ok, cfg} ->
        idle_ms = cfg.idle_bank_seconds * 1000
        # An idle instance served nothing across the window. last_active_at is set by
        # the activity touch; before the first activity it is nil, so fall back to
        # created_at as the idle baseline (a never-active instance idles from birth).
        idle = Enum.filter(instances, &idle?(&1, idle_ms, now))

        # Respect the minInstances floor: only bank instances ABOVE it. The floor
        # counts ALL currently-live instances of the workload (published + starting +
        # already-draining-for-bank), so a bank in flight already counts against it.
        live_count = ServingStore.counts(state.store, workload).live
        bankable = max(live_count - max(cfg.min_instances, 0), 0)

        idle
        |> Enum.sort_by(&idle_baseline/1)
        |> Enum.take(bankable)
        |> Enum.reduce(state, fn instance, acc -> begin_bank_drain(acc, instance, cfg, now) end)

      :error ->
        state
    end
  end

  # Force-bank every published serving instance on a draining node (R6, ADR
  # embervm/009). Called by the DrainCoordinator on the drain edge. Unlike the idle
  # pass, it ignores idle age and the minInstances floor (the node is being
  # preempted, so every warm endpoint banks) but reuses the same drain-then-bank
  # machinery, whose short drainSeconds timer completes well within noded's 120s
  # hold. Starting instances (no snapshot yet) are left to die and re-provision.
  # Returns {count_started, state}.
  @spec drain_node(GenServer.server(), String.t()) :: non_neg_integer()
  def drain_node(server \\ __MODULE__, node_id) do
    GenServer.call(server, {:drain_node, node_id}, :infinity)
  end

  defp drain_bank_node(state, node_id) do
    now = state.clock.()

    instances =
      ServingStore.all(state.store)
      |> Enum.filter(&(&1.state == :published and &1.node_id == node_id))
      |> Enum.reject(&Map.has_key?(state.draining, &1.instance_id))

    state =
      Enum.reduce(instances, state, fn instance, acc ->
        case serving_cfg(acc, instance.workload) do
          {:ok, cfg} -> begin_bank_drain(acc, instance, cfg, now)
          :error -> acc
        end
      end)

    {length(instances), state}
  end

  # Begin the drain-before-bank: unpublish (reason :bank, so the EDS update pulls the
  # endpoint and, if this empties the cluster, installs the activator atomically; and
  # the drain_reason stamp tells ServingHealth not to republish it), then arm the
  # drainSeconds timer. The StopServing(BANK) itself waits for the timer.
  defp begin_bank_drain(state, instance, cfg, _now) do
    # The `drain` lifecycle span (Task 10): the unpublish + re-push that pulls the
    # endpoint from the fan-out and arms the drainSeconds timer. A ROOT span (idle-
    # bank is timer-driven, no caller trace). ember.drain_ms is the CONFIGURED drain
    # window (the deadline the {:bank_drained} timer will honor), not a measured
    # duration: this call only arms the timer, it does not block for the drain.
    Tracer.with_span "embervm.serving.drain",
                     %{attributes: %{"ember.workload" => instance.workload, "ember.instance_id" => instance.instance_id}} do
      begin_bank_drain_body(state, instance, cfg)
    end
  end

  defp begin_bank_drain_body(state, instance, cfg) do
    case ServingStore.unpublish(state.store, instance.instance_id, :bank) do
      {:ok, _} ->
        # Re-derive + re-push so the endpoint leaves the fan-out now (and the activator
        # is installed in the same update if this was the last instance).
        Embervm.EndpointPublisher.publish(state.publisher)

        drain_ms = max(cfg.drain_seconds, 0) * 1000
        state.timer_fun.({:bank_drained, instance.instance_id}, drain_ms)
        Tracer.set_attributes(%{"ember.drain_ms" => drain_ms})

        Logger.info("embervm serving: draining for bank",
          instance_id: instance.instance_id,
          workload: instance.workload,
          drain_ms: drain_ms
        )

        put_draining(state, instance)

      {:error, reason} ->
        Logger.warning("embervm serving: bank-drain unpublish failed",
          instance_id: instance.instance_id,
          reason: inspect(reason)
        )

        state
    end
  end

  defp put_draining(state, instance) do
    entry = %{node_id: instance.node_id, vm_id: instance.vm_id, workload: instance.workload}
    %{state | draining: Map.put(state.draining, instance.instance_id, entry)}
  end

  # -- drain deadline -> bank ------------------------------------------------

  # The drain timer fired: the instance drained for drainSeconds. Admit the bank
  # (per-node cap) and spawn the StopServing(BANK) worker. If the node is at its bank
  # cap, DEFER: leave the instance draining and re-arm a short retry timer, so it
  # banks once a slot frees (it is already unpublished, so it costs nothing to wait).
  # If the instance moved off `draining` concurrently (a forced roll destroyed it),
  # drop it.
  defp on_drain_deadline(state, instance_id) do
    with %{node_id: node_id, vm_id: vm_id} <- Map.get(state.draining, instance_id),
         {:ok, %{state: :draining} = instance} <- ServingStore.get(state.store, instance_id) do
      if bank_at_cap?(state, node_id) do
        # Defer: re-arm a short timer to retry once a bank slot frees.
        state.timer_fun.({:bank_drained, instance_id}, 1_000)
        state
      else
        admit_bank(state, instance, node_id, vm_id)
      end
    else
      _ ->
        # Instance gone or no longer draining (forced roll / concurrent terminal):
        # forget the drain bookkeeping.
        %{state | draining: Map.delete(state.draining, instance_id)}
    end
  end

  # Admit the bank: ETS `draining -[bank]-> banking` (transient, no op), reserve the
  # node slot, and spawn the StopServing(BANK) worker. The durable serving_banked op
  # lands in finish_bank (AFTER the daemon returns), the crash-consistent order.
  defp admit_bank(state, instance, node_id, vm_id) do
    case ServingStore.mark(state.store, instance.instance_id, :bank) do
      {:ok, _} ->
        state = incr_bank_inflight(state, node_id)
        spawn_bank_worker(state, instance, node_id, vm_id)
        state

      {:error, reason} ->
        Logger.warning("embervm serving: bank mark failed",
          instance_id: instance.instance_id,
          reason: inspect(reason)
        )

        %{state | draining: Map.delete(state.draining, instance.instance_id)}
    end
  end

  # The async StopServing(BANK) worker: the RPC off this process (a bank writes GiBs
  # and can take seconds), reporting {:bank_done} back so the durable append + slot
  # release stay serialized on the manager. ALWAYS reports an outcome, even on a
  # crash, so bookkeeping never leaks a reserved slot.
  defp spawn_bank_worker(state, instance, node_id, vm_id) do
    owner = self()
    channel_fun = state.channel_fun
    stop_fun = state.stop_serving_fun
    instance_id = instance.instance_id
    workload = instance.workload
    generation = (instance.generation || 0) + 1
    # Dial the OWNING instance (the one whose capacity fact reports this live serving
    # vm_id), not the node-name alias (co-location made it point at an arbitrary
    # sibling brick). Falls back to node_id for a legacy/single-instance fact,
    # preserving single-instance behaviour. Resolved on the owner so the worker reads
    # a settled key.
    dial_key = dial_for_serving_vm(state, node_id, vm_id)

    spawn(fn ->
      # The `bank` lifecycle span (Task 10): a ROOT span in the spawned worker (the
      # StopServing(BANK) has no caller trace, mirroring session.bank). ember.
      # snapshot_bytes is set on success; the span bounds the seconds-long GiB
      # snapshot write so a slow bank is visible.
      outcome =
        Tracer.with_span "embervm.serving.bank",
                         %{
                           attributes: %{
                             "ember.workload" => workload,
                             "ember.instance_id" => instance_id,
                             "ember.node_id" => node_id,
                             "ember.generation" => generation
                           }
                         } do
          try do
            with {:ok, channel} <- safe_channel(channel_fun, dial_key),
                 {:ok, %StopServingResponse{snapshot_ref: ref, size_bytes: size}}
                 when is_binary(ref) and ref != "" <-
                   stop_fun.(channel, bank_request(vm_id)) do
              Tracer.set_attributes(%{"ember.snapshot_bytes" => size})
              {:ok, ref, size, generation}
            else
              other -> {:error, other}
            end
          rescue
            e -> {:error, {:bank_raised, e}}
          catch
            kind, reason -> {:error, {:bank_raised, {kind, reason}}}
          end
        end

      send(owner, {:bank_done, instance_id, node_id, outcome})
    end)
  end

  defp bank_request(vm_id) do
    %StopServingRequest{trace: %Trace{}, vm_id: vm_id, mode: :STOP_SERVING_MODE_BANK}
  end

  # The StopServing(BANK) completed: release the node slot + drain bookkeeping, then:
  #   * on success: `banking -[bank_ready]-> banked` with serving_banked (snapshot
  #     fact + generation+1);
  #   * on failure: `banking -[bank_abort]-> draining` (ETS-only; the VM is still
  #     alive, no snapshot written) and re-publish the endpoint so the still-live VM
  #     re-enters the fan-out (a failed bank should NOT leave a warm VM dark). The next
  #     idle sweep re-arms the drain.
  defp finish_bank(state, instance_id, node_id, outcome) do
    state =
      state
      |> decr_bank_inflight(node_id)
      |> Map.update!(:draining, &Map.delete(&1, instance_id))

    case ServingStore.get(state.store, instance_id) do
      # Terminal mid-bank (a forced roll destroyed it): never resurrect. A produced
      # snapshot is orphaned; the activator's adoption reconcile reaps it.
      {:ok, %{state: st}} when st in [:evicted, :destroyed, :failed] ->
        state

      {:ok, instance} ->
        finish_bank_active(state, instance, node_id, outcome)

      :error ->
        state
    end
  end

  defp finish_bank_active(state, instance, node_id, {:ok, ref, size, generation}) do
    _ =
      ServingStore.transition(
        state.store,
        instance.instance_id,
        :bank_ready,
        :serving_banked,
        %{snapshot_ref: ref, size_bytes: size, generation: generation},
        %{snapshot_ref: ref, snapshot_size_bytes: size, generation: generation, node_id: node_id, vm_id: nil}
      )

    Logger.info("embervm serving banked",
      instance_id: instance.instance_id,
      workload: instance.workload,
      node_id: node_id,
      snapshot_bytes: size
    )

    state
  end

  defp finish_bank_active(state, instance, _node_id, {:error, reason}) do
    Logger.warning("embervm serving: bank failed, returning to fan-out",
      instance_id: instance.instance_id,
      workload: instance.workload,
      reason: inspect(reason)
    )

    # The bank RPC failed with the VM still alive and no snapshot written: abort back
    # to draining (ETS-only), then republish so the live VM re-enters the fan-out.
    _ = ServingStore.mark(state.store, instance.instance_id, :bank_abort)

    if republishable?(instance) do
      _ = ServingStore.publish(state.store, instance.instance_id, instance.ip, instance.port, :healthy)
      Embervm.EndpointPublisher.publish(state.publisher)
    end

    state
  end

  # -- lifetime expiry -------------------------------------------------------

  # Max-lifetime expiry: any instance older than maxLifetimeSeconds is destroyed
  # (drain-then-destroy for a live one, evict for a banked one) so the next miss
  # cold-starts on the CURRENT base. Unlike the idle bank this does NOT snapshot: an
  # over-lifetime instance's base lineage is exactly what we want to shed. Includes a
  # wake-time TTL check by construction: relight produces a `starting`/`published`
  # instance whose created_at is unchanged, so an over-lifetime banked instance is
  # expired here BEFORE a miss can relight it (and the activator's relight also sees
  # it terminal). v1 destroys a live over-lifetime instance WITHOUT the drainSeconds
  # wait (lifetime expiry is a hard cap; the in-flight drop is accepted, like a
  # session expiry).
  defp sweep_lifetime(state, now) do
    ServingStore.all(state.store)
    |> Enum.reject(&ServingState.terminal?(&1.state))
    |> Enum.filter(&over_lifetime?(state, &1, now))
    |> Enum.reduce(state, fn instance, acc -> expire_instance(acc, instance, now) end)
  end

  defp over_lifetime?(state, instance, now) do
    case serving_cfg(state, instance.workload) do
      {:ok, cfg} ->
        max_ms = cfg.max_lifetime_seconds * 1000
        is_integer(instance.created_at) and now - instance.created_at >= max_ms

      :error ->
        false
    end
  end

  # Expire one over-lifetime instance: a banked one is evicted (its snapshot is stale
  # lineage); any live one is destroyed (StopServing DESTROY on the node, then the
  # durable serving_destroyed). A published live one is unpublished first so the
  # endpoint leaves the fan-out (the activator installs atomically if it was the
  # last), then destroyed.
  defp expire_instance(state, %{state: :banked} = instance, _now) do
    evict_banked(state, instance, :lifetime)
  end

  defp expire_instance(state, instance, _now) do
    # Pull a published instance from the fan-out first. A lifetime drain is neither a
    # bank drain nor a health drain, but the drain_reason does not gate anything here
    # because we destroy immediately (the row goes terminal in the same sweep). We
    # unpublish only to emit the EDS update + activator swap, then destroy.
    if instance.state == :published do
      _ = ServingStore.unpublish(state.store, instance.instance_id, :destroyed)
      Embervm.EndpointPublisher.publish(state.publisher)
    end

    destroy_instance(state, instance, :lifetime)
  end

  # -- banked-TTL GC ---------------------------------------------------------

  # A banked instance untouched (last_active_at, else banked-at via updated_at) for
  # longer than bankedTtlSeconds is evicted (serving_evicted, reason idle_ttl), the
  # exact session banked-TTL GC.
  defp sweep_banked_ttl(state, now) do
    ServingStore.all(state.store)
    |> Enum.filter(&(&1.state == :banked))
    |> Enum.reduce(state, fn instance, acc ->
      case serving_cfg(acc, instance.workload) do
        {:ok, cfg} ->
          ttl_ms = cfg.banked_ttl_seconds * 1000
          last = instance.last_active_at || instance.updated_at || 0

          if now - last >= ttl_ms do
            evict_banked(acc, instance, :idle_ttl)
          else
            acc
          end

        :error ->
          acc
      end
    end)
  end

  # -- stale-base lineage GC (D-R3.11.3 follow-up) ---------------------------

  # Evict banked snapshots born from a SUPERSEDED base: after a runtime roll the node
  # reports a new serving_image_ref, so a snapshot whose base_snapshot_ref no longer
  # matches would relight OLD code. pick_bankable_instance already refuses to relight
  # these; this pass reclaims them so they do not squat node disk indefinitely (the
  # accumulation the R3 serving drill surfaced). Only a NON-EMPTY current ref that
  # DIFFERS marks staleness: an absent ref means the new base is not built yet, so the
  # snapshot is kept (fail-open, matching the relight lineage check). Live instances
  # are untouched (only :banked rows), and the node's evict guard already refuses to
  # remove a snapshot a live VM was relit from.
  defp sweep_stale_lineage(state, _now) do
    ServingStore.all(state.store)
    |> Enum.filter(&(&1.state == :banked))
    |> Enum.reduce(state, fn instance, acc ->
      if stale_lineage?(acc.capacity_table, instance) do
        evict_banked(acc, instance, :stale_base)
      else
        acc
      end
    end)
  end

  defp stale_lineage?(capacity_table, instance) do
    case Embervm.WakeInstance.current_serving_image_ref(
           capacity_table,
           instance.node_id,
           instance.workload
         ) do
      ref when is_binary(ref) and ref != "" -> ref != instance.base_snapshot_ref
      _ -> false
    end
  end

  # -- forced roll -----------------------------------------------------------

  # Drain + DESTROY every live instance of the workload and evict its banked
  # snapshots, so the next miss cold-creates on the current base. No snapshot, no
  # drainSeconds wait (operator override). The last live unpublish installs the
  # activator atomically (the publisher re-derives once at the end).
  defp do_force_roll(state, workload) do
    instances = ServingStore.list(state.store, workload)
    now = state.clock.()

    {state, destroyed, evicted} =
      Enum.reduce(instances, {state, 0, 0}, fn instance, {acc, d, e} ->
        cond do
          ServingState.terminal?(instance.state) ->
            {acc, d, e}

          instance.state == :banked ->
            {evict_banked(acc, instance, :forced_roll), d, e + 1}

          true ->
            {force_destroy(acc, instance, now), d + 1, e}
        end
      end)

    # One re-derive after the batch: every removed endpoint (and the activator swap
    # for the now-empty cluster) lands in a single EDS update.
    Embervm.EndpointPublisher.publish(state.publisher)

    Logger.info("embervm serving: forced roll", workload: workload, destroyed: destroyed, evicted: evicted)
    {%{destroyed: destroyed, evicted: evicted}, state}
  end

  # Force-destroy one live instance: unpublish (if published) to drop the endpoint,
  # cancel any in-flight bank-drain bookkeeping, then StopServing(DESTROY) + the
  # durable serving_destroyed. The publisher re-derive is batched by the caller.
  defp force_destroy(state, instance, _now) do
    if instance.state == :published do
      _ = ServingStore.unpublish(state.store, instance.instance_id, :destroyed)
    end

    state = %{state | draining: Map.delete(state.draining, instance.instance_id)}
    destroy_instance(state, instance, :forced_roll)
  end

  # -- destroy / evict RPCs + durable ops ------------------------------------

  # Destroy a live instance. The gate-off branch is the existing best-effort
  # StopServing(DESTROY) followed by serving_destroyed path. The ADR embervm/014
  # branch durably records destroying first and waits for the daemon's real
  # teardown_confirmed response before recording serving_destroyed.
  defp destroy_instance(state, instance, reason) do
    if state.node_confirmed_destroy do
      destroy_instance_node_confirmed(state, instance, reason)
    else
      destroy_instance_legacy(state, instance, reason)
    end
  end

  defp destroy_instance_legacy(state, instance, reason) do
    _ = stop_serving_destroy(state, instance)

    _ =
      ServingStore.transition(
        state.store,
        instance.instance_id,
        :destroy,
        :serving_destroyed,
        %{reason: reason},
        %{}
      )

    state
  end

  defp destroy_instance_node_confirmed(state, instance, reason) do
    intent =
      ServingStore.transition(
        state.store,
        instance.instance_id,
        :begin_destroy,
        :serving_destroying,
        %{reason: reason},
        %{}
      )

    case intent do
      {:ok, _} ->
        # A row without a reachable VM holds nothing on a node, so teardown is
        # confirmed by construction. Otherwise this issues the real StopServing
        # destroy RPC and accepts only the daemon's teardown_confirmed=true reply.
        confirmed =
          if is_binary(instance.node_id) and is_binary(instance.vm_id) do
            stop_serving_destroy_confirmed(state, instance)
          else
            true
          end

        if confirmed do
          _ =
            ServingStore.transition(
              state.store,
              instance.instance_id,
              :destroy,
              :serving_destroyed,
              %{reason: reason},
              %{}
            )

          state
        else
          Logger.warning("embervm serving teardown unconfirmed, left destroying",
            instance_id: instance.instance_id,
            workload: instance.workload,
            vm_id: instance.vm_id
          )

          state
        end

      {:error, _reason} ->
        state
    end
  end

  defp evict_banked(state, instance, reason) do
    # Serving snapshots reuse the node's own GC / the adoption orphan-evict path; the
    # durable serving_evicted records the intent and moves the row terminal so the
    # snapshot is no longer a relight target. (v1 does not issue a separate evict RPC
    # here; the activator's reconcile evicts the orphan on the node, matching the
    # merged evict_orphan_snapshots path.)
    # R6, Task 9: the store copy of the bundle DOES need dropping even though local
    # disk is reclaimed lazily by the node's own GC, so fire the remote EvictArtifact
    # alongside the durable transition (banked TTL, stale lineage, and forced roll all
    # route through here).
    _ = evict_remote_bundle(state, instance)

    _ =
      ServingStore.transition(
        state.store,
        instance.instance_id,
        :evict,
        :serving_evicted,
        %{reason: reason},
        %{}
      )

    Logger.info("embervm serving evicted",
      instance_id: instance.instance_id,
      workload: instance.workload,
      reason: reason
    )

    state
  end

  # R6, Task 9: drop the store copy of a banked SERVING bundle (EvictArtifact,
  # remote=true, kind SERVING) alongside the local eviction transition. Best-effort: a
  # failure never wedges the sweep (the durable transition is authoritative; a
  # stranded store copy is swept later by the remote-orphan reconcile). Idempotent on
  # the daemon; an already-absent store copy is a no-op.
  defp evict_remote_bundle(state, %{node_id: node_id, snapshot_ref: ref, workload: workload})
       when is_binary(node_id) and is_binary(ref) and ref != "" do
    artifact = %ArtifactRef{kind: :ARTIFACT_KIND_SERVING, workload: workload, ref: ref}
    req = %EvictArtifactRequest{artifact: artifact, remote: true, trace: %Trace{workload: workload}}
    # Dial the instance holding the banked serving bundle on disk, not the alias.
    dial_key = dial_for_serving_bundle(state, node_id, ref)

    with {:ok, channel} <- safe_channel(state.channel_fun, dial_key) do
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

  defp evict_remote_bundle(_state, _instance), do: :ok

  defp stop_serving_destroy(state, %{node_id: node_id, vm_id: vm_id})
       when is_binary(node_id) and is_binary(vm_id) do
    req = %StopServingRequest{trace: %Trace{}, vm_id: vm_id, mode: :STOP_SERVING_MODE_DESTROY}
    # Dial the OWNING instance running this serving vm_id, not the node-name alias.
    dial_key = dial_for_serving_vm(state, node_id, vm_id)

    with {:ok, channel} <- safe_channel(state.channel_fun, dial_key) do
      try do
        state.stop_serving_fun.(channel, req)
      rescue
        _ -> :error
      catch
        _, _ -> :error
      end
    end

    :ok
  end

  defp stop_serving_destroy(_state, _instance), do: :ok

  # The node-confirmed destroy path uses the same real StopServing(DESTROY) RPC as
  # the legacy sweeper path, but requires the daemon's explicit confirmation. RPC,
  # dial, and fault failures are all unconfirmed and leave the durable row in
  # destroying for ServingManager reconciliation to re-drive.
  defp stop_serving_destroy_confirmed(state, %{node_id: node_id, vm_id: vm_id})
       when is_binary(node_id) and is_binary(vm_id) do
    req = %StopServingRequest{trace: %Trace{}, vm_id: vm_id, mode: :STOP_SERVING_MODE_DESTROY}
    dial_key = dial_for_serving_vm(state, node_id, vm_id)

    with {:ok, channel} <- safe_channel(state.channel_fun, dial_key) do
      try do
        match?({:ok, %{teardown_confirmed: true}}, state.stop_serving_fun.(channel, req))
      rescue
        _ -> false
      catch
        _, _ -> false
      end
    else
      _ -> false
    end
  end

  defp stop_serving_destroy_confirmed(_state, _instance), do: false

  # -- per-node bank cap -----------------------------------------------------

  defp bank_at_cap?(state, node_id) do
    Map.get(state.bank_inflight, node_id, 0) >= state.bank_concurrency
  end

  defp incr_bank_inflight(state, node_id) do
    %{state | bank_inflight: Map.update(state.bank_inflight, node_id, 1, &(&1 + 1))}
  end

  defp decr_bank_inflight(state, node_id) do
    %{state | bank_inflight: Map.update(state.bank_inflight, node_id, 0, &max(&1 - 1, 0))}
  end

  # -- stats scrape helpers --------------------------------------------------

  defp serving_nodes(state) do
    NodeCapacity.all(state.capacity_table)
    |> Enum.filter(&serving_capable?/1)
  end

  defp serving_capable?(fact) do
    cidr = Map.get(fact, :serving_subnet_cidr)
    is_binary(cidr) and cidr != ""
  end

  # v1: one serving node reached via the one serving Service stats URL. The
  # capacity fact selects WHETHER to scrape (a node must be serving-capable); the URL
  # is the Service DNS (stats_base), matching how the EndpointPublisher PUTs the one
  # loopback sidecar for every node. Multi-node needs a per-node stats address here
  # (a data change), same seam as the publisher's per-node PUT.
  defp scrape_node(%{stats_base: nil}, _node), do: {:error, :no_stats_base}

  defp scrape_node(state, _node) do
    state.scrape_fun.(stats_url(state.stats_base))
  end

  defp stats_url(base), do: String.trim_trailing(base, "/") <> "/stats?format=json"

  # -- catalog + instance helpers --------------------------------------------

  defp serving_cfg(state, workload) do
    case WorkloadCatalog.fetch(state.catalog_table, workload) do
      {:ok, %{class: "serving", serving: cfg}} when is_map(cfg) -> {:ok, cfg}
      _ -> :error
    end
  end

  defp live_instance?(instance) do
    not ServingState.terminal?(instance.state) and instance.state != :banked
  end

  defp idle?(instance, idle_ms, now) do
    now - idle_baseline(instance) >= idle_ms
  end

  # The instant an instance was last known active: last_active_at if the activity
  # touch has ever fired, else its created_at (a never-active instance idles from
  # birth). Never nil, so the idle window is always well-defined.
  defp idle_baseline(instance) do
    instance.last_active_at || instance.created_at || 0
  end

  defp group_by_workload(instances) do
    Enum.group_by(instances, & &1.workload)
  end

  defp republishable?(instance) do
    is_binary(instance.ip) and instance.ip != "" and is_integer(instance.port)
  end

  # The op principal a serving usage/lifecycle op is attributed to: serving is
  # workload-scoped (one owner), so the workload's system owner principal, exactly
  # the activator's wake_principal.
  defp usage_principal(workload), do: "system:serving:#{workload}"

  # cluster.serve|<workload>.upstream_rq_total -> <workload>, or nil for a cluster
  # name that is not a serving cluster (the scrape returns EVERY Envoy cluster,
  # including xds_cluster/admin_loopback, which we ignore).
  @cluster_prefix "serve|"
  defp workload_of_cluster(@cluster_prefix <> workload) when workload != "", do: workload
  defp workload_of_cluster(_other), do: nil

  # -- daemon + timer seams --------------------------------------------------

  defp safe_channel(channel_fun, node_id) do
    channel_fun.(node_id)
  rescue
    e -> {:error, {:channel_raised, e}}
  catch
    kind, reason -> {:error, {:channel_raised, {kind, reason}}}
  end

  # The channel key for a live serving-VM dial (bank/destroy): the OWNING instance
  # reporting `vm_id` in serving_vms, else the node name (single-instance / legacy
  # fallback). Instance-key unification (PR-B0b): a co-located node's node-name alias
  # resolves to an arbitrary sibling brick, so a bank/destroy against the alias
  # misroutes to a brick that never ran the VM.
  defp dial_for_serving_vm(state, node_id, vm_id) do
    Embervm.WakeInstance.dial_for_serving_vm(state.capacity_table, node_id, vm_id)
  end

  # The channel key for a serving-bundle dial (EvictArtifact SERVING): the instance
  # holding the bundle on disk (serving_snapshots), else the node name.
  defp dial_for_serving_bundle(state, node_id, snapshot_ref) do
    Embervm.WakeInstance.dial_for_serving_bundle(state.capacity_table, node_id, snapshot_ref)
  end

  defp default_stop_serving(channel, req) do
    Embervm.Node.V1.NodeService.Stub.stop_serving(channel, req)
  end

  defp default_evict_artifact(channel, req) do
    Embervm.Node.V1.NodeService.Stub.evict_artifact(channel, req)
  end

  # Production scrape: GET /stats?format=json over the shared Finch pool, parse the
  # per-cluster upstream_rq_total counters into %{cluster_name => rq_total}. A
  # non-200, a transport error, or an unparseable body is a scrape failure (fail-open
  # upstream). Envoy's ?format=json emits {"stats":[{"name":..,"value":..}, ...]};
  # we keep only the cluster.*.upstream_rq_total gauges and strip to the raw cluster
  # name (cluster.<name>.upstream_rq_total -> <name>).
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

  @rq_total_suffix ".upstream_rq_total"
  @cluster_stat_prefix "cluster."

  @doc """
  Parse an Envoy `/stats?format=json` body into `%{cluster_name => rq_total}`, keeping
  only `cluster.*.upstream_rq_total` counters and stripping to the raw cluster name.
  Public (`@doc false`) ONLY so the parse path (prefix/suffix stripping, noise
  filtering) is exercised on a REALISTIC raw body in tests; production reaches it via
  `default_scrape`. Not part of the module's API.
  """
  @doc false
  @spec parse_stats(binary()) :: {:ok, %{optional(String.t()) => integer()}} | {:error, term()}
  def parse_stats(body) do
    case Jason.decode(body) do
      {:ok, %{"stats" => stats}} when is_list(stats) ->
        reading =
          for %{"name" => name, "value" => value} <- stats,
              String.starts_with?(name, @cluster_stat_prefix),
              String.ends_with?(name, @rq_total_suffix),
              is_integer(value),
              into: %{} do
            {cluster_name_of(name), value}
          end

        {:ok, reading}

      _ ->
        {:error, :unparseable_stats}
    end
  rescue
    e -> {:error, {:stats_parse_raised, e}}
  end

  # cluster.<name>.upstream_rq_total -> <name>. The name can itself contain dots (the
  # cluster is serve|<workload>, and a workload may contain none here), so strip the
  # fixed prefix and suffix rather than split on dots. replace_prefix/replace_suffix
  # (NOT trim_leading/trim_trailing) so the fixed affixes are removed as WHOLE
  # substrings exactly once: trim_leading("cluster.", ...) would risk eating into the
  # name, replace_prefix removes precisely the literal prefix and nothing more.
  defp cluster_name_of(name) do
    name
    |> String.replace_prefix(@cluster_stat_prefix, "")
    |> String.replace_suffix(@rq_total_suffix, "")
  end

  defp default_timer(msg, delay_ms) when delay_ms > 0 do
    Process.send_after(self(), msg, delay_ms)
  end

  defp default_timer(msg, _delay_ms) do
    send(self(), msg)
  end

  defp schedule(msg, interval_ms) when interval_ms > 0 do
    Process.send_after(self(), msg, interval_ms)
  end

  defp schedule(_msg, _interval), do: :ok

  defp default_clock, do: System.system_time(:millisecond)
end
