defmodule Embervm.SpecTrace.Store.SQLite do
  @behaviour Embervm.SpecTrace.Store
  use GenServer
  require Logger
  alias Exqlite.Sqlite3

  @ddl [
    "CREATE TABLE IF NOT EXISTS spec_trace (seq BIGINT PRIMARY KEY, run_id TEXT NOT NULL, mono BIGINT NOT NULL, ts BIGINT NOT NULL, spec TEXT NOT NULL, action TEXT NOT NULL, vars BLOB)",
    "CREATE INDEX IF NOT EXISTS spec_trace_ts_idx ON spec_trace(ts)",
    "CREATE INDEX IF NOT EXISTS spec_trace_run_seq_idx ON spec_trace(run_id, seq)"
  ]

  # :ignore when the trace gate is off, matching Embervm.SpecTrace.start_link/1.
  # The store exists only to serve the trace, so starting it while the trace
  # emits nothing buys nothing and costs a process (and, for the Postgres
  # backend, a connection to the shared production cluster).
  def start_link(opts \\ []) do
    if Embervm.SpecTrace.enabled_now?() do
      case Keyword.get(opts, :name, __MODULE__) do
        nil -> GenServer.start_link(__MODULE__, opts)
        name -> GenServer.start_link(__MODULE__, opts, name: name)
      end
    else
      :ignore
    end
  end

  @impl true
  def init(opts) do
    path = Keyword.get(opts, :path, ":memory:") || ":memory:"
    with {:ok, conn} <- Sqlite3.open(path), :ok <- create_schema(conn) do
      {:ok, %{conn: conn}}
    else
      {:error, reason} -> {:stop, {:open_failed, reason}}
    end
  end

  @impl true
  def terminate(_reason, %{conn: conn}), do: Sqlite3.close(conn)

  def write(server \\ __MODULE__, records), do: GenServer.call(server, {:write, records})
  def read_window(server \\ __MODULE__, opts \\ []), do: GenServer.call(server, {:read, opts})
  def sweep(server \\ __MODULE__, opts \\ []), do: GenServer.call(server, {:sweep, opts})

  @impl true
  def max_seq(server \\ __MODULE__), do: GenServer.call(server, :max_seq)

  @impl true
  def handle_call({:write, records}, _from, state) do
    {:reply, safe(state, fn -> do_write(state.conn, records) end), state}
  end

  def handle_call(:max_seq, _from, state) do
    {:reply, do_max_seq(state.conn), state}
  end

  def handle_call({:read, opts}, _from, state) do
    {:reply, safe(state, fn -> do_read(state.conn, opts) end), state}
  end

  def handle_call({:sweep, opts}, _from, state) do
    {:reply, safe(state, fn -> do_sweep(state.conn, opts) end), state}
  end

  defp create_schema(conn), do: Enum.reduce_while(@ddl, :ok, fn sql, :ok ->
    case Sqlite3.execute(conn, sql) do :ok -> {:cont, :ok}; error -> {:halt, error} end
  end)

  defp do_write(_conn, []), do: :ok
  defp do_write(conn, records) do
    with :ok <- Sqlite3.execute(conn, "BEGIN IMMEDIATE"),
         :ok <- insert_records(conn, records),
         :ok <- Sqlite3.execute(conn, "COMMIT") do
      :ok
    else
      {:error, reason} -> Sqlite3.execute(conn, "ROLLBACK"); {:error, reason}
    end
  end

  defp insert_records(_conn, []), do: :ok
  defp insert_records(conn, [record | rest]) do
    sql = "INSERT INTO spec_trace (seq, run_id, mono, ts, spec, action, vars) VALUES (?, ?, ?, ?, ?, ?, ?)"
    with {:ok, stmt} <- Sqlite3.prepare(conn, sql),
         :ok <- Sqlite3.bind(stmt, [record["seq"] || record[:seq], record["run_id"] || record[:run_id], record["mono"] || record[:mono], record["ts"] || record[:ts], record["spec"] || record[:spec], record["action"] || record[:action], :erlang.term_to_binary(record["vars"] || record[:vars])]),
         :done <- Sqlite3.step(conn, stmt),
         :ok <- Sqlite3.release(conn, stmt),
         :ok <- insert_records(conn, rest), do: :ok
  end

  defp do_read(conn, opts) do
    {where, params} = filters(opts)
    sql = "SELECT run_id, seq, mono, ts, spec, action, vars FROM spec_trace" <> where <> " ORDER BY mono ASC"
    with {:ok, stmt} <- Sqlite3.prepare(conn, sql), :ok <- Sqlite3.bind(stmt, params) do
      rows = collect_rows(conn, stmt, [])
      :ok = Sqlite3.release(conn, stmt)
      {:ok, rows}
    end
  end

  defp do_max_seq(conn) do
    with {:ok, stmt} <- Sqlite3.prepare(conn, "SELECT COALESCE(MAX(seq), 0) FROM spec_trace") do
      result =
        case Sqlite3.step(conn, stmt) do
          {:row, [max]} when is_integer(max) -> max
          _ -> 0
        end

      :ok = Sqlite3.release(conn, stmt)
      result
    else
      _ -> 0
    end
  end

  defp collect_rows(conn, stmt, acc) do
    case Sqlite3.step(conn, stmt) do
      {:row, [run_id, seq, mono, ts, spec, action, vars]} ->
        collect_rows(conn, stmt, [%{"run_id" => run_id, "seq" => seq, "mono" => mono, "ts" => ts, "spec" => spec, "action" => action, "vars" => :erlang.binary_to_term(vars)} | acc])
      :done -> Enum.reverse(acc)
      {:error, reason} -> raise "sqlite read failed: #{inspect(reason)}"
    end
  end

  defp filters(opts) do
    Enum.reduce([{:since_seq, "seq >= ?"}, {:since_ts_ms, "ts >= ?"}, {:until_ts_ms, "ts <= ?"}, {:run_id, "run_id = ?"}, {:spec, "spec = ?"}, {:action, "action = ?"}], {"", []}, fn {key, clause}, {sql, params} ->
      case Keyword.get(opts, key) do nil -> {sql, params}; value -> {sql <> if(sql == "", do: " WHERE ", else: " AND ") <> clause, params ++ [value]} end
    end)
  end

  defp do_sweep(conn, opts) do
    cutoff = Keyword.fetch!(opts, :now_ms) - Keyword.fetch!(opts, :ttl_ms)
    batch_size = Keyword.get(opts, :batch_size, 1000)
    with {:ok, stmt} <- Sqlite3.prepare(conn, "SELECT seq FROM spec_trace WHERE ts < ? ORDER BY ts, seq LIMIT ?"),
         :ok <- Sqlite3.bind(stmt, [cutoff, batch_size]) do
      seqs = collect_seqs(conn, stmt, [])
      :ok = Sqlite3.release(conn, stmt)
      delete_seqs(conn, seqs)
      {:ok, %{deleted: length(seqs), done: not has_old?(conn, cutoff)}}
    end
  end

  defp collect_seqs(conn, stmt, acc) do
    case Sqlite3.step(conn, stmt) do {:row, [seq]} -> collect_seqs(conn, stmt, [seq | acc]); :done -> Enum.reverse(acc); {:error, reason} -> raise inspect(reason) end
  end
  defp delete_seqs(_conn, []), do: :ok
  defp delete_seqs(conn, seqs) do
    Enum.each(seqs, fn seq -> {:ok, stmt} = Sqlite3.prepare(conn, "DELETE FROM spec_trace WHERE seq = ?"); :ok = Sqlite3.bind(stmt, [seq]); :done = Sqlite3.step(conn, stmt); :ok = Sqlite3.release(conn, stmt) end)
  end
  defp has_old?(conn, cutoff) do
    {:ok, stmt} = Sqlite3.prepare(conn, "SELECT 1 FROM spec_trace WHERE ts < ? LIMIT 1"); :ok = Sqlite3.bind(stmt, [cutoff]); result = Sqlite3.step(conn, stmt) != :done; :ok = Sqlite3.release(conn, stmt); result
  end
  defp safe(_state, fun) do
    try do fun.() rescue error -> Logger.warning("embervm spec_trace store error", error: inspect(error)); {:error, error} catch kind, reason -> Logger.warning("embervm spec_trace store error", error: inspect({kind, reason})); {:error, reason} end
  end
end
