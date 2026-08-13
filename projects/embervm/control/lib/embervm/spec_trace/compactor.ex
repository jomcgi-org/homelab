defmodule Embervm.SpecTrace.Compactor do
  use GenServer
  require Logger
  @default_interval_ms 3_600_000
  @default_ttl_ms 86_400_000
  # :ignore when the trace gate is off. A sweeper with nothing to sweep is not
  # merely idle: it wakes on an interval and issues a DELETE, which against the
  # Postgres backend means recurring writes to the shared production cluster on
  # behalf of a disabled feature.
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
    state = %{spec_trace: Keyword.get(opts, :spec_trace, Embervm.SpecTrace.Store.SQLite), spec_trace_mod: Keyword.get(opts, :spec_trace_mod, Embervm.SpecTrace.Store.SQLite), interval_ms: Keyword.get(opts, :interval_ms, @default_interval_ms), ttl_ms: Keyword.get(opts, :ttl_ms, @default_ttl_ms)}
    Process.send_after(self(), :sweep, state.interval_ms); {:ok, state}
  end
  @impl true
  def handle_info(:sweep, state) do
    now = System.system_time(:millisecond); sweep_loop(state, now, 0); Process.send_after(self(), :sweep, state.interval_ms); {:noreply, state}
  end
  def handle_info(_, state), do: {:noreply, state}
  defp sweep_loop(state, now, total) do
    case state.spec_trace_mod.sweep(state.spec_trace, now_ms: now, ttl_ms: state.ttl_ms, batch_size: 1000) do
      {:ok, %{deleted: count, done: true}} -> Logger.info("embervm spec-trace sweep complete", deleted: total + count)
      {:ok, %{deleted: count, done: false}} -> sweep_loop(state, now, total + count)
      {:error, reason} -> Logger.warning("embervm spec-trace sweep aborted", reason: inspect(reason), deleted: total)
    end
  end
end
