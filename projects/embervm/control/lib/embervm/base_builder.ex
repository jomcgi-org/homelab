defmodule Embervm.BaseBuilder do
  @moduledoc """
  Turns a `Workload`'s OCI image into a pristine base snapshot by driving the
  node daemon's `BuildBase` RPC, then reports the result back into the
  Workload's `status` subresource (`snapshotRef`, `snapshotDigest`, and the
  `Ready`/`BaseBuilt` conditions).

  This is the first control-plane component that DRIVES the node daemon (issues
  a mutating RPC and reconciles its result into CR status), where
  `Embervm.NodeRegistry` only CONSUMES node truth. Building a base is the
  on-ramp that makes a Workload eligible to ever run: a Workload never becomes
  `Ready` without a restorable base.

  ## trigger: the watcher hands us admissions and spec changes

  `Embervm.WorkloadWatcher` is already the informer that sees every Workload
  admission, spec change, and deletion. Rather than open a second watch, it
  calls `reconcile/2` (a cast) for every valid Workload it reconciles and
  `forget/2` for every invalid or deleted one. Those are `whereis`-guarded
  no-ops when this server is not running, so the watcher's own unit tests (which
  start no BaseBuilder) are unaffected, and a BaseBuilder crash cannot take the
  watcher down. Because the supervision tree starts this process BEFORE the
  watcher under `:rest_for_one`, a BaseBuilder restart also restarts the
  watcher, whose boot LIST re-casts every Workload into the fresh builder: the
  reconcile is idempotent, so re-driving is free and self-healing (no separate
  boot sweep needed).

  ## per-node serialization

  A base build is heavy (pull, bake, cold boot, health-gate, snapshot), so
  builds are serialized PER NODE: at most one build runs on a node at a time and
  concurrent admissions queue behind it. Serialization is structural, not a
  lock: each node has a FIFO `queue` and a single `building` slot, and the next
  queued build starts only when the current one finishes. The blocking
  `BuildBase` call runs in a `spawn_monitor`'d worker (the NodeRegistry streamer
  discipline) so it never freezes this GenServer, which owns the queues, the
  per-workload build state, and every status-write decision.

  In R0 there is exactly one node and every Workload's base is built on it (its
  single `status.snapshotRef` is that node's base handle). Multi-node base
  fan-out (a base per node a Workload may be primed on, with per-node snapshot
  refs) is the dispatcher's concern (Task 11); the per-node keying here means it
  needs more queue entries, not a reshape.

  ## change detection is on the spec signature, not the resolved digest

  A tag-pinned `image_ref` is resolved to a digest by the DAEMON at build time,
  so the control plane cannot compare digests before a build. Instead it detects
  change on a "base signature" derived from the spec fields that actually shape
  the base: `image_ref`, resources (`vcpus`, `mem_mib`), the guest contract
  (`guest_port`, `ready_path`), `init_env`, and (for the zip lane) the zip
  archive sha256. A rebuild is triggered iff the signature differs from the one
  the recorded base was built from. Including the zip sha256 is what makes the
  no-gap turnover property hold for a re-registered function: a new zip under
  the same name changes the sha256, so the signature differs and the base
  rebuilds, even though every other field is unchanged. Tag drift
  (same `image_ref` string, new upstream digest) does NOT rebuild: deploys are
  explicit CR updates, not tag drift. The digest the daemon resolves comes back
  in the response and is recorded in `status.snapshotDigest` for auditability.

  The daemon's own `BuildBase` idempotency (keyed on the resolved digest plus
  the `workload_revision` we send, the CR's `generation`) is the correctness
  backstop: re-driving a build we already made returns the existing snapshot
  with `already_built: true`, cheaply, so the signature map here is a
  latency optimization, not a source of truth we must persist across restarts.

  ## failure, backoff, and the Ready gate

  A failed build sets `BaseBuilt=False` with the daemon's error string in the
  condition message and retries with exponential backoff (capped at 10m). The
  `Ready` condition is derived purely from whether a restorable base exists
  (`snapshotRef` present), so:

    * first build in progress or failed -> no base -> `Ready=False`;
    * a rebuild (signature change) in progress or failed while an OLD base is
      still recorded -> `Ready=True` (the old base still serves; no dispatch
      gap), `BaseBuilt=False`.

  `status.snapshotRef` is only ever ADVANCED to a base the daemon has already
  confirmed built, never to an in-progress one, which is what makes the
  acceptance property hold: at any point, the `snapshotRef` in status names a
  restorable base.

  ## turnover on a digest change (a documented seam, not built here)

  When a rebuild produces a new snapshot, the old snapshot ref is recorded in
  the workload's `superseded_refs` for Task 11's PoolManager to reconcile:
  proactively destroy the old-base primed VMs and re-prime from the new base,
  destroying the old base file only after zero primed VMs reference it. There is
  no pool in R0, so this module only emits the new `snapshotRef` and records the
  old one; it does not destroy or re-prime anything.

  ## status write coordination with the watcher

  Status is patched with a JSON merge-patch, which REPLACES arrays wholesale, so
  two writers touching `conditions` would clobber each other. Ownership is split
  by key: this module owns `conditions` (`Ready` + `BaseBuilt`), `snapshotRef`,
  and `snapshotDigest` for valid task Workloads; the watcher owns
  `observedGeneration` and `primedFloorSatisfied` (disjoint keys, no lost
  update) and keeps `conditions` only for the invalid-CR validation lane, which
  never reaches this module.
  """

  use GenServer
  require Logger

  alias Embervm.Node.V1.{
    ArtifactRef,
    BuildBaseRequest,
    BuildBaseResponse,
    EvictArtifactRequest,
    ExportArtifactRequest,
    NodeService,
    ResourceSpec,
    RestoreArtifactRequest,
    Trace,
    ZipSource
  }

  # Backoff for a failed build: exponential from 1s, capped at the spec's 10m.
  @base_backoff_ms 1_000
  @max_backoff_ms 600_000

  # Base-durability PR-1: how often the export reconcile sweeps for a current base
  # that is present-but-unexported and re-issues ExportArtifact. The immediate
  # post-build export is the fast path; this sweep is the self-healing backstop
  # (a lost export result, a CP restart between build and export, or a store that
  # was down at build time). 60s is well below any base's lifetime and the sweep
  # is bounded to current-base-present-but-unexported, so it never hammers.
  @export_reconcile_interval_ms 60_000

  # Base-durability PR-3: how often the reconciled base-retention sweep runs. It
  # reconciles each node's reported local base inventory (NodeStatus.local_bases,
  # the full per-ref disk truth) against the desired set (current ref + still-
  # refcounted superseded refs) and, when the destructive gate is on, evicts the
  # difference. 300s: retention is a slow-moving reconcile (a superseded base is
  # only born on a build turnover), never a hot path, and the sweep is bounded to
  # workloads whose current base is already exported. Well below any base's
  # lifetime; the guarded, idempotent EvictArtifact makes a re-sweep harmless.
  @retention_sweep_interval_ms 300_000

  # -- Client API ------------------------------------------------------------

  # :name defaults to __MODULE__ for the application's supervised singleton;
  # tests pass name: nil to run PID-addressed instances concurrently (the same
  # idiom as the other GenServers in this app).
  @spec start_link(keyword()) :: GenServer.on_start()
  def start_link(opts) do
    case Keyword.get(opts, :name, __MODULE__) do
      nil -> GenServer.start_link(__MODULE__, opts)
      name -> GenServer.start_link(__MODULE__, opts, name: name)
    end
  end

  @typedoc """
  The build descriptor the watcher hands us for a valid Workload: the spec
  fields that shape a base plus the identity needed to write status back.
  """
  @type desc :: %{
          required(:name) => String.t(),
          required(:namespace) => String.t(),
          required(:generation) => integer() | nil,
          optional(:class) => String.t() | nil,
          required(:image_ref) => String.t() | nil,
          optional(:zip) => %{
            required(:runtime) => String.t(),
            required(:code_uri) => String.t(),
            required(:sha256) => String.t(),
            required(:handler) => String.t()
          } | nil,
          required(:guest_port) => integer() | nil,
          required(:ready_path) => String.t(),
          required(:vcpus) => integer() | nil,
          required(:mem_mib) => integer() | nil,
          required(:init_env) => %{String.t() => String.t()}
        }

  @doc """
  Reconcile one Workload's desired base against what has been built. A cast so
  the watcher never blocks on a build. A `whereis`-guarded no-op when this
  server is not running (the watcher's own unit tests start no BaseBuilder, and
  the trigger fails open: the watcher's boot re-LIST re-drives us anyway).
  """
  @spec reconcile(GenServer.server(), desc()) :: :ok
  def reconcile(server \\ __MODULE__, desc) do
    cast_if_alive(server, {:reconcile, desc})
  end

  @doc """
  Drop a Workload the watcher no longer considers a valid build target (it was
  deleted, or an edit made it invalid). Cancels any pending retry and removes it
  from its node queue; a build already in flight is left to finish and its
  result discarded (the workload is gone from state). `whereis`-guarded like
  `reconcile/2`.
  """
  @spec forget(GenServer.server(), String.t()) :: :ok
  def forget(server \\ __MODULE__, name) do
    cast_if_alive(server, {:forget, name})
  end

  @doc """
  Add (or update the address of) a node to the builder's placement set
  (artifact-decoupling PR-C, C4). Under EndpointSlice discovery the builder is
  SEEDED EMPTY at boot (discovery cannot run at construction time, before Finch),
  so `Embervm.NodeRegistry`'s post-Finch discovery calls this as each noded pod
  appears. Adding the FIRST node re-drives every workload that was held
  `{:pending, :no_node}` so its base finally builds. Idempotent: re-adding a known
  node only refreshes its address. `whereis`-guarded like `reconcile/2`.
  """
  @spec add_node(GenServer.server(), String.t(), String.t()) :: :ok
  def add_node(server \\ __MODULE__, node_id, address) do
    cast_if_alive(server, {:add_node, node_id, address})
  end

  @doc """
  Remove a node from the builder's placement set (its noded pod vanished from
  discovery). A workload placed on it is unpinned so a later add re-places it; a
  build already in flight for that node is left to finish and its result is applied
  or dropped as usual. `whereis`-guarded like `reconcile/2`.
  """
  @spec remove_node(GenServer.server(), String.t()) :: :ok
  def remove_node(server \\ __MODULE__, node_id) do
    cast_if_alive(server, {:remove_node, node_id})
  end

  @doc """
  Report current refcounts against a superseded base snapshot ref for a
  workload, so the BaseBuilder can decide when it is safe to evict (R2 base
  refcounting, ADR embervm/001 standing decision 5). Callers report the counts
  they own: the PoolManager reports `:primed` (primed pristine VMs still on the
  old base), the SessionStore reports `:sessions` (non-terminal sessions still
  pinned to the old base as their birth lineage). A superseded base is destroyed
  (via EvictSnapshot) ONLY once BOTH are reported as zero; until then it stays
  restorable so a live or banked session can always relight from its birth base.
  Unknown/absent refs are ignored (the ref was never superseded, or already
  evicted). A synchronous call so a test can assert eviction ordering.

  R3: a future ServingStore (Task 9) may also report `:serving` (non-terminal
  serving instances still pinned to the old base as their birth lineage,
  mirroring `:sessions`); the counts are accepted today but NOT yet required
  for eviction (see the comment on `merge_refcounts/2`) since nothing reports
  it until that store exists.
  """
  @spec report_base_refs(GenServer.server(), String.t(), keyword()) :: :ok
  def report_base_refs(server \\ __MODULE__, ref, counts) do
    GenServer.call(server, {:report_base_refs, ref, counts})
  end

  @doc """
  A snapshot of every tracked Workload's build state, for tests and operational
  visibility. Includes queue/building state per node so an operator can see WHY
  a base is not built yet.
  """
  @spec status(GenServer.server()) :: map()
  def status(server \\ __MODULE__) do
    GenServer.call(server, :status)
  end

  @doc """
  Run one reconciled base-retention sweep synchronously and return the dry-run
  plan (a list of `%{workload, evict_refs, evict_bytes, skipped_unexported}`
  descriptors), for tests and operational visibility. A synchronous call so a
  test can drive the sweep deterministically and assert what it evicted (or, with
  the destructive gate off, what it WOULD evict) without waiting on the timer.
  The actual evictions (gate on) are spawned fire-and-forget exactly as the timer
  path does; the returned plan is the sweep's decision, independent of the gate.
  """
  @spec retention_sweep_now(GenServer.server()) :: [map()]
  def retention_sweep_now(server \\ __MODULE__) do
    GenServer.call(server, :retention_sweep_now)
  end

  defp cast_if_alive(server, msg) do
    case GenServer.whereis(server) do
      nil -> :ok
      _pid -> GenServer.cast(server, msg)
    end
  end

  # -- GenServer callbacks ---------------------------------------------------

  @impl true
  def init(opts) do
    nodes = Keyword.get(opts, :nodes, [])
    # Injected seams (defaults are the real gRPC + K8s status patch + wall clock):
    build_fun = Keyword.get(opts, :build_fun, &default_build/2)
    connect_fun = Keyword.get(opts, :connect_fun, &default_connect/1)
    disconnect_fun = Keyword.get(opts, :disconnect_fun, &default_disconnect/1)
    # R2 + PR-3: the eviction seam for destroying a fully-drained superseded base.
    # It now uses EvictArtifact{remote: false} (kind BASE) rather than the R2-era
    # EvictSnapshot: EvictSnapshot dispatched a base ref into the SESSION bundle
    # path on the node (RemoveSessionBundle), which no-op-removed a nonexistent
    # sessions/<ref> dir and returned success while bases/<ref> survived, so every
    # superseded base leaked. EvictArtifact routes to the new noded BASE arm that
    # removes bases/<ref> behind an in-use/BUILDING/not-last guard. Defaults to the
    # real NodeService.Stub.evict_artifact/2 over a per-call channel; tests inject a
    # fake to assert eviction fires exactly when both refcounts hit zero. The seam
    # takes (channel, workload, ref) so it can compose the vendor-keyed store prefix
    # (the BASE key is base/<vendor>/<workload>/<ref>).
    evict_fun = Keyword.get(opts, :evict_fun, &default_evict/3)
    # Base-durability PR-1: the ExportArtifact seam that writes a freshly-built
    # (or present-but-unexported) base back to the object store. Defaults to the
    # real NodeService.Stub.export_artifact/2 over a per-call channel; tests
    # inject a fake to assert export fires after a build and on the reconcile.
    export_fun = Keyword.get(opts, :export_fun, &default_export/2)
    # Base-durability PR-2: the RestoreArtifact seam that HYDRATES a recorded base
    # back onto a node that lacks it locally (cold-start / node-replacement),
    # turning a rebuild into an S3 download. Defaults to the real
    # NodeService.Stub.restore_artifact/2 over a per-call channel; tests inject a
    # fake to assert the restore-first/fallback decision. noded fast-ACKs a BASE
    # restore (accepted=true) and downloads async, so the CP polls NodeStatus for
    # the base to appear READY (see spawn_hydrate).
    restore_fun = Keyword.get(opts, :restore_fun, &default_restore/2)
    # Op-log seam for the audit-only :artifact_exported record (no projection
    # table; the log itself is the record). Mirrors the sweeper managers.
    op_log = Keyword.get(opts, :op_log, Embervm.OpLog.SQLite)
    op_log_mod = Keyword.get(opts, :op_log_mod, Embervm.OpLog.SQLite)
    tenant = Keyword.get(opts, :tenant, "homelab")
    # Sweep cadence for the export reconcile. 0 disables the timer entirely (the
    # unit-test default, so a test drives :export_reconcile explicitly and asserts
    # deterministically); production uses the module default.
    export_reconcile_interval_ms =
      Keyword.get(opts, :export_reconcile_interval_ms, @export_reconcile_interval_ms)

    # Base-durability PR-2: how long a hydrate worker polls NodeStatus for the
    # base to appear READY before giving up and falling back to BuildBase. noded
    # downloads the base async (a multi-GB, multi-minute S3 read), so the CP polls
    # rather than blocks. hydrate_poll_interval_ms is the poll cadence,
    # hydrate_poll_max the attempt ceiling; interval * max bounds the wait before
    # a stuck/failed hydrate (node died mid-download, S3 flaked) rebuilds instead.
    # Defaults cover a ~3G base download comfortably (5s * 90 = 7.5 min); tests
    # shrink both so a fallback path asserts deterministically without real sleep.
    hydrate_poll_interval_ms = Keyword.get(opts, :hydrate_poll_interval_ms, 5_000)
    hydrate_poll_max = Keyword.get(opts, :hydrate_poll_max, 90)

    # Base-durability PR-3: the reconciled base-retention sweep cadence. 0 disables
    # the timer entirely (the unit-test default, so a test drives :retention_sweep
    # explicitly and asserts deterministically); production uses the module default.
    retention_sweep_interval_ms =
      Keyword.get(opts, :retention_sweep_interval_ms, @retention_sweep_interval_ms)

    # Base-durability PR-3: the destructive gate. When FALSE (the default, and the
    # production default until an operator opts in) the retention sweep computes and
    # LOGS what it would evict (a dry-run line per workload: candidate count + total
    # bytes) but deletes NOTHING. When TRUE it issues EvictArtifact{remote: false}
    # for each superseded local base outside the desired set. application.ex reads it
    # from EMBERVM_BASE_RETENTION_SWEEP ("1"/"true" => on, unset/"0" => off) so it can
    # be enabled later via deploy values without a code change; nothing in this PR
    # sets it on. Merging this PR is inert: the sweep runs but only logs.
    retention_sweep_enabled = Keyword.get(opts, :retention_sweep_enabled, false)

    status_writer = Keyword.get(opts, :status_writer, &Embervm.K8s.patch_workload_status/3)
    clock = Keyword.get(opts, :clock, fn -> System.system_time(:millisecond) end)
    base_backoff = Keyword.get(opts, :base_backoff_ms, @base_backoff_ms)
    max_backoff = Keyword.get(opts, :max_backoff_ms, @max_backoff_ms)
    # runtime -> pinned runtime base image ref (e.g. %{"python312" => "...:tag"}).
    # The zip lane resolves source.zip.runtime to the ZipSource.runtime_image_ref
    # through this map; an unknown runtime yields a Ready=False condition, never a
    # crash. Empty (no runtime image wired, e.g. a CI chart without the pin) means
    # every zip build is held with a clear "runtime not configured" condition.
    runtime_images = Keyword.get(opts, :runtime_images, %{})

    # Per-instance capacity facts (Step 5): placement reads each registered
    # instance's mem_budget_mib / size_class / node_id from here so a base is built
    # on an instance big enough to boot the guest, and the biggest-budget instance is
    # preferred (the DS wildcard first, else the largest classed brick), instead of
    # blindly pinning the first-registered instance. Defaults to the shared registry
    # table; tests that assert pure placement leave it empty, and placement fails OPEN
    # (treats a budget-unknown instance as eligible) so a builder with no capacity view
    # still places, keeping its behaviour identical to the pre-Step-5 List.first pick.
    capacity_table = Keyword.get(opts, :capacity_table, Embervm.NodeCapacity.table())

    node_ids = Enum.map(nodes, & &1.id)

    node_addr = for %{id: id, address: address} <- nodes, into: %{}, do: {id, address}

    node_runtime =
      for id <- node_ids, into: %{} do
        {id, %{building: nil, queue: [], worker: nil}}
      end

    state = %{
      node_ids: node_ids,
      node_addr: node_addr,
      nodes: node_runtime,
      workloads: %{},
      # pid -> %{node_id, name, signature} for the CURRENT build worker, so a
      # result from a superseded worker (or for a since-changed spec) is dropped.
      workers: %{},
      # In-flight base exports, a MapSet of {workload, ref} (fast-durability-export
      # fix). A base export now returns a fast ack and completes asynchronously on
      # the node, but a large base's upload takes longer than the 60s export
      # reconcile interval, so the sweep would otherwise pile up duplicate concurrent
      # ExportArtifact calls for the same ref that is still uploading. spawn_export
      # skips a ref already tracked here and clears it when the spawned worker
      # finishes (success OR failure), so the reconcile re-issues only once the prior
      # attempt has settled. Keyed by {workload, ref} (not ref alone) so two
      # workloads that ever shared a ref never mask each other.
      exports_in_flight: MapSet.new(),
      build_fun: build_fun,
      connect_fun: connect_fun,
      disconnect_fun: disconnect_fun,
      evict_fun: evict_fun,
      export_fun: export_fun,
      restore_fun: restore_fun,
      op_log: op_log,
      op_log_mod: op_log_mod,
      tenant: tenant,
      export_reconcile_interval_ms: export_reconcile_interval_ms,
      hydrate_poll_interval_ms: hydrate_poll_interval_ms,
      hydrate_poll_max: hydrate_poll_max,
      # In-flight hydrate guard: workloads with a hydrate worker running, so a
      # re-reconcile during the async download does not spawn a second hydrate for
      # the same base (mirrors the node queue's role for exports/evicts). A worker
      # clears its own entry on completion via the {:hydrate_done, name} message.
      hydrating: MapSet.new(),
      runtime_images: runtime_images,
      capacity_table: capacity_table,
      status_writer: status_writer,
      clock: clock,
      base_backoff_ms: base_backoff,
      max_backoff_ms: max_backoff,
      retention_sweep_interval_ms: retention_sweep_interval_ms,
      retention_sweep_enabled: retention_sweep_enabled
    }

    schedule_export_reconcile(state)
    schedule_retention_sweep(state)

    {:ok, state}
  end

  @impl true
  def handle_cast({:reconcile, desc}, state) do
    {:noreply, reconcile_desc(state, desc)}
  end

  def handle_cast({:forget, name}, state) do
    {:noreply, forget_workload(state, name)}
  end

  def handle_cast({:add_node, node_id, address}, state) do
    {:noreply, add_node_to_state(state, node_id, address)}
  end

  def handle_cast({:remove_node, node_id}, state) do
    {:noreply, remove_node_from_state(state, node_id)}
  end

  @impl true
  def handle_call({:report_base_refs, ref, counts}, _from, state) do
    {:reply, :ok, apply_base_refs(state, ref, counts)}
  end

  def handle_call(:retention_sweep_now, _from, state) do
    {plan, state} = retention_sweep_plan(state)
    {:reply, plan, state}
  end

  def handle_call(:status, _from, state) do
    workloads =
      for {name, w} <- state.workloads, into: %{} do
        {name,
         %{
           node_id: w.node_id,
           image_ref: w.image_ref,
           snapshot_ref: w.snapshot_ref,
           snapshot_digest: w.snapshot_digest,
           built: w.built_signature != nil and w.built_signature == signature(w),
           superseded_refs: w.superseded_refs,
           base_refs: w.base_refs,
           backoff_ms: w.backoff_ms
         }}
      end

    nodes =
      for {id, n} <- state.nodes, into: %{} do
        {id, %{building: n.building, queued: n.queue, connected: n.worker != nil}}
      end

    {:reply, %{workloads: workloads, nodes: nodes}, state}
  end

  # A build worker reported its result. Drop it if the pid is not our current
  # worker (defensive; builds are serial so this is rare), then apply.
  @impl true
  def handle_info({:build_result, pid, result}, state) do
    case Map.get(state.workers, pid) do
      nil -> {:noreply, state}
      meta -> {:noreply, finish_build(state, pid, meta, result)}
    end
  end

  # A worker died WITHOUT reporting a result (it always sends one in the normal
  # path, so a bare DOWN is an abnormal exit). Treat as a build error.
  def handle_info({:DOWN, _ref, :process, pid, reason}, state) do
    case Map.get(state.workers, pid) do
      nil -> {:noreply, state}
      meta -> {:noreply, finish_build(state, pid, meta, {:error, {:worker_down, reason}})}
    end
  end

  # A backoff retry timer fired for a failed build.
  def handle_info({:retry, name}, state) do
    {:noreply, retry_workload(state, name)}
  end

  # Base-durability PR-2: a hydrate worker finished (hydrated, fell back, or
  # crashed in `after`). Clear the in-flight guard so a later reconcile can hydrate
  # (or build) again; the fallback path already re-drove a build via :reconcile.
  def handle_info({:hydrate_done, name}, state) do
    {:noreply, update_in(state.hydrating, &MapSet.delete(&1, name))}
  end

  # Base-durability PR-1: the periodic export reconcile fired. Re-issue export for
  # any current base present-but-unexported, then re-arm the timer.
  def handle_info(:export_reconcile, state) do
    state = export_reconcile(state)
    schedule_export_reconcile(state)
    {:noreply, state}
  end

  # Base-durability PR-3: the periodic reconciled base-retention sweep fired.
  # Reconcile each node's reported local base inventory against the desired set and
  # (when the destructive gate is on) evict the difference, then re-arm the timer.
  def handle_info(:retention_sweep, state) do
    state = retention_sweep(state)
    schedule_retention_sweep(state)
    {:noreply, state}
  end

  # A base export worker finished (success OR failure): clear its in-flight key so
  # the next export reconcile may re-issue it. See spawn_export for the dedup model.
  def handle_info({:export_done, key}, state) do
    {:noreply, %{state | exports_in_flight: MapSet.delete(state.exports_in_flight, key)}}
  end

  def handle_info(_msg, state), do: {:noreply, state}

  # Best-effort: stop orphaned build workers from outliving the GenServer that
  # owns their gRPC connections. Not load-bearing (a monitored worker exits when
  # its owner dies anyway), just tidy.
  @impl true
  def terminate(_reason, state) do
    for {pid, _meta} <- state.workers, do: Process.exit(pid, :shutdown)
    :ok
  end

  # -- reconcile ---------------------------------------------------------------

  # Store/update a Workload's desired base and, if the desired signature differs
  # from what has been built (or is being built), enqueue a build. No node
  # configured yet means we cannot build: record the intent and report it.
  defp reconcile_desc(state, %{name: name} = desc) do
    prev = Map.get(state.workloads, name)
    node_id = placement(state, prev, Map.get(desc, :mem_mib) || 0)

    w = merge_desc(prev, desc, node_id)
    state = put_in(state.workloads[name], w)

    cond do
      node_id == nil ->
        # No node wired (empty config, e.g. CI): hold the desc and report why.
        write_base_status(state, w, {:pending, :no_node})

      zip_runtime_unresolved?(state, w) ->
        # A zip workload whose runtime does not resolve to a pinned image ref
        # (unknown runtime, or no runtime image configured in this release).
        # Report a precise Ready=False and build nothing, never crash. The
        # existing base (if any) is untouched, so Ready stays True on an edit
        # that broke only the runtime resolution.
        write_base_status(state, w, {:failed, zip_runtime_error(state, w)})

      should_hydrate?(state, w, node_id) ->
        # Base-durability PR-2: restore-first (hydrate-on-miss). The CP recorded a
        # base (snapshot_ref set, signature unchanged) but the node AFFIRMATIVELY
        # reports it absent (base_state NONE, not merely unreported): a cold-start,
        # a node replacement, or scratch loss. Instead of rebuilding, download the
        # SAME content-addressed ref back from S3. noded fast-ACKs and downloads
        # async, so this spawns a worker that triggers RestoreArtifact then polls
        # NodeStatus for the base to appear READY; it falls back to BuildBase on an
        # S3 miss (FAILED_PRECONDITION) or a poll timeout.
        #
        # SCOPE (deliberate): restore-first covers ONLY the recorded-but-absent
        # case. A NEW build and a spec-change REBUILD both target a ref the CP
        # cannot know before building (noded derives <workload>__hash(digest +
        # revision + vendor) node-side), so they correctly stay BuildBase.
        # Rollback-to-a-predecessor is intentionally a rebuild, NOT a hydrate:
        # under N=1 S3 retention (Joe, 2026-07-21) no predecessor ref persists in
        # the store to hydrate from, so a signature->ref history would be dead
        # weight. See docs/plans/2026-07-21-embervm-base-durability-and-cross-vendor.md.
        state
        |> mark_hydrating(name)
        |> spawn_hydrate(w)

      w.built_signature == signature(w) and w.snapshot_ref != nil ->
        # Desired base already built and recorded: idempotent no-op. (The watcher
        # separately writes observedGeneration for a generation-only change.)
        state

      already_targeting?(state, node_id, name, signature(w)) ->
        # A build for this exact signature is already queued or in flight.
        state

      true ->
        state
        |> cancel_pending_retry(name)
        |> enqueue(node_id, name)
        |> write_base_status(w, :building)
        |> maybe_start_build(node_id)
    end
  end

  # -- restore-first (hydrate-on-miss, base-durability PR-2) --------------------

  # Should this workload's recorded base be HYDRATED (S3 restore) rather than
  # rebuilt? True only when ALL four guards hold (fail-safe: any unmet guard falls
  # through to the ordinary build/no-op branches):
  #
  #   1. a base is recorded (snapshot_ref set) AND the spec is unchanged
  #      (built_signature == signature): we are restoring the SAME ref we would
  #      otherwise rebuild, never papering over a spec change (whose target ref is
  #      unknowable pre-build anyway);
  #   2. the node AFFIRMATIVELY reports that base absent (base_state NONE for the
  #      workload). This is the KEY guard: a workload the node has not yet reported
  #      (no fact) is NOT hydrated, so the race window right after a build (base
  #      built, node has not re-reported READY) never triggers a spurious restore;
  #   3. no build is already queued or in flight for the workload (do not race a
  #      hydrate against a build);
  #   4. no hydrate is already in flight for the workload (the in-flight guard, so
  #      a re-reconcile during the async download does not spawn a second worker).
  defp should_hydrate?(state, w, node_id) do
    is_binary(w.snapshot_ref) and w.built_signature == signature(w) and
      node_reports_base_absent?(state, node_id, w.name) and
      not already_targeting?(state, node_id, w.name, signature(w)) and
      not MapSet.member?(state.hydrating, w.name)
  end

  # The node AFFIRMATIVELY reports the workload's base absent: a fact exists for
  # the workload on the node AND its base_state is NONE. A missing fact (node not
  # reported yet, or the base present-but-unprojected) is NOT "absent": returning
  # false there is what keeps the post-build race from hydrating (guard 2 above).
  defp node_reports_base_absent?(state, node_id, workload) do
    case node_base_fact(state, node_id, workload) do
      {:ok, %{base_state: :BASE_BUILD_STATE_NONE}} -> true
      _ -> false
    end
  end

  defp mark_hydrating(state, name) do
    update_in(state.hydrating, &MapSet.put(&1, name))
  end

  # Spawn a monitored-free (fire-and-forget) hydrate worker for one recorded base.
  # The worker triggers RestoreArtifact (vendor-stamped) then polls NodeStatus for
  # the base to appear READY, since noded downloads a BASE async (fast-ACK
  # accepted=true, multi-GB download off the RPC). It falls back to a BuildBase by
  # casting :reconcile for the workload on an S3 miss (FAILED_PRECONDITION) or a
  # poll timeout; on hydrate success it records :artifact_restored{kind:"base"} so
  # the hydrate-vs-build ratio is auditable from the op-log. It always sends
  # {:hydrate_done, name} so the GenServer clears the in-flight guard.
  #
  # Not spawn_monitor (unlike a build worker): a hydrate result never mutates
  # BaseBuilder's build state (the base's presence lives on the node's own fact,
  # read back on the next status), so a crashed worker only needs the in-flight
  # guard cleared, which the {:hydrate_done, ...} in `after` guarantees.
  defp spawn_hydrate(state, w) do
    owner = self()
    name = w.name
    node_id = w.node_id
    ref = w.snapshot_ref
    address = state.node_addr[node_id]
    connect_fun = state.connect_fun
    disconnect_fun = state.disconnect_fun
    restore_fun = state.restore_fun
    capacity_table = state.capacity_table
    poll_interval = state.hydrate_poll_interval_ms
    poll_max = state.hydrate_poll_max
    op_log = state.op_log
    op_log_mod = state.op_log_mod
    tenant = state.tenant
    clock = state.clock

    spawn(fn ->
      try do
        outcome =
          run_hydrate(%{
            owner: owner,
            name: name,
            node_id: node_id,
            ref: ref,
            address: address,
            connect_fun: connect_fun,
            disconnect_fun: disconnect_fun,
            restore_fun: restore_fun,
            capacity_table: capacity_table,
            poll_interval: poll_interval,
            poll_max: poll_max
          })

        case outcome do
          :hydrated ->
            record_base_restored(op_log_mod, op_log, tenant, clock, name, ref)

          {:fallback, reason} ->
            Logger.info(
              "embervm base builder: hydrate #{name}/#{ref} fell back to build (#{inspect(reason)})"
            )

            # Re-drive a build for the workload. The reconcile is idempotent and
            # re-reads current desc/placement, so a spec that changed under the
            # hydrate is handled correctly.
            reconcile(owner, hydrate_fallback_desc(w))
        end
      after
        send(owner, {:hydrate_done, name})
      end
    end)

    state
  end

  # Drive one hydrate attempt: RestoreArtifact, then (on accepted) poll NodeStatus
  # for the base to become READY. Returns :hydrated on success, {:fallback, reason}
  # on an S3 miss / restore failure / poll timeout (the caller then rebuilds).
  defp run_hydrate(ctx) do
    case dial_restore(ctx) do
      {:ok, %{skipped: true}} ->
        # The base was already present locally when the RPC ran (a race with
        # another restore, or the node reported stale): treat as hydrated, the
        # node's next status advertises it READY.
        :hydrated

      {:ok, %{accepted: true}} ->
        poll_base_ready(ctx, ctx.poll_max)

      {:ok, other} ->
        # An unexpected non-accepted, non-skipped response (e.g. an inline restore
        # that reported bytes_moved without accepted, from a daemon that predates
        # the async change). The base is present; treat as hydrated.
        _ = other
        :hydrated

      {:error, {:grpc, :failed_precondition, _msg}} ->
        # S3 does not hold the ref (or the store is disabled): rebuild AT ONCE, no
        # wait. This is the fast, distinguishable miss the async ack guarantees.
        {:fallback, :not_present_in_store}

      {:error, reason} ->
        {:fallback, {:restore_failed, reason}}
    end
  end

  # Poll the node fact until the workload's base shows READY (hydrate landed), or
  # the attempt budget is exhausted (fall back to build). Reads the SHARED capacity
  # table the node registry keeps fresh, so the poll sees the async download's
  # ReconcileBasesFromDisk re-registration as soon as it lands. Resolves the fact
  # by instance_id (find_capacity_fact), matching how node_base_fact reads it (the
  # builder's node_id is an instance_id "node/pod_uid", not the daemon's node_id).
  defp poll_base_ready(_ctx, 0), do: {:fallback, :poll_timeout}

  defp poll_base_ready(ctx, attempts_left) do
    case find_capacity_fact(ctx.capacity_table, ctx.node_id) do
      {:ok, fact} ->
        case get_in(fact, [:workloads, ctx.name]) do
          %{base_state: :BASE_BUILD_STATE_READY, snapshot_ref: ref} when ref == ctx.ref ->
            :hydrated

          _ ->
            sleep_then_poll(ctx, attempts_left)
        end

      :error ->
        sleep_then_poll(ctx, attempts_left)
    end
  end

  defp sleep_then_poll(ctx, attempts_left) do
    Process.sleep(ctx.poll_interval)
    poll_base_ready(ctx, attempts_left - 1)
  end

  # Issue the vendor-stamped RestoreArtifact over a per-call channel, normalising a
  # GRPC.RPCError into a {:grpc, status_atom, message} tuple so run_hydrate can
  # distinguish an S3 miss (FAILED_PRECONDITION, fast fallback) from other errors.
  defp dial_restore(ctx) do
    case ctx.connect_fun.(ctx.address) do
      {:ok, channel} ->
        req =
          Embervm.RestoreVendor.stamp(
            ctx.capacity_table,
            stamp_key(ctx.capacity_table, ctx.node_id),
            %RestoreArtifactRequest{
              artifact: %ArtifactRef{
                kind: :ARTIFACT_KIND_BASE,
                workload: ctx.name,
                ref: ctx.ref
              },
              trace: %Trace{workload: ctx.name}
            }
          )

        try do
          case ctx.restore_fun.(channel, req) do
            {:ok, resp} -> {:ok, resp}
            {:error, err} -> {:error, normalize_grpc_error(err)}
          end
        catch
          kind, reason -> {:error, {kind, reason}}
        after
          ctx.disconnect_fun.(channel)
        end

      {:error, reason} ->
        {:error, {:connect, reason}}
    end
  end

  # Resolve the anchor key RestoreVendor.stamp reads the CPU vendor off. The
  # builder's node_id is an instance_id ("node/pod_uid"), which NodeCapacity.fetch
  # does NOT resolve (its binary form matches the daemon's node_id field, its tuple
  # form matches the {configured_id, pod_uid} ETS key). So resolve the instance's
  # own capacity fact by instance_id and hand stamp the {configured_id, pod_uid}
  # tuple, the exact per-instance key. Falls back to the raw node_id string when no
  # fact is found (stamp then resolves "" and noded maps it to the legacy alias, so
  # today's single-vendor fleet still restores; correct once vendors are reported).
  defp stamp_key(table, node_id) do
    case find_capacity_fact(table, node_id) do
      {:ok, %{configured_id: cfg, pod_uid: pod}} when is_binary(cfg) -> {cfg, pod || ""}
      _ -> node_id
    end
  end

  # Map a GRPC.RPCError to {:grpc, status_atom, message}; pass other errors through.
  # FAILED_PRECONDITION (status 9) is noded's S3-not-present / store-disabled /
  # sku-mismatch signal, which run_hydrate treats as an immediate rebuild fallback.
  defp normalize_grpc_error(%GRPC.RPCError{status: 9, message: msg}),
    do: {:grpc, :failed_precondition, msg}

  defp normalize_grpc_error(%GRPC.RPCError{status: status, message: msg}),
    do: {:grpc, status, msg}

  defp normalize_grpc_error(other), do: other

  # The desc a hydrate fallback re-drives: reconstruct the minimal build descriptor
  # from the workload's own recorded fields, so the reconcile rebuilds the SAME
  # base the hydrate failed to restore. class/zip are optional map keys.
  defp hydrate_fallback_desc(w) do
    %{
      name: w.name,
      namespace: w.namespace,
      generation: w.generation,
      class: Map.get(w, :class),
      image_ref: w.image_ref,
      zip: Map.get(w, :zip),
      guest_port: w.guest_port,
      ready_path: w.ready_path,
      vcpus: w.vcpus,
      mem_mib: w.mem_mib,
      init_env: w.init_env
    }
  end

  # Append the audit-only :artifact_restored op recording a base HYDRATE (kind
  # "base"), the partner signal to :base_built, so the hydrate-vs-build ratio is
  # auditable from the log alone. Best-effort: an append failure never crashes the
  # hydrate worker (the durable fact is the restored bytes on the node).
  defp record_base_restored(op_log_mod, op_log, tenant, clock, workload, ref) do
    op = %Embervm.OpLog.Op{
      kind: :artifact_restored,
      tenant: tenant,
      principal: "system:base:#{workload}",
      workload: workload,
      ts: clock.(),
      payload: %{kind: "base", ref: ref}
    }

    _ = op_log_mod.append(op_log, op)
    :ok
  rescue
    e ->
      Logger.warning("embervm base builder: artifact_restored append raised",
        workload: workload,
        error: inspect(e)
      )

      :ok
  end

  # -- node set updates (discovery) --------------------------------------------

  # Add or refresh a node in the placement set, then re-drive every workload that
  # was held with no node so its base finally builds. Idempotent: a known node id
  # keeps its queue/worker and only its address is refreshed.
  defp add_node_to_state(state, node_id, address) do
    known? = Map.has_key?(state.nodes, node_id)

    state =
      state
      |> put_in([:node_addr, node_id], address)
      |> update_in([:node_ids], fn ids -> if node_id in ids, do: ids, else: ids ++ [node_id] end)
      |> update_in([:nodes], fn nodes ->
        Map.put_new(nodes, node_id, %{building: nil, queue: [], worker: nil})
      end)

    if known? do
      # A pure address refresh (a rolled pod at a new IP): nothing to re-drive, the
      # workloads already placed on this id keep their placement and its next build
      # dials the refreshed address.
      state
    else
      # A newly-added node: re-drive every workload that was held {:pending,
      # :no_node} (node_id nil, unbuilt) so it places onto the fleet now.
      state.workloads
      |> Map.keys()
      |> Enum.reduce(state, fn name, acc -> redrive_pending_workload(acc, name) end)
    end
  end

  # Re-place a held workload (no node, no built base) now that a node exists,
  # reusing the same enqueue + status + build path reconcile_desc's build branch
  # uses. A workload already placed or already built is left untouched.
  defp redrive_pending_workload(state, name) do
    w = Map.get(state.workloads, name)
    node_id = placement(state, %{node_id: nil}, (w && w.mem_mib) || 0)

    cond do
      w == nil or node_id == nil ->
        state

      w.node_id != nil ->
        # Already placed on some node (or built); do not steal it to a new node.
        state

      w.snapshot_ref != nil and w.built_signature == signature(w) ->
        state

      zip_runtime_unresolved?(state, w) ->
        write_base_status(state, w, {:failed, zip_runtime_error(state, w)})

      true ->
        w = %{w | node_id: node_id}
        state = put_in(state.workloads[name], w)

        state
        |> cancel_pending_retry(name)
        |> enqueue(node_id, name)
        |> write_base_status(w, :building)
        |> maybe_start_build(node_id)
    end
  end

  # Remove a node from the placement set: drop it from node_ids/node_addr (so no
  # NEW build targets it) and UNPIN every workload placed on it (node_id -> nil) so
  # a later add re-drives it.
  #
  # The runtime entry (state.nodes[node_id]) is dropped ONLY when it has no build in
  # flight. If a worker is still running for this node, we KEEP the entry (with its
  # queue cleared) so finish_build's put_in([:nodes, node_id, :building], nil) does
  # not crash on a missing key when that worker reports; the entry is harmless once
  # node_id is gone from node_ids (nothing places onto it) and finish_build's
  # trailing maybe_start_build drains the empty queue to a no-op. A pure orphan
  # entry with no queue and no worker is fine to leave until the next add refreshes
  # it, but we drop it when idle to keep the map tidy.
  defp remove_node_from_state(state, node_id) do
    state =
      state
      |> update_in([:node_ids], &List.delete(&1, node_id))
      |> update_in([:node_addr], &Map.delete(&1, node_id))

    state =
      case Map.get(state.nodes, node_id) do
        %{building: nil, worker: nil} ->
          update_in(state.nodes, &Map.delete(&1, node_id))

        %{} = n ->
          # Build in flight: keep the entry (clear its queue) so the in-flight
          # worker's finish_build lands cleanly; it self-cleans on completion.
          put_in(state.nodes[node_id], %{n | queue: []})

        nil ->
          state
      end

    update_in(state.workloads, fn workloads ->
      for {name, w} <- workloads, into: %{} do
        if w.node_id == node_id, do: {name, %{w | node_id: nil}}, else: {name, w}
      end
    end)
  end

  # Cancel a workload's pending backoff-retry timer (if any) when a build is
  # about to be enqueued by another path, so the timer cannot later fire and
  # enqueue a redundant second build for the same target.
  defp cancel_pending_retry(state, name) do
    case Map.get(state.workloads, name) do
      %{retry_timer: ref} when ref != nil ->
        cancel_timer(ref)
        put_in(state.workloads[name].retry_timer, nil)

      _ ->
        state
    end
  end

  # Placement (Step 5, brick co-location): pick the registered INSTANCE a workload's
  # base builds on. `state.node_ids` are instance_ids (NodeRegistry registers each
  # noded pod by its instance_id, node_registry.ex default_base_builder_update), so
  # under co-located bricks a node holds several. The old `List.first(state.node_ids)`
  # blindly pinned the first-registered instance, which Fable found live pinned ALL
  # workloads to the one 2Gi brick, starving the DS/16Gi bricks of bases and wedging
  # the fleet at :no_capacity.
  #
  # Instead: among the instances that can actually BUILD this workload (mem_budget_mib
  # big enough to boot a `need_mib` guest, OR a wildcard/zero-budget instance that is
  # always eligible), pick the one with the LARGEST budget (the DS wildcard ranks
  # first as the full-node envelope, else the biggest classed brick) so a small brick
  # never captures a base it cannot build. Bases are NODE-SHARED (baseDir =
  # SnapshotRoot/bases, served by any co-located instance), so we only need ONE build
  # per node; picking a single best instance yields exactly one (node, workload) build.
  #
  # STICKY (no thrash): a workload keeps its current pin as long as that instance is
  # still registered AND still eligible AND no STRICTLY-larger eligible instance has
  # since registered. It re-places only when its instance expired/deregistered, became
  # ineligible, or a bigger eligible instance appeared. Determinism: ties (equal rank)
  # break on instance_id, so the choice is stable run to run.
  #
  # Fail-OPEN when capacity is unknown: an instance with no capacity fact (a builder
  # seeded before WatchNode populated facts, or a test with no table) is treated as an
  # eligible wildcard, so placement still returns a node and is behaviour-identical to
  # the pre-Step-5 pick on a fleet with no per-instance budgets reported.
  defp placement(state, prev, need_mib) do
    case eligible_build_instances(state, need_mib) do
      [] ->
        nil

      instances ->
        best = Enum.max_by(instances, &build_rank/1)
        keep_or_replace(prev, instances, best)
    end
  end

  # Keep the workload's current pin when it is still an eligible candidate and no
  # strictly-larger one exists; otherwise move to the best. Comparing on build_rank
  # (not identity) means an equal-rank newcomer never steals a healthy pin.
  defp keep_or_replace(%{node_id: node_id}, instances, best) when is_binary(node_id) do
    case Enum.find(instances, fn i -> i.instance_id == node_id end) do
      nil -> best.instance_id
      current -> if build_rank(best) > build_rank(current), do: best.instance_id, else: node_id
    end
  end

  defp keep_or_replace(_prev, _instances, best), do: best.instance_id

  # The registered instances that can BUILD `need_mib`, each as a compact map
  # (instance_id + the fields the rank/eligibility read). An instance with no capacity
  # fact is a fail-open wildcard (budget/class unknown => always eligible). Node-shared
  # bases mean we keep at most one instance per node_id (the highest-ranked), so the
  # best pick is naturally one build per node.
  defp eligible_build_instances(state, need_mib) do
    state.node_ids
    |> Enum.map(fn iid -> instance_build_facts(state, iid) end)
    |> Enum.filter(fn i -> build_eligible?(i, need_mib) end)
    |> dedupe_per_node()
  end

  # Compact build-facts for a registered instance_id: its reported size_class /
  # mem_budget_mib / node_id, or wildcard defaults (size_class "", budget 0) when no
  # capacity fact is found (fail-open). node_id falls back to the instance_id itself so
  # dedupe_per_node still groups sanely for a fact-less id.
  defp instance_build_facts(state, instance_id) do
    case find_capacity_fact(state.capacity_table, instance_id) do
      {:ok, f} ->
        %{
          instance_id: instance_id,
          node_id: Map.get(f, :node_id) || instance_id,
          size_class: Map.get(f, :size_class, ""),
          mem_budget_mib: Map.get(f, :mem_budget_mib, 0)
        }

      :error ->
        %{instance_id: instance_id, node_id: instance_id, size_class: "", mem_budget_mib: 0}
    end
  end

  # Look up the capacity fact whose derived instance_id matches (the registry stamps
  # :instance_id = "node/pod_uid" into each fact). Returns :error when the table is
  # absent/empty or no fact carries that instance_id, which makes placement fail-open.
  defp find_capacity_fact(table, instance_id) do
    table
    |> Embervm.NodeCapacity.all()
    |> Enum.find(fn f -> Map.get(f, :instance_id) == instance_id end)
    |> case do
      nil -> :error
      f -> {:ok, f}
    end
  end

  # An instance can build the workload when it is a wildcard (empty class or zero
  # budget: the full-node envelope, always able to boot) or its budget covers need.
  defp build_eligible?(i, need_mib) do
    Embervm.Placement.wildcard?(i) or i.mem_budget_mib >= need_mib
  end

  # Rank for "largest-budget eligible": the DS wildcard ranks above every classed
  # brick (rank tuple {1, budget}); classed bricks rank by budget ({0, budget}). Higher
  # is better; max_by picks the biggest. (A zero-budget wildcard still beats a classed
  # brick because its first tuple element is 1.)
  defp build_rank(i) do
    if Embervm.Placement.wildcard?(i), do: {1, i.mem_budget_mib}, else: {0, i.mem_budget_mib}
  end

  # Bases are node-shared, so at most one instance per node_id needs to build: keep the
  # highest-ranked instance per node (ties break on instance_id for determinism).
  defp dedupe_per_node(instances) do
    instances
    |> Enum.group_by(& &1.node_id)
    |> Enum.map(fn {_node, group} ->
      Enum.max_by(group, fn i -> {build_rank(i), i.instance_id} end)
    end)
  end

  # Fold a fresh desc into the workload's build state, preserving the built base
  # (built_signature/snapshot_ref/digest/superseded) across spec edits so a
  # rebuild-in-progress keeps serving the old base.
  defp merge_desc(nil, desc, node_id) do
    %{
      name: desc.name,
      namespace: desc.namespace,
      generation: desc.generation,
      node_id: node_id,
      # class drives whether BuildBase marks the base serving (so noded builds the
      # cold-boot handler artifact, D-R3.11.2). Carried across spec edits below.
      class: Map.get(desc, :class),
      image_ref: desc.image_ref,
      zip: Map.get(desc, :zip),
      guest_port: desc.guest_port,
      ready_path: desc.ready_path,
      vcpus: desc.vcpus,
      mem_mib: desc.mem_mib,
      init_env: desc.init_env || %{},
      built_signature: nil,
      snapshot_ref: nil,
      snapshot_digest: nil,
      superseded_refs: [],
      # R2 refcounting: per-superseded-ref refcount tracking, keyed by snapshot
      # ref. A superseded base is destroyed (via EvictSnapshot) ONLY when zero
      # primed VMs AND zero non-terminal sessions reference it. Both counts start
      # nil (unknown) and are reported by the PoolManager (primed) and the
      # SessionStore (sessions); eviction fires only once BOTH are known-and-zero,
      # so a base is never evicted while a session still rides its birth version
      # (standing decision 5). Multiple superseded bases coexist here, each
      # TTL-bounded by its referencing sessions' maxLifetimeSeconds.
      base_refs: %{},
      backoff_ms: nil,
      retry_timer: nil
    }
  end

  defp merge_desc(prev, desc, node_id) do
    %{
      prev
      | namespace: desc.namespace,
        generation: desc.generation,
        node_id: node_id,
        class: Map.get(desc, :class),
        image_ref: desc.image_ref,
        zip: Map.get(desc, :zip),
        guest_port: desc.guest_port,
        ready_path: desc.ready_path,
        vcpus: desc.vcpus,
        mem_mib: desc.mem_mib,
        init_env: desc.init_env || %{}
    }
  end

  # The base signature: the spec fields that actually shape the base. A rebuild
  # is triggered iff this differs from the signature the recorded base was built
  # from. Deliberately excludes concurrency/invocation/retry (they do not change
  # the base VM), so a cap-only edit never rebuilds.
  defp signature(w) do
    # The zip archive sha256 (nil for the image lane) is part of the signature so
    # a new zip under the same workload name rebuilds the base: every other field
    # can be identical, only the sha256 changes, and the no-gap turnover property
    # requires that to rebuild. The runtime is included too, so switching runtime
    # (a different base image) also rebuilds even if the archive is unchanged.
    zip_sig = if w.zip, do: {w.zip.runtime, w.zip.sha256}, else: nil
    {w.image_ref, zip_sig, w.vcpus, w.mem_mib, w.guest_port, w.ready_path, w.init_env}
  end

  # A zip workload whose runtime does not resolve to a pinned image ref: unknown
  # runtime or no runtime image configured. The image lane always resolves (nil
  # zip), so this is false for it.
  defp zip_runtime_unresolved?(state, %{zip: %{runtime: runtime}}) do
    not is_binary(Map.get(state.runtime_images, runtime))
  end

  defp zip_runtime_unresolved?(_state, _w), do: false

  defp zip_runtime_error(state, %{zip: %{runtime: runtime}}) do
    if map_size(state.runtime_images) == 0 do
      "zip runtime #{inspect(runtime)} cannot be resolved: no runtime image is configured in this release"
    else
      "zip runtime #{inspect(runtime)} does not resolve to a pinned runtime image"
    end
  end

  # Is a build for this signature already queued or in flight on the node? Avoids
  # enqueuing a duplicate while an identical build is pending.
  defp already_targeting?(state, node_id, name, sig) do
    n = state.nodes[node_id]
    building_this = n.building == name and worker_signature(state, node_id) == sig
    queued = name in n.queue
    building_this or queued
  end

  defp worker_signature(state, node_id) do
    case state.nodes[node_id].worker do
      {pid, _ref} -> get_in(state.workers, [pid, :signature])
      nil -> nil
    end
  end

  defp enqueue(state, node_id, name) do
    update_in(state.nodes[node_id].queue, fn q -> if name in q, do: q, else: q ++ [name] end)
  end

  # -- forget ------------------------------------------------------------------

  defp forget_workload(state, name) do
    case Map.get(state.workloads, name) do
      nil ->
        state

      w ->
        cancel_timer(w.retry_timer)

        state
        |> update_in([:workloads], &Map.delete(&1, name))
        |> dequeue_everywhere(name)
    end
  end

  defp dequeue_everywhere(state, name) do
    update_in(state.nodes, fn nodes ->
      for {id, n} <- nodes, into: %{}, do: {id, %{n | queue: List.delete(n.queue, name)}}
    end)
  end

  # -- serial build execution --------------------------------------------------

  # If the node is idle and its queue is non-empty, pop the next Workload and
  # start its build. Skips (and drops) any queued name whose Workload was
  # forgotten while it waited.
  defp maybe_start_build(state, node_id) do
    n = state.nodes[node_id]

    cond do
      n.building != nil ->
        state

      n.queue == [] ->
        state

      true ->
        [name | rest] = n.queue
        state = put_in(state.nodes[node_id].queue, rest)

        case Map.get(state.workloads, name) do
          nil -> maybe_start_build(state, node_id)
          w -> start_worker(state, node_id, w)
        end
    end
  end

  # Spawn the monitored worker that owns the blocking BuildBase call for one
  # Workload. spawn_monitor (not link): a worker crash surfaces as a :DOWN we
  # treat as a build error, never escalating to this GenServer.
  defp start_worker(state, node_id, w) do
    owner = self()
    address = state.node_addr[node_id]
    build_fun = state.build_fun
    connect_fun = state.connect_fun
    disconnect_fun = state.disconnect_fun
    request = build_request(w, state.runtime_images)
    sig = signature(w)

    {pid, ref} =
      spawn_monitor(fn ->
        me = self()

        result =
          case connect_fun.(address) do
            {:ok, channel} ->
              try do
                build_fun.(channel, request)
              catch
                kind, reason -> {:error, {kind, reason}}
              after
                disconnect_fun.(channel)
              end

            {:error, reason} ->
              {:error, {:connect, reason}}
          end

        send(owner, {:build_result, me, result})
      end)

    state
    |> put_in([:nodes, node_id, :building], w.name)
    |> put_in([:nodes, node_id, :worker], {pid, ref})
    |> put_in([:workers, pid], %{node_id: node_id, name: w.name, signature: sig})
  end

  # The image lane sets image_ref (proto field 2); the zip lane leaves it empty
  # and sets source.zip (the ZipSource: the resolved runtime image ref, the
  # archive url = codeUri, the archive sha256). reconcile_desc has already gated
  # a zip build on the runtime resolving, so runtime_images has the ref here.
  defp build_request(%{zip: %{} = zip} = w, runtime_images) do
    %BuildBaseRequest{
      trace: %Trace{workload: w.name},
      workload_revision: to_string(w.generation || 0),
      guest_port: w.guest_port || 0,
      ready_path: w.ready_path,
      resources: %ResourceSpec{vcpus: w.vcpus || 0, mem_mib: w.mem_mib || 0},
      init_env: w.init_env,
      # serving marks a serving-class zip base so noded ALSO writes the cold-boot
      # handler artifact (D-R3.11.2): a serving VM cold-boots with a NIC and cannot
      # resume the vsock-only base memory snapshot to get the handler, so it imports
      # it off the artifact drive. Task/session zip bases leave this false and their
      # build path is byte-unchanged. Only the zip lane carries a handler to
      # materialize; the image lane omits the flag entirely.
      serving: Map.get(w, :class) == "serving",
      source:
        {:zip,
         %ZipSource{
           runtime_image_ref: Map.get(runtime_images, zip.runtime),
           archive_url: zip.code_uri,
           archive_sha256: zip.sha256
         }}
    }
  end

  defp build_request(w, _runtime_images) do
    %BuildBaseRequest{
      trace: %Trace{workload: w.name},
      image_ref: w.image_ref,
      workload_revision: to_string(w.generation || 0),
      guest_port: w.guest_port || 0,
      ready_path: w.ready_path,
      resources: %ResourceSpec{vcpus: w.vcpus || 0, mem_mib: w.mem_mib || 0},
      init_env: w.init_env
    }
  end

  # A build finished. Clear the node's building slot and worker index, apply the
  # result to the Workload's state and status (unless it was forgotten or its
  # spec changed under us), then start the next queued build on that node.
  defp finish_build(state, pid, %{node_id: node_id, name: name, signature: built_sig}, result) do
    state =
      state
      |> update_in([:workers], &Map.delete(&1, pid))
      |> put_in([:nodes, node_id, :building], nil)
      |> put_in([:nodes, node_id, :worker], nil)

    state =
      case Map.get(state.workloads, name) do
        nil ->
          # Forgotten while building: discard the result.
          state

        w ->
          if signature(w) == built_sig do
            apply_result(state, w, built_sig, result)
          else
            # Spec changed under us: this result is for a stale signature. Discard
            # it and re-enqueue the current desired build.
            state
            |> enqueue(node_id, name)
            |> write_base_status(w, :building)
          end
      end

    maybe_start_build(state, node_id)
  end

  # A successful build: record the new base, advance status.snapshotRef (the
  # first time the acceptance property's "always restorable" ref is set/moved),
  # push any superseded ref onto the turnover list (Task 11 seam), and reset
  # backoff.
  defp apply_result(state, w, built_sig, {:ok, %BuildBaseResponse{} = resp}) do
    # Strict boolean (not `&&`): a first build has w.snapshot_ref == nil, and
    # `nil && _` returns nil, which the strict `and` on the base_refs guard below
    # rejects with BadBooleanError. `!=` always yields a boolean.
    turned_over? = w.snapshot_ref != nil and w.snapshot_ref != resp.snapshot_ref

    superseded =
      if turned_over?, do: [w.snapshot_ref | w.superseded_refs], else: w.superseded_refs

    # On turnover, start refcounting the freshly-superseded base. Counts begin
    # unknown (nil); eviction is withheld until the PoolManager and SessionStore
    # report both as zero (report_base_refs/3). A ref already tracked keeps its
    # reported counts.
    base_refs =
      if turned_over? and not Map.has_key?(w.base_refs, w.snapshot_ref) do
        Map.put(w.base_refs, w.snapshot_ref, %{
          node_id: w.node_id,
          primed: nil,
          sessions: nil,
          evicted: false
        })
      else
        w.base_refs
      end

    w = %{
      w
      | built_signature: built_sig,
        snapshot_ref: resp.snapshot_ref,
        snapshot_digest: resp.image_digest,
        superseded_refs: superseded,
        base_refs: base_refs,
        backoff_ms: nil,
        retry_timer: nil
    }

    state = put_in(state.workloads[w.name], w)

    # Base-durability PR-1: drive the durability floor. Export the freshly-built
    # current base to the object store on the node that built it. Fire-and-forget
    # in a spawned worker (the RPC returns a fast ack and the node uploads
    # asynchronously); the periodic export reconcile is the backstop if this
    # result is lost or the store was down. Idempotent per checksum, so a redundant
    # export (e.g. an already_built no-op build that reports the same ref) is a
    # cheap skipped no-op on the node.
    state = spawn_export(state, w.node_id, w.name, w.snapshot_ref)

    write_base_status(state, w, :built)
  end

  # -- base export + durability (base-durability PR-1) ------------------------

  # Periodic reconcile: for every workload whose CURRENT base (snapshot_ref) is
  # present on its node but reported not-yet-exported, re-issue ExportArtifact.
  # This is the self-healing backstop to the immediate post-build export: it
  # recovers a lost export result, a CP restart between build and export, or a
  # store that was down at build time. Deliberately bounded to
  # current-base-present-but-unexported (never the superseded refs, which PR-1
  # must not ship into the store), so it cannot hammer.
  defp export_reconcile(state) do
    Enum.reduce(state.workloads, state, fn {_name, w}, acc ->
      if current_base_present_but_unexported?(acc, w) do
        spawn_export(acc, w.node_id, w.name, w.snapshot_ref)
      else
        acc
      end
    end)
  end

  # A workload's current base needs (re-)export when it has a built ref placed on
  # a known node AND the node's reported fact for that same ref is a READY base
  # that is not yet exported. Matching the ref guards against exporting during a
  # turnover (the node may still report the old ref); requiring READY guards
  # against exporting a BUILDING/FAILED entry; requiring the node fact at all
  # means a base whose node has not yet reported is left for the next sweep
  # (never blindly re-exported without evidence it is present).
  defp current_base_present_but_unexported?(state, w) do
    is_binary(w.node_id) and is_binary(w.snapshot_ref) and
      case node_base_fact(state, w.node_id, w.name) do
        {:ok, %{snapshot_ref: ref, base_state: base_state, exported: exported}} ->
          ref == w.snapshot_ref and
            base_state == :BASE_BUILD_STATE_READY and exported != true

        _ ->
          false
      end
  end

  # Look up one node's reported per-workload base fact (snapshot_ref, base_state,
  # exported) from the shared capacity table. Returns :error when the node has no
  # fact for the workload (unreported, or the base is not present there).
  defp node_base_fact(state, node_id, workload) do
    case find_capacity_fact(state.capacity_table, node_id) do
      {:ok, fact} ->
        case get_in(fact, [:workloads, workload]) do
          nil -> :error
          wl -> {:ok, wl}
        end

      :error ->
        :error
    end
  end

  # -- reconciled base-retention sweep (base-durability PR-3) ------------------

  # Run one reconciled base-retention sweep and apply it: with the destructive gate
  # OFF, log the dry-run plan and change nothing; with the gate ON, spawn an
  # EvictArtifact{remote: false} for each superseded local base outside the desired
  # set. Returns the (possibly updated) state. This is the reconciled backstop the
  # event-driven refcount path lacks: it enumerates from the node's REPORTED local
  # inventory (NodeStatus.local_bases), so it prunes even the pre-R2 orphans the CP
  # never tracked in base_refs and the superseded refs whose refcount event fired
  # and no-op'd before this fix, which the event path alone can never re-fire.
  defp retention_sweep(state) do
    {_plan, state} = retention_sweep_plan(state)
    state
  end

  # Compute the sweep plan (a per-workload list of what to evict) AND apply the
  # gated action. The plan is returned for tests/visibility; the action is the
  # spawned evictions (gate on) or the dry-run log (gate off).
  #
  # For each workload with a placed, exported current base:
  #   candidates = local READY bases the node reports (local_bases, READY only)
  #                MINUS the desired set (current ref + still-refcounted superseded
  #                refs). BUILDING/FAILED local bases are never candidates (a
  #                BUILDING base is being written; noded also refuses it).
  # Durability-before-eviction: a workload whose CURRENT base is not yet exported
  # is SKIPPED WHOLE (never empty the local cache before the S3 floor lands). noded's
  # in-use/current/BUILDING guard is the final backstop beneath this.
  defp retention_sweep_plan(state) do
    Enum.reduce(state.workloads, {[], state}, fn {name, w}, {plan, acc} ->
      case workload_retention(acc, w) do
        :skip ->
          {plan, acc}

        {:skip_unexported, node_id} ->
          entry = %{workload: name, evict_refs: [], evict_bytes: 0, skipped_unexported: true, node_id: node_id}
          {[entry | plan], acc}

        {:evict, node_id, refs, bytes} ->
          entry = %{workload: name, evict_refs: refs, evict_bytes: bytes, skipped_unexported: false, node_id: node_id}
          acc = apply_retention(acc, name, node_id, refs, bytes)
          {[entry | plan], acc}
      end
    end)
    |> then(fn {plan, acc} -> {Enum.reverse(plan), acc} end)
  end

  # Decide one workload's retention outcome from the node's reported facts:
  #   :skip                          - not placed, no node fact, or nothing to evict
  #   {:skip_unexported, node_id}    - current base present but not yet exported
  #   {:evict, node_id, refs, bytes} - superseded local bases to evict + their bytes
  defp workload_retention(state, w) do
    with true <- is_binary(w.node_id) and is_binary(w.snapshot_ref),
         {:ok, fact} <- find_capacity_fact(state.capacity_table, w.node_id),
         locals when is_list(locals) <- Map.get(fact, :local_bases, []) do
      current_exported? =
        case get_in(fact, [:workloads, w.name]) do
          %{snapshot_ref: ref, exported: exported} -> ref == w.snapshot_ref and exported == true
          _ -> false
        end

      cond do
        not current_exported? ->
          # Durability floor not yet confirmed for this workload: skip it whole.
          {:skip_unexported, w.node_id}

        true ->
          desired = desired_refs(w)

          # A candidate is any local base for this workload that is NOT desired and
          # NOT currently BUILDING. READY superseded versions AND unregistered/.tmp
          # orphan dirs (base_state UNSPECIFIED) are both candidates, so the reclaim
          # drains the orphans too; a BUILDING ref is never a candidate (it is being
          # written, and noded refuses it as the final backstop). The current ref is
          # excluded via `desired`, and durability-before-eviction gated this whole
          # workload above.
          candidates =
            for b <- locals,
                b.workload == w.name,
                b.base_state != :BASE_BUILD_STATE_BUILDING,
                b.ref not in desired,
                do: b

          if candidates == [] do
            :skip
          else
            refs = Enum.map(candidates, & &1.ref)
            bytes = candidates |> Enum.map(&(&1.size_bytes || 0)) |> Enum.sum()
            {:evict, w.node_id, refs, bytes}
          end
      end
    else
      _ -> :skip
    end
  end

  # The desired base refs for a workload: its CURRENT ref, PLUS every superseded
  # ref the CP still refcounts (a base_refs entry that is not yet evicted, so a
  # primed VM or pinned session may still ride it). A ref whose refcounts drained
  # and fired an eviction (evicted: true) is NOT desired: it is a candidate. This
  # keeps the sweep from ever removing a base a live VM or banked session still
  # needs, independent of noded's own in-use guard.
  defp desired_refs(w) do
    superseded_desired =
      for {ref, entry} <- w.base_refs, not Map.get(entry, :evicted, false), do: ref

    [w.snapshot_ref | superseded_desired]
  end

  # Apply the gated retention action for one workload. Gate OFF: log the dry-run
  # (count + total bytes) and change nothing. Gate ON: spawn an idempotent
  # EvictArtifact{remote: false} per candidate ref (noded's guard is the final
  # backstop) and log the reclaim.
  defp apply_retention(state, workload, node_id, refs, bytes) do
    if state.retention_sweep_enabled do
      Logger.info(
        "embervm base builder: base-retention sweep evicting #{length(refs)} superseded local base(s) for #{workload} (~#{bytes} bytes) on #{node_id}"
      )

      Enum.each(refs, fn ref -> spawn_evict(state, node_id, workload, ref) end)
      state
    else
      Logger.info(
        "embervm base builder: base-retention sweep (DRY RUN, gate off) WOULD evict #{length(refs)} superseded local base(s) for #{workload} (~#{bytes} bytes) on #{node_id}"
      )

      state
    end
  end

  defp schedule_retention_sweep(%{retention_sweep_interval_ms: ms}) when ms > 0 do
    Process.send_after(self(), :retention_sweep, ms)
    :ok
  end

  defp schedule_retention_sweep(_state), do: :ok

  # Spawn a fire-and-forget ExportArtifact worker for one base ref on its node,
  # unless an export for the same {workload, ref} is already in flight. Returns the
  # (possibly updated) state with the in-flight key marked.
  #
  # In-flight tracking (fast-durability-export fix): the ExportArtifact RPC now
  # returns a FAST ACK (milliseconds) and the node uploads a large base
  # asynchronously (minutes for a 3GB memfile). This CP-side {workload, ref} set does
  # NOT prevent duplicate UPLOADS: it clears on the fast ack, long before the async
  # upload finishes, so it cannot be what serializes the multi-minute write. noded's
  # own exportDedupe (keyed on the store prefix, held from enqueue THROUGH
  # store.Export completion) is the real guard that prevents concurrent uploads of the
  # same ref. What this set does is trim reconcile RPC CHURN: without it the 60s sweep
  # would fire a fresh ExportArtifact RPC (each opening a gRPC channel) for a ref whose
  # prior fast-ack call has already returned, every sweep, while the node is still
  # uploading. A ref already tracked is skipped; the worker signals :export_done on
  # completion (success OR failure) so the key clears, and clearing on failure too is
  # what keeps a failed export re-issuable by the next reconcile rather than skipped
  # forever.
  #
  # The :export_done signal is sent from a top-level `after`, so it fires on EVERY exit
  # path, including a raise in connect_fun or disconnect_fun (which are outside the
  # inner catch). Without that, a worker that crashed before signalling would leave the
  # key stuck in exports_in_flight forever, permanently skipping that ref's re-export
  # and defeating the reconcile backstop (a silent durability gap).
  #
  # ExportArtifact is idempotent per checksum (an already-exported base is a skipped
  # no-op), so a lost result or a retry is harmless; failures are logged, not retried
  # here (the periodic reconcile is the backstop). Not monitored: an export result
  # never mutates BaseBuilder state (durability lives on the node's exported flag,
  # read back on the next status). The op-log audit entry is written from the worker
  # only on a successful ack so the log records a real export request, never a mere
  # crash (see record_base_exported for the request-vs-durable-completion nuance).
  defp spawn_export(state, node_id, workload, ref) do
    key = {workload, ref}

    if MapSet.member?(state.exports_in_flight, key) do
      # An export RPC for this exact ref is already in flight; skip a redundant one.
      state
    else
      owner = self()
      address = state.node_addr[node_id]
      connect_fun = state.connect_fun
      disconnect_fun = state.disconnect_fun
      export_fun = state.export_fun
      op_log = state.op_log
      op_log_mod = state.op_log_mod
      tenant = state.tenant
      clock = state.clock

      spawn(fn ->
        try do
          result =
            case connect_fun.(address) do
              {:ok, channel} ->
                try do
                  export_fun.(channel, %ExportArtifactRequest{
                    artifact: %ArtifactRef{
                      kind: :ARTIFACT_KIND_BASE,
                      workload: workload,
                      ref: ref
                    },
                    trace: %Trace{workload: workload}
                  })
                catch
                  kind, reason -> {:error, {kind, reason}}
                after
                  disconnect_fun.(channel)
                end

              {:error, reason} ->
                {:error, {:connect, reason}}
            end

          case result do
            {:ok, resp} ->
              record_base_exported(op_log_mod, op_log, tenant, clock, workload, ref, resp)

            other ->
              Logger.warning(
                "embervm base builder: ExportArtifact #{workload}/#{ref} failed: #{inspect(other)}"
              )
          end
        after
          # Always clear the in-flight key: on success, on a logged failure, and on any
          # raise from connect_fun/disconnect_fun that skips the case above. A stuck key
          # would permanently skip this ref's re-export.
          send(owner, {:export_done, key})
        end
      end)

      %{state | exports_in_flight: MapSet.put(state.exports_in_flight, key)}
    end
  end

  # Append the audit-only :artifact_exported op (no projection table; the log
  # itself is the record), mirroring the restore-audit recorders in the session/
  # serving/stateful managers. Best-effort: an append failure must never crash
  # the export worker (the durable fact is the exported bytes, not this row).
  #
  # For a BASE this now fires on the FAST ACK, i.e. it records the export REQUEST, not
  # the durable landing: the async upload completes minutes later, so this ts is eager
  # by the upload duration. Do NOT read this op-log entry as a durability timestamp;
  # the authoritative durability signal is the `exported` flag on WorkloadCapacity
  # (which flips only after the node's upload lands). bytes_moved is 0 on the fast ack.
  defp record_base_exported(op_log_mod, op_log, tenant, clock, workload, ref, resp) do
    op = %Embervm.OpLog.Op{
      kind: :artifact_exported,
      tenant: tenant,
      principal: "system:base:#{workload}",
      workload: workload,
      ts: clock.(),
      payload: %{
        kind: "base",
        ref: ref,
        bytes_moved: Map.get(resp, :bytes_moved, 0),
        generation: Map.get(resp, :generation, 0),
        skipped: Map.get(resp, :skipped, false)
      }
    }

    _ = op_log_mod.append(op_log, op)
    :ok
  rescue
    e ->
      Logger.warning("embervm base builder: artifact_exported append raised",
        workload: workload,
        error: inspect(e)
      )

      :ok
  end

  defp schedule_export_reconcile(%{export_reconcile_interval_ms: ms}) when ms > 0 do
    Process.send_after(self(), :export_reconcile, ms)
    :ok
  end

  defp schedule_export_reconcile(_state), do: :ok

  # -- base refcounting + eviction (R2) ---------------------------------------

  # Update the reported refcounts for one superseded base ref and, if it is now
  # fully drained (zero primed AND zero sessions, both known), evict it. Finds
  # the workload that recorded the ref; a ref we do not track (never superseded,
  # or already evicted) is a no-op. This is the ONLY place a superseded base is
  # destroyed, and only via the EvictSnapshot verb.
  defp apply_base_refs(state, ref, counts) do
    case workload_for_ref(state, ref) do
      nil ->
        state

      name ->
        w = state.workloads[name]
        entry = Map.get(w.base_refs, ref)
        updated = merge_refcounts(entry, counts)
        w = put_in(w.base_refs[ref], updated)
        state = put_in(state.workloads[name], w)
        maybe_evict_base(state, name, ref)
    end
  end

  defp workload_for_ref(state, ref) do
    Enum.find_value(state.workloads, fn {name, w} ->
      if not Map.get(w.base_refs[ref] || %{}, :evicted, true) and Map.has_key?(w.base_refs, ref),
        do: name,
        else: nil
    end)
  end

  defp merge_refcounts(entry, counts) do
    entry
    |> maybe_put_count(:primed, Keyword.get(counts, :primed))
    |> maybe_put_count(:sessions, Keyword.get(counts, :sessions))
    # R3 (D-R3.3.1, RESOLVED in Task 9): this :serving key is DELIBERATELY INERT.
    # The investigation in Task 9 established that serving does NOT participate in
    # base-refcounting at all: a serving instance cold-boots from a rootfs IMAGE
    # (D-R3.4.2), never restores a BuildBase base snapshot, and once banked rides its
    # own per-instance serving snapshot, so base eviction can never remove anything a
    # live serving instance needs. Nothing reports a :serving count, and evictable?/1
    # below is correctly NOT widened to require one. This key is kept (not removed) so
    # PR-1's accepted counts contract is not churned; it stays a no-op unless a future
    # rung ever gives serving a shared evictable base lineage. See D-R3.3.1.
    |> maybe_put_count(:serving, Keyword.get(counts, :serving))
  end

  defp maybe_put_count(entry, _key, nil), do: entry
  defp maybe_put_count(entry, key, value) when is_integer(value), do: Map.put(entry, key, value)

  # A superseded base is evictable only when BOTH refcounts are known and zero.
  # A nil (unreported) count is treated as "still referenced": eviction is
  # withheld until every owner has spoken, so a base is never destroyed under a
  # session that has not yet been counted (fail-safe).
  #
  # R3 (D-R3.3.1, RESOLVED in Task 9): correctly keyed on primed/sessions only, NOT
  # serving. Serving does NOT participate in base-refcounting: a serving instance
  # cold-boots from a rootfs IMAGE (D-R3.4.2), never restores a BuildBase base
  # snapshot, and rides its own per-instance serving snapshot once banked, so base
  # eviction can never remove anything it needs. There is nothing to report and no
  # serving: 0 term to add: widening this to require serving: 0 with no reporter would
  # make it require serving: 0 forever (nil never equals 0), silently wedging base
  # eviction for EVERY workload class. Not-widening is both the correct model and the
  # only safe move. The :serving key in merge_refcounts/2 above stays deliberately
  # inert. See D-R3.3.1.
  defp evictable?(%{primed: 0, sessions: 0, evicted: false}), do: true
  defp evictable?(_), do: false

  defp maybe_evict_base(state, name, ref) do
    w = state.workloads[name]
    entry = w.base_refs[ref]

    if evictable?(entry) do
      # Mark evicted synchronously (so a second report cannot double-evict), drop
      # the ref from the turnover list, and fire EvictSnapshot in a spawned worker
      # so the blocking RPC never freezes this GenServer.
      w = %{
        w
        | base_refs: Map.put(w.base_refs, ref, %{entry | evicted: true}),
          superseded_refs: List.delete(w.superseded_refs, ref)
      }

      state = put_in(state.workloads[name], w)
      spawn_evict(state, entry.node_id, name, ref)
      state
    else
      state
    end
  end

  # Spawn a fire-and-forget eviction worker. EvictArtifact{remote: false} is
  # idempotent (an already-gone base is success), so a lost result or a retry is
  # harmless; failures are logged, not retried here (the next report, or the
  # reconciled retention sweep, is the backstop). Not monitored: a crash cannot
  # un-evict the already-marked ref.
  defp spawn_evict(state, node_id, workload, ref) do
    address = state.node_addr[node_id]
    connect_fun = state.connect_fun
    disconnect_fun = state.disconnect_fun
    evict_fun = state.evict_fun

    spawn(fn ->
      result =
        case connect_fun.(address) do
          {:ok, channel} ->
            try do
              evict_fun.(channel, workload, ref)
            catch
              kind, reason -> {:error, {kind, reason}}
            after
              disconnect_fun.(channel)
            end

          {:error, reason} ->
            {:error, {:connect, reason}}
        end

      case result do
        {:ok, _} ->
          :ok

        other ->
          Logger.warning(
            "embervm base builder: EvictArtifact #{workload}/#{ref} failed: #{inspect(other)}"
          )
      end
    end)

    :ok
  end

  # A failed build: keep any existing base (Ready stays True if one is recorded),
  # report the daemon error, and schedule a backed-off retry.
  defp apply_result(state, w, _built_sig, {:error, reason}) do
    message = format_build_error(reason)

    Logger.warning("embervm base builder: BuildBase for #{w.namespace}/#{w.name} failed: #{message}")

    next_backoff = min((w.backoff_ms || state.base_backoff_ms) * backoff_factor(w), state.max_backoff_ms)
    timer = Process.send_after(self(), {:retry, w.name}, next_backoff)

    w = %{w | backoff_ms: next_backoff, retry_timer: timer}
    state = put_in(state.workloads[w.name], w)
    write_base_status(state, w, {:failed, message})
  end

  # Backoff doubles from the base on each consecutive failure. The first failure
  # (backoff_ms nil) starts at the base; thereafter it doubles the last value.
  defp backoff_factor(%{backoff_ms: nil}), do: 1
  defp backoff_factor(_), do: 2

  defp retry_workload(state, name) do
    case Map.get(state.workloads, name) do
      nil ->
        state

      w ->
        w = %{w | retry_timer: nil}
        state = put_in(state.workloads[name], w)

        cond do
          w.built_signature == signature(w) and w.snapshot_ref != nil ->
            # The desired base got built by some other path meanwhile; nothing to do.
            state

          already_targeting?(state, w.node_id, name, signature(w)) ->
            # A build for the current signature is already queued or in flight
            # (a reconcile beat this stale timer message to it, or the timer
            # double-fired). Re-driving would enqueue a redundant heavy build,
            # so no-op. Same guard reconcile_desc uses, mirroring the streamer
            # guard in Embervm.NodeRegistry's reconnect handler.
            state

          true ->
            state
            |> enqueue(w.node_id, name)
            |> write_base_status(w, :building)
            |> maybe_start_build(w.node_id)
        end
    end
  end

  # -- status writing ----------------------------------------------------------

  # Build and patch the two conditions this module owns. Ready is derived purely
  # from whether a restorable base exists (snapshot_ref present); BaseBuilt
  # tracks the DESIRED base. Only a :built result writes snapshotRef/Digest, so a
  # build-in-progress or failure never clears an existing (still restorable) ref.
  defp write_base_status(state, w, phase) do
    ready = ready_condition(state, w)
    base_built = base_built_condition(state, phase)

    status_map =
      %{"conditions" => [ready, base_built]}
      |> maybe_put_snapshot(w, phase)

    case state.status_writer.(w.namespace, w.name, status_map) do
      :ok ->
        :ok

      {:error, reason} ->
        # Visibility-only: a status-write failure must not crash the loop or lose
        # the build state (already recorded above); log and swallow.
        Logger.warning(
          "embervm base builder: status patch failed for #{w.namespace}/#{w.name}: #{inspect(reason)}"
        )
    end

    state
  end

  defp maybe_put_snapshot(status_map, w, :built) do
    status_map
    |> Map.put("snapshotRef", w.snapshot_ref || "")
    |> Map.put("snapshotDigest", w.snapshot_digest || "")
  end

  defp maybe_put_snapshot(status_map, _w, _phase), do: status_map

  defp ready_condition(state, w) do
    if w.snapshot_ref do
      condition(state, "Ready", "True", "BaseReady", "a restorable base snapshot is available")
    else
      condition(state, "Ready", "False", "BaseNotBuilt", "base snapshot not built yet")
    end
  end

  defp base_built_condition(state, :building) do
    condition(state, "BaseBuilt", "False", "BaseBuilding", "base build in progress")
  end

  defp base_built_condition(state, :built) do
    condition(state, "BaseBuilt", "True", "BaseBuilt", "base snapshot built and recorded")
  end

  defp base_built_condition(state, {:failed, message}) do
    condition(state, "BaseBuilt", "False", "BuildFailed", message)
  end

  defp base_built_condition(state, {:pending, :no_node}) do
    condition(state, "BaseBuilt", "Unknown", "NoNodeAvailable", "no node daemon is configured to build the base")
  end

  defp condition(state, type, status, reason, message) do
    %{
      "type" => type,
      "status" => status,
      "reason" => reason,
      "message" => message,
      "lastTransitionTime" => iso8601(state.clock.())
    }
  end

  defp iso8601(ms) do
    ms
    |> DateTime.from_unix!(:millisecond)
    |> DateTime.to_iso8601()
  end

  defp cancel_timer(nil), do: :ok
  defp cancel_timer(ref), do: Process.cancel_timer(ref)

  # -- default (production) seams ---------------------------------------------

  # Plaintext h2c to the noded Service over the Mint adapter, opened per build
  # (builds are infrequent and serialized). Same pattern as Embervm.NodeRegistry.
  defp default_connect(address) do
    GRPC.Stub.connect(address, adapter: GRPC.Client.Adapters.Mint)
  end

  defp default_disconnect(channel) do
    _ = GRPC.Stub.disconnect(channel)
    :ok
  end

  # BuildBase legitimately runs for MINUTES (bazel warming + settle, the k3s
  # airgap image import, cold-boot + WaitReady + snapshot), but elixir-grpc's Mint
  # adapter defaults to a 10s per-call timeout (config :grpc, GRPC.Client.Adapters.Mint,
  # timeout: 10_000). With no :timeout option the stub sent grpc-timeout "10S" on
  # the HTTP/2 stream, so noded's gRPC server enforced the deadline at exactly
  # boot+10s and SIGKILLed the warming VM (the bazel-query base is 6s warming + 10s
  # settle, the first base to cross 10s; python/postgres/semgrep all went ready
  # under 10s and never hit it). Worse, the Mint adapter then crashed on the cancel
  # frame, swallowing the error so BaseBuilder never logged a failure and the
  # Workload wedged in BaseBuilding. An explicit generous per-call timeout (10 min
  # in ms) covers any realistic base build; BuildBase's own BootReadyTimeout is the
  # real inner bound. This is the SLOW-path build only; the hot-path Prime/Assign
  # calls keep the short default deliberately.
  defp default_build(channel, %BuildBaseRequest{} = request) do
    NodeService.Stub.build_base(channel, request, timeout: 600_000)
  end

  # EvictArtifact{remote: false} the superseded base ref (kind BASE): remove the
  # LOCAL bases/<ref> dir on the node, leaving any store copy untouched. The
  # ArtifactRef carries the workload so the node composes the vendor-keyed base
  # store prefix; the node stamps its own cpu vendor. Idempotent on an
  # already-gone base.
  defp default_evict(channel, workload, ref) do
    NodeService.Stub.evict_artifact(channel, %EvictArtifactRequest{
      artifact: %ArtifactRef{
        kind: :ARTIFACT_KIND_BASE,
        workload: workload,
        ref: ref
      },
      remote: false,
      trace: %Trace{workload: workload}
    })
  end

  # ExportArtifact one base ref to the object store (base-durability PR-1). The
  # node stamps its own cpu vendor into the store key and meta.json, so the
  # request carries only the ref (unlike restore, which the CP vendor-stamps).
  # A generous timeout: a first-time export moves the whole base (up to a few GB)
  # to SeaweedFS; a re-export is a checksum-compare skip and returns fast.
  defp default_export(channel, %ExportArtifactRequest{} = request) do
    NodeService.Stub.export_artifact(channel, request, timeout: 600_000)
  end

  # RestoreArtifact one base ref from the object store (base-durability PR-2). noded
  # FAST-ACKs a BASE restore (accepted=true) and downloads async, so this is NOT a
  # long-held call: it returns in milliseconds with the ack (or a fast
  # FAILED_PRECONDITION on an S3 miss). The short default per-call timeout is
  # therefore correct here (unlike the slow BuildBase path); the multi-minute
  # download runs on the node's own restore queue, and the CP polls NodeStatus for
  # READY rather than holding this RPC open (the whole reason the download went
  # async, mirroring the export idle-close lesson).
  defp default_restore(channel, %RestoreArtifactRequest{} = request) do
    NodeService.Stub.restore_artifact(channel, request)
  end

  # A gRPC-level failure (e.g. FAILED_PRECONDITION for an image the daemon does
  # not know, which is every image in R0 until guest-image provisioning lands)
  # arrives as a GRPC.RPCError; surface its message verbatim in the condition.
  defp format_build_error(%GRPC.RPCError{message: message}), do: message
  defp format_build_error(reason), do: inspect(reason)
end
