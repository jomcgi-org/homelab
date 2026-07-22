defmodule Embervm.StatefulSweeper do
  @moduledoc """
  The stateful lifecycle-economics loop (R4, Task 9): the L4 counterpart of
  `Embervm.ServingSweeper`, and the piece that makes an idle stateful sandbox
  actually cost one volume file plus one bundle rather than a running VM
  forever. Like serving, a stateful instance has NO per-instance supervised
  process (it is an ETS row in `Embervm.StatefulStore`), so this ONE process
  drives every economic decision on a timer: it scrapes each node Envoy's L4
  connection counters to detect idleness, banks idle instances (unpublish then
  StopStateful BANK), expires over-lifetime ones, GCs stale banked bundles, and
  keeps the pairing hygiene current between wakes.

  ## the idle signal: opaque L4, not request counts

  Serving is HTTP, so its idle signal is a request-count delta on the node
  Envoy's cluster. Stateful is opaque L4 TCP (a raw byte stream to, say,
  Postgres), so there is no request boundary to count. Instead the signal is
  the node Envoy's PER-LISTENER tcp_proxy connection stats (Task 5 set
  `stat_prefix` to the listener name, `state-<listenPort>`):

    * `downstream_cx_active`: the number of currently-open connections. This
      MUST be zero before a bank is even considered (standing decision 7: NEVER
      sever a live connection to pause the VM underneath it).
    * `downstream_cx_total`: the CUMULATIVE connection count. A zero delta
      across `idleBankSeconds` means no new connection arrived either, so the
      workload is truly quiet, not merely between two long-lived connections
      that both happen to be momentarily closed.

  An instance is idle-bankable iff `downstream_cx_active == 0` AND the
  `downstream_cx_total` delta across the window is `0`. The active check is
  RE-READ with a fresh scrape at bank time too (the recheck below), because a
  connection racing in between the tick-start idle scan and the actual bank
  action is exactly the case decision 7 exists to catch.

  ## fail-open for warmth (ADR embervm/001 posture), exactly as serving

  A scrape failure yields no reading for that node this tick: idle detection is
  suppressed for every stateful workload on that node, never banking on missing
  or stale stats. Lifetime/TTL GC still run (they read the durable store, not
  stats). A fresh sweeper's first tick only baselines (no prior reading to
  delta against), banking nothing; the same code path a scrape failure takes.

  ## the bank sequence (standing decision 7, no drain window)

  Serving's idle bank waits `drainSeconds` after unpublish so an in-flight HTTP
  request can finish before the VM is paused; a stateful L4 connection has no
  such natural completion boundary (it may be a long-lived session), so the
  precondition IS the wait: banking only begins once `downstream_cx_active` has
  already read zero for the whole idle window. The sequence is therefore:

    1. `StatefulStore.unpublish` (serving -> banking, ETS-only per the store's
       "unpublish is ETS-only" decision): the endpoint leaves the fan-out and,
       if this was the workload's only route to a live instance, the SAME
       `EndpointPublisher.publish` call installs the activator TCP fallback so
       a racing connection parks and wakes fresh instead of hitting a dead
       socket.
    2. RECHECK `downstream_cx_active` with a FRESH re-scrape (not the
       tick-start reading the idle scan acted on): if a connection opened
       between the idle decision and this point, the bank ABORTS
       (`bank_abort`, banking -> serving) and the instance is republished
       immediately. This is the decision-7 guard actually enforced at the
       moment it matters, not just at the idle-scan moment a tick earlier.
    3. `StopStateful(BANK)` via the node stub (spawned off this process,
       exactly like serving's bank worker, since it writes GiBs and can take
       seconds).
    4. On success: `StatefulStore.transition(:bank_ready, :stateful_banked,
       ...)` with `{snapshot_ref, generation, size_bytes}` (the pair-key
       baseline the wake path checks), then `EndpointPublisher.publish` again
       (a no-op fan-out-wise, since the endpoint already left, but keeps the
       publisher's derived state current).
    5. On RPC failure: `StatefulStore.mark(:bank_abort)` (banking -> serving,
       ETS-only: no snapshot was written, the VM is still alive) and
       republish, so the instance stays live and the next sweep retries.

  ## max-lifetime: bounded drain patience, then destroy anyway (decision 8)

  An instance older than `maxLifetimeSeconds` must eventually go so a stale
  base lineage cannot squat forever, but destroying a stateful VM mid-connection
  loses whatever the client was doing (there is no HTTP retry semantics to fall
  back on). The compromise (standing decision 8): wait for
  `downstream_cx_active == 0` up to a capped patience window
  (`EMBERVM_STATEFUL_LIFETIME_DRAIN_MAX_MS`, default 1 hour), then destroy
  regardless. This is safe because the class carries a durable, node-local
  volume and the guest's own WAL (or equivalent) recovers a mid-write
  interruption on the NEXT boot; the alternative (waiting forever for a
  long-lived idle-looking connection to close) would let a single stuck client
  pin a workload on an ancient base indefinitely. The patience window is
  tracked per-instance from the first tick it was seen both over-lifetime AND
  active (never from creation, so a workload that only recently exceeded its
  lifetime gets the full window, not a window already half-consumed by however
  long it has been over).

  ## banked-TTL GC: the bundle, never the volume

  A banked bundle untouched (`updated_at`, the banked-at or last-touched
  timestamp) longer than `bankedTtlSeconds` is evicted: `EvictSnapshot` on the
  node (reclaiming the bundle's disk, the same RPC session snapshots reuse per
  the R2 `EvictSnapshot` doc) plus the durable `stateful_evicted` transition,
  reason `"ttl"`. The VOLUME is NEVER touched by this GC (or by anything in
  this module): the volume is the durable data, the bundle is only a warm-start
  shortcut to resuming a live VM against it. Losing the bundle costs one cold
  boot on the next wake; losing the volume would lose data. `DELETE
  /v1/stateful/:name/volume` (Task 8's management route, already wired) is the
  ONLY destructive-to-data verb in the system, and this sweeper never calls it.

  ## eager broken-pair eviction, every tick

  `StatefulStore.eager_evict_broken_pairs/1` is called on every sweep so a
  bundle whose pair broke (the volume's generation moved out from under it,
  e.g. via a cold boot from a DIFFERENT banked-then-evicted lineage, or an
  adoption reconcile that refreshed the volume fact) is evicted BEFORE the next
  wake attempt discovers it stale, not AT wake time (which would cost the
  connecting client an extra round trip through the discovery). This mirrors
  `StatefulManager.reconcile/1`'s post-refresh eager-evict call; running it
  here too means the hygiene runs on the sweep cadence even between
  reconciles.

  ## no stale-base lineage GC (unlike serving)

  `Embervm.ServingSweeper` carries a `sweep_stale_lineage` pass that evicts a
  banked serving snapshot whose base was superseded by a runtime roll. There is
  deliberately no counterpart here: a stateful bundle is paired to its VOLUME's
  generation (`snapshot_generation == volume.generation`), not to a base image
  lineage, so the equivalent staleness is exactly a broken pair, already handled
  by `eager_evict_broken_pairs` above. A runtime image roll for a stateful
  workload surfaces as a cold boot on the next wake (the daemon boots the current
  image against the intact volume), not as a squatting stale snapshot to GC.

  ## economics: what R4 buys

  Before this task, an idle stateful workload keeps its VM (and the vCPU/mem it
  holds) running forever: the "actually cost one volume file + one bundle" goal
  only exists once idle instances bank. This sweeper is the piece that turns
  the class's promised economics (pay for compute only while a client is
  connected, pay for storage always) into an enforced behavior.
  """

  use GenServer
  require Logger

  require OpenTelemetry.Tracer, as: Tracer

  alias Embervm.{NodeCapacity, StatefulManager, StatefulState, StatefulStore, WorkloadCatalog}
  alias Embervm.OpLog.Op

  alias Embervm.Node.V1.{
    ArtifactRef,
    EvictArtifactRequest,
    EvictSnapshotRequest,
    ResolveStatefulRequest,
    ResolveStatefulResponse,
    StopStatefulRequest,
    StopStatefulResponse,
    Trace
  }

  # Interruptible bank (ADR embervm/008): after this many CONSECUTIVE aborts of a
  # workload's checkpoint (a client keeps racing back in the moment we pause), the
  # next cycle FORCES a commit regardless of a parked connection, so a hot-looping
  # client cannot pin the VM live forever and defeat the economics. Reset to 0 the
  # moment the workload settles to banked.
  @default_flap_abort_threshold 20

  # The unpublish -> node-Envoy propagation settle bound (ms) an interruptible
  # bank waits after dropping the endpoint before it pauses the VM with a
  # CHECKPOINT, so a connection the fan-out was still routing at unpublish time
  # has drained to the activator rather than being severed mid-pause. Waited
  # inside the spawned bank worker (never blocking the GenServer); 0-able so tests
  # run instantly.
  @default_propagation_settle_ms 200

  # Per-node concurrent-bank cap (banking writes GiBs; serialize per node), the
  # exact ServingSweeper default and rationale.
  @default_bank_concurrency 1

  # The capped patience window for a live over-lifetime instance to drain
  # (downstream_cx_active reach zero) before it is destroyed anyway (decision
  # 8). One hour default: long enough that a genuinely finishing session is not
  # cut off, short enough that a stuck client cannot pin an ancient base
  # forever.
  @default_lifetime_drain_max_ms 3_600_000

  # Bank-retry backoff (instance-key unification PR-B0a). A NON-terminal bank
  # failure (a transient transport/daemon error, not an unknown-vm
  # FAILED_PRECONDITION) aborts+republishes and defers the next bank attempt for
  # this workload, doubling from @bank_backoff_base_ms up to @bank_backoff_cap_ms.
  # Without it a persistent failure re-drives the bank at the 1 s sweep frequency;
  # with it a stuck workload backs off to ~0.03 Hz. A successful bank (or a bank
  # abort for a raced-in connection, which is not a failure) clears the entry.
  @bank_backoff_base_ms 1_000
  @bank_backoff_cap_ms 30_000

  # -- Client API ------------------------------------------------------------

  @spec start_link(keyword()) :: GenServer.on_start()
  def start_link(opts) do
    case Keyword.get(opts, :name, __MODULE__) do
      nil -> GenServer.start_link(__MODULE__, opts)
      name -> GenServer.start_link(__MODULE__, opts, name: name)
    end
  end

  @doc """
  Runs one full sweep synchronously (scrape + stateful_stats + idle-bank +
  recheck-and-bank + lifetime GC + banked-TTL GC + eager broken-pair eviction)
  and returns after it completes. Tests drive the economics deterministically
  through this (with an injected clock + stats seam) instead of waiting on the
  timer.
  """
  @spec sweep(GenServer.server()) :: :ok
  def sweep(server \\ __MODULE__) do
    GenServer.call(server, :sweep, :infinity)
  end

  @doc """
  Whether `workload`'s volume is eligible to have any of its artifacts (its
  banked STATEFUL bundle, or in a later rung its VOLUME snapshot/backup)
  planned for export (R7, ADR embervm/011, standing decision 4): false while
  the volume is quarantined (`StatefulStore.quarantined?/2`), true otherwise
  (including an unknown workload, which has no volume to quarantine). No
  ExportArtifact planning exists in this control plane yet (Task 10 lands the
  Longhorn-backed snapshot/backup planning); this predicate is the guard
  future export-planning call sites (and `eager_evict_broken_pairs`'s bundle
  eviction, see `do_sweep/1`) must consult FIRST, so quarantine enforcement
  lands before there is anything to enforce it against.
  """
  @spec export_allowed?(GenServer.server(), String.t()) :: boolean()
  def export_allowed?(store \\ StatefulStore, workload) do
    not StatefulStore.quarantined?(store, workload)
  end

  # -- GenServer callbacks ---------------------------------------------------

  @impl true
  def init(opts) do
    state = %{
      store: Keyword.get(opts, :store, StatefulStore),
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
      # The node Envoy stats scrape seam: (stats_url) -> {:ok, %{stat_prefix =>
      # %{active: n, total: n}}} | {:error, reason}. Production GETs
      # /stats?format=json over Finch and parses the per-listener
      # tcp.<prefix>.downstream_cx_{active,total} counters; tests inject
      # scripted readings.
      scrape_fun: Keyword.get(opts, :scrape_fun, &default_scrape/1),
      # The base URL of the node Envoy's stats port. Reuses the SAME serving
      # stats endpoint (EMBERVM_SERVING_STATS_BASE): stateful's tcp_proxy
      # listeners live on the same node Envoy admin the serving scrape already
      # reaches. nil disables the scrape (fails open: no idle-bank decision
      # ever runs), the safe default.
      stats_base: Keyword.get(opts, :stats_base, nil),
      # The daemon stateful-verb seams (injected for tests; production dials
      # the real NodeService stub over the shared NodeChannel).
      channel_fun: Keyword.get(opts, :channel_fun, &Embervm.NodeChannel.get/1),
      # Drop the shared node channel when a verb reveals its transport is dead (a
      # noded rollout), so the next safe_channel/2 re-dials instead of hammering a
      # dead ConnectionProcess forever (D-R2.7.2). Injected for tests.
      invalidate_fun: Keyword.get(opts, :invalidate_fun, &Embervm.NodeChannel.invalidate/2),
      stop_stateful_fun: Keyword.get(opts, :stop_stateful_fun, &default_stop_stateful/2),
      # ResolveStateful seam (ADR embervm/008): (channel, %ResolveStatefulRequest{})
      # -> {:ok, %ResolveStatefulResponse{}} | {:error, _}. Injected for tests;
      # production dials the real NodeService stub.
      resolve_stateful_fun: Keyword.get(opts, :resolve_stateful_fun, &default_resolve_stateful/2),
      evict_snapshot_fun: Keyword.get(opts, :evict_snapshot_fun, &default_evict_snapshot/2),
      # Remote artifact eviction seam (R6, Task 9): (channel, %EvictArtifactRequest{})
      # -> {:ok, %EvictArtifactResponse{}} | {:error, _}. Fired alongside the local
      # EvictSnapshot so the store copy of a banked bundle is dropped on the same
      # triggers (banked TTL, superseded generation). Injected for tests; production
      # dials the real NodeService stub.
      evict_artifact_fun: Keyword.get(opts, :evict_artifact_fun, &default_evict_artifact/2),
      # status.stateful {state,generation,bundleGeneration,volumeBytes} writer
      # (Task 10). Defaults to the K8s merge-patch on the workload status
      # subresource, the same seam ServingSweeper.status_writer uses; tests inject
      # a recorder. Disjoint status keys (stateful/statefulSummary) from
      # status.serving/status.sessions and the watcher, so the merge-patch never
      # clobbers another writer.
      status_writer: Keyword.get(opts, :status_writer, &Embervm.K8s.patch_workload_status/3),
      # workload -> last-written {state, generation, bundleGeneration,
      # volumeBytes} tuple, so the sweep patches only a workload whose values
      # changed (debounce: at most one API call per stateful workload per sweep,
      # never per transition). Mirrors ServingSweeper.serving_status_written.
      stateful_status_written: %{},
      bank_concurrency: Keyword.get(opts, :bank_concurrency, @default_bank_concurrency),
      bank_inflight: %{},
      # -- interruptible bank (ADR embervm/008) ------------------------------
      # The StatefulManager ref, so the sweeper can ask parked?/2 (commit-vs-abort
      # input) and cast {:checkpoint_resolved, ...} to it on resolve.
      manager: Keyword.get(opts, :manager, Embervm.StatefulManager),
      # workload -> boolean: whether a connection is parked for it RIGHT NOW.
      # Defaults to StatefulManager.parked?/2 against the manager ref; tests inject
      # a fun (or a fake) to drive the commit/abort fork deterministically.
      parked_fun:
        Keyword.get(opts, :parked_fun, fn wl ->
          StatefulManager.parked?(Keyword.get(opts, :manager, Embervm.StatefulManager), wl)
        end),
      # workload -> consecutive-abort count, for the flap guard (force-commit after
      # the threshold so a hot-looping client cannot pin the VM live forever).
      checkpoint_aborts: %{},
      flap_abort_threshold: Keyword.get(opts, :flap_abort_threshold, @default_flap_abort_threshold),
      # workload -> {next_attempt_at_ms, current_backoff_ms}: a non-terminal bank
      # failure defers this workload's next bank attempt (exponential, capped), so
      # a persistent daemon-side wedge loops at ~0.03 Hz not the 1 s sweep. Cleared
      # on a successful bank. Overridable for tests.
      bank_backoff: %{},
      bank_backoff_base_ms: Keyword.get(opts, :bank_backoff_base_ms, @bank_backoff_base_ms),
      bank_backoff_cap_ms: Keyword.get(opts, :bank_backoff_cap_ms, @bank_backoff_cap_ms),
      propagation_settle_ms: Keyword.get(opts, :propagation_settle_ms, @default_propagation_settle_ms),
      lifetime_drain_max_ms: Keyword.get(opts, :lifetime_drain_max_ms, @default_lifetime_drain_max_ms),
      # workload -> the wall-clock ms the workload was first observed idle
      # (cx_active == 0 AND cx_total delta == 0) this run, so idleBankSeconds is
      # measured from the FIRST idle tick, not re-armed every tick a scrape
      # happens to re-confirm zero. A nonzero delta or nonzero active resets
      # (deletes) the entry: any activity restarts the idle clock.
      idle_since: %{},
      # instance_id -> workload, for an instance currently unpublished
      # (banking) awaiting the recheck-and-bank step within the SAME sweep, or
      # whose bank RPC is in flight in a spawned worker. Prevents a second tick
      # from re-arming a bank already in progress.
      banking: %{},
      # The set of workloads whose node is DRAINING (R6, ADR embervm/009): a
      # drain-forced bank bypasses the recheck raced-in/scrape/cap aborts and
      # forces COMMIT even against a parked connection (spot semantics: the parked
      # caller re-wakes against the new noded). Added in force_bank_node, cleared
      # when the bank/resolve for that workload completes.
      draining_workloads: MapSet.new(),
      # workload -> the wall-clock ms an over-lifetime LIVE instance was first
      # seen both over-lifetime and with an active connection, so the drain
      # patience window is measured from THAT point (never from created_at, so
      # a workload only recently over lifetime gets the full window).
      lifetime_drain_since: %{},
      # The prior tick's per-node stats reading: node_id -> %{stat_prefix =>
      # %{active, total}}. A delta needs two consecutive successful readings; a
      # failed scrape drops the node's prior reading so the next success
      # re-establishes a baseline.
      last_stats: %{},
      # THIS tick's per-node reading, kept alongside last_stats so the bank
      # recheck can consult the freshest read without a second network hop
      # (the reading a moment old within the same tick is fresh enough; the
      # correctness the recheck buys is against races that happened BEFORE
      # this tick's scrape, not within it).
      current_stats: %{},
      sweep_interval_ms: Keyword.get(opts, :sweep_interval_ms, 0)
    }

    if state.sweep_interval_ms > 0 do
      schedule(:sweep, state.sweep_interval_ms)
    end

    {:ok, state}
  end

  @doc """
  Force-bank every live stateful instance on a draining node (R6, ADR embervm/009).

  Called by the DrainCoordinator on the drain rising edge. Returns the count of
  instances whose bank was started. Each is banked with COMMIT-despite-parked
  semantics (a parked caller re-wakes against the new noded); the idle predicate,
  the recheck raced-in/scrape aborts, and the per-node bank cap are all bypassed.
  """
  @spec drain_node(GenServer.server(), String.t()) :: non_neg_integer()
  def drain_node(server \\ __MODULE__, node_id) do
    GenServer.call(server, {:drain_node, node_id}, :infinity)
  end

  @impl true
  def handle_call(:sweep, _from, state) do
    {:reply, :ok, do_sweep(state)}
  end

  def handle_call({:drain_node, node_id}, _from, state) do
    {count, state} = force_bank_node(state, node_id)
    {:reply, count, state}
  end

  @impl true
  def handle_info(:sweep, state) do
    state = do_sweep(state)
    schedule(:sweep, state.sweep_interval_ms)
    {:noreply, state}
  end

  # The async StopStateful(BANK) worker finished: complete the durable
  # transition + inflight release on this serialized process. ip/port are the
  # endpoint captured BEFORE unpublish cleared the ETS row (see
  # spawn_bank_worker/2's comment); needed to restore it on a failed bank.
  def handle_info({:bank_done, instance_id, node_id, vm_id, ip, port, workload, owner_resolved, outcome}, state) do
    # Atomic bank finished: clear any drain mark (interruptible workloads clear at
    # resolve_done instead, after decide_resolve has read it).
    state = clear_draining(state, workload)
    {:noreply, finish_bank(state, instance_id, node_id, vm_id, ip, port, workload, owner_resolved, outcome)}
  end

  # The CHECKPOINT phase finished (ADR embervm/008): mark :checkpoint_ready and
  # fork commit-vs-abort (all serialized here), then spawn the resolve worker.
  def handle_info({:checkpoint_done, instance_id, node_id, vm_id, ip, port, workload, outcome}, state) do
    {:noreply, finish_checkpoint(state, instance_id, node_id, vm_id, ip, port, workload, outcome)}
  end

  # The ResolveStateful RPC finished: complete the durable transition, notify the
  # manager, and release the per-node bank slot + banking bookkeeping.
  def handle_info({:resolve_done, instance_id, node_id, vm_id, ip, port, workload, mode, outcome}, state) do
    # Interruptible bank resolved (COMMIT or ABORT): decide_resolve has already read
    # the drain mark, so clear it now.
    state = clear_draining(state, workload)
    {:noreply, finish_resolve(state, instance_id, node_id, vm_id, ip, port, workload, mode, outcome)}
  end

  def handle_info(_msg, state), do: {:noreply, state}

  # -- the sweep -------------------------------------------------------------

  # One tick: (1) scrape every stateful-capable node's L4 listener stats,
  # append stateful_stats + touch activity; (2) idle-bank: unpublish + recheck
  # + StopStateful(BANK) for instances confirmed idle past idleBankSeconds;
  # (3) max-lifetime expiry (drain-then-destroy, capped patience); (4)
  # banked-TTL GC; (5) eager broken-pair eviction.
  defp do_sweep(state) do
    now = state.clock.()

    # A ROOT span per sweep tick (timer-driven, no caller trace to nest under),
    # mirroring embervm.serving.stats_sweep.
    Tracer.with_span "embervm.stateful.stats_sweep", %{attributes: %{"ember.tenant" => state.tenant}} do
      state
      |> scrape_and_record(now)
      |> idle_bank_pass(now)
      |> sweep_lifetime(now)
      |> sweep_banked_ttl(now)
      |> eager_evict_broken_pairs()
      |> write_stateful_status()
    end
  end

  # -- status.stateful (Task 10) ----------------------------------------------

  # Write status.stateful {state, generation, bundleGeneration, volumeBytes} +
  # statefulSummary for every stateful-class workload, DEBOUNCED: only patch a
  # workload whose values changed since the last write. Runs on the sweep tick
  # (not per transition), so the K8s API is touched at most once per stateful
  # workload per sweep. Disjoint status keys (stateful/statefulSummary) from
  # status.serving/status.sessions/the watcher, so the merge-patch never clobbers
  # another writer. Mirrors ServingSweeper.write_serving_status/1 exactly.
  defp write_stateful_status(state) do
    stateful_workloads_with_ns(state)
    |> Enum.reduce(state, fn %{workload: workload, namespace: namespace}, acc ->
      fields = stateful_status_fields(acc, workload)
      quad = {fields.state, fields.generation, fields.bundle_generation, fields.volume_bytes}

      if Map.get(acc.stateful_status_written, workload) == quad do
        acc
      else
        _ = patch_stateful_status(acc, namespace, workload, fields)
        %{acc | stateful_status_written: Map.put(acc.stateful_status_written, workload, quad)}
      end
    end)
  end

  # Every cataloged stateful-class workload with its namespace, for the status
  # write. A catalog with no stateful workloads yields [], a no-op on
  # session/serving-only clusters. Mirrors ServingSweeper.serving_workloads_with_ns/1.
  defp stateful_workloads_with_ns(state) do
    for name <- WorkloadCatalog.all_names(state.catalog_table),
        {:ok, %{class: "stateful", namespace: namespace}} <- [WorkloadCatalog.fetch(state.catalog_table, name)],
        is_binary(namespace) do
      %{workload: name, namespace: namespace}
    end
  end

  # Reads the primary (non-terminal) instance's state + generation, the banked
  # instance's snapshot_generation for bundleGeneration, and the volume's
  # allocated_bytes for the watermark. Every field defaults to 0 (or "" for
  # state) when absent (no instance, no banked bundle, no volume row yet) so the
  # status-patch map below never carries a nil (the router's `:json.encode` nil
  # trap: OTP renders Elixir nil as the STRING "nil"). A workload with no banked
  # bundle reads bundleGeneration 0, which reads as a mismatch against a nonzero
  # live generation; that is the correct signal (no valid warm pair to relight),
  # not a false positive, since pair_valid?/1 (the actual wake-path decision)
  # never consults this status projection.
  defp stateful_status_fields(state, workload) do
    instances = StatefulStore.list(state.store, workload)
    # The status `state`/`generation` reflect the LIVE instance only. `:banked` is
    # non-terminal but holds no live VM, so a banked-only workload reads state ""
    # and generation 0; its banked-ness is carried by bundleGeneration instead.
    primary = Enum.find(instances, &(not StatefulState.terminal?(&1.state) and &1.state != :banked))
    banked = Enum.find(instances, &(&1.state == :banked))
    volume = StatefulStore.get_volume(state.store, workload)

    %{
      state: primary_state_string(primary),
      generation: (primary && primary.generation) || 0,
      bundle_generation: (banked && banked.snapshot_generation) || 0,
      volume_bytes: (volume && volume.allocated_bytes) || 0
    }
  end

  # The FSM state as the string the CRD's status.stateful.state documents, or ""
  # when no instance exists yet (never nil, per the no-nil-in-status-patch rule).
  defp primary_state_string(nil), do: ""
  defp primary_state_string(%{state: fsm_state}), do: Atom.to_string(fsm_state)

  defp patch_stateful_status(state, namespace, name, fields) do
    status_map = %{
      "stateful" => %{
        "state" => fields.state,
        "generation" => fields.generation,
        "bundleGeneration" => fields.bundle_generation,
        "volumeBytes" => fields.volume_bytes
      },
      "statefulSummary" => "#{fields.state} gen=#{fields.generation}/bundle=#{fields.bundle_generation}"
    }

    case state.status_writer.(namespace, name, status_map) do
      :ok ->
        :ok

      {:error, reason} ->
        # Visibility-only: a status-write failure must never crash the sweep.
        Logger.warning("embervm stateful status patch failed for #{namespace}/#{name}: #{inspect(reason)}")
    end
  rescue
    e -> Logger.warning("embervm stateful status patch raised for #{namespace}/#{name}: #{inspect(e)}")
  catch
    _, _ -> :ok
  end

  # -- stats scrape + activity -----------------------------------------------

  # Scrape each stateful-capable node's Envoy stats, keyed by listener
  # stat_prefix (state-<listenPort>), map each prefix back to its workload via
  # the catalog, and: append a stateful_stats op for any cx_total delta (the
  # usage signal, connection-count based) and touch last_active_at when there
  # was cx activity (a positive total delta OR a nonzero cx_active reading,
  # since an open connection is itself activity even if no NEW connection
  # arrived this window). A failed node scrape drops its prior reading (so the
  # next success re-baselines) and is otherwise ignored: idle detection for its
  # workloads is suppressed this tick (fail-open).
  defp scrape_and_record(state, now) do
    nodes = stateful_nodes(state)

    {state, last_stats, current_stats} =
      Enum.reduce(nodes, {state, state.last_stats, %{}}, fn node, {state_acc, last_acc, current_acc} ->
        case scrape_node(state_acc, node) do
          {:ok, reading} ->
            prior = Map.get(state_acc.last_stats, node.configured_id)
            state_acc = record_reading(state_acc, node, prior, reading, now)
            {state_acc, Map.put(last_acc, node.configured_id, reading), Map.put(current_acc, node.configured_id, reading)}

          {:error, reason} ->
            Logger.debug("embervm stateful: stats scrape failed",
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

  # For every listener in this node's reading: charge usage on a positive
  # cx_total delta, touch activity, and maintain the idle_since baseline. No
  # prior reading (first tick or post-failure) means no delta is computable;
  # skip usage/idle accounting entirely for that listener this tick (baseline
  # only, mirroring serving's record_deltas nil-prior clause).
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

  defp record_one_reading(state, node, workload, %{total: prior_total}, active, total, now) do
    delta = total - prior_total

    cond do
      delta < 0 ->
        # Counter reset (Envoy restarted): re-baseline, no usage charge, and
        # reset the idle clock (we cannot tell how much traffic the reset hid,
        # the same posture as serving's negative-delta clause).
        clear_idle_since(state, workload)

      delta > 0 or active > 0 ->
        # Either a new connection arrived (delta > 0, chargeable usage) or a
        # connection is currently open (active > 0, not chargeable by itself
        # but still activity): touch active and clear the idle clock either
        # way. Only charge usage for the actual delta.
        if delta > 0 do
          append_stateful_stats(state, node, workload, delta, now)
        end

        touch_workload_active(state, workload, now)
        clear_idle_since(state, workload)

      true ->
        # active == 0 and delta == 0: confirmed idle this tick against a prior
        # reading. Record the FIRST idle tick's timestamp so idleBankSeconds is
        # measured from when idleness began, not re-armed on every confirming
        # tick.
        mark_idle_since(state, workload, now)
    end
  end

  # Record the FIRST idle tick's timestamp only: a workload already marked
  # idle keeps its original baseline (idleBankSeconds is measured from when
  # idleness BEGAN, not re-armed by a later confirming tick).
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

  # Append a stateful_stats op (connection-count usage, the L4 unit of work).
  # A best-effort append: a usage-op failure must never wedge the sweep.
  defp append_stateful_stats(state, _node, workload, cx_delta, now) do
    op = %Op{
      kind: :stateful_stats,
      tenant: state.tenant,
      principal: usage_principal(workload),
      workload: workload,
      stateful_instance_id: nil,
      ts: now,
      payload: %{workload: workload, cx_delta: cx_delta, window_ms: nil}
    }

    _ = state.op_log_mod.append(state.op_log, op)
    :ok
  rescue
    e ->
      Logger.warning("embervm stateful: stateful_stats append raised", workload: workload, error: inspect(e))
      :ok
  end

  defp touch_workload_active(state, workload, now) do
    StatefulStore.list(state.store, workload)
    |> Enum.filter(&live_instance?/1)
    |> Enum.each(fn instance ->
      _ = StatefulStore.touch_active(state.store, instance.instance_id, now)
    end)
  end

  # -- idle-bank pass ---------------------------------------------------------

  # For every SERVING stateful instance whose workload has been confirmed idle
  # (cx_active == 0, cx_total delta == 0) for at least idleBankSeconds: begin
  # the bank (unpublish, recheck, StopStateful BANK). A workload with no
  # confirmed-idle reading this tick (a scrape failed, first reading, or it saw
  # activity) is untouched: fail-open for warmth.
  defp idle_bank_pass(state, now) do
    StatefulStore.all(state.store)
    |> Enum.filter(&(&1.state == :serving))
    |> Enum.reject(&Map.has_key?(state.banking, &1.instance_id))
    |> Enum.reduce(state, fn instance, acc -> maybe_begin_bank(acc, instance, now) end)
  end

  defp maybe_begin_bank(state, instance, now) do
    with {:ok, cfg} <- stateful_cfg(state, instance.workload),
         since when is_integer(since) <- Map.get(state.idle_since, instance.workload) do
      idle_ms = cfg.idle_bank_seconds * 1000

      cond do
        # A workload backing off from a recent non-terminal bank failure is not
        # re-driven until its next-attempt time, so a persistent daemon-side wedge
        # loops at ~0.03 Hz instead of the sweep frequency.
        bank_backed_off?(state, instance.workload, now) ->
          state

        now - since >= idle_ms ->
          begin_bank(state, instance, now)

        true ->
          state
      end
    else
      _ -> state
    end
  end

  defp bank_backed_off?(state, workload, now) do
    case Map.get(state.bank_backoff, workload) do
      {until, _cur} when is_integer(until) -> now < until
      _ -> false
    end
  end

  # Arm/advance the workload's bank backoff after a non-terminal failure: next
  # attempt at now + backoff, backoff doubling from the base to the cap.
  defp arm_bank_backoff(state, workload, now) do
    cur =
      case Map.get(state.bank_backoff, workload) do
        {_until, prev} when is_integer(prev) -> min(prev * 2, state.bank_backoff_cap_ms)
        _ -> state.bank_backoff_base_ms
      end

    %{state | bank_backoff: Map.put(state.bank_backoff, workload, {now + cur, cur})}
  end

  defp clear_bank_backoff(state, workload) do
    %{state | bank_backoff: Map.delete(state.bank_backoff, workload)}
  end

  # -- drain force-bank (R6, ADR embervm/009) ---------------------------------

  # Force-bank every live stateful instance on a draining node. Marks each
  # workload draining (so recheck skips the raced-in/scrape/cap aborts and
  # decide_resolve forces COMMIT despite a parked connection) then begins its bank,
  # bypassing the idle predicate. Instances already banking are skipped (idempotent
  # under a repeated drain edge). Returns {count_started, state}.
  defp force_bank_node(state, node_id) do
    now = state.clock.()

    instances =
      StatefulStore.all(state.store)
      |> Enum.filter(&(&1.state == :serving and &1.node_id == node_id))
      |> Enum.reject(&Map.has_key?(state.banking, &1.instance_id))

    state =
      Enum.reduce(instances, state, fn instance, acc ->
        acc = %{acc | draining_workloads: MapSet.put(acc.draining_workloads, instance.workload)}
        begin_bank(acc, instance, now)
      end)

    {length(instances), state}
  end

  defp draining_workload?(state, workload), do: MapSet.member?(state.draining_workloads, workload)

  defp clear_draining(state, workload) do
    %{state | draining_workloads: MapSet.delete(state.draining_workloads, workload)}
  end

  # Begin the bank: unpublish (serving -> banking, ETS-only), republish so the
  # activator installs atomically, RECHECK the freshest cx_active reading, and
  # either abort (a connection raced in) or proceed to StopStateful(BANK).
  defp begin_bank(state, instance, _now) do
    Tracer.with_span "embervm.stateful.drain",
                     %{attributes: %{"ember.workload" => instance.workload, "ember.instance_id" => instance.instance_id}} do
      begin_bank_body(state, instance)
    end
  end

  defp begin_bank_body(state, instance) do
    case StatefulStore.unpublish(state.store, instance.instance_id, :bank) do
      {:ok, _} ->
        Embervm.EndpointPublisher.publish(state.publisher)
        recheck_and_bank(state, instance)

      {:error, reason} ->
        Logger.warning("embervm stateful: bank-drain unpublish failed",
          instance_id: instance.instance_id,
          reason: inspect(reason)
        )

        state
    end
  end

  # The decision-7 recheck: RE-SCRAPE (a fresh network read, not the
  # tick-start reading idle_bank_pass acted on) to catch a connection that
  # opened in the gap between the idle decision and this point. A nonzero
  # cx_active means a connection raced in: ABORT (banking -> serving) and
  # republish immediately, never severing a live connection. A scrape failure
  # here fails OPEN toward proceeding (the idle confirmation that got us here
  # already required cx_active == 0 on the last successful scrape, and a
  # stateful workload is a singleton with one owner, so the undetected race
  # window is narrow); the freshly re-scraped reading also updates
  # current_stats so the max-lifetime pass later in this same tick sees it
  # too. Otherwise admit the bank (per-node cap) and spawn the worker.
  defp recheck_and_bank(state, instance) do
    {state, fresh?} = refresh_current_stats(state, instance.node_id)

    cond do
      # DRAIN (R6): bank unconditionally. The node is being preempted, so a raced-in
      # connection, a failed re-scrape, and the per-node bank cap all defer to
      # evacuating state. The parked caller re-wakes against the new noded, and
      # decide_resolve forces COMMIT for this workload.
      draining_workload?(state, instance.workload) ->
        admit_bank(state, instance)

      # A FRESH reading shows a connection raced in: abort for everyone.
      connection_raced_in?(state, instance) ->
        abort_bank(state, instance, :recheck_active)

      # Fail CLOSED for an interruptible workload whose re-scrape did NOT produce a
      # fresh reading (ADR embervm/008): a checkpoint PAUSES a live VM, so it must
      # not proceed on an unconfirmed scrape. Crucially this is gated on `fresh?`,
      # NOT on whether a (possibly STALE) reading is present in current_stats: a
      # failed re-scrape leaves the last-known reading behind, and pausing on stale
      # evidence is exactly the risk the two-phase design refuses.
      interruptible?(state, instance.workload) and not fresh? ->
        abort_bank(state, instance, :recheck_scrape_failed)

      bank_at_cap?(state, instance.node_id) ->
        # Deferred: abort back to serving and let the next tick's idle
        # confirmation re-drive the bank once a slot is free. This costs one
        # extra idle-confirmation cycle under sustained cap pressure, the same
        # trade serving's `admit_bank` defers via a short timer, adapted to
        # stateful's synchronous-within-a-tick shape (there is no drain timer
        # to re-arm here).
        abort_bank(state, instance, :bank_at_cap)

      true ->
        admit_bank(state, instance)
    end
  end

  # Re-scrape the instance's node RIGHT NOW (not the tick-start reading) and fold
  # it into current_stats, so the recheck consults the freshest possible signal.
  # Returns {state, fresh?}: fresh? is true only when THIS re-scrape succeeded. On
  # a scrape failure current_stats is left UNCHANGED (keeps whatever the tick-start
  # scrape produced, or absent if that also failed) and fresh? is false, so an
  # interruptible workload can fail closed on a stale reading rather than pausing
  # a live VM on unconfirmed evidence (see recheck_and_bank).
  defp refresh_current_stats(state, node_id) do
    case scrape_node(state, %{configured_id: node_id}) do
      {:ok, reading} ->
        {%{state | current_stats: Map.put(state.current_stats, node_id, reading)}, true}

      {:error, _reason} ->
        {state, false}
    end
  end

  # True when the instance's node's reading reports cx_active > 0 for its listener,
  # i.e. a connection raced in and the bank must abort. When there is NO reading
  # for the node it FAILS OPEN (returns false): the interruptible fail-closed case
  # is handled separately in recheck_and_bank via the re-scrape freshness flag, so
  # this predicate is purely "does a present reading show an active connection".
  defp connection_raced_in?(state, instance) do
    with {:ok, entry} <- Map.fetch(state.current_stats, instance.node_id),
         {:ok, cfg} <- stateful_cfg(state, instance.workload),
         {:ok, %{active: active}} <- Map.fetch(entry, stat_prefix(cfg.listen_port)) do
      active > 0
    else
      _ -> false
    end
  end

  # Whether the workload is opted in to the two-phase interruptible bank. A
  # missing/malformed cfg reads false (the safe default: the atomic path).
  defp interruptible?(state, workload) do
    case stateful_cfg(state, workload) do
      {:ok, cfg} -> Map.get(cfg, :interruptible_bank, false) == true
      :error -> false
    end
  end

  # `bank_abort` (banking -> serving) lands the instance DIRECTLY back in the
  # live `serving` state (unlike ServingState, where bank_abort only reaches
  # the intermediate `draining` and a separate `publish` edge is needed to
  # reach `published`). StatefulState's `{:banking, :bank_abort} => :serving`
  # has no such intermediate, so calling `publish` here would attempt the
  # ILLEGAL edge `{:serving, :publish}` (not in the transition table).
  # `unpublish/3` cleared ip/port when the bank began, so the row is now
  # `serving` with a nil endpoint; restore the endpoint fields directly via
  # `StatefulStore.adopt_endpoint/5` (an ETS-only fact write with no FSM
  # transition, the same seam adoption uses to rebind a live VM's endpoint
  # without an illegal transition).
  defp abort_bank(state, instance, reason) do
    _ = StatefulStore.mark(state.store, instance.instance_id, :bank_abort)

    _ =
      StatefulStore.adopt_endpoint(state.store, instance.instance_id, instance.node_id, instance.vm_id, %{
        ip: instance.ip,
        port: instance.port,
        healthy: true
      })

    Embervm.EndpointPublisher.publish(state.publisher)

    Logger.info("embervm stateful: bank aborted, republished",
      instance_id: instance.instance_id,
      workload: instance.workload,
      reason: reason
    )

    state
  end

  # Admit the bank: reserve the node slot and spawn the StopStateful(BANK)
  # worker. The durable stateful_banked op lands in finish_bank, AFTER the
  # daemon returns (the crash-consistent order).
  defp admit_bank(state, instance) do
    state = incr_bank_inflight(state, instance.node_id)
    state = %{state | banking: Map.put(state.banking, instance.instance_id, instance.workload)}
    spawn_bank_worker(state, instance)
    state
  end

  # Carries the endpoint (ip/port/vm_id) captured BEFORE unpublish cleared it
  # from the ETS row (StatefulStore.unpublish/3 nils ip/port, unlike
  # ServingStore.unpublish/3 which leaves them intact): a failed bank needs
  # this to restore the still-alive VM's endpoint fact via adopt_endpoint, and
  # StatefulStore.get/2 by then would only return the nil'd-out row.
  #
  # Branches on the workload's interruptible_bank opt-in (ADR embervm/008): the
  # DEFAULT path is the unchanged atomic StopStateful(BANK) worker; an
  # interruptible workload runs the two-phase CHECKPOINT worker instead (settle,
  # then StopStateful(CHECKPOINT), reporting {:checkpoint_done}). Both keep the
  # per-node bank slot until their terminal report releases it.
  defp spawn_bank_worker(state, instance) do
    if interruptible?(state, instance.workload) do
      spawn_checkpoint_worker(state, instance)
    else
      spawn_atomic_bank_worker(state, instance)
    end
  end

  defp spawn_atomic_bank_worker(state, instance) do
    owner = self()
    channel_fun = state.channel_fun
    invalidate_fun = state.invalidate_fun
    stop_fun = state.stop_stateful_fun
    instance_id = instance.instance_id
    workload = instance.workload
    node_id = instance.node_id
    vm_id = instance.vm_id
    # Dial the OWNING noded instance (the one whose capacity fact reports this
    # live vm_id), not the node-name alias (which co-location made point at an
    # arbitrary sibling brick). Falls back to node_id for a legacy/single-instance
    # fact, preserving single-instance behaviour exactly. Resolved here on the
    # owner process so the worker reads a settled key.
    dial_key = dial_for_vm(state, node_id, vm_id)
    # Whether the dial resolved to a SPECIFIC owning instance (a live owner fact
    # reported the vm_id) versus falling open to the bare node name. Task #12: an
    # unknown-vm FAILED_PRECONDITION only terminalizes the instance when we dialled
    # the owner; a fail-open dial to the node alias could be hitting the WRONG sibling
    # (a stale-fact race), so terminalizing there could fail a still-live instance and
    # split-brain a second VM on the same volume. See finish_bank_active.
    owner_resolved = dial_key != node_id
    ip = instance.ip
    port = instance.port

    spawn(fn ->
      outcome =
        Tracer.with_span "embervm.stateful.bank",
                         %{
                           attributes: %{
                             "ember.workload" => workload,
                             "ember.instance_id" => instance_id,
                             "ember.node_id" => node_id
                           }
                         } do
          try do
            case over_channel(channel_fun, invalidate_fun, dial_key, &stop_fun.(&1, bank_request(vm_id))) do
              {:ok, %StopStatefulResponse{snapshot_ref: ref, size_bytes: size, generation: generation}}
              when is_binary(ref) and ref != "" ->
                # generation only becomes known once the daemon replies (the pair-key
                # baseline the bundle is stamped with), so it is set here rather than
                # in the span's opening attributes, exactly like ember.snapshot_bytes.
                Tracer.set_attributes(%{"ember.snapshot_bytes" => size, "ember.generation" => generation || 0})
                {:ok, ref, size, generation}

              other ->
                {:error, other}
            end
          rescue
            e -> {:error, {:bank_raised, e}}
          catch
            kind, reason -> {:error, {:bank_raised, {kind, reason}}}
          end
        end

      send(owner, {:bank_done, instance_id, node_id, vm_id, ip, port, workload, owner_resolved, outcome})
    end)
  end

  defp bank_request(vm_id) do
    %StopStatefulRequest{trace: %Trace{}, vm_id: vm_id, mode: :STOP_STATEFUL_MODE_BANK}
  end

  # -- interruptible bank: CHECKPOINT worker (ADR embervm/008) -----------------

  # The two-phase bank's first phase, off the GenServer: wait out the
  # propagation-settle bound (so a connection the fan-out was still routing at
  # unpublish has drained to the activator), then StopStateful(CHECKPOINT) to
  # PAUSE the VM and get back a checkpoint_token. Reports {:checkpoint_done, ...,
  # outcome}; the serialized process marks :checkpoint_ready and forks
  # commit/abort. Never touches ETS or the store from here (that stays on the
  # owner process). settle_ms 0 (tests) makes this run instantly.
  defp spawn_checkpoint_worker(state, instance) do
    owner = self()
    channel_fun = state.channel_fun
    invalidate_fun = state.invalidate_fun
    stop_fun = state.stop_stateful_fun
    settle_ms = state.propagation_settle_ms
    instance_id = instance.instance_id
    workload = instance.workload
    node_id = instance.node_id
    vm_id = instance.vm_id
    # Same owning-instance dial resolution as the atomic bank worker.
    dial_key = dial_for_vm(state, node_id, vm_id)
    ip = instance.ip
    port = instance.port

    spawn(fn ->
      outcome =
        Tracer.with_span "embervm.stateful.checkpoint",
                         %{
                           attributes: %{
                             "ember.workload" => workload,
                             "ember.instance_id" => instance_id,
                             "ember.node_id" => node_id
                           }
                         } do
          if is_integer(settle_ms) and settle_ms > 0, do: Process.sleep(settle_ms)

          try do
            case over_channel(channel_fun, invalidate_fun, dial_key, &stop_fun.(&1, checkpoint_request(vm_id))) do
              {:ok, %StopStatefulResponse{checkpoint_token: token, generation: generation}}
              when is_binary(token) and token != "" ->
                {:ok, token, generation}

              other ->
                {:error, other}
            end
          rescue
            e -> {:error, {:checkpoint_raised, e}}
          catch
            kind, reason -> {:error, {:checkpoint_raised, {kind, reason}}}
          end
        end

      send(owner, {:checkpoint_done, instance_id, node_id, vm_id, ip, port, workload, outcome})
    end)
  end

  defp checkpoint_request(vm_id) do
    %StopStatefulRequest{trace: %Trace{}, vm_id: vm_id, mode: :STOP_STATEFUL_MODE_CHECKPOINT}
  end

  # The StopStateful(BANK) completed: release the node slot + banking
  # bookkeeping, then:
  #   * on success: `banking -[bank_ready]-> banked` with stateful_banked
  #     (snapshot fact + generation, the pair-key baseline);
  #   * on failure: `banking -[bank_abort]-> serving` (ETS-only, VM intact) and
  #     republish (a failed bank must not leave a warm VM dark).
  defp finish_bank(state, instance_id, node_id, vm_id, ip, port, workload, owner_resolved, outcome) do
    state =
      state
      |> decr_bank_inflight(node_id)
      |> Map.update!(:banking, &Map.delete(&1, instance_id))

    case StatefulStore.get(state.store, instance_id) do
      # Terminal mid-bank (a forced destroy raced it): never resurrect.
      {:ok, %{state: st}} when st in [:evicted, :destroyed, :failed] ->
        state

      {:ok, instance} ->
        finish_bank_active(state, instance, node_id, vm_id, ip, port, workload, owner_resolved, outcome)

      :error ->
        state
    end
  end

  defp finish_bank_active(state, instance, node_id, _vm_id, _ip, _port, workload, _owner_resolved, {:ok, ref, size, generation}) do
    _ =
      StatefulStore.transition(
        state.store,
        instance.instance_id,
        :bank_ready,
        :stateful_banked,
        %{snapshot_ref: ref, size_bytes: size, generation: generation},
        %{snapshot_ref: ref, snapshot_size_bytes: size, snapshot_generation: generation, node_id: node_id, vm_id: nil}
      )

    Embervm.EndpointPublisher.publish(state.publisher)

    Logger.info("embervm stateful banked",
      instance_id: instance.instance_id,
      workload: workload,
      node_id: node_id,
      snapshot_bytes: size
    )

    # A clean bank clears any backoff armed by a prior failure for this workload.
    clear_bank_backoff(state, workload)
  end

  # UNKNOWN-VM FAILED_PRECONDITION (gRPC status 9): the daemon we dialled does not
  # own this VM ("not bankable / unknown vm").
  #
  # Terminalize (banking -> failed, durable stateful_failed, so the wake path
  # cold-boots a fresh one) ONLY when the dial was OWNER-RESOLVED: we reached the
  # exact instance whose capacity fact reported this vm_id, so its "unknown vm" reply
  # is authoritative that the VM is genuinely gone. No backoff needed: the instance is
  # terminal, so idle_bank_pass never re-selects it.
  #
  # When the dial FELL OPEN to the bare node name (no owner fact reported the vm_id,
  # owner_resolved false), an unknown-vm reply is NOT authoritative: under co-location
  # the node-name alias can resolve to the wrong sibling brick (a stale-fact race
  # where the owner's fact has not landed yet), so terminalizing here could fail a
  # still-live instance and cold-boot a SECOND VM on the same volume (split-brain).
  # Treat it as a NON-terminal error instead: abort back to the fan-out and arm the
  # backoff, so the next sweep re-resolves the dial once the owner fact settles.
  defp finish_bank_active(state, instance, node_id, vm_id, ip, port, workload, owner_resolved, {:error, reason}) do
    if failed_precondition?(reason) and owner_resolved do
      Logger.warning("embervm stateful: bank rejected unknown vm on owner-resolved dial, failing instance",
        instance_id: instance.instance_id,
        workload: workload,
        reason: inspect(reason)
      )

      _ =
        StatefulStore.transition(
          state.store,
          instance.instance_id,
          :fail,
          :stateful_failed,
          %{reason: "bank_unknown_vm"},
          %{}
        )

      state
    else
      other_error_bank_recovery(state, instance, node_id, vm_id, ip, port, workload, reason)
    end
  end

  # Any OTHER bank error (transient transport/daemon failure, the VM still live):
  # abort back to the fan-out and republish the still-alive VM, but ARM the
  # per-workload backoff so a persistent failure re-drives at ~0.03 Hz, not the
  # sweep frequency.
  #
  # bank_abort (banking -> serving) lands directly in the live state; no further
  # FSM transition applies (see abort_bank/3's comment on why a `publish` call here
  # would be the illegal {:serving, :publish} edge). Restore the endpoint fact via
  # adopt_endpoint (ETS-only, no transition) using the endpoint captured BEFORE
  # unpublish cleared the ETS row (the freshly-refetched `instance` here has nil
  # ip/port, per StatefulStore.unpublish/3's clearing).
  defp other_error_bank_recovery(state, instance, node_id, vm_id, ip, port, workload, reason) do
    Logger.warning("embervm stateful: bank failed, returning to fan-out (backing off)",
      instance_id: instance.instance_id,
      workload: workload,
      reason: inspect(reason)
    )

    _ = StatefulStore.mark(state.store, instance.instance_id, :bank_abort)

    if republishable_endpoint?(ip, port) do
      _ =
        StatefulStore.adopt_endpoint(state.store, instance.instance_id, node_id, vm_id, %{
          ip: ip,
          port: port,
          healthy: true
        })

      Embervm.EndpointPublisher.publish(state.publisher)
    end

    arm_bank_backoff(state, workload, state.clock.())
  end

  # -- interruptible bank: finish CHECKPOINT + resolve (ADR embervm/008) -------

  # The CHECKPOINT completed. On success: mark :checkpoint_ready (banking ->
  # checkpointed, ETS-only, stamping checkpoint_token + vm_id so adoption/resolve
  # can read them), decide COMMIT vs ABORT, and spawn the resolve worker (the
  # per-node bank slot + banking entry stay held until finish_resolve). On a
  # CHECKPOINT RPC failure: noded left the VM live and nothing entered
  # checkpointed, so this is exactly the atomic bank-failure recovery (bank_abort
  # back to serving, republish, release the slot).
  defp finish_checkpoint(state, instance_id, node_id, vm_id, ip, port, workload, {:ok, token, generation}) do
    case StatefulStore.get(state.store, instance_id) do
      {:ok, %{state: :banking}} ->
        _ =
          StatefulStore.mark_with(state.store, instance_id, :checkpoint_ready, %{
            checkpoint_token: token,
            checkpoint_generation: generation,
            vm_id: vm_id
          })

        {mode, state} = decide_resolve(state, workload)
        {mode, blessed_generation, state} = plan_resolve_blessing(state, workload, mode)
        spawn_resolve_worker(state, instance_id, node_id, vm_id, ip, port, workload, token, mode, blessed_generation)
        state

      # Terminal mid-checkpoint (a forced destroy raced it) or gone: release the
      # slot and do not resurrect. The paused VM is noded's to reap (it
      # auto-aborts on its own timeout).
      _ ->
        release_bank_slot(state, node_id, instance_id)
    end
  end

  defp finish_checkpoint(state, instance_id, node_id, vm_id, ip, port, workload, {:error, reason}) do
    Logger.warning("embervm stateful: checkpoint failed, returning to fan-out",
      instance_id: instance_id,
      workload: workload,
      reason: inspect(reason)
    )

    state = release_bank_slot(state, node_id, instance_id)

    # Nothing entered :checkpointed; the VM is still live in :banking. Recover
    # exactly like a failed atomic bank: bank_abort back to serving + restore the
    # endpoint (unpublish nil'd ip/port when the bank began).
    case StatefulStore.get(state.store, instance_id) do
      {:ok, %{state: :banking}} ->
        _ = StatefulStore.mark(state.store, instance_id, :bank_abort)

        if republishable_endpoint?(ip, port) do
          _ =
            StatefulStore.adopt_endpoint(state.store, instance_id, node_id, vm_id, %{
              ip: ip,
              port: port,
              healthy: true
            })

          Embervm.EndpointPublisher.publish(state.publisher)
        end

        state

      _ ->
        state
    end
  end

  # COMMIT vs ABORT for a just-checkpointed workload. The flap guard fires FIRST:
  # once consecutive aborts reach the threshold, force a commit (regardless of a
  # parked connection) and reset the counter, so a hot-looping client cannot pin
  # the VM live forever. Otherwise: a parked connection means a client wants it
  # hot NOW, so ABORT (resume); no parked connection means COMMIT (bank it). The
  # returned state carries any counter change.
  defp decide_resolve(state, workload) do
    aborts = Map.get(state.checkpoint_aborts, workload, 0)

    cond do
      # DRAIN (R6): force COMMIT even against a parked connection. The node is being
      # preempted; durability is the hard guarantee, the parked caller re-wakes
      # against the new noded (spot semantics). The flap guard is irrelevant here.
      draining_workload?(state, workload) ->
        {:commit, state}

      aborts >= state.flap_abort_threshold ->
        Tracer.with_span "embervm.stateful.checkpoint_flap_guard",
                         %{attributes: %{"ember.workload" => workload, "ember.checkpoint_aborts" => aborts}} do
          :ok
        end

        Logger.info("embervm stateful: checkpoint flap guard fired, forcing commit",
          workload: workload,
          consecutive_aborts: aborts
        )

        {:commit, reset_checkpoint_aborts(state, workload)}

      parked?(state, workload) ->
        {:abort, state}

      true ->
        {:commit, state}
    end
  end

  defp parked?(state, workload) do
    state.parked_fun.(workload)
  rescue
    _ -> false
  catch
    _, _ -> false
  end

  # An ABORT resumes the paused guest, which may write, so the volume generation
  # must advance. Under R7 (ADR embervm/011, standing decision 4) the control plane
  # is the sole issuer: bless the next generation and thread it into the
  # ResolveStateful request so noded RecordBlessed's it (genFile == blessedFile,
  # reported generation_blessed:true) rather than self-bumping into a false
  # quarantine that fails every subsequent wake closed. The bless op-log append is
  # the fence, op-log-before-dispatch, mirroring Embervm.StatefulManager.plan_wake:
  # if it fails we force COMMIT rather than dispatch an unblessed abort, since a
  # commit invents no generation and is always ledger-safe (the parked caller
  # relights, slightly colder, off the fresh bundle). COMMIT needs no blessing (it
  # publishes the already-blessed boot generation).
  defp plan_resolve_blessing(state, workload, :abort) do
    next = StatefulStore.next_blessed_generation(state.store, workload)

    case StatefulStore.bless_generation(state.store, workload, next) do
      {:ok, _fact} ->
        {:abort, next, state}

      {:error, reason} ->
        Logger.warning("embervm stateful: bless for checkpoint abort failed, forcing commit",
          workload: workload,
          reason: inspect(reason)
        )

        {:commit, 0, state}
    end
  end

  defp plan_resolve_blessing(state, _workload, :commit), do: {:commit, 0, state}

  # The resolve worker: ResolveStateful(vm_id, token, mode) off the GenServer (it
  # can take seconds: a commit publishes the bundle, an abort resumes the VM).
  # Reports {:resolve_done, ..., mode, outcome}; finish_resolve completes the
  # durable transition on the owner process.
  defp spawn_resolve_worker(state, instance_id, node_id, vm_id, ip, port, workload, token, mode, blessed_generation) do
    owner = self()
    channel_fun = state.channel_fun
    invalidate_fun = state.invalidate_fun
    resolve_fun = state.resolve_stateful_fun
    # The paused VM is still on its owning instance: dial that, not the alias.
    dial_key = dial_for_vm(state, node_id, vm_id)

    spawn(fn ->
      outcome =
        Tracer.with_span "embervm.stateful.resolve",
                         %{
                           attributes: %{
                             "ember.workload" => workload,
                             "ember.instance_id" => instance_id,
                             "ember.resolve_mode" => Atom.to_string(mode)
                           }
                         } do
          try do
            case over_channel(channel_fun, invalidate_fun, dial_key, &resolve_fun.(&1, resolve_request(vm_id, token, mode, blessed_generation))) do
              {:ok, %ResolveStatefulResponse{} = resp} ->
                {:ok, resp}

              other ->
                {:error, other}
            end
          rescue
            e -> {:error, {:resolve_raised, e}}
          catch
            kind, reason -> {:error, {:resolve_raised, {kind, reason}}}
          end
        end

      send(owner, {:resolve_done, instance_id, node_id, vm_id, ip, port, workload, mode, outcome})
    end)
  end

  defp resolve_request(vm_id, token, mode, blessed_generation) do
    %ResolveStatefulRequest{
      trace: %Trace{},
      vm_id: vm_id,
      checkpoint_token: token,
      mode: resolve_mode(:commit == mode),
      # CP-issued generation for an ABORT resume (0 for COMMIT, which publishes the
      # already-blessed boot generation). noded RecordBlessed's it so the resumed
      # volume stays blessed and is never falsely quarantined.
      blessed_generation: blessed_generation
    }
  end

  defp resolve_mode(true), do: :RESOLVE_MODE_COMMIT
  defp resolve_mode(false), do: :RESOLVE_MODE_ABORT

  # The resolve RPC completed: release the slot + banking entry, then apply the
  # outcome. A terminal instance mid-resolve (forced destroy raced) is left alone.
  defp finish_resolve(state, instance_id, node_id, vm_id, ip, port, workload, mode, outcome) do
    state = release_bank_slot(state, node_id, instance_id)

    case StatefulStore.get(state.store, instance_id) do
      {:ok, %{state: st}} when st in [:evicted, :destroyed, :failed] ->
        state

      {:ok, _instance} ->
        apply_resolve(state, instance_id, node_id, vm_id, ip, port, workload, mode, outcome)

      :error ->
        state
    end
  end

  # COMMIT success: the temp snapshot is now the workload's bundle. Transition
  # :commit (checkpointed -> banked) durably with a stateful_banked op carrying
  # the bundle fact, exactly the atomic bank's success payload shape. Reset the
  # abort counter (settled to banked) and notify the manager so parked callers
  # relight off the fresh bundle.
  defp apply_resolve(state, instance_id, node_id, _vm_id, _ip, _port, workload, :commit, {:ok, %ResolveStatefulResponse{snapshot_ref: ref, generation: generation, size_bytes: size}}) do
    _ =
      StatefulStore.transition(
        state.store,
        instance_id,
        :commit,
        :stateful_banked,
        %{snapshot_ref: ref, size_bytes: size, generation: generation},
        %{snapshot_ref: ref, snapshot_size_bytes: size, snapshot_generation: generation, node_id: node_id, vm_id: nil}
      )

    state = reset_checkpoint_aborts(state, workload)
    notify_checkpoint_resolved(state, workload, :commit)

    Logger.info("embervm stateful checkpoint committed",
      instance_id: instance_id,
      workload: workload,
      node_id: node_id,
      snapshot_bytes: size
    )

    state
  end

  # ABORT success: noded resumed the SAME paused VM hot. Transition :abort
  # (checkpointed -> serving, ETS-only mark: the durable projection was already
  # serving-or-published, so no new op is owed) + restore the endpoint and
  # republish. Increment the consecutive-abort counter (flap guard input) and
  # notify the manager so parked callers get the hot endpoint.
  defp apply_resolve(state, instance_id, node_id, vm_id, ip, port, workload, :abort, {:ok, %ResolveStatefulResponse{}}) do
    _ = StatefulStore.mark(state.store, instance_id, :abort)

    if republishable_endpoint?(ip, port) do
      _ =
        StatefulStore.adopt_endpoint(state.store, instance_id, node_id, vm_id, %{
          ip: ip,
          port: port,
          healthy: true
        })
    end

    Embervm.EndpointPublisher.publish(state.publisher)

    state = incr_checkpoint_aborts(state, workload)

    Tracer.with_span "embervm.stateful.checkpoint_abort",
                     %{attributes: %{"ember.workload" => workload, "ember.reason" => "parked_connection"}} do
      :ok
    end

    notify_checkpoint_resolved(state, workload, :abort)

    Logger.info("embervm stateful checkpoint aborted, resumed hot",
      instance_id: instance_id,
      workload: workload,
      node_id: node_id
    )

    state
  end

  # Resolve rejected FAILED_PRECONDITION (gRPC status 9): noded already resolved
  # this checkpoint ITSELF. Its resolve-timeout auto-abort fired first (we were
  # slow, not dead) and lost us the single-resolve race. A noded auto-abort always
  # RESUMES the VM hot (it never tears it down on the timeout path), so the VM is
  # LIVE and serving on the node, NOT gone. Reconcile the instance to :serving
  # exactly as a successful ABORT would (mark serving, republish, serve parked
  # callers) rather than marking it :failed and orphaning a healthy live VM that
  # adoption could never heal (adoption skips terminal instances). If noded had in
  # fact torn the VM down (a resume failure, which noded reports as a DIFFERENT
  # error, not FAILED_PRECONDITION), we would not land here.
  defp apply_resolve(state, instance_id, node_id, vm_id, ip, port, workload, mode, {:error, reason}) do
    if failed_precondition?(reason) do
      # noded already resolved this checkpoint ITSELF: its resolve-timeout
      # auto-abort fired first (we were slow, not dead) and lost us the
      # single-resolve race. A noded auto-abort always RESUMES the VM hot (it never
      # tears it down on the timeout path), so the VM is LIVE and serving on the
      # node, NOT gone. Reconcile to :serving exactly as a successful ABORT would
      # (mark serving, republish, serve parked callers) rather than marking it
      # :failed and orphaning a healthy live VM adoption could never heal.
      Logger.info(
        "embervm stateful: resolve lost the single-resolve race (noded auto-aborted, VM resumed); reconciling to serving",
        instance_id: instance_id,
        workload: workload
      )

      apply_resolve(state, instance_id, node_id, vm_id, ip, port, workload, :abort, {:ok, %ResolveStatefulResponse{}})
    else
      # Any OTHER resolve error (transport/timeout/raised, or a noded resume
      # failure that tore the VM down): the paused VM is gone, so the instance
      # FAILS (checkpointed -> failed, durable stateful_failed). The next
      # connection cold-boots. Reset the abort counter (this lifecycle is over).
      Logger.warning("embervm stateful: checkpoint resolve failed, failing instance",
        instance_id: instance_id,
        workload: workload,
        mode: mode,
        reason: inspect(reason)
      )

      _ =
        StatefulStore.transition(
          state.store,
          instance_id,
          :fail,
          :stateful_failed,
          %{reason: "resolve_failed"},
          %{}
        )

      reset_checkpoint_aborts(state, workload)
    end
  end

  # Whether a resolve error is a gRPC FAILED_PRECONDITION (status 9), tolerating
  # the resolve worker's `with/else -> {:error, other}` re-wrapping (the raw error
  # arrives nested one or more `{:error, ...}` deep).
  defp failed_precondition?(%GRPC.RPCError{status: 9}), do: true
  defp failed_precondition?({:error, inner}), do: failed_precondition?(inner)
  defp failed_precondition?(_), do: false

  defp release_bank_slot(state, node_id, instance_id) do
    state
    |> decr_bank_inflight(node_id)
    |> Map.update!(:banking, &Map.delete(&1, instance_id))
  end

  defp notify_checkpoint_resolved(state, workload, outcome) do
    GenServer.cast(state.manager, {:checkpoint_resolved, workload, outcome})
    :ok
  rescue
    _ -> :ok
  catch
    _, _ -> :ok
  end

  defp incr_checkpoint_aborts(state, workload) do
    %{state | checkpoint_aborts: Map.update(state.checkpoint_aborts, workload, 1, &(&1 + 1))}
  end

  defp reset_checkpoint_aborts(state, workload) do
    %{state | checkpoint_aborts: Map.put(state.checkpoint_aborts, workload, 0)}
  end

  # -- max-lifetime expiry -----------------------------------------------------

  # An instance older than maxLifetimeSeconds is destroyed. A banked one is
  # evicted immediately (it holds no VM to drain). A live one waits for
  # downstream_cx_active == 0 up to the capped patience window (decision 8),
  # tracked from the first tick it was BOTH over-lifetime and active; once the
  # window elapses, destroy anyway (WAL recovery covers the interruption).
  defp sweep_lifetime(state, now) do
    StatefulStore.all(state.store)
    |> Enum.reject(&StatefulState.terminal?(&1.state))
    # An instance with a bank RPC in flight this tick (see idle_bank_pass) is
    # left alone: destroying it here would race the spawned worker's
    # {:bank_done} report. finish_bank's terminal-state guard would no-op
    # correctly even without this filter (mirroring serving's exact
    # mid-bank-destroy safety), but skipping it here avoids an unnecessary
    # StopStateful(DESTROY) racing a StopStateful(BANK) against the same VM.
    |> Enum.reject(&Map.has_key?(state.banking, &1.instance_id))
    |> Enum.filter(&over_lifetime?(state, &1, now))
    |> Enum.reduce(state, fn instance, acc -> expire_instance(acc, instance, now) end)
  end

  defp over_lifetime?(state, instance, now) do
    case stateful_cfg(state, instance.workload) do
      {:ok, cfg} ->
        max_ms = cfg.max_lifetime_seconds * 1000
        is_integer(instance.created_at) and now - instance.created_at >= max_ms

      :error ->
        false
    end
  end

  defp expire_instance(state, %{state: :banked} = instance, _now) do
    state = clear_lifetime_drain_since(state, instance.workload)
    evict_banked(state, instance, "lifetime")
  end

  defp expire_instance(state, instance, now) do
    if instance_active?(state, instance) do
      lifetime_drain_wait(state, instance, now)
    else
      state = clear_lifetime_drain_since(state, instance.workload)
      destroy_over_lifetime(state, instance)
    end
  end

  # An active live instance over its lifetime: track the drain-since baseline
  # and destroy only once the patience window elapses.
  defp lifetime_drain_wait(state, instance, now) do
    since = Map.get(state.lifetime_drain_since, instance.workload, now)
    state = %{state | lifetime_drain_since: Map.put(state.lifetime_drain_since, instance.workload, since)}

    if now - since >= state.lifetime_drain_max_ms do
      destroy_over_lifetime(clear_lifetime_drain_since(state, instance.workload), instance)
    else
      state
    end
  end

  defp clear_lifetime_drain_since(state, workload) do
    %{state | lifetime_drain_since: Map.delete(state.lifetime_drain_since, workload)}
  end

  # Whether the instance's L4 listener currently reports an open connection,
  # from THIS tick's freshest reading (falls back to "not active" when there
  # is no fresh reading for the node, which only delays a destroy that will
  # still happen once the patience window elapses regardless).
  defp instance_active?(state, instance) do
    with {:ok, entry} <- Map.fetch(state.current_stats, instance.node_id),
         {:ok, cfg} <- stateful_cfg(state, instance.workload),
         {:ok, %{active: active}} <- Map.fetch(entry, stat_prefix(cfg.listen_port)) do
      active > 0
    else
      _ -> false
    end
  end

  # Destroy a live over-lifetime instance: unpublish first (if published, i.e.
  # `serving`; a `starting`/`banking` instance was never in the fan-out) so the
  # activator swap lands, then StopStateful(DESTROY) + the durable
  # stateful_destroyed.
  defp destroy_over_lifetime(state, instance) do
    if instance.state == :serving do
      _ = StatefulStore.unpublish(state.store, instance.instance_id, :destroyed)
      Embervm.EndpointPublisher.publish(state.publisher)
    end

    destroy_instance(state, instance, "lifetime")
  end

  # -- banked-TTL GC -----------------------------------------------------------

  # A banked bundle untouched (last_active_at, else updated_at) for longer than
  # bankedTtlSeconds is evicted: EvictSnapshot on the node + the durable
  # stateful_evicted (reason "ttl"). The volume is never touched.
  defp sweep_banked_ttl(state, now) do
    StatefulStore.all(state.store)
    |> Enum.filter(&(&1.state == :banked))
    |> Enum.reduce(state, fn instance, acc ->
      case stateful_cfg(acc, instance.workload) do
        {:ok, cfg} ->
          ttl_ms = cfg.banked_ttl_seconds * 1000
          last = instance.last_active_at || instance.updated_at || 0

          if now - last >= ttl_ms do
            evict_banked(acc, instance, "ttl")
          else
            acc
          end

        :error ->
          acc
      end
    end)
  end

  # -- eager broken-pair eviction ----------------------------------------------

  # Runs the store's sweep primitive every tick, so a bundle whose pair broke
  # (the volume's generation moved out from under it, a superseded-generation
  # eviction) is evicted before the next wake discovers it stale. R6, Task 9: the
  # store copy of each just-evicted bundle follows the local eviction on the same
  # trigger. The primitive returns the evicted instance_ids; we read each one's
  # last-known node/ref (it is now terminal but still in the store) to target the
  # remote evict. A bundle whose row no longer resolves is skipped (nothing to
  # target). The VOLUME is never evicted here: a broken pair means the volume moved
  # ON to a newer generation, which is exactly a generation a live/future bundle
  # will pair with, so the store must keep it (generation guard, standing decision 8).
  defp eager_evict_broken_pairs(state) do
    evicted = StatefulStore.eager_evict_broken_pairs(state.store)

    for instance_id <- evicted do
      case StatefulStore.get(state.store, instance_id) do
        {:ok, instance} -> _ = evict_remote_bundle(state, instance)
        _ -> :ok
      end
    end

    state
  end

  # -- destroy / evict RPCs + durable ops --------------------------------------

  defp destroy_instance(state, instance, reason) do
    _ = stop_stateful_destroy(state, instance)

    _ =
      StatefulStore.transition(
        state.store,
        instance.instance_id,
        :destroy,
        :stateful_destroyed,
        %{reason: reason},
        %{}
      )

    Logger.info("embervm stateful destroyed", instance_id: instance.instance_id, workload: instance.workload, reason: reason)

    state
  end

  defp evict_banked(state, instance, reason) do
    _ = evict_snapshot(state, instance)
    # R6, Task 9: the store copy of the bundle follows the local eviction on the
    # same trigger (banked TTL, superseded generation). Best-effort and idempotent
    # on the daemon side; a store copy that was never made is an already-absent
    # no-op. The VOLUME is never touched here (bundle eviction never strands a
    # volume generation), so no pairing guard applies to this call.
    _ = evict_remote_bundle(state, instance)

    _ =
      StatefulStore.transition(
        state.store,
        instance.instance_id,
        :evict,
        :stateful_evicted,
        %{reason: reason},
        %{}
      )

    Logger.info("embervm stateful evicted", instance_id: instance.instance_id, workload: instance.workload, reason: reason)

    state
  end

  defp stop_stateful_destroy(state, %{node_id: node_id, vm_id: vm_id}) when is_binary(node_id) and is_binary(vm_id) do
    req = %StopStatefulRequest{trace: %Trace{}, vm_id: vm_id, mode: :STOP_STATEFUL_MODE_DESTROY}
    dial_key = dial_for_vm(state, node_id, vm_id)

    with {:ok, channel} <- safe_channel(state.channel_fun, dial_key) do
      try do
        state.stop_stateful_fun.(channel, req)
      rescue
        _ -> :error
      catch
        _, _ -> :error
      end
    end

    :ok
  end

  defp stop_stateful_destroy(_state, _instance), do: :ok

  defp evict_snapshot(state, %{node_id: node_id, snapshot_ref: ref}) when is_binary(node_id) and is_binary(ref) do
    req = %EvictSnapshotRequest{trace: %Trace{}, snapshot_ref: ref}
    dial_key = dial_for_bundle(state, node_id, ref)

    with {:ok, channel} <- safe_channel(state.channel_fun, dial_key) do
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

  defp evict_snapshot(_state, _instance), do: :ok

  # R6, Task 9: drop the store copy of a banked STATEFUL bundle (EvictArtifact,
  # remote=true, kind STATEFUL) alongside the local EvictSnapshot. Best-effort: a
  # failure never wedges the sweep (the local eviction and durable transition are
  # authoritative; a stranded store copy is swept later by the remote-orphan
  # reconcile). Idempotent on the daemon; an already-absent store copy is a no-op.
  defp evict_remote_bundle(state, %{node_id: node_id, snapshot_ref: ref, workload: workload})
       when is_binary(node_id) and is_binary(ref) and ref != "" do
    artifact = %ArtifactRef{kind: :ARTIFACT_KIND_STATEFUL, workload: workload, ref: ref}
    req = %EvictArtifactRequest{artifact: artifact, remote: true, trace: %Trace{workload: workload}}
    dial_key = dial_for_bundle(state, node_id, ref)

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

  # -- per-node bank cap --------------------------------------------------------

  defp bank_at_cap?(state, node_id) do
    Map.get(state.bank_inflight, node_id, 0) >= state.bank_concurrency
  end

  defp incr_bank_inflight(state, node_id) do
    %{state | bank_inflight: Map.update(state.bank_inflight, node_id, 1, &(&1 + 1))}
  end

  defp decr_bank_inflight(state, node_id) do
    %{state | bank_inflight: Map.update(state.bank_inflight, node_id, 0, &max(&1 - 1, 0))}
  end

  # -- stats scrape helpers -----------------------------------------------------

  # Stateful workloads run on the same node Envoy as serving (the TCP proxy
  # listeners live alongside the serving clusters), so this reuses the serving
  # capacity predicate: any node reporting a serving_subnet_cidr is scraped.
  defp stateful_nodes(state) do
    NodeCapacity.all(state.capacity_table)
    |> Enum.filter(&stateful_capable?/1)
  end

  defp stateful_capable?(fact) do
    cidr = Map.get(fact, :serving_subnet_cidr)
    is_binary(cidr) and cidr != ""
  end

  defp scrape_node(%{stats_base: nil}, _node), do: {:error, :no_stats_base}

  defp scrape_node(state, _node) do
    state.scrape_fun.(stats_url(state.stats_base))
  end

  defp stats_url(base), do: String.trim_trailing(base, "/") <> "/stats?format=json"

  # -- catalog + instance helpers -----------------------------------------------

  defp stateful_cfg(state, workload) do
    case WorkloadCatalog.fetch(state.catalog_table, workload) do
      {:ok, %{class: "stateful", stateful: cfg}} when is_map(cfg) -> {:ok, cfg}
      _ -> :error
    end
  end

  # The listener stat_prefix for a workload's listen_port (state-<listenPort>,
  # Task 5's stat_prefix wiring), the key the scrape reading is keyed by.
  @stat_prefix "state-"
  defp stat_prefix(listen_port) when is_integer(listen_port), do: @stat_prefix <> Integer.to_string(listen_port)

  # Map a scraped stat_prefix back to its owning workload via the catalog: the
  # inverse of stat_prefix/1, found by matching every stateful-class catalog
  # entry's listen_port. O(workload count) per lookup, called once per (node,
  # listener) pair per tick, an acceptable cost at this cadence and workload
  # count.
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
        {:ok, %{class: "stateful", stateful: %{listen_port: ^port}}} -> name
        _ -> nil
      end
    end)
  end

  # A live instance is any non-terminal, non-banked state (mirrors
  # StatefulState.live_states/0's set, but expressed against the instance
  # struct rather than the bare state atom for call-site brevity).
  defp live_instance?(instance), do: StatefulState.live?(instance.state)

  defp republishable_endpoint?(ip, port) do
    is_binary(ip) and ip != "" and is_integer(port)
  end

  defp usage_principal(workload), do: "system:stateful:#{workload}"

  # -- daemon + stats seams -------------------------------------------------

  defp safe_channel(channel_fun, node_id) do
    channel_fun.(node_id)
  rescue
    e -> {:error, {:channel_raised, e}}
  catch
    kind, reason -> {:error, {:channel_raised, {kind, reason}}}
  end

  # The channel key for a live-VM dial (bank/checkpoint/resolve/destroy): the
  # OWNING noded instance reporting `vm_id`, else the node name (single-instance /
  # legacy fallback, unchanged behaviour). Instance-key unification (PR-B0a): a
  # co-located node's node-name alias resolves to an arbitrary sibling brick, so a
  # bank against the alias misroutes to a brick that never owned the VM and loops.
  defp dial_for_vm(state, node_id, vm_id) do
    Embervm.WakeInstance.dial_for_vm(state.capacity_table, node_id, vm_id)
  end

  # The channel key for a bundle dial (EvictSnapshot/EvictArtifact): the instance
  # holding the bundle on disk (per-instance-on-disk, PR-2.5), else the node name.
  defp dial_for_bundle(state, node_id, snapshot_ref) do
    Embervm.WakeInstance.dial_for_bundle(state.capacity_table, node_id, snapshot_ref)
  end

  # Acquire the node's shared channel and run a stateful verb over it, invalidating
  # that channel (so the next safe_channel/2 re-dials) when the call reveals the
  # transport is dead: a transport_dead? error return, or an :exit from the Mint
  # ConnectionProcess dying (its crash on an in-flight stream RST during a noded
  # rollout surfaces here as an exit). A server-returned gRPC status rode a HEALTHY
  # channel and never invalidates it (Embervm.NodeChannel.transport_dead?/1, D-R2.7.2).
  # Returns the verb's raw result ({:ok, _} | {:error, _}), {:error, {:exit, reason}}
  # on an exit, or the safe_channel error if no channel could be obtained.
  defp over_channel(channel_fun, invalidate_fun, node_id, call_fun) do
    with {:ok, channel} <- safe_channel(channel_fun, node_id) do
      try do
        result = call_fun.(channel)
        if Embervm.NodeChannel.transport_dead?(result), do: invalidate_fun.(node_id, channel)
        result
      catch
        :exit, reason ->
          invalidate_fun.(node_id, channel)
          {:error, {:exit, reason}}
      end
    end
  end

  defp default_stop_stateful(channel, req) do
    Embervm.Node.V1.NodeService.Stub.stop_stateful(channel, req)
  end

  defp default_evict_snapshot(channel, req) do
    Embervm.Node.V1.NodeService.Stub.evict_snapshot(channel, req)
  end

  defp default_evict_artifact(channel, req) do
    Embervm.Node.V1.NodeService.Stub.evict_artifact(channel, req)
  end

  defp default_resolve_stateful(channel, req) do
    Embervm.Node.V1.NodeService.Stub.resolve_stateful(channel, req)
  end

  # Production scrape: GET /stats?format=json over the shared Finch pool, parse
  # the per-listener tcp.<prefix>.downstream_cx_{active,total} counters into
  # %{stat_prefix => %{active: n, total: n}}. A non-200, a transport error, or
  # an unparseable body is a scrape failure (fail-open). Envoy's ?format=json
  # emits {"stats":[{"name":..,"value":..}, ...]}; keep only the
  # tcp.<prefix>.downstream_cx_{active,total} gauges/counters.
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
  Parse an Envoy `/stats?format=json` body into `%{stat_prefix => %{active: n,
  total: n}}`, keeping only `tcp.<prefix>.downstream_cx_{active,total}`
  counters. `@doc false` only so the parse path is exercised directly on a
  realistic raw body in tests; production reaches it via `default_scrape`. Not
  part of the module's public API.
  """
  @doc false
  @spec parse_stats(binary()) :: {:ok, %{optional(String.t()) => %{active: integer(), total: integer()}}} | {:error, term()}
  def parse_stats(body) do
    case Jason.decode(body) do
      {:ok, %{"stats" => stats}} when is_list(stats) ->
        reading =
          stats
          |> Enum.reduce(%{}, fn stat, acc -> fold_tcp_stat(stat, acc) end)

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
