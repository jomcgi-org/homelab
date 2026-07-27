defmodule Embervm.Metering do
  @moduledoc """
  Metering, audit, and quota enforcement (Task 12): the third job of the op-log.

  ## What this owns

  A single public ETS table, `{principal, day} -> total_cpu_ms` (an integer, so
  `:ets.update_counter/4` can accumulate it lock-free), holding today's running
  per-principal CPU total. This is the READ-THROUGH CACHE the quota gate consults
  O(1); it is NOT the source of truth. The durable truth is the op-log's `usage`
  projection (`(principal, day) -> vcpu_seconds/gb_seconds/task_count`), which is
  written transactionally with each `:succeeded`/`:failed` op by
  `Embervm.OpLog.SQLite` and read back paged at `GET /v1/usage`. On boot this
  process rebuilds today's cache from that projection, so the cache is always
  reconstructible and a dropped `charge/4` only under-counts the advisory cache
  until the next rebuild, never the durable record.

  ## Why usage rides the existing append, not a flush timer

  The spec's "aggregate in ETS, flush to the op-log on interval and drain" exists
  to avoid ADDING a blocking store write on the dispatch path. But every task
  completion ALREADY makes one durable, fsynced op-log append (the `:succeeded`
  or `:failed` op, which the task FSM requires and we cannot remove). Carrying the
  usage facts in that op's payload and projecting them in the same transaction
  adds zero writes, zero extra fsyncs, and zero unflushed window: there is no
  interval during which a crash loses accumulated usage, and no flush-atomicity
  question, because the usage row commits with the op or not at all. So this
  module has no flush timer; `charge/4` only bumps the read cache.

  ## Quota (fail-closed, opt-in)

  `within_quota?/4` reads the per-principal daily vCPU-second budget from
  values-configured app-env and compares it to the cache. It is deliberately
  asymmetric to the auth allow-list: auth is a security gate (empty = deny-all),
  quota is a resource-abuse gate that is OFF until a budget is configured (no
  budget for a principal => allowed). "Fail-closed" is scoped to a principal that
  HAS a budget: if its budget exists but the cache table is unreadable, dispatch
  is denied. That scoping matters, without it a Metering crash would brick all
  dispatch on a cluster that never opted into quotas, turning an opt-in gate into
  a global availability dependency.

  ## Audit (denials)

  `record_denial/4` appends one op-log entry per REQUEST-scoped denial (the
  audit record and the metering stream are the same log). It is an async cast so
  a 4xx response never waits on an fsync, and it is used only for the
  principal-attributable, request-bounded denials, quota (`:quota_enforced`),
  auth-forbidden and per-principal queue-depth (`:denied`). Unauthenticated 401s
  are deliberately NOT appended: each append is an fsync, and letting an
  unauthenticated caller force durable writes is a write-amplification vector
  against the single-writer op-log. Dispatch-tick saturation conditions
  (`:cap`/`:stale_capacity`/`:no_capacity`/`:principal_share`) are also not
  appended, they are standing conditions re-evaluated every drain tick with no
  principal, exposed as `Embervm.Dispatcher.stats/0` counters, not audit records.
  """

  use GenServer
  require Logger

  alias Embervm.OpLog.Op

  @table :embervm_usage_quota
  @day_ms 86_400_000
  # Hourly sweep of cache rows for days before today: yesterday's totals never
  # gate today's quota, and left unpruned the table would grow one row per
  # principal per day forever.
  @prune_interval_ms 3_600_000

  # -- Client API ------------------------------------------------------------

  @spec start_link(keyword()) :: GenServer.on_start()
  def start_link(opts) do
    case Keyword.get(opts, :name, __MODULE__) do
      nil -> GenServer.start_link(__MODULE__, opts)
      name -> GenServer.start_link(__MODULE__, opts, name: name)
    end
  end

  @doc """
  The default public table name (the supervised singleton owns it). The router's
  quota gate and the dispatcher's per-principal skip read it directly.
  """
  @spec table() :: atom()
  def table, do: @table

  @doc """
  Charge a completed task's `cpu_ms` to the per-principal daily quota cache. A
  lock-free `:ets.update_counter/4` on the public table (no GenServer round-trip),
  so `Embervm.TaskStore`'s completion hook calls it inline off the durable write.
  A no-op when the table is absent (the durable projection already recorded the
  charge; the cache re-derives it on the next rebuild) or when there is nothing to
  charge. `day` is derived from the WALL-clock `now_ms`, never the dispatcher's
  monotonic clock.
  """
  @spec charge(atom(), String.t() | nil, integer(), integer()) :: :ok
  def charge(table \\ @table, principal, now_ms, cpu_ms) do
    if is_binary(principal) and is_integer(cpu_ms) and cpu_ms > 0 and
         :ets.whereis(table) != :undefined do
      :ets.update_counter(table, {principal, day_of(now_ms)}, {2, cpu_ms}, {{principal, day_of(now_ms)}, 0})
    end

    :ok
  end

  @doc """
  Whether `principal` is within its daily vCPU-second budget as of `now_ms`.
  Lock-free and fail-closed:

    * no budget configured for the principal => `true` (quota is opt-in);
    * budget configured but the cache table is absent => `false` (fail-closed);
    * otherwise `used_vcpu_seconds < budget`, where used is `cache_cpu_ms / 1000`.

  `quota` is `%{budgets: %{principal => vcpu_seconds}, default: vcpu_seconds | nil}`.
  Reading app-env and a `read_concurrency` ETS table, this takes no lock and never
  calls the GenServer, so it is safe on the submit path and inside the dispatcher.
  """
  @spec within_quota?(String.t(), integer(), map(), atom()) :: boolean()
  def within_quota?(principal, now_ms, quota, table \\ @table) do
    case budget_for(quota, principal) do
      nil ->
        true

      budget ->
        case :ets.whereis(table) do
          :undefined ->
            false

          _ ->
            used_cpu_ms =
              case :ets.lookup(table, {principal, day_of(now_ms)}) do
                [{_key, n}] -> n
                [] -> 0
              end

            used_cpu_ms / 1000 < budget
        end
    end
  end

  @doc """
  Convenience for the router: resolves `now`, the quota config, and the table
  from app-env/defaults. Production submit path uses this; the dispatcher and
  tests use the 4-arity form with injected values.
  """
  @spec within_quota?(String.t()) :: boolean()
  def within_quota?(principal) do
    within_quota?(principal, System.system_time(:millisecond), quota_config(), @table)
  end

  @doc "The values-configured quota, from app-env (empty = quota off)."
  @spec quota_config() :: map()
  def quota_config, do: Application.get_env(:embervm, :quota, %{budgets: %{}, default: nil})

  @doc """
  Records a request-scoped denial as an op-log append (async cast). `reason`
  `:quota` becomes a `:quota_enforced` op; every other reason becomes a `:denied`
  op carrying `reason` in its payload. A no-op when no metering process is running
  (unit tests without one), so callers need no guard.
  """
  @spec record_denial(GenServer.server(), String.t() | nil, String.t() | nil, atom()) :: :ok
  def record_denial(server \\ __MODULE__, principal, workload, reason) do
    case GenServer.whereis(server) do
      nil -> :ok
      _ -> GenServer.cast(server, {:denial, principal, workload, reason})
    end
  end

  @doc """
  The completion hook `Embervm.TaskStore` fires after a `:succeeded`/`:failed`
  op with usage lands durably. Charges the quota cache and logs once per node
  when a success reports all-zero usage (a daemon that never filled `UsageStats`,
  which would silently disable metering). `event` is `%{principal, ts, stats}`.
  """
  @spec on_metered(atom(), map()) :: :ok
  def on_metered(table \\ @table, %{principal: principal, ts: ts, stats: stats}) do
    charge(table, principal, ts, Map.get(stats, :cpu_ms, 0))

    if Embervm.Usage.all_zero?(stats) do
      Logger.warning("embervm metering: task charged all-zero usage for #{principal} (daemon UsageStats unset?)")
    end

    :ok
  end

  # -- GenServer callbacks ---------------------------------------------------

  @impl true
  def init(opts) do
    op_log_mod = Keyword.get(opts, :op_log_mod, Embervm.OpLog.SQLite)
    op_log = Keyword.get(opts, :op_log, op_log_mod)

    state = %{
      op_log: op_log,
      # The backend module dispatched at every call site below, threaded alongside
      # :op_log (the server address) so a non-default backend never requires
      # editing this module. Defaults to the selected backend module.
      op_log_mod: op_log_mod,
      tenant: Keyword.get(opts, :tenant, "homelab"),
      table: Keyword.get(opts, :table, @table),
      clock: Keyword.get(opts, :clock, &default_clock/0),
      prune_interval_ms: Keyword.get(opts, :prune_interval_ms, @prune_interval_ms)
    }

    create_table(state.table)

    if Keyword.get(opts, :rebuild, true) do
      {:ok, state, {:continue, :rebuild}}
    else
      schedule_prune(state)
      {:ok, state}
    end
  end

  @impl true
  def handle_continue(:rebuild, state) do
    rebuild(state)
    schedule_prune(state)
    {:noreply, state}
  end

  @impl true
  def handle_cast({:denial, principal, workload, reason}, state) do
    kind = if reason == :quota, do: :quota_enforced, else: :denied

    # Structured warn (Task 13): every request-scoped denial (quota, auth-forbidden,
    # queue-depth) is logged with principal + workload + reason so it is searchable
    # in SigNoz, alongside the durable op-log append below.
    Logger.warning("embervm denial", principal: principal, workload: workload, reason: reason, kind: kind)

    op = %Op{
      kind: kind,
      tenant: state.tenant,
      principal: principal,
      workload: workload,
      task_id: nil,
      ts: state.clock.(),
      payload: %{reason: reason}
    }

    _ = safe(fn -> state.op_log_mod.append(state.op_log, op) end)
    {:noreply, state}
  end

  @impl true
  def handle_info(:prune, state) do
    prune(state)
    schedule_prune(state)
    {:noreply, state}
  end

  def handle_info(_msg, state), do: {:noreply, state}

  # -- internals -------------------------------------------------------------

  # Seed today's cache from the durable usage projection so the quota gate is
  # correct immediately after a restart. The cache holds integer cpu_ms; the
  # projection holds vcpu_seconds (float), so convert back (round(vcpu * 1000)):
  # since each task adds cpu_ms/1000, the summed vcpu_seconds * 1000 recovers the
  # summed cpu_ms exactly modulo float rounding, which is fine for an advisory
  # cache whose authority is the projection.
  defp rebuild(state) do
    today = day_of(state.clock.())

    case safe(fn -> state.op_log_mod.list_usage(state.op_log, since_day: today, limit: :infinity) end) do
      {:ok, %{items: items}} ->
        Enum.each(items, fn row ->
          cpu_ms = round(row.vcpu_seconds * 1000)
          if cpu_ms > 0, do: :ets.insert(state.table, {{row.principal, row.day}, cpu_ms})
        end)

      _ ->
        :ok
    end
  end

  # Drop cache rows for days strictly before today. select_delete is safe against
  # a table being read concurrently (unlike deleting during a foldl).
  defp prune(state) do
    today = day_of(state.clock.())

    :ets.select_delete(state.table, [
      {{{:"$1", :"$2"}, :"$3"}, [{:<, :"$2", today}], [true]}
    ])
  end

  defp create_table(table) do
    if :ets.whereis(table) == :undefined do
      :ets.new(table, [:set, :public, :named_table, read_concurrency: true, write_concurrency: true])
    end
  end

  defp schedule_prune(state) do
    Process.send_after(self(), :prune, state.prune_interval_ms)
    state
  end

  defp budget_for(%{budgets: budgets, default: default}, principal) do
    Map.get(budgets, principal) || default
  end

  defp budget_for(_quota, _principal), do: nil

  defp day_of(now_ms), do: div(now_ms, @day_ms)

  defp safe(fun) do
    try do
      fun.()
    rescue
      _ -> :error
    catch
      _, _ -> :error
    end
  end

  defp default_clock, do: System.system_time(:millisecond)
end
