defmodule Embervm.OpLog.SQLite do
  @moduledoc """
  SQLite-WAL implementation of the `Embervm.OpLog` behaviour: single-writer
  GenServer owning one `Exqlite.Sqlite3` connection. Every append is one
  transaction that writes the immutable `ops` row and write-through projects
  it into the mutable `tasks`/`results` tables the dispatcher's ETS rebuild
  (Task 7) reads on boot. `compact/2` prunes the projection tables (expired
  results, terminal tasks past retention) AND prefix-compacts the `ops` journal
  itself (ADR embervm/002): ops are deleted from the front behind a durable
  `compacted_through_seq` marker (a row in the `meta` table). The marker only
  advances to a seq such that every op at or below it is older than the journal
  horizon (default 30 days) AND not owned by a live (non-terminal) task, so the
  deletion is always a true prefix (`seq <= marker`) and ops for in-flight work
  are never removed regardless of age. A future replayer (a `ra` replica, the R6
  watch) that starts below the marker learns that history is available only as
  projected state, not as ops: `read_from/2` returns `{:error, {:compacted, _}}`
  there rather than an empty log. GC never emits an op.

  WAL is chosen for local durability on the pod's PVC: readers never block
  the writer and vice versa, though in R0 we still serialize all access
  (reads included) through this GenServer for simplicity, since the
  dispatch hot path reads ETS, not the op-log, so serialized reads cost
  nothing on the critical path.
  """

  @behaviour Embervm.OpLog

  use GenServer
  require Logger

  alias Embervm.OpLog.Op
  alias Exqlite.Sqlite3

  @terminal_states ["succeeded", "failed_permanent", "dead_lettered"]
  # The complementary set: states a task can still leave, so its ops must never be
  # prefix-compacted regardless of age. Used by the marker computation below.
  @live_states ["queued", "assigned", "running", "failed_retryable"]

  # Session lifecycle states (R2), mirroring the task retention discipline. A
  # non-terminal (live) session pins its ops against prefix compaction and is
  # never pruned by the retention sweep, exactly as a live task does; a terminal
  # session prunes past retention and releases its ops for compaction.
  @session_terminal_states ["expired", "evicted", "destroyed", "failed"]
  @session_live_states ["creating", "running", "banking", "banked", "relighting"]

  # Serving instance lifecycle states (R3), mirroring the session retention
  # discipline exactly. A non-terminal (live) serving instance pins its ops
  # against prefix compaction and is never pruned by the retention sweep,
  # exactly as a live session does; a terminal instance prunes past retention
  # and releases its ops for compaction. "published"/"draining" sit inside the
  # live set (the instance is still a VM the control plane owns, whether or
  # not its endpoint is currently in the fan-out); only banked-but-not-yet-
  # relit is the exception, which mirrors "banked" being live for sessions too.
  @serving_terminal_states ["evicted", "destroyed", "failed"]
  @serving_live_states [
    "starting",
    "published",
    "draining",
    "banking",
    "banked",
    "relighting"
  ]

  # Stateful instance lifecycle states (R4), mirroring the serving retention
  # discipline exactly. A non-terminal (live) stateful instance pins its ops
  # against prefix compaction and is never pruned by the retention sweep; a
  # terminal instance prunes past retention. "cold_booting" is the fall-back boot
  # path (a relight whose pair broke), live like "starting". The `volumes` table
  # is NOT swept here at all: a volume row lives until volume_deleted, outliving
  # every instance by design (data on the volume, warmth in the snapshot), so it
  # has no terminal-state retention clause.
  @stateful_terminal_states ["evicted", "destroyed", "failed"]
  @stateful_live_states [
    "starting",
    "serving",
    "banking",
    "banked",
    "relighting",
    "cold_booting"
  ]

  # Composite-group instance lifecycle states (R5), mirroring the stateful
  # retention discipline exactly. A non-terminal (live) group instance pins its
  # ops against prefix compaction and is never pruned by the retention sweep; a
  # terminal instance prunes past retention (ADR embervm/002). "degraded" is a
  # LIVE state (a member fell unhealthy but the group is still up), like
  # "serving". "fresh_booting" is the cold-boot path (a wake that discarded
  # warmth), live like "starting". The `expired` terminal state is folded into
  # "destroyed" (it rides group_destroyed{reason: expired}), so it is not a
  # distinct state here. `group_members` rows are NOT swept independently: they
  # live and die with their group instance (pruned when it is).
  @group_terminal_states ["evicted", "destroyed", "failed"]
  @group_live_states [
    "starting",
    "running",
    "degraded",
    "banking",
    "banked",
    "relighting",
    "fresh_booting"
  ]
  # Seven days in milliseconds: default age (from last update) at which a
  # terminal task is eligible for compaction. This is the TERMINAL-TASK retention
  # window and is DISTINCT from the ops-journal horizon below.
  @default_retention_ms 7 * 24 * 60 * 60 * 1000
  # Thirty days in milliseconds: default age past which an op is eligible for
  # prefix compaction, PROVIDED its task is not still live. The ops journal is
  # the on-box book of record for recovery and recent audit; older audit is
  # delegated to SigNoz (ADR embervm/002). DISTINCT from @default_retention_ms.
  @default_journal_horizon_ms 30 * 24 * 60 * 60 * 1000
  # Rows deleted per table per compact/2 batch: bounds how long the single writer
  # holds the connection so appends queued behind it never blow the 5ms budget.
  @default_compact_batch_size 500
  # The meta key the durable ops-journal prefix marker lives at; absent reads 0.
  @marker_key "compacted_through_seq"

  @ddl [
    """
    CREATE TABLE IF NOT EXISTS ops (
      seq INTEGER PRIMARY KEY AUTOINCREMENT,
      ts INTEGER NOT NULL,
      tenant TEXT NOT NULL,
      principal TEXT,
      workload TEXT,
      task_id TEXT,
      session_id TEXT,
      serving_instance_id TEXT,
      stateful_instance_id TEXT,
      group_instance_id TEXT,
      kind TEXT NOT NULL,
      payload_json TEXT NOT NULL DEFAULT '{}'
    )
    """,
    "CREATE INDEX IF NOT EXISTS ops_task_id_idx ON ops(task_id)",
    """
    CREATE TABLE IF NOT EXISTS tasks (
      task_id TEXT PRIMARY KEY,
      tenant TEXT NOT NULL,
      principal TEXT NOT NULL,
      workload TEXT NOT NULL,
      state TEXT NOT NULL,
      attempt INTEGER NOT NULL DEFAULT 0,
      idempotency_key TEXT,
      submitted_at INTEGER NOT NULL,
      updated_at INTEGER NOT NULL,
      expires_at INTEGER
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS tasks_idem_idx
      ON tasks(workload, idempotency_key)
      WHERE idempotency_key IS NOT NULL
    """,
    """
    CREATE TABLE IF NOT EXISTS results (
      task_id TEXT PRIMARY KEY REFERENCES tasks(task_id) ON DELETE CASCADE,
      status_code INTEGER NOT NULL,
      body BLOB,
      size_bytes INTEGER NOT NULL,
      truncated INTEGER NOT NULL DEFAULT 0,
      created_at INTEGER NOT NULL,
      expires_at INTEGER,
      headers TEXT
    )
    """,
    # Metering projection (Task 12): accumulated billed usage per principal per
    # UTC epoch-day. Written by the :succeeded/:failed projection (an accumulating
    # upsert, the ONLY projection that adds rather than overwrites, see project/2),
    # read paged by GET /v1/usage and by Embervm.Metering's boot rebuild. day is
    # div(op.ts, 86_400_000) in the SAME wall-clock ms the op carries.
    # request_count (R3, D-R3.2.1) is a SEPARATE counter from task_count, charged
    # only by serving_stats (see project_usage_serving/2): serving requests are
    # never conflated with task/session invocation counts.
    """
    CREATE TABLE IF NOT EXISTS usage (
      principal TEXT NOT NULL,
      day INTEGER NOT NULL,
      tenant TEXT NOT NULL,
      vcpu_seconds REAL NOT NULL DEFAULT 0,
      gb_seconds REAL NOT NULL DEFAULT 0,
      task_count INTEGER NOT NULL DEFAULT 0,
      request_count INTEGER NOT NULL DEFAULT 0,
      updated_at INTEGER NOT NULL,
      PRIMARY KEY (principal, day)
    )
    """,
    # Session projection (R2): the durable lifecycle + lineage row per session,
    # write-through projected from the session_* ops (see project/2). The lineage
    # fields (base_snapshot_ref, base_digest, generation, snapshot_ref) are schema
    # from the first banked byte (ADR embervm/001 standing decision 4/5): a session
    # is pinned to its birth base and records its parent lineage forever, and
    # `generation` increments on every bank. token_sha256 is the sha256 of the
    # per-session capability token (the plaintext token is never stored). A
    # non-terminal session pins its ops against compaction and is never pruned by
    # retention, exactly like a live task.
    """
    CREATE TABLE IF NOT EXISTS sessions (
      session_id TEXT PRIMARY KEY,
      tenant TEXT NOT NULL,
      principal TEXT,
      workload TEXT,
      state TEXT NOT NULL,
      node_id TEXT,
      volume_node_id TEXT,
      base_snapshot_ref TEXT,
      base_digest TEXT,
      generation INTEGER NOT NULL DEFAULT 0,
      snapshot_ref TEXT,
      snapshot_size_bytes INTEGER,
      token_sha256 TEXT,
      created_at INTEGER NOT NULL,
      last_invoke_at INTEGER,
      expires_at INTEGER,
      updated_at INTEGER NOT NULL,
      terminal_reason TEXT
    )
    """,
    # Serving instance projection (R3): the durable lifecycle + endpoint row per
    # serving instance, write-through projected from the serving_* ops (see
    # project/2), mirroring the sessions table above. ip/port are the tap
    # endpoint fact the daemon reported at StartServing (R3 node contract);
    # they are cleared on bank/destroy since a relight gets a fresh allocation
    # (ADR embervm/001: the endpoint is re-reported and republished every
    # wake). base_snapshot_ref/base_digest/generation/snapshot_ref are the
    # birth-base lineage, exactly the session pattern. A non-terminal instance
    # pins its ops against compaction and is never pruned by retention,
    # exactly like a live session.
    """
    CREATE TABLE IF NOT EXISTS serving_instances (
      instance_id TEXT PRIMARY KEY,
      tenant TEXT NOT NULL,
      principal TEXT,
      workload TEXT,
      state TEXT NOT NULL,
      node_id TEXT,
      vm_id TEXT,
      ip TEXT,
      port INTEGER,
      base_snapshot_ref TEXT,
      base_digest TEXT,
      generation INTEGER NOT NULL DEFAULT 0,
      snapshot_ref TEXT,
      snapshot_size_bytes INTEGER,
      created_at INTEGER NOT NULL,
      last_active_at INTEGER,
      updated_at INTEGER NOT NULL,
      terminal_reason TEXT
    )
    """,
    # Stateful instance projection (R4): the durable lifecycle + endpoint + pairing
    # row per stateful instance, write-through projected from the stateful_* ops
    # (see project/2), mirroring serving_instances above. ip/port are the L4
    # endpoint the daemon reported at StartStateful; cleared on bank/destroy (a
    # relight re-reports and republishes). generation is the volume generation the
    # live instance holds; snapshot_generation is the generation STAMPED into the
    # banked bundle (the pair key): pair validity is snapshot_generation ==
    # volumes.generation, recomputed on every sweep. A non-terminal instance pins
    # its ops against compaction and is never pruned by retention.
    """
    CREATE TABLE IF NOT EXISTS stateful_instances (
      instance_id TEXT PRIMARY KEY,
      tenant TEXT NOT NULL,
      principal TEXT,
      workload TEXT,
      state TEXT NOT NULL,
      node_id TEXT,
      vm_id TEXT,
      ip TEXT,
      port INTEGER,
      generation INTEGER NOT NULL DEFAULT 0,
      snapshot_ref TEXT,
      snapshot_generation INTEGER,
      snapshot_size_bytes INTEGER,
      created_at INTEGER NOT NULL,
      last_active_at INTEGER,
      updated_at INTEGER NOT NULL,
      terminal_reason TEXT
    )
    """,
    # Volume projection (R4): the durable per-workload volume facts, keyed by
    # workload (one raw file per stateful workload). generation is the on-disk
    # ledger's current value (bumped on every writable attach); size_bytes is the
    # declared sparse cap; allocated_bytes is the file's actual block usage (the
    # watermark source). A volume row is created by volume_created and lives until
    # volume_deleted, OUTLIVING every instance by design (ADR embervm/001: data on
    # the volume, warmth in the snapshot). Retention never prunes a live volume row.
    """
    CREATE TABLE IF NOT EXISTS volumes (
      workload TEXT PRIMARY KEY,
      node_id TEXT,
      generation INTEGER NOT NULL DEFAULT 0,
      size_bytes INTEGER,
      allocated_bytes INTEGER,
      created_at INTEGER NOT NULL,
      updated_at INTEGER NOT NULL
    )
    """,
    # Generation-blessing ledger (R7, ADR embervm/011, standing decision 4): a
    # SEPARATE table from `volumes`, keyed by workload the same way, because a
    # workload can be blessed BEFORE it has a real volume row at all (the very
    # first wake blesses generation 1 before the daemon's FRESH boot creates the
    # volume). Folding this into `volumes` would make the very first blessing
    # fabricate a phantom volume row with no node_id, which both defeats
    # Embervm.StatefulStore.get_volume/2's nil-means-no-volume-yet contract and
    # crashes the anchor_node placement logic that reads it. blessed_generation is
    # the last generation THIS control plane's blessing ledger issued; quarantined
    # is NOT persisted here (it is a live node-report-derived fact, re-derived on
    # every NodeStatus refresh, exactly like a stateful instance's `healthy`).
    """
    CREATE TABLE IF NOT EXISTS volume_blessing (
      workload TEXT PRIMARY KEY,
      blessed_generation INTEGER NOT NULL,
      created_at INTEGER NOT NULL,
      updated_at INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS blessing_lease (
      workload TEXT NOT NULL,
      node_id TEXT NOT NULL,
      next_generation INTEGER NOT NULL,
      lease_end INTEGER NOT NULL,
      created_at INTEGER NOT NULL,
      updated_at INTEGER NOT NULL,
      PRIMARY KEY (workload, node_id)
    )
    """,
    # Checkpoint-dispatch record (R7, ADR embervm/017): one row per workload with an
    # in-flight interruptible-bank CHECKPOINT the control plane dispatched, so a
    # recovered control plane can recognize its OWN auto-aborted checkpoint (noded's
    # resolve-timeout self-bump advances the volume generation by exactly +1 on the
    # same vm_id) and auto-heal only that provably self-inflicted quarantine.
    # Written by checkpoint_dispatched, deleted by checkpoint_resolved. One row per
    # workload (the stop-serialization guard means one in-flight checkpoint at a
    # time), keyed by workload the same way volume_blessing is. An UNRESOLVED row
    # must outlive its ops against compaction (see compactor); it is tiny and
    # short-lived (cleared at the next resolve or auto-heal).
    """
    CREATE TABLE IF NOT EXISTS checkpoint_dispatch (
      workload TEXT PRIMARY KEY,
      vm_id TEXT NOT NULL,
      generation INTEGER NOT NULL,
      created_at INTEGER NOT NULL,
      updated_at INTEGER NOT NULL
    )
    """,
    # Composite-group instance projection (R5): the durable lifecycle + entry-
    # endpoint + set row per group instance, write-through projected from the
    # group_* ops (see project/2), mirroring stateful_instances above. A composite
    # group is a set of member microVMs that live/bank/relight/die as ONE unit
    # (ADR embervm/001). subnet_cidr is the group's private /29 (recorded by
    # group_net_created); entry_member/entry_port are the declared entry target,
    # and listen_port is the node-Envoy TCP listener the group is exposed on
    # cluster-internally (the group identity on the wire). set_id is the banked
    # bundle-set handle stamped by group_banked (the whole-set warmth key); it is
    # cleared on a fresh boot or set eviction that discarded warmth. entry_member/
    # entry_port/listen_port are schema from the first row (the group's identity);
    # set_id and terminal_reason fill in on the relevant transitions. A non-terminal
    # instance pins its ops against compaction and is never pruned by retention.
    """
    CREATE TABLE IF NOT EXISTS group_instances (
      instance_id TEXT PRIMARY KEY,
      tenant TEXT NOT NULL,
      principal TEXT,
      workload TEXT,
      state TEXT NOT NULL,
      node_id TEXT,
      subnet_cidr TEXT,
      entry_member TEXT,
      entry_port INTEGER,
      listen_port INTEGER,
      set_id TEXT,
      created_at INTEGER NOT NULL,
      last_active_at INTEGER,
      updated_at INTEGER NOT NULL,
      terminal_reason TEXT
    )
    """,
    # Composite-group member projection (R5): one row per (group instance, member
    # name), the per-member lifecycle/health/snapshot facts. This is the FIRST
    # multi-row-per-instance projection (sessions/serving/stateful are one row per
    # instance); the composite PK (instance_id, member_name) keys each member. vm_id/
    # ip are the member VM's facts (reported at member start, cleared on bank);
    # member_index is the expanded-replica ordinal (a member `agent` with replicas 2
    # expands to agent-0, agent-1). snapshot_ref is the member's slice of the banked
    # bundle set (stamped atomically for every member by ONE group_banked append).
    # healthy tracks the per-VM health the group_degraded/group_running edges read.
    # Member rows live and die with their group instance (retention prunes them when
    # the instance is pruned).
    """
    CREATE TABLE IF NOT EXISTS group_members (
      instance_id TEXT NOT NULL,
      member_name TEXT NOT NULL,
      member_index INTEGER,
      vm_id TEXT,
      ip TEXT,
      state TEXT,
      snapshot_ref TEXT,
      healthy INTEGER NOT NULL DEFAULT 0,
      updated_at INTEGER NOT NULL,
      PRIMARY KEY (instance_id, member_name)
    )
    """,
    # Durable single-row scalars for the op-log. Today it holds only the
    # ops-journal prefix marker (`compacted_through_seq`): the newest op seq that
    # has been prefix-compacted away, part of the durable OpLog contract so a
    # future replayer knows history below it is projected state, not ops. Absent
    # reads as 0.
    """
    CREATE TABLE IF NOT EXISTS meta (
      key TEXT PRIMARY KEY,
      value INTEGER
    )
    """
  ]

  # One UTC day in ms: the `usage` projection buckets by epoch-day so a
  # per-principal DAILY quota has a stable, replay-deterministic key.
  @day_ms 86_400_000

  # -- Client API --------------------------------------------------------

  # :name defaults to __MODULE__ for the application's supervised singleton
  # (Task 7's TaskStore and friends call it unqualified); tests that need
  # several independent instances alive at once pass name: nil explicitly to
  # get an unnamed, PID-addressed process.
  @spec start_link(keyword()) :: GenServer.on_start()
  def start_link(opts) do
    case Keyword.get(opts, :name, __MODULE__) do
      nil -> GenServer.start_link(__MODULE__, opts)
      name -> GenServer.start_link(__MODULE__, opts, name: name)
    end
  end

  @impl Embervm.OpLog
  def append(server \\ __MODULE__, %Op{} = op) do
    GenServer.call(server, {:append, op})
  end

  @impl Embervm.OpLog
  def read_from(server \\ __MODULE__, seq) do
    GenServer.call(server, {:read_from, seq})
  end

  @impl Embervm.OpLog
  def load_tasks(server \\ __MODULE__) do
    GenServer.call(server, :load_tasks)
  end

  @impl Embervm.OpLog
  def load_sessions(server \\ __MODULE__) do
    GenServer.call(server, :load_sessions)
  end

  @impl Embervm.OpLog
  def load_serving_instances(server \\ __MODULE__) do
    GenServer.call(server, :load_serving_instances)
  end

  @impl Embervm.OpLog
  def load_stateful_instances(server \\ __MODULE__) do
    GenServer.call(server, :load_stateful_instances)
  end

  @impl Embervm.OpLog
  def load_volumes(server \\ __MODULE__) do
    GenServer.call(server, :load_volumes)
  end

  @impl Embervm.OpLog
  def load_volume_blessing(server \\ __MODULE__) do
    GenServer.call(server, :load_volume_blessing)
  end

  @impl Embervm.OpLog
  def load_blessing_leases(server \\ __MODULE__) do
    GenServer.call(server, :load_blessing_leases)
  end

  @impl Embervm.OpLog
  def load_checkpoint_dispatches(server \\ __MODULE__) do
    GenServer.call(server, :load_checkpoint_dispatches)
  end

  @impl Embervm.OpLog
  def load_group_instances(server \\ __MODULE__) do
    GenServer.call(server, :load_group_instances)
  end

  @impl Embervm.OpLog
  def load_group_members(server \\ __MODULE__) do
    GenServer.call(server, :load_group_members)
  end

  @impl Embervm.OpLog
  def load_result(server \\ __MODULE__, task_id) do
    GenServer.call(server, {:load_result, task_id})
  end

  @impl Embervm.OpLog
  def load_request(server \\ __MODULE__, task_id) do
    GenServer.call(server, {:load_request, task_id})
  end

  @impl Embervm.OpLog
  def list_usage(server \\ __MODULE__, opts \\ []) do
    GenServer.call(server, {:list_usage, opts})
  end

  @impl Embervm.OpLog
  def compact(server \\ __MODULE__, now_ms) do
    GenServer.call(server, {:compact, now_ms})
  end

  @impl Embervm.OpLog
  def compacted_through(server \\ __MODULE__) do
    GenServer.call(server, :compacted_through)
  end

  @impl Embervm.OpLog
  def evict_task(server \\ __MODULE__, task_id) do
    GenServer.call(server, {:evict_task, task_id})
  end

  # The op-log's database file size in bytes, for the sweeper's disk-usage log
  # field (ADR embervm/002 rule 4). Reads the connection's own path from state so
  # the Compactor never needs to know it; the WAL/SHM sidecars are not counted
  # (the main db is what the retention policy bounds).
  @spec db_size(GenServer.server()) :: {:ok, non_neg_integer()} | {:error, term()}
  def db_size(server \\ __MODULE__) do
    GenServer.call(server, :db_size)
  end

  # -- GenServer callbacks ------------------------------------------------

  @impl true
  def init(opts) do
    path = Keyword.get(opts, :path) || default_path()
    retention_ms = Keyword.get(opts, :retention_ms, @default_retention_ms)
    journal_horizon_ms = Keyword.get(opts, :journal_horizon_ms, @default_journal_horizon_ms)
    compact_batch_size = Keyword.get(opts, :compact_batch_size, @default_compact_batch_size)

    with {:ok, conn} <- Sqlite3.open(path),
         :ok <- apply_pragmas(conn),
         :ok <- apply_ddl(conn),
         :ok <- apply_migrations(conn) do
      Logger.info("embervm op-log opened at #{path}")

      {:ok,
       %{
         conn: conn,
         path: path,
         retention_ms: retention_ms,
         journal_horizon_ms: journal_horizon_ms,
         compact_batch_size: compact_batch_size
       }}
    else
      {:error, reason} -> {:stop, {:open_failed, reason}}
    end
  end

  @impl true
  def terminate(_reason, %{conn: conn}) do
    Sqlite3.close(conn)
    :ok
  end

  @impl true
  def handle_call({:append, %Op{} = op}, _from, state) do
    case do_append(state.conn, op) do
      {:ok, seq} -> {:reply, {:ok, seq}, state}
      {:error, reason} -> {:reply, {:error, reason}, state}
    end
  end

  def handle_call({:read_from, seq}, _from, state) do
    {:reply, do_read_from(state.conn, seq), state}
  end

  def handle_call(:load_tasks, _from, state) do
    {:reply, do_load_tasks(state.conn), state}
  end

  def handle_call(:load_sessions, _from, state) do
    {:reply, do_load_sessions(state.conn), state}
  end

  def handle_call(:load_serving_instances, _from, state) do
    {:reply, do_load_serving_instances(state.conn), state}
  end

  def handle_call(:load_stateful_instances, _from, state) do
    {:reply, do_load_stateful_instances(state.conn), state}
  end

  def handle_call(:load_volumes, _from, state) do
    {:reply, do_load_volumes(state.conn), state}
  end

  def handle_call(:load_volume_blessing, _from, state) do
    {:reply, do_load_volume_blessing(state.conn), state}
  end

  def handle_call(:load_blessing_leases, _from, state) do
    {:reply, do_load_blessing_leases(state.conn), state}
  end

  def handle_call(:load_checkpoint_dispatches, _from, state) do
    {:reply, do_load_checkpoint_dispatches(state.conn), state}
  end

  def handle_call(:load_group_instances, _from, state) do
    {:reply, do_load_group_instances(state.conn), state}
  end

  def handle_call(:load_group_members, _from, state) do
    {:reply, do_load_group_members(state.conn), state}
  end

  def handle_call({:load_result, task_id}, _from, state) do
    {:reply, do_load_result(state.conn, task_id), state}
  end

  def handle_call({:load_request, task_id}, _from, state) do
    {:reply, do_load_request(state.conn, task_id), state}
  end

  def handle_call({:list_usage, opts}, _from, state) do
    {:reply, do_list_usage(state.conn, opts), state}
  end

  def handle_call({:compact, now_ms}, _from, state) do
    {:reply, do_compact(state.conn, now_ms, state), state}
  end

  def handle_call(:compacted_through, _from, state) do
    {:reply, {:ok, read_marker(state.conn)}, state}
  end

  def handle_call({:evict_task, task_id}, _from, state) do
    {:reply, do_evict_task(state.conn, task_id), state}
  end

  def handle_call(:db_size, _from, state) do
    reply =
      case File.stat(state.path) do
        {:ok, %File.Stat{size: size}} -> {:ok, size}
        {:error, reason} -> {:error, reason}
      end

    {:reply, reply, state}
  end

  # -- append + projection -------------------------------------------------

  defp do_append(conn, %Op{} = op) do
    if op.kind not in Embervm.OpLog.kinds() do
      {:error, {:unknown_kind, op.kind}}
    else
      with :ok <- Sqlite3.execute(conn, "BEGIN IMMEDIATE"),
           {:ok, seq} <- insert_op(conn, op),
           :ok <- project(conn, op, seq) do
        :ok = Sqlite3.execute(conn, "COMMIT")
        {:ok, seq}
      else
        {:error, reason} ->
          Sqlite3.execute(conn, "ROLLBACK")
          {:error, reason}
      end
    end
  end

  defp insert_op(conn, %Op{} = op) do
    sql = """
    INSERT INTO ops (ts, tenant, principal, workload, task_id, session_id, serving_instance_id, stateful_instance_id, group_instance_id, kind, payload_json)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    with {:ok, stmt} <- Sqlite3.prepare(conn, sql),
         :ok <-
           Sqlite3.bind(stmt, [
             op.ts,
             op.tenant,
             op.principal,
             op.workload,
             op.task_id,
             op.session_id,
             op.serving_instance_id,
             op.stateful_instance_id,
             op.group_instance_id,
             Atom.to_string(op.kind),
             # An ETF {:blob, _}, not JSON, so a binary body persists byte-exact
             # (see encode_payload/1).
             encode_payload(op.payload)
           ]),
         :done <- Sqlite3.step(conn, stmt),
         :ok <- Sqlite3.release(conn, stmt),
         {:ok, seq} <- Sqlite3.last_insert_rowid(conn) do
      {:ok, seq}
    end
  end

  # Write-through projection: applies the effect of one op onto the mutable
  # tasks/results tables. Kinds not listed here are audit-only (ops row
  # already written above, nothing further to project).
  defp project(conn, %Op{kind: :submitted} = op, _seq) do
    idempotency_key = Map.get(op.payload, :idempotency_key)
    expires_at = Map.get(op.payload, :expires_at)

    case existing_task_for_idempotency_key(conn, op.workload, idempotency_key) do
      {:ok, nil} ->
        sql = """
        INSERT INTO tasks
          (task_id, tenant, principal, workload, state, attempt, idempotency_key, submitted_at, updated_at, expires_at)
        VALUES (?, ?, ?, ?, 'queued', 0, ?, ?, ?, ?)
        """

        with {:ok, stmt} <- Sqlite3.prepare(conn, sql),
             :ok <-
               Sqlite3.bind(stmt, [
                 op.task_id,
                 op.tenant,
                 op.principal,
                 op.workload,
                 idempotency_key,
                 op.ts,
                 op.ts,
                 expires_at
               ]),
             :done <- Sqlite3.step(conn, stmt),
             :ok <- Sqlite3.release(conn, stmt) do
          :ok
        end

      {:ok, existing_task_id} ->
        {:error, {:duplicate_idempotency_key, existing_task_id}}
    end
  end

  # :assigned/:started advance the projection MONOTONICALLY along the task-lifecycle
  # partial order (queued < assigned < running < terminal), never as an
  # unconditional SET. Under EMBERVM_ASYNC_LIFECYCLE_WRITES (ADR embervm/014
  # decision 2) these appends are deferred through Embervm.AsyncWriter, so an append
  # can arrive out of order relative to a later synchronous op: a stale :assigned
  # landing AFTER :succeeded, or a :started landing after its own :assigned was lost.
  # advance_task_state SETs the target state only from a STRICTLY-LOWER-ranked state
  # (Embervm.TaskState.forward_rank/1), so a stale late append against an equal-or-
  # higher-ranked row is the no-op the FSM already treats it as, while a legitimate
  # forward jump (queued -> running when :assigned was lost) still applies. The
  # durable projection therefore converges to the same terminal state on BOTH the
  # live write-through path and a full oplog rebuild-replay, regardless of async
  # append order. Inert under the gate off (appends always arrive in rank order).
  defp project(conn, %Op{kind: :assigned} = op, _seq) do
    advance_task_state(conn, op.task_id, :assigned, op.ts, epoch_of(op))
  end

  defp project(conn, %Op{kind: :started} = op, _seq) do
    advance_task_state(conn, op.task_id, :running, op.ts, epoch_of(op))
  end

  defp project(conn, %Op{kind: :succeeded} = op, _seq) do
    with :ok <- update_task_state(conn, op.task_id, "succeeded", op.ts) do
      sql = """
      INSERT OR REPLACE INTO results
        (task_id, status_code, body, size_bytes, truncated, created_at, expires_at, headers)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?)
      """

      payload = op.payload

      with {:ok, stmt} <- Sqlite3.prepare(conn, sql),
           :ok <-
             Sqlite3.bind(stmt, [
               op.task_id,
               Map.fetch!(payload, :status_code),
               # {:blob, _} forces BLOB binding so a non-UTF-8 body (a PNG) is
               # stored byte-exact; a plain binary would bind as TEXT.
               blob(Map.get(payload, :body)),
               Map.fetch!(payload, :size_bytes),
               bool_to_int(Map.get(payload, :truncated, false)),
               op.ts,
               Map.get(payload, :expires_at),
               encode_headers(Map.get(payload, :headers, %{}))
             ]),
           :done <- Sqlite3.step(conn, stmt),
           :ok <- Sqlite3.release(conn, stmt) do
        project_usage(conn, op)
      end
    end
  end

  defp project(conn, %Op{kind: :failed} = op, _seq) do
    state = Map.fetch!(op.payload, :state)

    with :ok <- update_task_state(conn, op.task_id, to_string(state), op.ts) do
      # A guest 4xx/5xx returns a well-formed AssignResponse WITH usage: it did
      # real work, so it is charged. Transport/timeout failures carry no usage
      # (payload has no :usage key), so project_usage is a no-op for them.
      project_usage(conn, op)
    end
  end

  defp project(conn, %Op{kind: :retried} = op, _seq) do
    sql = "UPDATE tasks SET state='queued', attempt=attempt+1, updated_at=? WHERE task_id=?"

    with {:ok, stmt} <- Sqlite3.prepare(conn, sql),
         :ok <- Sqlite3.bind(stmt, [op.ts, op.task_id]),
         :done <- Sqlite3.step(conn, stmt),
         :ok <- Sqlite3.release(conn, stmt) do
      :ok
    end
  end

  defp project(conn, %Op{kind: :dead_lettered} = op, _seq) do
    update_task_state(conn, op.task_id, "dead_lettered", op.ts)
  end

  # Redrive re-queues a dead-lettered task and RESETS the attempt counter to 0
  # (SQL is 0-based; ETS surfaces it 1-based), so the redriven task gets a full
  # fresh retry budget rather than starting already-exhausted. The `redrive`
  # op is the audit record of the manual intervention, distinct from automatic
  # `retried` ops.
  defp project(conn, %Op{kind: :redrive} = op, _seq) do
    sql = "UPDATE tasks SET state='queued', attempt=0, updated_at=? WHERE task_id=?"

    with {:ok, stmt} <- Sqlite3.prepare(conn, sql),
         :ok <- Sqlite3.bind(stmt, [op.ts, op.task_id]),
         :done <- Sqlite3.step(conn, stmt),
         :ok <- Sqlite3.release(conn, stmt) do
      :ok
    end
  end

  # -- session projection (R2) ----------------------------------------------
  #
  # Write-through onto the `sessions` table, the same discipline as tasks: the
  # op row is already written (insert_op above), and this projects its effect.
  # session_created inserts the lineage-bearing row; bank/relight/invoke update
  # the relevant columns; the terminal kinds set a terminal state + reason.

  defp project(conn, %Op{kind: :session_created} = op, _seq) do
    payload = op.payload

    # INSERT OR IGNORE: idempotent on session_id so the adopt-and-backfill repair
    # (session_manager Direction-2, ADR embervm/014) can re-append session_created
    # for a lost async write without a primary-key clash if the original append
    # later drains. A row already present (created before, or a backfill that won
    # the race) is authoritative and left untouched.
    sql = """
    INSERT OR IGNORE INTO sessions
      (session_id, tenant, principal, workload, state, node_id, volume_node_id,
       base_snapshot_ref, base_digest, generation, snapshot_ref, snapshot_size_bytes,
       token_sha256, created_at, last_invoke_at, expires_at, updated_at, terminal_reason)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL, ?, ?, NULL, ?, ?, NULL)
    """

    with {:ok, stmt} <- Sqlite3.prepare(conn, sql),
         :ok <-
           Sqlite3.bind(stmt, [
             op.session_id,
             op.tenant,
             op.principal,
             op.workload,
             # Create yields an immediately-live session (assigned from the primed
             # pool); the transient FSM "creating" is a process concern, so the
             # durable projected state defaults to "running" unless scripted.
             Map.get(payload, :state, "running"),
             Map.get(payload, :node_id),
             Map.get(payload, :volume_node_id),
             Map.get(payload, :base_snapshot_ref),
             Map.get(payload, :base_digest),
             Map.get(payload, :token_sha256),
             op.ts,
             Map.get(payload, :expires_at),
             op.ts
           ]),
         :done <- Sqlite3.step(conn, stmt),
         :ok <- Sqlite3.release(conn, stmt) do
      :ok
    end
  end

  # session_invoked: usage + last_invoke, NO state change and NO request/response
  # bodies in the payload (at-most-once, data minimization). Charges the same
  # (principal, day) usage projection tasks do (D12.1), in this same transaction.
  defp project(conn, %Op{kind: :session_invoked} = op, _seq) do
    sql = "UPDATE sessions SET last_invoke_at=?, updated_at=? WHERE session_id=?"

    with {:ok, stmt} <- Sqlite3.prepare(conn, sql),
         :ok <- Sqlite3.bind(stmt, [op.ts, op.ts, op.session_id]),
         :done <- Sqlite3.step(conn, stmt),
         :ok <- Sqlite3.release(conn, stmt) do
      project_usage(conn, op)
    end
  end

  # session_banked: the VM is snapshotted and destroyed. Records the new snapshot
  # ref + size and bumps generation (every bank increments it, the lineage rule).
  defp project(conn, %Op{kind: :session_banked} = op, _seq) do
    payload = op.payload

    sql = """
    UPDATE sessions
    SET state='banked', snapshot_ref=?, snapshot_size_bytes=?, generation=?, updated_at=?
    WHERE session_id=?
    """

    with {:ok, stmt} <- Sqlite3.prepare(conn, sql),
         :ok <-
           Sqlite3.bind(stmt, [
             Map.get(payload, :snapshot_ref),
             Map.get(payload, :size_bytes),
             Map.get(payload, :generation, 0),
             op.ts,
             op.session_id
           ]),
         :done <- Sqlite3.step(conn, stmt),
         :ok <- Sqlite3.release(conn, stmt) do
      :ok
    end
  end

  defp project(conn, %Op{kind: :session_parked} = op, _seq) do
    sql = "UPDATE sessions SET state='parked', volume_node_id=?, node_id=NULL, updated_at=? WHERE session_id=?"

    with {:ok, stmt} <- Sqlite3.prepare(conn, sql),
         :ok <- Sqlite3.bind(stmt, [Map.get(op.payload, :volume_node_id), op.ts, op.session_id]),
         :done <- Sqlite3.step(conn, stmt),
         :ok <- Sqlite3.release(conn, stmt) do
      :ok
    end
  end

  defp project(conn, %Op{kind: :session_parking} = op, _seq) do
    sql = "UPDATE sessions SET state='parking', volume_node_id=?, updated_at=? WHERE session_id=?"

    with {:ok, stmt} <- Sqlite3.prepare(conn, sql),
         :ok <- Sqlite3.bind(stmt, [Map.get(op.payload, :volume_node_id), op.ts, op.session_id]),
         :done <- Sqlite3.step(conn, stmt),
         :ok <- Sqlite3.release(conn, stmt), do: :ok
  end

  # session_relit: restored to a live VM from its banked snapshot; back to running.
  # Guarded against terminal states so a deferred async relit append (ADR embervm/014
  # decision 2) that lands after the session was later destroyed/expired/failed
  # cannot resurrect it to running; a live (running/banked/relighting) row still
  # advances. Inert under the gate off (relit always lands from banked/relighting).
  defp project(conn, %Op{kind: :session_relit} = op, _seq) do
    sql =
      "UPDATE sessions SET state='running', updated_at=? WHERE session_id=? AND state NOT IN ('destroyed','expired','evicted','failed')"

    with {:ok, stmt} <- Sqlite3.prepare(conn, sql),
         :ok <- Sqlite3.bind(stmt, [op.ts, op.session_id]),
         :done <- Sqlite3.step(conn, stmt),
         :ok <- Sqlite3.release(conn, stmt) do
      :ok
    end
  end

  # session_destroying: the durable destroy INTENT (ADR embervm/014 decision 5).
  # A non-terminal state marker appended BEFORE the node-confirmed teardown RPC, so
  # a CP crash mid-destroy rebuilds as destroying and re-drives the destroy rather
  # than forgetting it. session_destroyed (terminal) is appended only after the node
  # confirms teardown.
  defp project(conn, %Op{kind: :session_destroying} = op, _seq) do
    sql = "UPDATE sessions SET state='destroying', updated_at=? WHERE session_id=?"

    with {:ok, stmt} <- Sqlite3.prepare(conn, sql),
         :ok <- Sqlite3.bind(stmt, [op.ts, op.session_id]),
         :done <- Sqlite3.step(conn, stmt),
         :ok <- Sqlite3.release(conn, stmt) do
      :ok
    end
  end

  defp project(conn, %Op{kind: :session_expired} = op, _seq),
    do: terminate_session(conn, op, "expired")

  defp project(conn, %Op{kind: :session_evicted} = op, _seq),
    do: terminate_session(conn, op, "evicted")

  defp project(conn, %Op{kind: :session_destroyed} = op, _seq),
    do: terminate_session(conn, op, "destroyed")

  defp project(conn, %Op{kind: :session_failed} = op, _seq),
    do: terminate_session(conn, op, "failed")

  # -- serving instance projection (R3) --------------------------------------
  #
  # Write-through onto the `serving_instances` table, mirroring the session
  # projection above exactly. serving_started inserts the lineage-bearing row;
  # published/unpublished/bank/relight update the relevant columns; the
  # terminal kinds set a terminal state + reason. serving_stats is
  # audit-plus-usage only (no state change), the serving counterpart of
  # session_invoked.

  defp project(conn, %Op{kind: :serving_started} = op, _seq) do
    payload = op.payload

    sql = """
    INSERT INTO serving_instances
      (instance_id, tenant, principal, workload, state, node_id, vm_id, ip, port,
       base_snapshot_ref, base_digest, generation, snapshot_ref, snapshot_size_bytes,
       created_at, last_active_at, updated_at, terminal_reason)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL, ?, NULL, ?, NULL)
    """

    with {:ok, stmt} <- Sqlite3.prepare(conn, sql),
         :ok <-
           Sqlite3.bind(stmt, [
             op.serving_instance_id,
             op.tenant,
             op.principal,
             op.workload,
             # A freshly started instance is not yet published (the daemon has
             # returned {vm_id, ip, port} but the control plane has not yet
             # installed it in the fan-out); "starting" until the paired
             # serving_published op lands.
             Map.get(payload, :state, "starting"),
             Map.get(payload, :node_id),
             Map.get(payload, :vm_id),
             Map.get(payload, :ip),
             Map.get(payload, :port),
             Map.get(payload, :base_snapshot_ref),
             Map.get(payload, :base_digest),
             op.ts,
             op.ts
           ]),
         :done <- Sqlite3.step(conn, stmt),
         :ok <- Sqlite3.release(conn, stmt) do
      :ok
    end
  end

  # serving_published: the endpoint is live in the fan-out. reason is one of
  # started|relit|healthy (see op-kind doc); state moves to "published".
  defp project(conn, %Op{kind: :serving_published} = op, _seq) do
    payload = op.payload

    sql = """
    UPDATE serving_instances
    SET state='published', ip=?, port=?, updated_at=?
    WHERE instance_id=?
    """

    with {:ok, stmt} <- Sqlite3.prepare(conn, sql),
         :ok <-
           Sqlite3.bind(stmt, [
             Map.get(payload, :ip),
             Map.get(payload, :port),
             op.ts,
             op.serving_instance_id
           ]),
         :done <- Sqlite3.step(conn, stmt),
         :ok <- Sqlite3.release(conn, stmt) do
      :ok
    end
  end

  # serving_unpublished: the endpoint is removed from the fan-out (reason one
  # of drain|unhealthy|banked|destroyed|failed). The VM is not necessarily
  # gone yet (a drain precedes a bank), so this only moves state to
  # "draining"; the terminal kinds below set the real terminal state.
  defp project(conn, %Op{kind: :serving_unpublished} = op, _seq) do
    sql = "UPDATE serving_instances SET state='draining', updated_at=? WHERE instance_id=?"

    with {:ok, stmt} <- Sqlite3.prepare(conn, sql),
         :ok <- Sqlite3.bind(stmt, [op.ts, op.serving_instance_id]),
         :done <- Sqlite3.step(conn, stmt),
         :ok <- Sqlite3.release(conn, stmt) do
      :ok
    end
  end

  # serving_banked: the VM is snapshotted and destroyed. Records the new
  # snapshot ref + size, bumps generation, and clears the endpoint fact (a
  # relight gets a fresh ip allocation, never the stale one).
  defp project(conn, %Op{kind: :serving_banked} = op, _seq) do
    payload = op.payload

    sql = """
    UPDATE serving_instances
    SET state='banked', snapshot_ref=?, snapshot_size_bytes=?, generation=?,
        ip=NULL, port=NULL, updated_at=?
    WHERE instance_id=?
    """

    with {:ok, stmt} <- Sqlite3.prepare(conn, sql),
         :ok <-
           Sqlite3.bind(stmt, [
             Map.get(payload, :snapshot_ref),
             Map.get(payload, :size_bytes),
             Map.get(payload, :generation, 0),
             op.ts,
             op.serving_instance_id
           ]),
         :done <- Sqlite3.step(conn, stmt),
         :ok <- Sqlite3.release(conn, stmt) do
      :ok
    end
  end

  # serving_relit: restored to a live VM from its banked snapshot; back to
  # "starting" (not yet republished, exactly the serving_started posture)
  # until the paired serving_published op lands with the fresh endpoint.
  defp project(conn, %Op{kind: :serving_relit} = op, _seq) do
    payload = op.payload

    sql = """
    UPDATE serving_instances
    SET state='starting', node_id=?, vm_id=?, updated_at=?
    WHERE instance_id=?
    """

    with {:ok, stmt} <- Sqlite3.prepare(conn, sql),
         :ok <-
           Sqlite3.bind(stmt, [
             Map.get(payload, :node_id),
             Map.get(payload, :vm_id),
             op.ts,
             op.serving_instance_id
           ]),
         :done <- Sqlite3.step(conn, stmt),
         :ok <- Sqlite3.release(conn, stmt) do
      :ok
    end
  end

  defp project(conn, %Op{kind: :serving_evicted} = op, _seq),
    do: terminate_serving(conn, op, "evicted")

  # serving_destroying: the durable destroy INTENT (ADR embervm/014 decision 5), the
  # serving counterpart of session_destroying. A non-terminal state marker (no
  # terminal_reason) so a rebuild replays it as destroying.
  defp project(conn, %Op{kind: :serving_destroying} = op, _seq) do
    sql = "UPDATE serving_instances SET state='destroying', updated_at=? WHERE instance_id=?"

    with {:ok, stmt} <- Sqlite3.prepare(conn, sql),
         :ok <- Sqlite3.bind(stmt, [op.ts, op.serving_instance_id]),
         :done <- Sqlite3.step(conn, stmt),
         :ok <- Sqlite3.release(conn, stmt) do
      :ok
    end
  end

  defp project(conn, %Op{kind: :serving_destroyed} = op, _seq),
    do: terminate_serving(conn, op, "destroyed")

  defp project(conn, %Op{kind: :serving_failed} = op, _seq),
    do: terminate_serving(conn, op, "failed")

  # serving_stats: request-count usage ONLY, no serving_instances row to touch.
  # Carries {workload, rq_delta, window_ms} from the Task 9 idle-signal scrape,
  # which is PER-CLUSTER (one Envoy cluster fans out to many instance
  # endpoints of a workload), so there is no single instance_id to update or
  # to join through for (principal, day): serving_instance_id is NULL on this
  # kind by construction. principal/tenant instead ride the op's own top-level
  # fields (the workload owner, populated by the Task 9 appender), exactly
  # like every other op kind, so project_usage stays a pure function of the op
  # and kill-and-restart rebuild equivalence holds (see D-R3.2.1 in
  # DECISIONS.md). This charges usage.request_count only: live-seconds
  # (vcpu/gb-seconds over the alive interval) are DEFERRED to the Task 9+
  # lifecycle/sweeper machinery that has the resource shape (serving_instances
  # carries no vcpu/memMib columns to accrue against here). Serving compute is
  # intentionally un-accrued at this seam, not free.
  defp project(conn, %Op{kind: :serving_stats} = op, _seq) do
    project_usage_serving(conn, op)
  end

  # -- Stateful projections (R4) --------------------------------------------
  # The durable model: ONE `stateful_instances` row per boot-lifecycle (created
  # by stateful_started or stateful_cold_booted, retired by a terminal kind),
  # over ONE `volumes` row per workload that OUTLIVES every instance (the data,
  # created by volume_created, gone only on volume_deleted). Every attach (start,
  # relight, cold boot) bumps the generation, recorded on both the instance and
  # the volume so pair validity (a banked bundle's snapshot_generation ==
  # volumes.generation) is a complete staleness test. Mirrors the serving
  # projection shape; the divergences (a persistent volumes row, snapshot_generation
  # as the pair key, cold_booted as a distinct insert) are the R4 contract.

  # volume_created: the durable volume file now exists (or its facts refreshed).
  # Upsert so a create-after-delete or a generation refresh re-establishes the row.
  defp project(conn, %Op{kind: :volume_created} = op, _seq) do
    payload = op.payload

    sql = """
    INSERT INTO volumes (workload, node_id, generation, size_bytes, allocated_bytes, created_at, updated_at)
    VALUES (?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(workload) DO UPDATE SET
      node_id = excluded.node_id,
      generation = excluded.generation,
      size_bytes = excluded.size_bytes,
      allocated_bytes = excluded.allocated_bytes,
      updated_at = excluded.updated_at
    """

    with {:ok, stmt} <- Sqlite3.prepare(conn, sql),
         :ok <-
           Sqlite3.bind(stmt, [
             op.workload,
             Map.get(payload, :node_id),
             Map.get(payload, :generation, 0),
             Map.get(payload, :size_bytes),
             Map.get(payload, :allocated_bytes),
             op.ts,
             op.ts
           ]),
         :done <- Sqlite3.step(conn, stmt),
         :ok <- Sqlite3.release(conn, stmt) do
      :ok
    end
  end

  # volume_deleted: the durable data is gone (the only destructive data path).
  defp project(conn, %Op{kind: :volume_deleted} = op, _seq) do
    sql = "DELETE FROM volumes WHERE workload=?"

    with {:ok, stmt} <- Sqlite3.prepare(conn, sql),
         :ok <- Sqlite3.bind(stmt, [op.workload]),
         :done <- Sqlite3.step(conn, stmt),
         :ok <- Sqlite3.release(conn, stmt) do
      :ok
    end
  end

  # generation_blessed (R7, ADR embervm/011): the control plane durably records
  # the generation it is about to issue for a workload's next writable attach,
  # BEFORE dispatching the boot request carrying it. Projects into the SEPARATE
  # `volume_blessing` table, never `volumes` (see that table's comment): a
  # workload's FIRST wake blesses before its FRESH boot's volume_created has
  # landed, so writing into `volumes` here would fabricate a phantom,
  # node_id-less volume row that breaks StatefulStore.get_volume/2's
  # nil-means-no-volume-yet contract. Upserted (not a plain UPDATE) so a later
  # blessing for the same workload updates rather than duplicates the row.
  defp project(conn, %Op{kind: :generation_blessed} = op, _seq) do
    payload = op.payload

    sql = """
    INSERT INTO volume_blessing (workload, blessed_generation, created_at, updated_at)
    VALUES (?, ?, ?, ?)
    ON CONFLICT(workload) DO UPDATE SET
      blessed_generation = excluded.blessed_generation,
      updated_at = excluded.updated_at
    """

    with {:ok, stmt} <- Sqlite3.prepare(conn, sql),
         :ok <-
           Sqlite3.bind(stmt, [
             op.workload,
             Map.get(payload, :generation),
             op.ts,
             op.ts
           ]),
         :done <- Sqlite3.step(conn, stmt),
         :ok <- Sqlite3.release(conn, stmt) do
      :ok
    end
  end

  defp project(conn, %Op{kind: :blessing_lease_granted} = op, _seq) do
    payload = op.payload
    sql = """
    INSERT INTO blessing_lease (workload, node_id, next_generation, lease_end, created_at, updated_at)
    VALUES (?, ?, ?, ?, ?, ?)
    ON CONFLICT(workload, node_id) DO UPDATE SET
      next_generation = excluded.next_generation,
      lease_end = excluded.lease_end,
      updated_at = excluded.updated_at
    """
    with {:ok, stmt} <- Sqlite3.prepare(conn, sql),
         :ok <- Sqlite3.bind(stmt, [op.workload, Map.get(payload, :node_id), Map.get(payload, :next_generation), Map.get(payload, :lease_end), op.ts, op.ts]),
         :done <- Sqlite3.step(conn, stmt),
         :ok <- Sqlite3.release(conn, stmt) do
      :ok
    end
  end

  # checkpoint_dispatched (R7, ADR embervm/017): upsert the one-per-workload
  # in-flight checkpoint record {vm_id, generation}. Upserted so a later checkpoint
  # for the same workload replaces the prior (a stale record can only heal a +1 on
  # its exact vm_id, so replacement is always safe).
  defp project(conn, %Op{kind: :checkpoint_dispatched} = op, _seq) do
    payload = op.payload

    sql = """
    INSERT INTO checkpoint_dispatch (workload, vm_id, generation, created_at, updated_at)
    VALUES (?, ?, ?, ?, ?)
    ON CONFLICT(workload) DO UPDATE SET
      vm_id = excluded.vm_id,
      generation = excluded.generation,
      updated_at = excluded.updated_at
    """

    with {:ok, stmt} <- Sqlite3.prepare(conn, sql),
         :ok <-
           Sqlite3.bind(stmt, [
             op.workload,
             Map.get(payload, :vm_id),
             Map.get(payload, :generation),
             op.ts,
             op.ts
           ]),
         :done <- Sqlite3.step(conn, stmt),
         :ok <- Sqlite3.release(conn, stmt) do
      :ok
    end
  end

  # checkpoint_resolved (R7, ADR embervm/017): the control plane drove this
  # workload's checkpoint resolve (or auto-heal consumed the record), so drop the
  # in-flight row. Idempotent (a DELETE of an absent row is a no-op).
  defp project(conn, %Op{kind: :checkpoint_resolved} = op, _seq) do
    sql = "DELETE FROM checkpoint_dispatch WHERE workload = ?"

    with {:ok, stmt} <- Sqlite3.prepare(conn, sql),
         :ok <- Sqlite3.bind(stmt, [op.workload]),
         :done <- Sqlite3.step(conn, stmt),
         :ok <- Sqlite3.release(conn, stmt) do
      :ok
    end
  end

  # stateful_started: a fresh instance boots (FRESH first boot, or an explicit
  # COLD boot creating a new lifecycle). Inserts the instance and bumps the
  # volume generation to the attach's post-bump value.
  defp project(conn, %Op{kind: :stateful_started} = op, _seq) do
    with :ok <- insert_stateful_instance(conn, op, "starting") do
      bump_volume_generation(conn, op)
    end
  end

  # stateful_cold_booted: a WAKE that discarded warmth and cold-booted, a NEW
  # instance replacing the retired banked one (the eviction of that bundle rides
  # a paired stateful_evicted op). reason (generation_mismatch|no_bundle|
  # ledger_unreadable|explicit) lives in the payload, so the discarded-warmth
  # event is fully reconstructable from the op alone.
  defp project(conn, %Op{kind: :stateful_cold_booted} = op, _seq) do
    with :ok <- insert_stateful_instance(conn, op, "starting") do
      bump_volume_generation(conn, op)
    end
  end

  # stateful_published: the L4 endpoint is live in the fan-out; the instance is
  # serving.
  defp project(conn, %Op{kind: :stateful_published} = op, _seq) do
    payload = op.payload
    sql = "UPDATE stateful_instances SET state='serving', ip=?, port=?, updated_at=? WHERE instance_id=?"

    with {:ok, stmt} <- Sqlite3.prepare(conn, sql),
         :ok <-
           Sqlite3.bind(stmt, [
             Map.get(payload, :ip),
             Map.get(payload, :port),
             op.ts,
             op.stateful_instance_id
           ]),
         :done <- Sqlite3.step(conn, stmt),
         :ok <- Sqlite3.release(conn, stmt) do
      :ok
    end
  end

  # stateful_unpublished: the endpoint left the fan-out (the activator is
  # installed in the same LDS/EDS update). Audit-only: the instance's VM is not
  # necessarily gone (a bank or health-eject follows and sets the real state), so
  # this only stamps updated_at and leaves the state, keeping boot rebuild's
  # "there is a live VM, republish it" verdict correct.
  defp project(conn, %Op{kind: :stateful_unpublished} = op, _seq) do
    sql = "UPDATE stateful_instances SET updated_at=? WHERE instance_id=?"

    with {:ok, stmt} <- Sqlite3.prepare(conn, sql),
         :ok <- Sqlite3.bind(stmt, [op.ts, op.stateful_instance_id]),
         :done <- Sqlite3.step(conn, stmt),
         :ok <- Sqlite3.release(conn, stmt) do
      :ok
    end
  end

  # stateful_banked: the VM is paused, snapshotted, and destroyed. Records the
  # bundle ref, the STAMPED generation (the pair key), and size; clears the
  # endpoint (a wake re-reports it).
  defp project(conn, %Op{kind: :stateful_banked} = op, _seq) do
    payload = op.payload

    sql = """
    UPDATE stateful_instances
    SET state='banked', snapshot_ref=?, snapshot_generation=?, snapshot_size_bytes=?,
        ip=NULL, port=NULL, updated_at=?
    WHERE instance_id=?
    """

    with {:ok, stmt} <- Sqlite3.prepare(conn, sql),
         :ok <-
           Sqlite3.bind(stmt, [
             Map.get(payload, :snapshot_ref),
             Map.get(payload, :generation),
             Map.get(payload, :size_bytes),
             op.ts,
             op.stateful_instance_id
           ]),
         :done <- Sqlite3.step(conn, stmt),
         :ok <- Sqlite3.release(conn, stmt) do
      :ok
    end
  end

  # stateful_relit: a WARM wake resumed the banked bundle. Back to "starting"
  # (not yet republished) with the fresh vm_id and the post-bump generation;
  # clears the bundle fields because relight bumped the generation, spending the
  # bundle (its stamped generation is now stale by construction, decision 2).
  defp project(conn, %Op{kind: :stateful_relit} = op, _seq) do
    payload = op.payload

    sql = """
    UPDATE stateful_instances
    SET state='starting', node_id=?, vm_id=?, generation=?,
        snapshot_ref=NULL, snapshot_generation=NULL, updated_at=?
    WHERE instance_id=?
    """

    with {:ok, stmt} <- Sqlite3.prepare(conn, sql),
         :ok <-
           Sqlite3.bind(stmt, [
             Map.get(payload, :node_id),
             Map.get(payload, :vm_id),
             Map.get(payload, :generation, 0),
             op.ts,
             op.stateful_instance_id
           ]),
         :done <- Sqlite3.step(conn, stmt),
         :ok <- Sqlite3.release(conn, stmt) do
      bump_volume_generation(conn, op)
    end
  end

  defp project(conn, %Op{kind: :stateful_evicted} = op, _seq),
    do: terminate_stateful(conn, op, "evicted")

  # stateful_destroying: the durable destroy INTENT (ADR embervm/014 decision 5), the
  # stateful counterpart of session_destroying.
  defp project(conn, %Op{kind: :stateful_destroying} = op, _seq) do
    sql = "UPDATE stateful_instances SET state='destroying', updated_at=? WHERE instance_id=?"

    with {:ok, stmt} <- Sqlite3.prepare(conn, sql),
         :ok <- Sqlite3.bind(stmt, [op.ts, op.stateful_instance_id]),
         :done <- Sqlite3.step(conn, stmt),
         :ok <- Sqlite3.release(conn, stmt) do
      :ok
    end
  end

  defp project(conn, %Op{kind: :stateful_destroyed} = op, _seq),
    do: terminate_stateful(conn, op, "destroyed")

  defp project(conn, %Op{kind: :stateful_failed} = op, _seq),
    do: terminate_stateful(conn, op, "failed")

  # stateful_stats: connection-count usage ONLY, no stateful_instances row to
  # touch. Carries {workload, cx_delta, window_ms} from the Task 9 idle-signal
  # TCP scrape (opaque L4 counts connections, not requests), charged into the
  # same usage.request_count column serving requests use (the L4 unit of work),
  # so stateful_instance_id is NULL on this kind by construction and the
  # (principal, day) accrual stays a pure function of the op.
  defp project(conn, %Op{kind: :stateful_stats} = op, _seq) do
    project_usage_stateful(conn, op)
  end

  # -- composite-group projection (R5) --------------------------------------
  #
  # The durable model: ONE `group_instances` row per group boot-lifecycle (created
  # by group_created, retired by a terminal kind) plus N `group_members` rows (one
  # per expanded member). A composite group lives/banks/relights/dies as ONE unit
  # (ADR embervm/001), so group_banked stamps the ENTIRE member set atomically in a
  # single append (decision 3), and a fresh boot / set eviction that discards warmth
  # records its reason so every discarded-warmth event reconstructs from the log
  # alone. Mirrors the stateful projection block above; the one structural novelty
  # is the multi-row `group_members` write on member/bank transitions.

  # group_created: a fresh group instance row (the whole-group boot). entry_member/
  # entry_port/listen_port are the group's identity (from the validated CR); state
  # is "starting" until every member is up and the group_running edge lands.
  defp project(conn, %Op{kind: :group_created} = op, _seq) do
    payload = op.payload

    sql = """
    INSERT INTO group_instances
      (instance_id, tenant, principal, workload, state, node_id, subnet_cidr,
       entry_member, entry_port, listen_port, set_id,
       created_at, last_active_at, updated_at, terminal_reason)
    VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, NULL, ?, NULL, ?, NULL)
    """

    with {:ok, stmt} <- Sqlite3.prepare(conn, sql),
         :ok <-
           Sqlite3.bind(stmt, [
             op.group_instance_id,
             op.tenant,
             op.principal,
             op.workload,
             Map.get(payload, :state, "starting"),
             Map.get(payload, :node_id),
             Map.get(payload, :entry_member),
             Map.get(payload, :entry_port),
             Map.get(payload, :listen_port),
             op.ts,
             op.ts
           ]),
         :done <- Sqlite3.step(conn, stmt),
         :ok <- Sqlite3.release(conn, stmt) do
      :ok
    end
  end

  # group_net_created: the group's private /29 subnet is up. Records subnet_cidr
  # (the group's own address space its members share); audit-plus-fact, no state
  # change.
  defp project(conn, %Op{kind: :group_net_created} = op, _seq) do
    sql = "UPDATE group_instances SET subnet_cidr=?, updated_at=? WHERE instance_id=?"

    with {:ok, stmt} <- Sqlite3.prepare(conn, sql),
         :ok <-
           Sqlite3.bind(stmt, [
             Map.get(op.payload, :subnet_cidr),
             op.ts,
             op.group_instance_id
           ]),
         :done <- Sqlite3.step(conn, stmt),
         :ok <- Sqlite3.release(conn, stmt) do
      :ok
    end
  end

  # group_net_deleted: the group's subnet is torn down (the group is going away).
  # Audit-only for the subnet fact; the terminal kind clears the instance state.
  # Clears subnet_cidr so a rebuild never shows a live subnet on a torn-down group.
  defp project(conn, %Op{kind: :group_net_deleted} = op, _seq) do
    sql = "UPDATE group_instances SET subnet_cidr=NULL, updated_at=? WHERE instance_id=?"

    with {:ok, stmt} <- Sqlite3.prepare(conn, sql),
         :ok <- Sqlite3.bind(stmt, [op.ts, op.group_instance_id]),
         :done <- Sqlite3.step(conn, stmt),
         :ok <- Sqlite3.release(conn, stmt) do
      :ok
    end
  end

  # group_member_started: one member VM came up. Upserts its `group_members` row
  # (member_name, member_index, vm_id, ip), state "starting", not yet healthy.
  # UPSERT so a relight/fresh-boot re-start of the same member overwrites its prior
  # row (a member row lives with the group, its facts refresh on each boot).
  defp project(conn, %Op{kind: :group_member_started} = op, _seq) do
    upsert_group_member(conn, op, "starting", healthy: false, clear_snapshot: true)
  end

  # group_running: the whole-group readiness edge (every member health-gated). The
  # group instance moves to "running"; every member row flips healthy. last_active_at
  # advances (the group is live and serving).
  defp project(conn, %Op{kind: :group_running} = op, _seq) do
    with :ok <- set_group_state(conn, op, "running", last_active: true) do
      mark_all_members_healthy(conn, op.group_instance_id, true, op.ts)
    end
  end

  # group_published: the entry endpoint is live in the fan-out (who traffic reaches).
  # Records the listen_port the entry is exposed on; keeps the group "running"
  # (published is the entry-lifetime audit, not a lifecycle change).
  defp project(conn, %Op{kind: :group_published} = op, _seq) do
    sql = "UPDATE group_instances SET listen_port=COALESCE(?, listen_port), updated_at=? WHERE instance_id=?"

    with {:ok, stmt} <- Sqlite3.prepare(conn, sql),
         :ok <-
           Sqlite3.bind(stmt, [
             Map.get(op.payload, :listen_port),
             op.ts,
             op.group_instance_id
           ]),
         :done <- Sqlite3.step(conn, stmt),
         :ok <- Sqlite3.release(conn, stmt) do
      :ok
    end
  end

  # group_unpublished: the entry endpoint left the fan-out (a drain precedes a
  # bank). Audit-only for the endpoint; the VM set is not necessarily gone, so this
  # does not move the instance to a terminal state (the terminal kinds below do).
  defp project(conn, %Op{kind: :group_unpublished} = op, _seq) do
    set_group_state(conn, op, "banking")
  end

  # group_banked: the whole set is snapshotted and destroyed, recorded ATOMICALLY
  # in one append (decision 3). Stamps set_id on the instance and each member's
  # snapshot_ref from payload.members (the bundle-set audit: every discarded-warmth
  # event reconstructs from this one row + its member rows). Clears each member's
  # vm_id/ip (the VMs are gone) and moves the instance to "banked".
  defp project(conn, %Op{kind: :group_banked} = op, _seq) do
    payload = op.payload

    sql = """
    UPDATE group_instances
    SET state='banked', set_id=?, updated_at=?
    WHERE instance_id=?
    """

    with {:ok, stmt} <- Sqlite3.prepare(conn, sql),
         :ok <-
           Sqlite3.bind(stmt, [
             Map.get(payload, :set_id),
             op.ts,
             op.group_instance_id
           ]),
         :done <- Sqlite3.step(conn, stmt),
         :ok <- Sqlite3.release(conn, stmt),
         :ok <- bank_group_members(conn, op) do
      :ok
    end
  end

  # group_relit: a WARM wake resumed the banked set. Back to "starting" (not yet
  # re-running) until the paired group_running edge lands with the fresh member
  # endpoints; the members re-report via group_member_started, so this only moves
  # the instance state and advances last_active_at.
  defp project(conn, %Op{kind: :group_relit} = op, _seq) do
    set_group_state(conn, op, "starting", last_active: true)
  end

  # group_fresh_booted: a wake that DISCARDED warmth and cold-booted the whole set
  # (a NEW cold group boot). reason (no_set|partial_set|set_unreadable|
  # clock_resync_failed|explicit) rides the payload so the discarded-warmth event is
  # reconstructable from the log alone. Clears set_id (the warmth is spent) and
  # returns to "starting"; members re-report fresh.
  defp project(conn, %Op{kind: :group_fresh_booted} = op, _seq) do
    sql = """
    UPDATE group_instances
    SET state='starting', set_id=NULL, updated_at=?
    WHERE instance_id=?
    """

    with {:ok, stmt} <- Sqlite3.prepare(conn, sql),
         :ok <- Sqlite3.bind(stmt, [op.ts, op.group_instance_id]),
         :done <- Sqlite3.step(conn, stmt),
         :ok <- Sqlite3.release(conn, stmt) do
      :ok
    end
  end

  # group_set_evicted: the banked set is discarded (its warmth is stale/unreadable),
  # the partner event to a later fresh boot. Records the reason and clears set_id so
  # the next wake cold-boots. The instance stays live (banked -> starting is the
  # relight/fresh path); this is the bundle-set audit, not a terminal transition.
  defp project(conn, %Op{kind: :group_set_evicted} = op, _seq) do
    sql = "UPDATE group_instances SET set_id=NULL, updated_at=? WHERE instance_id=?"

    with {:ok, stmt} <- Sqlite3.prepare(conn, sql),
         :ok <- Sqlite3.bind(stmt, [op.ts, op.group_instance_id]),
         :done <- Sqlite3.step(conn, stmt),
         :ok <- Sqlite3.release(conn, stmt) do
      :ok
    end
  end

  # group_degraded: a member fell unhealthy while the group stays up (crash-
  # consistency is per-VM, never across members). Moves the instance to "degraded"
  # (a LIVE state) and flips the named member's healthy flag off.
  defp project(conn, %Op{kind: :group_degraded} = op, _seq) do
    with :ok <- set_group_state(conn, op, "degraded") do
      case Map.get(op.payload, :member_name) do
        nil -> :ok
        member -> set_member_health(conn, op.group_instance_id, member, false, op.ts)
      end
    end
  end

  # group_destroying: the durable destroy INTENT (ADR embervm/014 decision 5), the
  # group counterpart of session_destroying.
  defp project(conn, %Op{kind: :group_destroying} = op, _seq) do
    sql = "UPDATE group_instances SET state='destroying', updated_at=? WHERE instance_id=?"

    with {:ok, stmt} <- Sqlite3.prepare(conn, sql),
         :ok <- Sqlite3.bind(stmt, [op.ts, op.group_instance_id]),
         :done <- Sqlite3.step(conn, stmt),
         :ok <- Sqlite3.release(conn, stmt) do
      :ok
    end
  end

  defp project(conn, %Op{kind: :group_destroyed} = op, _seq),
    do: terminate_group(conn, op, "destroyed")

  defp project(conn, %Op{kind: :group_failed} = op, _seq),
    do: terminate_group(conn, op, "failed")

  # group_stats: per-member usage ONLY, no group_instances row to touch. A composite
  # group bills every member's live-seconds (a 3-member group bills 3 VMs' worth),
  # so group_instance_id is NULL on this kind by construction (workload-scoped, like
  # stateful_stats) and the (principal, day) accrual stays a pure function of the op.
  defp project(conn, %Op{kind: :group_stats} = op, _seq) do
    project_usage_group(conn, op)
  end

  # Audit-only kinds: no task/result projection.
  defp project(_conn, %Op{kind: kind}, _seq)
       when kind in [
              :denied,
              :base_built,
              :primed,
              :vm_destroyed,
              :quota_enforced,
              :drain,
              :node_drain_started,
              :node_drain_finished,
              :artifact_exported,
              :artifact_restored,
              :artifact_evicted_remote
            ] do
    :ok
  end

  # A terminal session transition: set the terminal state + a machine-readable
  # reason (defaulting to the state itself when the payload omits one).
  defp terminate_session(conn, %Op{} = op, state) do
    reason = to_string(Map.get(op.payload, :reason, state))
    sql = "UPDATE sessions SET state=?, terminal_reason=?, updated_at=? WHERE session_id=?"

    with {:ok, stmt} <- Sqlite3.prepare(conn, sql),
         :ok <- Sqlite3.bind(stmt, [state, reason, op.ts, op.session_id]),
         :done <- Sqlite3.step(conn, stmt),
         :ok <- Sqlite3.release(conn, stmt) do
      :ok
    end
  end

  # A terminal serving-instance transition: set the terminal state + a
  # machine-readable reason (defaulting to the state itself when the payload
  # omits one), mirroring terminate_session/3 exactly.
  defp terminate_serving(conn, %Op{} = op, state) do
    reason = to_string(Map.get(op.payload, :reason, state))
    sql = "UPDATE serving_instances SET state=?, terminal_reason=?, updated_at=? WHERE instance_id=?"

    with {:ok, stmt} <- Sqlite3.prepare(conn, sql),
         :ok <- Sqlite3.bind(stmt, [state, reason, op.ts, op.serving_instance_id]),
         :done <- Sqlite3.step(conn, stmt),
         :ok <- Sqlite3.release(conn, stmt) do
      :ok
    end
  end

  # Insert a fresh stateful_instances row (stateful_started / stateful_cold_booted).
  # generation is the volume generation this attach booted with (the pair key
  # baseline); the bundle fields are NULL until a later stateful_banked stamps them.
  defp insert_stateful_instance(conn, %Op{} = op, state) do
    payload = op.payload

    sql = """
    INSERT INTO stateful_instances
      (instance_id, tenant, principal, workload, state, node_id, vm_id, ip, port,
       generation, snapshot_ref, snapshot_generation, snapshot_size_bytes,
       created_at, last_active_at, updated_at, terminal_reason)
    VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, NULL, NULL, NULL, ?, NULL, ?, NULL)
    """

    with {:ok, stmt} <- Sqlite3.prepare(conn, sql),
         :ok <-
           Sqlite3.bind(stmt, [
             op.stateful_instance_id,
             op.tenant,
             op.principal,
             op.workload,
             Map.get(payload, :state, state),
             Map.get(payload, :node_id),
             Map.get(payload, :vm_id),
             Map.get(payload, :generation, 0),
             op.ts,
             op.ts
           ]),
         :done <- Sqlite3.step(conn, stmt),
         :ok <- Sqlite3.release(conn, stmt) do
      :ok
    end
  end

  # Bump the volume row's generation to the attach's post-bump value (every
  # start/relight/cold-boot bumps the on-disk ledger, decision 2). No-op when the
  # payload carries no generation. Keeps volumes.generation the authoritative
  # "current" side of the pair check (bundle.snapshot_generation == this).
  defp bump_volume_generation(conn, %Op{} = op) do
    case Map.get(op.payload, :generation) do
      nil ->
        :ok

      generation ->
        sql = "UPDATE volumes SET generation=?, updated_at=? WHERE workload=?"

        with {:ok, stmt} <- Sqlite3.prepare(conn, sql),
             :ok <- Sqlite3.bind(stmt, [generation, op.ts, op.workload]),
             :done <- Sqlite3.step(conn, stmt),
             :ok <- Sqlite3.release(conn, stmt) do
          :ok
        end
    end
  end

  # A terminal stateful-instance transition, mirroring terminate_serving/3.
  defp terminate_stateful(conn, %Op{} = op, state) do
    reason = to_string(Map.get(op.payload, :reason, state))
    sql = "UPDATE stateful_instances SET state=?, terminal_reason=?, updated_at=? WHERE instance_id=?"

    with {:ok, stmt} <- Sqlite3.prepare(conn, sql),
         :ok <- Sqlite3.bind(stmt, [state, reason, op.ts, op.stateful_instance_id]),
         :done <- Sqlite3.step(conn, stmt),
         :ok <- Sqlite3.release(conn, stmt) do
      :ok
    end
  end

  # -- composite-group projection helpers (R5) ------------------------------

  # Move a group instance to `state`, optionally advancing last_active_at (for the
  # live-serving edges: group_running/group_relit). A no-op-shaped UPDATE keyed by
  # instance_id, mirroring the serving/stateful state setters.
  defp set_group_state(conn, %Op{} = op, state, opts \\ []) do
    sql =
      if Keyword.get(opts, :last_active, false) do
        "UPDATE group_instances SET state=?, last_active_at=?, updated_at=? WHERE instance_id=?"
      else
        "UPDATE group_instances SET state=?, updated_at=? WHERE instance_id=?"
      end

    params =
      if Keyword.get(opts, :last_active, false) do
        [state, op.ts, op.ts, op.group_instance_id]
      else
        [state, op.ts, op.group_instance_id]
      end

    with {:ok, stmt} <- Sqlite3.prepare(conn, sql),
         :ok <- Sqlite3.bind(stmt, params),
         :done <- Sqlite3.step(conn, stmt),
         :ok <- Sqlite3.release(conn, stmt) do
      :ok
    end
  end

  # A terminal group-instance transition, mirroring terminate_stateful/3. The
  # `expired` terminal state has no dedicated kind: it arrives as group_destroyed
  # {reason: expired}, so the reason (defaulting to the state) carries it through.
  defp terminate_group(conn, %Op{} = op, state) do
    reason = to_string(Map.get(op.payload, :reason, state))
    sql = "UPDATE group_instances SET state=?, terminal_reason=?, updated_at=? WHERE instance_id=?"

    with {:ok, stmt} <- Sqlite3.prepare(conn, sql),
         :ok <- Sqlite3.bind(stmt, [state, reason, op.ts, op.group_instance_id]),
         :done <- Sqlite3.step(conn, stmt),
         :ok <- Sqlite3.release(conn, stmt) do
      :ok
    end
  end

  # UPSERT one `group_members` row from a group_member_started op. The composite PK
  # (instance_id, member_name) means a re-start of the same member (relight/fresh
  # boot re-reports it) overwrites its prior facts. clear_snapshot resets the banked
  # slice on a fresh member boot (the warmth is spent). healthy starts false; the
  # group_running edge flips it.
  defp upsert_group_member(conn, %Op{} = op, state, opts) do
    payload = op.payload
    healthy = if Keyword.get(opts, :healthy, false), do: 1, else: 0
    clear_snapshot = Keyword.get(opts, :clear_snapshot, false)

    snapshot_set = if clear_snapshot, do: "NULL", else: "snapshot_ref"

    sql = """
    INSERT INTO group_members
      (instance_id, member_name, member_index, vm_id, ip, state, snapshot_ref, healthy, updated_at)
    VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)
    ON CONFLICT(instance_id, member_name) DO UPDATE SET
      member_index = excluded.member_index,
      vm_id = excluded.vm_id,
      ip = excluded.ip,
      state = excluded.state,
      snapshot_ref = #{snapshot_set},
      healthy = excluded.healthy,
      updated_at = excluded.updated_at
    """

    with {:ok, stmt} <- Sqlite3.prepare(conn, sql),
         :ok <-
           Sqlite3.bind(stmt, [
             op.group_instance_id,
             Map.get(payload, :member_name),
             Map.get(payload, :member_index),
             Map.get(payload, :vm_id),
             Map.get(payload, :ip),
             state,
             healthy,
             op.ts
           ]),
         :done <- Sqlite3.step(conn, stmt),
         :ok <- Sqlite3.release(conn, stmt) do
      :ok
    end
  end

  # Flip every member row of a group to (un)healthy in one statement (the group_running
  # readiness edge marks the whole set healthy).
  defp mark_all_members_healthy(conn, instance_id, healthy, ts) do
    flag = if healthy, do: 1, else: 0
    sql = "UPDATE group_members SET healthy=?, updated_at=? WHERE instance_id=?"

    with {:ok, stmt} <- Sqlite3.prepare(conn, sql),
         :ok <- Sqlite3.bind(stmt, [flag, ts, instance_id]),
         :done <- Sqlite3.step(conn, stmt),
         :ok <- Sqlite3.release(conn, stmt) do
      :ok
    end
  end

  # Set one named member's health flag (group_degraded flips it off).
  defp set_member_health(conn, instance_id, member_name, healthy, ts) do
    flag = if healthy, do: 1, else: 0
    sql = "UPDATE group_members SET healthy=?, updated_at=? WHERE instance_id=? AND member_name=?"

    with {:ok, stmt} <- Sqlite3.prepare(conn, sql),
         :ok <- Sqlite3.bind(stmt, [flag, ts, instance_id, member_name]),
         :done <- Sqlite3.step(conn, stmt),
         :ok <- Sqlite3.release(conn, stmt) do
      :ok
    end
  end

  # Stamp the banked bundle-set slice onto every member row ATOMICALLY (decision 3:
  # the whole set is banked in ONE append). payload.members is a list of
  # %{name, snapshot_ref} (atom or string keys, since the projection runs on the
  # freshly-appended atom-keyed op); each member's snapshot_ref is recorded and its
  # live VM facts (vm_id/ip) cleared (the VMs are gone). A member with no matching
  # payload entry is left untouched. No-op when the payload carries no member list.
  defp bank_group_members(conn, %Op{} = op) do
    case Map.get(op.payload, :members) do
      members when is_list(members) ->
        Enum.reduce_while(members, :ok, fn member, :ok ->
          name = member_field(member, :name)
          snapshot_ref = member_field(member, :snapshot_ref)

          sql = """
          UPDATE group_members
          SET snapshot_ref=?, vm_id=NULL, ip=NULL, state='banked', updated_at=?
          WHERE instance_id=? AND member_name=?
          """

          result =
            with {:ok, stmt} <- Sqlite3.prepare(conn, sql),
                 :ok <- Sqlite3.bind(stmt, [snapshot_ref, op.ts, op.group_instance_id, name]),
                 :done <- Sqlite3.step(conn, stmt),
                 :ok <- Sqlite3.release(conn, stmt) do
              :ok
            end

          case result do
            :ok -> {:cont, :ok}
            {:error, _} = err -> {:halt, err}
          end
        end)

      _ ->
        :ok
    end
  end

  # A member payload entry may carry atom keys (the projection sees the freshly
  # appended atom-keyed op) or string keys (a value rebuilt from the durable
  # payload_json), so read either.
  defp member_field(member, key) when is_map(member) do
    Map.get(member, key) || Map.get(member, Atom.to_string(key))
  end

  defp member_field(_member, _key), do: nil

  defp update_task_state(conn, task_id, state, ts) do
    sql = "UPDATE tasks SET state=?, updated_at=? WHERE task_id=?"

    with {:ok, stmt} <- Sqlite3.prepare(conn, sql),
         :ok <- Sqlite3.bind(stmt, [state, ts, task_id]),
         :done <- Sqlite3.step(conn, stmt),
         :ok <- Sqlite3.release(conn, stmt) do
      :ok
    end
  end

  # Monotonic advance for a deferred async lifecycle append (see the :assigned/
  # :started projections): SET `to_state` (an atom) only when the row's current
  # state ranks STRICTLY BELOW it in the task-lifecycle partial order
  # (Embervm.TaskState.states_below/1). A late append against a row that already
  # ran/terminalized (equal or higher rank) matches no row and is a harmless no-op,
  # so an out-of-order async append can never regress the durable state; a
  # legitimate forward jump (queued -> running when :assigned was lost) still
  # applies. Same guard, same outcome on the live path and on rebuild-replay.
  #
  # `epoch` is the SECOND, ORTHOGONAL half of the guard: the state-rank test alone
  # cannot catch an ATTEMPT-ALIASED stale append, because queued -> assigned is
  # FORWARD in the rank order regardless of which dispatch attempt issued it. A task
  # that was assigned under attempt N (deferred :assigned enqueued), then failed and
  # `:retried` (back to queued, attempt N+1) awaiting a fresh dispatch, would have
  # that stale attempt-N :assigned land against the now-queued row and re-assign it
  # to a worker that no longer exists. So the deferred op carries the 1-based attempt
  # it was issued under, and we additionally require the row's retry count (the
  # 0-based `attempt` column) to still be BELOW that epoch: `attempt < epoch`. A
  # stale attempt-N append (epoch N) against a row already retried to attempt N (SQL
  # attempt column N, i.e. not < N) matches no row and is dropped; the current
  # attempt's append (epoch N+1 against SQL attempt N) still applies. `epoch` is nil
  # for the write-through (gate-off) path and every pre-epoch op, in which case only
  # the rank guard runs and behaviour is exactly as before.
  defp advance_task_state(conn, task_id, to_state, ts, epoch \\ nil) do
    below = Embervm.TaskState.states_below(to_state)
    placeholders = Enum.map_join(below, ",", fn _ -> "?" end)

    {epoch_clause, epoch_args} =
      case epoch do
        e when is_integer(e) -> {" AND attempt < ?", [e]}
        _ -> {"", []}
      end

    sql =
      "UPDATE tasks SET state=?, updated_at=? WHERE task_id=? AND state IN (#{placeholders})" <>
        epoch_clause

    with {:ok, stmt} <- Sqlite3.prepare(conn, sql),
         :ok <- Sqlite3.bind(stmt, [Atom.to_string(to_state), ts, task_id | below] ++ epoch_args),
         :done <- Sqlite3.step(conn, stmt),
         :ok <- Sqlite3.release(conn, stmt) do
      :ok
    end
  end

  # The dispatch attempt (1-based) a deferred async :assigned/:started op was issued
  # under, read from the op payload (atom key on a fresh append, string key if ever
  # re-projected from durable JSON). nil when absent (sync write-through path, or an
  # op appended before the epoch tag existed): the caller then applies only the
  # state-rank guard, unchanged.
  defp epoch_of(%Op{payload: payload}) when is_map(payload) do
    Map.get(payload, :epoch) || Map.get(payload, "epoch")
  end

  defp epoch_of(_op), do: nil

  # Accumulating usage upsert, run INSIDE the same transaction as the
  # :succeeded/:failed op (see project/2), so a task's usage commits with its
  # terminal transition or not at all: no separate flush, no unflushed window.
  # A no-op when the op carried no usage (transport/timeout failures, or any
  # older op predating metering). This is the one projection that ACCUMULATES
  # rather than overwrites; that makes it non-idempotent under op replay (the
  # future read_from/2 replica path), which is safe today because R0 projects
  # each op exactly once at append. Bucketed by UTC epoch-day from op.ts.
  defp project_usage(conn, %Op{payload: payload} = op) do
    case Map.get(payload, :usage) do
      usage when is_map(usage) ->
        sql = """
        INSERT INTO usage (principal, day, tenant, vcpu_seconds, gb_seconds, task_count, updated_at)
        VALUES (?, ?, ?, ?, ?, 1, ?)
        ON CONFLICT(principal, day) DO UPDATE SET
          vcpu_seconds = vcpu_seconds + excluded.vcpu_seconds,
          gb_seconds = gb_seconds + excluded.gb_seconds,
          task_count = task_count + 1,
          updated_at = excluded.updated_at
        """

        with {:ok, stmt} <- Sqlite3.prepare(conn, sql),
             :ok <-
               Sqlite3.bind(stmt, [
                 op.principal,
                 div(op.ts, @day_ms),
                 op.tenant,
                 to_float(Map.get(usage, :vcpu_seconds, 0)),
                 to_float(Map.get(usage, :gb_seconds, 0)),
                 op.ts
               ]),
             :done <- Sqlite3.step(conn, stmt),
             :ok <- Sqlite3.release(conn, stmt) do
          :ok
        end

      _ ->
        :ok
    end
  end

  # Accumulating request-count usage upsert for serving_stats (R3, D-R3.2.1),
  # run INSIDE the same transaction as the serving_stats op. DISTINCT from
  # project_usage/2 above: it charges the NEW usage.request_count column, NOT
  # task_count, so serving requests are never conflated with task/session
  # invocation counts (an irreversible merge once mixed). vcpu_seconds/
  # gb_seconds are left untouched (serving live-seconds accrual is deferred to
  # the Task 9+ lifecycle machinery, see the serving_stats project/2 comment).
  # principal/tenant come from the op's own top-level fields (populated by the
  # Task 9 appender from the workload owner), never a join: serving_stats is
  # per-cluster (per-workload), not per-instance, so there is no single
  # serving_instances row to join through. A no-op when principal is nil (an
  # op predating the appender wiring, or a malformed scrape), mirroring
  # project_usage/2's no-op-on-absent-usage guard.
  defp project_usage_serving(_conn, %Op{principal: nil}), do: :ok

  defp project_usage_serving(conn, %Op{payload: payload} = op) do
    rq_delta = Map.get(payload, :rq_delta, 0)

    sql = """
    INSERT INTO usage (principal, day, tenant, vcpu_seconds, gb_seconds, task_count, request_count, updated_at)
    VALUES (?, ?, ?, 0, 0, 0, ?, ?)
    ON CONFLICT(principal, day) DO UPDATE SET
      request_count = request_count + excluded.request_count,
      updated_at = excluded.updated_at
    """

    with {:ok, stmt} <- Sqlite3.prepare(conn, sql),
         :ok <-
           Sqlite3.bind(stmt, [
             op.principal,
             div(op.ts, @day_ms),
             op.tenant,
             rq_delta,
             op.ts
           ]),
         :done <- Sqlite3.step(conn, stmt),
         :ok <- Sqlite3.release(conn, stmt) do
      :ok
    end
  end

  # stateful_stats usage: charge the connection delta into usage.request_count
  # (the L4 unit of work), mirroring project_usage_serving/2. Skips a principal-less
  # op exactly as the serving path does.
  defp project_usage_stateful(_conn, %Op{principal: nil}), do: :ok

  defp project_usage_stateful(conn, %Op{payload: payload} = op) do
    cx_delta = Map.get(payload, :cx_delta, 0)

    sql = """
    INSERT INTO usage (principal, day, tenant, vcpu_seconds, gb_seconds, task_count, request_count, updated_at)
    VALUES (?, ?, ?, 0, 0, 0, ?, ?)
    ON CONFLICT(principal, day) DO UPDATE SET
      request_count = request_count + excluded.request_count,
      updated_at = excluded.updated_at
    """

    with {:ok, stmt} <- Sqlite3.prepare(conn, sql),
         :ok <-
           Sqlite3.bind(stmt, [
             op.principal,
             div(op.ts, @day_ms),
             op.tenant,
             cx_delta,
             op.ts
           ]),
         :done <- Sqlite3.step(conn, stmt),
         :ok <- Sqlite3.release(conn, stmt) do
      :ok
    end
  end

  # group_stats usage: accrue LIVE-SECONDS PER MEMBER into vcpu_seconds/gb_seconds
  # (the compute unit, unlike serving/stateful stats which count L4/L7 units into
  # request_count). A composite group bills every member's live-seconds: a 3-member
  # group bills 3 VMs' worth. The payload carries the per-member live-seconds for one
  # VM plus `member_count` (from the R4 stats-sweep shape, generalized), and the
  # projection multiplies, so the "3 VMs' worth" is explicit and replay-deterministic.
  # task_count is left untouched (a group is not a task). Skips a principal-less op
  # exactly as the serving/stateful paths do. No-op when the op carried no usage.
  defp project_usage_group(_conn, %Op{principal: nil}), do: :ok

  defp project_usage_group(conn, %Op{payload: payload} = op) do
    case Map.get(payload, :usage) do
      usage when is_map(usage) ->
        # member_count is coerced the same defensive way the usage values go through
        # to_float/1: a malformed op (e.g. a string member_count) must never raise an
        # ArithmeticError inside the append transaction. A non-integer/absent count
        # bills one member's worth.
        member_count = to_pos_int(Map.get(payload, :member_count), 1)
        vcpu_seconds = to_float(Map.get(usage, :vcpu_seconds, 0)) * member_count
        gb_seconds = to_float(Map.get(usage, :gb_seconds, 0)) * member_count

        sql = """
        INSERT INTO usage (principal, day, tenant, vcpu_seconds, gb_seconds, task_count, request_count, updated_at)
        VALUES (?, ?, ?, ?, ?, 0, 0, ?)
        ON CONFLICT(principal, day) DO UPDATE SET
          vcpu_seconds = vcpu_seconds + excluded.vcpu_seconds,
          gb_seconds = gb_seconds + excluded.gb_seconds,
          updated_at = excluded.updated_at
        """

        with {:ok, stmt} <- Sqlite3.prepare(conn, sql),
             :ok <-
               Sqlite3.bind(stmt, [
                 op.principal,
                 div(op.ts, @day_ms),
                 op.tenant,
                 vcpu_seconds,
                 gb_seconds,
                 op.ts
               ]),
             :done <- Sqlite3.step(conn, stmt),
             :ok <- Sqlite3.release(conn, stmt) do
          :ok
        end

      _ ->
        :ok
    end
  end

  defp to_float(n) when is_number(n), do: n * 1.0
  defp to_float(_), do: 0.0

  # Coerce a payload count to a POSITIVE integer, defaulting on anything else (a
  # non-positive, non-integer, or absent value). Keeps a malformed op from raising
  # inside the append transaction, mirroring to_float/1's total-function shape.
  defp to_pos_int(n, _default) when is_integer(n) and n > 0, do: n
  defp to_pos_int(_n, default), do: default

  defp existing_task_for_idempotency_key(_conn, _workload, nil), do: {:ok, nil}

  defp existing_task_for_idempotency_key(conn, workload, idempotency_key) do
    sql = "SELECT task_id FROM tasks WHERE workload=? AND idempotency_key=?"

    with {:ok, stmt} <- Sqlite3.prepare(conn, sql),
         :ok <- Sqlite3.bind(stmt, [workload, idempotency_key]) do
      result =
        case Sqlite3.step(conn, stmt) do
          {:row, [task_id]} -> {:ok, task_id}
          :done -> {:ok, nil}
          {:error, reason} -> {:error, reason}
        end

      :ok = Sqlite3.release(conn, stmt)
      result
    end
  end

  # -- reads ---------------------------------------------------------------

  # A caller asking for a `seq` below the durable prefix marker is asking for
  # history that has been compacted away (it lives only as projected state now),
  # which MUST be distinguishable from an empty-but-intact log so a replayer does
  # not silently assume it saw the whole journal. `read_from/2` reads ops STRICTLY
  # after `seq`, so any `seq >= marker` still sees every surviving op; only a
  # `seq < marker` could miss compacted ops, hence the guard. See ADR embervm/002.
  defp do_read_from(conn, seq) do
    marker = read_marker(conn)

    if seq < marker do
      {:error, {:compacted, marker}}
    else
      sql = """
      SELECT seq, ts, tenant, principal, workload, task_id, session_id, serving_instance_id, stateful_instance_id, group_instance_id, kind, payload_json
      FROM ops WHERE seq > ? ORDER BY seq ASC
      """

      with {:ok, stmt} <- Sqlite3.prepare(conn, sql),
           :ok <- Sqlite3.bind(stmt, [seq]) do
        ops = collect_ops(conn, stmt, [])
        :ok = Sqlite3.release(conn, stmt)
        {:ok, ops}
      end
    end
  end

  defp collect_ops(conn, stmt, acc) do
    case Sqlite3.step(conn, stmt) do
      {:row,
       [
         seq,
         ts,
         tenant,
         principal,
         workload,
         task_id,
         session_id,
         serving_instance_id,
         stateful_instance_id,
         group_instance_id,
         kind,
         payload_json
       ]} ->
        op = %Op{
          seq: seq,
          ts: ts,
          tenant: tenant,
          principal: principal,
          workload: workload,
          task_id: task_id,
          session_id: session_id,
          serving_instance_id: serving_instance_id,
          stateful_instance_id: stateful_instance_id,
          group_instance_id: group_instance_id,
          kind: String.to_existing_atom(kind),
          payload: decode_payload(payload_json)
        }

        collect_ops(conn, stmt, [op | acc])

      :done ->
        Enum.reverse(acc)
    end
  end

  defp do_load_tasks(conn) do
    sql = """
    SELECT task_id, tenant, principal, workload, state, attempt, idempotency_key, submitted_at, updated_at, expires_at
    FROM tasks
    """

    with {:ok, stmt} <- Sqlite3.prepare(conn, sql) do
      tasks = collect_tasks(conn, stmt, [])
      :ok = Sqlite3.release(conn, stmt)
      {:ok, tasks}
    end
  end

  defp do_load_sessions(conn) do
    sql = """
    SELECT session_id, tenant, principal, workload, state, node_id, volume_node_id,
           base_snapshot_ref, base_digest, generation, snapshot_ref, snapshot_size_bytes,
           token_sha256, created_at, last_invoke_at, expires_at, updated_at, terminal_reason
    FROM sessions
    """

    with {:ok, stmt} <- Sqlite3.prepare(conn, sql) do
      sessions = collect_sessions(conn, stmt, [])
      :ok = Sqlite3.release(conn, stmt)
      {:ok, sessions}
    end
  end

  defp collect_sessions(conn, stmt, acc) do
    case Sqlite3.step(conn, stmt) do
      {:row,
       [
         session_id,
         tenant,
         principal,
         workload,
         state,
         node_id,
         volume_node_id,
         base_snapshot_ref,
         base_digest,
         generation,
         snapshot_ref,
         snapshot_size_bytes,
         token_sha256,
         created_at,
         last_invoke_at,
         expires_at,
         updated_at,
         terminal_reason
       ]} ->
        session = %{
          session_id: session_id,
          tenant: tenant,
          principal: principal,
          workload: workload,
          state: state,
          node_id: node_id,
          volume_node_id: volume_node_id,
          base_snapshot_ref: base_snapshot_ref,
          base_digest: base_digest,
          generation: generation,
          snapshot_ref: snapshot_ref,
          snapshot_size_bytes: snapshot_size_bytes,
          token_sha256: token_sha256,
          created_at: created_at,
          last_invoke_at: last_invoke_at,
          expires_at: expires_at,
          updated_at: updated_at,
          terminal_reason: terminal_reason
        }

        collect_sessions(conn, stmt, [session | acc])

      :done ->
        Enum.reverse(acc)
    end
  end

  defp do_load_serving_instances(conn) do
    sql = """
    SELECT instance_id, tenant, principal, workload, state, node_id, vm_id, ip, port,
           base_snapshot_ref, base_digest, generation, snapshot_ref, snapshot_size_bytes,
           created_at, last_active_at, updated_at, terminal_reason
    FROM serving_instances
    """

    with {:ok, stmt} <- Sqlite3.prepare(conn, sql) do
      instances = collect_serving_instances(conn, stmt, [])
      :ok = Sqlite3.release(conn, stmt)
      {:ok, instances}
    end
  end

  defp collect_serving_instances(conn, stmt, acc) do
    case Sqlite3.step(conn, stmt) do
      {:row,
       [
         instance_id,
         tenant,
         principal,
         workload,
         state,
         node_id,
         vm_id,
         ip,
         port,
         base_snapshot_ref,
         base_digest,
         generation,
         snapshot_ref,
         snapshot_size_bytes,
         created_at,
         last_active_at,
         updated_at,
         terminal_reason
       ]} ->
        instance = %{
          instance_id: instance_id,
          tenant: tenant,
          principal: principal,
          workload: workload,
          state: state,
          node_id: node_id,
          vm_id: vm_id,
          ip: ip,
          port: port,
          base_snapshot_ref: base_snapshot_ref,
          base_digest: base_digest,
          generation: generation,
          snapshot_ref: snapshot_ref,
          snapshot_size_bytes: snapshot_size_bytes,
          created_at: created_at,
          last_active_at: last_active_at,
          updated_at: updated_at,
          terminal_reason: terminal_reason
        }

        collect_serving_instances(conn, stmt, [instance | acc])

      :done ->
        Enum.reverse(acc)
    end
  end

  defp do_load_stateful_instances(conn) do
    sql = """
    SELECT instance_id, tenant, principal, workload, state, node_id, vm_id, ip, port,
           generation, snapshot_ref, snapshot_generation, snapshot_size_bytes,
           created_at, last_active_at, updated_at, terminal_reason
    FROM stateful_instances
    """

    with {:ok, stmt} <- Sqlite3.prepare(conn, sql) do
      instances = collect_stateful_instances(conn, stmt, [])
      :ok = Sqlite3.release(conn, stmt)
      {:ok, instances}
    end
  end

  defp collect_stateful_instances(conn, stmt, acc) do
    case Sqlite3.step(conn, stmt) do
      {:row,
       [
         instance_id,
         tenant,
         principal,
         workload,
         state,
         node_id,
         vm_id,
         ip,
         port,
         generation,
         snapshot_ref,
         snapshot_generation,
         snapshot_size_bytes,
         created_at,
         last_active_at,
         updated_at,
         terminal_reason
       ]} ->
        instance = %{
          instance_id: instance_id,
          tenant: tenant,
          principal: principal,
          workload: workload,
          state: state,
          node_id: node_id,
          vm_id: vm_id,
          ip: ip,
          port: port,
          generation: generation,
          snapshot_ref: snapshot_ref,
          snapshot_generation: snapshot_generation,
          snapshot_size_bytes: snapshot_size_bytes,
          created_at: created_at,
          last_active_at: last_active_at,
          updated_at: updated_at,
          terminal_reason: terminal_reason
        }

        collect_stateful_instances(conn, stmt, [instance | acc])

      :done ->
        Enum.reverse(acc)
    end
  end

  defp do_load_volumes(conn) do
    sql = """
    SELECT workload, node_id, generation, size_bytes, allocated_bytes, created_at, updated_at
    FROM volumes
    """

    with {:ok, stmt} <- Sqlite3.prepare(conn, sql) do
      volumes = collect_volumes(conn, stmt, [])
      :ok = Sqlite3.release(conn, stmt)
      {:ok, volumes}
    end
  end

  defp collect_volumes(conn, stmt, acc) do
    case Sqlite3.step(conn, stmt) do
      {:row,
       [workload, node_id, generation, size_bytes, allocated_bytes, created_at, updated_at]} ->
        volume = %{
          workload: workload,
          node_id: node_id,
          generation: generation,
          size_bytes: size_bytes,
          allocated_bytes: allocated_bytes,
          created_at: created_at,
          updated_at: updated_at
        }

        collect_volumes(conn, stmt, [volume | acc])

      :done ->
        Enum.reverse(acc)
    end
  end

  defp do_load_volume_blessing(conn) do
    sql = """
    SELECT workload, blessed_generation, created_at, updated_at
    FROM volume_blessing
    """

    with {:ok, stmt} <- Sqlite3.prepare(conn, sql) do
      rows = collect_volume_blessing(conn, stmt, [])
      :ok = Sqlite3.release(conn, stmt)
      {:ok, rows}
    end
  end

  defp do_load_blessing_leases(conn) do
    sql = "SELECT workload, node_id, next_generation, lease_end, created_at, updated_at FROM blessing_lease"
    with {:ok, stmt} <- Sqlite3.prepare(conn, sql) do
      rows = collect_blessing_leases(conn, stmt, [])
      :ok = Sqlite3.release(conn, stmt)
      {:ok, rows}
    end
  end

  defp collect_blessing_leases(conn, stmt, acc) do
    case Sqlite3.step(conn, stmt) do
      {:row, [workload, node_id, next_generation, lease_end, created_at, updated_at]} ->
        collect_blessing_leases(conn, stmt, [%{workload: workload, node_id: node_id, next_generation: next_generation, lease_end: lease_end, created_at: created_at, updated_at: updated_at} | acc])
      :done -> Enum.reverse(acc)
    end
  end

  defp collect_volume_blessing(conn, stmt, acc) do
    case Sqlite3.step(conn, stmt) do
      {:row, [workload, blessed_generation, created_at, updated_at]} ->
        row = %{
          workload: workload,
          blessed_generation: blessed_generation,
          created_at: created_at,
          updated_at: updated_at
        }

        collect_volume_blessing(conn, stmt, [row | acc])

      :done ->
        Enum.reverse(acc)
    end
  end

  defp do_load_checkpoint_dispatches(conn) do
    sql = """
    SELECT workload, vm_id, generation
    FROM checkpoint_dispatch
    """

    with {:ok, stmt} <- Sqlite3.prepare(conn, sql) do
      rows = collect_checkpoint_dispatches(conn, stmt, [])
      :ok = Sqlite3.release(conn, stmt)
      {:ok, rows}
    end
  end

  defp collect_checkpoint_dispatches(conn, stmt, acc) do
    case Sqlite3.step(conn, stmt) do
      {:row, [workload, vm_id, generation]} ->
        row = %{workload: workload, vm_id: vm_id, generation: generation}
        collect_checkpoint_dispatches(conn, stmt, [row | acc])

      :done ->
        Enum.reverse(acc)
    end
  end

  defp do_load_group_instances(conn) do
    sql = """
    SELECT instance_id, tenant, principal, workload, state, node_id, subnet_cidr,
           entry_member, entry_port, listen_port, set_id,
           created_at, last_active_at, updated_at, terminal_reason
    FROM group_instances
    """

    with {:ok, stmt} <- Sqlite3.prepare(conn, sql) do
      instances = collect_group_instances(conn, stmt, [])
      :ok = Sqlite3.release(conn, stmt)
      {:ok, instances}
    end
  end

  defp collect_group_instances(conn, stmt, acc) do
    case Sqlite3.step(conn, stmt) do
      {:row,
       [
         instance_id,
         tenant,
         principal,
         workload,
         state,
         node_id,
         subnet_cidr,
         entry_member,
         entry_port,
         listen_port,
         set_id,
         created_at,
         last_active_at,
         updated_at,
         terminal_reason
       ]} ->
        instance = %{
          instance_id: instance_id,
          tenant: tenant,
          principal: principal,
          workload: workload,
          state: state,
          node_id: node_id,
          subnet_cidr: subnet_cidr,
          entry_member: entry_member,
          entry_port: entry_port,
          listen_port: listen_port,
          set_id: set_id,
          created_at: created_at,
          last_active_at: last_active_at,
          updated_at: updated_at,
          terminal_reason: terminal_reason
        }

        collect_group_instances(conn, stmt, [instance | acc])

      :done ->
        Enum.reverse(acc)
    end
  end

  defp do_load_group_members(conn) do
    sql = """
    SELECT instance_id, member_name, member_index, vm_id, ip, state, snapshot_ref, healthy, updated_at
    FROM group_members
    """

    with {:ok, stmt} <- Sqlite3.prepare(conn, sql) do
      members = collect_group_members(conn, stmt, [])
      :ok = Sqlite3.release(conn, stmt)
      {:ok, members}
    end
  end

  defp collect_group_members(conn, stmt, acc) do
    case Sqlite3.step(conn, stmt) do
      {:row, [instance_id, member_name, member_index, vm_id, ip, state, snapshot_ref, healthy, updated_at]} ->
        member = %{
          instance_id: instance_id,
          member_name: member_name,
          member_index: member_index,
          vm_id: vm_id,
          ip: ip,
          state: state,
          snapshot_ref: snapshot_ref,
          # SQLite stores the flag as 0/1; surface it as a bool for the GroupStore.
          healthy: healthy == 1,
          updated_at: updated_at
        }

        collect_group_members(conn, stmt, [member | acc])

      :done ->
        Enum.reverse(acc)
    end
  end

  defp do_load_result(conn, task_id) do
    sql = """
    SELECT status_code, body, size_bytes, truncated, created_at, expires_at, headers
    FROM results WHERE task_id=?
    """

    with {:ok, stmt} <- Sqlite3.prepare(conn, sql),
         :ok <- Sqlite3.bind(stmt, [task_id]) do
      result =
        case Sqlite3.step(conn, stmt) do
          {:row, [status_code, body, size_bytes, truncated, created_at, expires_at, headers]} ->
            {:ok,
             %{
               task_id: task_id,
               status_code: status_code,
               body: body,
               size_bytes: size_bytes,
               truncated: truncated == 1,
               created_at: created_at,
               expires_at: expires_at,
               headers: decode_headers(headers)
             }}

          :done ->
            {:ok, nil}

          {:error, reason} ->
            {:error, reason}
        end

      :ok = Sqlite3.release(conn, stmt)
      result
    end
  end

  # Reads the guest-request envelope from a task's `submitted` op. The request
  # lives only in the immutable ops log (payload_json), never a projected
  # column, so this selects the earliest submitted op for the task (the
  # ops_task_id_idx index makes it cheap) and pulls the `request` member out of
  # the decoded payload. Keys come back as strings (decode_payload normalizes both
  # the legacy JSON and the new ETF-blob forms to string keys), which the
  # dispatcher expects. {:ok, nil} for an unknown task or a submitted op that
  # carried no request (an older record).
  defp do_load_request(conn, task_id) do
    sql = """
    SELECT payload_json FROM ops
    WHERE task_id=? AND kind='submitted' ORDER BY seq ASC LIMIT 1
    """

    with {:ok, stmt} <- Sqlite3.prepare(conn, sql),
         :ok <- Sqlite3.bind(stmt, [task_id]) do
      result =
        case Sqlite3.step(conn, stmt) do
          {:row, [payload_json]} ->
            {:ok, Map.get(decode_payload(payload_json), "request")}

          :done ->
            {:ok, nil}

          {:error, reason} ->
            {:error, reason}
        end

      :ok = Sqlite3.release(conn, stmt)
      result
    end
  end

  # Pages the usage projection (Task 12). Filters by an epoch-day floor and an
  # optional exact principal, ordered stably by (principal, day) so offset paging
  # is deterministic. `limit: :infinity` maps to SQLite's LIMIT -1 (no cap), used
  # by Embervm.Metering's boot rebuild; the API path always passes a clamped
  # integer limit so an unbounded scan never queues ahead of a completion append
  # on this single-writer connection.
  defp do_list_usage(conn, opts) do
    since_day = Keyword.get(opts, :since_day, 0)
    principal = Keyword.get(opts, :principal)
    limit = Keyword.get(opts, :limit, 100)
    offset = Keyword.get(opts, :offset, 0)

    {where, filter_params} =
      case principal do
        nil -> {"day >= ?", [since_day]}
        p -> {"day >= ? AND principal = ?", [since_day, p]}
      end

    limit_sql = if limit == :infinity, do: -1, else: limit

    sql = """
    SELECT principal, day, tenant, vcpu_seconds, gb_seconds, task_count, request_count, updated_at
    FROM usage WHERE #{where}
    ORDER BY principal ASC, day ASC
    LIMIT ? OFFSET ?
    """

    with {:ok, total} <- count_usage(conn, where, filter_params),
         {:ok, stmt} <- Sqlite3.prepare(conn, sql),
         :ok <- Sqlite3.bind(stmt, filter_params ++ [limit_sql, offset]) do
      items = collect_usage(conn, stmt, [])
      :ok = Sqlite3.release(conn, stmt)
      {:ok, %{items: items, total: total, limit: limit, offset: offset}}
    end
  end

  defp count_usage(conn, where, params) do
    with {:ok, stmt} <- Sqlite3.prepare(conn, "SELECT COUNT(*) FROM usage WHERE #{where}"),
         :ok <- Sqlite3.bind(stmt, params) do
      result =
        case Sqlite3.step(conn, stmt) do
          {:row, [n]} -> {:ok, n}
          :done -> {:ok, 0}
          {:error, reason} -> {:error, reason}
        end

      :ok = Sqlite3.release(conn, stmt)
      result
    end
  end

  defp collect_usage(conn, stmt, acc) do
    case Sqlite3.step(conn, stmt) do
      {:row, [principal, day, tenant, vcpu_seconds, gb_seconds, task_count, request_count, updated_at]} ->
        row = %{
          principal: principal,
          day: day,
          tenant: tenant,
          vcpu_seconds: vcpu_seconds,
          gb_seconds: gb_seconds,
          task_count: task_count,
          request_count: request_count,
          updated_at: updated_at
        }

        collect_usage(conn, stmt, [row | acc])

      :done ->
        Enum.reverse(acc)
    end
  end

  defp collect_tasks(conn, stmt, acc) do
    case Sqlite3.step(conn, stmt) do
      {:row,
       [task_id, tenant, principal, workload, state, attempt, idempotency_key, submitted_at, updated_at, expires_at]} ->
        task = %{
          task_id: task_id,
          tenant: tenant,
          principal: principal,
          workload: workload,
          state: state,
          attempt: attempt,
          idempotency_key: idempotency_key,
          submitted_at: submitted_at,
          updated_at: updated_at,
          expires_at: expires_at
        }

        collect_tasks(conn, stmt, [task | acc])

      :done ->
        Enum.reverse(acc)
    end
  end

  # -- compaction ------------------------------------------------------------

  # ONE bounded batch (ADR embervm/002): at most `compact_batch_size` deletions
  # per table so the single writer never holds the connection long enough to blow
  # the 5ms append budget (the scheduled Compactor loops until `done`, and each
  # batch is a discrete GenServer call, so queued appends interleave between them).
  # Prunes the projection tables (expired results, terminal tasks past retention)
  # AND prefix-compacts the ops journal behind the durable marker. `done` is true
  # only when every table came in under the batch ceiling (nothing left to sweep).
  defp do_compact(conn, now_ms, state) do
    batch = state.compact_batch_size

    with {:ok, results_deleted} <- delete_expired_results(conn, now_ms, batch),
         {:ok, tasks_compacted} <- delete_terminal_tasks(conn, now_ms, state.retention_ms, batch),
         {:ok, sessions_compacted} <- delete_terminal_sessions(conn, now_ms, state.retention_ms, batch),
         {:ok, serving_instances_compacted} <-
           delete_terminal_serving_instances(conn, now_ms, state.retention_ms, batch),
         {:ok, stateful_instances_compacted} <-
           delete_terminal_stateful_instances(conn, now_ms, state.retention_ms, batch),
         {:ok, group_instances_compacted} <-
           delete_terminal_group_instances(conn, now_ms, state.retention_ms, batch),
         {:ok, ops_compacted, marker} <- compact_ops(conn, now_ms, state.journal_horizon_ms, batch) do
      done =
        results_deleted < batch and tasks_compacted < batch and sessions_compacted < batch and
          serving_instances_compacted < batch and stateful_instances_compacted < batch and
          group_instances_compacted < batch and ops_compacted < batch

      {:ok,
       %{
         results_deleted: results_deleted,
         tasks_compacted: tasks_compacted,
         sessions_compacted: sessions_compacted,
         serving_instances_compacted: serving_instances_compacted,
         stateful_instances_compacted: stateful_instances_compacted,
         group_instances_compacted: group_instances_compacted,
         ops_compacted: ops_compacted,
         compacted_through: marker,
         done: done
       }}
    end
  end

  # Prune terminal session projection rows past the 7-day retention, the same
  # cutoff and bounded-batch discipline as terminal tasks. A non-terminal session
  # is never pruned (its state is not in @session_terminal_states), and its ops
  # remain pinned against compaction by blocker_seq. Bounded via the rowid
  # subquery form (DELETE ... LIMIT is unavailable on the bundled SQLite).
  defp delete_terminal_sessions(conn, now_ms, retention_ms, batch) do
    cutoff = now_ms - retention_ms
    placeholders = @session_terminal_states |> Enum.map(fn _ -> "?" end) |> Enum.join(", ")

    sql = """
    DELETE FROM sessions WHERE rowid IN (
      SELECT rowid FROM sessions WHERE state IN (#{placeholders}) AND updated_at < ? LIMIT ?
    )
    """

    with {:ok, stmt} <- Sqlite3.prepare(conn, sql),
         :ok <- Sqlite3.bind(stmt, @session_terminal_states ++ [cutoff, batch]),
         :done <- Sqlite3.step(conn, stmt),
         :ok <- Sqlite3.release(conn, stmt),
         {:ok, changed} <- Sqlite3.changes(conn) do
      {:ok, changed}
    end
  end

  # Prune terminal serving-instance projection rows past the 7-day retention,
  # mirroring delete_terminal_sessions/4 exactly. A non-terminal serving
  # instance is never pruned (its state is not in @serving_terminal_states),
  # and its ops remain pinned against compaction by blocker_seq.
  defp delete_terminal_serving_instances(conn, now_ms, retention_ms, batch) do
    cutoff = now_ms - retention_ms
    placeholders = @serving_terminal_states |> Enum.map(fn _ -> "?" end) |> Enum.join(", ")

    sql = """
    DELETE FROM serving_instances WHERE rowid IN (
      SELECT rowid FROM serving_instances WHERE state IN (#{placeholders}) AND updated_at < ? LIMIT ?
    )
    """

    with {:ok, stmt} <- Sqlite3.prepare(conn, sql),
         :ok <- Sqlite3.bind(stmt, @serving_terminal_states ++ [cutoff, batch]),
         :done <- Sqlite3.step(conn, stmt),
         :ok <- Sqlite3.release(conn, stmt),
         {:ok, changed} <- Sqlite3.changes(conn) do
      {:ok, changed}
    end
  end

  # Prune terminal stateful-instance projection rows past retention, mirroring
  # delete_terminal_serving_instances/4. A non-terminal instance is never pruned,
  # and its ops stay pinned by blocker_seq. The `volumes` table is deliberately
  # NOT swept: a volume row lives until volume_deleted (data outlives instances).
  defp delete_terminal_stateful_instances(conn, now_ms, retention_ms, batch) do
    cutoff = now_ms - retention_ms
    placeholders = @stateful_terminal_states |> Enum.map(fn _ -> "?" end) |> Enum.join(", ")

    sql = """
    DELETE FROM stateful_instances WHERE rowid IN (
      SELECT rowid FROM stateful_instances WHERE state IN (#{placeholders}) AND updated_at < ? LIMIT ?
    )
    """

    with {:ok, stmt} <- Sqlite3.prepare(conn, sql),
         :ok <- Sqlite3.bind(stmt, @stateful_terminal_states ++ [cutoff, batch]),
         :done <- Sqlite3.step(conn, stmt),
         :ok <- Sqlite3.release(conn, stmt),
         {:ok, changed} <- Sqlite3.changes(conn) do
      {:ok, changed}
    end
  end

  # Prune terminal group-instance projection rows past retention, mirroring
  # delete_terminal_stateful_instances/4. A non-terminal group is never pruned, and
  # its ops stay pinned by blocker_seq. `group_members` has no FK cascade (it keys on
  # instance_id but does not REFERENCE group_instances), so this deletes the pruned
  # instances' member rows in the same batch: member rows live and die with their
  # group instance, so an orphaned member row must never survive its instance.
  defp delete_terminal_group_instances(conn, now_ms, retention_ms, batch) do
    cutoff = now_ms - retention_ms
    placeholders = @group_terminal_states |> Enum.map(fn _ -> "?" end) |> Enum.join(", ")

    # Members of the instances about to be pruned, deleted FIRST so the member rows
    # never outlive the instance row (the selection is the same terminal+aged set).
    member_sql = """
    DELETE FROM group_members WHERE instance_id IN (
      SELECT instance_id FROM group_instances WHERE state IN (#{placeholders}) AND updated_at < ? LIMIT ?
    )
    """

    instance_sql = """
    DELETE FROM group_instances WHERE rowid IN (
      SELECT rowid FROM group_instances WHERE state IN (#{placeholders}) AND updated_at < ? LIMIT ?
    )
    """

    with {:ok, mstmt} <- Sqlite3.prepare(conn, member_sql),
         :ok <- Sqlite3.bind(mstmt, @group_terminal_states ++ [cutoff, batch]),
         :done <- Sqlite3.step(conn, mstmt),
         :ok <- Sqlite3.release(conn, mstmt),
         {:ok, stmt} <- Sqlite3.prepare(conn, instance_sql),
         :ok <- Sqlite3.bind(stmt, @group_terminal_states ++ [cutoff, batch]),
         :done <- Sqlite3.step(conn, stmt),
         :ok <- Sqlite3.release(conn, stmt),
         {:ok, changed} <- Sqlite3.changes(conn) do
      {:ok, changed}
    end
  end

  # Bounded DELETE via the portable rowid-subquery form: Exqlite's bundled SQLite
  # is not built with SQLITE_ENABLE_UPDATE_DELETE_LIMIT, so `DELETE ... LIMIT` is
  # unavailable; `DELETE ... WHERE rowid IN (SELECT rowid ... LIMIT ?)` bounds the
  # batch on any build.
  defp delete_expired_results(conn, now_ms, batch) do
    sql = """
    DELETE FROM results WHERE rowid IN (
      SELECT rowid FROM results WHERE expires_at IS NOT NULL AND expires_at < ? LIMIT ?
    )
    """

    with {:ok, stmt} <- Sqlite3.prepare(conn, sql),
         :ok <- Sqlite3.bind(stmt, [now_ms, batch]),
         :done <- Sqlite3.step(conn, stmt),
         :ok <- Sqlite3.release(conn, stmt),
         {:ok, changed} <- Sqlite3.changes(conn) do
      {:ok, changed}
    end
  end

  defp delete_terminal_tasks(conn, now_ms, retention_ms, batch) do
    cutoff = now_ms - retention_ms
    placeholders = @terminal_states |> Enum.map(fn _ -> "?" end) |> Enum.join(", ")

    sql = """
    DELETE FROM tasks WHERE rowid IN (
      SELECT rowid FROM tasks WHERE state IN (#{placeholders}) AND updated_at < ? LIMIT ?
    )
    """

    with {:ok, stmt} <- Sqlite3.prepare(conn, sql),
         :ok <- Sqlite3.bind(stmt, @terminal_states ++ [cutoff, batch]),
         :done <- Sqlite3.step(conn, stmt),
         :ok <- Sqlite3.release(conn, stmt),
         {:ok, changed} <- Sqlite3.changes(conn) do
      {:ok, changed}
    end
  end

  # Advance the durable ops-journal prefix marker (monotonic, prefix-safe,
  # horizon-bounded), then batch-delete `ops WHERE seq <= marker`.
  #
  # The marker may only reach a seq such that EVERY op at or below it is (a) older
  # than the horizon AND (b) not owned by a live (non-terminal) task. We find the
  # smallest seq that must stay BLOCKED (the first op that is either recent OR owned
  # by a live task); the highest safely-compactable candidate is one below it. With
  # no blocker at all, the whole log is compactable up to MAX(seq). The marker never
  # decreases (`max(current, candidate)`), so a shrinking horizon or a task going
  # live again can never un-compact already-projected history.
  defp compact_ops(conn, now_ms, journal_horizon_ms, batch) do
    cutoff = now_ms - journal_horizon_ms
    current = read_marker(conn)

    with {:ok, blocker} <- blocker_seq(conn, cutoff),
         {:ok, candidate} <- marker_candidate(conn, blocker) do
      new_marker = max(current, candidate)

      with :ok <- write_marker(conn, new_marker),
           {:ok, deleted} <- delete_ops_prefix(conn, new_marker, batch) do
        {:ok, deleted, new_marker}
      end
    end
  end

  # The smallest seq that must NOT be compacted: the first op that is either newer
  # than the horizon (ts >= cutoff), owned by a live (non-terminal) task, owned
  # by a live (non-terminal) session, owned by a live (non-terminal) serving
  # instance, owned by a live (non-terminal) stateful instance, OR owned by a live
  # (non-terminal) group instance. The session clause is the R2 never-compact-a-
  # live-session rule; the serving/stateful/group clauses are its R3/R4/R5 mirrors:
  # a non-terminal instance pins its ops exactly as a live task or session does, so
  # its lineage/lifecycle history stays replayable until it terminates. A
  # serving_stats/stateful_stats/group_stats op carries no instance id (it is
  # workload-scoped), so it is pinned only by the ts >= cutoff clause like any other
  # audit-only op, never by this clause. NULL means no op is blocked, so the whole
  # log is eligible.
  defp blocker_seq(conn, cutoff) do
    live_placeholders = @live_states |> Enum.map(fn _ -> "?" end) |> Enum.join(", ")
    session_live_placeholders = @session_live_states |> Enum.map(fn _ -> "?" end) |> Enum.join(", ")
    serving_live_placeholders = @serving_live_states |> Enum.map(fn _ -> "?" end) |> Enum.join(", ")
    stateful_live_placeholders = @stateful_live_states |> Enum.map(fn _ -> "?" end) |> Enum.join(", ")
    group_live_placeholders = @group_live_states |> Enum.map(fn _ -> "?" end) |> Enum.join(", ")

    sql = """
    SELECT MIN(seq) FROM ops
    WHERE ts >= ?
       OR task_id IN (SELECT task_id FROM tasks WHERE state IN (#{live_placeholders}))
       OR session_id IN (SELECT session_id FROM sessions WHERE state IN (#{session_live_placeholders}))
       OR serving_instance_id IN (
            SELECT instance_id FROM serving_instances WHERE state IN (#{serving_live_placeholders})
          )
       OR stateful_instance_id IN (
            SELECT instance_id FROM stateful_instances WHERE state IN (#{stateful_live_placeholders})
          )
       OR group_instance_id IN (
            SELECT instance_id FROM group_instances WHERE state IN (#{group_live_placeholders})
          )
    """

    with {:ok, stmt} <- Sqlite3.prepare(conn, sql),
         :ok <-
           Sqlite3.bind(
             stmt,
             [cutoff] ++
               @live_states ++
               @session_live_states ++
               @serving_live_states ++ @stateful_live_states ++ @group_live_states
           ) do
      result =
        case Sqlite3.step(conn, stmt) do
          {:row, [seq]} -> {:ok, seq}
          :done -> {:ok, nil}
          {:error, reason} -> {:error, reason}
        end

      :ok = Sqlite3.release(conn, stmt)
      result
    end
  end

  # No blocker -> the whole log is compactable up to its max seq (0 for an empty
  # log). Otherwise everything strictly below the first blocked op is compactable.
  defp marker_candidate(_conn, blocker) when is_integer(blocker), do: {:ok, blocker - 1}

  defp marker_candidate(conn, nil) do
    with {:ok, stmt} <- Sqlite3.prepare(conn, "SELECT MAX(seq) FROM ops") do
      result =
        case Sqlite3.step(conn, stmt) do
          {:row, [nil]} -> {:ok, 0}
          {:row, [seq]} -> {:ok, seq}
          :done -> {:ok, 0}
          {:error, reason} -> {:error, reason}
        end

      :ok = Sqlite3.release(conn, stmt)
      result
    end
  end

  defp delete_ops_prefix(conn, marker, batch) do
    sql = """
    DELETE FROM ops WHERE rowid IN (
      SELECT rowid FROM ops WHERE seq <= ? LIMIT ?
    )
    """

    with {:ok, stmt} <- Sqlite3.prepare(conn, sql),
         :ok <- Sqlite3.bind(stmt, [marker, batch]),
         :done <- Sqlite3.step(conn, stmt),
         :ok <- Sqlite3.release(conn, stmt),
         {:ok, changed} <- Sqlite3.changes(conn) do
      {:ok, changed}
    end
  end

  # Reads the durable prefix marker (`meta.compacted_through_seq`); absent -> 0.
  defp read_marker(conn) do
    with {:ok, stmt} <- Sqlite3.prepare(conn, "SELECT value FROM meta WHERE key = ?"),
         :ok <- Sqlite3.bind(stmt, [@marker_key]) do
      result =
        case Sqlite3.step(conn, stmt) do
          {:row, [value]} -> value
          _ -> 0
        end

      :ok = Sqlite3.release(conn, stmt)
      result
    else
      _ -> 0
    end
  end

  defp write_marker(conn, value) do
    sql = """
    INSERT INTO meta (key, value) VALUES (?, ?)
    ON CONFLICT(key) DO UPDATE SET value = excluded.value
    """

    with {:ok, stmt} <- Sqlite3.prepare(conn, sql),
         :ok <- Sqlite3.bind(stmt, [@marker_key, value]),
         :done <- Sqlite3.step(conn, stmt),
         :ok <- Sqlite3.release(conn, stmt) do
      :ok
    end
  end

  # Projection prune (no op emitted): drops one task's `tasks` row; its `results`
  # row cascades (FK ON DELETE CASCADE, foreign_keys=ON). The dedupe path calls this
  # before a fresh resubmit under a colliding idempotency key.
  defp do_evict_task(conn, task_id) do
    with {:ok, stmt} <- Sqlite3.prepare(conn, "DELETE FROM tasks WHERE task_id = ?"),
         :ok <- Sqlite3.bind(stmt, [task_id]),
         :done <- Sqlite3.step(conn, stmt),
         :ok <- Sqlite3.release(conn, stmt) do
      :ok
    end
  end

  # -- pragmas / ddl ---------------------------------------------------------

  defp apply_pragmas(conn) do
    with :ok <- Sqlite3.execute(conn, "PRAGMA journal_mode=WAL"),
         # fsync every commit: task-state durability takes priority over
         # write latency in R0. If Longhorn p95 write latency proves too high
         # under load, the documented fallback is synchronous=NORMAL plus an
         # explicit WAL checkpoint schedule, trading a small durability window
         # for throughput; not adopted here without a measured need.
         :ok <- Sqlite3.execute(conn, "PRAGMA synchronous=FULL"),
         :ok <- Sqlite3.execute(conn, "PRAGMA foreign_keys=ON") do
      :ok
    end
  end

  defp apply_ddl(conn) do
    Enum.reduce_while(@ddl, :ok, fn stmt, :ok ->
      case Sqlite3.execute(conn, stmt) do
        :ok -> {:cont, :ok}
        {:error, reason} -> {:halt, {:error, reason}}
      end
    end)
  end

  # Additive, idempotent schema migrations for op-log DBs created before a column
  # existed. `CREATE TABLE IF NOT EXISTS` is a no-op on an existing table, so a new
  # column on `results` (the guest response `headers` map, stored as JSON) has to be
  # added with ALTER TABLE. SQLite raises "duplicate column name" if the column is
  # already present (a fresh DB just built it from the CREATE), so this checks the
  # live column set first and only adds what is missing. Fresh AND upgraded DBs both
  # end with the same shape; old result rows keep headers NULL, read back as %{}.
  defp apply_migrations(conn) do
    with :ok <- migrate_results_headers(conn),
         :ok <- migrate_sessions_volume_node_id(conn),
         :ok <- migrate_ops_session_id(conn),
         :ok <- migrate_ops_serving_instance_id(conn),
         :ok <- migrate_usage_request_count(conn),
         :ok <- migrate_ops_stateful_instance_id(conn),
         :ok <- migrate_ops_group_instance_id(conn) do
      :ok
    end
  end

  defp migrate_sessions_volume_node_id(conn) do
    with {:ok, cols} <- table_columns(conn, "sessions") do
      if "volume_node_id" in cols do
        :ok
      else
        Sqlite3.execute(conn, "ALTER TABLE sessions ADD COLUMN volume_node_id TEXT")
      end
    end
  end

  defp migrate_results_headers(conn) do
    with {:ok, cols} <- table_columns(conn, "results") do
      if "headers" in cols do
        :ok
      else
        Sqlite3.execute(conn, "ALTER TABLE results ADD COLUMN headers TEXT")
      end
    end
  end

  # R2: the additive nullable `ops.session_id` column plus its index. A fresh DB
  # already built the column from the CREATE TABLE (and skips the ALTER), so this
  # only fires on a DB created before R2; the ALTER-after-DDL guard is the
  # D-R1.2.1 precedent. The index is created here (not in @ddl) so it always runs
  # AFTER the column exists on both fresh and upgraded DBs. `sessions` itself is a
  # whole new table, so `CREATE TABLE IF NOT EXISTS` in @ddl handles both cases;
  # no ALTER is needed for it.
  defp migrate_ops_session_id(conn) do
    with {:ok, cols} <- table_columns(conn, "ops"),
         :ok <- add_column_if_missing(conn, cols, "session_id", "ALTER TABLE ops ADD COLUMN session_id TEXT") do
      Sqlite3.execute(conn, "CREATE INDEX IF NOT EXISTS ops_session_id_idx ON ops(session_id)")
    end
  end

  # R3: the additive nullable `ops.serving_instance_id` column plus its index,
  # mirroring migrate_ops_session_id/1 exactly. A fresh DB already built the
  # column from the CREATE TABLE (and skips the ALTER), so this only fires on a
  # DB created before R3; the guard is the same D-R1.2.1/R2 ALTER-after-DDL
  # precedent. `serving_instances` itself is a whole new table, so
  # `CREATE TABLE IF NOT EXISTS` in @ddl handles both cases; no ALTER is needed
  # for it.
  defp migrate_ops_serving_instance_id(conn) do
    with {:ok, cols} <- table_columns(conn, "ops"),
         :ok <-
           add_column_if_missing(
             conn,
             cols,
             "serving_instance_id",
             "ALTER TABLE ops ADD COLUMN serving_instance_id TEXT"
           ) do
      Sqlite3.execute(conn, "CREATE INDEX IF NOT EXISTS ops_serving_instance_id_idx ON ops(serving_instance_id)")
    end
  end

  # R3 (D-R3.2.1): the additive `usage.request_count` column, guarded the same
  # way migrate_results_headers/1 guards `results.headers`. A fresh DB already
  # built the column (DEFAULT 0) from the CREATE TABLE and skips the ALTER; an
  # upgraded DB gets it added with existing rows defaulting to 0 (SQLite's
  # ALTER TABLE ADD COLUMN honours the column default for existing rows), so a
  # pre-R3 principal's historical usage rows read back with request_count=0,
  # correctly reflecting that no serving usage existed for them.
  defp migrate_usage_request_count(conn) do
    with {:ok, cols} <- table_columns(conn, "usage") do
      if "request_count" in cols do
        :ok
      else
        Sqlite3.execute(conn, "ALTER TABLE usage ADD COLUMN request_count INTEGER NOT NULL DEFAULT 0")
      end
    end
  end

  # R4: the additive nullable `ops.stateful_instance_id` column plus its index,
  # mirroring migrate_ops_serving_instance_id/1 exactly. A fresh DB already built
  # the column from the CREATE TABLE (and skips the ALTER), so this only fires on
  # a DB created before R4; the guard is the same D-R1.2.1/R2/R3 ALTER-after-DDL
  # precedent. `stateful_instances` and `volumes` are whole new tables, so
  # `CREATE TABLE IF NOT EXISTS` in @ddl handles both cases; no ALTER is needed
  # for them.
  defp migrate_ops_stateful_instance_id(conn) do
    with {:ok, cols} <- table_columns(conn, "ops"),
         :ok <-
           add_column_if_missing(
             conn,
             cols,
             "stateful_instance_id",
             "ALTER TABLE ops ADD COLUMN stateful_instance_id TEXT"
           ) do
      Sqlite3.execute(
        conn,
        "CREATE INDEX IF NOT EXISTS ops_stateful_instance_id_idx ON ops(stateful_instance_id)"
      )
    end
  end

  # R5: the additive nullable `ops.group_instance_id` column plus its index,
  # mirroring migrate_ops_stateful_instance_id/1 exactly. A fresh DB already built
  # the column from the CREATE TABLE (and skips the ALTER), so this only fires on a
  # DB created before R5; the guard is the same D-R1.2.1/R2/R3/R4 ALTER-after-DDL
  # precedent. `group_instances` and `group_members` are whole new tables, so
  # `CREATE TABLE IF NOT EXISTS` in @ddl handles both cases; no ALTER is needed for
  # them.
  defp migrate_ops_group_instance_id(conn) do
    with {:ok, cols} <- table_columns(conn, "ops"),
         :ok <-
           add_column_if_missing(
             conn,
             cols,
             "group_instance_id",
             "ALTER TABLE ops ADD COLUMN group_instance_id TEXT"
           ) do
      Sqlite3.execute(
        conn,
        "CREATE INDEX IF NOT EXISTS ops_group_instance_id_idx ON ops(group_instance_id)"
      )
    end
  end

  defp add_column_if_missing(conn, cols, column, alter_sql) do
    if column in cols do
      :ok
    else
      Sqlite3.execute(conn, alter_sql)
    end
  end

  defp table_columns(conn, table) do
    with {:ok, stmt} <- Sqlite3.prepare(conn, "PRAGMA table_info(#{table})") do
      cols = collect_column_names(conn, stmt, [])
      :ok = Sqlite3.release(conn, stmt)
      {:ok, cols}
    end
  end

  # PRAGMA table_info rows are [cid, name, type, notnull, dflt_value, pk]; the name
  # is column 1. Accumulates every row's name so the migration can test membership.
  defp collect_column_names(conn, stmt, acc) do
    case Sqlite3.step(conn, stmt) do
      {:row, [_cid, name | _rest]} -> collect_column_names(conn, stmt, [name | acc])
      :done -> acc
      {:error, _reason} -> acc
    end
  end

  defp default_path do
    case System.get_env("EMBERVM_OPLOG_PATH") do
      nil -> tmp_path()
      "" -> tmp_path()
      path -> path
    end
  end

  defp tmp_path do
    Path.join(System.tmp_dir!(), "embervm_oplog_#{System.unique_integer([:positive, :monotonic])}.db")
  end

  defp bool_to_int(true), do: 1
  defp bool_to_int(false), do: 0
  defp bool_to_int(nil), do: 0

  # Wrap a binary as an Exqlite {:blob, _} so it binds via sqlite3_bind_blob and
  # stores byte-exact (a plain binary binds as TEXT). nil (a bodyless result)
  # passes through as a NULL bind.
  defp blob(nil), do: nil
  defp blob(bin) when is_binary(bin), do: {:blob, bin}

  # -- payload codec ----------------------------------------------------

  # Op payloads are stored as an Erlang External Term Format binary
  # (:erlang.term_to_binary/1), bound as a {:blob, _} so SQLite keeps the exact
  # bytes. This replaces the previous :json.encode/1: JSON cannot represent a
  # non-UTF-8 binary, so a task whose result (or request) body was binary (e.g.
  # a PNG, first byte 0x89 = 137) crashed the writer with {:invalid_byte, 137}
  # and cascaded a control-plane restart. ETF round-trips ANY term, so a binary
  # body persists byte-exact. Payloads are this node's own trusted data (never
  # untrusted external input), so binary_to_term/1 on read is safe here.
  defp encode_payload(payload) when is_map(payload) do
    {:blob, :erlang.term_to_binary(payload)}
  end

  # An ETF blob begins with the version byte 131; legacy rows written before this
  # change are JSON objects beginning with "{" (123). The first byte disambiguates
  # the two formats during the retention overlap, so old and new rows both read
  # back correctly. binary_to_term/1 restores the original atom-keyed map, which
  # `stringify/1` coerces into the string-keyed shape :json.decode/1 produced, so
  # every reader (the dispatcher's request envelope, replay) is unchanged.
  defp decode_payload(<<131, _::binary>> = term) do
    term |> :erlang.binary_to_term() |> stringify()
  end

  defp decode_payload(json) when is_binary(json) do
    :json.decode(json)
  end

  # Coerces a binary_to_term result into the exact shape :json.decode/1 yields
  # for a legacy row: string keys, non-boolean atom values stringified, and
  # nil-valued map keys dropped (encode used to reject them before JSON encoding).
  # Binaries, numbers, booleans and lists pass through, so a binary body survives
  # intact while staying contract-compatible with the JSON path for all readers.
  defp stringify(map) when is_map(map) do
    for {k, v} <- map, not is_nil(v), into: %{}, do: {to_string(k), stringify(v)}
  end

  defp stringify(list) when is_list(list), do: Enum.map(list, &stringify/1)
  defp stringify(v) when is_atom(v) and not is_boolean(v), do: Atom.to_string(v)
  defp stringify(v), do: v

  # Guest response headers (a string->string map) ride the `results.headers` column
  # as a JSON object. An empty/absent map is stored as NULL so old rows and
  # headerless results are indistinguishable and both read back as %{}.
  defp encode_headers(headers) when is_map(headers) and map_size(headers) == 0, do: nil

  defp encode_headers(headers) when is_map(headers) do
    headers |> :json.encode() |> :erlang.iolist_to_binary()
  end

  defp encode_headers(_), do: nil

  # NULL (old rows, headerless results) and a malformed blob both default to %{},
  # so a read never crashes on a pre-migration record (backward compatibility).
  defp decode_headers(nil), do: %{}

  defp decode_headers(json) when is_binary(json) do
    case :json.decode(json) do
      map when is_map(map) -> map
      _ -> %{}
    end
  rescue
    _ -> %{}
  end

  defp decode_headers(_), do: %{}
end
