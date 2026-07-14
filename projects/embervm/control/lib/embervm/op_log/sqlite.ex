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
      expires_at INTEGER
    )
    """,
    # Metering projection (Task 12): accumulated billed usage per principal per
    # UTC epoch-day. Written by the :succeeded/:failed projection (an accumulating
    # upsert, the ONLY projection that adds rather than overwrites, see project/2),
    # read paged by GET /v1/usage and by Embervm.Metering's boot rebuild. day is
    # div(op.ts, 86_400_000) in the SAME wall-clock ms the op carries.
    """
    CREATE TABLE IF NOT EXISTS usage (
      principal TEXT NOT NULL,
      day INTEGER NOT NULL,
      tenant TEXT NOT NULL,
      vcpu_seconds REAL NOT NULL DEFAULT 0,
      gb_seconds REAL NOT NULL DEFAULT 0,
      task_count INTEGER NOT NULL DEFAULT 0,
      updated_at INTEGER NOT NULL,
      PRIMARY KEY (principal, day)
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
         :ok <- apply_ddl(conn) do
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
    INSERT INTO ops (ts, tenant, principal, workload, task_id, kind, payload_json)
    VALUES (?, ?, ?, ?, ?, ?, ?)
    """

    with {:ok, stmt} <- Sqlite3.prepare(conn, sql),
         :ok <-
           Sqlite3.bind(stmt, [
             op.ts,
             op.tenant,
             op.principal,
             op.workload,
             op.task_id,
             Atom.to_string(op.kind),
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

  defp project(conn, %Op{kind: :assigned} = op, _seq) do
    update_task_state(conn, op.task_id, "assigned", op.ts)
  end

  defp project(conn, %Op{kind: :started} = op, _seq) do
    update_task_state(conn, op.task_id, "running", op.ts)
  end

  defp project(conn, %Op{kind: :succeeded} = op, _seq) do
    with :ok <- update_task_state(conn, op.task_id, "succeeded", op.ts) do
      sql = """
      INSERT OR REPLACE INTO results
        (task_id, status_code, body, size_bytes, truncated, created_at, expires_at)
      VALUES (?, ?, ?, ?, ?, ?, ?)
      """

      payload = op.payload

      with {:ok, stmt} <- Sqlite3.prepare(conn, sql),
           :ok <-
             Sqlite3.bind(stmt, [
               op.task_id,
               Map.fetch!(payload, :status_code),
               Map.get(payload, :body),
               Map.fetch!(payload, :size_bytes),
               bool_to_int(Map.get(payload, :truncated, false)),
               op.ts,
               Map.get(payload, :expires_at)
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

  # Audit-only kinds: no task/result projection.
  defp project(_conn, %Op{kind: kind}, _seq)
       when kind in [:denied, :base_built, :primed, :vm_destroyed, :quota_enforced, :drain] do
    :ok
  end

  defp update_task_state(conn, task_id, state, ts) do
    sql = "UPDATE tasks SET state=?, updated_at=? WHERE task_id=?"

    with {:ok, stmt} <- Sqlite3.prepare(conn, sql),
         :ok <- Sqlite3.bind(stmt, [state, ts, task_id]),
         :done <- Sqlite3.step(conn, stmt),
         :ok <- Sqlite3.release(conn, stmt) do
      :ok
    end
  end

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

  defp to_float(n) when is_number(n), do: n * 1.0
  defp to_float(_), do: 0.0

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
      SELECT seq, ts, tenant, principal, workload, task_id, kind, payload_json
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
      {:row, [seq, ts, tenant, principal, workload, task_id, kind, payload_json]} ->
        op = %Op{
          seq: seq,
          ts: ts,
          tenant: tenant,
          principal: principal,
          workload: workload,
          task_id: task_id,
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

  defp do_load_result(conn, task_id) do
    sql = """
    SELECT status_code, body, size_bytes, truncated, created_at, expires_at
    FROM results WHERE task_id=?
    """

    with {:ok, stmt} <- Sqlite3.prepare(conn, sql),
         :ok <- Sqlite3.bind(stmt, [task_id]) do
      result =
        case Sqlite3.step(conn, stmt) do
          {:row, [status_code, body, size_bytes, truncated, created_at, expires_at]} ->
            {:ok,
             %{
               task_id: task_id,
               status_code: status_code,
               body: body,
               size_bytes: size_bytes,
               truncated: truncated == 1,
               created_at: created_at,
               expires_at: expires_at
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
  # the decoded payload. Keys come back as strings (:json.decode never restores
  # atoms), which the dispatcher expects. {:ok, nil} for an unknown task or a
  # submitted op that carried no request (an older record).
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
    SELECT principal, day, tenant, vcpu_seconds, gb_seconds, task_count, updated_at
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
      {:row, [principal, day, tenant, vcpu_seconds, gb_seconds, task_count, updated_at]} ->
        row = %{
          principal: principal,
          day: day,
          tenant: tenant,
          vcpu_seconds: vcpu_seconds,
          gb_seconds: gb_seconds,
          task_count: task_count,
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
         {:ok, ops_compacted, marker} <- compact_ops(conn, now_ms, state.journal_horizon_ms, batch) do
      done =
        results_deleted < batch and tasks_compacted < batch and ops_compacted < batch

      {:ok,
       %{
         results_deleted: results_deleted,
         tasks_compacted: tasks_compacted,
         ops_compacted: ops_compacted,
         compacted_through: marker,
         done: done
       }}
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
  # than the horizon (ts >= cutoff) or owned by a live (non-terminal) task. NULL
  # means no op is blocked, so the whole log is eligible.
  defp blocker_seq(conn, cutoff) do
    live_placeholders = @live_states |> Enum.map(fn _ -> "?" end) |> Enum.join(", ")

    sql = """
    SELECT MIN(seq) FROM ops
    WHERE ts >= ?
       OR task_id IN (SELECT task_id FROM tasks WHERE state IN (#{live_placeholders}))
    """

    with {:ok, stmt} <- Sqlite3.prepare(conn, sql),
         :ok <- Sqlite3.bind(stmt, [cutoff] ++ @live_states) do
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

  # -- JSON -------------------------------------------------------------

  # Normalizes a payload map before encoding: drops nil-valued keys and
  # stringifies atom values, so :json.encode/1 never sees an atom it can't
  # represent and decode/1 round-trips to plain strings/numbers/maps only.
  # Keys are already expected to be atoms or strings; :json.encode/1 handles
  # atom keys directly, so only values need normalizing here.
  defp encode_payload(payload) when is_map(payload) do
    payload
    |> Enum.reject(fn {_k, v} -> is_nil(v) end)
    |> Map.new(fn {k, v} -> {k, normalize_value(v)} end)
    |> :json.encode()
    |> :erlang.iolist_to_binary()
  end

  # nil is already dropped by encode_payload/1 before this runs; booleans are
  # atoms too but :json.encode/1 handles true/false natively so they pass through.
  defp normalize_value(v) when is_atom(v) and not is_boolean(v), do: Atom.to_string(v)
  defp normalize_value(v), do: v

  defp decode_payload(json) when is_binary(json) do
    :json.decode(json)
  end
end
