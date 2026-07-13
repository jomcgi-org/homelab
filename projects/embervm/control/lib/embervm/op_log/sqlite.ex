defmodule Embervm.OpLog.SQLite do
  @moduledoc """
  SQLite-WAL implementation of the `Embervm.OpLog` behaviour: single-writer
  GenServer owning one `Exqlite.Sqlite3` connection. Every append is one
  transaction that writes the immutable `ops` row and write-through projects
  it into the mutable `tasks`/`results` tables the dispatcher's ETS rebuild
  (Task 7) reads on boot. The op-log itself (the `ops` table) is append-only
  and is never rewritten by a projection; `compact/2` only prunes the
  projection tables (expired results, terminal tasks past retention).

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
  # Seven days in milliseconds: default age (from last update) at which a
  # terminal task is eligible for compaction.
  @default_retention_ms 7 * 24 * 60 * 60 * 1000

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
    """
  ]

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
  def compact(server \\ __MODULE__, now_ms) do
    GenServer.call(server, {:compact, now_ms})
  end

  # -- GenServer callbacks ------------------------------------------------

  @impl true
  def init(opts) do
    path = Keyword.get(opts, :path) || default_path()
    retention_ms = Keyword.get(opts, :retention_ms, @default_retention_ms)

    with {:ok, conn} <- Sqlite3.open(path),
         :ok <- apply_pragmas(conn),
         :ok <- apply_ddl(conn) do
      Logger.info("embervm op-log opened at #{path}")
      {:ok, %{conn: conn, path: path, retention_ms: retention_ms}}
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

  def handle_call({:compact, now_ms}, _from, state) do
    {:reply, do_compact(state.conn, now_ms, state.retention_ms), state}
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
        :ok
      end
    end
  end

  defp project(conn, %Op{kind: :failed} = op, _seq) do
    state = Map.fetch!(op.payload, :state)
    update_task_state(conn, op.task_id, to_string(state), op.ts)
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

  defp do_read_from(conn, seq) do
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

  # Prunes only the mutable projection tables. The ops table is the durable
  # audit log and is never rewritten here; ops-table rotation/archival is a
  # documented follow-on, not part of this seam.
  defp do_compact(conn, now_ms, retention_ms) do
    with {:ok, results_deleted} <- delete_expired_results(conn, now_ms),
         {:ok, tasks_compacted} <- delete_terminal_tasks(conn, now_ms, retention_ms) do
      {:ok, %{results_deleted: results_deleted, tasks_compacted: tasks_compacted}}
    end
  end

  defp delete_expired_results(conn, now_ms) do
    sql = "DELETE FROM results WHERE expires_at IS NOT NULL AND expires_at < ?"

    with {:ok, stmt} <- Sqlite3.prepare(conn, sql),
         :ok <- Sqlite3.bind(stmt, [now_ms]),
         :done <- Sqlite3.step(conn, stmt),
         :ok <- Sqlite3.release(conn, stmt),
         {:ok, changed} <- Sqlite3.changes(conn) do
      {:ok, changed}
    end
  end

  defp delete_terminal_tasks(conn, now_ms, retention_ms) do
    cutoff = now_ms - retention_ms
    placeholders = @terminal_states |> Enum.map(fn _ -> "?" end) |> Enum.join(", ")
    sql = "DELETE FROM tasks WHERE state IN (#{placeholders}) AND updated_at < ?"

    with {:ok, stmt} <- Sqlite3.prepare(conn, sql),
         :ok <- Sqlite3.bind(stmt, @terminal_states ++ [cutoff]),
         :done <- Sqlite3.step(conn, stmt),
         :ok <- Sqlite3.release(conn, stmt),
         {:ok, changed} <- Sqlite3.changes(conn) do
      {:ok, changed}
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
