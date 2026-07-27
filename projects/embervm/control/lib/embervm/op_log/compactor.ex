defmodule Embervm.OpLog.Compactor do
  @moduledoc """
  The scheduled op-log sweeper (ADR embervm/002 rule 2 + rule 3): a supervised
  GenServer that periodically drives the configured op-log backend's `compact/2`
  to reclaim space, then logs one structured summary line.

  It owns NO SQLite connection and adds NO second writer. Every batch is a
  discrete `GenServer.call` to the op-log's single-writer process, so appends
  queued on that mailbox interleave BETWEEN batches by construction: the sweep is
  a loop of bounded batches, not one long transaction, which is what keeps a large
  reclamation from head-of-line-blocking the 5ms append budget. Correctness never
  depends on this cadence (TTLs are enforced at read time in `Embervm.TaskStore`);
  the sweeper only reclaims disk.

  It is placed LATE in the application's `:rest_for_one` tree (right before Bandit)
  because it depends only on the op-log, which starts early: a Compactor crash
  restarts only Bandit, the minimum blast radius.
  """

  use GenServer
  require Logger

  # Default cadence: hourly. The application wires this from
  # EMBERVM_OPLOG_SWEEP_INTERVAL_MS (chart values.opLog.sweepIntervalSeconds).
  @default_interval_ms 3_600_000

  @spec start_link(keyword()) :: GenServer.on_start()
  def start_link(opts) do
    case Keyword.get(opts, :name, __MODULE__) do
      nil -> GenServer.start_link(__MODULE__, opts)
      name -> GenServer.start_link(__MODULE__, opts, name: name)
    end
  end

  @impl true
  def init(opts) do
    op_log_mod = Keyword.get(opts, :op_log_mod, Embervm.OpLog.SQLite)
    op_log = Keyword.get(opts, :op_log, op_log_mod)

    state = %{
      op_log: op_log,
      # The backend module dispatched for compact/2 + db_size/1, threaded
      # alongside :op_log (the server address) so a non-default backend never
      # requires editing this module. Defaults to the selected backend module.
      op_log_mod: op_log_mod,
      interval_ms: Keyword.get(opts, :interval_ms, @default_interval_ms)
    }

    schedule_sweep(state)
    {:ok, state}
  end

  @impl true
  def handle_info(:sweep, state) do
    run_sweep(state)
    schedule_sweep(state)
    {:noreply, state}
  end

  def handle_info(_msg, state), do: {:noreply, state}

  # One sweep: loop compact/2 until the op-log reports `done: true`, accumulating
  # per-table totals, then log one summary. `now_ms` is injected ONCE per sweep so
  # every batch prunes against the same instant (a task cannot straddle the
  # retention/horizon cutoff between batches). Each batch is a separate call, so
  # appends interleave. A compact error aborts the loop and logs what was reclaimed
  # so far; the next scheduled sweep retries.
  defp run_sweep(state) do
    now_ms = System.system_time(:millisecond)
    totals = %{
      results_deleted: 0,
      tasks_compacted: 0,
      sessions_compacted: 0,
      serving_instances_compacted: 0,
      stateful_instances_compacted: 0,
      group_instances_compacted: 0,
      ops_compacted: 0,
      compacted_through: 0
    }

    sweep_loop(state, now_ms, totals)
  end

  defp sweep_loop(state, now_ms, totals) do
    case state.op_log_mod.compact(state.op_log, now_ms) do
      {:ok, batch} ->
        totals = %{
          results_deleted: totals.results_deleted + batch.results_deleted,
          tasks_compacted: totals.tasks_compacted + batch.tasks_compacted,
          sessions_compacted: totals.sessions_compacted + batch.sessions_compacted,
          serving_instances_compacted:
            totals.serving_instances_compacted + batch.serving_instances_compacted,
          stateful_instances_compacted:
            totals.stateful_instances_compacted + batch.stateful_instances_compacted,
          group_instances_compacted:
            totals.group_instances_compacted + batch.group_instances_compacted,
          ops_compacted: totals.ops_compacted + batch.ops_compacted,
          # The marker is monotonic and reported per batch; the last batch's value
          # is the authoritative post-sweep marker.
          compacted_through: batch.compacted_through
        }

        if batch.done do
          log_summary(state, totals)
        else
          sweep_loop(state, now_ms, totals)
        end

      {:error, reason} ->
        Logger.warning("embervm op-log sweep aborted: #{inspect(reason)}; reclaimed=#{inspect(totals)}")
    end
  end

  defp log_summary(state, totals) do
    # Embervm.OpLog.Postgres (PR-4, #18/#27) has no single PVC file to stat, so
    # its db_size/1 always returns {:error, :not_supported}; this already-generic
    # {:error, _} clause omits the field rather than crashing or warning, so the
    # sweep summary is silently size-less on that backend instead of failing.
    db_size =
      case state.op_log_mod.db_size(state.op_log) do
        {:ok, size} -> size
        {:error, _} -> nil
      end

    Logger.info(
      "embervm op-log sweep complete " <>
        inspect(
          results_deleted: totals.results_deleted,
          tasks_compacted: totals.tasks_compacted,
          sessions_compacted: totals.sessions_compacted,
          serving_instances_compacted: totals.serving_instances_compacted,
          stateful_instances_compacted: totals.stateful_instances_compacted,
          group_instances_compacted: totals.group_instances_compacted,
          ops_compacted: totals.ops_compacted,
          compacted_through: totals.compacted_through,
          db_size_bytes: db_size
        )
    )
  end

  defp schedule_sweep(state) do
    Process.send_after(self(), :sweep, state.interval_ms)
    state
  end
end
