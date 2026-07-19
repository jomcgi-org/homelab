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

  ## the multi-node seam

  `start_link/1` takes `:nodes`, a LIST of `%{id, address}` specs. v1 configures
  exactly one (from chart values), but every internal structure is keyed by node
  id and every loop iterates the list, so multi-node needs no reshaping here,
  only more entries in the list. There is deliberately no cross-node placement
  logic in R0; that is the dispatcher's job (Task 11).
  """

  use GenServer
  require Logger

  alias Embervm.NodeCapacity

  alias Embervm.Node.V1.{
    GroupBundleSet,
    GroupMemberVm,
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
  # How often the node set is re-discovered from EndpointSlices (C4 interim: a
  # poll-and-reconcile, not a live watch). 30s is well under the time a rolled
  # pod's absence matters and cheap (a cached-read LIST).
  @discover_interval_ms 30_000

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
  Forces one synchronous node-set re-discovery + reconcile (the same code the
  periodic `:discover` timer runs). Tests drive discovery deterministically through
  this with an injected `discover_fun`; in production the timer fires it every
  #{@discover_interval_ms}ms. A no-op when no `discover_fun` is configured.
  """
  @spec discover(GenServer.server()) :: :ok
  def discover(server \\ __MODULE__) do
    GenServer.call(server, :discover)
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
    # Periodic node-set re-discovery (artifact-decoupling PR-C, C4). noded is a
    # DaemonSet behind a headless Service; a roll changes pod IPs, so the node list
    # is re-listed on an interval and reconciled (new pods get a streamer, vanished
    # pods are torn down) instead of frozen at boot. The default reads
    # EndpointSlices via Embervm.Application; tests inject a fake discover_fun and
    # drive :discover synchronously. A nil discover_fun (the default when no
    # discovery source is configured, e.g. a pinned single-node override or tests)
    # disables re-discovery entirely, so the node set stays exactly the :nodes list.
    discover_fun = Keyword.get(opts, :discover_fun, nil)
    discover_interval = Keyword.get(opts, :discover_interval_ms, @discover_interval_ms)
    # How a re-discovered node's NEW address is propagated to the Prime/Assign
    # hot-path channel holder (Embervm.NodeChannel), whose node_addr map is static
    # and would otherwise keep dialing a rolled pod's dead IP. Default calls the
    # singleton NodeChannel; tests inject a fake to assert the propagation without a
    # running NodeChannel.
    channel_updater_fun =
      Keyword.get(opts, :channel_updater_fun, &default_channel_update/2)

    # How a discovered node add/remove is propagated to Embervm.BaseBuilder, which
    # (like NodeChannel) is seeded with an EMPTY node list at boot under discovery
    # and must learn the fleet so BuildBase can PLACE builds. Without this, a
    # discovery-configured control plane would never build any base (every workload
    # holds {:pending, :no_node}). Default calls the singleton BaseBuilder; tests
    # inject a fake to assert the propagation without a running BaseBuilder.
    base_builder_updater_fun =
      Keyword.get(opts, :base_builder_updater_fun, &default_base_builder_update/1)

    NodeCapacity.create(table)

    now = clock.()

    node_runtime =
      for %{id: id, address: address} <- nodes, into: %{} do
        {id,
         %{
           configured_id: id,
           address: address,
           # {pid, ref} of the live streamer, or nil when no stream is open.
           streamer: nil,
           backoff_ms: base_backoff,
           # Monotonic ms the current stream opened, to tell a healthy long-lived
           # close (reconnect now) from a suspect fast close (back off).
           watch_started_at: now,
           # Monotonic ms of the last NodeStatus. nil until the first arrives; the
           # age baseline before then is started_at, so a daemon that never
           # answers still ages starting -> unknown -> down.
           last_status_at: nil,
           started_at: now,
           health: :starting,
           draining: false
         }}
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
      discover_fun: discover_fun,
      discover_interval_ms: discover_interval,
      channel_updater_fun: channel_updater_fun,
      base_builder_updater_fun: base_builder_updater_fun,
      node_runtime: node_runtime,
      # The process notified once per drain rising edge with {:node_draining,
      # node_id, deadline_ms} so it can force-bank the node's live instances before
      # the pod exits (R6, ADR embervm/009). A registered name or pid; default the
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

  # Open every node's stream and arm the age-out timer once the process is fully
  # initialized (continue runs after init returns, before any external message).
  @impl true
  def handle_continue(:start, state) do
    state =
      Enum.reduce(Map.keys(state.node_runtime), state, fn node_id, acc ->
        start_streamer(acc, node_id)
      end)

    schedule_age_check(state)
    # Run the FIRST discovery immediately (not after the 30s interval): the app
    # seeds an EMPTY node list at construction (discovery cannot touch Finch there,
    # ADR embervm/012 boot ordering), so the fleet only populates once this fires.
    # By handle_continue time the supervisor has started Finch (NodeRegistry sits
    # after Finch in the rest_for_one chain), so the K8s list is safe here. A
    # discover_fun that is nil (pinned override / tests) makes this a no-op.
    state = reconcile_discovered(state)
    schedule_discover(state)
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

  # Periodic node-set re-discovery, then re-arm the timer (C4 interim).
  def handle_info(:discover, state) do
    state = reconcile_discovered(state)
    schedule_discover(state)
    {:noreply, state}
  end

  def handle_info(_msg, state), do: {:noreply, state}

  @impl true
  def handle_call(:status, _from, state) do
    snapshot =
      for {node_id, rt} <- state.node_runtime, into: %{} do
        facts =
          case NodeCapacity.fetch(state.table, node_id) do
            {:ok, f} -> f
            :error -> nil
          end

        {node_id,
         %{
           configured_id: rt.configured_id,
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

  def handle_call({:inject_status, node_id, status}, _from, state) do
    if Map.has_key?(state.node_runtime, node_id) do
      {:reply, :ok, apply_status(state, node_id, status)}
    else
      {:reply, :ok, state}
    end
  end

  def handle_call(:tick, _from, state) do
    {:reply, :ok, evaluate_ages(state)}
  end

  def handle_call(:discover, _from, state) do
    {:reply, :ok, reconcile_discovered(state)}
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
  defp apply_status(state, node_id, %NodeStatus{} = status) do
    now = state.clock.()
    prev = state.node_runtime[node_id]
    rt = %{prev | last_status_at: now, health: :healthy, draining: status.draining}
    state = put_in(state.node_runtime[node_id], rt)
    notify_drain_edge(state, node_id, prev, status)
    refresh_capacity(state, node_id, status)
  end

  # On the RISING edge of draining (false -> true) notify the drain listener exactly
  # once with the node's published deadline, so it force-banks the node's live
  # instances within the bounded-preemption window (R6). Only the edge fires; a
  # steady draining=true stream (re-sent every heartbeat) does not re-notify.
  defp notify_drain_edge(state, node_id, prev, %NodeStatus{} = status) do
    if status.draining and not prev.draining do
      send_drain(state.drain_listener, {:node_draining, node_id, status.drain_deadline_unix_ms})
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

  # A node is dispatchable iff its stream is healthy AND its daemon is not
  # draining. Only then are its facts in the capacity table; every other case
  # deletes the row (fail-closed). The table is keyed by the CONFIGURED node id
  # (the node_runtime key, stable and known in every path); the daemon-reported
  # id is carried inside the facts map for the dispatcher's correlation.
  defp refresh_capacity(state, node_id, %NodeStatus{} = status) do
    rt = state.node_runtime[node_id]

    if rt.health == :healthy and not rt.draining do
      NodeCapacity.put(state.table, node_id, facts_from_status(status, node_id, state.clock.()))
    else
      NodeCapacity.drop(state.table, node_id)
    end

    state
  end

  # Retract a node's capacity facts. Keyed by the configured node id, the same
  # key refresh_capacity writes under, so degradation always deletes the row a
  # prior status published.
  defp retract_capacity(state, node_id) do
    NodeCapacity.drop(state.table, node_id)
    state
  end

  defp facts_from_status(%NodeStatus{} = s, configured_id, now) do
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
      group_bundle_sets: group_bundle_sets_from_status(s)
    }
  end

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

  # -- age-out state machine --------------------------------------------------

  # Recompute every node's health from time-since-last-status and act on any
  # transition. This is the sole age-out authority, decoupled from stream
  # liveness so a silently wedged stream still ages out.
  defp evaluate_ages(state) do
    now = state.clock.()

    Enum.reduce(Map.keys(state.node_runtime), state, fn node_id, acc ->
      evaluate_node_age(acc, node_id, now)
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

  # Arm the next periodic re-discovery, but only when a discover_fun is configured
  # (no source => the node set is the static :nodes list and never re-lists).
  defp schedule_discover(%{discover_fun: nil}), do: :ok

  defp schedule_discover(state) do
    Process.send_after(self(), :discover, state.discover_interval_ms)
    :ok
  end

  # Re-list the node set and reconcile it against the current runtime: START a
  # streamer for any newly-discovered node id, and TEAR DOWN a node that has
  # vanished from discovery (drop its capacity row, kill its streamer, forget its
  # runtime). A node that is still present is left exactly as-is (its stream, health
  # and backoff untouched), so a steady fleet costs nothing. This is the C4 interim
  # bridge: a poll-and-reconcile, not a live EndpointSlice watch (PR-H replaces the
  # seam with CP-managed per-node Deployments). A discover_fun that errors (returns
  # a non-list) leaves the set unchanged rather than tearing every node down.
  defp reconcile_discovered(%{discover_fun: nil} = state), do: state

  defp reconcile_discovered(state) do
    case safe_discover(state.discover_fun) do
      {:ok, discovered} ->
        desired = for %{id: id, address: address} <- discovered, into: %{}, do: {id, address}
        current_ids = MapSet.new(Map.keys(state.node_runtime))
        desired_ids = MapSet.new(Map.keys(desired))

        # New ids: seed + open a streamer.
        state =
          Enum.reduce(MapSet.difference(desired_ids, current_ids), state, fn id, acc ->
            add_discovered_node(acc, id, desired[id])
          end)

        # Vanished ids: tear down.
        state =
          Enum.reduce(MapSet.difference(current_ids, desired_ids), state, fn id, acc ->
            remove_discovered_node(acc, id)
          end)

        # Ids present in BOTH but whose ADDRESS changed (a DaemonSet pod roll keeps
        # the stable node id but changes the pod IP). The stored address would never
        # otherwise be compared, so reconnect and the Prime/Assign channel would
        # re-dial the DEAD OLD IP forever. Re-point the node: tear down the old
        # streamer, re-add at the new address, and propagate to NodeChannel.
        MapSet.intersection(desired_ids, current_ids)
        |> Enum.reduce(state, fn id, acc ->
          if acc.node_runtime[id].address != desired[id] do
            acc
            |> remove_discovered_node(id)
            |> add_discovered_node(id, desired[id])
          else
            acc
          end
        end)

      :error ->
        state
    end
  end

  defp safe_discover(discover_fun) do
    case discover_fun.() do
      nodes when is_list(nodes) -> {:ok, nodes}
      _ -> :error
    end
  rescue
    e ->
      Logger.warning("embervm node registry: discovery raised: #{inspect(e)}")
      :error
  end

  # Propagate a node's address to the Prime/Assign channel holder, swallowing any
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

  # Default: point the singleton Embervm.NodeChannel at the node's new address.
  defp default_channel_update(node_id, address) do
    Embervm.NodeChannel.update_address(node_id, address)
    :ok
  end

  # Propagate a discovered node add/remove to the BaseBuilder, swallowing any error
  # (a BaseBuilder that is not running, a slow call): the fleet still gets WatchNode
  # capacity via our own streamers, and the next discover tick re-notifies. Never
  # crash the reconcile on a BaseBuilder hiccup.
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

  # Default: add/remove the node on the singleton Embervm.BaseBuilder so BuildBase
  # placement learns the discovered fleet.
  defp default_base_builder_update({:add, node_id, address}) do
    Embervm.BaseBuilder.add_node(node_id, address)
    :ok
  end

  defp default_base_builder_update({:remove, node_id}) do
    Embervm.BaseBuilder.remove_node(node_id)
    :ok
  end

  # Add a freshly-discovered node (or re-add one at a NEW address after a roll):
  # seed its runtime (mirroring init's shape), propagate the address to the
  # Prime/Assign channel holder so the hot path dials the current endpoint, and
  # open its streamer, which pushes SyncRegistry + consumes WatchNode like any
  # other node.
  defp add_discovered_node(state, node_id, address) do
    now = state.clock.()

    rt = %{
      configured_id: node_id,
      address: address,
      streamer: nil,
      backoff_ms: state.base_backoff_ms,
      watch_started_at: now,
      last_status_at: nil,
      started_at: now,
      health: :starting,
      draining: false
    }

    Logger.info("embervm node registry: discovered node #{node_id} (#{address})")
    safe_channel_update(state.channel_updater_fun, node_id, address)
    safe_base_builder_update(state.base_builder_updater_fun, {:add, node_id, address})

    state
    |> put_in([:node_runtime, node_id], rt)
    |> start_streamer(node_id)
  end

  # Remove a node that vanished from discovery: retract its capacity row, tell the
  # BaseBuilder to drop it, kill its streamer if any (forgetting the pid first so
  # the ensuing DOWN is ignored), and drop its runtime so no reconnect resurrects
  # it. (NodeChannel needs no explicit removal: it lazy-dials and invalidates a
  # dead channel on the next transport error, so a stale map entry is harmless.)
  defp remove_discovered_node(state, node_id) do
    Logger.info("embervm node registry: node #{node_id} vanished from discovery; tearing down")
    safe_base_builder_update(state.base_builder_updater_fun, {:remove, node_id})
    state = retract_capacity(state, node_id)

    state =
      case state.node_runtime[node_id] do
        %{streamer: {pid, _ref}} ->
          state = forget_streamer(state, pid, node_id)
          Process.exit(pid, :kill)
          state

        _ ->
          state
      end

    %{state | node_runtime: Map.delete(state.node_runtime, node_id)}
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
