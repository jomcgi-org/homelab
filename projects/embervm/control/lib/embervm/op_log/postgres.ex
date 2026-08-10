defmodule Embervm.OpLog.Postgres do
  @moduledoc """
  Postgres implementation of the `Embervm.OpLog` behaviour (PR-4, #18/#27): a
  DSN-selected alternative to `Embervm.OpLog.SQLite`, dormant until an operator
  sets `EMBERVM_OPLOG_DSN` (see `Embervm.Application.op_log_mod/0`). Mirrors the
  SQLite adapter's schema, projection semantics, and single-writer GenServer
  shape exactly, translating only the SQL dialect: postgrex numbered parameters
  (`$1`, `$2`, ...) instead of `?`, `ON CONFLICT` the same way Postgres already
  supports it, `BYTEA` in place of SQLite's untyped BLOB column for the ETF
  payload, and `BIGSERIAL` in place of `INTEGER PRIMARY KEY AUTOINCREMENT` for
  the ops journal's monotonic seq.

  Like the SQLite adapter, every read and write is serialized through this one
  GenServer's postgrex connection: the dispatch hot path never touches the
  op-log directly (it reads ETS), so serializing here costs nothing on the
  critical path and keeps append ordering (and the compaction batch discipline)
  identical to SQLite's. A future PR may widen this to a pool once a real
  workload demands concurrent readers; R0 does not need it.

  `db_size/1` is NOT part of the behaviour (see `Embervm.OpLog`): there is no
  single PVC file to `File.stat/1` for a Postgres backend, so it always returns
  `{:error, :not_supported}` and the Compactor (see `compactor.ex`) omits the
  size from its sweep summary rather than crashing or warning every tick.
  """

  @behaviour Embervm.OpLog

  use GenServer
  require Logger

  alias Embervm.OpLog.Op

  @terminal_states ["succeeded", "failed_permanent", "dead_lettered"]
  @live_states ["queued", "assigned", "running", "failed_retryable"]

  @session_terminal_states ["expired", "evicted", "destroyed", "failed"]
  @session_live_states ["creating", "running", "banking", "banked", "relighting"]

  @serving_terminal_states ["evicted", "destroyed", "failed"]
  @serving_live_states [
    "starting",
    "published",
    "draining",
    "banking",
    "banked",
    "relighting"
  ]

  @stateful_terminal_states ["evicted", "destroyed", "failed"]
  @stateful_live_states [
    "starting",
    "serving",
    "banking",
    "banked",
    "relighting",
    "cold_booting"
  ]

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

  # Same retention/horizon/batch defaults as Embervm.OpLog.SQLite; see that
  # module's moduledoc for the rationale (distinct terminal-task retention vs.
  # ops-journal horizon, and the 5ms-append-budget batch ceiling).
  @default_retention_ms 7 * 24 * 60 * 60 * 1000
  @default_journal_horizon_ms 30 * 24 * 60 * 60 * 1000
  @default_compact_batch_size 500
  @marker_key "compacted_through_seq"

  @ddl [
    """
    CREATE TABLE IF NOT EXISTS ops (
      seq BIGSERIAL PRIMARY KEY,
      ts BIGINT NOT NULL,
      tenant TEXT NOT NULL,
      principal TEXT,
      workload TEXT,
      task_id TEXT,
      session_id TEXT,
      serving_instance_id TEXT,
      stateful_instance_id TEXT,
      group_instance_id TEXT,
      kind TEXT NOT NULL,
      payload_blob BYTEA NOT NULL
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
      submitted_at BIGINT NOT NULL,
      updated_at BIGINT NOT NULL,
      expires_at BIGINT
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
      body BYTEA,
      size_bytes BIGINT NOT NULL,
      truncated INTEGER NOT NULL DEFAULT 0,
      created_at BIGINT NOT NULL,
      expires_at BIGINT,
      headers TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS usage (
      principal TEXT NOT NULL,
      day BIGINT NOT NULL,
      tenant TEXT NOT NULL,
      vcpu_seconds DOUBLE PRECISION NOT NULL DEFAULT 0,
      gb_seconds DOUBLE PRECISION NOT NULL DEFAULT 0,
      task_count INTEGER NOT NULL DEFAULT 0,
      request_count INTEGER NOT NULL DEFAULT 0,
      updated_at BIGINT NOT NULL,
      PRIMARY KEY (principal, day)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sessions (
      session_id TEXT PRIMARY KEY,
      lineage_id TEXT,
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
      snapshot_size_bytes BIGINT,
      token_sha256 TEXT,
      created_at BIGINT NOT NULL,
      last_invoke_at BIGINT,
      expires_at BIGINT,
      updated_at BIGINT NOT NULL,
      terminal_reason TEXT
    )
    """,
    "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS volume_node_id TEXT",
    # #4306 slice 1: additive nullable lineage_id, mirroring
    # Embervm.OpLog.SQLite.migrate_sessions_lineage_id/1. Existing rows get
    # lineage_id=NULL from the ALTER (no DEFAULT); do_load_sessions/1's COALESCE
    # reads those back as session_id, exactly what lineage_id always equalled
    # for them since there is no adoption yet to make the two diverge.
    "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS lineage_id TEXT",
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
      snapshot_size_bytes BIGINT,
      created_at BIGINT NOT NULL,
      last_active_at BIGINT,
      updated_at BIGINT NOT NULL,
      terminal_reason TEXT
    )
    """,
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
      snapshot_size_bytes BIGINT,
      created_at BIGINT NOT NULL,
      last_active_at BIGINT,
      updated_at BIGINT NOT NULL,
      terminal_reason TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS volumes (
      workload TEXT PRIMARY KEY,
      node_id TEXT,
      generation INTEGER NOT NULL DEFAULT 0,
      size_bytes BIGINT,
      allocated_bytes BIGINT,
      created_at BIGINT NOT NULL,
      updated_at BIGINT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS volume_blessing (
      workload TEXT PRIMARY KEY,
      blessed_generation INTEGER NOT NULL,
      created_at BIGINT NOT NULL,
      updated_at BIGINT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS blessing_lease (
      workload TEXT NOT NULL,
      node_id TEXT NOT NULL,
      next_generation BIGINT NOT NULL,
      lease_end BIGINT NOT NULL,
      created_at BIGINT NOT NULL,
      updated_at BIGINT NOT NULL,
      PRIMARY KEY (workload, node_id)
    )
    """,
    # Checkpoint-dispatch record (R7, ADR embervm/017): one row per workload with an
    # in-flight CHECKPOINT the control plane dispatched, so a recovered control plane
    # can auto-heal ONLY its own auto-aborted checkpoint (same vm_id, exactly +1).
    # Written by checkpoint_dispatched, deleted by checkpoint_resolved. Mirrors the
    # SQLite backend's table (see that comment for the full rationale).
    """
    CREATE TABLE IF NOT EXISTS checkpoint_dispatch (
      workload TEXT PRIMARY KEY,
      vm_id TEXT NOT NULL,
      generation INTEGER NOT NULL,
      created_at BIGINT NOT NULL,
      updated_at BIGINT NOT NULL
    )
    """,
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
      created_at BIGINT NOT NULL,
      last_active_at BIGINT,
      updated_at BIGINT NOT NULL,
      terminal_reason TEXT
    )
    """,
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
      updated_at BIGINT NOT NULL,
      PRIMARY KEY (instance_id, member_name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS meta (
      key TEXT PRIMARY KEY,
      value BIGINT
    )
    """
  ]

  @day_ms 86_400_000

  # -- Client API --------------------------------------------------------

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

  # db_size/1 is NOT an Embervm.OpLog callback (see moduledoc): there is no
  # single file to stat for a Postgres backend, so the Compactor's dispatch
  # here always gets {:error, :not_supported} and omits the size from its
  # sweep summary rather than crashing or warning every tick.
  @spec db_size(GenServer.server()) :: {:error, :not_supported}
  def db_size(_server \\ __MODULE__), do: {:error, :not_supported}

  # -- GenServer callbacks ------------------------------------------------

  @impl true
  def init(opts) do
    dsn = Keyword.fetch!(opts, :dsn)
    retention_ms = Keyword.get(opts, :retention_ms, @default_retention_ms)
    journal_horizon_ms = Keyword.get(opts, :journal_horizon_ms, @default_journal_horizon_ms)
    compact_batch_size = Keyword.get(opts, :compact_batch_size, @default_compact_batch_size)

    with {:ok, conn} <- connect(dsn),
         :ok <- apply_ddl(conn) do
      Logger.info("embervm op-log opened against Postgres")

      {:ok,
       %{
         conn: conn,
         retention_ms: retention_ms,
         journal_horizon_ms: journal_horizon_ms,
         compact_batch_size: compact_batch_size
       }}
    else
      {:error, reason} -> {:stop, {:connect_failed, reason}}
    end
  end

  # Postgrex.start_link/1 takes connection opts, not a URL string directly;
  # Postgrex.Utils.default_opts/1 has no public DSN parser, so we accept
  # either a full "postgres://user:pass@host:port/db" DSN (parsed here) or,
  # for tests, opts already in Postgrex's own keyword shape.
  defp connect(dsn) when is_binary(dsn) do
    uri = URI.parse(dsn)
    [user, pass] = String.split(uri.userinfo || ":", ":", parts: 2)
    database = String.trim_leading(uri.path || "", "/")

    Postgrex.start_link(
      hostname: uri.host,
      port: uri.port || 5432,
      username: nil_if_empty(user),
      password: nil_if_empty(pass),
      database: nil_if_empty(database),
      name: nil
    )
  end

  defp connect(opts) when is_list(opts), do: Postgrex.start_link(Keyword.put(opts, :name, nil))

  defp nil_if_empty(""), do: nil
  defp nil_if_empty(v), do: v

  @impl true
  def terminate(_reason, %{conn: conn}) do
    GenServer.stop(conn)
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

  # -- append + projection -------------------------------------------------

  defp do_append(conn, %Op{} = op) do
    if op.kind not in Embervm.OpLog.kinds() do
      {:error, {:unknown_kind, op.kind}}
    else
      case Postgrex.transaction(conn, fn tx ->
             with {:ok, seq} <- insert_op(tx, op),
                  :ok <- project(tx, op, seq) do
               seq
             else
               {:error, reason} -> Postgrex.rollback(tx, reason)
             end
           end) do
        {:ok, seq} -> {:ok, seq}
        {:error, reason} -> {:error, reason}
      end
    end
  end

  defp insert_op(conn, %Op{} = op) do
    sql = """
    INSERT INTO ops (ts, tenant, principal, workload, task_id, session_id, serving_instance_id, stateful_instance_id, group_instance_id, kind, payload_blob)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
    RETURNING seq
    """

    params = [
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
      encode_payload(op.payload)
    ]

    with {:ok, %Postgrex.Result{rows: [[seq]]}} <- Postgrex.query(conn, sql, params) do
      {:ok, seq}
    end
  end

  # Write-through projection: applies the effect of one op onto the mutable
  # tasks/results tables, mirroring Embervm.OpLog.SQLite.project/3 exactly
  # (same kinds, same column effects); only the parameter/upsert syntax differs.
  defp project(conn, %Op{kind: :submitted} = op, _seq) do
    idempotency_key = Map.get(op.payload, :idempotency_key)
    expires_at = Map.get(op.payload, :expires_at)

    case existing_task_for_idempotency_key(conn, op.workload, idempotency_key) do
      {:ok, nil} ->
        sql = """
        INSERT INTO tasks
          (task_id, tenant, principal, workload, state, attempt, idempotency_key, submitted_at, updated_at, expires_at)
        VALUES ($1, $2, $3, $4, 'queued', 0, $5, $6, $7, $8)
        """

        exec(conn, sql, [
          op.task_id,
          op.tenant,
          op.principal,
          op.workload,
          idempotency_key,
          op.ts,
          op.ts,
          expires_at
        ])

      {:ok, existing_task_id} ->
        {:error, {:duplicate_idempotency_key, existing_task_id}}
    end
  end

  # Monotonic advance for the deferred async lifecycle appends (ADR embervm/014
  # decision 2), mirroring Embervm.OpLog.SQLite: SET only from a strictly-lower-
  # ranked state (Embervm.TaskState.states_below/1), so a late/out-of-order async
  # :assigned/:started append cannot regress a row that already ran or terminalized,
  # while a legitimate forward jump still applies. Inert under the gate off. See the
  # SQLite backend for the full rationale.
  defp project(conn, %Op{kind: :assigned} = op, _seq) do
    advance_task_state(conn, op.task_id, :assigned, op.ts, epoch_of(op))
  end

  defp project(conn, %Op{kind: :started} = op, _seq) do
    advance_task_state(conn, op.task_id, :running, op.ts, epoch_of(op))
  end

  defp project(conn, %Op{kind: :succeeded} = op, _seq) do
    with :ok <- update_task_state(conn, op.task_id, "succeeded", op.ts) do
      sql = """
      INSERT INTO results
        (task_id, status_code, body, size_bytes, truncated, created_at, expires_at, headers)
      VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
      ON CONFLICT(task_id) DO UPDATE SET
        status_code = excluded.status_code,
        body = excluded.body,
        size_bytes = excluded.size_bytes,
        truncated = excluded.truncated,
        created_at = excluded.created_at,
        expires_at = excluded.expires_at,
        headers = excluded.headers
      """

      payload = op.payload

      with :ok <-
             exec(conn, sql, [
               op.task_id,
               Map.fetch!(payload, :status_code),
               Map.get(payload, :body),
               Map.fetch!(payload, :size_bytes),
               bool_to_int(Map.get(payload, :truncated, false)),
               op.ts,
               Map.get(payload, :expires_at),
               encode_headers(Map.get(payload, :headers, %{}))
             ]) do
        project_usage(conn, op)
      end
    end
  end

  defp project(conn, %Op{kind: :failed} = op, _seq) do
    state = Map.fetch!(op.payload, :state)

    with :ok <- update_task_state(conn, op.task_id, to_string(state), op.ts) do
      project_usage(conn, op)
    end
  end

  defp project(conn, %Op{kind: :retried} = op, _seq) do
    exec(conn, "UPDATE tasks SET state='queued', attempt=attempt+1, updated_at=$1 WHERE task_id=$2", [
      op.ts,
      op.task_id
    ])
  end

  defp project(conn, %Op{kind: :dead_lettered} = op, _seq) do
    update_task_state(conn, op.task_id, "dead_lettered", op.ts)
  end

  defp project(conn, %Op{kind: :redrive} = op, _seq) do
    exec(conn, "UPDATE tasks SET state='queued', attempt=0, updated_at=$1 WHERE task_id=$2", [
      op.ts,
      op.task_id
    ])
  end

  # -- session projection (R2), mirrors Embervm.OpLog.SQLite exactly ---------

  defp project(conn, %Op{kind: :session_created} = op, _seq) do
    payload = op.payload

    # ON CONFLICT DO NOTHING: idempotent on session_id so the adopt-and-backfill
    # repair can re-append session_created for a lost async write without a clash
    # (mirrors the SQLite backend's INSERT OR IGNORE), ADR embervm/014 decision 2.
    sql = """
    INSERT INTO sessions
      (session_id, tenant, principal, workload, state, node_id, volume_node_id,
       base_snapshot_ref, base_digest, generation, snapshot_ref, snapshot_size_bytes,
       token_sha256, created_at, last_invoke_at, expires_at, updated_at, terminal_reason,
       lineage_id)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 0, NULL, NULL, $10, $11, NULL, $12, $13, NULL, $14)
    ON CONFLICT (session_id) DO NOTHING
    """

    exec(conn, sql, [
      op.session_id,
      op.tenant,
      op.principal,
      op.workload,
      Map.get(payload, :state, "running"),
      Map.get(payload, :node_id),
      Map.get(payload, :volume_node_id),
      Map.get(payload, :base_snapshot_ref),
      Map.get(payload, :base_digest),
      Map.get(payload, :token_sha256),
      op.ts,
      Map.get(payload, :expires_at),
      op.ts,
      # #4306 slice 1: do_create always sends lineage_id now (= session_id this
      # slice); the default here is belt-and-suspenders for an op written by
      # code that predates this field.
      Map.get(payload, :lineage_id, op.session_id)
    ])
  end

  defp project(conn, %Op{kind: :session_invoked} = op, _seq) do
    with :ok <-
           exec(conn, "UPDATE sessions SET last_invoke_at=$1, updated_at=$2 WHERE session_id=$3", [
             op.ts,
             op.ts,
             op.session_id
           ]) do
      project_usage(conn, op)
    end
  end

  defp project(conn, %Op{kind: :session_banked} = op, _seq) do
    payload = op.payload

    sql = """
    UPDATE sessions
    SET state='banked', snapshot_ref=$1, snapshot_size_bytes=$2, generation=$3, updated_at=$4
    WHERE session_id=$5
    """

    exec(conn, sql, [
      Map.get(payload, :snapshot_ref),
      Map.get(payload, :size_bytes),
      Map.get(payload, :generation, 0),
      op.ts,
      op.session_id
    ])
  end

  defp project(conn, %Op{kind: :session_parked} = op, _seq) do
    exec(conn, "UPDATE sessions SET state='parked', volume_node_id=$1, node_id=NULL, updated_at=$2 WHERE session_id=$3", [
      Map.get(op.payload, :volume_node_id),
      op.ts,
      op.session_id
    ])
  end

  defp project(conn, %Op{kind: :session_parking} = op, _seq) do
    exec(conn, "UPDATE sessions SET state='parking', volume_node_id=$1, updated_at=$2 WHERE session_id=$3", [
      Map.get(op.payload, :volume_node_id), op.ts, op.session_id
    ])
  end

  # Guarded against terminal states so a deferred async relit append cannot
  # resurrect a since-destroyed session (mirrors the SQLite backend), ADR
  # embervm/014 decision 2. Inert under the gate off.
  defp project(conn, %Op{kind: :session_relit} = op, _seq) do
    exec(
      conn,
      "UPDATE sessions SET state='running', updated_at=$1 WHERE session_id=$2 AND state NOT IN ('destroyed','expired','evicted','failed')",
      [op.ts, op.session_id]
    )
  end

  # session_destroying: the durable destroy INTENT (ADR embervm/014 decision 5).
  # A non-terminal state marker appended BEFORE the teardown, so a CP crash
  # mid-destroy rebuilds as destroying and re-drives it rather than forgetting.
  # session_destroyed (terminal) is appended only once teardown completes.
  #
  # This clause was missing while the op kind was only ever written under
  # EMBERVM_NODE_CONFIRMED_DESTROY, which has never been armed in prod. The
  # SQLite backend has always had it, so the two backends had drifted. Deferring
  # the teardown made this kind reachable on the ungated path, and the missing
  # clause raised FunctionClauseError inside the append transaction, which
  # cascaded a crash through OpLog, SessionStore and SessionManager on every
  # destroy of a live session.
  defp project(conn, %Op{kind: :session_destroying} = op, _seq) do
    exec(conn, "UPDATE sessions SET state='destroying', updated_at=$1 WHERE session_id=$2", [
      op.ts,
      op.session_id
    ])
  end

  defp project(conn, %Op{kind: :session_expired} = op, _seq),
    do: terminate_session(conn, op, "expired")

  defp project(conn, %Op{kind: :session_evicted} = op, _seq),
    do: terminate_session(conn, op, "evicted")

  defp project(conn, %Op{kind: :session_destroyed} = op, _seq),
    do: terminate_session(conn, op, "destroyed")

  defp project(conn, %Op{kind: :session_failed} = op, _seq),
    do: terminate_session(conn, op, "failed")

  # -- serving instance projection (R3), mirrors Embervm.OpLog.SQLite exactly -

  defp project(conn, %Op{kind: :serving_started} = op, _seq) do
    payload = op.payload

    sql = """
    INSERT INTO serving_instances
      (instance_id, tenant, principal, workload, state, node_id, vm_id, ip, port,
       base_snapshot_ref, base_digest, generation, snapshot_ref, snapshot_size_bytes,
       created_at, last_active_at, updated_at, terminal_reason)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, 0, NULL, NULL, $12, NULL, $13, NULL)
    """

    exec(conn, sql, [
      op.serving_instance_id,
      op.tenant,
      op.principal,
      op.workload,
      Map.get(payload, :state, "starting"),
      Map.get(payload, :node_id),
      Map.get(payload, :vm_id),
      Map.get(payload, :ip),
      Map.get(payload, :port),
      Map.get(payload, :base_snapshot_ref),
      Map.get(payload, :base_digest),
      op.ts,
      op.ts
    ])
  end

  defp project(conn, %Op{kind: :serving_published} = op, _seq) do
    payload = op.payload

    sql = """
    UPDATE serving_instances
    SET state='published', ip=$1, port=$2, updated_at=$3
    WHERE instance_id=$4
    """

    exec(conn, sql, [
      Map.get(payload, :ip),
      Map.get(payload, :port),
      op.ts,
      op.serving_instance_id
    ])
  end

  defp project(conn, %Op{kind: :serving_unpublished} = op, _seq) do
    exec(conn, "UPDATE serving_instances SET state='draining', updated_at=$1 WHERE instance_id=$2", [
      op.ts,
      op.serving_instance_id
    ])
  end

  defp project(conn, %Op{kind: :serving_banked} = op, _seq) do
    payload = op.payload

    sql = """
    UPDATE serving_instances
    SET state='banked', snapshot_ref=$1, snapshot_size_bytes=$2, generation=$3,
        ip=NULL, port=NULL, updated_at=$4
    WHERE instance_id=$5
    """

    exec(conn, sql, [
      Map.get(payload, :snapshot_ref),
      Map.get(payload, :size_bytes),
      Map.get(payload, :generation, 0),
      op.ts,
      op.serving_instance_id
    ])
  end

  defp project(conn, %Op{kind: :serving_relit} = op, _seq) do
    payload = op.payload

    sql = """
    UPDATE serving_instances
    SET state='starting', node_id=$1, vm_id=$2, updated_at=$3
    WHERE instance_id=$4
    """

    exec(conn, sql, [
      Map.get(payload, :node_id),
      Map.get(payload, :vm_id),
      op.ts,
      op.serving_instance_id
    ])
  end

  defp project(conn, %Op{kind: :serving_evicted} = op, _seq),
    do: terminate_serving(conn, op, "evicted")

  defp project(conn, %Op{kind: :serving_destroyed} = op, _seq),
    do: terminate_serving(conn, op, "destroyed")

  defp project(conn, %Op{kind: :serving_failed} = op, _seq),
    do: terminate_serving(conn, op, "failed")

  defp project(conn, %Op{kind: :serving_stats} = op, _seq) do
    project_usage_serving(conn, op)
  end

  # -- stateful projections (R4), mirrors Embervm.OpLog.SQLite exactly -------

  defp project(conn, %Op{kind: :volume_created} = op, _seq) do
    payload = op.payload

    sql = """
    INSERT INTO volumes (workload, node_id, generation, size_bytes, allocated_bytes, created_at, updated_at)
    VALUES ($1, $2, $3, $4, $5, $6, $7)
    ON CONFLICT(workload) DO UPDATE SET
      node_id = excluded.node_id,
      generation = excluded.generation,
      size_bytes = excluded.size_bytes,
      allocated_bytes = excluded.allocated_bytes,
      updated_at = excluded.updated_at
    """

    exec(conn, sql, [
      op.workload,
      Map.get(payload, :node_id),
      Map.get(payload, :generation, 0),
      Map.get(payload, :size_bytes),
      Map.get(payload, :allocated_bytes),
      op.ts,
      op.ts
    ])
  end

  defp project(conn, %Op{kind: :volume_deleted} = op, _seq) do
    exec(conn, "DELETE FROM volumes WHERE workload=$1", [op.workload])
  end

  defp project(conn, %Op{kind: :generation_blessed} = op, _seq) do
    payload = op.payload

    sql = """
    INSERT INTO volume_blessing (workload, blessed_generation, created_at, updated_at)
    VALUES ($1, $2, $3, $4)
    ON CONFLICT(workload) DO UPDATE SET
      blessed_generation = excluded.blessed_generation,
      updated_at = excluded.updated_at
    """

    exec(conn, sql, [
      op.workload,
      Map.get(payload, :generation),
      op.ts,
      op.ts
    ])
  end

  defp project(conn, %Op{kind: :blessing_lease_granted} = op, _seq) do
    payload = op.payload
    sql = """
    INSERT INTO blessing_lease (workload, node_id, next_generation, lease_end, created_at, updated_at)
    VALUES ($1, $2, $3, $4, $5, $6)
    ON CONFLICT(workload, node_id) DO UPDATE SET
      next_generation = excluded.next_generation,
      lease_end = excluded.lease_end,
      updated_at = excluded.updated_at
    """
    exec(conn, sql, [op.workload, Map.get(payload, :node_id), Map.get(payload, :next_generation), Map.get(payload, :lease_end), op.ts, op.ts])
  end

  # checkpoint_dispatched (R7, ADR embervm/017): upsert the one-per-workload
  # in-flight checkpoint record {vm_id, generation}; SQLite-backend parity.
  defp project(conn, %Op{kind: :checkpoint_dispatched} = op, _seq) do
    payload = op.payload

    sql = """
    INSERT INTO checkpoint_dispatch (workload, vm_id, generation, created_at, updated_at)
    VALUES ($1, $2, $3, $4, $5)
    ON CONFLICT(workload) DO UPDATE SET
      vm_id = excluded.vm_id,
      generation = excluded.generation,
      updated_at = excluded.updated_at
    """

    exec(conn, sql, [
      op.workload,
      Map.get(payload, :vm_id),
      Map.get(payload, :generation),
      op.ts,
      op.ts
    ])
  end

  # checkpoint_resolved (R7, ADR embervm/017): drop the in-flight row when the
  # control plane drove the resolve or auto-heal consumed it. Idempotent.
  defp project(conn, %Op{kind: :checkpoint_resolved} = op, _seq) do
    exec(conn, "DELETE FROM checkpoint_dispatch WHERE workload=$1", [op.workload])
  end

  defp project(conn, %Op{kind: :stateful_started} = op, _seq) do
    with :ok <- insert_stateful_instance(conn, op, "starting") do
      bump_volume_generation(conn, op)
    end
  end

  defp project(conn, %Op{kind: :stateful_cold_booted} = op, _seq) do
    with :ok <- insert_stateful_instance(conn, op, "starting") do
      bump_volume_generation(conn, op)
    end
  end

  defp project(conn, %Op{kind: :stateful_published} = op, _seq) do
    payload = op.payload

    exec(
      conn,
      "UPDATE stateful_instances SET state='serving', ip=$1, port=$2, updated_at=$3 WHERE instance_id=$4",
      [Map.get(payload, :ip), Map.get(payload, :port), op.ts, op.stateful_instance_id]
    )
  end

  defp project(conn, %Op{kind: :stateful_unpublished} = op, _seq) do
    exec(conn, "UPDATE stateful_instances SET updated_at=$1 WHERE instance_id=$2", [
      op.ts,
      op.stateful_instance_id
    ])
  end

  defp project(conn, %Op{kind: :stateful_banked} = op, _seq) do
    payload = op.payload

    sql = """
    UPDATE stateful_instances
    SET state='banked', snapshot_ref=$1, snapshot_generation=$2, snapshot_size_bytes=$3,
        ip=NULL, port=NULL, updated_at=$4
    WHERE instance_id=$5
    """

    exec(conn, sql, [
      Map.get(payload, :snapshot_ref),
      Map.get(payload, :generation),
      Map.get(payload, :size_bytes),
      op.ts,
      op.stateful_instance_id
    ])
  end

  defp project(conn, %Op{kind: :stateful_relit} = op, _seq) do
    payload = op.payload

    sql = """
    UPDATE stateful_instances
    SET state='starting', node_id=$1, vm_id=$2, generation=$3,
        snapshot_ref=NULL, snapshot_generation=NULL, updated_at=$4
    WHERE instance_id=$5
    """

    with :ok <-
           exec(conn, sql, [
             Map.get(payload, :node_id),
             Map.get(payload, :vm_id),
             Map.get(payload, :generation, 0),
             op.ts,
             op.stateful_instance_id
           ]) do
      bump_volume_generation(conn, op)
    end
  end

  defp project(conn, %Op{kind: :stateful_evicted} = op, _seq),
    do: terminate_stateful(conn, op, "evicted")

  defp project(conn, %Op{kind: :stateful_destroyed} = op, _seq),
    do: terminate_stateful(conn, op, "destroyed")

  defp project(conn, %Op{kind: :stateful_failed} = op, _seq),
    do: terminate_stateful(conn, op, "failed")

  defp project(conn, %Op{kind: :stateful_stats} = op, _seq) do
    project_usage_stateful(conn, op)
  end

  # -- composite-group projection (R5), mirrors Embervm.OpLog.SQLite exactly -

  defp project(conn, %Op{kind: :group_created} = op, _seq) do
    payload = op.payload

    sql = """
    INSERT INTO group_instances
      (instance_id, tenant, principal, workload, state, node_id, subnet_cidr,
       entry_member, entry_port, listen_port, set_id,
       created_at, last_active_at, updated_at, terminal_reason)
    VALUES ($1, $2, $3, $4, $5, $6, NULL, $7, $8, $9, NULL, $10, NULL, $11, NULL)
    """

    exec(conn, sql, [
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
    ])
  end

  defp project(conn, %Op{kind: :group_net_created} = op, _seq) do
    exec(conn, "UPDATE group_instances SET subnet_cidr=$1, updated_at=$2 WHERE instance_id=$3", [
      Map.get(op.payload, :subnet_cidr),
      op.ts,
      op.group_instance_id
    ])
  end

  defp project(conn, %Op{kind: :group_net_deleted} = op, _seq) do
    exec(conn, "UPDATE group_instances SET subnet_cidr=NULL, updated_at=$1 WHERE instance_id=$2", [
      op.ts,
      op.group_instance_id
    ])
  end

  defp project(conn, %Op{kind: :group_member_started} = op, _seq) do
    upsert_group_member(conn, op, "starting", healthy: false, clear_snapshot: true)
  end

  defp project(conn, %Op{kind: :group_running} = op, _seq) do
    with :ok <- set_group_state(conn, op, "running", last_active: true) do
      mark_all_members_healthy(conn, op.group_instance_id, true, op.ts)
    end
  end

  defp project(conn, %Op{kind: :group_published} = op, _seq) do
    sql = "UPDATE group_instances SET listen_port=COALESCE($1, listen_port), updated_at=$2 WHERE instance_id=$3"

    exec(conn, sql, [
      Map.get(op.payload, :listen_port),
      op.ts,
      op.group_instance_id
    ])
  end

  defp project(conn, %Op{kind: :group_unpublished} = op, _seq) do
    set_group_state(conn, op, "banking")
  end

  defp project(conn, %Op{kind: :group_banked} = op, _seq) do
    payload = op.payload

    sql = """
    UPDATE group_instances
    SET state='banked', set_id=$1, updated_at=$2
    WHERE instance_id=$3
    """

    with :ok <- exec(conn, sql, [Map.get(payload, :set_id), op.ts, op.group_instance_id]),
         :ok <- bank_group_members(conn, op) do
      :ok
    end
  end

  defp project(conn, %Op{kind: :group_relit} = op, _seq) do
    set_group_state(conn, op, "starting", last_active: true)
  end

  defp project(conn, %Op{kind: :group_fresh_booted} = op, _seq) do
    sql = """
    UPDATE group_instances
    SET state='starting', set_id=NULL, updated_at=$1
    WHERE instance_id=$2
    """

    exec(conn, sql, [op.ts, op.group_instance_id])
  end

  defp project(conn, %Op{kind: :group_set_evicted} = op, _seq) do
    exec(conn, "UPDATE group_instances SET set_id=NULL, updated_at=$1 WHERE instance_id=$2", [
      op.ts,
      op.group_instance_id
    ])
  end

  defp project(conn, %Op{kind: :group_degraded} = op, _seq) do
    with :ok <- set_group_state(conn, op, "degraded") do
      case Map.get(op.payload, :member_name) do
        nil -> :ok
        member -> set_member_health(conn, op.group_instance_id, member, false, op.ts)
      end
    end
  end

  defp project(conn, %Op{kind: :group_destroyed} = op, _seq),
    do: terminate_group(conn, op, "destroyed")

  defp project(conn, %Op{kind: :group_failed} = op, _seq),
    do: terminate_group(conn, op, "failed")

  defp project(conn, %Op{kind: :group_stats} = op, _seq) do
    project_usage_group(conn, op)
  end

  # Audit-only kinds: no task/result projection, mirroring Embervm.OpLog.SQLite.
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

  defp terminate_session(conn, %Op{} = op, state) do
    reason = to_string(Map.get(op.payload, :reason, state))

    exec(conn, "UPDATE sessions SET state=$1, terminal_reason=$2, updated_at=$3 WHERE session_id=$4", [
      state,
      reason,
      op.ts,
      op.session_id
    ])
  end

  defp terminate_serving(conn, %Op{} = op, state) do
    reason = to_string(Map.get(op.payload, :reason, state))

    exec(
      conn,
      "UPDATE serving_instances SET state=$1, terminal_reason=$2, updated_at=$3 WHERE instance_id=$4",
      [state, reason, op.ts, op.serving_instance_id]
    )
  end

  defp insert_stateful_instance(conn, %Op{} = op, state) do
    payload = op.payload

    sql = """
    INSERT INTO stateful_instances
      (instance_id, tenant, principal, workload, state, node_id, vm_id, ip, port,
       generation, snapshot_ref, snapshot_generation, snapshot_size_bytes,
       created_at, last_active_at, updated_at, terminal_reason)
    VALUES ($1, $2, $3, $4, $5, $6, $7, NULL, NULL, $8, NULL, NULL, NULL, $9, NULL, $10, NULL)
    """

    exec(conn, sql, [
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
    ])
  end

  defp bump_volume_generation(conn, %Op{} = op) do
    case Map.get(op.payload, :generation) do
      nil ->
        :ok

      generation ->
        exec(conn, "UPDATE volumes SET generation=$1, updated_at=$2 WHERE workload=$3", [
          generation,
          op.ts,
          op.workload
        ])
    end
  end

  defp terminate_stateful(conn, %Op{} = op, state) do
    reason = to_string(Map.get(op.payload, :reason, state))

    exec(
      conn,
      "UPDATE stateful_instances SET state=$1, terminal_reason=$2, updated_at=$3 WHERE instance_id=$4",
      [state, reason, op.ts, op.stateful_instance_id]
    )
  end

  # -- composite-group projection helpers (R5) ------------------------------

  defp set_group_state(conn, %Op{} = op, state, opts \\ []) do
    if Keyword.get(opts, :last_active, false) do
      exec(
        conn,
        "UPDATE group_instances SET state=$1, last_active_at=$2, updated_at=$3 WHERE instance_id=$4",
        [state, op.ts, op.ts, op.group_instance_id]
      )
    else
      exec(conn, "UPDATE group_instances SET state=$1, updated_at=$2 WHERE instance_id=$3", [
        state,
        op.ts,
        op.group_instance_id
      ])
    end
  end

  defp terminate_group(conn, %Op{} = op, state) do
    reason = to_string(Map.get(op.payload, :reason, state))

    exec(conn, "UPDATE group_instances SET state=$1, terminal_reason=$2, updated_at=$3 WHERE instance_id=$4", [
      state,
      reason,
      op.ts,
      op.group_instance_id
    ])
  end

  defp upsert_group_member(conn, %Op{} = op, state, opts) do
    payload = op.payload
    healthy = if Keyword.get(opts, :healthy, false), do: 1, else: 0
    clear_snapshot = Keyword.get(opts, :clear_snapshot, false)

    snapshot_set = if clear_snapshot, do: "NULL", else: "group_members.snapshot_ref"

    sql = """
    INSERT INTO group_members
      (instance_id, member_name, member_index, vm_id, ip, state, snapshot_ref, healthy, updated_at)
    VALUES ($1, $2, $3, $4, $5, $6, NULL, $7, $8)
    ON CONFLICT(instance_id, member_name) DO UPDATE SET
      member_index = excluded.member_index,
      vm_id = excluded.vm_id,
      ip = excluded.ip,
      state = excluded.state,
      snapshot_ref = #{snapshot_set},
      healthy = excluded.healthy,
      updated_at = excluded.updated_at
    """

    exec(conn, sql, [
      op.group_instance_id,
      Map.get(payload, :member_name),
      Map.get(payload, :member_index),
      Map.get(payload, :vm_id),
      Map.get(payload, :ip),
      state,
      healthy,
      op.ts
    ])
  end

  defp mark_all_members_healthy(conn, instance_id, healthy, ts) do
    flag = if healthy, do: 1, else: 0

    exec(conn, "UPDATE group_members SET healthy=$1, updated_at=$2 WHERE instance_id=$3", [
      flag,
      ts,
      instance_id
    ])
  end

  defp set_member_health(conn, instance_id, member_name, healthy, ts) do
    flag = if healthy, do: 1, else: 0

    exec(
      conn,
      "UPDATE group_members SET healthy=$1, updated_at=$2 WHERE instance_id=$3 AND member_name=$4",
      [flag, ts, instance_id, member_name]
    )
  end

  defp bank_group_members(conn, %Op{} = op) do
    case Map.get(op.payload, :members) do
      members when is_list(members) ->
        Enum.reduce_while(members, :ok, fn member, :ok ->
          name = member_field(member, :name)
          snapshot_ref = member_field(member, :snapshot_ref)

          sql = """
          UPDATE group_members
          SET snapshot_ref=$1, vm_id=NULL, ip=NULL, state='banked', updated_at=$2
          WHERE instance_id=$3 AND member_name=$4
          """

          case exec(conn, sql, [snapshot_ref, op.ts, op.group_instance_id, name]) do
            :ok -> {:cont, :ok}
            {:error, _} = err -> {:halt, err}
          end
        end)

      _ ->
        :ok
    end
  end

  defp member_field(member, key) when is_map(member) do
    Map.get(member, key) || Map.get(member, Atom.to_string(key))
  end

  defp member_field(_member, _key), do: nil

  defp update_task_state(conn, task_id, state, ts) do
    exec(conn, "UPDATE tasks SET state=$1, updated_at=$2 WHERE task_id=$3", [state, ts, task_id])
  end

  # Monotonic advance for a deferred async lifecycle append: SET `to_state` only
  # when the row ranks strictly below it in the lifecycle partial order AND the
  # row's retry count is still below the op's issue-time attempt (see
  # Embervm.OpLog.SQLite.advance_task_state/5 for the full attempt-aliasing
  # rationale and Embervm.TaskState.states_below/1 for the rank order). `epoch` nil
  # (sync write-through, or a pre-epoch op) runs only the rank guard, unchanged.
  defp advance_task_state(conn, task_id, to_state, ts, epoch \\ nil) do
    below = Embervm.TaskState.states_below(to_state)
    # $1 to_state, $2 ts, $3 task_id, then $4.. for the state-in list.
    placeholders = below |> Enum.with_index(4) |> Enum.map_join(",", fn {_, i} -> "$#{i}" end)

    {epoch_clause, epoch_args} =
      case epoch do
        e when is_integer(e) -> {" AND attempt < $#{4 + length(below)}", [e]}
        _ -> {"", []}
      end

    exec(
      conn,
      "UPDATE tasks SET state=$1, updated_at=$2 WHERE task_id=$3 AND state IN (#{placeholders})" <>
        epoch_clause,
      [Atom.to_string(to_state), ts, task_id | below] ++ epoch_args
    )
  end

  # The dispatch attempt (1-based) a deferred async :assigned/:started op was issued
  # under, from the op payload (atom key on a fresh append, string key if ever
  # re-projected from durable JSON). nil when absent. Mirrors Embervm.OpLog.SQLite.
  defp epoch_of(%Op{payload: payload}) when is_map(payload) do
    Map.get(payload, :epoch) || Map.get(payload, "epoch")
  end

  defp epoch_of(_op), do: nil

  # -- usage accrual, mirrors Embervm.OpLog.SQLite exactly -------------------

  defp project_usage(conn, %Op{payload: payload} = op) do
    case Map.get(payload, :usage) do
      usage when is_map(usage) ->
        sql = """
        INSERT INTO usage (principal, day, tenant, vcpu_seconds, gb_seconds, task_count, updated_at)
        VALUES ($1, $2, $3, $4, $5, 1, $6)
        ON CONFLICT(principal, day) DO UPDATE SET
          vcpu_seconds = usage.vcpu_seconds + excluded.vcpu_seconds,
          gb_seconds = usage.gb_seconds + excluded.gb_seconds,
          task_count = usage.task_count + 1,
          updated_at = excluded.updated_at
        """

        exec(conn, sql, [
          op.principal,
          div(op.ts, @day_ms),
          op.tenant,
          to_float(Map.get(usage, :vcpu_seconds, 0)),
          to_float(Map.get(usage, :gb_seconds, 0)),
          op.ts
        ])

      _ ->
        :ok
    end
  end

  defp project_usage_serving(_conn, %Op{principal: nil}), do: :ok

  defp project_usage_serving(conn, %Op{payload: payload} = op) do
    rq_delta = Map.get(payload, :rq_delta, 0)

    sql = """
    INSERT INTO usage (principal, day, tenant, vcpu_seconds, gb_seconds, task_count, request_count, updated_at)
    VALUES ($1, $2, $3, 0, 0, 0, $4, $5)
    ON CONFLICT(principal, day) DO UPDATE SET
      request_count = usage.request_count + excluded.request_count,
      updated_at = excluded.updated_at
    """

    exec(conn, sql, [op.principal, div(op.ts, @day_ms), op.tenant, rq_delta, op.ts])
  end

  defp project_usage_stateful(_conn, %Op{principal: nil}), do: :ok

  defp project_usage_stateful(conn, %Op{payload: payload} = op) do
    cx_delta = Map.get(payload, :cx_delta, 0)

    sql = """
    INSERT INTO usage (principal, day, tenant, vcpu_seconds, gb_seconds, task_count, request_count, updated_at)
    VALUES ($1, $2, $3, 0, 0, 0, $4, $5)
    ON CONFLICT(principal, day) DO UPDATE SET
      request_count = usage.request_count + excluded.request_count,
      updated_at = excluded.updated_at
    """

    exec(conn, sql, [op.principal, div(op.ts, @day_ms), op.tenant, cx_delta, op.ts])
  end

  defp project_usage_group(_conn, %Op{principal: nil}), do: :ok

  defp project_usage_group(conn, %Op{payload: payload} = op) do
    case Map.get(payload, :usage) do
      usage when is_map(usage) ->
        member_count = to_pos_int(Map.get(payload, :member_count), 1)
        vcpu_seconds = to_float(Map.get(usage, :vcpu_seconds, 0)) * member_count
        gb_seconds = to_float(Map.get(usage, :gb_seconds, 0)) * member_count

        sql = """
        INSERT INTO usage (principal, day, tenant, vcpu_seconds, gb_seconds, task_count, request_count, updated_at)
        VALUES ($1, $2, $3, $4, $5, 0, 0, $6)
        ON CONFLICT(principal, day) DO UPDATE SET
          vcpu_seconds = usage.vcpu_seconds + excluded.vcpu_seconds,
          gb_seconds = usage.gb_seconds + excluded.gb_seconds,
          updated_at = excluded.updated_at
        """

        exec(conn, sql, [
          op.principal,
          div(op.ts, @day_ms),
          op.tenant,
          vcpu_seconds,
          gb_seconds,
          op.ts
        ])

      _ ->
        :ok
    end
  end

  defp to_float(n) when is_number(n), do: n * 1.0
  defp to_float(_), do: 0.0

  defp to_pos_int(n, _default) when is_integer(n) and n > 0, do: n
  defp to_pos_int(_n, default), do: default

  defp existing_task_for_idempotency_key(_conn, _workload, nil), do: {:ok, nil}

  defp existing_task_for_idempotency_key(conn, workload, idempotency_key) do
    sql = "SELECT task_id FROM tasks WHERE workload=$1 AND idempotency_key=$2"

    case Postgrex.query(conn, sql, [workload, idempotency_key]) do
      {:ok, %Postgrex.Result{rows: [[task_id]]}} -> {:ok, task_id}
      {:ok, %Postgrex.Result{rows: []}} -> {:ok, nil}
      {:error, reason} -> {:error, reason}
    end
  end

  # -- reads -----------------------------------------------------------------

  defp do_read_from(conn, seq) do
    marker = read_marker(conn)

    if seq < marker do
      {:error, {:compacted, marker}}
    else
      sql = """
      SELECT seq, ts, tenant, principal, workload, task_id, session_id, serving_instance_id, stateful_instance_id, group_instance_id, kind, payload_blob
      FROM ops WHERE seq > $1 ORDER BY seq ASC
      """

      case Postgrex.query(conn, sql, [seq]) do
        {:ok, %Postgrex.Result{rows: rows}} -> {:ok, Enum.map(rows, &row_to_op/1)}
        {:error, reason} -> {:error, reason}
      end
    end
  end

  defp row_to_op([
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
         payload_blob
       ]) do
    %Op{
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
      payload: decode_payload(payload_blob)
    }
  end

  defp do_load_tasks(conn) do
    sql = """
    SELECT task_id, tenant, principal, workload, state, attempt, idempotency_key, submitted_at, updated_at, expires_at
    FROM tasks
    """

    case Postgrex.query(conn, sql, []) do
      {:ok, %Postgrex.Result{rows: rows}} -> {:ok, Enum.map(rows, &row_to_task/1)}
      {:error, reason} -> {:error, reason}
    end
  end

  defp row_to_task([
         task_id,
         tenant,
         principal,
         workload,
         state,
         attempt,
         idempotency_key,
         submitted_at,
         updated_at,
         expires_at
       ]) do
    %{
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
  end

  defp do_load_sessions(conn) do
    sql = """
    SELECT session_id, tenant, principal, workload, state, node_id, volume_node_id,
           base_snapshot_ref, base_digest, generation, snapshot_ref, snapshot_size_bytes,
           token_sha256, created_at, last_invoke_at, expires_at, updated_at, terminal_reason,
           COALESCE(lineage_id, session_id)
    FROM sessions
    """

    case Postgrex.query(conn, sql, []) do
      {:ok, %Postgrex.Result{rows: rows}} -> {:ok, Enum.map(rows, &row_to_session/1)}
      {:error, reason} -> {:error, reason}
    end
  end

  defp row_to_session([
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
         terminal_reason,
         lineage_id
       ]) do
    %{
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
      terminal_reason: terminal_reason,
      lineage_id: lineage_id
    }
  end

  defp do_load_serving_instances(conn) do
    sql = """
    SELECT instance_id, tenant, principal, workload, state, node_id, vm_id, ip, port,
           base_snapshot_ref, base_digest, generation, snapshot_ref, snapshot_size_bytes,
           created_at, last_active_at, updated_at, terminal_reason
    FROM serving_instances
    """

    case Postgrex.query(conn, sql, []) do
      {:ok, %Postgrex.Result{rows: rows}} -> {:ok, Enum.map(rows, &row_to_serving_instance/1)}
      {:error, reason} -> {:error, reason}
    end
  end

  defp row_to_serving_instance([
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
       ]) do
    %{
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
  end

  defp do_load_stateful_instances(conn) do
    sql = """
    SELECT instance_id, tenant, principal, workload, state, node_id, vm_id, ip, port,
           generation, snapshot_ref, snapshot_generation, snapshot_size_bytes,
           created_at, last_active_at, updated_at, terminal_reason
    FROM stateful_instances
    """

    case Postgrex.query(conn, sql, []) do
      {:ok, %Postgrex.Result{rows: rows}} -> {:ok, Enum.map(rows, &row_to_stateful_instance/1)}
      {:error, reason} -> {:error, reason}
    end
  end

  defp row_to_stateful_instance([
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
       ]) do
    %{
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
  end

  defp do_load_volumes(conn) do
    sql = """
    SELECT workload, node_id, generation, size_bytes, allocated_bytes, created_at, updated_at
    FROM volumes
    """

    case Postgrex.query(conn, sql, []) do
      {:ok, %Postgrex.Result{rows: rows}} ->
        {:ok,
         Enum.map(rows, fn [workload, node_id, generation, size_bytes, allocated_bytes, created_at, updated_at] ->
           %{
             workload: workload,
             node_id: node_id,
             generation: generation,
             size_bytes: size_bytes,
             allocated_bytes: allocated_bytes,
             created_at: created_at,
             updated_at: updated_at
           }
         end)}

      {:error, reason} ->
        {:error, reason}
    end
  end

  defp do_load_volume_blessing(conn) do
    sql = """
    SELECT workload, blessed_generation, created_at, updated_at
    FROM volume_blessing
    """

    case Postgrex.query(conn, sql, []) do
      {:ok, %Postgrex.Result{rows: rows}} ->
        {:ok,
         Enum.map(rows, fn [workload, blessed_generation, created_at, updated_at] ->
           %{
             workload: workload,
             blessed_generation: blessed_generation,
             created_at: created_at,
             updated_at: updated_at
           }
         end)}

      {:error, reason} ->
        {:error, reason}
    end
  end

  defp do_load_blessing_leases(conn) do
    sql = "SELECT workload, node_id, next_generation, lease_end, created_at, updated_at FROM blessing_lease"
    case Postgrex.query(conn, sql, []) do
      {:ok, %Postgrex.Result{rows: rows}} ->
        {:ok, Enum.map(rows, fn [workload, node_id, next_generation, lease_end, created_at, updated_at] -> %{workload: workload, node_id: node_id, next_generation: next_generation, lease_end: lease_end, created_at: created_at, updated_at: updated_at} end)}
      {:error, reason} -> {:error, reason}
    end
  end

  defp do_load_checkpoint_dispatches(conn) do
    sql = """
    SELECT workload, vm_id, generation
    FROM checkpoint_dispatch
    """

    case Postgrex.query(conn, sql, []) do
      {:ok, %Postgrex.Result{rows: rows}} ->
        {:ok,
         Enum.map(rows, fn [workload, vm_id, generation] ->
           %{workload: workload, vm_id: vm_id, generation: generation}
         end)}

      {:error, reason} ->
        {:error, reason}
    end
  end

  defp do_load_group_instances(conn) do
    sql = """
    SELECT instance_id, tenant, principal, workload, state, node_id, subnet_cidr,
           entry_member, entry_port, listen_port, set_id,
           created_at, last_active_at, updated_at, terminal_reason
    FROM group_instances
    """

    case Postgrex.query(conn, sql, []) do
      {:ok, %Postgrex.Result{rows: rows}} -> {:ok, Enum.map(rows, &row_to_group_instance/1)}
      {:error, reason} -> {:error, reason}
    end
  end

  defp row_to_group_instance([
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
       ]) do
    %{
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
  end

  defp do_load_group_members(conn) do
    sql = """
    SELECT instance_id, member_name, member_index, vm_id, ip, state, snapshot_ref, healthy, updated_at
    FROM group_members
    """

    case Postgrex.query(conn, sql, []) do
      {:ok, %Postgrex.Result{rows: rows}} ->
        {:ok,
         Enum.map(rows, fn [instance_id, member_name, member_index, vm_id, ip, state, snapshot_ref, healthy, updated_at] ->
           %{
             instance_id: instance_id,
             member_name: member_name,
             member_index: member_index,
             vm_id: vm_id,
             ip: ip,
             state: state,
             snapshot_ref: snapshot_ref,
             # Stored as 0/1 (mirroring SQLite's integer flag); surfaced as a bool.
             healthy: healthy == 1,
             updated_at: updated_at
           }
         end)}

      {:error, reason} ->
        {:error, reason}
    end
  end

  defp do_load_result(conn, task_id) do
    sql = """
    SELECT status_code, body, size_bytes, truncated, created_at, expires_at, headers
    FROM results WHERE task_id=$1
    """

    case Postgrex.query(conn, sql, [task_id]) do
      {:ok, %Postgrex.Result{rows: [[status_code, body, size_bytes, truncated, created_at, expires_at, headers]]}} ->
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

      {:ok, %Postgrex.Result{rows: []}} ->
        {:ok, nil}

      {:error, reason} ->
        {:error, reason}
    end
  end

  defp do_load_request(conn, task_id) do
    sql = """
    SELECT payload_blob FROM ops
    WHERE task_id=$1 AND kind='submitted' ORDER BY seq ASC LIMIT 1
    """

    case Postgrex.query(conn, sql, [task_id]) do
      {:ok, %Postgrex.Result{rows: [[payload_blob]]}} ->
        {:ok, Map.get(decode_payload(payload_blob), "request")}

      {:ok, %Postgrex.Result{rows: []}} ->
        {:ok, nil}

      {:error, reason} ->
        {:error, reason}
    end
  end

  defp do_list_usage(conn, opts) do
    since_day = Keyword.get(opts, :since_day, 0)
    principal = Keyword.get(opts, :principal)
    limit = Keyword.get(opts, :limit, 100)
    offset = Keyword.get(opts, :offset, 0)

    {where, filter_params} =
      case principal do
        nil -> {"day >= $1", [since_day]}
        p -> {"day >= $1 AND principal = $2", [since_day, p]}
      end

    limit_sql = if limit == :infinity, do: -1, else: limit
    limit_idx = length(filter_params) + 1
    offset_idx = limit_idx + 1

    sql = """
    SELECT principal, day, tenant, vcpu_seconds, gb_seconds, task_count, request_count, updated_at
    FROM usage WHERE #{where}
    ORDER BY principal ASC, day ASC
    LIMIT $#{limit_idx} OFFSET $#{offset_idx}
    """

    with {:ok, total} <- count_usage(conn, where, filter_params) do
      case Postgrex.query(conn, sql, filter_params ++ [limit_sql, offset]) do
        {:ok, %Postgrex.Result{rows: rows}} ->
          items =
            Enum.map(rows, fn [principal, day, tenant, vcpu_seconds, gb_seconds, task_count, request_count, updated_at] ->
              %{
                principal: principal,
                day: day,
                tenant: tenant,
                vcpu_seconds: vcpu_seconds,
                gb_seconds: gb_seconds,
                task_count: task_count,
                request_count: request_count,
                updated_at: updated_at
              }
            end)

          {:ok, %{items: items, total: total, limit: limit, offset: offset}}

        {:error, reason} ->
          {:error, reason}
      end
    end
  end

  defp count_usage(conn, where, params) do
    case Postgrex.query(conn, "SELECT COUNT(*) FROM usage WHERE #{where}", params) do
      {:ok, %Postgrex.Result{rows: [[n]]}} -> {:ok, n}
      {:error, reason} -> {:error, reason}
    end
  end

  # -- compaction, mirrors Embervm.OpLog.SQLite exactly -----------------------

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

  # Bounded DELETE via a ctid subquery: Postgres has no DELETE ... LIMIT either,
  # so this is the direct analog of Embervm.OpLog.SQLite's rowid-subquery form.
  defp delete_terminal_sessions(conn, now_ms, retention_ms, batch) do
    cutoff = now_ms - retention_ms
    placeholders = placeholder_list(@session_terminal_states, 3)

    sql = """
    DELETE FROM sessions WHERE ctid IN (
      SELECT ctid FROM sessions WHERE state IN (#{placeholders}) AND updated_at < $1 LIMIT $2
    )
    """

    exec_changes(conn, sql, [cutoff, batch] ++ @session_terminal_states)
  end

  defp delete_terminal_serving_instances(conn, now_ms, retention_ms, batch) do
    cutoff = now_ms - retention_ms
    placeholders = placeholder_list(@serving_terminal_states, 3)

    sql = """
    DELETE FROM serving_instances WHERE ctid IN (
      SELECT ctid FROM serving_instances WHERE state IN (#{placeholders}) AND updated_at < $1 LIMIT $2
    )
    """

    exec_changes(conn, sql, [cutoff, batch] ++ @serving_terminal_states)
  end

  defp delete_terminal_stateful_instances(conn, now_ms, retention_ms, batch) do
    cutoff = now_ms - retention_ms
    placeholders = placeholder_list(@stateful_terminal_states, 3)

    sql = """
    DELETE FROM stateful_instances WHERE ctid IN (
      SELECT ctid FROM stateful_instances WHERE state IN (#{placeholders}) AND updated_at < $1 LIMIT $2
    )
    """

    exec_changes(conn, sql, [cutoff, batch] ++ @stateful_terminal_states)
  end

  # Members of the pruned instances are deleted FIRST (same terminal+aged
  # selection), so a member row never outlives its group instance, mirroring
  # Embervm.OpLog.SQLite.delete_terminal_group_instances/4 exactly.
  defp delete_terminal_group_instances(conn, now_ms, retention_ms, batch) do
    cutoff = now_ms - retention_ms
    placeholders = placeholder_list(@group_terminal_states, 3)

    member_sql = """
    DELETE FROM group_members WHERE instance_id IN (
      SELECT instance_id FROM group_instances WHERE state IN (#{placeholders}) AND updated_at < $1 LIMIT $2
    )
    """

    instance_sql = """
    DELETE FROM group_instances WHERE ctid IN (
      SELECT ctid FROM group_instances WHERE state IN (#{placeholders}) AND updated_at < $1 LIMIT $2
    )
    """

    with :ok <- exec(conn, member_sql, [cutoff, batch] ++ @group_terminal_states) do
      exec_changes(conn, instance_sql, [cutoff, batch] ++ @group_terminal_states)
    end
  end

  defp delete_expired_results(conn, now_ms, batch) do
    sql = """
    DELETE FROM results WHERE ctid IN (
      SELECT ctid FROM results WHERE expires_at IS NOT NULL AND expires_at < $1 LIMIT $2
    )
    """

    exec_changes(conn, sql, [now_ms, batch])
  end

  defp delete_terminal_tasks(conn, now_ms, retention_ms, batch) do
    cutoff = now_ms - retention_ms
    placeholders = placeholder_list(@terminal_states, 3)

    sql = """
    DELETE FROM tasks WHERE ctid IN (
      SELECT ctid FROM tasks WHERE state IN (#{placeholders}) AND updated_at < $1 LIMIT $2
    )
    """

    exec_changes(conn, sql, [cutoff, batch] ++ @terminal_states)
  end

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

  defp blocker_seq(conn, cutoff) do
    live_p = placeholder_list(@live_states, 2)
    session_live_p = placeholder_list(@session_live_states, 2 + length(@live_states))
    serving_live_p = placeholder_list(@serving_live_states, 2 + length(@live_states) + length(@session_live_states))

    stateful_live_p =
      placeholder_list(
        @stateful_live_states,
        2 + length(@live_states) + length(@session_live_states) + length(@serving_live_states)
      )

    group_live_p =
      placeholder_list(
        @group_live_states,
        2 + length(@live_states) + length(@session_live_states) + length(@serving_live_states) +
          length(@stateful_live_states)
      )

    sql = """
    SELECT MIN(seq) FROM ops
    WHERE ts >= $1
       OR task_id IN (SELECT task_id FROM tasks WHERE state IN (#{live_p}))
       OR session_id IN (SELECT session_id FROM sessions WHERE state IN (#{session_live_p}))
       OR serving_instance_id IN (
            SELECT instance_id FROM serving_instances WHERE state IN (#{serving_live_p})
          )
       OR stateful_instance_id IN (
            SELECT instance_id FROM stateful_instances WHERE state IN (#{stateful_live_p})
          )
       OR group_instance_id IN (
            SELECT instance_id FROM group_instances WHERE state IN (#{group_live_p})
          )
    """

    params =
      [cutoff] ++
        @live_states ++ @session_live_states ++ @serving_live_states ++ @stateful_live_states ++ @group_live_states

    case Postgrex.query(conn, sql, params) do
      {:ok, %Postgrex.Result{rows: [[seq]]}} -> {:ok, seq}
      {:error, reason} -> {:error, reason}
    end
  end

  defp marker_candidate(_conn, blocker) when is_integer(blocker), do: {:ok, blocker - 1}

  defp marker_candidate(conn, nil) do
    case Postgrex.query(conn, "SELECT MAX(seq) FROM ops", []) do
      {:ok, %Postgrex.Result{rows: [[nil]]}} -> {:ok, 0}
      {:ok, %Postgrex.Result{rows: [[seq]]}} -> {:ok, seq}
      {:error, reason} -> {:error, reason}
    end
  end

  defp delete_ops_prefix(conn, marker, batch) do
    sql = """
    DELETE FROM ops WHERE ctid IN (
      SELECT ctid FROM ops WHERE seq <= $1 LIMIT $2
    )
    """

    exec_changes(conn, sql, [marker, batch])
  end

  defp read_marker(conn) do
    case Postgrex.query(conn, "SELECT value FROM meta WHERE key = $1", [@marker_key]) do
      {:ok, %Postgrex.Result{rows: [[value]]}} -> value
      {:ok, %Postgrex.Result{rows: []}} -> 0
      {:error, _} -> 0
    end
  end

  defp write_marker(conn, value) do
    sql = """
    INSERT INTO meta (key, value) VALUES ($1, $2)
    ON CONFLICT(key) DO UPDATE SET value = excluded.value
    """

    exec(conn, sql, [@marker_key, value])
  end

  defp do_evict_task(conn, task_id) do
    exec(conn, "DELETE FROM tasks WHERE task_id = $1", [task_id])
  end

  # -- ddl ---------------------------------------------------------------

  defp apply_ddl(conn) do
    Enum.reduce_while(@ddl, :ok, fn stmt, :ok ->
      case Postgrex.query(conn, stmt, []) do
        {:ok, _result} -> {:cont, :ok}
        {:error, reason} -> {:halt, {:error, reason}}
      end
    end)
  end

  # -- helpers -------------------------------------------------------------

  # $N placeholders for a fixed-length state list, numbered starting at
  # `start_idx` (the caller's own leading params occupy 1..start_idx-1).
  defp placeholder_list(states, start_idx) do
    states
    |> Enum.with_index(start_idx)
    |> Enum.map(fn {_state, idx} -> "$#{idx}" end)
    |> Enum.join(", ")
  end

  # Runs one statement, discarding its result, mirroring the SQLite adapter's
  # `with ... :done <- Sqlite3.step(...)` shape as a plain :ok/:error.
  defp exec(conn, sql, params) do
    case Postgrex.query(conn, sql, params) do
      {:ok, _result} -> :ok
      {:error, reason} -> {:error, reason}
    end
  end

  # Runs one statement and returns its affected-row count (Postgrex.Result's
  # num_rows), mirroring the SQLite adapter's Sqlite3.changes/1 read.
  defp exec_changes(conn, sql, params) do
    case Postgrex.query(conn, sql, params) do
      {:ok, %Postgrex.Result{num_rows: n}} -> {:ok, n}
      {:error, reason} -> {:error, reason}
    end
  end

  defp bool_to_int(true), do: 1
  defp bool_to_int(false), do: 0
  defp bool_to_int(nil), do: 0

  # -- payload codec ----------------------------------------------------

  # Mirrors Embervm.OpLog.SQLite's ETF codec exactly (see that module's
  # comment): payloads are :erlang.term_to_binary/1'd into BYTEA so any term,
  # including a non-UTF-8 binary body, round-trips byte-exact. Payloads are
  # this node's own trusted data, so binary_to_term/1 on read is safe here.
  defp encode_payload(payload) when is_map(payload) do
    :erlang.term_to_binary(payload)
  end

  defp decode_payload(<<131, _::binary>> = term) do
    term |> :erlang.binary_to_term() |> stringify()
  end

  defp decode_payload(json) when is_binary(json) do
    :json.decode(json)
  end

  defp stringify(map) when is_map(map) do
    for {k, v} <- map, not is_nil(v), into: %{}, do: {to_string(k), stringify(v)}
  end

  defp stringify(list) when is_list(list), do: Enum.map(list, &stringify/1)
  defp stringify(v) when is_atom(v) and not is_boolean(v), do: Atom.to_string(v)
  defp stringify(v), do: v

  defp encode_headers(headers) when is_map(headers) and map_size(headers) == 0, do: nil

  defp encode_headers(headers) when is_map(headers) do
    headers |> :json.encode() |> :erlang.iolist_to_binary()
  end

  defp encode_headers(_), do: nil

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
