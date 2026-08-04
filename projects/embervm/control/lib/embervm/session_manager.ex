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
       the per-workload live+disk count (banked and parked both hold a resource,
       a workspace volume, not a VM) against `session.maxSessions`, the live
       count (VM holders only) against `concurrency.cap`, and the principal's
       daily quota;
    2. place the session on an ordered, memory-eligible brick;
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
  require OpenTelemetry.Tracer, as: Tracer

  alias Embervm.{Brick, NodeCapacity, SessionState, SessionStore, SessionTrace, WakeInstance, WorkloadCatalog}
  alias Embervm.Scheduler.Request
  alias Embervm.Scheduler

  alias Embervm.Node.V1.{
    ArtifactRef,
    ArchiveVolumeRequest,
    BankRequest,
    BankResponse,
    DeleteVolumeRequest,
    EvictArtifactRequest,
    EvictSnapshotRequest,
    PrimeRequest,
    PrimeResponse,
    RetireVolumeRequest,
    RelightRequest,
    RelightResponse,
    RestoreArtifactRequest,
    Trace
  }

  @registry Embervm.SessionRegistry

  # Per-node concurrent-bank cap: banking writes GiBs, so serialize per node like
  # base builds (the node daemon also refuses a second concurrent bank, but the
  # control plane must not even ISSUE a second one and stack the I/O).
  @default_bank_concurrency 1

  # Wake-rate limit: relight-triggering invokes per principal per window. A relight
  # restores a full 2 GiB snapshot, so a burst of misses is an asymmetric-cost DoS
  # lever; excess relights get 429 WITHOUT touching the node.
  @default_wake_max 30
  @default_wake_window_ms 60_000
  @create_worker_timeout_ms 200_000

  # Three consecutive bank failures fail the session and destroy its VM: a session
  # that cannot bank must not squat live capacity forever.
  @bank_fail_limit 3

  # NodeCapacity.updated_at is stamped from a monotonic clock. Keep its freshness
  # gate in that domain instead of comparing it with session row wall-clock times.
  @fleet_freshness_window_ms 120_000

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
  `{:ok, %{session_id, lineage_id, token, expires_at, base_digest, restored}}`
  (the token is returned ONCE, here; `lineage_id` equals `session_id` for a
  normal create), or `{:error, {:denied, reason}}` for a capacity/quota denial
  (`:session_cap`, `:workload_cap`, `:quota`, `:no_capacity`,
  `:unknown_workload`, `:not_session_class`) that the router maps to a
  429/403. `restored` is always `false` here (no restore requested); see
  `create/4`.
  """
  @spec create(GenServer.server(), String.t(), String.t()) :: {:ok, map()} | {:error, term()}
  def create(server \\ __MODULE__, workload, principal) do
    create(server, workload, principal, nil)
  end

  @doc """
  Like `create/3`, but `restore_lineage` (nilable, empty treated as absent)
  makes this a #4306 slice 3 CROSS-GENERATION create: the new session
  inherits `restore_lineage`'s durable workspace instead of minting a fresh
  one, so `session_id != lineage_id` for the first time in this codebase
  (`lineage_id` becomes `restore_lineage`; a fresh `session_id` is still
  minted). This is deliberately NOT called "adopt": that word already names
  the unrelated boot-time rebind mechanism in this module (`adopt_state`/
  adoption reconcile). Restore is a distinct, cross-generation concept and
  stays a distinct word throughout.

  Validated before anything is placed or primed, each denying with a distinct
  reason the router maps to a 4xx:

    * `:unknown_lineage` -- no session has ever carried `restore_lineage`.
    * `:lineage_workload_mismatch` -- the lineage belongs to a different workload.
    * `:lineage_principal_mismatch` -- the lineage belongs to a different
      principal (ADR embervm/025's cross-principal prohibition).
    * `:lineage_live_heir` -- the lineage's newest holder is not terminal yet
      (exclusivity: at most one live heir per lineage).
    * `:lineage_restore_in_flight` -- another restoring create for this SAME
      lineage is already mid-flight (TOCTOU guard: the durable heir row this
      validates against does not exist until that create finishes, tens of
      seconds later for a cold restore, so a client retry must be denied
      rather than raced). Retryable once the in-flight restore settles,
      unlike `:lineage_live_heir`, which is a committed heir.

  `restored` in the returned map is `true` when the inherited workspace was
  actually recovered (attached locally or an S3 restore hit), `false` for a
  genuine blank/miss (tolerated, not an error) or for a normal create.

  The returned map also carries `lineage_id`: the durable workspace handle a
  caller passes as `restore_lineage` to restore THIS session's generation in
  a future create. For a normal create it equals `session_id`; for a
  restoring create it equals the inherited `restore_lineage` (session_id
  diverges from it instead, per above). A caller chaining generations must
  track `lineage_id`, not `session_id`: restoring a non-first generation's own
  `session_id` finds no lineage at all (`:unknown_lineage`), since
  `get_latest_by_lineage/2` keys strictly on `lineage_id`.
  """
  @spec create(GenServer.server(), String.t(), String.t(), String.t() | nil) ::
          {:ok, map()} | {:error, term()}
  def create(server, workload, principal, restore_lineage) do
    # Cold-boot persistence creates can legitimately take tens of seconds while
    # the manager remains responsive to other calls.
    GenServer.call(server, {:create, workload, principal, normalize_restore_lineage(restore_lineage)}, 180_000)
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

  @doc """
  Banks an idle session (called by its `Embervm.Session` process when its idle
  timer fires and it is quiescent). Enforces the per-node concurrent-bank cap and
  the disk fail-closed gate here (node-global policy, serialized through this
  process), runs the `Bank` RPC, and appends `session_banked` (generation+1). On
  `:ok` the caller (the session process) stops itself: a banked session has no
  process. Returns `:ok`, `{:error, :bank_busy}` (another bank in flight on the
  node), `{:error, :disk_unknown}` (fail-closed: missing disk facts, stay live),
  or `{:error, reason}` on a daemon failure (the session stays running, the caller
  re-arms its timer and counts the failure).
  """
  @spec bank(GenServer.server(), String.t()) :: :ok | {:error, term()}
  def bank(server \\ __MODULE__, session_id) do
    GenServer.call(server, {:bank, session_id}, :infinity)
  end

  @doc """
  Runs one adoption reconcile synchronously (the same code the boot continue and
  the periodic sweep run) and returns after it completes. Reconciles the ETS
  projection against every node's reported `session_vms` + `session_snapshots`:
  rebinds live session VMs to fresh processes, heals `banking`/`relighting` limbo,
  fails sessions whose VM AND snapshot both vanished, and evicts orphaned
  snapshots. Tests drive adoption deterministically through this.
  """
  @spec reconcile(GenServer.server()) :: :ok
  def reconcile(server \\ __MODULE__) do
    GenServer.call(server, :reconcile, :infinity)
  end

  @doc """
  Runs one capacity/TTL sweep synchronously: expires sessions past `expires_at`
  (live -> destroy VM, banked/parked -> EvictSnapshot), GCs banked or parked
  sessions untouched past `bankedTtlSeconds` (#4305), and evicts banked sessions
  LRU while any node is below its snapshot-disk low watermark. Tests drive it
  deterministically; production fires it on the sweeper timer.
  """
  @spec sweep(GenServer.server()) :: :ok
  def sweep(server \\ __MODULE__) do
    GenServer.call(server, :sweep, :infinity)
  end

  # -- GenServer callbacks ---------------------------------------------------

  @impl true
  def init(opts) do
    op_log_mod = Keyword.get(opts, :op_log_mod, Embervm.OpLog.SQLite)
    op_log = Keyword.get(opts, :op_log, op_log_mod)

    state = %{
      session_store: Keyword.get(opts, :session_store, SessionStore),
      dispatcher: Keyword.get(opts, :dispatcher, Embervm.Dispatcher),
      supervisor: Keyword.get(opts, :supervisor, Embervm.SessionSupervisor),
      registry: Keyword.get(opts, :registry, @registry),
      capacity_table: Keyword.get(opts, :capacity_table, NodeCapacity.table()),
      catalog_table: Keyword.get(opts, :catalog_table, WorkloadCatalog.table()),
      clock: Keyword.get(opts, :clock, &default_clock/0),
      monotonic_clock: Keyword.get(opts, :monotonic_clock, fn -> System.monotonic_time(:millisecond) end),
      quota_table: Keyword.get(opts, :quota_table, Embervm.Metering.table()),
      quota_config: Keyword.get(opts, :quota_config, Embervm.Metering.quota_config()),
      # Injected for tests; production uses the real claim/prime/channel seams.
      claim_fun: Keyword.get(opts, :claim_fun, &default_claim/3),
      id_fun: Keyword.get(opts, :id_fun),
      channel_fun: Keyword.get(opts, :channel_fun, &Embervm.NodeChannel.get/1),
      prime_fun: Keyword.get(opts, :prime_fun, &default_prime/2),
      # Daemon session verb seams (Bank/Relight/EvictSnapshot). Injected for tests;
      # production dials the real NodeService stub over the shared NodeChannel.
      bank_fun: Keyword.get(opts, :bank_fun, &default_bank/2),
      relight_fun: Keyword.get(opts, :relight_fun, &default_relight/2),
      evict_fun: Keyword.get(opts, :evict_fun, &default_evict/2),
      # Restore-on-miss seam (R6, Task 8): (channel, %RestoreArtifactRequest{}) ->
      # {:ok, %RestoreArtifactResponse{}} | {:error, _}. Fetches a banked SESSION
      # bundle back onto local disk from the object store before a relight, when the
      # bundle is exported but no longer locally reported. Injected for tests;
      # production dials the real NodeService stub.
      restore_artifact_fun: Keyword.get(opts, :restore_artifact_fun, &default_restore_artifact/2),
      archive_volume_fun: Keyword.get(opts, :archive_volume_fun, &default_archive_volume/2),
      retire_volume_fun: Keyword.get(opts, :retire_volume_fun, &default_retire_volume/2),
      # Remote artifact eviction seam (R6, Task 9): (channel, %EvictArtifactRequest{})
      # -> {:ok, %EvictArtifactResponse{}} | {:error, _}. Fired alongside every local
      # EvictSnapshot so the store copy of a session's bundle follows on the same
      # triggers (banked TTL, disk pressure, destroy, expiry, orphan sweep). Sessions
      # carry no volume/generation, so no pairing guard applies here. Injected for
      # tests; production dials the real NodeService stub.
      evict_artifact_fun: Keyword.get(opts, :evict_artifact_fun, &default_evict_artifact/2),
      delete_session_volume_fun: Keyword.get(opts, :delete_session_volume_fun, &default_delete_session_volume/2),
      # The op-log the restore audit record (:artifact_restored) is appended to.
      # Injected for tests; production uses the configured backend.
      op_log: op_log,
      # The backend module dispatched below, threaded alongside :op_log (the
      # server address) so a non-default backend never requires editing this
      # module. Defaults to the selected backend module.
      op_log_mod: op_log_mod,
      # Extra opts threaded into every started Embervm.Session (the daemon seams),
      # so a test can inject a fake session_assign into the spawned process.
      session_opts: Keyword.get(opts, :session_opts, []),
      tenant: Keyword.get(opts, :tenant, "homelab"),
      # Per-node concurrent-bank cap: node_id -> count of banks in flight on it. A
      # node at its cap refuses a new bank (the session stays live, re-arms its timer).
      bank_concurrency: Keyword.get(opts, :bank_concurrency, @default_bank_concurrency),
      bank_inflight: %{},
      # session_id -> %{node_id: n} for banks currently running async, so a mid-bank
      # invoke can park (relighting ledger) and be relit once the bank completes.
      banking: %{},
      # create_ref -> {from, lineage_id, workload, principal, entry, node_id,
      # snapshot_ref, dial_id, restore_lineage}; claim/prime runs outside this
      # process. restore_lineage is nil for a normal create (#4306 slice 3).
      create_inflight: %{},
      create_next_ref: 0,
      create_workers: %{},
      # session_id -> consecutive bank-failure count. Lives HERE (not in the session
      # process, which stops on admission) so three strikes across bank attempts fails
      # the session. Cleared on a successful bank or a successful invoke-driven relight.
      bank_failures: %{},
      # Snapshot-disk low watermark (bytes): when a node's snapshot_disk_free_bytes
      # is below this, disk-pressure eviction fires. nil = eviction disabled.
      disk_low_watermark_bytes: Keyword.get(opts, :disk_low_watermark_bytes, nil),
      # Wake-rate limit: max relight-triggering invokes per principal per window.
      wake_max: Keyword.get(opts, :wake_max, @default_wake_max),
      wake_window_ms: Keyword.get(opts, :wake_window_ms, @default_wake_window_ms),
      # principal -> [relight timestamps within the window]. Sliding-window counter.
      wake_events: %{},
      # session_id -> [{from, req}] parked callers waiting on an in-flight relight,
      # so concurrent invokes to one banked session share ONE relight instead of
      # racing two. Also the crash-consistency ledger: a relight appends session_relit
      # ONLY after the daemon returns a live vm_id, and until then these callers park.
      relighting: %{},
      # The registry sweep (adoption) and the TTL/eviction sweep cadences. Distinct
      # timers; both clock-injected and disable-able for tests (interval 0 = off).
      reconcile_interval_ms: Keyword.get(opts, :reconcile_interval_ms, 0),
      sweep_interval_ms: Keyword.get(opts, :sweep_interval_ms, 0),
      # status.sessions {live,banked} + sessionsSummary writer (Task 9). Written on
      # the sweep tick, DEBOUNCED: only patched when a session workload's live/banked
      # pair differs from the last-written pair (workload -> {live, banked}), the same
      # change-detection the PoolManager uses for primedFloorSatisfied. Disjoint status
      # keys (`sessions`/`sessionsSummary`) so it never clobbers the watcher/pool/base
      # writers' keys (the merge-patch coexistence rule).
      status_writer: Keyword.get(opts, :status_writer, &Embervm.K8s.patch_workload_status/3),
      session_status_written: %{},
      # EMBERVM_NODE_CONFIRMED_DESTROY (ADR embervm/014 decision 5). Off (default):
      # destroyed is recorded first, then the VM is torn down asynchronously (today's
      # behaviour). On: the durable destroying intent is recorded, the teardown RPC
      # runs, and destroyed is recorded ONLY when the node confirms teardown; an
      # unconfirmed teardown leaves the session in destroying for the reconcile loop.
      node_confirmed_destroy: Keyword.get(opts, :node_confirmed_destroy, false),
      # Alarm threshold (ms) for a session stuck in destroying, and the grace window
      # (ms) before fail-closed orphan reconciliation acts (ADR embervm/014). Both
      # only bite under the node_confirmed_destroy gate.
      destroying_alarm_ms: Keyword.get(opts, :destroying_alarm_ms, 300_000),
      orphan_grace_ms: Keyword.get(opts, :orphan_grace_ms, 60_000),
      fleet_freshness_window_ms: Keyword.get(opts, :fleet_freshness_window_ms, @fleet_freshness_window_ms),
      orphan_volume_first_seen: %{},
      # Ids already alarmed for being stuck in destroying, so the error logs once
      # per stuck id rather than every reconcile tick (pruned as ids terminalize).
      destroying_alarmed: MapSet.new(),
      # Session ids whose adoption transition was not applicable, with the state
      # that produced the warning. A state change makes the warning eligible again.
      logged_unapplicable: MapSet.new(),
      unapplicable_states: %{},
      # EMBERVM_ASYNC_LIFECYCLE_WRITES (ADR embervm/014 decision 2). When on, the
      # Direction-2 orphan reconcile ADOPTS-and-backfills a reported session VM whose
      # store row is absent BUT a durable write is still in flight for it (a young
      # async-write race), rather than destroying it as a true orphan. The pending
      # check consults Embervm.AsyncWriter; async_writer is the process to ask.
      async_lifecycle_writes: Keyword.get(opts, :async_lifecycle_writes, false),
      async_writer: Keyword.get(opts, :async_writer, Embervm.AsyncWriter)
    }

    # session_opts always carry a manager reference so the per-session idle timer can
    # call back for a bank; a test-supplied :manager wins (an isolated harness).
    state = %{state | session_opts: Keyword.put_new(state.session_opts, :manager, self())}

    if state.reconcile_interval_ms > 0 or state.sweep_interval_ms > 0 do
      {:ok, state, {:continue, :boot}}
    else
      {:ok, state}
    end
  end

  # Boot: run one adoption reconcile against whatever the node registry has already
  # populated, then arm the periodic reconcile + sweep timers. Reconcile runs first
  # so the residency/limbo heal lands before the first sweep can evict on it.
  @impl true
  def handle_continue(:boot, state) do
    state = do_reconcile(state)
    schedule(:reconcile, state.reconcile_interval_ms)
    schedule(:sweep, state.sweep_interval_ms)
    {:noreply, state}
  end

  # Runs after the {:bank, _} reply has been sent (so the idle-path caller, which
  # blocks on that call before stopping itself, is never killed mid-call).
  def handle_continue({:reap_banked_process, session_id}, state) do
    terminate_session_process(state, session_id)
    {:noreply, state}
  end

  @impl true
  def handle_call({:create, workload, principal, restore_lineage}, from, state) do
    case do_create_inline(state, workload, principal, restore_lineage) do
      {:ok,
       %{entry: entry, node_id: node_id, dial_id: dial_id, snapshot_ref: snapshot_ref, lineage_id: lineage_id}} ->
        ref = state.create_next_ref

        state =
          put_in(
            state.create_inflight[ref],
            {from, lineage_id, workload, principal, entry, node_id, snapshot_ref, dial_id, restore_lineage}
          )

        state = %{state | create_next_ref: ref + 1}
        {state, worker} =
          spawn_create_worker(state, ref, node_id, dial_id, workload, snapshot_ref, entry, lineage_id, restore_lineage)

        {:noreply, put_in(state.create_workers[ref], worker)}

      {:error, {:denied, reason}} = error_result ->
        # Bind the FULL {:error, {:denied, _}} tuple: binding the inner term
        # replied {:denied, reason} and every denial test failed on the shape.
        audit_denial(state, principal, workload, reason)
        log_create_result(error_result, workload, principal)
        {:reply, error_result, state}
    end
  end

  def handle_call({:route_invoke, session_id, req}, from, state) do
    # Resolve the route on this process (fast), but do NOT block it on the invoke:
    # the session process's own FIFO parks the caller. So we reply the pid to the
    # caller path via a spawned forwarder, keeping the manager responsive. Simpler:
    # forward synchronously from a short task so the manager is not the bottleneck.
    case resolve_route(state, session_id) do
      {:live, pid} ->
        # Forward off the manager so a long guest round-trip does not serialize other
        # sessions' routing through this one GenServer.
        _ = spawn_forward(pid, req, from)
        {:noreply, state}

      # A banked session: this invoke is a lifecycle MISS. Park the caller and (if it
      # is the first for this session) trigger a relight, subject to the per-principal
      # wake-rate limit. Concurrent invokes to the same banked session share the one
      # relight (they all park under relighting[session_id]).
      {:relight, session} ->
        {:noreply, park_and_relight(state, session, from, req)}

      # A relight is already in flight for this session (started by an earlier
      # invoke): just park behind it. Drained when the relight completes.
      {:relighting, _session} ->
        {:noreply, park_relighting(state, session_id, from, req)}

      {:error, _reason} = error ->
        {:reply, error, state}
    end
  end

  def handle_call({:destroy, session_id}, _from, state) do
    {reply, state} = do_destroy(state, session_id)
    {:reply, reply, state}
  end

  def handle_call({:bank, session_id}, _from, state) do
    {reply, state} = do_bank(state, session_id)

    case reply do
      # "A banked session has no process" (see Embervm.Session): the idle-bank
      # path stops itself on admission, but an API/drain-driven bank left the
      # process alive holding the Registry key, so the eventual relight's process
      # start collided with it and failed the session. Reap it AFTER the reply
      # (handle_continue) so the idle-path caller is not killed mid-call.
      :ok -> {:reply, reply, state, {:continue, {:reap_banked_process, session_id}}}
      _ -> {:reply, reply, state}
    end
  end

  def handle_call({:drain_node, node_id}, _from, state) do
    {count, state} = drain_bank_node(state, node_id)
    {:reply, count, state}
  end

  def handle_call(:reconcile, _from, state) do
    {:reply, :ok, do_reconcile(state)}
  end

  def handle_call(:sweep, _from, state) do
    {:reply, :ok, do_sweep(state)}
  end

  # The async result of an in-flight relight worker (spawned by park_and_relight):
  # {:ok, node_id, vm_id, relight_ms} on a live restore, or {:error, reason}. On
  # success, crash-consistently append session_relit (AFTER the daemon returned a
  # live vm_id), start the session process, and drain the parked callers into it.
  # On failure, fail the session (snapshot_lost -> 410) and 410 the parked callers.
  @impl true
  def handle_info({:relight_done, session_id, outcome}, state) do
    {:noreply, finish_relight(state, session_id, outcome)}
  end

  def handle_info({:rejoin_done, session_id, outcome}, state) do
    {:noreply, finish_rejoin(state, session_id, outcome)}
  end

  def handle_info({:rejoin_assign_failed, session_id, reason}, state) do
    case SessionStore.get(state.session_store, session_id) do
      {:ok, %{state: :running} = session} ->
        _ = SessionStore.transition(state.session_store, session_id, :park, :session_parking,
          %{reason: "rejoin delivery failed", volume_node_id: session.volume_node_id}, %{})
        _ = stop_session_process(state, session_id, session)
        _ = SessionStore.transition(state.session_store, session_id, :park_complete, :session_parked,
          %{reason: "rejoin delivery failed", volume_node_id: session.volume_node_id},
          %{node_id: nil, vm_id: nil})
        Logger.warning("embervm session rejoin delivery failed", session_id: session_id, reason: inspect(reason))
        {:noreply, state}
      _ -> {:noreply, state}
    end
  end

  # The async bank worker finished: complete the durable transition + failure
  # recovery on this (serialized) process. See finish_bank.
  def handle_info({:bank_done, session_id, node_id, outcome}, state) do
    {:noreply, finish_bank(state, session_id, node_id, outcome)}
  end

  def handle_info({:create_done, ref, result}, state) do
    {:noreply, finish_create(state, ref, result)}
  end

  def handle_info({:create_timeout, ref}, state) do
    case Map.get(state.create_workers, ref) do
      {pid, _monitor_ref, _timer_ref} ->
        Process.exit(pid, :kill)
        {:noreply, finish_create(state, ref, {:error, :create_timeout})}

      _ ->
        {:noreply, state}
    end
  end

  def handle_info({:DOWN, monitor_ref, :process, _pid, reason}, state) do
    case Enum.find(state.create_workers, fn {_ref, {_pid, mref, _timer_ref}} -> mref == monitor_ref end) do
      {ref, _worker} ->
        {:noreply, finish_create(state, ref, {:error, {:create_worker_crashed, reason}})}
      nil -> {:noreply, state}
    end
  end

  def handle_info(:reconcile, state) do
    state = do_reconcile(state)
    schedule(:reconcile, state.reconcile_interval_ms)
    {:noreply, state}
  end

  def handle_info(:sweep, state) do
    state = do_sweep(state)
    schedule(:sweep, state.sweep_interval_ms)
    {:noreply, state}
  end

  def handle_info(_msg, state), do: {:noreply, state}

  # -- create ----------------------------------------------------------------

  defp do_create_inline(state, workload, principal, restore_lineage) do
    # The management create span covers the manager-owned fast path. The
    # claim/prime portion runs in a worker, so its pool-hit attributes are split.
    Tracer.with_span "embervm.session.create",
                     %{
                       attributes: %{
                         "ember.workload" => workload,
                         "ember.principal" => principal
                       }
                     } do
      with {:ok, entry} <- fetch_session_workload(state, workload),
           :ok <- check_session_cap(state, workload, entry),
           :ok <- check_workload_cap(state, workload, entry),
           :ok <- check_quota(state, principal),
           :ok <- validate_restore_lineage(state, restore_lineage, workload, principal),
           :ok <- check_restore_not_inflight(state, restore_lineage),
           pin_node_id = restore_lineage_volume_node(state, restore_lineage, workload),
           {:ok, node_id, dial_id, snapshot_ref} <- place_create(state, workload, entry, pin_node_id) do
        {:ok,
         %{
           entry: entry,
           node_id: node_id,
           dial_id: dial_id,
           snapshot_ref: snapshot_ref,
           # #4306 slice 3: a restoring create's lineage_id is the INHERITED
           # restore_lineage, not a freshly minted one (this is the divergence
           # point where session_id and lineage_id first differ; see
           # register_and_start). Non-restoring behavior is unchanged.
           lineage_id:
             restore_lineage ||
               if(persistence_enabled_workload?(entry), do: mint_lineage_id(state), else: nil)
         }}
      else
        {:error, reason} -> {:error, {:denied, reason}}
      end
    end
  end

  # restore_lineage present but no session has EVER carried it, belongs to a
  # different workload or principal (ADR embervm/025's cross-principal
  # prohibition), or its newest holder is not terminal yet (exclusivity: at
  # most one live heir per lineage). nil/empty restore_lineage is always a
  # normal create and always validates.
  defp validate_restore_lineage(_state, nil, _workload, _principal), do: :ok

  defp validate_restore_lineage(state, restore_lineage, workload, principal) do
    case SessionStore.get_latest_by_lineage(state.session_store, restore_lineage) do
      :error ->
        {:error, :unknown_lineage}

      {:ok, %{workload: lineage_workload}} when lineage_workload != workload ->
        {:error, :lineage_workload_mismatch}

      {:ok, %{principal: lineage_principal}} when lineage_principal != principal ->
        {:error, :lineage_principal_mismatch}

      {:ok, %{state: session_state}} when session_state not in [:expired, :evicted, :destroyed, :failed] ->
        {:error, :lineage_live_heir}

      {:ok, _terminal_holder} ->
        :ok
    end
  end

  # #4306/#4313 review fix 2 (TOCTOU): validate_restore_lineage/4 only
  # consults the durable store, but the heir row it looks for is written
  # asynchronously (finish_create -> register_and_start), tens of seconds
  # later for a cold-boot restore. A second restoring create of the SAME
  # lineage (a client retrying a slow restore) would re-validate against the
  # store, still see only the terminal predecessor, pass, and both would then
  # prime and register_and_start with lineage_id = restore_lineage: two live
  # heirs writing one volume concurrently. Worse, the LOSER's failure-branch
  # reclaim (finish_create, keyed only by workload+lineage_id) would then
  # re-arm retirement of the lineage the WINNER is live on, exporting then
  # deleting a live volume out from under it.
  #
  # do_create_inline runs SYNCHRONOUSLY inside the {:create, ...}
  # GenServer.call, strictly BEFORE the worker spawns and BEFORE
  # create_inflight gains an entry for THIS create (that write happens back
  # in handle_call, after do_create_inline returns). Every other create the
  # manager is mid-processing is either already in create_inflight (its
  # worker spawned, still primed/restoring) or blocked behind this same
  # single-process mailbox waiting its own turn at do_create_inline. So a scan
  # of create_inflight here can only ever see OTHER creates, never this one,
  # and no other create can interleave a write between this read and this
  # create's own create_inflight insert: check-then-proceed is atomic with
  # respect to a concurrent restore of the same lineage, closing the window
  # the store-only check leaves open.
  defp check_restore_not_inflight(_state, nil), do: :ok

  defp check_restore_not_inflight(state, restore_lineage) do
    in_flight? =
      state.create_inflight
      |> Map.values()
      |> Enum.any?(fn {_from, _lineage_id, _workload, _principal, _entry, _node_id, _snapshot_ref, _dial_id,
                        inflight_restore_lineage} ->
        inflight_restore_lineage == restore_lineage
      end)

    if in_flight?, do: {:error, :lineage_restore_in_flight}, else: :ok
  end

  # The node currently reporting restore_lineage's workspace volume in its
  # fleet facts (the same session_volumes list retire_orphan_session_volumes
  # reads), or nil when no node reports it. A cold-restore create (no node
  # found) places anywhere, same as a normal create; restore_session_workspace
  # then tries the object store instead of a local attach.
  defp restore_lineage_volume_node(_state, nil, _workload), do: nil

  defp restore_lineage_volume_node(state, restore_lineage, workload) do
    state.capacity_table
    |> NodeCapacity.all()
    |> Enum.find_value(fn f ->
      Enum.find_value(Map.get(f, :session_volumes, []) || [], fn volume ->
        if volume.lineage_id == restore_lineage and volume.workload == workload, do: f.configured_id
      end)
    end)
  end

  defp spawn_create_worker(state, ref, node_id, dial_id, workload, snapshot_ref, entry, lineage_id, restore_lineage) do
    owner = self()
    timeout_ref = Process.send_after(owner, {:create_timeout, ref}, @create_worker_timeout_ms)

    {pid, monitor_ref} = spawn_monitor(fn ->
      result =
        try do
          case restore_lineage do
            nil ->
              case claim_or_prime(state, dial_id, workload, snapshot_ref, entry, lineage_id) do
                {:ok, vm_id} -> {:ok, vm_id, false}
                {:error, reason} -> {:error, reason}
              end

            _ ->
              restore_then_prime(state, node_id, workload, snapshot_ref, entry, restore_lineage, dial_id)
          end
        rescue
          exception -> {:error, {:create_worker_crashed, exception}}
        catch
          kind, reason -> {:error, {:create_worker_crashed, {kind, reason}}}
        end

      send(owner, {:create_done, ref, result})
    end)

    {state, {pid, monitor_ref, timeout_ref}}
  end

  # Restore-then-prime for a restoring create (#4306 slice 3): the create-path
  # mirror of perform_rejoin_prime's restore-before-boot ordering. place_create
  # only PINS the bare node when the fleet facts locate the lineage's volume;
  # it may still hand back a dial_id for the wrong co-located instance on that
  # node (multiple instances can share one node_id), so this re-resolves the
  # dial itself via the same session_volumes fact perform_rejoin_prime uses,
  # restores the workspace onto it BEFORE prime so the guest boots with data
  # already in place, then primes with lineage_id = restore_lineage so the
  # volume attaches under the inherited identity rather than a fresh one.
  # restore-on-miss is tolerated (see restore_session_workspace/4): a genuine
  # store miss proceeds with a blank workspace and reports restored: false.
  #
  # placed_dial_id is place_create's OWN resolved brick instance dial (the
  # same one the non-restore path dials via claim_or_prime), threaded through
  # from spawn_create_worker's call site as the COLD-restore fallback. A warm
  # restore (a session_volumes fact reports this lineage on node_id) still
  # prefers the volume-owning instance. But dial_for_session_volume fails
  # OPEN, not nil, on a miss: it returns the bare node_id itself when no
  # instance reports the lineage, which is exactly the after-eviction
  # S3-restore case (nothing local yet). Dialing that bare node name fails
  # :unknown_node on the real fleet (channel_fun only ever accepts an
  # instance dial, never the node-name alias; the node name is an anchor,
  # not a dial key, same lesson perform_rejoin_prime's own comment
  # documents), which turned a recoverable S3 restore into a hard 500 rather
  # than the blank-workspace degrade this path is supposed to tolerate. So a
  # plain `||` cannot detect the miss (the fallthrough value is truthy); this
  # explicitly compares the resolved dial against the bare node_id instead,
  # and falls back to placed_dial_id (always a real, dialable instance)
  # whenever dial_for_session_volume did not actually find an owning
  # instance.
  defp restore_then_prime(state, node_id, workload, snapshot_ref, entry, restore_lineage, placed_dial_id) do
    dial_id =
      case Embervm.WakeInstance.dial_for_session_volume(state.capacity_table, node_id, restore_lineage) do
        ^node_id -> placed_dial_id
        resolved -> resolved
      end

    with {:ok, restored} <- restore_session_workspace(state, dial_id, workload, restore_lineage),
         {:ok, vm_id} <- prime(state, dial_id, snapshot_ref, entry, restore_lineage) do
      {:ok, vm_id, restored}
    end
  end

  defp finish_create(state, ref, outcome) do
    case Map.pop(state.create_inflight, ref) do
      {nil, _} ->
        state

      {{from, lineage_id, workload, principal, entry, node_id, _snapshot_ref, dial_id, restore_lineage}, inflight} ->
        cleanup_create_worker(Map.get(state.create_workers, ref))
        state = %{state | create_inflight: inflight, create_workers: Map.delete(state.create_workers, ref)}

        result =
          case outcome do
            {:ok, vm_id, restored} ->
              register_and_start(state, workload, principal, entry, node_id, vm_id, dial_id, lineage_id, restore_lineage, restored)

            {:error, reason} ->
              audit_denial(state, principal, workload, reason)

              # #4306/#4313: noded clears the lineage's retirement intent early
              # in an adopting Prime, so a RESTORING create that fails after
              # prime started (this branch only runs once the worker actually
              # ran, i.e. validate_restore_lineage already passed) can leave
              # the volume on disk with no retirement intent and no live VM.
              # Re-drive reclamation so noded's sweep exports then deletes it
              # instead of orphaning. node_id here is wherever the worker
              # actually ran, pinned or not, so it is always the right target.
              if restore_lineage do
                _ = retire_session_volume(state, %{volume_node_id: node_id, workload: workload, lineage_id: restore_lineage})
              end

              {:error, {:denied, reason}}
          end

        GenServer.reply(from, result)
        log_create_result(result, workload, principal)
        state
    end
  end

  defp cleanup_create_worker({pid, monitor_ref, timeout_ref}) do
    Process.cancel_timer(timeout_ref)
    Process.demonitor(monitor_ref, [:flush])
    if Process.alive?(pid), do: Process.exit(pid, :kill)
  end

  defp cleanup_create_worker(_), do: :ok

  # Structured create log (Task 9): success at info, denial at warn, both keyed
  # with session_id (when minted)/workload/principal for consistent lifecycle logs.
  defp log_create_result({:ok, %{session_id: session_id}}, workload, principal) do
    Logger.info("embervm session created", session_id: session_id, workload: workload, principal: principal)
  end

  defp log_create_result({:error, {:denied, reason}}, workload, principal) do
    Logger.warning("embervm session create denied",
      workload: workload,
      principal: principal,
      reason: inspect(reason)
    )
  end

  defp log_create_result(_other, _workload, _principal), do: :ok

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

  # live + disk-held (banked or parked) < session.maxSessions (both hold a
  # resource: a VM or disk).
  defp check_session_cap(state, workload, %{session: session}) do
    counts = SessionStore.counts(state.session_store, workload)

    if counts.live + counts.banked < session.max_sessions do
      :ok
    else
      {:error, :session_cap}
    end
  end

  # live session VMs < concurrency.cap (banked and parked sessions hold disk,
  # not a VM). Wake asymmetry: rejoin and relight admit on placement memory
  # headroom, not on cap, so cap is a create-time governor only.
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

  # pin_node_id (#4306 slice 3, nil for every pre-existing caller) PINS
  # placement to a specific node, the same way perform_rejoin_prime pins
  # volume_node_id for a rejoin: a restoring create whose fleet facts locate
  # restore_lineage's volume must land where the volume already is.
  defp place_create(state, workload, entry, pin_node_id) do
    case Scheduler.place_with_demand(%Request{
           table: state.capacity_table,
           workload: workload,
           key: workload,
           node_id: pin_node_id,
           need_mib: Map.get(entry, :mem_mib) || 512,
           base: {:ready, :snapshot_ref}
         }) do
      {:ok, [brick | _]} ->
        workload_entry = Map.get(brick.workloads, workload, %{})
        {:ok, brick.configured_id, Brick.dial_id(brick), Map.get(workload_entry, :snapshot_ref)}

      {:error, :capacity} ->
        {:error, :no_capacity}

      {:error, :no_bricks} ->
        {:error, :no_capacity}

      {:error, _reason} ->
        {:error, :no_capacity}
    end
  end

  # Claim a primed VM from the dispatcher's inventory, or Prime one on a miss (the
  # dispatcher's own miss path, run inline here since create latency is not the
  # per-invoke hot path). A Prime failure is a create denial, not a crash.
  defp claim_or_prime(state, node_id, workload, snapshot_ref, entry, lineage_id) do
    if persistence_enabled_workload?(entry) do
      Tracer.set_attributes(%{"ember.pool_hit" => false})
      prime(state, node_id, snapshot_ref, entry, lineage_id)
    else
      case state.claim_fun.(state.dispatcher, node_id, workload) do
        {:ok, vm_id} ->
        # A warm claim from the primed pool: pool_hit=true (parity with the
        # dispatcher's warm-dispatch marking).
        Tracer.set_attributes(%{"ember.pool_hit" => true})
        {:ok, vm_id}

        :miss ->
          Tracer.set_attributes(%{"ember.pool_hit" => false})
          prime(state, node_id, snapshot_ref, entry, lineage_id)
      end
    end
  end

  defp persistence_enabled_workload?(%{persistence: %{memory: false, filesystem: %{enabled: true}}}), do: true
  defp persistence_enabled_workload?(_), do: false

  defp mint_lineage_id(%{id_fun: fun}) when is_function(fun, 0), do: fun.()
  defp mint_lineage_id(%{id_fun: fun, clock: clock}) when is_function(fun, 1), do: fun.(clock.())
  defp mint_lineage_id(state), do: Embervm.SessionId.new(state.clock.())

  # #4306 slice 3: absent, empty, or non-string all mean "no restore
  # requested" (a normal create), the same tolerant contract the router's
  # own optional_restore_lineage/1 body parse uses.
  defp normalize_restore_lineage(lineage) when is_binary(lineage) and lineage != "", do: lineage
  defp normalize_restore_lineage(_other), do: nil

  defp prime(state, node_id, snapshot_ref, entry, lineage_id) do
    # A prime failure is NOT a capacity problem, and reporting it as one cost a
    # whole debugging cycle: the persistence flip denied every create with
    # :no_capacity while the real reason (a rejected Prime) never left this
    # function. Carry the reason out and log it; the caller still denies.
    case state.channel_fun.(node_id) do
      {:ok, channel} ->
        case safe_prime(state, channel, snapshot_ref, entry, lineage_id) do
          {:ok, %PrimeResponse{vm_id: vm_id}} when is_binary(vm_id) and vm_id != "" ->
            {:ok, vm_id}

          other ->
            Logger.warning("embervm prime failed",
              node_id: node_id,
              snapshot_ref: snapshot_ref,
              lineage_id: lineage_id,
              reason: inspect(other)
            )

            {:error, {:prime_failed, other}}
        end

      other ->
        Logger.warning("embervm prime dial failed", node_id: node_id, reason: inspect(other))
        {:error, {:prime_dial_failed, other}}
    end
  end

  defp safe_prime(state, channel, snapshot_ref, entry, lineage_id) do
    p = Map.get(entry, :persistence) || %{}
    fs = Map.get(p, :filesystem) || %{}
    enabled = persistence_enabled_workload?(entry)
    if enabled and (not is_binary(lineage_id) or lineage_id == "") do
      {:error, :invalid_lineage_id}
    else
      req = %PrimeRequest{
        trace: %Trace{},
        snapshot_ref: snapshot_ref || "",
        volume_mount: if(enabled, do: Map.get(fs, :mount_path, "/session"), else: ""),
        volume_size_bytes: if(enabled, do: Map.get(fs, :size_bytes, 0), else: 0),
        lineage_id: if(enabled, do: lineage_id, else: "")
      }

      state.prime_fun.(channel, req)
    end
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
  defp register_and_start(state, workload, principal, entry, node_id, vm_id, dial_id, lineage_id, restore_lineage, restored) do
    attrs = %{
      tenant: state.tenant,
      principal: principal,
      workload: workload,
      node_id: node_id,
      vm_id: vm_id,
      # session_id: lineage_id is intentional for a normal persistence create,
      # not a naming slip: a persistence-enabled workload pre-mints its
      # lineage id (mint_lineage_id/1) and that SAME value becomes the
      # session's own session_id too (do_create mints a fresh one only when
      # this is nil, the non-persistence case). #4306 slice 1 also records it
      # explicitly as lineage_id below, rather than relying on do_create's
      # session_id-default fallback, so the durable row's lineage_id traces
      # back to the actual pre-minted value (nil here for a non-persistence
      # workload, which still falls through to the same default:
      # Map.get(attrs, :lineage_id) || session_id treats a present-but-nil key
      # the same as an absent one).
      #
      # #4306 slice 3 is the one place this diverges: a RESTORING create
      # inherits restore_lineage as its lineage_id but still needs its OWN
      # fresh session_id (the first place session_id != lineage_id in this
      # codebase), so session_id is left nil here for do_create to mint, and
      # lineage_id is pinned to restore_lineage explicitly rather than falling
      # through to do_create's session_id default.
      session_id: if(restore_lineage, do: nil, else: lineage_id),
      lineage_id: restore_lineage || lineage_id,
      volume_node_id: if(persistence_enabled_workload?(entry), do: node_id, else: nil),
      base_snapshot_ref: Map.get(entry, :image_ref),
      base_digest: base_digest(entry),
      expires_at: state.clock.() + entry.session.max_lifetime_seconds * 1000
    }

    case SessionStore.create(state.session_store, attrs) do
      {:ok, %{session_id: session_id} = created} ->
        case start_session_process(state, session_id, workload, principal, entry, node_id, vm_id, dial_id) do
          {:ok, _pid} ->
            {:ok, Map.put(created, :restored, restored)}

          {:error, reason} ->
            Logger.error("embervm session: process start failed for #{session_id}: #{inspect(reason)}")
            {:error, {:denied, :process_start_failed}}
        end

      {:error, reason} ->
        {:error, {:denied, {:store, reason}}}
    end
  end

  defp start_session_process(state, session_id, workload, principal, entry, node_id, vm_id, dial_id, extra_opts \\ []) do
    child_opts =
      [
        session_id: session_id,
        workload: workload,
        principal: principal,
        node_id: node_id,
        # The per-invoke dial key: the SPECIFIC co-located instance running this
        # session's VM (instance-key unification PR-B0b), not the node-name alias.
        # Falls back to node_id when the caller had no owning instance to resolve.
        dial_id: dial_id || node_id,
        vm_id: vm_id,
        queue_cap: entry.session.invoke_queue_cap,
        timeout_ms: entry.timeout_ms,
        # The guest path a bare invoke falls back to (the shim serves only the
        # workload's invokePath; a "/" default 404s the guest). Both the create and
        # the relight/adoption restart paths reach here with the catalog entry.
        invoke_path: Map.get(entry, :invoke_path) || "/invoke",
        # Arm the idle-bank timer (Task 7): a live session with zero in-flight and
        # zero queued invokes for this long calls back to bank/2. Zero disables it.
        idle_bank_ms: entry.session.idle_bank_seconds * 1000,
        session_store: state.session_store,
        # Register under the session id so the router/manager resolve the pid.
        name: {:via, Registry, {state.registry, session_id}}
      ] ++ state.session_opts ++ extra_opts

    spec = %{
      id: session_id,
      start: {Embervm.Session, :start_link, [child_opts]},
      restart: :transient,
      type: :worker
    }

    start_child_over_registry_sweep(state.supervisor, spec)
  end

  # terminate_child returns when the old session process has exited, but the Registry
  # frees its :unique key only after processing the monitor DOWN, so a fast
  # bank->relight (or adoption restart) can land here while a DEAD pid still holds
  # the key and spuriously fail the session. Retry over that window for a dead
  # holder only; a LIVE holder is a genuine double start and is surfaced as-is.
  # The async variant was reverted because it lost the continuation that starts the session.
  defp start_child_over_registry_sweep(supervisor, spec, attempts \\ 10) do
    case DynamicSupervisor.start_child(supervisor, spec) do
      {:error, {:already_started, pid}} = error ->
        if Process.alive?(pid) or attempts <= 1 do
          error
        else
          Process.sleep(10)
          start_child_over_registry_sweep(supervisor, spec, attempts - 1)
        end

      other ->
        other
    end
  end

  # Start (or restart) a session process from a durable session row + its catalog
  # entry, used by relight and adoption (which have the row, not the create args).
  # A catalog miss (workload deleted) is surfaced so the caller can fail the session
  # rather than start a process with no config.
  defp start_session_from_row(state, session, node_id, vm_id, dial_id, extra_opts \\ []) do
    case fetch_session_workload(state, session.workload) do
      {:ok, entry} ->
        start_session_process(
          state,
          session.session_id,
          session.workload,
          session.principal,
          entry,
          node_id,
          vm_id,
          dial_id,
          extra_opts
        )

      {:error, reason} ->
        {:error, {:no_catalog_entry, reason}}
    end
  end

  # -- invoke routing --------------------------------------------------------

  defp resolve_route(state, session_id) do
    case SessionStore.get(state.session_store, session_id) do
      # Invoke-time expiry (ADR 002 rule 1): expiry must NOT depend on sweep cadence.
      # An invoke arriving on a running/banked session past its deadline expires it
      # HERE (destroy VM / evict snapshot) and 410s the caller, so a session never
      # serves an invoke past expires_at even between sweeps.
      {:ok, %{state: st, expires_at: exp} = session}
      when st in [:running, :banked, :parked] and is_integer(exp) ->
        if exp <= state.clock.() do
          _ = expire_session(state, session)
          {:error, {:gone, "expired"}}
        else
          resolve_non_expired(state, session_id)
        end

      _ ->
        resolve_non_expired(state, session_id)
    end
  end

  # resolve_route's tail, after the invoke-time expiry check (kept separate so the
  # expiry guard does not have to re-list every route arm). Re-reads the row (the
  # expiry guard already fetched it, but a re-read keeps this arm total and simple).
  defp resolve_non_expired(state, session_id) do
    case SessionStore.get(state.session_store, session_id) do
      {:ok, %{state: :running} = _session} ->
        case Registry.lookup(state.registry, session_id) do
          [{pid, _}] -> {:live, pid}
          # Running per the durable store but no process: a restart-limbo the
          # adoption sweep rebinds. Surface a transient error rather than pretend the
          # VM is reachable; the boot reconcile heals it to a live process shortly.
          [] -> {:error, {:not_ready, :running}}
        end

      {:ok, %{state: :parked} = session} ->
        if Map.has_key?(state.relighting, session_id) do
          {:relighting, session}
        else
          {:relight, session}
        end

      # Banked: an invoke is a relight MISS. If a relight is already in flight
      # (started by an earlier concurrent invoke), park behind it; otherwise this is
      # the first invoke, which triggers the relight.
      {:ok, %{state: :banked} = session} ->
        if Map.has_key?(state.relighting, session_id) do
          {:relighting, session}
        else
          {:relight, session}
        end

      {:ok, %{state: :relighting} = session} ->
        # A relight the manager is driving (parked callers pending). Park behind it.
        # A `relighting` durable state with NO in-flight relight in this process is a
        # restart limbo the reconcile heals; treat as a transient not_ready so the
        # caller retries after the heal rather than double-driving a relight.
        if Map.has_key?(state.relighting, session_id) do
          {:relighting, session}
        else
          {:error, {:not_ready, :relighting}}
        end

      # A bank is in flight: an invoke mid-bank PARKS and is relit after the bank
      # completes (no cancel path; banking is short). Park in the relighting ledger;
      # finish_bank relights the parked callers once the session is banked.
      {:ok, %{state: :banking} = session} ->
        if Map.has_key?(state.banking, session_id) do
          {:relighting, session}
        else
          # A `banking` durable state with no in-flight bank in this process is a
          # restart limbo the reconcile heals; transient not_ready so the caller
          # retries after the heal.
          {:error, {:not_ready, :banking}}
        end

      {:ok, %{state: session_state}} ->
        cond do
          session_state == :creating -> {:error, {:not_ready, :creating}}
          session_state == :parking -> {:error, {:not_ready, :parking}}
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

  # -- bank (Task 7) ---------------------------------------------------------

  # ADMIT a bank (synchronous, fast): enforce the per-node concurrent-bank cap and
  # the disk fail-closed gate (node-global policy, serialized on this process), mark
  # ETS `banking` (so a concurrent invoke parks), reserve the node's bank slot, and
  # spawn the async bank WORKER (the RPC + durable append run OFF this process, so a
  # multi-second bank never head-of-line-blocks another session's routing, gate 3).
  # Replies `:ok` (admitted; the caller session process stops) or a refusal (the
  # caller re-arms its timer and stays live).
  @doc """
  Force-bank every live session on a draining node (R6, ADR embervm/009).

  Called by the DrainCoordinator on the drain edge. Banks each `:running` session
  on the node via the existing bank path (so its state survives the roll and relights
  on the next invoke). Sessions already banking are skipped. Sessions refused (at the
  per-node bank cap, or with unknown disk facts) are left for the normal sweep, which
  keeps ticking during the hold. Returns the count whose bank was admitted.
  """
  @spec drain_node(GenServer.server(), String.t()) :: non_neg_integer()
  def drain_node(server \\ __MODULE__, node_id) do
    GenServer.call(server, {:drain_node, node_id}, :infinity)
  end

  defp drain_bank_node(state, node_id) do
    sessions =
      SessionStore.all(state.session_store)
      |> Enum.filter(&(&1.state == :running and &1.node_id == node_id))
      |> Enum.reject(&Map.has_key?(state.banking, &1.session_id))

    {count, state} = Enum.reduce(sessions, {0, state}, fn session, {n, acc} ->
      case do_bank(acc, session.session_id) do
        {:ok, acc} -> {n + 1, acc}
        {{:error, _reason}, acc} -> {n, acc}
      end
    end)

    # Snapshot after parking completes so sessions that were running when the
    # drain began are included alongside already-parked lineages. Archive OFF
    # the manager: the RPC is a fast enqueue-ACK on a healthy node, but an
    # unresponsive brick would otherwise stall invoke routing for its 10s
    # deadline once per lineage, serialized on this process.
    parked =
      SessionStore.all(state.session_store)
      |> Enum.filter(&(&1.state == :parked and &1.volume_node_id == node_id))
      |> Enum.filter(&(persistence_enabled_workload?(session_workload_entry(state, &1.workload))))

    spawn(fn -> Enum.each(parked, fn session -> _ = archive_session_volume(state, session) end) end)

    {count, state}
  end

  defp do_bank(state, session_id) do
    case SessionStore.get(state.session_store, session_id) do
      {:ok, %{state: :running, node_id: node_id, vm_id: vm_id} = session}
      when is_binary(node_id) and is_binary(vm_id) ->
        if persistence_enabled_workload?(session_workload_entry(state, session.workload)) do
          {reply, state} = park_session(state, session_id)
          case reply do
            {:ok, _} -> {:ok, state}
            other -> {other, state}
          end
        else
          if memory_false_without_filesystem?(state, session.workload) do
            destroy_live_legacy(state, session)
            {:ok, state}
          else
            cond do
              bank_at_cap?(state, node_id) ->
                {{:error, :bank_busy}, state}

              not disk_ok_for_bank?(state, node_id) ->
                # Fail-closed: with missing/over-watermark disk facts, do NOT bank onto a
                # possibly-full disk. The session stays live and re-arms its timer.
                {{:error, :disk_unknown}, state}

              true ->
                admit_bank(state, session, node_id, vm_id)
            end
          end
        end

      {:ok, _session} ->
        # Not a bankable state (already banked/terminal/relighting): a benign race,
        # the timer fired against a session that moved on. Refuse so the caller does
        # NOT stop on a false admission; if the session truly moved to banked/terminal
        # its process is already stopping anyway.
        {{:error, :not_bankable}, state}

      :error ->
        {{:error, :not_found}, state}
    end
  end

  defp park_session(state, session_id) do
    case SessionStore.get(state.session_store, session_id) do
      {:ok, %{state: :running, node_id: node_id, vm_id: vm_id} = session}
      when is_binary(node_id) and is_binary(vm_id) ->
        intent = SessionStore.transition(
          state.session_store,
          session_id,
          :park,
          :session_parking,
          %{reason: "idled", volume_node_id: node_id},
          %{volume_node_id: node_id}
        )
        case intent do
          {:ok, _} ->
            # The parking intent is durable before teardown. noded's auto-destroy
            # timer covers a primed VM only after PrimeAssign, not this park window.
            case stop_session_process(state, session_id, session) do
              true ->
                result = SessionStore.transition(state.session_store, session_id, :park_complete,
                  :session_parked, %{reason: "idled", volume_node_id: node_id},
                  %{volume_node_id: node_id, node_id: nil, vm_id: nil})
                Logger.info("embervm session parked", session_id: session_id, workload: session.workload, node_id: node_id)
                {result, state}
              false ->
                {{:error, :park_teardown_unconfirmed}, state}
            end
          error -> {error, state}
        end

      {:ok, _session} -> {{:ok, :not_parked}, state}
      :error -> {{:error, :not_found}, state}
    end
  end

  defp session_workload_entry(state, workload) do
    case WorkloadCatalog.fetch(state.catalog_table, workload) do
      {:ok, entry} -> entry
      _ -> %{}
    end
  end

  defp memory_false_without_filesystem?(state, workload) do
    case session_workload_entry(state, workload) do
      %{persistence: %{memory: false, filesystem: fs}} when is_map(fs) -> Map.get(fs, :enabled, false) != true
      _ -> false
    end
  end

  defp admit_bank(state, session, node_id, vm_id) do
    case SessionStore.mark(state.session_store, session.session_id, :bank) do
      {:ok, _} ->
        state =
          state
          |> incr_bank_inflight(node_id)
          |> Map.update!(:banking, &Map.put(&1, session.session_id, %{node_id: node_id}))

        spawn_bank_worker(state, session, node_id, vm_id)
        {:ok, state}

      {:error, reason} ->
        {{:error, {:mark, reason}}, state}
    end
  end

  # The async bank worker: Bank RPC on the node, then report the result back to the
  # manager. It does NOT append the durable op itself (the manager serializes the
  # store transition + inflight release on {:bank_done}), keeping the op-log's
  # single-writer discipline and the failure-recovery decision on one process.
  defp spawn_bank_worker(state, session, node_id, vm_id) do
    owner = self()
    channel_fun = state.channel_fun
    bank_fun = state.bank_fun
    session_id = session.session_id
    workload = session.workload
    parent_base_ref = session.base_snapshot_ref
    generation = (session.generation || 0) + 1
    # Dial the OWNING instance running this live vm_id, not the node-name alias
    # (co-location made it point at an arbitrary sibling brick). Resolved on the owner
    # so the worker reads a settled key; fail-open to node_id.
    dial_key = Embervm.WakeInstance.dial_for_session_vm(state.capacity_table, node_id, vm_id)

    spawn(fn ->
      # A lifecycle span (Task 9). Idle-bank has no caller trace, so it is a root
      # span (like the dispatcher's embervm.prime); the Task 11 bank-p95 gate reads
      # it. snapshot_bytes is set on success.
      outcome =
        Tracer.with_span "embervm.session.bank",
                         %{
                           attributes: %{
                             "ember.session_id" => session_id,
                             "ember.workload" => workload,
                             "ember.generation" => generation
                           }
                         } do
          with {:ok, channel} <- safe_channel(channel_fun, dial_key),
               {:ok, %BankResponse{snapshot_ref: ref, size_bytes: size}} when is_binary(ref) and ref != "" <-
                 safe_bank(bank_fun, channel, workload, session_id, vm_id) do
            Tracer.set_attributes(%{"ember.snapshot_bytes" => size})
            {:ok, ref, size, generation, parent_base_ref}
          else
            other -> {:error, other}
          end
        end

      send(owner, {:bank_done, session_id, node_id, outcome})
    end)
  end

  # The async bank completed (a {:bank_done} message on the manager). Release the
  # node's bank slot and the banking marker, then:
  #   * on success: transition `banking -[bank_ready]-> banked` with the durable
  #     session_banked op (generation+1, snapshot fact), clear the failure streak,
  #     and if a mid-bank invoke parked, immediately relight it;
  #   * on failure: mark ETS `banking -[bank_abort]-> running` (the VM is still alive,
  #     no snapshot written), count the failure, and either restart a session process
  #     (so it can serve invokes + retry) or, at three strikes, fail + destroy the VM.
  defp finish_bank(state, session_id, node_id, outcome) do
    state =
      state
      |> decr_bank_inflight(node_id)
      |> Map.update!(:banking, &Map.delete(&1, session_id))

    case SessionStore.get(state.session_store, session_id) do
      # Destroyed/expired mid-bank: the durable state is already terminal. On a
      # successful bank the produced snapshot is orphaned and adoption reaps it; drain
      # any mid-bank parked callers gone. Never resurrect a terminal session.
      {:ok, %{state: st}} when st in [:expired, :evicted, :destroyed, :failed] ->
        drain_relight_waiters(state, session_id, {:error, {:gone, to_string(st)}})

      _ ->
        finish_bank_active(state, session_id, node_id, outcome)
    end
  end

  defp finish_bank_active(state, session_id, node_id, outcome) do
    case outcome do
      {:ok, ref, size, generation, parent_base_ref} ->
        _ =
          SessionStore.transition(
            state.session_store,
            session_id,
            :bank_ready,
            :session_banked,
            %{snapshot_ref: ref, size_bytes: size, generation: generation, parent_base_ref: parent_base_ref},
            %{snapshot_ref: ref, snapshot_size_bytes: size, generation: generation, node_id: node_id, vm_id: nil}
          )

        Logger.info("embervm session banked",
          session_id: session_id,
          node_id: node_id,
          workload: workload_of(state, session_id),
          principal: principal_of(state, session_id),
          snapshot_bytes: size
        )
        state = clear_bank_failures(state, session_id)
        relight_parked_after_bank(state, session_id)

      {:error, reason} ->
        handle_bank_failure(state, session_id, reason)
    end
  end

  # A mid-bank invoke parked in the relighting ledger while the bank ran. Now the
  # session is banked, relight it (subject to the wake-rate limit) and drain the
  # parked callers into the relit process. No parked caller = nothing to do.
  defp relight_parked_after_bank(state, session_id) do
    case Map.get(state.relighting, session_id) do
      nil ->
        state

      _waiters ->
        case SessionStore.get(state.session_store, session_id) do
          {:ok, %{state: :banked} = session} -> start_relight(state, session)
          # Raced a destroy/expire: drain the waiters gone.
          _ -> drain_relight_waiters(state, session_id, {:error, {:gone, "unavailable"}})
        end
    end
  end

  # A bank failed: ETS back to running, count the strike. Three strikes fail the
  # session + destroy its VM; under the limit, restart a session process so the live
  # VM keeps serving and its idle timer will retry the bank.
  defp handle_bank_failure(state, session_id, reason) do
    _ = SessionStore.mark(state.session_store, session_id, :bank_abort)
    failures = Map.get(state.bank_failures, session_id, 0) + 1
    Logger.warning("embervm session bank failed", session_id: session_id, reason: inspect(reason), consecutive: failures)

    if failures >= @bank_fail_limit do
      state = clear_bank_failures(state, session_id)

      case SessionStore.get(state.session_store, session_id) do
        {:ok, session} ->
          fail_session_and_destroy(state, session)
          # Any mid-bank parked callers: the session is failed now.
          drain_relight_waiters(state, session_id, {:error, {:gone, "failed"}})

        :error ->
          state
      end
    else
      state = %{state | bank_failures: Map.put(state.bank_failures, session_id, failures)}
      restart_running_process(state, session_id)
    end
  end

  # Restart a session process for a running session that lost its process (a failed
  # bank: the process stopped on admission but the VM is still live). Idempotent: a
  # session that already has a process is left alone.
  defp restart_running_process(state, session_id) do
    case SessionStore.get(state.session_store, session_id) do
      {:ok, %{state: :running, node_id: node_id, vm_id: vm_id} = session}
      when is_binary(node_id) and is_binary(vm_id) ->
        case Registry.lookup(state.registry, session_id) do
          [{_pid, _}] ->
            state

          [] ->
            # Resolve the co-located instance still running this live vm_id so the
            # restarted process's per-invoke dial targets the owner, not the alias.
            dial_id = Embervm.WakeInstance.dial_for_session_vm(state.capacity_table, node_id, vm_id)
            _ = start_session_from_row(state, session, node_id, vm_id, dial_id)
            state
        end

      _ ->
        state
    end
  end

  defp fail_session_and_destroy(state, session) do
    _ = destroy_vm(state, session)
    fail_session(state, session.session_id, :failed, "bank_failed_three_strikes")
    state
  end

  defp clear_bank_failures(state, session_id) do
    %{state | bank_failures: Map.delete(state.bank_failures, session_id)}
  end

  # A node is at its bank cap when the count of in-flight banks on it reaches the
  # per-node concurrency limit (default 1).
  defp bank_at_cap?(state, node_id) do
    Map.get(state.bank_inflight, node_id, 0) >= state.bank_concurrency
  end

  # Disk is OK to bank when eviction is disabled (no watermark configured) OR the
  # node reports snapshot_disk_free_bytes strictly ABOVE the low watermark. Missing
  # disk facts (node absent from the capacity table, or a nil free-bytes field) are
  # fail-closed: not OK. This never banks onto a disk we cannot prove has room.
  defp disk_ok_for_bank?(%{disk_low_watermark_bytes: nil}, _node_id), do: true

  defp disk_ok_for_bank?(state, node_id) do
    case NodeCapacity.fetch(state.capacity_table, node_id) do
      {:ok, fact} ->
        free = Map.get(fact, :snapshot_disk_free_bytes)
        is_integer(free) and free > state.disk_low_watermark_bytes

      :error ->
        false
    end
  end

  defp incr_bank_inflight(state, node_id) do
    %{state | bank_inflight: Map.update(state.bank_inflight, node_id, 1, &(&1 + 1))}
  end

  defp decr_bank_inflight(state, node_id) do
    %{state | bank_inflight: Map.update(state.bank_inflight, node_id, 0, &max(&1 - 1, 0))}
  end

  defp safe_bank(bank_fun, channel, workload, session_id, vm_id) do
    req = %BankRequest{trace: %Trace{workload: workload}, vm_id: vm_id, session_id: session_id}
    bank_fun.(channel, req)
  rescue
    e -> {:error, {:bank_raised, e}}
  catch
    kind, reason -> {:error, {:bank_raised, {kind, reason}}}
  end

  # Acquiring a node channel is a GenServer.call that EXITS (not returns) if the
  # NodeChannel is down or the dial times out. An exit thrown in a bank/relight
  # worker's `with` head would kill the worker BEFORE it sends {:bank_done}/
  # {:relight_done}, hanging every parked caller forever and leaking the per-node
  # bank slot (the node could then never bank again). Trap it into an {:error, _}
  # value so the worker always reports an outcome the manager can clean up.
  defp safe_channel(channel_fun, node_id) do
    channel_fun.(node_id)
  rescue
    e -> {:error, {:channel_raised, e}}
  catch
    kind, reason -> {:error, {:channel_raised, {kind, reason}}}
  end

  # -- relight-on-invoke (Task 8) --------------------------------------------

  # First invoke on a banked session: apply the wake-rate limit, then either park +
  # start a relight, or 429 the caller without touching the node.
  defp park_and_relight(state, session, from, req) do
    principal = session.principal

    cond do
      session.state == :parked and is_binary(session.volume_node_id) ->
        if persistence_enabled_workload?(session_workload_entry(state, session.workload)) do
          rejoin_parked(state, session, from, req)
        else
          _ = destroy_live_legacy(state, session)
          GenServer.reply(from, {:error, {:gone, "persistence_disabled"}})
          state
        end

      wake_allowed?(state, principal) ->
        state = record_wake(state, principal)
        state = park_relighting(state, session.session_id, from, req)
        start_relight(state, session)

      true ->
        audit_denial(state, principal, session.workload, :wake_rate)
        GenServer.reply(from, {:error, :wake_rate_limited})
        state
    end
  end

  defp rejoin_parked(state, session, from, req) do
    principal = session.principal

    if wake_allowed?(state, principal) do
      state = record_wake(state, principal)
      state = park_relighting(state, session.session_id, from, req)
      start_rejoin_worker(state, session)
    else
      audit_denial(state, principal, session.workload, :wake_rate)
      GenServer.reply(from, {:error, :wake_rate_limited})
      state
    end
  end

  defp start_rejoin_worker(state, session) do
    case SessionStore.mark(state.session_store, session.session_id, :relight) do
      {:ok, _} ->
        owner = self()
        volume_node_id = session.volume_node_id
        session_id = session.session_id
        # #4306 slice 1: the volume identity the prime/restore chain needs is
        # lineage_id, not session_id (no behavior change this slice, the two are
        # always equal until adoption ships).
        lineage_id = session.lineage_id

        spawn(fn ->
          outcome =
            Tracer.with_span "embervm.session.rejoin",
                             %{attributes: %{
                               "ember.session_id" => session_id,
                               "ember.workload" => session.workload,
                               "ember.principal" => session.principal,
                               "ember.volume_node_id" => volume_node_id
                             }} do
              with {:ok, vm_id, dial_id} <- perform_rejoin_prime(state, volume_node_id, session.workload, lineage_id) do
                # noded's auto-destroy timer covers PrimeAssign after delivery starts.
                # The CP destroys a successfully primed VM when delivery never starts.
                {:ok, volume_node_id, vm_id, 0, dial_id}
              else
                {:error, reason} -> {:error, reason}
                other -> {:error, other}
              end
            end

          send(owner, {:rejoin_done, session_id, outcome})
        end)

        state

      {:error, _} ->
        drain_relight_waiters(state, session.session_id, {:error, {:not_ready, :parked}})
    end
  end

  defp perform_rejoin_prime(state, volume_node_id, workload, lineage_id) do
    with {:ok, entry} <- fetch_session_workload(state, workload),
         {:ok, [brick | _]} <- Scheduler.place_with_demand(%Request{
           table: state.capacity_table, node_id: volume_node_id, workload: workload,
           key: workload, need_mib: Map.get(entry, :mem_mib) || 512,
           base: {:ready, :snapshot_ref}
         }),
         # Both the restore and the prime must reach the INSTANCE that owns this
         # lineage on disk. volume_node_id is a bare node name (an anchor), and
         # dialing it fails :unknown_node, which is exactly how the first live
         # rejoin died after create and park both worked.
         dial_id <- Embervm.WakeInstance.dial_for_session_volume(state.capacity_table, volume_node_id, lineage_id),
         {:ok, _restored} <- restore_session_workspace(state, dial_id, workload, lineage_id),
         snapshot_ref <- get_in(brick, [:workloads, workload, :snapshot_ref]),
         {:ok, vm_id} <- prime(state, dial_id, snapshot_ref, entry, lineage_id) do
      # Return dial_id because this is the third distinct bare-anchor dial site.
      # The previous fix resolved it correctly, then stopped one hop short by discarding it.
      {:ok, vm_id, dial_id}
    else
      {:error, reason} -> {:error, reason}
      other -> {:error, other}
    end
  end

  # Always ask noded about the workspace. A genuine store miss is the documented
  # first-boot case; any other restore failure must stop rejoin rather than prime
  # a blank image over data that may exist remotely. Returns {:ok, true} on an
  # actual hit, {:ok, false} on a tolerated store miss (#4306 slice 3 needs to
  # tell these apart to report `restored` on a create; a rejoin's one caller,
  # perform_rejoin_prime, only cares that it is `:ok`).
  defp restore_session_workspace(state, node_id, workload, lineage_id) do
    ref = %ArtifactRef{kind: :ARTIFACT_KIND_SESSION_WORKSPACE, workload: workload, ref: lineage_id}

    case safe_restore_artifact(state, node_id, node_id, ref) do
      {:ok, resp} ->
        record_restore(state, workload, :ARTIFACT_KIND_SESSION_WORKSPACE, lineage_id, resp)
        {:ok, true}

      {:error, %GRPC.RPCError{status: 5}} ->
        {:ok, false}

      {:error, reason} ->
        {:error, {:session_workspace_restore_failed, reason}}

      other ->
        {:error, {:session_workspace_restore_failed, other}}
    end
  end

  defp finish_rejoin(state, session_id, {:ok, node_id, vm_id, _ms, dial_id}) do
    session = get_session!(state, session_id)
    owner = self()
    failure_fun = fn reason -> send(owner, {:rejoin_assign_failed, session_id, reason}) end

    case start_session_from_row(state, %{session | node_id: node_id, vm_id: vm_id}, node_id, vm_id,
           dial_id, [rejoin_failure_fun: failure_fun]) do
      {:ok, pid} ->
        case SessionStore.transition(state.session_store, session_id, :rejoin_ready, :session_relit,
               %{volume_node_id: session.volume_node_id}, %{node_id: node_id, vm_id: vm_id}) do
          {:ok, _} -> drain_relight_into_process(state, session_id, pid)
          {:error, reason} ->
            _ = destroy_vm(state, %{node_id: node_id, vm_id: vm_id})
            drain_relight_waiters(state, session_id, {:error, reason})
        end
      {:error, reason} ->
        _ = destroy_vm(state, %{node_id: node_id, vm_id: vm_id})
        drain_relight_waiters(state, session_id, {:error, reason})
    end
  end

  defp finish_rejoin(state, session_id, {:error, reason}) do
    _ = SessionStore.mark(state.session_store, session_id, :parked_abort)
    drain_relight_waiters(state, session_id, {:error, {:relight_failed, reason}})
  end

  # Park a caller behind an in-flight relight for its session (FIFO within the
  # session), draining them all into the fresh process once the relight lands.
  defp park_relighting(state, session_id, from, req) do
    waiters = Map.get(state.relighting, session_id, [])
    %{state | relighting: Map.put(state.relighting, session_id, waiters ++ [{from, req}])}
  end

  # Kick off the relight: mark ETS banked -[relight]-> relighting (ETS-only, no op;
  # crash-consistency requires session_relit land only AFTER the daemon returns a
  # live vm_id, see finish_relight), then spawn a worker that Relights on the
  # resident node and reports back.
  defp start_relight(state, session) do
    case SessionStore.mark(state.session_store, session.session_id, :relight) do
      {:ok, _} ->
        spawn_relight_worker(state, session)
        state

      {:error, _} ->
        # Illegal (session moved off banked concurrently): drain any parked callers
        # for it as not_ready so they retry.
        drain_relight_waiters(state, session.session_id, {:error, {:not_ready, :banked}})
    end
  end

  defp spawn_relight_worker(state, session) do
    owner = self()
    capacity_table = state.capacity_table
    channel_fun = state.channel_fun
    relight_fun = state.relight_fun
    clock = state.clock
    session_id = session.session_id
    # The relight nests under the ROOT span of the invoke that triggered it: the
    # first parked caller's carried traceparent (concurrent invokes to one banked
    # session share this one relight). nil when tracing is off.
    traceparent = first_waiter_traceparent(state, session_id)

    # Restore-on-miss (R6, Task 8): the session's recorded node_id may no longer
    # report the bundle locally (disk lost, or a node restart that dropped it),
    # while the bundle is still exported (its store copy is present and current)
    # and the store is reachable. The DECISION (a pure ETS read) is taken here on
    # the serialized manager, cheap and consistent with the wake decision itself;
    # the RESTORE RPC, when the decision says to attempt one, runs inside the
    # spawned worker below (before node_for_relight's placement check), so park
    # semantics are unchanged and a slow store round-trip never blocks the
    # manager. A restore failure (or a genuinely unreachable store / a bundle
    # that was never exported) is fail-open: node_for_relight then makes exactly
    # the same call it always did, degrading to the daemon's existing
    # snapshot_lost failure rather than blocking the relight on store state.
    restore = restore_plan(state, session)

    spawn(fn ->
      SessionTrace.restore_parent(traceparent)

      Tracer.with_span "embervm.session.relight",
                       %{
                         attributes: %{
                           "ember.session_id" => session_id,
                           "ember.workload" => session.workload,
                           "ember.principal" => session.principal,
                           "ember.generation" => session.generation || 0
                         }
                       } do
        # Restore-on-miss: when the bundle is gone locally but exported, restore it
        # first and then relight against the SAME anchor node directly. The relight
        # must NOT re-consult node_for_relight after a restore: that guard checks the
        # CP's ETS session_snapshots fact, which the just-completed restore has not
        # yet refreshed (it updates on the next NodeStatus), so it would spuriously
        # report snapshot_lost and skip the relight the restore just enabled. A
        # restore FAILURE falls back to the normal node_for_relight path (fail-open).
        restore_target =
          case restore do
            {:restore, restore_node_id, restore_dial_id, restore_ref} ->
              # Restore onto, and later relight from, the SAME co-located instance
              # (restore_dial_id): a session snapshot is per-instance ON DISK
              # (PR-2.5), so restoring onto one brick while the relight dials another
              # leaves the relight's local disk empty. On a true local miss no
              # instance reports the snapshot, so this is a mem-eligible pick that both
              # the restore and the relight agree on.
              case restore_bundle(state, session.node_id, restore_dial_id, session.workload, restore_ref) do
                :ok -> {:ok, restore_dial_id}
                _ -> :none
              end

            _ ->
              :none
          end

        outcome =
          case relight_node(restore_target, session, capacity_table) do
            {:ok, dial_id} ->
              t0 = clock.()

              # dial_id is the OWNING instance the relight lands on (instance-key
              # unification PR-B0b); the durable row keeps the NODE name (session.
              # node_id), which adoption/drain read node-scoped. On the single-instance
              # fleet dial_id == node_id, so this is inert there.
              with {:ok, channel} <- safe_channel(channel_fun, dial_id),
                   {:ok, %RelightResponse{vm_id: vm_id}} when is_binary(vm_id) and vm_id != "" <-
                     safe_relight(relight_fun, channel, session) do
                relight_ms = clock.() - t0
                Tracer.set_attributes(%{"ember.relight_ms" => relight_ms})
                {:ok, session.node_id, vm_id, relight_ms, dial_id}
              else
                {:error, reason} -> classify_relight_error(reason)
                other -> classify_relight_error({:relight_failed, other})
              end

            {:error, :snapshot_lost} ->
              {:error, :snapshot_lost}
          end

        send(owner, {:relight_done, session_id, outcome})
      end
    end)
  end

  # -- restore-on-miss (R6, Task 8) -------------------------------------------

  # The node to relight against. After a SUCCESSFUL restore-on-miss the anchor node
  # is authoritative (the bundle was just restored there), bypassing the
  # node_for_relight snapshot-presence guard which the fresh restore has not yet
  # reflected in the CP's ETS facts. Otherwise the normal placement check.
  defp relight_node({:ok, node_id}, _session, _capacity_table), do: {:ok, node_id}
  defp relight_node(:none, session, capacity_table), do: WakeInstance.node_for_relight(session, capacity_table)

  # Whether the session's bundle should be restored before the relight: the
  # session's recorded node_id + snapshot_ref are both present, the bundle is NOT
  # locally reported on that node, and the node's store is reachable. Returns
  # `{:restore, node_id, snapshot_ref}` when a restore should be attempted, or
  # `:skip` when the bundle is already local, the node/ref is missing, or the
  # store is unreachable (fail-open: skip never blocks the relight, it only
  # withholds the restore attempt).
  defp restore_plan(state, session) do
    node_id = Map.get(session, :node_id)
    snapshot_ref = Map.get(session, :snapshot_ref)

    if is_binary(node_id) and node_id != "" and is_binary(snapshot_ref) and snapshot_ref != "" and
         not bundle_local?(state, node_id, snapshot_ref) and store_reachable?(state, node_id) do
      # The co-located instance to restore onto AND then relight from. On a true
      # local miss no instance reports the snapshot, so WakeInstance.select falls to a
      # mem-eligible pick sized for the session's mem_mib (the owning-instance branch
      # would apply only if an instance still reported it). Fail-open: no eligible
      # instance leaves the bare node_id, byte-identical to pre-co-location behaviour.
      {:restore, node_id, restore_dial_id(state, session, node_id, snapshot_ref), snapshot_ref}
    else
      :skip
    end
  end

  # The instance key to restore the session bundle onto (and relight from). Prefers
  # the instance still reporting the snapshot; else a mem-eligible pick (the true
  # local-miss case), sized by the catalog mem_mib; else the bare node_id.
  defp restore_dial_id(state, session, node_id, snapshot_ref) do
    need_mib =
      case WorkloadCatalog.fetch(state.catalog_table, Map.get(session, :workload)) do
        {:ok, entry} -> Map.get(entry, :mem_mib) || 512
        _ -> 512
      end

    case Embervm.WakeInstance.select(node_id,
           table: state.capacity_table,
           workload: Map.get(session, :workload),
           need_mib: need_mib,
           warmth_key: :session_snapshots,
           warmth_ref: snapshot_ref
         ) do
      {:ok, dial_id} -> dial_id
      {:error, _} -> node_id
    end
  end

  # Whether the node still reports the bundle on LOCAL disk (its snapshot_ref is in
  # the node's session_snapshots). A relight needs no restore when true; a true
  # local miss (false) may consult the store.
  defp bundle_local?(state, node_id, snapshot_ref) do
    case NodeCapacity.fetch(state.capacity_table, node_id) do
      {:ok, fact} ->
        fact
        |> Map.get(:session_snapshots, [])
        |> Enum.any?(&(Map.get(&1, :snapshot_ref) == snapshot_ref))

      :error ->
        false
    end
  end

  # The node's latest object-store reachability verdict (R6). Absent/false (a node
  # with no store configured, or one that never reported) reads as NOT reachable,
  # so no restore is attempted and the relight degrades to node_for_relight's
  # existing snapshot_lost path. This only gates the store consultation, never a
  # local-state relight.
  defp store_reachable?(state, node_id) do
    case NodeCapacity.fetch(state.capacity_table, node_id) do
      {:ok, fact} -> Map.get(fact, :store_reachable, false) == true
      :error -> false
    end
  end

  # Restore the SESSION bundle for `session_id` (implicit via workload/ref) from the
  # object store back onto `node_id`'s disk (RestoreArtifact, kind SESSION), then
  # record :artifact_restored. Best-effort: a restore failure returns :error and the
  # caller falls through to node_for_relight's existing check, which the daemon (or
  # the placement layer) degrades to snapshot_lost exactly as it would without this
  # feature (fail-open warmth). Idempotent on the daemon side.
  defp restore_bundle(state, node_id, dial_id, workload, snapshot_ref) do
    ref = %ArtifactRef{kind: :ARTIFACT_KIND_SESSION, workload: workload, ref: snapshot_ref}

    case safe_restore_artifact(state, node_id, dial_id, ref) do
      {:ok, resp} ->
        record_restore(state, workload, :ARTIFACT_KIND_SESSION, snapshot_ref, resp)
        :ok

      other ->
        Logger.warning("embervm session: bundle restore-on-miss failed, degrading to snapshot_lost path",
          workload: workload,
          snapshot_ref: snapshot_ref,
          reason: inspect(other)
        )

        :error
    end
  end

  defp safe_restore_artifact(state, node_id, dial_id, %ArtifactRef{} = ref) do
    req = %RestoreArtifactRequest{artifact: ref, trace: %Trace{workload: ref.workload}}
    # Stamp the vendor from the NODE (a node-scoped fact shared across its instances),
    # but DIAL the specific owning/target instance (dial_id): the restore must land on
    # the same co-located instance the relight then dials (PR-B0b), mirroring
    # ServingManager.safe_restore_artifact.
    req = Embervm.RestoreVendor.stamp(state.capacity_table, node_id, req)

    with {:ok, channel} <- safe_channel(state.channel_fun, dial_id) do
      # The `artifact_restore` span (Task 11): a child span around the
      # RestoreArtifact RPC (the restore-on-miss read path). Identity up front,
      # bytes-moved/skipped stamped from the response.
      Tracer.with_span "embervm.artifact_restore",
                       %{
                         attributes: %{
                           "ember.workload" => ref.workload,
                           "ember.artifact_kind" => artifact_kind_string(ref.kind),
                           "ember.artifact_ref" => ref.ref
                         }
                       } do
        result =
          try do
            state.restore_artifact_fun.(channel, req)
          rescue
            e -> {:error, {:restore_artifact_raised, e}}
          catch
            kind, reason -> {:error, {:restore_artifact_raised, {kind, reason}}}
          end

        stamp_restore_span(result)
        result
      end
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

  # Append the audit-only :artifact_restored op (no projection table; the log
  # itself is the record). Best-effort: an append failure must never fail the
  # relight, which already ran the restore RPC (the durable state is the restored
  # bytes on disk, not this audit row).
  defp record_restore(state, workload, kind, ref, resp) do
    op = %Embervm.OpLog.Op{
      kind: :artifact_restored,
      tenant: state.tenant,
      principal: workload_principal(state, workload),
      workload: workload,
      ts: state.clock.(),
      payload: %{
        kind: artifact_kind_string(kind),
        ref: ref,
        bytes_moved: Map.get(resp, :bytes_moved, 0),
        generation: Map.get(resp, :generation, 0),
        skipped: Map.get(resp, :skipped, false)
      }
    }

    _ = state.op_log_mod.append(state.op_log, op)
    :ok
  rescue
    e ->
      Logger.warning("embervm session: artifact_restored append raised", workload: workload, error: inspect(e))
      :ok
  end

  defp artifact_kind_string(:ARTIFACT_KIND_SESSION), do: "session"
  defp artifact_kind_string(:ARTIFACT_KIND_SESSION_WORKSPACE), do: "session-workspace"
  defp artifact_kind_string(other), do: to_string(other)

  # The op-log principal attribution for a workload-scoped audit row (the restore/
  # remote-evict ops carry no single session's principal, since a bundle-level
  # store action is keyed by workload, not by the individual session that happened
  # to trigger it). Mirrors StatefulManager.wake_principal/1's synthesized-owner
  # idiom for a class with no natural single owner at this granularity.
  defp workload_principal(_state, workload), do: "system:session:#{workload}"

  # The traceparent carried on the FIRST parked invoke for `session_id` (they share
  # the relight), or nil when the ledger is empty / tracing is off.
  defp first_waiter_traceparent(state, session_id) do
    case Map.get(state.relighting, session_id, []) do
      [{_from, req} | _] -> Map.get(req, :traceparent)
      _ -> nil
    end
  end

  defp safe_relight(relight_fun, channel, session) do
    req = %RelightRequest{
      trace: %Trace{workload: session.workload},
      snapshot_ref: session.snapshot_ref,
      session_id: session.session_id
    }

    relight_fun.(channel, req)
  rescue
    e -> {:error, {:relight_raised, e}}
  catch
    kind, reason -> {:error, {:relight_raised, {kind, reason}}}
  end

  # A FAILED_PRECONDITION from Relight means the snapshot is unrestorable: the
  # session is lost (snapshot_lost -> 410). Any other transport error is a retryable
  # relight failure (the snapshot is NOT deleted on a failed restore, per the proto),
  # surfaced as a generic error the parked caller sees; the session stays banked.
  defp classify_relight_error(%GRPC.RPCError{status: 9}), do: {:error, :snapshot_lost}
  defp classify_relight_error({:relight_failed, %GRPC.RPCError{status: 9}}), do: {:error, :snapshot_lost}
  defp classify_relight_error(reason), do: {:error, {:relight, reason}}

  # Relight completed. On success: append session_relit (NOW, after a live vm_id),
  # move ETS to running with the fresh residency, start the process, and drain the
  # parked callers into it. On snapshot_lost: fail the session, evict the snapshot,
  # 410 the parked callers. On a transient relight error: leave the session banked
  # and reply the error to parked callers (they may retry, re-relighting).
  defp finish_relight(state, session_id, outcome) do
    case SessionStore.get(state.session_store, session_id) do
      # The session went terminal mid-relight (a concurrent destroy/expire): the
      # relight's live VM, if any, is orphaned and adoption/next-sweep reaps it. Drain
      # any parked callers gone and drop the ledger. Never start a process for a
      # terminal session.
      {:ok, %{state: st}} when st in [:expired, :evicted, :destroyed, :failed] ->
        drain_relight_waiters(state, session_id, {:error, {:gone, to_string(st)}})

      _ ->
        finish_relight_active(state, session_id, outcome)
    end
  end

  defp finish_relight_active(state, session_id, outcome) do
    case outcome do
      {:ok, node_id, vm_id, relight_ms, dial_id} ->
        session = get_session!(state, session_id)

        # The wake hot-path durable append (session_relit): deferred to AsyncWriter
        # under EMBERVM_ASYNC_LIFECYCLE_WRITES (ADR embervm/014 decision 2), so the
        # woken session's durable write is off the wake path; the ETS row advances
        # synchronously first (routable at once). vm_id registers the pending write
        # for the adopt-and-backfill discriminator. Gate off: write-through.
        _ =
          SessionStore.transition_lifecycle(
            state.session_store,
            session_id,
            :relight_ready,
            :session_relit,
            %{snapshot_ref: session.snapshot_ref, generation: session.generation, relight_ms: relight_ms},
            %{node_id: node_id, vm_id: vm_id},
            vm_id
          )

        state = clear_bank_failures(state, session_id)

        case start_session_from_row(state, %{session | node_id: node_id, vm_id: vm_id}, node_id, vm_id, dial_id) do
          {:ok, pid} ->
            Logger.info("embervm session relit",
              session_id: session_id,
              workload: session.workload,
              principal: session.principal,
              relight_ms: relight_ms
            )
            drain_relight_into_process(state, session_id, pid)

          {:error, reason} ->
            fail_session(state, session_id, :failed, "relit process start failed: #{inspect(reason)}")
            drain_relight_waiters(state, session_id, {:error, :failed})
        end

      {:error, :snapshot_lost} ->
        session = get_session!(state, session_id)
        fail_session(state, session_id, :snapshot_lost, "snapshot_lost")
        _ = evict_snapshot(state, session)
        drain_relight_waiters(state, session_id, {:error, {:gone, "snapshot_lost"}})

      {:error, reason} ->
        # Transient (non-precondition) failure: the snapshot is intact (the proto
        # never deletes it on a failed restore), so return ETS relighting -> banked
        # (ETS-only, no op) and reply the error. A later invoke re-relights.
        Logger.warning("embervm session relight failed", session_id: session_id, reason: inspect(reason))
        abort_event =
          case SessionStore.get(state.session_store, session_id) do
            {:ok, %{volume_node_id: volume_node_id}} when is_binary(volume_node_id) -> :parked_abort
            _ -> :relight_abort
          end

        _ = SessionStore.mark(state.session_store, session_id, abort_event)
        drain_relight_waiters(state, session_id, {:error, {:relight_failed, reason}})
    end
  end

  # Drain every parked caller for a relit session by forwarding its req into the
  # fresh session process (FIFO order), then clear the relighting ledger. Each
  # forward runs off the manager so a long guest round-trip does not serialize.
  defp drain_relight_into_process(state, session_id, pid) do
    waiters = Map.get(state.relighting, session_id, [])
    for {from, req} <- waiters, do: spawn_forward(pid, req, from)
    %{state | relighting: Map.delete(state.relighting, session_id)}
  end

  # Reply `reply` to every parked caller for a session (a failed/lost relight) and
  # clear the ledger.
  defp drain_relight_waiters(state, session_id, reply) do
    waiters = Map.get(state.relighting, session_id, [])
    for {from, _req} <- waiters, do: GenServer.reply(from, reply)
    %{state | relighting: Map.delete(state.relighting, session_id)}
  end

  # -- wake-rate limit (Task 8) ----------------------------------------------

  # A sliding-window per-principal relight count: allowed while the count of relight
  # timestamps within the window is below wake_max. wake_max <= 0 disables the limit
  # entirely (never rate-limit), matching "0 = off" config idioms; a positive limit
  # is enforced.
  defp wake_allowed?(%{wake_max: max}, _principal) when not is_integer(max) or max <= 0, do: true

  defp wake_allowed?(state, principal) do
    now = state.clock.()
    recent = recent_wakes(state, principal, now)
    length(recent) < state.wake_max
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

  # -- adoption (Task 8) -----------------------------------------------------

  # Reconcile the ETS projection against every node's reported session inventory
  # (the #3517 drill lesson applied to sessions: the node is the source of truth,
  # the control plane adopts, NEVER reaps on a transient disconnect). For each
  # non-terminal session:
  #
  #   * running/relighting/banking with a node-reported LIVE VM -> rebind residency
  #     and (re)start the process bound to that vm_id. Heals a control-plane restart
  #     (the durable state is running but the process is gone) and heals banking/
  #     relighting limbo where the node actually holds a live VM.
  #   * banked, or banking/relighting where the node reports the SNAPSHOT (not a VM)
  #     -> heal ETS to banked (the bank/relight did not complete; the snapshot is the
  #     truth). No process.
  #   * neither a VM nor a snapshot reported for the session -> the VM and snapshot
  #     both vanished (node death after a live-only session, or an out-of-band wipe):
  #     mark it failed. This is the ONLY reaping, and only when node truth confirms
  #     the state is gone, never on a transient absence of the whole node's facts.
  #
  # Then evict snapshots the node reports whose session row is terminal or absent.
  #
  # NEVER reap when a node's facts are simply missing (a disconnect): a session on a
  # node not currently in the capacity table is left untouched, exactly the pool's
  # additive-only rule.
  defp do_reconcile(state) do
    facts = NodeCapacity.all(state.capacity_table)
    live_vms = index_session_vms(facts)
    snapshots = index_session_snapshots(facts)
    nodes_reporting_snaps = nodes_reporting_snapshots(facts)
    nodes_facts = index_node_facts(facts)

    state = clear_changed_unapplicable(state)

    state =
      SessionStore.all(state.session_store)
      |> Enum.reject(&SessionState.terminal?(&1.state))
      |> Enum.reduce(state, fn session, acc ->
        adopt_one(acc, session, live_vms, snapshots, nodes_reporting_snaps, nodes_facts)
      end)

    state = evict_orphan_snapshots(state, facts)
    state = retire_orphan_session_volumes(state, facts, nodes_facts)

    # Fail-closed reconciliation toward destruction (ADR embervm/014 decision 5),
    # gated: re-drive stuck destroying sessions (Direction 1 completion + alarm) and
    # destroy reported session VMs with no CP row (Direction 2 orphans). Gate off:
    # inert, so today's behaviour is unchanged.
    if state.node_confirmed_destroy do
      state
      |> redrive_destroying(live_vms)
      |> destroy_orphan_session_vms(facts, live_vms)
    else
      state
    end
  end

  # Direction 1 completion: a session left in destroying (RPC unconfirmed, or a CP
  # crash after the destroying intent) is re-driven here. If its owner still reports
  # the VM, re-issue the node-confirmed teardown and, on confirmation, record
  # destroyed. If the owner no longer reports the VM (teardown actually completed but
  # the destroyed op was lost), the destruction is confirmed by absence: record
  # destroyed. An alarm fires (error-level, SigNoz-visible) if a destroying session
  # persists past destroying_alarm_ms.
  defp redrive_destroying(state, live_vms) do
    now = state.clock.()

    destroying =
      SessionStore.all(state.session_store)
      |> Enum.filter(&(&1.state == :destroying))

    # Prune the alarmed set to sessions still destroying: a terminalized id must not
    # leak (unbounded growth) and a future re-destroy of the same id should re-alarm.
    still = MapSet.new(destroying, & &1.session_id)
    state = %{state | destroying_alarmed: MapSet.intersection(state.destroying_alarmed, still)}

    Enum.reduce(destroying, state, fn session, acc ->
      acc = maybe_alarm_destroying(acc, session, now)
      redrive_one_destroying(acc, session, live_vms)
    end)
  end

  # Alarm ONCE per stuck session (dedup via destroying_alarmed): reconcile runs every
  # few seconds, so logging every tick would flood SigNoz for a genuinely stuck
  # destroy. Returns the updated state (the alarmed set is threaded through redrive).
  defp maybe_alarm_destroying(state, session, now) do
    id = session.session_id
    elapsed = now - session.updated_at

    if elapsed > state.destroying_alarm_ms and not MapSet.member?(state.destroying_alarmed, id) do
      Logger.error("embervm session stuck in destroying",
        session_id: id,
        workload: session.workload,
        vm_id: session.vm_id,
        elapsed_ms: elapsed,
        alarm_threshold_ms: state.destroying_alarm_ms
      )

      %{state | destroying_alarmed: MapSet.put(state.destroying_alarmed, id)}
    else
      state
    end
  end

  defp redrive_one_destroying(state, session, live_vms) do
    sid = session.session_id

    cond do
      # Owner still reports the VM: retry the node-confirmed teardown. Terminate any
      # lingering process first (a same-CP retry after a failed RPC), then re-issue
      # the Destroy; a CP-crash re-drive has no process, so this is a no-op there.
      Map.has_key?(live_vms, sid) ->
        confirmed = stop_session_process(state, sid, session)

        if confirmed do
          record_session_destroyed(state, session)
        else
          state
        end

      # Owner no longer reports the VM but IS reporting (its absence is authoritative):
      # teardown completed, only the destroyed op was lost. Confirm by absence.
      node_reporting?(state, session.node_id) ->
        record_session_destroyed(state, session)

      # Owner not reporting (a disconnect): leave it destroying, never terminalize on
      # a transient absence of the whole node's facts.
      true ->
        state
    end
  end

  defp record_session_destroyed(state, session) do
    _ =
      SessionStore.transition(
        state.session_store,
        session.session_id,
        :destroy,
        :session_destroyed,
        %{reason: :destroyed},
        %{}
      )

    Logger.info("embervm session destroyed (reconcile-confirmed)",
      session_id: session.session_id,
      workload: session.workload
    )

    state
  end

  # Direction 2: a node reports a live session VM whose session_id no CP row matches.
  # It is an orphan to DESTROY (node-confirmed), UNLESS it is a young async-write
  # race (ADR embervm/014 decision 2): under EMBERVM_ASYNC_LIFECYCLE_WRITES the
  # durable session_created append is deferred, so between an interactive VM and its
  # durable row there is a window where a fresh report shows the VM but its durable
  # row is not yet written. Under Option A the create's ETS row IS advanced
  # synchronously at commit, so the discriminator queries the LIVE store/writer
  # state (present in the commit..append window), not only durable rows:
  #
  #   * a pending async write references this vm_id (Embervm.AsyncWriter.pending?/2)
  #     => ADOPT: the deferred append is in flight and IS the backfill; leave the VM.
  #   * a live in-memory session row references this vm_id (its durable append was
  #     lost, e.g. writer crash, but the ETS row survived) => ADOPT and actively
  #     RE-DRIVE the missing session_created append from that row so the durable
  #     projection catches up (the "resurfaces and adopts+backfills" ADR repair).
  #   * neither => a genuine orphan (a leaked VM with no owning write): destroy it,
  #     node-confirmed, exactly as PR 1 did.
  #
  # Primed-pool VMs are reported separately (WorkloadCapacity.primed_vm_ids), never
  # in session_vms, so a session VM here is never a primed VM. The gate-off path is
  # unchanged from PR 1 (no pending writes / async rows exist, so every rowless VM
  # is an orphan).
  defp destroy_orphan_session_vms(state, facts, _live_vms) do
    for f <- facts, v <- Map.get(f, :session_vms, []) || [], reduce: state do
      acc ->
        case SessionStore.get(acc.session_store, v.session_id) do
          :error ->
            reconcile_rowless_session_vm(acc, f, v)

          {:ok, _} ->
            acc
        end
    end
  end

  defp reconcile_rowless_session_vm(state, fact, v) do
    case adopt_discriminator(state, v.vm_id) do
      :pending ->
        # The deferred append is still in flight: it will create the durable row.
        Logger.info("embervm session vm adopted (async write in flight)",
          session_id: v.session_id,
          vm_id: v.vm_id,
          node_id: fact.configured_id
        )

        state

      {:backfill, row} ->
        # A live ETS row references the vm but its durable append was lost: actively
        # re-drive session_created from the surviving row (idempotent via INSERT OR
        # IGNORE) so the durable projection catches up. adopt_one rebinds it after.
        _ = SessionStore.backfill_created(state.session_store, row.session_id)

        Logger.info("embervm session vm adopted+backfilled (async write lost)",
          session_id: row.session_id,
          vm_id: v.vm_id,
          node_id: fact.configured_id
        )

        state

      :orphan ->
        confirmed =
          destroy_vm(state, %{
            node_id: fact.configured_id,
            vm_id: v.vm_id
          })

        Logger.warning("embervm orphan session vm destroyed",
          session_id: v.session_id,
          vm_id: v.vm_id,
          node_id: fact.configured_id,
          teardown_confirmed: confirmed
        )

        state
    end
  end

  # The adopt-vs-destroy discriminator. Only meaningful under the async gate (gate
  # off: no deferred writes / async rows, so always :orphan, PR 1 behaviour). Prefers
  # the pending-write signal (the append is landing on its own); else looks for a
  # live in-memory row referencing the vm (a lost append to re-drive); else :orphan.
  defp adopt_discriminator(%{async_lifecycle_writes: false}, _vm_id), do: :orphan

  defp adopt_discriminator(state, vm_id) do
    cond do
      Embervm.AsyncWriter.pending?(state.async_writer, vm_id) ->
        :pending

      row = session_row_for_vm(state, vm_id) ->
        {:backfill, row}

      true ->
        :orphan
    end
  end

  defp session_row_for_vm(state, vm_id) do
    state.session_store
    |> SessionStore.all()
    |> Enum.find(fn s -> s.vm_id == vm_id and not SessionState.terminal?(s.state) end)
  end

  # vm session_id -> {node_id, vm_id, dial_id}; snapshot session_id ->
  # {node_id, snapshot_ref}. dial_id is the REPORTING instance's channel key (its
  # instance_id, else the node name) so an adopted live session dials the co-located
  # instance actually running the VM, not the node-name alias (PR-B0b).
  defp index_session_vms(facts) do
    for f <- facts, v <- Map.get(f, :session_vms, []) || [], into: %{} do
      {v.session_id, {f.configured_id, v.vm_id, fact_dial_id(f)}}
    end
  end

  # The channel key for a capacity fact: its instance_id when present, else the node
  # name (legacy/single-instance facts resolve via the node-name alias, unchanged).
  defp fact_dial_id(fact) do
    case Map.get(fact, :instance_id) do
      id when is_binary(id) and id != "" -> id
      _ -> Map.get(fact, :configured_id)
    end
  end

  defp index_session_snapshots(facts) do
    for f <- facts, s <- Map.get(f, :session_snapshots, []) || [], into: %{} do
      {s.session_id, {f.configured_id, s.snapshot_ref}}
    end
  end


  # The authoritative-timestamp guard below is the real protection for eviction:
  # a banked session is only evicted on positive evidence that its snapshot is gone,
  # never on a missing or lagging report.
  defp nodes_reporting_snapshots(facts) do
    result = MapSet.new(facts, fn f ->
      case Map.get(f, :session_snapshots) do
        nil -> nil
        _ -> f.configured_id
      end
    end)
    |> MapSet.delete(nil)
    result
  end

  # Map of node_id -> node fact, keyed by configured_id (the node name). Used by
  # adoption to check whether a node's snapshot report was generated after a session
  # was banked (evidence ordering for vanished detection).
  defp index_node_facts(facts) do
    facts
    |> Enum.group_by(& &1.configured_id)
    |> Map.new(fn {configured_id, siblings} ->
      {configured_id, Enum.max_by(siblings, &Map.get(&1, :updated_at, 0))}
    end)
  end

  defp adopt_one(state, session, live_vms, snapshots, nodes_reporting_snaps, nodes_facts) do
    sid = session.session_id

    cond do
      # A session being torn down (destroying, ADR embervm/014 decision 5) must NOT be
      # re-adopted to running even though the node still reports its live VM: the
      # teardown RPC is in flight. redrive_destroying (later in do_reconcile) owns it.
      session.state == :destroying ->
        state

      session.state == :parked ->
        state

      session.state == :parking and not Map.has_key?(live_vms, sid) ->
        _ = SessionStore.transition(state.session_store, sid, :park_complete, :session_parked,
          %{reason: "idled", volume_node_id: session.volume_node_id},
          %{node_id: nil, vm_id: nil, volume_node_id: session.volume_node_id})
        state

      # This manager has an in-flight bank/relight for the session: it owns the
      # transition, so a periodic reconcile must NOT touch it. During a bank the node
      # still reports the live VM, and forcing ETS to running here would make the
      # pending finish_bank's session_banked transition illegal (and silently lost).
      # On BOOT these maps are empty (fresh process), so boot adoption still fully
      # heals every limbo; only the periodic sweep is guarded.
      Map.has_key?(state.banking, sid) or Map.has_key?(state.relighting, sid) ->
        state

      # The node reports a LIVE VM for this session: rebind it (residency + process),
      # regardless of whether ETS thinks it is running/banking/relighting.
      Map.has_key?(live_vms, sid) ->
        {node_id, vm_id, dial_id} = Map.fetch!(live_vms, sid)
        adopt_live(state, session, node_id, vm_id, dial_id)

      # No live VM, but the node reports its SNAPSHOT: it is banked (or a bank/relight
      # that did not finish leaving only the snapshot). Heal ETS to banked.
      Map.has_key?(snapshots, sid) ->
        heal_to_banked(state, session, snapshots)

      # A session the current node facts cover neither as a VM nor a snapshot. Only
      # reap if the node whose id the session records IS reporting (its absence is
      # then authoritative, the state truly vanished); if that node is not in the
      # facts at all (a disconnect), leave the session untouched. Only treat a snapshot
      # as vanished when the node's snapshot report is AUTHORITATIVE (the field is
      # present). Absence of evidence (no snapshot report yet) is not evidence of absence.
      # Under the node-confirmed-destroy gate a grace window (orphan_grace_ms since
      # the row was last updated) is honoured first, so an owner-resolved dial that
      # momentarily omits a just-created VM does not terminalize it (ADR embervm/014).
      node_reporting?(state, session.node_id) and MapSet.member?(nodes_reporting_snaps, session.node_id) and
        orphan_grace_elapsed?(state, session) and
        # The snapshot report must also be AUTHORITATIVE in time: the node's fact must
        # postdate the session's bank. A fact older than the session update is a stale
        # report, not evidence the snapshot vanished.
        node_fact_authoritative?(nodes_facts, session.node_id, session.updated_at) ->
        case session.state do
          :banked ->
            evict_banked(state, session, :snapshot_vanished)

          session_state when session_state in [:running, :banking, :relighting, :creating] ->
            fail_adopted_session(state, session)

          _ ->
            state
        end

      true ->
        state
    end
  end

  defp clear_changed_unapplicable(state) do
    sessions = Map.new(SessionStore.all(state.session_store), &{&1.session_id, &1.state})

    Enum.reduce(state.unapplicable_states, state, fn {session_id, logged_state}, acc ->
      if Map.get(sessions, session_id) == logged_state do
        acc
      else
        %{
          acc
          | logged_unapplicable: MapSet.delete(acc.logged_unapplicable, session_id),
            unapplicable_states: Map.delete(acc.unapplicable_states, session_id)
        }
      end
    end)
  end

  defp fail_adopted_session(state, session) do
    case SessionStore.transition(
           state.session_store,
           session.session_id,
           :fail,
           :session_failed,
           %{reason: :failed, detail: "vm_and_snapshot_vanished"},
           %{}
         ) do
      {:ok, _} ->
        state

      {:error, err} ->
        if MapSet.member?(state.logged_unapplicable, session.session_id) do
          state
        else
          Logger.warning("embervm session fail transition failed",
            session_id: session.session_id,
            error: inspect(err)
          )

          %{
            state
            | logged_unapplicable: MapSet.put(state.logged_unapplicable, session.session_id),
              unapplicable_states: Map.put(state.unapplicable_states, session.session_id, session.state)
          }
        end
    end
  end

  # Rebind a live session VM to a fresh process (idempotent). Forces ETS to running
  # from node truth (a banking/relighting/creating limbo whose node actually holds a
  # live VM), writes the residency fact + vm_id, and starts a process if none runs.
  defp adopt_live(state, session, node_id, vm_id, dial_id) do
    SessionStore.adopt_state(state.session_store, session.session_id, :running)
    SessionStore.adopt_residency(state.session_store, session.session_id, node_id, vm_id)

    case Registry.lookup(state.registry, session.session_id) do
      [{_pid, _}] ->
        # Already has a process (adoption ran twice, or the process outlived a sweep):
        # leave it. The residency/state heal above is idempotent.
        state

      [] ->
        case start_session_from_row(state, %{session | node_id: node_id, vm_id: vm_id}, node_id, vm_id, dial_id) do
          {:ok, _pid} ->
            Logger.info("embervm session adopted (live)", session_id: session.session_id, node_id: node_id)
            state

          {:error, reason} ->
            Logger.warning("embervm session adopt-live start failed",
              session_id: session.session_id,
              reason: inspect(reason)
            )

            state
        end
    end
  end

  # A session the node reports only as a snapshot: it is banked. Force ETS to banked
  # (idempotent) so a banking/relighting limbo whose bank/relight did not complete
  # resolves to the truth (the snapshot exists, no VM), dropping residency so the
  # next invoke routes as a relight miss.
  defp heal_to_banked(state, %{state: :banked}, _snapshots), do: state

  defp heal_to_banked(state, session, _snapshots) do
    SessionStore.adopt_state(state.session_store, session.session_id, :banked)
    Logger.info("embervm session adopted (banked)", session_id: session.session_id)
    state
  end

  defp node_reporting?(state, node_id) when is_binary(node_id) do
    match?({:ok, _}, NodeCapacity.fetch(state.capacity_table, node_id))
  end

  defp node_reporting?(_state, _node_id), do: false

  # Gate off: no grace (today's behaviour, immediate terminalization on an
  # owner-resolved dial). Gate on: the session's row must be older than orphan_grace_ms
  # before an owner-resolved dial may terminalize it (ADR embervm/014 decision 5), so a
  # just-created VM that momentarily does not appear in the owner's report is not lost.
  defp orphan_grace_elapsed?(%{node_confirmed_destroy: false}, _session), do: true

  defp orphan_grace_elapsed?(state, session) do
    state.clock.() - session.updated_at >= state.orphan_grace_ms
  end

  # Check if the node's snapshot report is authoritative (current): the fact's
  # updated_at must be greater than or equal to the session's updated_at (when it
  # was banked), meaning the node has had a chance to report the snapshot.
  defp node_fact_authoritative?(nodes_facts, node_id, session_updated_at) do
    case Map.get(nodes_facts, node_id) do
      %{updated_at: fact_updated_at} when fact_updated_at >= session_updated_at -> true
      _ -> false
    end
  end

  # Evict snapshots a node reports whose session row is terminal or absent: the
  # session is gone but its snapshot squats disk. EvictSnapshot is idempotent, so a
  # double-evict is harmless.
  defp evict_orphan_snapshots(state, facts) do
    for f <- facts, snap <- Map.get(f, :session_snapshots, []) || [] do
      case SessionStore.get(state.session_store, snap.session_id) do
        {:ok, %{state: st}} when st in [:expired, :evicted, :destroyed, :failed] ->
          _ = evict_snapshot_on_node(state, f.configured_id, snap.snapshot_ref, f.node_id, snap.workload)

        :error ->
          _ = evict_snapshot_on_node(state, f.configured_id, snap.snapshot_ref, f.node_id, snap.workload)

        _ ->
          :ok
      end
    end

    state
  end

  # Raw session workspaces are node facts, not snapshot warmth. Retire them so
  # the node exports to S3 before removing the local copy. NodeCapacity freshness
  # and row age use their respective clock domains; first-seen grace protects
  # create-in-flight windows when the row is absent.
  defp retire_orphan_session_volumes(state, facts, nodes_facts) do
    monotonic_now = state.monotonic_clock.()
    wall_now = state.clock.()

    reported =
      for f <- facts, volume <- Map.get(f, :session_volumes, []) || [], into: MapSet.new() do
        {f.configured_id, volume.lineage_id}
      end

    first_seen =
      state.orphan_volume_first_seen
      |> Enum.filter(fn {key, _} -> MapSet.member?(reported, key) end)
      |> Map.new()

    state = %{state | orphan_volume_first_seen: first_seen}

    Enum.reduce(facts, state, fn f, acc ->
      fact = Map.get(nodes_facts, f.configured_id, f)
      fact_updated = Map.get(fact, :updated_at, 0)
      fact_current = monotonic_now - fact_updated <= acc.fleet_freshness_window_ms

      Enum.reduce(Map.get(f, :session_volumes, []) || [], acc, fn volume, inner ->
        key = {f.configured_id, volume.lineage_id}

        # #4306 slice 1: looked up BY LINEAGE, not by session_id (SessionStore.get/2
        # would treat volume.lineage_id as a session id, which is only correct
        # today because the two are always equal). get_latest_by_lineage/2 answers
        # the real orphan-reconcile question, "is the NEWEST holder of this
        # lineage terminal", the shape a later adoption slice needs when a lineage
        # can outlive the session_id that first claimed it. No behavior change
        # this slice: the newest holder IS the only session IS the id.
        case SessionStore.get_latest_by_lineage(inner.session_store, volume.lineage_id) do
          {:ok, %{workload: workload, state: session_state}}
              when workload == volume.workload and session_state not in [:expired, :evicted, :destroyed, :failed] ->
            %{inner | orphan_volume_first_seen: Map.delete(inner.orphan_volume_first_seen, key)}

          {:ok, %{state: session_state, updated_at: row_updated}}
              when session_state in [:expired, :evicted, :destroyed, :failed] ->
            inner = %{inner | orphan_volume_first_seen: Map.delete(inner.orphan_volume_first_seen, key)}
            if fact_current and wall_now - row_updated >= inner.orphan_grace_ms do
              _ = retire_session_volume(inner, %{volume_node_id: f.configured_id, workload: volume.workload, lineage_id: volume.lineage_id})
            end
            inner

          _ ->
            seen = Map.get(inner.orphan_volume_first_seen, key, monotonic_now)
            inner = %{inner | orphan_volume_first_seen: Map.put(inner.orphan_volume_first_seen, key, seen)}
            if fact_current and monotonic_now - seen >= inner.orphan_grace_ms do
              _ = retire_session_volume(inner, %{volume_node_id: f.configured_id, workload: volume.workload, lineage_id: volume.lineage_id})
            end
            inner
        end
      end)
    end)
  end

  # -- sweep: expiry, banked-TTL GC, disk-pressure eviction (Task 7) ---------

  defp do_sweep(state) do
    now = state.clock.()

    state
    |> sweep_expiry(now)
    |> sweep_banked_ttl(now)
    |> sweep_disk_pressure()
    |> write_session_status()
  end

  # -- status.sessions counts (Task 9) ---------------------------------------

  # Write status.sessions {live,banked} + sessionsSummary for every session-class
  # workload, DEBOUNCED: only patch a workload whose live/banked pair changed since
  # its last write. Runs on the sweep tick (not on every transition), so the K8s API
  # is touched at most once per session workload per sweep, never per transition.
  # Disjoint status keys, so the merge-patch never clobbers the watcher/pool writers.
  defp write_session_status(state) do
    session_workloads(state)
    |> Enum.reduce(state, fn %{workload: workload, namespace: namespace}, acc ->
      counts = SessionStore.counts(acc.session_store, workload)
      pair = {counts.live, counts.banked}

      if Map.get(acc.session_status_written, workload) == pair do
        acc
      else
        _ = patch_session_status(acc, namespace, workload, counts)
        %{acc | session_status_written: Map.put(acc.session_status_written, workload, pair)}
      end
    end)
  end

  # Every cataloged session-class workload with its namespace, for the status write.
  # A catalog with no session workloads yields [], so this is a no-op on task-only
  # clusters.
  defp session_workloads(state) do
    for name <- WorkloadCatalog.all_names(state.catalog_table),
        {:ok, %{class: "session", namespace: namespace}} <- [WorkloadCatalog.fetch(state.catalog_table, name)],
        is_binary(namespace) do
      %{workload: name, namespace: namespace}
    end
  end

  defp patch_session_status(state, namespace, name, counts) do
    status_map = %{
      "sessions" => %{"live" => counts.live, "banked" => counts.banked},
      "sessionsSummary" => "#{counts.live} live / #{counts.banked} banked"
    }

    case state.status_writer.(namespace, name, status_map) do
      :ok ->
        :ok

      {:error, reason} ->
        # Visibility-only: a status-write failure must never crash the sweep.
        Logger.warning("embervm session status patch failed for #{namespace}/#{name}: #{inspect(reason)}")
    end
  rescue
    e -> Logger.warning("embervm session status patch raised for #{namespace}/#{name}: #{inspect(e)}")
  catch
    _, _ -> :ok
  end

  # Max-lifetime expiry (independent of the invoke-time check): any non-terminal
  # session past expires_at is expired. Live -> destroy the VM; banked -> evict the
  # snapshot. Appends session_expired either way.
  defp sweep_expiry(state, now) do
    SessionStore.all(state.session_store)
    # Only running/banked have an :expire FSM edge; a transient banking/relighting/
    # creating session settles within a sweep and is expired on the next pass.
    |> Enum.filter(fn s -> s.state in [:running, :banked, :parked, :parking] end)
    |> Enum.filter(fn s -> is_integer(s.expires_at) and s.expires_at <= now end)
    |> Enum.reduce(state, fn session, acc -> expire_session(acc, session) end)
  end

  defp expire_session(state, session) do
    if session.state in [:banked, :parked] do
      _ = evict_snapshot(state, session)
    else
      _ = stop_session_process(state, session.session_id, session)
    end

    retire_session_volume(state, session)

    _ =
      SessionStore.transition(
        state.session_store,
        session.session_id,
        :expire,
        :session_expired,
        %{reason: :expired},
        %{}
      )

    Logger.info("embervm session expired",
      session_id: session.session_id,
      workload: session.workload,
      principal: session.principal
    )

    state
  end

  # Banked/parked-TTL GC (#4305): a banked or parked session untouched
  # (last_invoke_at, or banked/parked-at via updated_at) for longer than its
  # workload's bankedTtlSeconds is evicted (session_evicted, reason idle_ttl).
  # A banked session's snapshot is released via EvictSnapshot (evict_banked); a
  # parked session holds no snapshot, so it retires its workspace volume
  # instead (evict_parked), the same disk it would surrender on max-lifetime
  # expiry (expire_session's parked arm). Without this, an abandoned parked
  # session sat only behind maxLifetimeSeconds (hours) and could squat a
  # session.maxSessions slot for the whole window.
  defp sweep_banked_ttl(state, now) do
    SessionStore.all(state.session_store)
    |> Enum.filter(&(&1.state in [:banked, :parked]))
    |> Enum.reduce(state, fn session, acc ->
      case fetch_session_workload(acc, session.workload) do
        {:ok, entry} ->
          ttl_ms = entry.session.banked_ttl_seconds * 1000
          last = session.last_invoke_at || session.updated_at || 0

          if now - last >= ttl_ms do
            case session.state do
              :banked -> evict_banked(acc, session, :idle_ttl)
              :parked -> evict_parked(acc, session, :idle_ttl)
            end
          else
            acc
          end

        {:error, _} ->
          acc
      end
    end)
  end

  # Disk-pressure eviction: for each node below its snapshot-disk low watermark,
  # evict banked sessions LRU by last_invoke_at (NEVER live, NEVER non-session
  # snapshots) until free bytes rise above the watermark. Each an audited
  # session_evicted, reason disk_pressure. Disabled when no watermark is configured.
  defp sweep_disk_pressure(%{disk_low_watermark_bytes: nil} = state), do: state

  defp sweep_disk_pressure(state) do
    NodeCapacity.all(state.capacity_table)
    |> Enum.reduce(state, fn fact, acc -> evict_node_to_watermark(acc, fact) end)
  end

  defp evict_node_to_watermark(state, fact) do
    free = Map.get(fact, :snapshot_disk_free_bytes)
    watermark = state.disk_low_watermark_bytes

    if is_integer(free) and free < watermark do
      # Victims: this node's banked sessions, coldest first. We evict until the
      # projected freed bytes lift us over the watermark (we cannot re-read node
      # disk mid-sweep, so we project free += snapshot_size_bytes per eviction).
      victims = banked_victims_on_node(state, fact.configured_id)
      evict_until_watermark(state, victims, free, watermark)
    else
      state
    end
  end

  # Banked sessions whose recorded node_id is this node, coldest last_invoke_at
  # first. We LRU across the node's workloads (a per-workload banked_lru merged and
  # re-sorted), so the globally coldest banked session on the pressured node goes
  # first regardless of workload.
  defp banked_victims_on_node(state, node_id) do
    SessionStore.all(state.session_store)
    |> Enum.filter(fn s -> s.state == :banked and s.node_id == node_id end)
    |> Enum.sort_by(&(&1.last_invoke_at || 0), :asc)
  end

  defp evict_until_watermark(state, [], _free, _watermark), do: state

  defp evict_until_watermark(state, _victims, free, watermark) when free >= watermark, do: state

  defp evict_until_watermark(state, [session | rest], free, watermark) do
    state = evict_banked(state, session, :disk_pressure)
    projected_free = free + (session.snapshot_size_bytes || 0)
    evict_until_watermark(state, rest, projected_free, watermark)
  end

  # Evict a banked session: EvictSnapshot on its node, append session_evicted with
  # the reason. The session becomes terminal (evicted -> next invoke 410s).
  defp evict_banked(state, session, reason) do
    _ = evict_snapshot(state, session)

    _ =
      SessionStore.transition(
        state.session_store,
        session.session_id,
        :evict,
        :session_evicted,
        %{reason: reason},
        %{}
      )

    Logger.warning("embervm session evicted",
      session_id: session.session_id,
      workload: session.workload,
      principal: session.principal,
      reason: reason
    )

    state
  end

  # Evict a parked session: skips stop_session_process (park already tore down
  # the VM), evict_snapshot is a no-op (a parked session has no node_id/
  # snapshot_ref, see evict_snapshot/2's fallback clause), and
  # retire_session_volume releases the workspace volume it holds instead,
  # mirroring expire_session's parked arm. Appends session_evicted with the
  # reason, same as evict_banked.
  defp evict_parked(state, session, reason) do
    _ = evict_snapshot(state, session)
    retire_session_volume(state, session)

    _ =
      SessionStore.transition(
        state.session_store,
        session.session_id,
        :evict,
        :session_evicted,
        %{reason: reason},
        %{}
      )

    Logger.warning("embervm session evicted",
      session_id: session.session_id,
      workload: session.workload,
      principal: session.principal,
      reason: reason
    )

    state
  end

  # -- snapshot eviction RPC -------------------------------------------------

  defp evict_snapshot(state, %{node_id: node_id, snapshot_ref: ref, workload: workload})
       when is_binary(node_id) and is_binary(ref) do
    evict_snapshot_on_node(state, node_id, ref, node_id, workload)
  end

  defp evict_snapshot(_state, _session), do: :ok

  defp evict_snapshot_on_node(state, node_id, snapshot_ref, _reported_id, workload)
       when is_binary(node_id) and is_binary(snapshot_ref) do
    # Dial the instance holding this banked session bundle on disk (session_snapshots),
    # not the node-name alias (co-location safe, PR-B0b). Fail-open to node_id.
    dial_key = Embervm.WakeInstance.dial_for_session_bundle(state.capacity_table, node_id, snapshot_ref)

    # A lifecycle span (Task 9): reclaiming a snapshot bundle from node disk. Root
    # span (eviction is sweep/adoption-driven, no caller trace).
    Tracer.with_span "embervm.session.evict",
                     %{attributes: %{"ember.node_id" => node_id}} do
      with {:ok, channel} <- state.channel_fun.(dial_key) do
        req = %EvictSnapshotRequest{trace: %Trace{}, snapshot_ref: snapshot_ref}

        try do
          state.evict_fun.(channel, req)
        rescue
          _ -> :error
        catch
          _, _ -> :error
        end
      end

      # R6, Task 9: drop the store copy of the SESSION bundle alongside the local
      # EvictSnapshot, on every eviction trigger (banked TTL, disk pressure,
      # destroy, expiry, orphan sweep) since they all funnel through here. Sessions
      # carry no volume/generation, so no pairing guard applies (unlike the
      # stateful class's volume-generation guard). Dial the SAME owning instance.
      _ = evict_remote_bundle(state, dial_key, workload, snapshot_ref)

      :ok
    end
  end

  defp evict_snapshot_on_node(_state, _node_id, _ref, _reported, _workload), do: :ok

  # Drop the store copy of a banked SESSION bundle (EvictArtifact, remote=true, kind
  # SESSION). Best-effort: a failure never wedges the caller (the local eviction and
  # durable transition are authoritative; a stranded store copy is swept later by
  # the remote-orphan reconcile). Idempotent on the daemon; an already-absent store
  # copy is a no-op. A missing/unknown workload (should not happen; every eviction
  # site carries one) skips the remote call rather than issuing a malformed ref.
  defp evict_remote_bundle(state, node_id, workload, snapshot_ref)
       when is_binary(node_id) and is_binary(workload) and workload != "" and is_binary(snapshot_ref) and
              snapshot_ref != "" do
    artifact = %ArtifactRef{kind: :ARTIFACT_KIND_SESSION, workload: workload, ref: snapshot_ref}
    req = %EvictArtifactRequest{artifact: artifact, remote: true, trace: %Trace{workload: workload}}

    with {:ok, channel} <- safe_channel(state.channel_fun, node_id) do
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

  defp evict_remote_bundle(_state, _node_id, _workload, _snapshot_ref), do: :ok

  # -- shared fail path ------------------------------------------------------

  # Fail a session terminally with a machine-readable reason (snapshot_lost, etc.).
  # Best-effort; a store error is logged, not raised (adoption/relight callers must
  # keep going).
  defp fail_session(state, session_id, reason, detail) do
    case SessionStore.transition(
           state.session_store,
           session_id,
           :fail,
           :session_failed,
           %{reason: reason, detail: detail},
           %{}
         ) do
      {:ok, _} ->
        :ok

      {:error, err} ->
        Logger.warning("embervm session fail transition failed", session_id: session_id, error: inspect(err))
        :error
    end
  end

  defp get_session!(state, session_id) do
    {:ok, session} = SessionStore.get(state.session_store, session_id)
    session
  end

  # Best-effort workload/principal lookups for consistent structured-log keys
  # (Task 9). nil when the row is gone (a log key, never load-bearing).
  defp workload_of(state, session_id) do
    case SessionStore.get(state.session_store, session_id) do
      {:ok, %{workload: workload}} -> workload
      _ -> nil
    end
  end

  defp principal_of(state, session_id) do
    case SessionStore.get(state.session_store, session_id) do
      {:ok, %{principal: principal}} -> principal
      _ -> nil
    end
  end

  defp schedule(_msg, interval) when interval <= 0, do: :ok
  defp schedule(msg, interval), do: Process.send_after(self(), msg, interval)

  # -- destroy ---------------------------------------------------------------

  defp do_destroy(state, session_id) do
    case SessionStore.get(state.session_store, session_id) do
      {:ok, %{state: session_state}} when session_state in [:expired, :evicted, :destroyed, :failed] ->
        {{:ok, :already_terminal}, state}

      {:ok, session} ->
        destroy_live(state, session)

      :error ->
        {{:error, :not_found}, state}
    end
  end

  # Destroy a session: tear down whatever it holds. A live session (running/banking/
  # relighting) has a process + VM, stopped and destroyed here; a banked session has
  # only a snapshot, evicted here. Any parked relight waiters are drained gone (the
  # session is being destroyed). Returns {reply, state} so the drained relighting
  # ledger is threaded back.
  #
  # Two orderings, selected by EMBERVM_NODE_CONFIRMED_DESTROY (ADR embervm/014
  # decision 5):
  #   * off (default): record session_destroyed first, then tear the VM down
  #     asynchronously (today's behaviour). A banked session (no VM) is the same
  #     either way: evict its snapshot, record destroyed.
  #   * on, live VM: record the destroying intent, run the node-confirmed teardown
  #     RPC, and record session_destroyed ONLY when the node confirms teardown. An
  #     unconfirmed teardown (RPC failure or teardown_confirmed=false) leaves the
  #     session in destroying for the reconcile loop to re-drive.
  defp destroy_live(state, session) do
    if state.node_confirmed_destroy and session.state not in [:banked, :parked] do
      destroy_live_node_confirmed(state, session)
    else
      destroy_live_legacy(state, session)
    end
  end

  # Gate-off (and banked) path: record destroyed first, tear down after.
  defp destroy_live_legacy(state, session) do
    if session.state in [:banked, :parked] do
      _ = evict_snapshot(state, session)
    else
      _ = stop_session_process(state, session.session_id, session)
    end

    retire_session_volume(state, session)

    state = drain_relight_waiters(state, session.session_id, {:error, {:gone, "destroyed"}})

    reply =
      SessionStore.transition(
        state.session_store,
        session.session_id,
        :destroy,
        :session_destroyed,
        %{reason: :destroyed},
        %{}
      )

    Logger.info("embervm session destroyed",
      session_id: session.session_id,
      workload: session.workload,
      principal: session.principal
    )

    {reply, state}
  end

  # Gate-on path for a live-VM session: destroying intent -> node-confirmed teardown
  # -> destroyed only on confirmation. Relight waiters drain immediately (the session
  # is going away regardless of how long teardown takes).
  defp destroy_live_node_confirmed(state, session) do
    # 1. Durable destroying intent BEFORE the RPC, so a CP crash mid-destroy rebuilds
    #    as destroying and re-drives the teardown rather than forgetting it.
    intent =
      SessionStore.transition(
        state.session_store,
        session.session_id,
        :begin_destroy,
        :session_destroying,
        %{reason: :destroyed},
        %{}
      )

    state = drain_relight_waiters(state, session.session_id, {:error, {:gone, "destroyed"}})

    case intent do
      {:ok, _} ->
        # 2. Terminate the process and issue the node-confirmed teardown RPC. A
        #    session with no reachable VM (node_id/vm_id not both set) holds nothing
        #    on a node, so its teardown is trivially confirmed.
        confirmed =
          if is_binary(session.node_id) and is_binary(session.vm_id) do
            stop_session_process(state, session.session_id, session)
          else
            _ = stop_session_process(state, session.session_id, session)
            true
          end

        if confirmed do
          # 3a. Node confirmed teardown: reclaim the workspace before recording
          # the terminal destroyed op.
          retire_session_volume(state, session)
          reply =
            SessionStore.transition(
              state.session_store,
              session.session_id,
              :destroy,
              :session_destroyed,
              %{reason: :destroyed},
              %{}
            )

          Logger.info("embervm session destroyed",
            session_id: session.session_id,
            workload: session.workload,
            principal: session.principal
          )

          {reply, state}
        else
          # 3b. Unconfirmed: leave the session in destroying. The reconcile loop
          #     re-drives the teardown; the destroying-alarm fires if it persists.
          Logger.warning("embervm session teardown unconfirmed, left destroying",
            session_id: session.session_id,
            workload: session.workload,
            vm_id: session.vm_id
          )

          {{:ok, :destroying}, state}
        end

      {:error, _reason} = error ->
        {error, state}
    end
  end

  # Terminate the session process and destroy its VM. We destroy the VM HERE (not in
  # the process's terminate, which may not run on a hard kill) so a destroy always
  # tears down the guest even if the process is wedged. Returns whether the node
  # CONFIRMED teardown (true when the Destroy RPC returned teardown_confirmed); the
  # legacy destroy path ignores it, the node-confirmed path gates destroyed on it.
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

  # Stop only the session PROCESS (the invoke router), leaving the VM alone: the
  # bank path needs the VM alive for the node-side snapshot RPC. Idempotent; the
  # idle-bank path's self-stop may already have emptied the Registry.
  defp terminate_session_process(state, session_id) do
    case Registry.lookup(state.registry, session_id) do
      [{pid, _}] -> _ = DynamicSupervisor.terminate_child(state.supervisor, pid)
      [] -> :ok
    end

    :ok
  end

  # Issue the Destroy RPC to the owning node and report whether teardown was
  # confirmed. teardown_confirmed=true only when the RPC returns a response whose
  # teardown_confirmed field is set (an old daemon leaves it false, which reads as
  # unconfirmed). A dial failure, an RPC error, or a raised/thrown fault all read as
  # unconfirmed (false), keeping the session in destroying under the node-confirmed
  # gate; the legacy path discards this and proceeds as today.
  defp destroy_vm(state, %{node_id: node_id, vm_id: vm_id}) when is_binary(node_id) and is_binary(vm_id) do
    # Dial the OWNING instance running this live vm_id, not the node-name alias.
    dial_key = Embervm.WakeInstance.dial_for_session_vm(state.capacity_table, node_id, vm_id)

    case state.channel_fun.(dial_key) do
      {:ok, channel} ->
        opts = state.session_opts

        destroy_fun =
          Keyword.get(opts, :destroy_fun, fn ch, id ->
            Embervm.Node.V1.NodeService.Stub.destroy(ch, %Embervm.Node.V1.DestroyRequest{vm_id: id})
          end)

        try do
          case destroy_fun.(channel, vm_id) do
            {:ok, %{teardown_confirmed: true}} -> true
            _ -> false
          end
        rescue
          _ -> false
        catch
          _, _ -> false
        end

      {:error, _} ->
        false
    end
  end

  defp destroy_vm(_state, _session), do: false

  # -- helpers ---------------------------------------------------------------

  # Audit a create denial as one op-log append (D12.2 cadence). Capacity/quota
  # denials are principal-attributable and request-bounded, so they are appended
  # (unlike per-tick dispatch saturation). Reuses the metering denial reason space.
  defp audit_denial(_state, principal, workload, reason) do
    metering_reason =
      case reason do
        :quota -> :quota
        {:prime_failed, _} -> :prime_failed
        {:prime_dial_failed, _} -> :prime_dial_failed
        other when is_atom(other) -> other
        # The metering reason space is atoms; a structured reason keeps its
        # detail in the log line prime/5 already emitted.
        _ -> :denied
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

  # A persistence Prime COLD BOOTS with a freshly created workspace volume, so it
  # runs for the guest's boot-ready budget (60s) rather than a warm restore's
  # couple of seconds. The elixir-grpc default is 10s, which cancelled every
  # such Prime mid-boot: the CP saw server_closed_request and denied the create,
  # and noded logged "vsock prime did not complete ... context canceled". Same
  # deadline-family trap as #4212/#4215.
  @prime_cold_boot_timeout_ms 120_000

  defp default_prime(channel, %PrimeRequest{} = req) do
    timeout = if req.lineage_id in [nil, ""], do: 30_000, else: @prime_cold_boot_timeout_ms
    Embervm.Node.V1.NodeService.Stub.prime(channel, req, timeout: timeout)
  end

  defp default_bank(channel, %BankRequest{} = req) do
    Embervm.Node.V1.NodeService.Stub.bank(channel, req)
  end

  defp default_relight(channel, %RelightRequest{} = req) do
    Embervm.Node.V1.NodeService.Stub.relight(channel, req)
  end

  defp default_evict(channel, %EvictSnapshotRequest{} = req) do
    Embervm.Node.V1.NodeService.Stub.evict_snapshot(channel, req)
  end

  defp default_restore_artifact(channel, %RestoreArtifactRequest{} = req) do
    Embervm.Node.V1.NodeService.Stub.restore_artifact(channel, req, timeout: 900_000)
  end

  defp default_archive_volume(channel, %ArchiveVolumeRequest{} = req) do
    Embervm.Node.V1.NodeService.Stub.archive_volume(channel, req)
  end

  defp default_retire_volume(channel, %RetireVolumeRequest{} = req) do
    Embervm.Node.V1.NodeService.Stub.retire_volume(channel, req)
  end

  defp default_evict_artifact(channel, %EvictArtifactRequest{} = req) do
    Embervm.Node.V1.NodeService.Stub.evict_artifact(channel, req)
  end

  defp default_delete_session_volume(channel, %DeleteVolumeRequest{} = req) do
    Embervm.Node.V1.NodeService.Stub.delete_volume(channel, req)
  end

  defp delete_session_volume(state, %{volume_node_id: node_id, workload: workload, lineage_id: lineage_id})
       when is_binary(node_id) and is_binary(workload) and is_binary(lineage_id) do
    req = %DeleteVolumeRequest{trace: %Trace{workload: workload}, workload: workload, lineage_id: lineage_id}
    result =
      with {:ok, channel} <- safe_channel(state.channel_fun, node_id) do
        try do
          state.delete_session_volume_fun.(channel, req)
        rescue
          error -> {:error, error}
        catch
          kind, reason -> {:error, {kind, reason}}
        end
      end

    case result do
      {:ok, _} -> :ok
      other -> Logger.warning("embervm session volume delete failed", workload: workload, lineage_id: lineage_id, reason: inspect(other))
    end
    :ok
  end

  defp delete_session_volume(_state, _session), do: :ok

  defp archive_session_volume(state, %{volume_node_id: node_id, workload: workload, lineage_id: lineage_id})
       when is_binary(node_id) and is_binary(workload) and is_binary(lineage_id) do
    if persistence_enabled_workload?(session_workload_entry(state, workload)) do
      req = %ArchiveVolumeRequest{trace: %Trace{workload: workload}, workload: workload, lineage_id: lineage_id}

      result =
        with {:ok, channel} <- safe_channel(state.channel_fun, node_id) do
          try do
            state.archive_volume_fun.(channel, req)
          rescue
            error -> {:error, error}
          catch
            kind, reason -> {:error, {kind, reason}}
          end
        end

      case result do
        {:ok, %{skipped: true}} ->
          Logger.warning("embervm drain archive skipped, lineage still attached",
            workload: workload, lineage_id: lineage_id, node_id: node_id)
          {:error, :archive_skipped}
        {:ok, _} -> :ok
        other ->
          Logger.warning("embervm session workspace archive failed; keeping volume",
            workload: workload,
            lineage_id: lineage_id,
            node_id: node_id,
            reason: inspect(other)
          )

          {:error, other}
      end
    else
      :ok
    end
  end

  defp archive_session_volume(_state, _session), do: :ok

  # Retirement is node-owned: the marker and retry sweep make export failure
  # durable, while the control plane advances the session lifecycle immediately.
  defp retire_session_volume(state, %{volume_node_id: node_id, workload: workload, lineage_id: lineage_id})
       when is_binary(node_id) and is_binary(workload) and is_binary(lineage_id) do
    # NOT gated on the workload's CURRENT persistence setting: a lineage volume
    # exists on disk because it was created once, and turning the feature off
    # must not strand it. Disarming after a failed rollout did exactly that,
    # leaving 10 GiB images that filled a node (#4286). Reclamation follows the
    # artifact, not the flag.
    req = %RetireVolumeRequest{trace: %Trace{workload: workload}, workload: workload, lineage_id: lineage_id}
    retire_fun = state.retire_volume_fun
    channel_fun = state.channel_fun
    # Dial the INSTANCE owning this lineage on disk, not the bare node name:
    # the node-name alias is an anchor, not a dial key, and dialing it fails
    # :unknown_node forever (observed live when the flip armed retirement).
    dial_id = Embervm.WakeInstance.dial_for_session_volume(state.capacity_table, node_id, lineage_id)
    spawn(fn ->
      result =
        with {:ok, channel} <- safe_channel(channel_fun, dial_id) do
          try do
            retire_fun.(channel, req)
          rescue
            error -> {:error, error}
          catch
            kind, reason -> {:error, {kind, reason}}
          end
        end

      case result do
        {:ok, _} -> :ok
        other -> Logger.warning("embervm session workspace retirement failed", workload: workload, lineage_id: lineage_id, node_id: node_id, reason: inspect(other))
      end
    end)
    :ok
  end

  defp retire_session_volume(_state, _session), do: :ok

  defp default_clock, do: System.system_time(:millisecond)
end
