defmodule Embervm.Application do
  @moduledoc """
  Root OTP application for the EmberVM control plane.

  R0 skeleton: the supervision tree holds the op-log (durable task-state
  seam), the ETS hot-set task store built on top of it, the Finch HTTP-client
  pool (K8s API calls: TokenReview in Task 8, the Workload CRD reconcile loop
  here in Task 5), the `Embervm.WorkloadWatcher` reconciler, and the
  Bandit-hosted `Embervm.Router` serving `/healthz` (and the `/v1` task routes
  in Task 8). Later tasks add children under this same tree (the
  NodeRegistry, the Dispatcher), which is exactly why the control plane is on
  the BEAM: each of those is a supervised process with its own failure
  domain, restarted in isolation.

  Strategy is `:rest_for_one`, not `:one_for_one`: `Embervm.TaskStore` reads
  the op-log once on init to rebuild its ETS tables and otherwise depends on
  it being alive for every write. If `Embervm.OpLog.SQLite` crashes and
  restarts, `:rest_for_one` also restarts every child listed after it, so the
  rebuild-on-boot path runs again against the op-log's post-restart state
  instead of leaving ETS stale against an op-log that came back with (in the
  worst case) a different in-memory connection. The ordering also puts `Finch`
  and the `Bandit` listener LAST, after both the op-log and the task store: the
  router's handlers call `TaskStore` (and, in Task 8, an auth reviewer over
  Finch), so the HTTP surface must not start accepting requests until its
  dependencies are up. `Finch` itself has no dependency on the op-log, but
  sitting before `Bandit` guarantees the client pool exists the moment the
  router can be hit.
  """
  use Application
  require Logger

  @impl true
  def start(_type, _args) do
    port = http_port()

    # Quota + usage-admin config into app-env BEFORE the supervisor starts, so the
    # Dispatcher (reads the quota budgets at init) and the Router (reads them and
    # the usage-admin list per request) see them. Empty budgets = quota off.
    Application.put_env(:embervm, :quota, quota_config())
    Application.put_env(:embervm, :usage_admins, usage_admins())
    Embervm.SpecTrace.configure()

    # The noded ServiceAccount username the router authenticates dial-home
    # registrations against (R0 PR-2), from EMBERVM_NODED_SERVICE_ACCOUNT. Set
    # BEFORE the supervisor starts so the Router (reads it per request) sees it.
    # Empty ("") accepts any valid ServiceAccount token (a permissive fallback for
    # a cluster that has not pinned the SA); it never accepts an invalid token.
    Application.put_env(:embervm, :noded_service_account, trimmed_env("EMBERVM_NODED_SERVICE_ACCOUNT"))

    # Composite-group (R5) capacity from the chart env into app-env BEFORE the
    # supervisor starts, so Embervm.WorkloadWatcher (reads them at init) sees an
    # operator's override of compositeTcpPortRange / maxGroupSize. Absent or
    # malformed env falls back to the watcher's own compile-time defaults (they are
    # NOT put here in that case, so the watcher's Application.get_env default fires).
    put_composite_group_config()

    # The periodic BaseBuilder catalog-resync cadence (RCA H1 self-heal, see
    # Embervm.WorkloadWatcher's @moduledoc): from EMBERVM_WORKLOAD_RESYNC_INTERVAL_MS
    # into app-env BEFORE the supervisor starts, exactly like the composite-group
    # config above. Absent or malformed env is left UNSET, so the watcher's own
    # Application.get_env default (60s) fires.
    put_workload_resync_config()

    # Brick capacity (PR-3): the per-size-class desired replica counts + the brick
    # Deployment name prefix from the chart env into app-env BEFORE the supervisor
    # starts, so Embervm.BrickController reads them at init. Absent env (bricks
    # disabled) leaves both UNSET, so the controller's Application.get_env defaults
    # (an empty class list, an empty prefix) fire and it reconciles nothing.
    put_brick_config()

    children = [
      # The sync-wait waiter registry + park-count ETS owner come FIRST: every
      # terminal task-state write in TaskStore calls Embervm.SyncWait.notify,
      # which dispatches through this registry, so it must already exist. Both
      # are dependency-free and effectively never crash, so leading the
      # rest_for_one chain costs nothing.
      {Registry, keys: :duplicate, name: Embervm.TaskWaiters},
      Embervm.SyncWait,
      op_log_child_spec(),
      # The async lifecycle-write queue (ADR embervm/014 decision 2), gated by
      # EMBERVM_ASYNC_LIFECYCLE_WRITES. Placed AFTER the op-log (it appends through
      # it) and BEFORE TaskStore/SessionStore/SessionManager (they enqueue their
      # off-hot-path :assigned/:started and session_created/session_relit appends
      # here). Owns no ETS and effectively never crashes, so leading the stores in
      # the rest_for_one chain costs nothing; it drains on graceful shutdown so a CP
      # roll loses no pending append. Started unconditionally: with the gate OFF it
      # receives no work (the stores keep write-through ordering), so it is inert.
      Embervm.AsyncWriter,
      Embervm.SpecTrace,
      # TaskStore fires on_metered after a :succeeded/:failed op with usage lands,
      # so Embervm.Metering charges the quota cache off the same durable write.
      # async_writer + async_lifecycle_writes wire the gated off-hot-path append of
      # :assigned/:started (dispatch); OFF (default) keeps write-through ordering.
      {Embervm.TaskStore,
       [
         op_log_mod: op_log_mod(),
         on_metered: &Embervm.Metering.on_metered/1,
         async_writer: Embervm.AsyncWriter,
         async_lifecycle_writes: async_lifecycle_writes_enabled()
       ]},
      # Finch (the shared HTTP pool, TLS-pinned to the K8s CA in-cluster) before
      # Embervm.Auth, whose TokenReview reviewer dials the API server over it.
      Embervm.K8s.finch_child_spec(),
      {Embervm.Auth, allowed: allowed_service_accounts()},
      # The base builder (Task 10): drives the node daemon's BuildBase RPC on
      # Workload admission / spec change and writes status.snapshotRef +
      # Ready/BaseBuilt conditions. Placed BEFORE the WorkloadWatcher on purpose:
      # the watcher's boot LIST casts each valid Workload into the builder, so
      # the builder must already be up to receive them, and under :rest_for_one a
      # builder restart also restarts the watcher, whose re-LIST re-drives the
      # (idempotent) reconcile. It writes status over the Finch pool above and
      # dials the daemon over its own Mint gRPC connection (per build), so it
      # depends on Finch but not on the watcher or node registry. Empty node
      # config (no daemon wired) means it holds descriptors and builds nothing.
      {Embervm.BaseBuilder,
       nodes: configured_nodes(),
       runtime_images: configured_runtime_images(),
       retention_sweep_enabled: base_retention_sweep_enabled(),
       retention_disk_driven_enabled: base_retention_disk_driven_enabled(),
       remote_retention_sweep_enabled: base_remote_retention_sweep_enabled(),
       op_log: op_log_mod(),
       op_log_mod: op_log_mod()},
      # The Workload informer (Task 5): LISTs then WATCHes Workload CRs over the
      # Finch pool above and writes Embervm.WorkloadCatalog, which
      # TaskStore.cfg_for/1 reads. Placed after Finch (its watch streams over
      # that pool) and after TaskStore in the supervision list; TaskStore does
      # not depend on the watcher being up (WorkloadCatalog.retry_config/1
      # tolerates the catalog table not existing yet), so their relative order
      # here is not load-bearing.
      Embervm.WorkloadWatcher,
      # The node registry (Task 9): one supervised gRPC stream per configured node
      # daemon, consuming WatchNode into the Embervm.NodeCapacity ETS table the
      # dispatcher (Task 11) reads, and reassigning a downed node's in-flight tasks
      # via Embervm.TaskStore. Placed after TaskStore (its reassignment path calls
      # it) and after WorkloadWatcher; it dials the daemon directly over its own
      # Mint gRPC connection, so it does not depend on the Finch pool. With no node
      # wired (empty address), it supervises an empty node list and does nothing.
      {Embervm.NodeRegistry, node_registry_opts()},
      # The shadow reservation ledger must be up before any claim site can use it.
      # It is placed before the readers so a ledger restart also bounces them
      # rather than leaving them reading a dead ETS table.
      {Embervm.Scheduler.Reservation, []},
      # The shared per-node gRPC channel holder (Task 11): one long-lived, reused
      # Mint channel per node for the Prime/Assign hot path (unlike NodeRegistry/
      # BaseBuilder, which each own their own channel and can afford a per-op
      # connect). Lazy-dials and caches in persistent_term for lock-free worker
      # reads; workers invalidate a channel on a transport error. With no node
      # wired it caches nothing.
      {Embervm.NodeChannel, nodes: configured_nodes()},
      # The gRPC connection orchestrator sweeper (issue #4419): a periodic GC that
      # reaps leaked GRPC.Client.Connection processes that hold dead addresses or are
      # wedged in initialization. Two classes of leak occur when an instance expires:
      # responsive orchestrators retrying dead addresses forever, and orchestrators
      # stuck in :proc_lib.sync_start that never enter their GenServer loop. Both are
      # reaped by exit signal (DynamicSupervisor.terminate_child), which works for
      # wedged processes that GenServer.call based disconnect cannot reach. Placed
      # AFTER NodeRegistry (whose live address set it reads to form the keep-set) and
      # AFTER NodeChannel (which reads the same node config). Duplicates on live
      # addresses are kept (expected when multiple components dial independently); the
      # predicate is "target not in live set", never "more than one per address". Gate
      # OFF by default (EMBERVM_GRPC_CONNECTION_SWEEP_ENABLED): the sweep runs and logs
      # but does not terminate, so merging is inert and arming is a separate values
      # change after observing the logs.
      {Embervm.GrpcConnectionSweeper, grpc_connection_sweeper_opts()},

      # Metering, audit, and quotas (Task 12). Owns the public per-principal daily
      # quota-cache ETS table (rebuilt on boot from the op-log's usage projection)
      # and appends request-scoped denials. Placed AFTER OpLog.SQLite (it reads the
      # usage projection on boot and appends denials) and BEFORE the Dispatcher and
      # Router, which read its table and quota budgets: under :rest_for_one a
      # Metering restart bounces the dispatcher (which rebuilds from backlog) and
      # Bandit, the same price BaseBuilder/NodeRegistry already pay for their
      # ordering. The TaskStore charge hook targets the module (the public table),
      # not the process, so TaskStore may start earlier.
      {Embervm.Metering, [op_log_mod: op_log_mod()]},
      # The dispatcher (Task 11): the heart of R0. Owns the per-workload fair
      # queues, the primed-VM inventory, the enforcement caps, and drives queued
      # tasks to terminal via Assign. Placed AFTER TaskStore (drives its FSM +
      # is the target of its on_queued hook), NodeRegistry (reads NodeCapacity),
      # WorkloadWatcher (reads WorkloadCatalog), NodeChannel (the assign channel),
      # and Metering (reads its quota table + budgets). Under :rest_for_one a
      # dispatcher restart loses its in-memory queues, which its boot backlog-sweep
      # rebuilds from TaskStore.
      {Embervm.Dispatcher, dispatcher_opts()},
      # The pool manager (Task 11): the background refill loop keeping `floor`
      # VMs primed per workload (floor-first, then proportional to queue depth),
      # depositing primed vm_ids into the dispatcher's inventory and owning
      # status.primedFloorSatisfied. After the dispatcher (it deposits into it)
      # and after the node registry/watcher (it reads capacity + catalog). Primes
      # nothing in R0 (no base is ready until guest images land, Task 14).
      {Embervm.PoolManager, pool_opts()},
      # The cron trigger adapter (Task 11): fires each Workload's spec.triggers[]
      # cron as an ordinary submit (principal system:cron:<workload>); misfires
      # during downtime are skipped, not replayed. After the watcher (reads the
      # catalog's triggers) and TaskStore (submits into it).
      Embervm.Trigger.Cron,
      # The brick controller (brick-capacity PR-3): reconciles each size-class
      # brick Deployment's replica count to its desired value and flags a class
      # fleet-full when desired outruns registered. AFTER Finch (it PATCHes the
      # apiserver /scale subresource through it) and after the node registry (it
      # reads registered bricks from the capacity ledger). Inert while
      # bricks.enabled=false renders no classes into its config.
      {Embervm.BrickController, brick_controller_opts()},
      # Session lifecycle (R2). The SessionStore (ETS hot set over the durable
      # `sessions` projection, rebuilt on boot) comes first; the SessionRegistry
      # (session_id -> live Embervm.Session pid) and the SessionSupervisor
      # (DynamicSupervisor for the per-live-session processes) next; the
      # SessionManager (create/destroy/route brain) last, since it starts children
      # into the supervisor and reads the store/registry. Placed AFTER Metering
      # (create reads the quota table + budgets), the Dispatcher (create CLAIMs a
      # primed VM from its inventory), and NodeChannel (the SessionAssign channel),
      # and BEFORE the Router (its session handlers call the manager + store). Under
      # :rest_for_one a SessionStore restart bounces the manager and Router, which
      # rebuild from the durable projection. With no node wired, create denies
      # :no_capacity and nothing runs, exactly like the dispatcher in R0.
      {Embervm.SessionStore,
       [
         op_log_mod: op_log_mod(),
         on_metered: &Embervm.Metering.on_metered/1,
         async_writer: Embervm.AsyncWriter,
         async_lifecycle_writes: async_lifecycle_writes_enabled()
       ]},
      {Registry, keys: :unique, name: Embervm.SessionRegistry},
      {DynamicSupervisor, strategy: :one_for_one, name: Embervm.SessionSupervisor},
      {Embervm.SessionManager, session_manager_opts()},
      # Serving lifecycle (R3). The ServingStore (ETS hot set over the durable
      # `serving_instances` projection, rebuilt on boot) comes first; the
      # EndpointPublisher (the SOLE writer to the xDS sidecar, deriving the fan-out
      # from ServingStore facts + the WorkloadCatalog) next. Placed AFTER the
      # WorkloadWatcher (the publisher reads serving-class catalog entries for
      # routes) and NodeRegistry (the publisher derives target serving nodes from
      # NodeCapacity's serving_subnet_cidr facts), and BEFORE the Router (its
      # GET /v1/serving handler reads the store). Under :rest_for_one a ServingStore
      # restart bounces the publisher and Router, which rebuild from the durable
      # projection and re-derive the fan-out. The publisher does one synchronous
      # boot publish before readiness, so a control-plane restart republishes the
      # rebuilt endpoints before any request can arrive; with no serving node wired
      # it pushes nothing (a clean no-op). The activator endpoint (Task 8) and the
      # xds http port are wired from the chart.
      {Embervm.ServingStore, [op_log_mod: op_log_mod()]},
      # Stateful lifecycle (R4). The StatefulStore (ETS hot set over the durable
      # `stateful_instances` + `volumes` projections, rebuilt on boot) is the L4
      # singleton-sandbox counterpart of the ServingStore. Placed BEFORE the
      # EndpointPublisher, which reads its `published_endpoint/1` for every stateful
      # workload's L4 cluster, so under :rest_for_one a StatefulStore restart bounces
      # the publisher (which re-derives the fan-out from the rebuilt projection). Like
      # ServingStore it depends only on the op-log (started far earlier).
      {Embervm.StatefulStore, [op_log_mod: op_log_mod()]},
      # Composite-group hot set (R5): the L4 composite counterpart of StatefulStore,
      # placed BEFORE the EndpointPublisher, which reads its `entry_endpoint/2` for
      # every composite workload's L4 cluster on its boot publish. Under :rest_for_one
      # a GroupStore restart bounces the publisher (which re-derives the fan-out from
      # the rebuilt `group_instances` + `group_members` projection). Depends only on
      # the op-log (started far earlier).
      {Embervm.GroupStore, [op_log_mod: op_log_mod()]},
      {Embervm.EndpointPublisher, endpoint_publisher_opts()},
      # The activator (R3, Task 8): the serving miss brain. It is the fallback
      # endpoint of an empty serve|<workload> cluster (a request the node Envoy
      # routes to the control plane IS a miss), single-flights the wake
      # (StartServing relight or cold create), publishes the fresh endpoint via the
      # EndpointPublisher, and resolves the parked caller so the router proxies the
      # one miss request to the VM. Placed AFTER ServingStore + EndpointPublisher (it
      # mutates the store and asks the publisher to re-push) and BEFORE the Router
      # (whose activator route calls it). Its adoption reconcile runs on boot +
      # timer, reconciling live serving VMs / banked snapshots from node facts so a
      # control-plane restart republishes exactly the same endpoints without touching
      # any VM. With no serving node wired, a miss denies :no_capacity.
      {Embervm.ServingManager, serving_manager_opts()},
      # The serving lifecycle-economics sweeper (R3, Task 9): the idle-to-bank /
      # scale-to-zero / max-lifetime / banked-TTL loop, plus the forced-roll verb the
      # Router's DELETE /v1/serving/:name/instances calls. It scrapes each node Envoy's
      # per-cluster request counters (the idle signal) and drives drain-before-bank.
      # Placed AFTER ServingStore/EndpointPublisher (it mutates the store + re-pushes)
      # and BEFORE the Router (whose forced-roll handler calls it). With no serving node
      # wired (no stats_base) every tick fails open and banks nothing.
      {Embervm.ServingSweeper, serving_sweeper_opts()},
      # The stateful wake brain (R4, Task 8): the L4 counterpart of ServingManager,
      # and the rung's headline verb. It is the fallback endpoint of an empty
      # state|<workload> cluster (an inbound TCP connection the node Envoy routes
      # to Embervm.TcpActivator IS a miss for that workload, resolved by the
      # LOCAL ACCEPT PORT, decision 5), single-flights the wake (StartStateful
      # relight or cold/fresh boot on the volume), publishes the fresh endpoint
      # via the EndpointPublisher, and resolves the parked connection so the
      # activator splices bytes to the VM. Placed AFTER StatefulStore +
      # EndpointPublisher (it mutates the store and asks the publisher to
      # re-push) and BEFORE TcpActivator (which calls it) and the Router (whose
      # DELETE /v1/stateful/:name/instance + /volume handlers call it). Its
      # adoption reconcile runs on boot + timer, reconciling live stateful VMs /
      # banked bundles / volume facts from node facts so a control-plane restart
      # republishes exactly the same endpoint without touching any VM. With no
      # stateful node wired, a wake denies :no_capacity.
      {Embervm.StatefulManager, stateful_manager_opts()},
      # The composite-group supervisor (R5): the DynamicSupervisor + Registry owning
      # one Embervm.GroupManager process per LIVE group instance. The GroupWakeManager
      # calls create_group/2 + wake_group/3 here on a wake-on-connect miss. Placed
      # AFTER GroupStore + EndpointPublisher (a GroupManager mutates the store and asks
      # the publisher to re-push) and BEFORE the GroupWakeManager + TcpActivator that
      # drive it. The per-group config (supernet, port_base, pod_ip, node funs) is
      # threaded via the :defaults opt from the chart env; with no composite supernet
      # wired a create denies (a clean no-op).
      {Embervm.GroupManager.Supervisor, group_manager_supervisor_opts()},
      # The composite-group wake brain (R5, Task 7): the group counterpart of
      # StatefulManager. It is the fallback endpoint of an empty group|<workload>
      # cluster (a TCP connection the node Envoy routes to the TcpActivator on a
      # composite entry.listenPort IS a group miss, resolved by the LOCAL ACCEPT
      # PORT, decision 5), single-flights the wake (relight a complete banked set, or
      # fresh-boot / create when the set is partial or absent, decision 8), publishes
      # the entry endpoint via the EndpointPublisher, and resolves the parked
      # connection so the activator splices bytes to the entry member. Placed AFTER
      # GroupStore + EndpointPublisher + GroupManager.Supervisor (it drives them) and
      # BEFORE TcpActivator (which calls wake/3). Its adoption reconcile runs on boot
      # + timer, reconciling live members / bundle sets / networks from node facts so
      # a control-plane restart republishes exactly the same entry endpoint without
      # touching any VM. With no composite node wired, a wake denies :no_capacity.
      {Embervm.GroupWakeManager, group_wake_manager_opts()},
      # The composite activator live-splice counter (R5, Task 8): a per-workload ETS
      # count of the byte-pump splices the TcpActivator holds open for a group's entry
      # member. A splice that began during a wake never re-enters the entry listener's
      # Envoy cx_active counter, so the GroupSweeper's idle predicate reads this to
      # avoid banking a group with a live session (standing decision 7). Placed BEFORE
      # TcpActivator (which increments it per composite splice) and GroupSweeper (which
      # reads it); it owns only a public named ETS table, so a restart is cheap.
      {Embervm.ActivatorSplices, []},
      # The L4 TCP activator (R4, Task 8; R5 composite): binds the values-declared
      # stateful (5400-5409) AND composite (5410-5419) port ranges and dispatches an
      # accepted connection to StatefulManager.wake/3 or GroupWakeManager.wake/3
      # (single-flighted there) by the accept port's class, then splices bytes
      # bidirectionally between the client and the woken VM/group entry. Placed AFTER
      # StatefulManager + GroupWakeManager (it calls their wake/3) and LAST among the
      # wake children: it is the front door, nothing depends on it being up. With an
      # empty port range (the default; the chart wires the ranges) it binds nothing, a
      # clean no-op.
      {Embervm.TcpActivator, tcp_activator_opts()},
      # The stateful lifecycle-economics sweeper (R4, Task 9): the L4 idle-to-bank /
      # max-lifetime / banked-TTL loop, the counterpart to Embervm.ServingSweeper. It
      # scrapes the SAME node Envoy stats port for per-listener L4 connection counters
      # (downstream_cx_active/total, keyed by the state-<listenPort> stat_prefix) rather
      # than serving's per-cluster request counters. Placed AFTER StatefulStore/
      # EndpointPublisher (it mutates the store + re-pushes) and AFTER StatefulManager +
      # TcpActivator (banking must not race an in-flight wake's own transition, and this
      # ordering means a StatefulSweeper restart under :rest_for_one does not bounce the
      # wake path). With no stats_base wired (reuses EMBERVM_SERVING_STATS_BASE) every
      # tick fails open and banks nothing, mirroring ServingSweeper's no-op default.
      {Embervm.StatefulSweeper, stateful_sweeper_opts()},
      # The composite-group lifecycle-economics sweeper (R5, Task 8): the group
      # idle-to-bank / max-lifetime / banked-TTL loop, plus the forced-roll verb the
      # Router's DELETE /v1/groups/:name/instance calls. It scrapes the SAME node Envoy
      # stats port for the group's entry listener L4 connection counters (keyed by the
      # group-<entry.listenPort> stat_prefix), ANDs in the ActivatorSplices count (the
      # session-splice signal Envoy cannot see), excludes degraded groups from banking
      # (decision 11), and drives GroupManager.Supervisor.bank_group/2. Placed AFTER
      # GroupStore/EndpointPublisher (it mutates + re-pushes) and AFTER the
      # GroupManager.Supervisor + GroupWakeManager + ActivatorSplices it drives/reads.
      # With no stats_base wired (reuses EMBERVM_SERVING_STATS_BASE) every tick fails
      # open and banks nothing, mirroring StatefulSweeper's no-op default.
      {Embervm.GroupSweeper, group_sweeper_opts()},
      # The drain coordinator (R6 Continuity, ADR embervm/009): NodeRegistry sends it
      # {:node_draining, node_id, pod_uid, deadline_ms} on the drain rising edge (the
      # drain scopes to the INSTANCE, R0 PR-2), and it fans
      # out drain_node/2 to the four class sweepers to force-bank the node's live
      # instances within the bounded-preemption window. Placed AFTER all four sweepers
      # it drives (so under :rest_for_one a sweeper restart does not orphan it) and
      # AFTER NodeRegistry (which finds it by registered name to send the edge event).
      # It holds no durable state; a restart only misses an in-flight drain edge, which
      # the daemon's own deadline reap backstops.
      {Embervm.DrainCoordinator, drain_coordinator_opts()},
      # The reconciled warmth-retention reaper (base-durability PR-3, extended to
      # STATEFUL bundles and GROUP sets): the structural analogue of the BaseBuilder
      # base-retention sweep, reconciling each node's REPORTED warmth inventory
      # (NodeStatus.stateful_bundles / group_bundle_sets, projected into NodeCapacity)
      # against the CP's non-terminal instance set and evicting the orphans the
      # event-driven sweepers can never reach (an instance the CP no longer tracks
      # fires no FSM trigger). Placed AFTER StatefulStore/GroupStore + the two
      # sweepers it reads-alongside (never mutates their FSM; it only evicts orphans
      # neither owns) and AFTER NodeRegistry (whose capacity projection it reads). It
      # holds no durable state; a restart only misses a tick the next one re-runs.
      # Gated OFF by default (EMBERVM_WARMTH_RETENTION_SWEEP): merging is inert (the
      # sweep runs but only LOGS what it would evict, deleting nothing).
      {Embervm.WarmthReaper, warmth_reaper_opts()},
      # The S3-direct warmth GC (task #39): the third GC arm, enumerating warmth
      # prefixes from the OBJECT STORE itself. The pre-sidecar orphan backlog is
      # invisible to both the sweepers and the WarmthReaper (its workload
      # binding was lost on a boot scan, so no node can even compose its S3
      # prefix); only a bucket listing can find it. Placed AFTER the stores +
      # NodeRegistry its fail-closed predicate consults (StatefulStore /
      # GroupStore / NodeCapacity) and after the WarmthReaper it complements
      # (disjoint by construction: the GC excludes anything node-reported).
      # Every periodic sweep is a logged dry-run plan + persisted manifest; the
      # destructive arm is gated OFF by default (EMBERVM_WARMTH_S3_GC), so
      # merging is inert. It holds no durable state.
      {Embervm.S3WarmthGc, s3_warmth_gc_opts()},
      # The op-log sweeper (ADR embervm/002): scheduled bounded-batch compaction of
      # the durable projection tables + ops-journal prefix. Placed LATE, right before
      # Bandit: it depends ONLY on the op-log (which starts early), so under
      # :rest_for_one a Compactor crash restarts only Bandit, the minimum blast
      # radius. It adds no writer (each batch is a call to the op-log's single
      # writer). Correctness (read-time TTLs) does not depend on it; it reclaims disk.
      {Embervm.OpLog.Compactor,
       op_log: op_log_mod(), op_log_mod: op_log_mod(), interval_ms: sweep_interval_ms()},
      # Bandit + the router last: its handlers call Auth, TaskStore, SyncWait, and
      # the dispatcher's admit? gate, so the HTTP surface must not accept requests
      # until all are up.
      {Bandit, plug: Embervm.Router, scheme: :http, port: port},
      # This is deliberately the final child. A measurement process must never
      # be able to take down the control plane it is measuring: under
      # :rest_for_one, an observer crash restarts nothing else.
      {Embervm.CapacityObserver, capacity_observer_opts()}
    ]

    opts = [strategy: :rest_for_one, name: Embervm.Supervisor]
    Supervisor.start_link(children, opts)
  end

  # Port comes from the environment (EMBERVM_HTTP_PORT), matching the chart's
  # env wiring; defaults to 8080 for local mix runs.
  defp http_port do
    case System.get_env("EMBERVM_HTTP_PORT") do
      nil -> 8080
      "" -> 8080
      value -> String.to_integer(value)
    end
  end

  # The op-log's durable path comes from EMBERVM_OPLOG_PATH (the chart wires
  # this to the pod's PVC mount); unset (mix test, local runs) falls back to
  # a per-run temp file so nothing collides across processes/nodes and no
  # state leaks between runs. nil here just means "let Embervm.OpLog.SQLite
  # pick its own default", which is that same tmp-file fallback.
  defp oplog_path do
    case System.get_env("EMBERVM_OPLOG_PATH") do
      nil -> nil
      "" -> nil
      path -> path
    end
  end

  # The single source of truth for the op-log backend MODULE, threaded into every
  # store/manager/sweeper's `:op_log_mod` opt so none of them hardcodes the
  # concrete backend. EMBERVM_OPLOG_DSN set and non-empty selects
  # Embervm.OpLog.Postgres (PR-4, #18/#27); unset (the default, and what every
  # cluster runs today) keeps Embervm.OpLog.SQLite. This PR ships the Postgres
  # module dormant: nothing in deploy values sets the DSN yet, so the selected
  # backend never changes as a result of merging it. Public (@doc false) so
  # Embervm.ApplicationTest can assert the selection directly, matching how
  # configured_nodes/0 is exposed for its own boot-ordering test.
  @doc false
  def op_log_mod do
    case trimmed_env("EMBERVM_OPLOG_DSN") do
      "" -> Embervm.OpLog.SQLite
      _dsn -> Embervm.OpLog.Postgres
    end
  end

  # The op-log child spec: SQLite takes its PVC path + journal horizon, Postgres
  # takes the DSN. Kept as one function (rather than inlining a case in the
  # children list) so the selected module and its opts stay next to each other.
  defp op_log_child_spec do
    case op_log_mod() do
      Embervm.OpLog.SQLite ->
        {Embervm.OpLog.SQLite, path: oplog_path(), journal_horizon_ms: journal_horizon_ms()}

      Embervm.OpLog.Postgres ->
        {Embervm.OpLog.Postgres, dsn: trimmed_env("EMBERVM_OPLOG_DSN"), journal_horizon_ms: journal_horizon_ms()}
    end
  end

  # The op-log sweeper cadence (EMBERVM_OPLOG_SWEEP_INTERVAL_MS, the chart wires
  # this from values.opLog.sweepIntervalSeconds x 1000); default hourly. Distinct
  # from the journal horizon below: this is HOW OFTEN we sweep, that is HOW OLD an
  # op must be before it is eligible.
  defp sweep_interval_ms do
    case trimmed_env("EMBERVM_OPLOG_SWEEP_INTERVAL_MS") do
      "" -> 3_600_000
      raw -> String.to_integer(raw)
    end
  end

  # The ops-journal prefix-compaction horizon (EMBERVM_OPLOG_JOURNAL_HORIZON_MS,
  # the chart wires this from values.opLog.journalHorizonDays x 86_400_000);
  # default 30 days. An op younger than this (or owned by a live task) is never
  # compacted. Distinct from the terminal-task 7-day retention baked into the
  # op-log; this bounds the append-only journal, that bounds the tasks projection.
  defp journal_horizon_ms do
    case trimmed_env("EMBERVM_OPLOG_JOURNAL_HORIZON_MS") do
      "" -> 30 * 24 * 60 * 60 * 1000
      raw -> String.to_integer(raw)
    end
  end

  # The STATIC node-daemon seed the registry starts with. Under dial-home (R0
  # PR-2) the fleet arrives via NodeRegistry.register/2 (a POST from each noded
  # instance), so this is usually the EMPTY list; the only static entry is an
  # explicit EMBERVM_NODE_ADDRESS override (a single pinned daemon for tests /
  # out-of-cluster, which cannot dial home). The id falls back to the address when
  # unset, purely for correlation labels.
  #
  # CRITICAL (ADR embervm/012 boot ordering): this runs while the supervisor
  # children list is being BUILT, BEFORE any child (including Finch) is started, so
  # it MUST NOT touch Finch / the K8s API. Under dial-home it never does: it reads
  # only env vars. The old EndpointSlice discovery (which needed Finch post-start)
  # is retired; registration is the only path that dials, and it runs only when the
  # router accepts a POST, well after Finch is up.
  # Public (@doc false) so the boot-ordering regression test can assert it is
  # Finch-free at construction; not part of the module's real API.
  @doc false
  def configured_nodes do
    address = trimmed_env("EMBERVM_NODE_ADDRESS")
    id = trimmed_env("EMBERVM_NODE_ID")

    cond do
      address != "" and id != "" -> [%{id: id, address: address}]
      address != "" -> [%{id: address, address: address}]
      # No pinned override: seed EMPTY. Instances arrive via dial-home
      # registration (NodeRegistry.register/2), never via a boot-time K8s call.
      true -> []
    end
  end

  defp trimmed_env(name) do
    case System.get_env(name) do
      nil -> ""
      value -> String.trim(value)
    end
  end

  # Zip-lane runtime -> pinned runtime base image ref, from EMBERVM_RUNTIME_IMAGES
  # (comma-separated `runtime=ref` pairs the chart renders from the Bazel-pinned
  # runtimePython.guestImage). The BaseBuilder resolves source.zip.runtime through
  # this map. Empty (no runtime image pinned) means zip builds are held with a
  # clear condition rather than crashing.
  defp configured_runtime_images do
    case trimmed_env("EMBERVM_RUNTIME_IMAGES") do
      "" ->
        %{}

      raw ->
        raw
        |> String.split(",", trim: true)
        |> Enum.reduce(%{}, fn pair, acc ->
          case String.split(pair, "=", parts: 2) do
            [runtime, ref] ->
              r = String.trim(runtime)
              v = String.trim(ref)
              if r != "" and v != "", do: Map.put(acc, r, v), else: acc

            _ ->
              acc
          end
        end)
    end
  end

  # Base-durability PR-3: the destructive gate for the BaseBuilder base-retention
  # sweep, from EMBERVM_BASE_RETENTION_SWEEP. UNSET or "0"/"false"/"" (the default,
  # and what this PR ships) => the sweep runs but only LOGS what it would evict,
  # deleting nothing. "1"/"true" => the sweep issues EvictArtifact{remote: false}
  # for each superseded local base outside the desired set (the one-shot ~290G
  # backlog reclaim an operator supervises). Wired here so it flips via a deploy
  # values env change, no code change. Nothing sets it on in this PR.
  defp base_retention_sweep_enabled do
    case trimmed_env("EMBERVM_BASE_RETENTION_SWEEP") do
      v when v in ["1", "true", "TRUE", "True"] -> true
      _ -> false
    end
  end

  # Destructive gate for disk-driven base retention, from
  # EMBERVM_BASE_RETENTION_DISK_DRIVEN. This is deliberately separate from the
  # legacy retention gate because disk enumeration must be reviewed in a live
  # manifest before it is allowed to evict anything.
  defp base_retention_disk_driven_enabled do
    case trimmed_env("EMBERVM_BASE_RETENTION_DISK_DRIVEN") do
      v when v in ["1", "true", "TRUE", "True"] -> true
      _ -> false
    end
  end

  # Destructive gate for REMOTE base retention (#3947 PR-4), from
  # EMBERVM_BASE_REMOTE_RETENTION_SWEEP. UNSET or "0"/"false"/"" => the sweep runs
  # but only LOGS what it would evict. "1"/"true" => it issues
  # EvictArtifact{remote: true, vendor: ...} for each superseded STORE base
  # outside the keep-set.
  #
  # Deliberately SEPARATE from base_retention_sweep_enabled/0 even though the
  # shapes match: the local gate's blast radius is one node's disk, this one's is
  # the shared bucket, so they are armed and rolled back independently. Flipping
  # back to "" is the entire rollback lever.
  defp base_remote_retention_sweep_enabled do
    case trimmed_env("EMBERVM_BASE_REMOTE_RETENTION_SWEEP") do
      v when v in ["1", "true", "TRUE", "True"] -> true
      _ -> false
    end
  end

  # WarmthReaper (base-durability PR-3, extended to stateful + group) config: only
  # the destructive gate is env-driven; the sweep cadence uses the module default.
  # The gate mirrors base_retention_sweep_enabled/0 exactly.
  defp warmth_reaper_opts do
    [sweep_enabled: warmth_retention_sweep_enabled()]
  end

  # The destructive gate for the WarmthReaper warmth-retention sweep, from
  # EMBERVM_WARMTH_RETENTION_SWEEP. UNSET or "0"/"false"/"" (the default, and what
  # this PR ships) => the sweep runs but only LOGS what it would evict, deleting
  # nothing. "1"/"true" => the sweep evicts each orphaned stateful bundle / group
  # set ENTIRELY (local disk AND remote S3). Wired here so it flips via a deploy
  # values env change, no code change. Nothing sets it on in this PR.
  defp warmth_retention_sweep_enabled do
    case trimmed_env("EMBERVM_WARMTH_RETENTION_SWEEP") do
      v when v in ["1", "true", "TRUE", "True"] -> true
      _ -> false
    end
  end

  # S3-direct warmth GC (task #39) config. The store endpoint/bucket mirror
  # noded's EMBERVM_NODED_STORE_* (the chart renders BOTH from the same
  # noded.store values, one source of truth); an empty endpoint leaves the GC
  # inert. The destructive gate parses exactly like the other retention gates.
  # Caps/cadence/freshness are values-overridable; the module carries the
  # supervised-first-run defaults. expected_nodes is the fleet contract the
  # fleet-freshness abort checks against: EMPTY (the chart default) means the
  # sweep always aborts, so even the dry-run plan requires an operator to have
  # named the fleet.
  defp s3_warmth_gc_opts do
    [
      enabled: warmth_s3_gc_enabled(),
      endpoint: trimmed_env("EMBERVM_STORE_ENDPOINT"),
      bucket: store_bucket(),
      expected_nodes: warmth_s3_gc_expected_nodes(),
      allow_empty_kinds: warmth_s3_gc_allow_empty_kinds(),
      max_prefixes: int_env_or_nil("EMBERVM_WARMTH_S3_GC_MAX_PREFIXES"),
      max_bytes: int_env_or_nil("EMBERVM_WARMTH_S3_GC_MAX_BYTES"),
      ttls: warmth_s3_gc_ttls(),
      sweep_interval_ms: int_env_or_nil("EMBERVM_WARMTH_S3_GC_INTERVAL_MS"),
      freshness_window_ms: int_env_or_nil("EMBERVM_WARMTH_S3_GC_FRESHNESS_MS"),
      min_uptime_ms: int_env_or_nil("EMBERVM_WARMTH_S3_GC_MIN_UPTIME_MS")
    ]
    |> Enum.reject(fn {_k, v} -> is_nil(v) end)
  end

  defp warmth_s3_gc_ttls do
    %{
      stateful: int_env_or_nil("EMBERVM_WARMTH_S3_GC_STATEFUL_TTL_MS"),
      session: int_env_or_nil("EMBERVM_WARMTH_S3_GC_SESSION_TTL_MS"),
      serving: int_env_or_nil("EMBERVM_WARMTH_S3_GC_SERVING_TTL_MS"),
      session_workspace: int_env_or_nil("EMBERVM_WARMTH_S3_GC_SESSION_WORKSPACE_TTL_MS"),
      group: int_env_or_nil("EMBERVM_WARMTH_S3_GC_GROUP_SET_TTL_MS")
    }
    |> Enum.reject(fn {_kind, ttl} -> is_nil(ttl) end)
    |> Map.new()
  end

  defp store_bucket do
    case trimmed_env("EMBERVM_STORE_BUCKET") do
      "" -> "embervm"
      bucket -> bucket
    end
  end

  # The destructive gate for the S3-direct warmth GC, from EMBERVM_WARMTH_S3_GC.
  # UNSET or "0"/"false"/"" (the default, and what this PR ships) => every sweep
  # computes, logs, and manifests the plan but deletes NOTHING. "1"/"true" =>
  # the gated delete arm fires (supervised first run via :sweep_now). Mirrors
  # warmth_retention_sweep_enabled/0 exactly.
  defp warmth_s3_gc_enabled do
    case trimmed_env("EMBERVM_WARMTH_S3_GC") do
      v when v in ["1", "true", "TRUE", "True"] -> true
      _ -> false
    end
  end

  # The comma-separated fleet contract (EMBERVM_WARMTH_S3_GC_EXPECTED_NODES,
  # e.g. "node-1,node-2,node-3,node-4"): every named node must be present AND
  # fresh in NodeCapacity or the sweep aborts. Empty = always abort.
  defp warmth_s3_gc_expected_nodes do
    trimmed_env("EMBERVM_WARMTH_S3_GC_EXPECTED_NODES")
    |> String.split(",", trim: true)
    |> Enum.map(&String.trim/1)
    |> Enum.reject(&(&1 == ""))
  end

  defp warmth_s3_gc_allow_empty_kinds do
    parse_allow_empty_kinds(trimmed_env("EMBERVM_WARMTH_S3_GC_ALLOW_EMPTY_KINDS"))
  end

  # Public for tests. Unknown tokens are LOGGED AND DROPPED, never raised:
  # a dropped token leaves the empty-store guard at full strength (the abort
  # still fires), while a raise here runs during the supervisor child-spec
  # build and would crash-loop the whole control plane on a values typo (the
  # EMBERVM_BRICK_CLASSES convention: a bad value leaves the feature inert,
  # never the control plane down). The chart also fail()s on unknown tokens
  # at render time, so a GitOps typo is caught before it ever deploys.
  @doc false
  def parse_allow_empty_kinds(value) do
    allowed = %{"stateful" => :stateful, "group" => :group, "session" => :session, "serving" => :serving}

    value
    |> String.split(",", trim: true)
    |> Enum.map(&String.trim/1)
    |> Enum.reject(&(&1 == ""))
    |> Enum.flat_map(fn token ->
      case Map.fetch(allowed, token) do
        {:ok, kind} ->
          [kind]

        :error ->
          Logger.warning(
            "embervm s3 warmth gc: ignoring unknown allow-empty kind #{inspect(token)} " <>
              "(valid: stateful, group, session, serving); the empty-store guard stays armed"
          )

          []
      end
    end)
  end

  # Dispatcher tuning from the chart (values -> env). The share fraction, when
  # set, caps each principal at that fraction of a workload's cap; unset (the
  # default) means the dynamic cap/active-principals split.
  defp dispatcher_opts do
    [queue_depth_cap: queue_depth_cap(), op_log: op_log_mod(), op_log_mod: op_log_mod()] ++ share_fraction_opt()
  end

  defp pool_opts, do: [op_log: op_log_mod(), op_log_mod: op_log_mod()]

  # Extra opts threaded into every Embervm.Session the SessionManager starts. Empty
  # in production (the session process uses its real NodeChannel/SessionAssign
  # defaults); tests inject fake daemon seams here.
  defp session_opts, do: []

  # SessionManager config: the session-process seams plus the R2 policy knobs the
  # chart wires from values (the reconcile/sweep cadences, the snapshot-disk low
  # watermark for LRU eviction, and the per-principal wake-rate limit). Defaults keep
  # the timers ON in production; a 0 disables the corresponding sweep.
  defp session_manager_opts do
    [
      op_log_mod: op_log_mod(),
      session_opts: session_opts(),
      reconcile_interval_ms: session_reconcile_interval_ms(),
      sweep_interval_ms: session_sweep_interval_ms(),
      disk_low_watermark_bytes: session_disk_low_watermark_bytes(),
      node_confirmed_destroy: node_confirmed_destroy_enabled(),
      destroying_alarm_ms: destroying_alarm_ms(),
      orphan_grace_ms: orphan_grace_ms(),
      # The Direction-2 adopt-and-backfill discriminator asks AsyncWriter whether a
      # rowless reported VM has a write in flight (ADR embervm/014 decision 2), so it
      # needs both the gate and the writer reference.
      async_lifecycle_writes: async_lifecycle_writes_enabled(),
      async_writer: Embervm.AsyncWriter
    ] ++ wake_opts()
  end

  # EMBERVM_NODE_CONFIRMED_DESTROY (ADR embervm/014 decision 5). UNSET or
  # "0"/"false"/"" (the default, and what this PR ships) => destruction records
  # destroyed first and tears the VM down asynchronously (today's behaviour).
  # "1"/"true" => destruction records a destroying intent, runs the node-confirmed
  # teardown RPC, and records destroyed ONLY on node confirmation, with fail-closed
  # reconciliation toward destruction. Wired here so it flips via a deploy values env
  # change, no code change. Nothing sets it on in this PR.
  defp node_confirmed_destroy_enabled do
    case trimmed_env("EMBERVM_NODE_CONFIRMED_DESTROY") do
      v when v in ["1", "true", "TRUE", "True"] -> true
      _ -> false
    end
  end

  # EMBERVM_ASYNC_LIFECYCLE_WRITES (ADR embervm/014 decision 2). UNSET or
  # "0"/"false"/"" (the default, and what this PR ships) => the boot/wake lifecycle
  # appends (:assigned/:started on dispatch, session_created/session_relit on session
  # boot/wake) stay write-through: the durable oplog append blocks the hot-path caller
  # before the instance is handed back, today's ordering. "1"/"true" => those four
  # appends are deferred to Embervm.AsyncWriter AFTER the in-memory state is advanced
  # (RPC already succeeded), taking the durable write off the hot path; a lost write
  # (CP crash before the async append) is repaired by the adoption backfill. Metering,
  # :submitted, destruction, and bank ops are NEVER deferred. Wired here so it flips
  # via a deploy values env change, no code change. Nothing sets it on in this PR.
  defp async_lifecycle_writes_enabled do
    case trimmed_env("EMBERVM_ASYNC_LIFECYCLE_WRITES") do
      v when v in ["1", "true", "TRUE", "True"] -> true
      _ -> false
    end
  end

  # Alarm threshold for an instance stuck in destroying (EMBERVM_DESTROYING_ALARM_MS);
  # default 5 minutes. The reconcile loop logs error-level with a SigNoz-visible field
  # when a destroying instance persists past this, per the ADR risk table.
  defp destroying_alarm_ms do
    int_env_or_nil("EMBERVM_DESTROYING_ALARM_MS") || 300_000
  end

  # Grace window before fail-closed orphan reconciliation acts
  # (EMBERVM_ORPHAN_GRACE_MS); default 60s. An instance younger than this window is
  # never terminalized/destroyed by reconciliation (it may be mid-boot or racing an
  # async write), per ADR embervm/014 decision 5.
  defp orphan_grace_ms do
    int_env_or_nil("EMBERVM_ORPHAN_GRACE_MS") || 60_000
  end

  # Adoption reconcile cadence (EMBERVM_SESSION_RECONCILE_INTERVAL_MS); default 10s,
  # matching the registry sweep tempo so a restart's residency/limbo heal lands fast.
  defp session_reconcile_interval_ms do
    case trimmed_env("EMBERVM_SESSION_RECONCILE_INTERVAL_MS") do
      "" -> 10_000
      raw -> String.to_integer(raw)
    end
  end

  # TTL/eviction sweep cadence (EMBERVM_SESSION_SWEEP_INTERVAL_MS); default 30s. This
  # is the safety net for expiry/GC/disk-pressure; invoke-time expiry is the primary
  # expiry check (ADR 002 rule 1), so this cadence is not correctness-critical.
  defp session_sweep_interval_ms do
    case trimmed_env("EMBERVM_SESSION_SWEEP_INTERVAL_MS") do
      "" -> 30_000
      raw -> String.to_integer(raw)
    end
  end

  # Snapshot-disk low watermark in bytes (EMBERVM_SESSION_DISK_LOW_WATERMARK_BYTES).
  # Unset disables disk-pressure eviction (nil): the watermark alert (Task 9) is the
  # only signal until a value is configured. A configured value arms LRU eviction.
  defp session_disk_low_watermark_bytes do
    case trimmed_env("EMBERVM_SESSION_DISK_LOW_WATERMARK_BYTES") do
      "" -> nil
      raw -> String.to_integer(raw)
    end
  end

  # Wake-rate limit (relight-triggering invokes per principal per window). Configured
  # via EMBERVM_SESSION_WAKE_MAX + EMBERVM_SESSION_WAKE_WINDOW_MS; unset uses the
  # SessionManager module defaults (30 / 60s). A max of 0 disables the limit.
  defp wake_opts do
    max =
      case trimmed_env("EMBERVM_SESSION_WAKE_MAX") do
        "" -> nil
        raw -> String.to_integer(raw)
      end

    window =
      case trimmed_env("EMBERVM_SESSION_WAKE_WINDOW_MS") do
        "" -> nil
        raw -> String.to_integer(raw)
      end

    [wake_max: max, wake_window_ms: window]
    |> Enum.reject(fn {_k, v} -> is_nil(v) end)
  end

  # EndpointPublisher config: the debounce/re-push cadences and the activator
  # fallback endpoint. The activator endpoint (the control plane's own
  # request-serving listener the node Envoy falls back to for a cold workload) is
  # wired in Task 8; until then it is nil, which renders an empty cluster for a
  # workload with no live instance (Envoy 503s until one publishes). The xds http
  # port the publisher PUTs to is read from EMBERVM_XDS_HTTP_PORT at request time
  # (see EndpointPublisher.default_put), matching the chart's sidecar wiring, so it
  # is not passed here.
  defp endpoint_publisher_opts do
    [
      activator_endpoint: activator_endpoint(),
      activator_ip: stateful_activator_ip()
    ] ++ repush_opt()
  end

  defp node_registry_opts do
    [
      nodes: configured_nodes(),
      control_plane_activator_ip: stateful_activator_ip()
    ]
  end

  # GrpcConnectionSweeper config: the destructive gate and sweep cadence from env.
  # Default is gate OFF (the sweep runs but only logs, leaving connections untouched).
  defp grpc_connection_sweeper_opts do
    [
      sweep_enabled: grpc_connection_sweep_enabled(),
      sweep_interval_ms: int_env_or_nil("EMBERVM_GRPC_CONNECTION_SWEEP_INTERVAL_MS")
    ]
    |> Enum.reject(fn {_k, v} -> is_nil(v) end)
  end

  # The destructive gate for the GrpcConnectionSweeper, from
  # EMBERVM_GRPC_CONNECTION_SWEEP_ENABLED. UNSET or "0"/"false"/"" (the default,
  # and what the chart defaults to) => the sweep runs but only logs a dry-run plan,
  # leaving no children terminated. "1"/"true" => the sweep terminates orchestrators
  # holding dead addresses or wedged in init. Wired here so it flips via a deploy
  # values env change, no code change.
  defp grpc_connection_sweep_enabled do
    case trimmed_env("EMBERVM_GRPC_CONNECTION_SWEEP_ENABLED") do
      v when v in ["1", "true", "TRUE", "True"] -> true
      _ -> false
    end
  end


  # The activator endpoint the node Envoy routes to when a serving workload has no
  # healthy published instance, from EMBERVM_SERVING_ACTIVATOR_IP + _PORT (Task 8
  # wires the chart values). Unset -> nil (empty-cluster fallback).
  defp activator_endpoint do
    ip = trimmed_env("EMBERVM_SERVING_ACTIVATOR_IP")
    port = trimmed_env("EMBERVM_SERVING_ACTIVATOR_PORT")

    if ip != "" and port != "" do
      %{ip: ip, port: String.to_integer(port)}
    else
      nil
    end
  end

  # The STATEFUL activator's IP (Task 8, D-R4.PR-4.1), from
  # EMBERVM_STATEFUL_ACTIVATOR_IP (the pod's own routable IP, wired via the
  # chart's downward API, matching how EMBERVM_SERVING_ACTIVATOR_IP is wired for
  # the L7 activator). This is a SINGLE ip, not an {ip, port} pair: the L4
  # activator resolves the workload from the LOCAL ACCEPT PORT it is dialed on
  # (there is no header at L4, decision 5), so EndpointPublisher derives each
  # stateful workload's OWN fallback port from its catalog listen_port, never a
  # single shared port. Unset -> nil, which makes a cold stateful workload emit
  # NO listener/cluster at all (it cannot be woken yet), so the sidecar never
  # sees an empty-endpoints cluster. Distinct from activator_endpoint (the L7
  # serving fallback over HTTP, one fixed {ip, port} because the HTTP activator
  # resolves the workload from the injected x-ember-workload header instead).
  defp stateful_activator_ip do
    case trimmed_env("EMBERVM_STATEFUL_ACTIVATOR_IP") do
      "" -> nil
      ip -> ip
    end
  end

  # The publisher's level-triggered re-push cadence (EMBERVM_SERVING_REPUSH_MS),
  # the safety net that makes a sidecar-container restart self-healing; default
  # 45s (module default). A 0 disables the periodic re-push (change-driven only).
  defp repush_opt do
    case trimmed_env("EMBERVM_SERVING_REPUSH_MS") do
      "" -> []
      raw -> [repush_ms: String.to_integer(raw)]
    end
  end

  # ServingManager (activator) config: the wake-rate limit + parked cap (chart
  # values) and the adoption reconcile cadence. Defaults keep the reconcile timer ON
  # in production; the module defaults the caps (30/min wake, 64 parked).
  defp serving_manager_opts do
    [
      op_log_mod: op_log_mod(),
      reconcile_interval_ms: serving_reconcile_interval_ms(),
      node_confirmed_destroy: node_confirmed_destroy_enabled(),
      destroying_alarm_ms: destroying_alarm_ms(),
      orphan_grace_ms: orphan_grace_ms()
    ] ++ serving_wake_opts()
  end

  # Serving adoption reconcile cadence (EMBERVM_SERVING_RECONCILE_INTERVAL_MS);
  # default 10s, matching the session reconcile tempo so a restart's endpoint
  # re-derivation lands fast.
  defp serving_reconcile_interval_ms do
    case trimmed_env("EMBERVM_SERVING_RECONCILE_INTERVAL_MS") do
      "" -> 10_000
      raw -> String.to_integer(raw)
    end
  end

  # Serving wake-rate + parked cap (EMBERVM_SERVING_WAKE_MAX / _WINDOW_MS /
  # _PARK_CAP); unset uses the ServingManager module defaults. A wake_max of 0
  # disables the wake-rate limit.
  defp serving_wake_opts do
    [
      wake_max: int_env_or_nil("EMBERVM_SERVING_WAKE_MAX"),
      wake_window_ms: int_env_or_nil("EMBERVM_SERVING_WAKE_WINDOW_MS"),
      park_cap: int_env_or_nil("EMBERVM_SERVING_PARK_CAP")
    ]
    |> Enum.reject(fn {_k, v} -> is_nil(v) end)
  end

  defp int_env_or_nil(name) do
    case trimmed_env(name) do
      "" -> nil
      raw -> String.to_integer(raw)
    end
  end

  # ServingSweeper (Task 9) config: the sweep cadence, the node Envoy stats base URL,
  # and the per-node concurrent-bank cap. The stats_base is the serving Service's
  # stats port (EMBERVM_SERVING_STATS_BASE, e.g. http://embervm-serving:9902); unset
  # -> nil, which disables the scrape so every tick fails open (banks nothing). The
  # cadence defaults to 30s (standing decision 9's low-cadence idle scrape).
  defp serving_sweeper_opts do
    [
      op_log_mod: op_log_mod(),
      node_confirmed_destroy: node_confirmed_destroy_enabled(),
      destroying_alarm_ms: destroying_alarm_ms(),
      orphan_grace_ms: orphan_grace_ms(),
      sweep_interval_ms: serving_sweep_interval_ms(),
      stats_base: sweeper_stats_base(),
      bank_concurrency: int_env_or_nil("EMBERVM_SERVING_BANK_CONCURRENCY")
    ]
    |> Enum.reject(fn {_k, v} -> is_nil(v) end)
  end

  defp serving_sweep_interval_ms do
    case trimmed_env("EMBERVM_SERVING_SWEEP_INTERVAL_MS") do
      "" -> 30_000
      raw -> String.to_integer(raw)
    end
  end

  defp sweeper_stats_base do
    case trimmed_env("EMBERVM_SERVING_STATS_BASE") do
      "" -> nil
      raw -> raw
    end
  end

  # StatefulSweeper (R4, Task 9) config: the sweep cadence, the node Envoy stats base
  # URL (reuses sweeper_stats_base/0, EMBERVM_SERVING_STATS_BASE: stateful's L4
  # tcp_proxy listeners live on the SAME node Envoy admin the serving scrape already
  # reaches, so there is no separate stateful stats env var), the per-node concurrent-
  # bank cap, and the max-lifetime drain patience window. Unset stats_base disables the
  # scrape, so every tick fails open and banks nothing, exactly the ServingSweeper
  # default.
  # The drain coordinator reads only its safety-margin knob from the environment;
  # an unset var lets the module apply its own 15s default (the reject drops the
  # nil so it never overrides the default with nil).
  defp drain_coordinator_opts do
    [op_log_mod: op_log_mod(), safety_margin_ms: int_env_or_nil("EMBERVM_DRAIN_SAFETY_MARGIN_MS")]
    |> Enum.reject(fn {_k, v} -> is_nil(v) end)
  end

  defp stateful_sweeper_opts do
    [
      op_log_mod: op_log_mod(),
      sweep_interval_ms: stateful_sweep_interval_ms(),
      stats_base: sweeper_stats_base(),
      bank_concurrency: int_env_or_nil("EMBERVM_STATEFUL_BANK_CONCURRENCY"),
      lifetime_drain_max_ms: int_env_or_nil("EMBERVM_STATEFUL_LIFETIME_DRAIN_MAX_MS")
    ]
    |> Enum.reject(fn {_k, v} -> is_nil(v) end)
  end

  defp stateful_sweep_interval_ms do
    case trimmed_env("EMBERVM_STATEFUL_SWEEP_INTERVAL_MS") do
      "" -> 30_000
      raw -> String.to_integer(raw)
    end
  end

  # GroupSweeper (R5, Task 8) config: the sweep cadence, the node Envoy stats base URL
  # (reuses sweeper_stats_base/0, EMBERVM_SERVING_STATS_BASE: composite entry listeners
  # live on the SAME node Envoy admin the serving/stateful scrape reaches), the
  # activator live-splice table (the idle predicate's third clause), and the
  # max-lifetime drain patience window. Unset stats_base disables the scrape, so every
  # tick fails open and banks nothing, exactly the StatefulSweeper default.
  defp group_sweeper_opts do
    [
      op_log_mod: op_log_mod(),
      node_confirmed_destroy: node_confirmed_destroy_enabled(),
      destroying_alarm_ms: destroying_alarm_ms(),
      orphan_grace_ms: orphan_grace_ms(),
      sweep_interval_ms: group_sweep_interval_ms(),
      stats_base: sweeper_stats_base(),
      splices_table: Embervm.ActivatorSplices,
      lifetime_drain_max_ms: int_env_or_nil("EMBERVM_GROUP_LIFETIME_DRAIN_MAX_MS")
    ]
    |> Enum.reject(fn {_k, v} -> is_nil(v) end)
  end

  defp group_sweep_interval_ms do
    case trimmed_env("EMBERVM_GROUP_SWEEP_INTERVAL_MS") do
      "" -> 30_000
      raw -> String.to_integer(raw)
    end
  end

  # StatefulManager (R4, Task 8) config: the adoption reconcile cadence + the
  # wake-rate/parked-cap knobs. Defaults keep the reconcile timer ON in
  # production; the module defaults the caps (10/min wake, 16 parked), an order
  # of magnitude below serving's because a stateful workload is a
  # singleton-owned sandbox, not multi-tenant fan-in.
  defp stateful_manager_opts do
    [
      op_log_mod: op_log_mod(),
      reconcile_interval_ms: stateful_reconcile_interval_ms(),
      node_confirmed_destroy: node_confirmed_destroy_enabled(),
      destroying_alarm_ms: destroying_alarm_ms(),
      orphan_grace_ms: orphan_grace_ms()
    ] ++ stateful_wake_opts()
  end

  # Stateful adoption reconcile cadence (EMBERVM_STATEFUL_RECONCILE_INTERVAL_MS);
  # default 10s, matching the serving/session reconcile tempo so a restart's
  # endpoint re-derivation lands fast.
  defp stateful_reconcile_interval_ms do
    case trimmed_env("EMBERVM_STATEFUL_RECONCILE_INTERVAL_MS") do
      "" -> 10_000
      raw -> String.to_integer(raw)
    end
  end

  # Stateful wake-rate + parked cap (EMBERVM_STATEFUL_WAKE_MAX / _WINDOW_MS /
  # _PARK_CAP); unset uses the StatefulManager module defaults. A wake_max of 0
  # disables the wake-rate limit.
  defp stateful_wake_opts do
    [
      wake_max: int_env_or_nil("EMBERVM_STATEFUL_WAKE_MAX"),
      wake_window_ms: int_env_or_nil("EMBERVM_STATEFUL_WAKE_WINDOW_MS"),
      park_cap: int_env_or_nil("EMBERVM_STATEFUL_PARK_CAP")
    ]
    |> Enum.reject(fn {_k, v} -> is_nil(v) end)
  end

  # TcpActivator (R4, Task 8) config: the values-declared stateful TCP port
  # range it binds (mirroring the chart's servingEnvoy.statefulTcpPortRange,
  # default 5400-5409) and the activator's own advertised ip (fed straight into
  # EndpointPublisher's activator_ip option too, see endpoint_publisher_opts/0).
  # An empty range binds NOTHING: a control plane with no stateful activator ip
  # configured runs with no L4 listener, exactly the "cannot wake yet" no-op
  # EndpointPublisher's stateful_render already handles for a nil activator_ip
  # (no point binding ports nothing is dialed on, and it keeps a plain
  # `mix test` / no-chart-values run from claiming a fixed port range on the
  # workstation or a shared CI executor).
  # The activator binds BOTH the R4 stateful range (5400-5409) and the R5 composite
  # range (5410-5419); a connection accepted on a port resolves the workload AND its
  # class by the local accept port (decision 5) and routes to the stateful or group
  # wake brain accordingly. The composite range binds only when the activator ip is
  # wired (same gate as stateful: no point binding ports nothing dials on), and reads
  # the SAME EMBERVM_COMPOSITE_LISTEN_PORT_RANGE the watcher + publisher use so the
  # bound listeners match the rendered group-<listenPort> listeners.
  defp tcp_activator_opts do
    [
      port_range: stateful_activator_port_range() ++ composite_activator_port_range(),
      activator_ip: stateful_activator_ip(),
      # The composite live-splice counter table the activator increments per group
      # splice and the GroupSweeper reads for its idle predicate (Task 8).
      splices_table: Embervm.ActivatorSplices
    ]
  end

  @default_composite_activator_port_start 5410
  @default_composite_activator_port_end 5419

  # The composite entry-listener ports the activator binds. Gated on the activator ip
  # (a no-ip run binds nothing, so a plain `mix test` never claims fixed ports),
  # reading EMBERVM_COMPOSITE_LISTEN_PORT_RANGE (the same value the watcher +
  # publisher key on); unset defaults to 5410-5419.
  defp composite_activator_port_range do
    if stateful_activator_ip() do
      case composite_listen_range_env() do
        nil -> Enum.to_list(@default_composite_activator_port_start..@default_composite_activator_port_end)
        range -> Enum.to_list(range)
      end
    else
      []
    end
  end

  @default_stateful_activator_port_start 5400
  @default_stateful_activator_port_end 5409

  # Only binds a range when EMBERVM_STATEFUL_ACTIVATOR_IP is actually
  # configured (an operator who wired the ip gets the sane 5400-5409 default
  # unless they also override the range); with no activator_ip configured this
  # returns [] (no listener), so an ordinary `mix test` / no-chart-values run
  # never claims fixed ports. EMBERVM_STATEFUL_ACTIVATOR_PORT_RANGE is a
  # "start-end" pair (e.g. "5400-5409"); EMBERVM_STATEFUL_ACTIVATOR_PORT_START/
  # _END are the split form the chart may render instead.
  defp stateful_activator_port_range do
    if stateful_activator_ip() do
      case trimmed_env("EMBERVM_STATEFUL_ACTIVATOR_PORT_RANGE") do
        "" ->
          start_port = int_env_or_nil("EMBERVM_STATEFUL_ACTIVATOR_PORT_START") || @default_stateful_activator_port_start
          end_port = int_env_or_nil("EMBERVM_STATEFUL_ACTIVATOR_PORT_END") || @default_stateful_activator_port_end
          Enum.to_list(start_port..end_port)

        raw ->
          case String.split(raw, "-", parts: 2) do
            [s, e] ->
              with {start_port, ""} <- Integer.parse(String.trim(s)),
                   {end_port, ""} <- Integer.parse(String.trim(e)) do
                Enum.to_list(start_port..end_port)
              else
                _ -> []
              end

            _ ->
              []
          end
      end
    else
      []
    end
  end

  # Composite-group capacity (R5): translate the chart env vars into the app-env
  # keys Embervm.WorkloadWatcher reads. EMBERVM_COMPOSITE_LISTEN_PORT_RANGE is a
  # "start-end" pair (e.g. "5410-5419"), parsed EXACTLY like
  # stateful_activator_port_range/0 parses its range; EMBERVM_MAX_GROUP_SIZE is a
  # positive integer. An absent or malformed value is left UNSET (put_env is
  # skipped), so the watcher's own Application.get_env default fires (5410..5419 /
  # 4) rather than a bad override taking hold. Only the R5 seam is wired here; the
  # R4 :stateful_listen_range seam is deliberately left as-is.
  defp put_composite_group_config do
    case composite_listen_range_env() do
      nil -> :ok
      range -> Application.put_env(:embervm, :composite_listen_range, range)
    end

    case max_group_size_env() do
      nil -> :ok
      size -> Application.put_env(:embervm, :max_group_size, size)
    end
  end

  # The periodic BaseBuilder catalog-resync cadence (RCA H1 self-heal), from
  # EMBERVM_WORKLOAD_RESYNC_INTERVAL_MS. Left UNSET on an absent or malformed
  # value (put_env skipped), so Embervm.WorkloadWatcher's own
  # Application.get_env default (60s) fires. A value of 0 is accepted and
  # disables the timer (the watcher treats <= 0 as "no timer").
  defp put_workload_resync_config do
    case workload_resync_interval_ms_env() do
      nil -> :ok
      ms -> Application.put_env(:embervm, :workload_resync_interval_ms, ms)
    end
  end

  # Brick capacity (PR-3): the size-class desired-replica list + the brick
  # Deployment name prefix from the chart env into app-env. Unset (bricks
  # disabled) leaves both keys absent, so Embervm.BrickController's own
  # Application.get_env defaults fire (empty class list + empty prefix = inert).
  defp put_brick_config do
    case brick_classes_env() do
      nil -> :ok
      classes -> Application.put_env(:embervm, :brick_classes, classes)
    end

    case trimmed_env("EMBERVM_BRICK_DEPLOYMENT_PREFIX") do
      "" -> :ok
      prefix -> Application.put_env(:embervm, :brick_deployment_prefix, prefix)
    end

    case brick_autoscale_mode_env() do
      nil -> :ok
      mode -> Application.put_env(:embervm, :brick_autoscale_mode, mode)
    end
  end

  # Parse EMBERVM_BRICK_AUTOSCALE_MODE (Axis C). Only the four known modes are
  # accepted; unset or unrecognized leaves the key absent so the controller's
  # :off default fires (fail-safe: a typo in values must never ENABLE acting).
  defp brick_autoscale_mode_env do
    case trimmed_env("EMBERVM_BRICK_AUTOSCALE_MODE") do
      "off" -> :off
      "observe" -> :observe
      "up" -> :up
      "full" -> :full
      _ -> nil
    end
  end

  # BrickController reads all of its inputs from Application env at init, so no
  # start options are threaded here (the test suite injects its own).
  defp brick_controller_opts, do: []

  defp capacity_observer_opts, do: []

  # Parse EMBERVM_BRICK_CLASSES (a JSON array of {"name","desired"} objects, plus
  # optional autoscale clamp fields "min"/"max") into a list of
  # %{name, desired, min, max}. nil when unset, malformed, or empty, so the
  # controller's empty-list default fires (it reconciles nothing). A class missing
  # a binary name or a non-negative integer desired is dropped, never crashing
  # boot; an invalid/absent min reads 0 and an invalid/absent max reads nil (the
  # controller then clamps to max(desired, min): no autoscale headroom).
  defp brick_classes_env do
    case trimmed_env("EMBERVM_BRICK_CLASSES") do
      "" ->
        nil

      raw ->
        case safe_json_decode(raw) do
          list when is_list(list) ->
            parsed =
              for %{"name" => name, "desired" => desired} = class <- list,
                  is_binary(name),
                  is_integer(desired),
                  desired >= 0 do
                %{
                  name: name,
                  desired: desired,
                  min: non_neg_int_or(Map.get(class, "min"), 0),
                  max: non_neg_int_or(Map.get(class, "max"), nil)
                }
              end

            if parsed == [], do: nil, else: parsed

          _ ->
            nil
        end
    end
  end

  defp non_neg_int_or(value, _default) when is_integer(value) and value >= 0, do: value
  defp non_neg_int_or(_value, default), do: default

  # :json.decode raises on malformed input; a bad EMBERVM_BRICK_CLASSES must leave
  # the controller inert, not crash the whole control plane at boot.
  defp safe_json_decode(raw) do
    :json.decode(raw)
  rescue
    _ -> nil
  catch
    _, _ -> nil
  end

  defp workload_resync_interval_ms_env do
    case trimmed_env("EMBERVM_WORKLOAD_RESYNC_INTERVAL_MS") do
      "" ->
        nil

      raw ->
        case Integer.parse(raw) do
          {ms, ""} when ms >= 0 -> ms
          _ -> nil
        end
    end
  end

  # Parse "start-end" into a start..end Range, or nil when unset/malformed (so the
  # watcher default fires). Mirrors stateful_activator_port_range/0's split+parse.
  defp composite_listen_range_env do
    case trimmed_env("EMBERVM_COMPOSITE_LISTEN_PORT_RANGE") do
      "" ->
        nil

      raw ->
        case String.split(raw, "-", parts: 2) do
          [s, e] ->
            with {start_port, ""} <- Integer.parse(String.trim(s)),
                 {end_port, ""} <- Integer.parse(String.trim(e)),
                 true <- start_port <= end_port do
              start_port..end_port
            else
              _ -> nil
            end

          _ ->
            nil
        end
    end
  end

  # GroupManager.Supervisor (R5) config: the per-group defaults threaded into every
  # Embervm.GroupManager the supervisor spawns. The composite supernet + port base are
  # wired from the chart env and MUST be the SAME shared values that feed noded's
  # CompositeSupernet + ServingPortBase (one chart value rendered into both pods), so
  # the CP's subnet allocation and entry-DNAT-port re-derivation stay in lockstep with
  # the daemon. pod_ip is the control-plane pod IP (the entry endpoint is published as
  # {pod IP, vmPort}, the D-R3.11.4 lane). With no supernet wired the defaults carry
  # the compile-time fallback (matching the chart defaults) so a no-chart `mix test`
  # run still allocates from a sane /16.
  defp group_manager_supervisor_opts do
    [
      node_confirmed_destroy: node_confirmed_destroy_enabled(),
      destroying_alarm_ms: destroying_alarm_ms(),
      orphan_grace_ms: orphan_grace_ms(),
      defaults: [
        supernet: composite_supernet_env(),
        port_base: composite_port_base_env(),
        pod_ip: trimmed_env("EMBERVM_POD_IP") |> nil_if_empty()
      ]
    ]
  end

  # GroupWakeManager (R5, Task 7) config: the adoption reconcile cadence + the
  # wake-rate/parked-cap knobs, mirroring StatefulManager. Defaults keep the
  # reconcile timer ON in production; the module defaults the caps (10/min wake, 16
  # parked). The reconcile reuses the stateful reconcile cadence env by default so a
  # restart's group endpoint re-derivation lands on the same tempo.
  defp group_wake_manager_opts do
    [
      op_log_mod: op_log_mod(),
      node_confirmed_destroy: node_confirmed_destroy_enabled(),
      destroying_alarm_ms: destroying_alarm_ms(),
      orphan_grace_ms: orphan_grace_ms(),
      reconcile_interval_ms: group_reconcile_interval_ms(),
      # The shared supernet + DNAT port base + pod IP the adoption reconcile re-derives
      # the entry DNAT endpoint from (the SAME values group_manager_supervisor_opts
      # threads into every GroupManager, so a republish equals the live publish).
      supernet: composite_supernet_env(),
      port_base: composite_port_base_env(),
      pod_ip: trimmed_env("EMBERVM_POD_IP") |> nil_if_empty()
    ] ++ group_wake_opts()
  end

  defp group_reconcile_interval_ms do
    case trimmed_env("EMBERVM_GROUP_RECONCILE_INTERVAL_MS") do
      "" -> 10_000
      raw -> String.to_integer(raw)
    end
  end

  defp group_wake_opts do
    [
      wake_max: int_env_or_nil("EMBERVM_GROUP_WAKE_MAX"),
      wake_window_ms: int_env_or_nil("EMBERVM_GROUP_WAKE_WINDOW_MS"),
      park_cap: int_env_or_nil("EMBERVM_GROUP_PARK_CAP")
    ]
    |> Enum.reject(fn {_k, v} -> is_nil(v) end)
  end

  # The composite supernet the CP allocates per-group /24s from. Shared source of
  # truth with noded (chart value noded.compositeSupernet rendered into BOTH pods).
  # Defaults to the chart default 10.101.0.0/16 when unset so a no-chart run allocates
  # from a sane /16.
  defp composite_supernet_env do
    case trimmed_env("EMBERVM_COMPOSITE_SUPERNET") do
      "" -> "10.101.0.0/16"
      raw -> raw
    end
  end

  # The DNAT port base the CP re-derives the entry vmPort from. Shared source of truth
  # with noded's ServingPortBase (chart value rendered into both pods). Defaults to
  # 30000 (noded's own compile-time ServingPortBase default) when unset.
  defp composite_port_base_env do
    case trimmed_env("EMBERVM_COMPOSITE_PORT_BASE") do
      "" ->
        30_000

      raw ->
        case Integer.parse(raw) do
          {base, ""} when base > 0 -> base
          _ -> 30_000
        end
    end
  end

  defp nil_if_empty(""), do: nil
  defp nil_if_empty(value), do: value

  # Parse EMBERVM_MAX_GROUP_SIZE into a positive integer, or nil when unset/malformed
  # (so the watcher default fires).
  defp max_group_size_env do
    case trimmed_env("EMBERVM_MAX_GROUP_SIZE") do
      "" ->
        nil

      raw ->
        case Integer.parse(raw) do
          {size, ""} when size > 0 -> size
          _ -> nil
        end
    end
  end

  defp queue_depth_cap do
    case trimmed_env("EMBERVM_QUEUE_DEPTH_CAP") do
      "" -> 10_000
      raw -> String.to_integer(raw)
    end
  end

  defp share_fraction_opt do
    case trimmed_env("EMBERVM_PRINCIPAL_SHARE_FRACTION") do
      "" ->
        []

      raw ->
        case Float.parse(raw) do
          {f, _} when f > 0 -> [share_fraction: f]
          _ -> []
        end
    end
  end

  # Per-principal daily vCPU-second budgets (Task 12), from the chart. Deliberately
  # OPT-IN and asymmetric to the auth allow-list: empty means quota OFF (a
  # principal with no configured budget is allowed), NOT deny-all. Fail-closed
  # applies only to a principal that HAS a budget when the quota cache is
  # unreadable. `EMBERVM_QUOTA_VCPU_SECONDS` is a comma list of `principal=budget`
  # pairs; `EMBERVM_QUOTA_DEFAULT_VCPU_SECONDS` is an optional blanket budget for
  # principals not named in the map. Budgets are vCPU-seconds (float).
  defp quota_config do
    %{budgets: quota_budgets(), default: quota_default()}
  end

  defp quota_budgets do
    case System.get_env("EMBERVM_QUOTA_VCPU_SECONDS") do
      nil ->
        %{}

      raw ->
        raw
        |> String.split(",", trim: true)
        |> Enum.reduce(%{}, fn pair, acc ->
          case String.split(pair, "=", parts: 2) do
            [k, v] ->
              case parse_budget(String.trim(v)) do
                nil -> acc
                budget -> Map.put(acc, String.trim(k), budget)
              end

            _ ->
              acc
          end
        end)
    end
  end

  defp quota_default do
    case trimmed_env("EMBERVM_QUOTA_DEFAULT_VCPU_SECONDS") do
      "" -> nil
      raw -> parse_budget(raw)
    end
  end

  # A budget of exactly 0 is VALID and means "deny this principal entirely": the
  # runtime gate (Embervm.Metering.within_quota?/4) compares `used < budget`, so a
  # 0 budget always denies. Accepting 0 here keeps the config surface consistent
  # with that runtime meaning (a 0 in values is a hard stop, not silently
  # unlimited). A negative or unparseable value is dropped (nil = no budget =
  # allowed, the opt-in default).
  defp parse_budget(raw) do
    case Float.parse(raw) do
      {f, _} when f >= 0 -> f
      _ -> nil
    end
  end

  # ServiceAccount usernames allowed to read other principals' usage at
  # GET /v1/usage (via ?principal=), from EMBERVM_USAGE_ADMINS (comma-separated).
  # Everyone else is self-scoped. Empty = nobody is an admin (self-scope only).
  defp usage_admins do
    case System.get_env("EMBERVM_USAGE_ADMINS") do
      nil ->
        []

      raw ->
        raw
        |> String.split(",", trim: true)
        |> Enum.map(&String.trim/1)
        |> Enum.reject(&(&1 == ""))
    end
  end

  # The TokenReview allow-list: ServiceAccount usernames permitted to submit
  # tasks, from EMBERVM_ALLOWED_SERVICE_ACCOUNTS (comma-separated), wired by the
  # chart from values.auth.allowedServiceAccounts. An empty list means deny-all
  # (fail-closed); Embervm.Auth logs a loud warning in that case.
  defp allowed_service_accounts do
    case System.get_env("EMBERVM_ALLOWED_SERVICE_ACCOUNTS") do
      nil ->
        []

      raw ->
        raw
        |> String.split(",", trim: true)
        |> Enum.map(&String.trim/1)
        |> Enum.reject(&(&1 == ""))
    end
  end
end
