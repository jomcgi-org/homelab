defmodule Embervm.SpecTrace.Store.Postgres do
  @behaviour Embervm.SpecTrace.Store
  use GenServer
  require Logger

  @ddl [
    "CREATE TABLE IF NOT EXISTS public.spec_trace (seq BIGSERIAL PRIMARY KEY, run_id TEXT NOT NULL, mono BIGINT NOT NULL, ts BIGINT NOT NULL, spec TEXT NOT NULL, action TEXT NOT NULL, vars JSONB)",
    "CREATE INDEX IF NOT EXISTS spec_trace_ts_idx ON public.spec_trace(ts)",
    "CREATE INDEX IF NOT EXISTS spec_trace_run_seq_idx ON public.spec_trace(run_id, seq)"
  ]

  # :ignore when the trace gate is off. This is the backend where the guard
  # actually matters: without it a DISABLED trace still opens a postgrex
  # connection to monolith-pg, the SHARED production cluster, and the compactor
  # then runs a periodic DELETE against it for a feature emitting nothing.
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
  def write(server \\ __MODULE__, records), do: GenServer.call(server, {:write, records})
  def read_window(server \\ __MODULE__, opts \\ []), do: GenServer.call(server, {:read, opts})
  def sweep(server \\ __MODULE__, opts \\ []), do: GenServer.call(server, {:sweep, opts})

  @impl true
  def max_seq(server \\ __MODULE__), do: GenServer.call(server, :max_seq)

  @impl true
  def init(opts) do
    dsn = Keyword.get(opts, :dsn, System.get_env("EMBERVM_OPLOG_DSN", ""))
    with {:ok, conn} <- connect(dsn), :ok <- create_schema(conn) do {:ok, %{conn: conn}} else {:error, reason} -> {:stop, {:connect_failed, reason}} end
  end
  @impl true
  def terminate(_reason, %{conn: conn}), do: GenServer.stop(conn)
  @impl true
  def handle_call({:write, records}, _from, state), do: {:reply, safe(fn -> do_write(state.conn, records) end), state}
  def handle_call(:max_seq, _from, state) do
    max =
      case safe(fn -> Postgrex.query!(state.conn, "SELECT COALESCE(MAX(seq), 0) FROM public.spec_trace", []) end) do
        %Postgrex.Result{rows: [[max]]} when is_integer(max) -> max
        _ -> 0
      end

    {:reply, max, state}
  end

  def handle_call({:read, opts}, _from, state), do: {:reply, safe(fn -> do_read(state.conn, opts) end), state}
  def handle_call({:sweep, opts}, _from, state), do: {:reply, safe(fn -> do_sweep(state.conn, opts) end), state}

  defp connect(dsn) when is_binary(dsn) do
    uri = URI.parse(dsn); [user, pass] = String.split(uri.userinfo || ":", ":", parts: 2)
    Postgrex.start_link(hostname: uri.host, port: uri.port || 5432, username: empty_nil(user), password: empty_nil(pass), database: empty_nil(String.trim_leading(uri.path || "", "/")), name: nil)
  end
  defp connect(opts) when is_list(opts), do: Postgrex.start_link(Keyword.put(opts, :name, nil))
  defp empty_nil(""), do: nil
  defp empty_nil(value), do: value
  defp create_schema(conn), do: Enum.reduce_while(@ddl, :ok, fn sql, :ok -> case Postgrex.query(conn, sql, []) do {:ok, _} -> {:cont, :ok}; error -> {:halt, error} end end)
  defp safe(fun) do
    try do fun.() rescue error -> Logger.warning("embervm spec_trace store error", error: inspect(error)); {:error, error} catch kind, reason -> Logger.warning("embervm spec_trace store error", error: inspect({kind, reason})); {:error, reason} end
  end
  defp do_write(_conn, []), do: :ok
  defp do_write(conn, records) do
    result =
      Postgrex.transaction(conn, fn tx ->
        Enum.reduce_while(records, :ok, fn r, :ok ->
          params = [
            r["seq"] || r[:seq], r["run_id"] || r[:run_id],
            r["mono"] || r[:mono], r["ts"] || r[:ts],
            r["spec"] || r[:spec], r["action"] || r[:action],
            Jason.encode!(r["vars"] || r[:vars])
          ]

          case Postgrex.query(tx, "INSERT INTO public.spec_trace (seq, run_id, mono, ts, spec, action, vars) VALUES ($1, $2, $3, $4, $5, $6, $7)", params) do
            {:ok, _} -> {:cont, :ok}
            {:error, reason} -> Postgrex.rollback(tx, reason)
          end
        end)
      end)

    case result do
      {:ok, :ok} -> :ok
      {:error, reason} -> {:error, reason}
    end
  end
  defp do_read(conn, opts) do
    {where, params} = filters(opts)
    case Postgrex.query(conn, "SELECT run_id, seq, mono, ts, spec, action, vars FROM public.spec_trace" <> where <> " ORDER BY mono ASC", params) do
      {:ok, %{rows: rows}} -> {:ok, Enum.map(rows, fn [run_id, seq, mono, ts, spec, action, vars] -> %{"run_id" => run_id, "seq" => seq, "mono" => mono, "ts" => ts, "spec" => spec, "action" => action, "vars" => vars || %{}} end)}
      error -> error
    end
  end
  defp filters(opts) do
    Enum.reduce([{:since_seq, "seq >= $N"}, {:since_ts_ms, "ts >= $N"}, {:until_ts_ms, "ts <= $N"}, {:run_id, "run_id = $N"}, {:spec, "spec = $N"}, {:action, "action = $N"}], {"", []}, fn {key, template}, {sql, params} -> case Keyword.get(opts, key) do nil -> {sql, params}; value -> n = length(params) + 1; {sql <> if(sql == "", do: " WHERE ", else: " AND ") <> String.replace(template, "N", Integer.to_string(n)), params ++ [value]} end end)
  end
  defp do_sweep(conn, opts) do
    cutoff = Keyword.fetch!(opts, :now_ms) - Keyword.fetch!(opts, :ttl_ms); size = Keyword.get(opts, :batch_size, 1000)
    case Postgrex.transaction(conn, fn tx ->
      with {:ok, %{rows: rows}} <- Postgrex.query(tx, "SELECT seq FROM public.spec_trace WHERE ts < $1 ORDER BY ts, seq LIMIT $2", [cutoff, size]),
           seqs <- Enum.map(rows, &hd/1),
           {:ok, _} <- Postgrex.query(tx, "DELETE FROM public.spec_trace WHERE seq = ANY($1)", [seqs]) do seqs end
    end) do
      {:ok, seqs} -> {:ok, %{deleted: length(seqs), done: no_old?(conn, cutoff)}}
      {:error, reason} -> {:error, reason}
    end
  end
  defp no_old?(conn, cutoff), do: match?({:ok, %{rows: []}}, Postgrex.query(conn, "SELECT 1 FROM public.spec_trace WHERE ts < $1 LIMIT 1", [cutoff]))
end
