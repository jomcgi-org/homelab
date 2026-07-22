defmodule Embervm.NodeRegistry do
  @moduledoc """
  The control plane's single source of node truth. Holds one supervised gRPC
  connection per configured node daemon (`embervm-noded`), consumes each
  daemon's server-streamed `WatchNode` heartbeat, and projects the `NodeStatus`
  facts into the `Embervm.NodeCapacity` ETS table the dispatcher (Task 11) reads
  O(1).

  The daemon is the health authority: it reports its own capacity (free primed
  slots per workload, base build state, memory/cpu headroom, live/max VMs,
  draining). This module never second-guesses those numbers; its whole job is to
  keep a fresh copy of them and to decide, from stream liveness alone, whether a
  node is still trustworthy.

  ## consuming a blocking server-stream (the watcher discipline)

  `NodeService.Stub.watch_node/2` returns an Enumerable whose iteration BLOCKS
  for the stream's multi-second-to-minutes lifetime. Running that inside this
  GenServer would freeze every capacity read behind it, so, exactly as
  `Embervm.WorkloadWatcher` does for the K8s watch, each node's stream runs in a
  `spawn_monitor`'d streamer process that forwards every `NodeStatus` back as a
  message tagged with its own pid. The GenServer keeps sole ownership of the ETS
  table, the per-node health state, and all reconnect decisions (a serialized,
  crash-isolated state machine). A streamer crash surfaces as a `:DOWN` handled
  like any other disconnect; it never takes the registry down. Events from a
  superseded streamer (a reconnect race) are dropped so stale facts can never
  overwrite fresh ones.

  ## age-out is a TIMER, not the stream

  The spec requires a node to age to `unknown` after 5s and `down` after 15s of
  silence, and this must hold even when the stream SILENTLY WEDGES (the TCP
  connection stays open but no `NodeStatus` arrives), which a blocking consumer
  cannot observe on its own. So age-out is driven by a periodic tick in this
  GenServer against a per-node `last_status_at` timestamp, entirely decoupled
  from stream liveness:

    * every `NodeStatus` stamps `last_status_at` and marks the node `:healthy`;
    * a ~1s tick recomputes each node's health from `now - last_status_at`;
    * crossing into `:down` fires the reassignment path ONCE and tears the
      (possibly wedged) streamer down so a fresh connection is attempted, which
      is the only way to recover from a silent wedge (a wedged stream never ends
      on its own).

  ## fail-closed

  A node's capacity facts are written to `Embervm.NodeCapacity` ONLY while it is
  `:healthy` and its daemon reports `draining: false`. Any degraded state
  (`:starting` before the first status, `:unknown`, `:down`) and any `draining`
  status immediately deletes the node's row. The `draining` flag therefore stops
  new assignments the instant it is observed (the daemon's own drain grace still
  lets in-flight Assigns finish; only a hard `:down` reassigns them). "No
  capacity facts means no dispatch" is enforced by the table being empty, not by
  the dispatcher checking a flag.

  ## dial-home registration, keyed by INSTANCE (R0 PR-2)

  The control plane no longer DISCOVERS daemons (the retired EndpointSlice poll):
  each noded instance DIALS HOME, POSTing `{node, pod_uid, address, boot_id}` to
  the control plane's `/v1/nodes/register` route, which forwards it here as
  `register/2`. Registration upserts an instance keyed by `{node, pod_uid}`
  (a new instance opens a streamer and joins the NodeChannel/BaseBuilder fleet; a
  changed address re-points; an unchanged one just refreshes the registration
  timestamp). This inverts the ownership: the CP never lists-and-watches daemon
  pods, and two instances on ONE node (a surge roll, ADR embervm/012) are
  simultaneously representable because the key is the pod UID, not the node name.

  An instance ages OUT of the registry only when BOTH its registration has lapsed
  (no re-register within `@expire_after_ms`) AND its WatchNode stream is dead, so
  a CP-side network blip alone never drops a healthy node.

  ## the static seam

  `start_link/1` still takes `:nodes`, a LIST of `%{id, address}` (optionally
  `pod_uid`) specs, for the pinned `EMBERVM_NODE_ADDRESS` single-daemon override
  and for tests. A spec with no `pod_uid` collapses to a node-scoped instance
  (`pod_uid: ""`), matching the pre-dial-home behaviour. Registration adds and
  removes instances on top of that static seed. There is deliberately no
  cross-node placement logic here; that is the dispatcher's job.
  """

  use GenServer
  require Logger

  alias Embervm.NodeCapacity

  alias Embervm.Node.V1.{
    GroupBundleSet,
    GroupMemberVm,
    BaseInventoryEntry,
    GroupNetwork,
    NodeService,
    NodeStatus,
    RegistryEntry,
    ResourceSpec,
    ServingSnapshot,
    ServingVm,
    SessionSnapshot,
    SessionVm,
    StatefulBundle,
    StatefulVm,
    SyncRegistryRequest,
    Volume,
    WatchNodeRequest,
    WorkloadCapacity
  }

  @unknown_after_ms 5_000
  @down_after_ms 15_000
  @age_check_ms 1_000
  # How long an instance may go without re-registering (dial-home) before its
  # registration is considered LAPSED. noded re-registers every ~30s, so 90s is
  # three missed intervals: an instance is expired from the registry only when its
  # registration is lapsed AND its WatchNode stream is dead (both signals), so a
  # CP-side blip alone never drops a healthy node. A statically-seeded instance
  # (EMBERVM_NODE_ADDRESS override / tests) never registers and so never lapses:
  # its last_registered_at stays nil and expiry is skipped for it.
  @expire_after_ms 90_000

  # Closed enum of node health states, the age-out machine's whole codomain
  # (evaluate_node_age computes exactly these). Exposed for the spec vocabulary
  # sync test (ADR embervm/006 layer 1); nothing here reads it, so behavior is
  # unchanged.
  @health_states [:starting, :healthy, :unknown, :down]

  # Reconnect backoff, same shape as Embervm.WorkloadWatcher: a healthy
  # long-lived stream close reconnects immediately (reset to base); a fast or
  # errored close backs off exponentially so a wedged/flapping daemon is not
  # hammered.
  @base_backoff_ms 1_000
  @max_backoff_ms 30_000
  @min_watch_ms 1_000

  # -- Client API ------------------------------------------------------------

  # :name defaults to __MODULE__ for the application's supervised singleton;
  # tests pass name: nil to run several PID-addressed instances concurrently
  # (the same idiom as the other GenServers in this app).
  @spec start_link(keyword()) :: GenServer.on_start()
  def start_link(opts) do
    case Keyword.get(opts, :name, __MODULE__) do
      nil -> GenServer.start_link(__MODULE__, opts)
      name -> GenServer.start_link(__MODULE__, opts, name: name)
    end
  end

  @doc """
  All currently dispatchable nodes' capacity facts (a direct read of the
  `Embervm.NodeCapacity` table). Empty when no node is healthy. This is the read
  Task 11's dispatcher makes; it does not go through this GenServer.
  """
  @spec capacity(atom()) :: [map()]
  def capacity(table \\ NodeCapacity.table()) do
    NodeCapacity.all(table)
  end

  @doc "Closed enum of node health states, exposed for the spec vocabulary sync test (ADR embervm/006 layer 1)."
  @spec health_states() :: [atom()]
  def health_states, do: @health_states

  @doc """
  A snapshot of every configured node's health and last-known facts, for
  operational visibility (a `kubectl exec` IEx query verifies the live daemon
  connection this way) and tests. Unlike `capacity/1` this includes degraded
  nodes, so an operator can see WHY a node is not dispatchable.
  """
  @spec status(GenServer.server()) :: %{String.t() => map()}
  def status(server \\ __MODULE__) do
    GenServer.call(server, :status)
  end

  @doc """
  Applies one `NodeStatus` as if it arrived from the given node's current stream.
  Test/operational seam: production status flows in from the streamer process,
  but this lets a test drive the projection + fail-closed logic synchronously
  without spawning a streamer. Unknown node ids are ignored.
  """
  @spec inject_status(GenServer.server(), String.t(), NodeStatus.t()) :: :ok
  def inject_status(server, node_id, %NodeStatus{} = status) do
    GenServer.call(server, {:inject_status, node_id, status})
  end

  @doc """
  Forces one synchronous age-out evaluation across all nodes (the same code the
  periodic timer runs). Tests drive age-out deterministically through this with
  an injected clock; in production the timer fires it every #{@age_check_ms}ms.
  """
  @spec tick(GenServer.server()) :: :ok
  def tick(server \\ __MODULE__) do
    GenServer.call(server, :tick)
  end

  @doc """
  Applies one dial-home registration from a noded instance. The map carries
  `node` (K8s node name), `pod_uid` (the pod UID, the instance identity),
  `address` (`"pod_ip:grpc_port"`), and optionally `boot_id`. Upserts the
  instance keyed by `{node, pod_uid}`: a new instance seeds runtime + opens a
  streamer + joins the NodeChannel/BaseBuilder fleet; a changed address re-points;
  an unchanged one just refreshes the registration timestamp. Called by the
  router's `/v1/nodes/register` handler; returns `:ok` (registration is
  advertisement, so even a malformed body is a benign no-op the caller reports as
  accepted). An empty `node` is rejected as `{:error, :invalid}`.
  """
  @spec register(GenServer.server(), map()) :: :ok | {:error, :invalid}
  def register(server \\ __MODULE__, %{} = reg) do
    GenServer.call(server, {:register, reg})
  end

  # -- GenServer callbacks ---------------------------------------------------

  @impl true
  def init(opts) do
    table = Keyword.get(opts, :table, NodeCapacity.table())
    nodes = Keyword.get(opts, :nodes, [])
    # Injected seams (defaults are the real gRPC + wall clock + task store):
    connect_fun = Keyword.get(opts, :connect_fun, &default_connect/1)
    watch_fun = Keyword.get(opts, :watch_fun, &default_watch/3)
    disconnect_fun = Keyword.get(opts, :disconnect_fun, &default_disconnect/1)
    # Artifact-decoupling Phase 2: the on-(re)connect registry replay seam. The
    # default reads the control plane's workload view (WorkloadCatalog + the
    # chart-delivered node-side image identity) and pushes SyncRegistry over the
    # just-connected channel. Tests inject a fake to assert the replay fires (and
    # with what entries) without a real daemon or catalog.
    sync_registry_fun = Keyword.get(opts, :sync_registry_fun, &default_sync_registry/2)
    clock = Keyword.get(opts, :clock, &default_clock/0)
    reassign_fun = Keyword.get(opts, :reassign_fun, &default_reassign/1)
    unknown_after = Keyword.get(opts, :unknown_after_ms, @unknown_after_ms)
    down_after = Keyword.get(opts, :down_after_ms, @down_after_ms)
    age_check = Keyword.get(opts, :age_check_ms, @age_check_ms)
    base_backoff = Keyword.get(opts, :base_backoff_ms, @base_backoff_ms)
    max_backoff = Keyword.get(opts, :max_backoff_ms, @max_backoff_ms)
    min_watch = Keyword.get(opts, :min_watch_ms, @min_watch_ms)
    # watch_startup drives the informer from init; tests set it false and drive
    # inject_status/1 + tick/1 explicitly so no background stream or timer fires.
    watch_startup = Keyword.get(opts, :watch_startup, true)
    # How long an instance may go without re-registering (dial-home) before its
    # registration is LAPSED; expiry additionally requires a dead stream.
    expire_after = Keyword.get(opts, :expire_after_ms, @expire_after_ms)
    # How a (re)registered instance's NEW address is propagated to the Prime/Assign
    # hot-path channel holder (Embervm.NodeChannel), whose node_addr map would
    # otherwise keep dialing a rolled pod's dead IP. Keyed by INSTANCE id. Default
    # calls the singleton NodeChannel; tests inject a fake to assert the
    # propagation without a running NodeChannel.
    channel_updater_fun =
      Keyword.get(opts, :channel_updater_fun, &default_channel_update/2)

    # How an expired instance's key is REMOVED from the Prime/Assign channel holder.
    # Post-B0c an instance is added to NodeChannel under its instance_id alone, so
    # expiry drops that one key and no stale endpoint keeps pointing at a torn-down
    # pod's address. Keyed like channel_updater_fun (one call per key). Default calls
    # the singleton NodeChannel; tests inject a fake to assert the removal fires.
    channel_remover_fun =
      Keyword.get(opts, :channel_remover_fun, &default_channel_remove/1)

    # How a registered instance's add/remove is propagated to Embervm.BaseBuilder,
    # which (like NodeChannel) is seeded EMPTY at boot under dial-home and must
    # learn the fleet so BuildBase can PLACE builds. Without this, a control plane
    # would never build any base (every workload holds {:pending, :no_node}).
    # Default calls the singleton BaseBuilder; tests inject a fake.
    base_builder_updater_fun =
      Keyword.get(opts, :base_builder_updater_fun, &default_base_builder_update/1)

    NodeCapacity.create(table)

    now = clock.()

    node_runtime =
      for spec <- nodes, into: %{} do
        instance = seed_runtime(spec, base_backoff, now)
        {instance.instance_id, instance}
      end

    state = %{
      table: table,
      clock: clock,
      connect_fun: connect_fun,
      watch_fun: watch_fun,
      disconnect_fun: disconnect_fun,
      sync_registry_fun: sync_registry_fun,
      reassign_fun: reassign_fun,
      unknown_after_ms: unknown_after,
      down_after_ms: down_after,
      age_check_ms: age_check,
      base_backoff_ms: base_backoff,
      max_backoff_ms: max_backoff,
      min_watch_ms: min_watch,
      expire_after_ms: expire_after,
      channel_updater_fun: channel_updater_fun,
      channel_remover_fun: channel_remover_fun,
      base_builder_updater_fun: base_builder_updater_fun,
      node_runtime: node_runtime,
      # The process notified once per drain rising edge with {:node_draining,
      # node_id, pod_uid, deadline_ms} so it can force-bank the instance's live VMs
      # before the pod exits (R6, ADR embervm/009). A registered name or pid; default the
      # DrainCoordinator. A missing target (tests, or drain during boot) is a silent
      # no-op: the daemon's own deadline reap is the backstop.
      drain_listener: Keyword.get(opts, :drain_listener, Embervm.DrainCoordinator),
      # pid -> node_id for the CURRENT streamer of each node, so an event tagged
      # with a superseded streamer's pid is dropped (never in this map).
      streamers: %{}
    }

    if watch_startup do
      {:ok, state, {:continue, :start}}
    else
      {:ok, state}
    end
  end

  # Open every statically-seeded instance's stream and arm the age-out timer once
  # the process is fully initialized (continue runs after init returns, before any
  # external message). Under dial-home the seed is usually EMPTY (the fleet arrives
  # via register/2 post-Finch); a pinned EMBERVM_NODE_ADDRESS override or a test
  # seed is opened here. No Finch/K8s call happens here or at construction, so the
  # boot-ordering invariant (ADR embervm/012) holds: registration is the only path
  # that dials, and it runs only after the router accepts a POST (well after Finch).
  @impl true
  def handle_continue(:start, state) do
    state =
      Enum.reduce(Map.keys(state.node_runtime), state, fn instance_id, acc ->
        start_streamer(acc, instance_id)
      end)

    schedule_age_check(state)
    {:noreply, state}
  end

  # A NodeStatus from the CURRENT streamer of some node. Events from a superseded
  # streamer (not in state.streamers) are dropped so stale facts cannot overwrite
  # fresh ones or resurrect a node we already tore down.
  @impl true
  def handle_info({:node_status, pid, status}, state) do
    case Map.get(state.streamers, pid) do
      nil -> {:noreply, state}
      node_id -> {:noreply, apply_status(state, node_id, status)}
    end
  end

  # A streamer finished (stream closed cleanly or errored). Decide the next move
  # for its node: reconnect immediately on a healthy long-lived close, back off
  # otherwise. Ignored if the pid is no longer the current streamer.
  def handle_info({:watch_result, pid, result}, state) do
    case Map.get(state.streamers, pid) do
      nil ->
        {:noreply, state}

      node_id ->
        state = forget_streamer(state, pid, node_id)
        {:noreply, handle_watch_end(state, node_id, result)}
    end
  end

  # A streamer died WITHOUT reporting a result (it always sends one in the normal
  # path, so a bare DOWN is an abnormal exit or a deliberate kill from the
  # down-edge wedge recovery). Treat as a watch error and reconnect with backoff.
  # A DOWN from a superseded streamer (not the current one) is expected on
  # supersession and ignored.
  def handle_info({:DOWN, _ref, :process, pid, reason}, state) do
    case Map.get(state.streamers, pid) do
      nil ->
        {:noreply, state}

      node_id ->
        state = forget_streamer(state, pid, node_id)
        {:noreply, handle_watch_end(state, node_id, {:error, {:streamer_down, reason}})}
    end
  end

  # Reconnect timer fired for a node whose stream is currently closed.
  def handle_info({:reconnect, node_id}, state) do
    rt = state.node_runtime[node_id]

    if rt && is_nil(rt.streamer) do
      {:noreply, start_streamer(state, node_id)}
    else
      {:noreply, state}
    end
  end

  # Periodic age-out evaluation, then re-arm the timer.
  def handle_info(:age_check, state) do
    state = evaluate_ages(state)
    schedule_age_check(state)
    {:noreply, state}
  end

  def handle_info(_msg, state), do: {:noreply, state}

  @impl true
  def handle_call(:status, _from, state) do
    snapshot =
      for {instance_id, rt} <- state.node_runtime, into: %{} do
        facts =
          case NodeCapacity.fetch(state.table, instance_key(rt)) do
            {:ok, f} -> f
            :error -> nil
          end

        {instance_id,
         %{
           configured_id: rt.configured_id,
           pod_uid: rt.pod_uid,
           instance_id: rt.instance_id,
           address: rt.address,
           health: rt.health,
           draining: rt.draining,
           dispatchable: not is_nil(facts),
           facts: facts,
           connected: not is_nil(rt.streamer)
         }}
      end

    {:reply, snapshot, state}
  end

  def handle_call({:inject_status, instance_id, status}, _from, state) do
    if Map.has_key?(state.node_runtime, instance_id) do
      {:reply, :ok, apply_status(state, instance_id, status)}
    else
      {:reply, :ok, state}
    end
  end

  def handle_call(:tick, _from, state) do
    {:reply, :ok, evaluate_ages(state)}
  end

  def handle_call({:register, reg}, _from, state) do
    case normalize_registration(reg) do
      {:ok, norm} -> {:reply, :ok, apply_registration(state, norm)}
      :error -> {:reply, {:error, :invalid}, state}
    end
  end

  # Best-effort: stop orphaned streamers from outliving the GenServer holding
  # their gRPC connections open. Not load-bearing (a dead owner's streamers exit
  # when their monitored owner dies anyway), just tidy.
  @impl true
  def terminate(_reason, state) do
    for {pid, _node_id} <- state.streamers, do: Process.exit(pid, :shutdown)
    :ok
  end

  # -- status projection ------------------------------------------------------

  # A fresh NodeStatus: stamp last_status_at, mark healthy, record draining, and
  # publish or retract the node's capacity facts by whether it is dispatchable.
  # Marking healthy here also resets the down-edge, so a node that recovers and
  # later goes down again fires reassignment afresh.
  defp apply_status(state, instance_id, %NodeStatus{} = status) do
    now = state.clock.()
    prev = state.node_runtime[instance_id]
    rt = %{prev | last_status_at: now, health: :healthy, draining: status.draining}
    state = put_in(state.node_runtime[instance_id], rt)
    notify_drain_edge(state, rt, prev, status)
    refresh_capacity(state, instance_id, status)
  end

  # On the RISING edge of draining (false -> true) notify the drain listener exactly
  # once with the instance's published deadline, so it force-banks the instance's
  # live VMs within the bounded-preemption window (R6). Drain scopes to the INSTANCE
  # (node + pod_uid): a surge roll drains only the old pod, never its fresh
  # replacement on the same node. Only the edge fires; a steady draining=true stream
  # (re-sent every heartbeat) does not re-notify.
  defp notify_drain_edge(state, rt, prev, %NodeStatus{} = status) do
    if status.draining and not prev.draining do
      send_drain(
        state.drain_listener,
        {:node_draining, rt.configured_id, rt.pod_uid, status.drain_deadline_unix_ms}
      )
    end

    :ok
  end

  defp send_drain(nil, _msg), do: :ok
  defp send_drain(pid, msg) when is_pid(pid), do: send(pid, msg)

  defp send_drain(name, msg) when is_atom(name) do
    case Process.whereis(name) do
      nil -> :ok
      pid -> send(pid, msg)
    end

    :ok
  end

  # An instance is dispatchable iff its stream is healthy AND its daemon is not
  # draining. Only then are its facts in the capacity table; every other case
  # deletes the row (fail-closed). The table is keyed by the INSTANCE tuple
  # {configured_id, pod_uid} (derived from the runtime entry, stable and known in
  # every path); node name, pod UID and instance id are carried inside the facts
  # map for the dispatcher's correlation.
  defp refresh_capacity(state, instance_id, %NodeStatus{} = status) do
    rt = state.node_runtime[instance_id]

    if rt.health == :healthy and not rt.draining do
      NodeCapacity.put(state.table, instance_key(rt), facts_from_status(status, rt, state.clock.()))
    else
      NodeCapacity.drop(state.table, instance_key(rt))
    end

    state
  end

  # Retract an instance's capacity facts. Keyed by the instance tuple, the same
  # key refresh_capacity writes under, so degradation always deletes the row a
  # prior status published.
  defp retract_capacity(state, instance_id) do
    case state.node_runtime[instance_id] do
      nil -> :ok
      rt -> NodeCapacity.drop(state.table, instance_key(rt))
    end

    state
  end

  # The ETS/capacity key for an instance: the {node_name, pod_uid} tuple. A
  # statically-seeded instance carries pod_uid "" and keys as {node, ""}
  # (node-scoped, matching the pre-dial-home behaviour).
  defp instance_key(rt), do: {rt.configured_id, rt.pod_uid}

  defp facts_from_status(%NodeStatus{} = s, rt, now) do
    configured_id = rt.configured_id
    workloads =
      for %WorkloadCapacity{} = wc <- s.workloads, into: %{} do
        {wc.workload,
         %{
           free_primed_slots: wc.free_primed_slots,
           snapshot_ref: wc.snapshot_ref,
           # serving_image_ref is the cold-boot handler-artifact ref (D-R3.11.2),
           # DISTINCT from snapshot_ref (the vsock-only base memory snapshot). Serving
           # placement cold-boots THIS ref, never snapshot_ref; empty for a workload
           # with no serving base built on this node.
           serving_image_ref: wc.serving_image_ref,
           base_state: wc.base_state,
           # exported (base-durability PR-1, additive): true when this workload's
           # current base has a complete store copy. BaseBuilder's periodic
           # export reconcile reads it to re-issue ExportArtifact only for a
           # current base that is present-but-unexported. false on a daemon that
           # predates the field, which BaseBuilder safely re-exports (the export
           # verb is idempotent per checksum).
           exported: wc.exported,
           # The vm_ids of this node's primed VMs for the workload, so the
           # dispatcher can adopt an existing warm pool into its inventory after
           # a control-plane restart instead of orphaning it (see Dispatcher
           # adopt_inventory). Empty list when the daemon reports none.
           primed_vm_ids: wc.primed_vm_ids
         }}
      end

    %{
      node_id: s.node_id,
      configured_id: configured_id,
      # Instance identity (R0 PR-2): pod_uid is the daemon-reported pod UID
      # (falling back to the runtime's registered pod_uid when a daemon predates
      # the field), and instance_id is the "node/pod_uid" string the dispatcher,
      # NodeChannel and BaseBuilder key their string-keyed maps on. Prefer the
      # runtime's pod_uid (the registration-authoritative identity) so a daemon
      # that has not yet stamped pod_uid on its status still keys consistently.
      pod_uid: rt.pod_uid,
      instance_id: rt.instance_id,
      # Brick fact (brick-capacity PR-1, additive): the daemon-reported T-shirt
      # size-class label ("2gi"/"4gi"/"8gi"/"16gi") this instance was deployed
      # as. Embervm.BrickLedger buckets per-instance headroom by it and (from
      # PR-2) pick_brick/3 selects a brick of the matching class. EMPTY on the
      # legacy DaemonSet and any daemon that predates the field, which the
      # ledger treats as the wildcard class (matches every request) so DS-only
      # placement is unchanged. Nothing reads this in PR-1 (the ledger is
      # populated but unread until the placement rewrite).
      size_class: s.size_class,
      # CPU-vendor fact (R7, ADR embervm/011, additive): the CPUID vendor this node
      # reports ("amd"/"intel"), from cpu_sku.vendor when the daemon sets the richer
      # CpuSku (field 28) and falling back to the standalone cpu_vendor (field 24)
      # for a daemon that predates it. The restore-on-miss wake planners
      # (stateful/serving/session/group managers) read this to stamp
      # RestoreArtifactRequest.artifact.vendor for the vendor-bound artifact kinds,
      # so noded's resolveRestorePrefix composes the vendor-keyed store prefix
      # instead of rejecting the restore InvalidArgument "vendor required". Empty on
      # a daemon that sets neither field (pre-R7), which noded treats as node-4's
      # vendor alias (standing decision 11), so an unset vendor still restores the
      # legacy un-vendored prefix rather than failing closed.
      cpu_vendor: cpu_vendor_from_status(s),
      workloads: workloads,
      mem_headroom_mib: s.mem_headroom_mib,
      cpu_headroom_millicores: s.cpu_headroom_millicores,
      live_vms: s.live_vms,
      max_live_vms: s.max_live_vms,
      # Budget facts (R0 PR-1, additive): the ceiling the daemon reads from its
      # OWN cgroup (ADR embervm/005 item 4, ADR embervm/013 section 7 as
      # amended). These feed the per-size-class brick slot ceiling; no
      # capacity decision may read max_live_vms after this. 0 means unknown
      # (unlimited cgroup or a daemon that never sets them, wire-compatible).
      mem_budget_mib: s.mem_budget_mib,
      cpu_budget_millicores: s.cpu_budget_millicores,
      draining: false,
      # Continuity fact (R6, additive): the daemon's latest object-store
      # reachability verdict. Read by the restore-on-miss wake planners to decide
      # whether a TRUE local miss can consult the store at all; false NEVER blocks a
      # local-state wake (fail-open warmth, standing decision 7). False on a daemon
      # with no store configured or one that never sets it (wire-compatible).
      store_reachable: s.store_reachable,
      updated_at: now,
      # Session facts (R2): the node's LIVE session VMs and BANKED snapshot
      # inventory, plus the sessions snapshot-dir disk usage. These are the source
      # of truth Embervm.SessionManager reconciles its ETS residency + banked
      # inventory against on boot and every sweep (adoption), and the disk numbers
      # the LRU capacity-eviction policy reads. Empty/zero when a daemon never sets
      # them (wire-compatible), which reads as "no session state, no disk pressure".
      session_vms: session_vms_from_status(s),
      session_snapshots: session_snapshots_from_status(s),
      snapshot_disk_free_bytes: s.snapshot_disk_free_bytes,
      snapshot_disk_used_bytes: s.snapshot_disk_used_bytes,
      # Serving facts (R3): the node's LIVE serving VMs (with the daemon's health
      # probe fact) and BANKED serving-snapshot inventory, plus the serving subnet
      # CIDR. These mirror the session facts above and are the source of truth the
      # serving lifecycle (Embervm.EndpointPublisher's node derivation,
      # Embervm.ServingHealth's ejection, and Task 8 adoption) reconciles against.
      # Empty when a daemon never sets them (wire-compatible): a node with no
      # serving_subnet_cidr is simply not a serving-capable node, so the publisher
      # pushes it no snapshot.
      serving_vms: serving_vms_from_status(s),
      serving_snapshots: serving_snapshots_from_status(s),
      serving_subnet_cidr: s.serving_subnet_cidr,
      # Stateful facts (R4, additive): the node's LIVE stateful VMs, BANKED
      # bundle inventory, and the per-workload volume ledger (generation +
      # allocated bytes). These are the source of truth
      # Embervm.StatefulManager's adoption reconcile heals its ETS residency +
      # pair-validity view against on boot and every sweep, mirroring the
      # serving facts above. Empty when a daemon never sets them
      # (wire-compatible), which reads as "no stateful state on this node".
      stateful_vms: stateful_vms_from_status(s),
      stateful_bundles: stateful_bundles_from_status(s),
      volumes: volumes_from_status(s),
      # Composite-group facts (R5, additive): the node's per-group bridges, LIVE
      # member VMs (with the daemon's health verdict), and banked bundle-set
      # inventory. These are the source of truth Embervm.GroupWakeManager's adoption
      # reconcile heals its ETS residency + set-completeness view against on boot and
      # every sweep, mirroring the stateful facts above. Empty when a daemon never
      # sets them (wire-compatible), which reads as "no group state on this node".
      group_networks: group_networks_from_status(s),
      group_member_vms: group_member_vms_from_status(s),
      group_bundle_sets: group_bundle_sets_from_status(s),
      # Local base inventory (base-durability PR-3, additive): the node's FULL
      # per-ref base disk inventory (every dir under bases/, including superseded
      # versions), which the WorkloadCapacity projection cannot convey (it reports
      # only the ONE current base per workload). Embervm.BaseBuilder's reconciled
      # base-retention sweep reconciles this observed local set against its desired
      # set (current + still-refcounted refs) and evicts the difference. Empty when
      # a daemon predates the field (wire-compatible), which the sweep reads as "no
      # local base inventory to reconcile", exactly the pre-PR-3 behavior.
      local_bases: local_bases_from_status(s)
    }
  end

  defp local_bases_from_status(%NodeStatus{local_bases: bases}) when is_list(bases) do
    for %BaseInventoryEntry{} = b <- bases do
      %{
        ref: b.ref,
        workload: b.workload,
        size_bytes: b.size_bytes,
        base_state: b.base_state
      }
    end
  end

  defp local_bases_from_status(_), do: []

  defp group_networks_from_status(%NodeStatus{group_networks: nets}) when is_list(nets) do
    for %GroupNetwork{} = n <- nets do
      %{
        group_instance_id: n.group_instance_id,
        cidr: n.cidr,
        bridge: n.bridge,
        member_count: n.member_count
      }
    end
  end

  defp group_networks_from_status(_s), do: []

  defp group_member_vms_from_status(%NodeStatus{group_member_vms: vms}) when is_list(vms) do
    for %GroupMemberVm{} = v <- vms do
      %{
        vm_id: v.vm_id,
        group_instance_id: v.group_instance_id,
        member_name: v.member_name,
        ip: v.ip,
        healthy: v.healthy,
        last_probe_unix_ms: v.last_probe_unix_ms
      }
    end
  end

  defp group_member_vms_from_status(_s), do: []

  defp group_bundle_sets_from_status(%NodeStatus{group_bundle_sets: sets}) when is_list(sets) do
    for %GroupBundleSet{} = s <- sets do
      %{
        set_id: s.set_id,
        group_instance_id: s.group_instance_id,
        created_at_unix_ms: s.created_at_unix_ms,
        # exported is true only when the WHOLE set's store copy is present and
        # current (R6): the restore-on-miss group planner reads it to know a
        # complete exported set can be restored on a true local miss.
        exported: s.exported,
        members:
          for(m <- s.members || [], do: %{member_name: m.member_name, snapshot_ref: m.snapshot_ref, size_bytes: m.size_bytes})
      }
    end
  end

  defp group_bundle_sets_from_status(_s), do: []

  defp serving_vms_from_status(%NodeStatus{serving_vms: vms}) when is_list(vms) do
    for %ServingVm{} = v <- vms do
      %{
        vm_id: v.vm_id,
        workload: v.workload,
        ip: v.ip,
        port: v.port,
        healthy: v.healthy,
        last_probe_unix_ms: v.last_probe_unix_ms
      }
    end
  end

  defp serving_vms_from_status(_s), do: []

  defp serving_snapshots_from_status(%NodeStatus{serving_snapshots: snaps}) when is_list(snaps) do
    for %ServingSnapshot{} = snap <- snaps do
      %{
        snapshot_ref: snap.snapshot_ref,
        workload: snap.workload,
        size_bytes: snap.size_bytes,
        created_at_unix_ms: snap.created_at_unix_ms,
        # exported is true when this bundle's store copy is present and current
        # (R6): the restore-on-miss serving planner reads it on a true local miss.
        exported: snap.exported
      }
    end
  end

  defp serving_snapshots_from_status(_s), do: []

  defp stateful_vms_from_status(%NodeStatus{stateful_vms: vms}) when is_list(vms) do
    for %StatefulVm{} = v <- vms do
      %{
        vm_id: v.vm_id,
        workload: v.workload,
        ip: v.ip,
        port: v.port,
        healthy: v.healthy,
        generation: v.generation,
        last_probe_unix_ms: v.last_probe_unix_ms,
        # Interruptible-bank checkpoint facts (ADR embervm/008): true + the token
        # when the VM is PAUSED awaiting a ResolveStateful, so the StatefulManager's
        # adoption resolves a stranded checkpoint (default abort) after a
        # control-plane restart.
        checkpoint_pending: v.checkpoint_pending,
        checkpoint_token: v.checkpoint_token
      }
    end
  end

  defp stateful_vms_from_status(_s), do: []

  defp stateful_bundles_from_status(%NodeStatus{stateful_bundles: bundles}) when is_list(bundles) do
    for %StatefulBundle{} = b <- bundles do
      %{
        snapshot_ref: b.snapshot_ref,
        workload: b.workload,
        generation: b.generation,
        size_bytes: b.size_bytes,
        created_at_unix_ms: b.created_at_unix_ms,
        # exported is true when this bundle's store copy is present and current
        # (R6): the restore-on-miss stateful planner reads it on a true local miss.
        exported: b.exported
      }
    end
  end

  defp stateful_bundles_from_status(_s), do: []

  defp volumes_from_status(%NodeStatus{volumes: vols}) when is_list(vols) do
    for %Volume{} = v <- vols do
      %{
        workload: v.workload,
        generation: v.generation,
        size_bytes: v.size_bytes,
        allocated_bytes: v.allocated_bytes,
        attached: v.attached,
        # exported_generation is the volume generation whose (vol.img, gen) pair
        # the store currently holds, 0 if no store copy exists (R6). The
        # restore-on-miss planner reads it to know a lost volume can be restored to
        # this generation, and the remote GC guard reads it to skip a re-export when
        # it already equals the live generation.
        exported_generation: v.exported_generation,
        # generation_blessed (R7, ADR embervm/011): true when the node's CURRENT
        # generation for this volume was recorded via a control-plane-issued
        # blessing, false for a self-bumped (unblessed) generation. Feeds
        # Embervm.StatefulManager.refresh_volume_facts/2's quarantine derivation.
        generation_blessed: v.generation_blessed
      }
    end
  end

  defp volumes_from_status(_s), do: []

  defp session_vms_from_status(%NodeStatus{session_vms: vms}) when is_list(vms) do
    for %SessionVm{} = v <- vms do
      %{vm_id: v.vm_id, session_id: v.session_id, workload: v.workload}
    end
  end

  defp session_vms_from_status(_s), do: []

  defp session_snapshots_from_status(%NodeStatus{session_snapshots: snaps}) when is_list(snaps) do
    for %SessionSnapshot{} = snap <- snaps do
      %{
        snapshot_ref: snap.snapshot_ref,
        session_id: snap.session_id,
        workload: snap.workload,
        size_bytes: snap.size_bytes,
        created_at_unix_ms: snap.created_at_unix_ms,
        # exported is true when this bundle's store copy is present and current
        # (R6): the restore-on-miss session planner reads it on a true local miss.
        exported: snap.exported
      }
    end
  end

  defp session_snapshots_from_status(_s), do: []

  # The node's CPUID vendor for restore-on-miss vendor keying (R7, ADR embervm/011).
  # Prefers the richer CpuSku.vendor (field 28) when the daemon reports it, falls
  # back to the standalone cpu_vendor (field 24) for a daemon that predates CpuSku,
  # and yields "" when the daemon sets neither (pre-R7, which noded maps to the
  # node-4 vendor alias). Matched structurally on the CpuSku struct so this module
  # needs no compile-time alias of it.
  defp cpu_vendor_from_status(%NodeStatus{cpu_sku: %{vendor: v}}) when is_binary(v) and v != "", do: v
  defp cpu_vendor_from_status(%NodeStatus{cpu_vendor: v}) when is_binary(v), do: v
  defp cpu_vendor_from_status(_s), do: ""

  # -- age-out state machine --------------------------------------------------

  # Recompute every instance's health from time-since-last-status and act on any
  # transition, then expire any instance that is BOTH registration-lapsed AND
  # stream-dead. This is the sole age-out authority, decoupled from stream liveness
  # so a silently wedged stream still ages out.
  defp evaluate_ages(state) do
    now = state.clock.()

    state =
      Enum.reduce(Map.keys(state.node_runtime), state, fn instance_id, acc ->
        evaluate_node_age(acc, instance_id, now)
      end)

    expire_lapsed_instances(state, now)
  end

  # Remove an instance from the registry entirely when BOTH signals say it is gone:
  # its dial-home registration has lapsed (no re-register within expire_after_ms)
  # AND its WatchNode stream is dead (:down). Requiring both means a CP-side network
  # blip (stream flaps, registration keeps arriving) never drops a healthy node, and
  # a control-plane restart (registration timers reset, stream re-establishes) never
  # drops one either. A statically-seeded instance (last_registered_at nil) never
  # lapses and is never expired here; only dial-home instances age out. Expiry drops
  # the capacity row, tells the BaseBuilder to forget the node, kills the streamer,
  # and removes the runtime entry so no reconnect resurrects it.
  defp expire_lapsed_instances(state, now) do
    Enum.reduce(Map.keys(state.node_runtime), state, fn instance_id, acc ->
      rt = acc.node_runtime[instance_id]

      lapsed? =
        not is_nil(rt.last_registered_at) and
          now - rt.last_registered_at >= acc.expire_after_ms

      if lapsed? and rt.health == :down do
        expire_instance(acc, instance_id)
      else
        acc
      end
    end)
  end

  defp evaluate_node_age(state, node_id, now) do
    rt = state.node_runtime[node_id]
    baseline = rt.last_status_at || rt.started_at
    elapsed = now - baseline

    new_health =
      cond do
        elapsed >= state.down_after_ms -> :down
        elapsed >= state.unknown_after_ms -> :unknown
        is_nil(rt.last_status_at) -> :starting
        true -> :healthy
      end

    if new_health == rt.health do
      state
    else
      apply_health_transition(state, node_id, rt.health, new_health)
    end
  end

  # Health changed for a node. Update the runtime, keep the capacity table
  # fail-closed (only :healthy-and-not-draining keeps a row; here we never enter
  # from a fresh status so a non-healthy target always retracts), and on the edge
  # INTO :down fire reassignment once and tear the streamer down to force a fresh
  # connection (silent-wedge recovery).
  defp apply_health_transition(state, node_id, _old_health, :healthy) do
    # A tick can only compute :healthy when last_status_at is recent, which means
    # apply_status already published facts; nothing to do but record it.
    put_in(state, [:node_runtime, node_id, :health], :healthy)
  end

  defp apply_health_transition(state, node_id, old_health, new_health) do
    state = put_in(state, [:node_runtime, node_id, :health], new_health)
    state = retract_capacity(state, node_id)

    if new_health == :down and old_health != :down do
      handle_node_down(state, node_id)
    else
      state
    end
  end

  # The node crossed into :down. Reassign its in-flight tasks (at-least-once, via
  # the existing Retry policy) exactly once, then drop the current streamer if any
  # so a wedged connection is torn down and reconnected.
  #
  # Ordering matters: we FORGET the streamer's pid before killing it, so a
  # NodeStatus the streamer enqueued concurrently with this down-transition (a
  # straggler still in our mailbox behind the age tick) is dropped by the
  # handle_info pid guard rather than applied, which would otherwise resurrect the
  # node to :healthy and republish capacity for a node we just declared down and
  # reassigned. Because the pid is forgotten, the kill's ensuing :DOWN is ignored,
  # so we schedule the backoff reconnect here explicitly instead of relying on it.
  #
  # When the streamer is already closed (nil), a reconnect is already pending from
  # the last watch end (nil is only ever reached via forget_streamer, which is
  # always immediately followed by handle_watch_end), so there is nothing to do.
  defp handle_node_down(state, node_id) do
    Logger.warning("embervm node registry: node #{node_id} is DOWN (stream silent > #{state.down_after_ms}ms)")

    try do
      state.reassign_fun.(node_id)
    rescue
      e -> Logger.error("embervm node registry: reassign for #{node_id} raised: #{inspect(e)}")
    end

    case state.node_runtime[node_id].streamer do
      {pid, _ref} ->
        state = forget_streamer(state, pid, node_id)
        Process.exit(pid, :kill)
        schedule_reconnect(state, node_id)

      nil ->
        state
    end
  end

  # -- streamer lifecycle -----------------------------------------------------

  # Spawn the monitored streamer that owns the blocking watch for one node.
  # spawn_monitor (not link): a streamer crash surfaces as a :DOWN handled as a
  # disconnect, never escalating to this GenServer. The streamer connects,
  # forwards each NodeStatus tagged with its own pid, and always reports a final
  # result even if the watch raises.
  defp start_streamer(state, node_id) do
    rt = state.node_runtime[node_id]
    owner = self()
    address = rt.address
    configured_id = rt.configured_id
    connect_fun = state.connect_fun
    watch_fun = state.watch_fun
    disconnect_fun = state.disconnect_fun
    sync_registry_fun = state.sync_registry_fun

    {pid, ref} =
      spawn_monitor(fn ->
        streamer = self()

        result =
          case connect_fun.(address) do
            {:ok, channel} ->
              try do
                # Artifact-decoupling Phase 2: on EVERY (re)connect, before we
                # start consuming the WatchNode stream, PUSH the authoritative
                # workload registry to the daemon (SyncRegistry). A freshly
                # (re)started noded boots with an empty (or stale-cache) registry
                # and gates readiness on this replay, so pushing it here is what
                # opens the daemon for new work: a daemon that missed incremental
                # Register/Deregister while disconnected re-converges to truth on
                # reconnect. A push failure is logged and non-fatal (the daemon
                # simply stays not-ready and the next reconnect retries); we still
                # enter the watch so capacity facts flow either way.
                sync_registry_fun.(channel, configured_id)

                watch_fun.(channel, configured_id, fn status ->
                  send(owner, {:node_status, streamer, status})
                end)
              catch
                kind, reason -> {:error, {kind, reason}}
              after
                disconnect_fun.(channel)
              end

            {:error, reason} ->
              {:error, {:connect, reason}}
          end

        send(owner, {:watch_result, streamer, result})
      end)

    rt = %{rt | streamer: {pid, ref}, watch_started_at: state.clock.()}

    state
    |> put_in([:node_runtime, node_id], rt)
    |> put_in([:streamers, pid], node_id)
  end

  # Clear a streamer from the current-streamer index and the node runtime. Safe
  # to call for a pid already forgotten (idempotent), which happens when the
  # down-edge kill and the subsequent :DOWN both run.
  defp forget_streamer(state, pid, node_id) do
    state = %{state | streamers: Map.delete(state.streamers, pid)}

    case state.node_runtime[node_id] do
      %{streamer: {^pid, _ref}} = rt ->
        put_in(state.node_runtime[node_id], %{rt | streamer: nil})

      _ ->
        state
    end
  end

  # A watch ended for a node; decide how to resume. A healthy, long-lived clean
  # close reconnects immediately (the daemon's normal stream refresh); a fast or
  # errored close backs off exponentially first (hot-loop guard against a daemon
  # that accepts then instantly closes, or a flapping mesh path).
  defp handle_watch_end(state, node_id, result) do
    rt = state.node_runtime[node_id]
    clean = match?({:ok, :closed}, result)
    long_lived = clean and state.clock.() - rt.watch_started_at >= state.min_watch_ms

    if long_lived do
      state
      |> put_in([:node_runtime, node_id, :backoff_ms], state.base_backoff_ms)
      |> start_streamer(node_id)
    else
      Logger.warning(
        "embervm node registry: node #{node_id} stream ended (#{inspect(result)}), reconnect in #{rt.backoff_ms}ms"
      )

      schedule_reconnect(state, node_id)
    end
  end

  # Arm a reconnect timer for one node and double its backoff (capped) for the
  # next consecutive failure. A successful long-lived stream resets it to base.
  defp schedule_reconnect(state, node_id) do
    rt = state.node_runtime[node_id]
    Process.send_after(self(), {:reconnect, node_id}, rt.backoff_ms)
    next = min(rt.backoff_ms * 2, state.max_backoff_ms)
    put_in(state, [:node_runtime, node_id, :backoff_ms], next)
  end

  defp schedule_age_check(state) do
    Process.send_after(self(), :age_check, state.age_check_ms)
  end

  # -- dial-home registration -------------------------------------------------

  # Normalize an incoming registration body into %{node, pod_uid, address,
  # boot_id, instance_id}. Accepts string or atom keys (the router decodes JSON to
  # string keys; a test may pass atoms). A blank node OR address is rejected; a
  # blank pod_uid collapses to a node-scoped instance (instance_id == node name),
  # so a pre-Downward-API daemon still registers under a stable key.
  defp normalize_registration(reg) do
    node = reg_field(reg, "node") |> to_trimmed()
    pod_uid = reg_field(reg, "pod_uid") |> to_trimmed()
    address = reg_field(reg, "address") |> to_trimmed()
    boot_id = reg_field(reg, "boot_id") |> to_trimmed()

    if node == "" or address == "" do
      :error
    else
      {:ok,
       %{
         node: node,
         pod_uid: pod_uid,
         address: address,
         boot_id: boot_id,
         instance_id: instance_id_of(node, pod_uid)
       }}
    end
  end

  defp reg_field(reg, key) do
    Map.get(reg, key) || Map.get(reg, String.to_atom(key))
  end

  defp to_trimmed(nil), do: ""
  defp to_trimmed(v) when is_binary(v), do: String.trim(v)
  defp to_trimmed(v), do: v |> to_string() |> String.trim()

  # The instance handle: "node/pod_uid" for a dial-home instance, or just the node
  # name for a node-scoped instance (empty pod_uid: a statically-seeded pinned
  # override or a pre-Downward-API daemon). Keeping the bare node name for the
  # empty case preserves the pre-dial-home keying so a pinned single-node override
  # and the existing tests key identically to before.
  defp instance_id_of(node, ""), do: node
  defp instance_id_of(node, pod_uid), do: node <> "/" <> pod_uid

  # The NodeChannel key this instance is registered under: its instance_id
  # ("node/pod_uid", what the dispatcher's pick_node resolves to and, post the
  # instance-key migration, what every stateful/serving/session/group wake resolves
  # to as well). PR-B0c removed the node-name alias: with all consumers dialing the
  # owning instance_id, a shared last-writer-wins node-name key could only misroute a
  # wake across a node's co-located bricks, so dropping it makes that misroute
  # structurally impossible. For a node-scoped instance (empty pod_uid) the instance_id
  # IS the node name, so a static/pinned single-daemon override still keys under its
  # node name unchanged. Returned as a list so add and remove drive off the same shape.
  defp channel_keys(rt) do
    [rt.instance_id]
  end

  # Apply one registration: upsert the instance keyed by {node, pod_uid}. Three
  # cases, all event-driven (no polling): a NEW instance seeds its runtime, joins
  # the NodeChannel/BaseBuilder fleet and opens a streamer; a KNOWN instance whose
  # advertised address CHANGED (a re-scheduled pod keeping the same UID, rare, or a
  # test) re-points its channel + streamer to the new address; a KNOWN instance at
  # the same address just refreshes last_registered_at (the liveness half of the
  # two-signal expiry). Registration never touches capacity facts directly; those
  # flow from the WatchNode stream the streamer consumes.
  defp apply_registration(state, %{instance_id: instance_id} = norm) do
    now = state.clock.()

    case state.node_runtime[instance_id] do
      nil ->
        add_instance(state, norm, now)

      %{address: addr} = _rt when addr != norm.address ->
        Logger.info(
          "embervm node registry: instance #{instance_id} re-registered at new address #{norm.address}"
        )

        state
        |> expire_instance(instance_id)
        |> add_instance(norm, now)

      _rt ->
        put_in(state, [:node_runtime, instance_id, :last_registered_at], now)
    end
  end

  # Seed a newly-registered instance's runtime, propagate its address to the
  # Prime/Assign channel holder (under BOTH keys, see below) and the BaseBuilder
  # (keyed by INSTANCE id), and open its streamer. Mirrors the static seed shape so
  # age-out and streamer plumbing treat a registered instance identically.
  #
  # NodeChannel keying (single-key, post instance-key migration): the registry's own
  # tables are keyed by instance_id ("node/pod_uid"), and so now is NodeChannel. Every
  # consumer resolves the owning instance_id before dialing: the dispatcher (PR-2)
  # resolves pick_node to an instance_id, and the stateful/session/serving/group wakes
  # and PoolManager were migrated (B0a/B0b) to dial the owning instance_id too. The
  # node-name alias that briefly bridged the legacy wakes was removed in PR-B0c: a
  # shared, last-writer-wins node-name key across a node's co-located bricks could only
  # misroute a wake to the wrong sibling, so dropping it makes that misroute
  # structurally impossible. A node-scoped instance (empty pod_uid) has instance_id ==
  # node name, so a static/pinned single-daemon override still keys under its node name.
  defp add_instance(state, norm, now) do
    rt =
      seed_runtime(
        %{id: norm.node, address: norm.address, pod_uid: norm.pod_uid},
        state.base_backoff_ms,
        now
      )

    rt = %{rt | last_registered_at: now}

    Logger.info(
      "embervm node registry: registered instance #{rt.instance_id} (node #{norm.node}, pod #{norm.pod_uid}, #{norm.address})"
    )

    for key <- channel_keys(rt) do
      safe_channel_update(state.channel_updater_fun, key, norm.address)
    end

    safe_base_builder_update(state.base_builder_updater_fun, {:add, rt.instance_id, norm.address})

    state
    |> put_in([:node_runtime, rt.instance_id], rt)
    |> start_streamer(rt.instance_id)
  end

  # Seed one instance runtime entry from a node spec (%{id, address} plus optional
  # pod_uid). The runtime map is keyed by instance_id ("node/pod_uid"); an absent
  # pod_uid collapses to "" (a node-scoped instance, the pre-dial-home shape).
  defp seed_runtime(spec, base_backoff, now) do
    node = spec.id
    pod_uid = Map.get(spec, :pod_uid, "") |> to_trimmed()

    %{
      configured_id: node,
      pod_uid: pod_uid,
      instance_id: instance_id_of(node, pod_uid),
      address: spec.address,
      # {pid, ref} of the live streamer, or nil when no stream is open.
      streamer: nil,
      backoff_ms: base_backoff,
      # Monotonic ms the current stream opened, to tell a healthy long-lived close
      # (reconnect now) from a suspect fast close (back off).
      watch_started_at: now,
      # Monotonic ms of the last NodeStatus. nil until the first arrives; the age
      # baseline before then is started_at, so a daemon that never answers still
      # ages starting -> unknown -> down.
      last_status_at: nil,
      started_at: now,
      # Monotonic ms of the last dial-home registration, nil for a statically-seeded
      # instance (which never registers and so is never expired). One half of the
      # two-signal expiry (the other is a dead stream).
      last_registered_at: nil,
      health: :starting,
      draining: false
    }
  end

  # Remove an instance from the registry: retract its capacity row, drop its
  # NodeChannel key (its instance_id, the only key it is registered under post-B0c),
  # tell the BaseBuilder to drop it, kill its streamer if any (forgetting the pid
  # first so the ensuing DOWN is ignored), and drop its runtime so no reconnect
  # resurrects it. Used both by two-signal expiry and by a re-registration re-point.
  #
  # NodeChannel removal (single-key, post instance-key migration): add_instance
  # registered this instance's address under its instance_id alone, and expiry removes
  # that same key. The instance_id is unique to this instance, so removing it is always
  # correct and, crucially, cannot affect a co-located sibling: siblings on the same
  # node now hold independent instance_id keys, so there is no shared node-name alias
  # left for one instance's expiry to clobber (the misroute PR-B0c eliminated).
  defp expire_instance(state, instance_id) do
    Logger.info("embervm node registry: instance #{instance_id} expired; tearing down")

    safe_channel_remove(state.channel_remover_fun, instance_id)

    safe_base_builder_update(state.base_builder_updater_fun, {:remove, instance_id})
    state = retract_capacity(state, instance_id)

    state =
      case state.node_runtime[instance_id] do
        %{streamer: {pid, _ref}} ->
          state = forget_streamer(state, pid, instance_id)
          Process.exit(pid, :kill)
          state

        _ ->
          state
      end

    %{state | node_runtime: Map.delete(state.node_runtime, instance_id)}
  end

  # Propagate an instance's address to the Prime/Assign channel holder, swallowing any
  # error (a NodeChannel that is not running, or a slow call): the streamer we open
  # next carries WatchNode over its own channel regardless, and the hot-path channel
  # simply re-dials on its next invalidate if this update did not land.
  defp safe_channel_update(nil, _node_id, _address), do: :ok

  defp safe_channel_update(updater, node_id, address) do
    updater.(node_id, address)
    :ok
  rescue
    e ->
      Logger.warning("embervm node registry: channel address update for #{node_id} failed: #{inspect(e)}")
      :ok
  catch
    # GenServer.call to a NodeChannel that is not running EXITs (does not raise);
    # swallow it (the streamer carries WatchNode regardless and the channel
    # re-dials on its next invalidate).
    kind, reason ->
      Logger.warning("embervm node registry: channel address update for #{node_id} exited: #{inspect({kind, reason})}")
      :ok
  end

  # Default: point the singleton Embervm.NodeChannel at the instance's address under
  # its instance_id (add_instance calls this once per key in channel_keys/1, which is
  # now just the instance_id). On a re-registration at a new address, update_address
  # unconditionally erases the channel cached under this key and re-points it, so no
  # stale endpoint survives.
  defp default_channel_update(key, address) do
    Embervm.NodeChannel.update_address(key, address)
    :ok
  end

  # Propagate an instance expiry to the Prime/Assign channel holder by dropping the
  # expiring instance's instance_id key (its only NodeChannel key post-B0c). Swallowing
  # any error mirrors safe_channel_update: a NodeChannel that is not running, or a slow
  # call, must never crash expiry.
  defp safe_channel_remove(nil, _key), do: :ok

  defp safe_channel_remove(remover, key) do
    remover.(key)
    :ok
  rescue
    e ->
      Logger.warning("embervm node registry: channel removal for #{key} failed: #{inspect(e)}")
      :ok
  catch
    kind, reason ->
      Logger.warning("embervm node registry: channel removal for #{key} exited: #{inspect({kind, reason})}")
      :ok
  end

  # Default: drop the key from the singleton Embervm.NodeChannel (erasing any cached
  # channel), so a subsequent get/1 returns :unknown_node rather than dialing a
  # torn-down pod's address.
  defp default_channel_remove(key) do
    Embervm.NodeChannel.remove_address(key)
    :ok
  end

  # Propagate a registered instance add/remove to the BaseBuilder, swallowing any
  # error (a BaseBuilder that is not running, a slow call): the fleet still gets
  # WatchNode capacity via our own streamers, and the next re-registration
  # re-notifies. Never crash registration on a BaseBuilder hiccup.
  defp safe_base_builder_update(nil, _msg), do: :ok

  defp safe_base_builder_update(updater, msg) do
    updater.(msg)
    :ok
  rescue
    e ->
      Logger.warning("embervm node registry: base builder update #{inspect(msg)} failed: #{inspect(e)}")
      :ok
  catch
    kind, reason ->
      Logger.warning("embervm node registry: base builder update #{inspect(msg)} exited: #{inspect({kind, reason})}")
      :ok
  end

  # Default: add/remove the instance on the singleton Embervm.BaseBuilder so
  # BuildBase placement learns the registered fleet (keyed by instance_id).
  defp default_base_builder_update({:add, instance_id, address}) do
    Embervm.BaseBuilder.add_node(instance_id, address)
    :ok
  end

  defp default_base_builder_update({:remove, instance_id}) do
    Embervm.BaseBuilder.remove_node(instance_id)
    :ok
  end

  # -- default (production) seams --------------------------------------------

  # Plaintext h2c to the noded Service over the Mint adapter (no TLS, no castore;
  # the daemon listens on the pod network gated by mesh policy). This is the
  # pattern the Task 3 Mint round-trip proved.
  defp default_connect(address) do
    GRPC.Stub.connect(address, adapter: GRPC.Client.Adapters.Mint)
  end

  defp default_disconnect(channel) do
    _ = GRPC.Stub.disconnect(channel)
    :ok
  end

  # Open WatchNode and forward each NodeStatus via emit. Returns {:ok, :closed}
  # when the server ends the stream cleanly, or {:error, reason} on any transport
  # or stream error, mirroring Embervm.WorkloadWatcher's watch contract so
  # handle_watch_end treats both watches identically.
  defp default_watch(channel, node_id, emit) do
    case NodeService.Stub.watch_node(channel, %WatchNodeRequest{node_id: node_id}) do
      {:ok, stream} ->
        Enum.reduce_while(stream, {:ok, :closed}, fn
          {:ok, %NodeStatus{} = status}, acc ->
            emit.(status)
            {:cont, acc}

          {:error, reason}, _acc ->
            {:halt, {:error, reason}}
        end)

      {:error, reason} ->
        {:error, {:watch, reason}}
    end
  end

  # Push the authoritative workload registry to the just-connected daemon
  # (artifact-decoupling Phase 2). Builds the entry set from the control plane's
  # workload view and calls SyncRegistry over the channel. The WHOLE body is
  # wrapped so a failure NEVER crashes the streamer: a returned {:error, _} is
  # logged, and a RAISED error (a WorkloadCatalog ETS table that does not exist yet
  # during early boot -> ArgumentError, or a bad channel value in a test)
  # or an EXIT is caught and logged too. The daemon simply stays not-ready and the
  # next reconnect retries; crashing the streamer here would take down capacity
  # reporting for the node, which is strictly worse than a missed replay.
  defp default_sync_registry(channel, node_id) do
    entries = registry_entries()

    case NodeService.Stub.sync_registry(channel, %SyncRegistryRequest{entries: entries}) do
      {:ok, %{entry_count: n}} ->
        Logger.info("embervm node registry: replayed #{n} workload registry entries to #{node_id}")
        :ok

      other ->
        Logger.warning("embervm node registry: SyncRegistry to #{node_id} failed: #{inspect(other)}")
        :ok
    end
  rescue
    e ->
      Logger.warning("embervm node registry: SyncRegistry to #{node_id} raised: #{inspect(e)}")
      :ok
  catch
    kind, reason ->
      Logger.warning("embervm node registry: SyncRegistry to #{node_id} exited: #{inspect({kind, reason})}")
      :ok
  end

  # Build the authoritative RegistryEntry set from the control plane's workload
  # view: the WorkloadCatalog (the op-log-backed catalog TaskStore/dispatcher
  # read) supplies each workload's name, image_ref and sizing, and the
  # chart-delivered node-side image identity (EMBERVM_NODE_IMAGE_IDENTITY, keyed by
  # image_ref) supplies the rootfs_ref + harness_init that USED to live in the
  # daemon's EMBERVM_NODED_IMAGES table. The join is by image_ref (the CR's
  # source.image.ref), the stable bridge between the per-workload catalog entry and
  # the per-image identity map. A workload whose image_ref the identity map does
  # not know still gets an entry (empty rootfs/harness), so the CP stays
  # authoritative for the SET of workloads regardless; the daemon then falls back
  # to its configured defaults for the missing node-side facts.
  defp registry_entries do
    identity = node_image_identity()

    catalog_entries =
      for name <- Embervm.WorkloadCatalog.all_names() do
        entry =
          case Embervm.WorkloadCatalog.fetch(name) do
            {:ok, e} -> e
            :error -> %{}
          end

        image_ref = entry[:image_ref] || ""
        {rootfs_ref, harness_init} = Map.get(identity, image_ref, {"", ""})

        %RegistryEntry{
          workload: name,
          image_digest: "",
          image_ref: image_ref,
          rootfs_ref: rootfs_ref,
          harness_init: harness_init,
          sizing: %ResourceSpec{vcpus: entry[:vcpus] || 0, mem_mib: entry[:mem_mib] || 0}
        }
      end

    # Also emit one entry PER node-side image_ref in the identity map, keyed by the
    # image_ref as a synthetic workload. This is what lets the daemon resolve a
    # cold-boot's RUNTIME image (serving-fresh's runtime rootfs, a stateful boot
    # image's runtime, a composite MEMBER image) by image_ref: those refs are not
    # 1:1 with a Workload CR (a zip-lane serving workload carries no image_ref, and
    # a composite CR carries several member images under one name), so the per-CR
    # entries above cannot cover them. The daemon's getByImageRef index resolves any
    # of them from these entries. Sizing is zero here (the per-BuildBase request or
    # the per-CR entry carries the real sizing); these carry only the rootfs/harness
    # identity keyed by image_ref. Entries the per-CR loop already produced for the
    # same image_ref are harmless duplicates the daemon's convergence de-dupes by
    # workload key (the synthetic key is the ref, distinct from a CR name).
    identity_entries =
      for {image_ref, {rootfs_ref, harness_init}} <- identity do
        %RegistryEntry{
          workload: "image:" <> image_ref,
          image_digest: "",
          image_ref: image_ref,
          rootfs_ref: rootfs_ref,
          harness_init: harness_init,
          sizing: %ResourceSpec{vcpus: 0, mem_mib: 0}
        }
      end

    catalog_entries ++ identity_entries
  end

  # Parse EMBERVM_NODE_IMAGE_IDENTITY (chart-rendered from the same values that
  # once fed EMBERVM_NODED_IMAGES). Format: comma-separated
  # `imageRef=rootfsRef|harnessInit` triples; harnessInit may be empty. An unset
  # or malformed env yields an empty map (every workload then gets empty node-side
  # identity, and the daemon uses its defaults).
  defp node_image_identity do
    case System.get_env("EMBERVM_NODE_IMAGE_IDENTITY") do
      nil ->
        %{}

      raw ->
        raw
        |> String.split(",", trim: true)
        |> Enum.reduce(%{}, fn pair, acc ->
          case String.split(pair, "=", parts: 2) do
            [name, rest] ->
              {rootfs, harness} =
                case String.split(rest, "|", parts: 2) do
                  [r, h] -> {String.trim(r), String.trim(h)}
                  [r] -> {String.trim(r), ""}
                end

              n = String.trim(name)
              if n != "", do: Map.put(acc, n, {rootfs, harness}), else: acc

            _ ->
              acc
          end
        end)
    end
  end

  # The production reassignment path: a node going down means every task it held
  # in-flight must be retried (at-least-once; we cannot know whether the guest
  # completed). In v1 there is exactly one node, so every in-flight task IS on
  # this node; Embervm.TaskStore.reassign_in_flight/0 fails each through the
  # existing Retry policy (transport class -> failed_retryable, then the
  # dispatcher's retry moves it back to queued). Inert until Task 11 actually
  # dispatches (nothing is ever in-flight before then), but wired and correct.
  defp default_reassign(node_id) do
    Logger.warning("embervm node registry: reassigning in-flight tasks from downed node #{node_id}")
    Embervm.TaskStore.reassign_in_flight()
    :ok
  end

  defp default_clock, do: System.monotonic_time(:millisecond)
end
