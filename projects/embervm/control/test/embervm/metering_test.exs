defmodule Embervm.MeteringTest do
  @moduledoc """
  Metering, audit, and quotas (Task 12). The quota-cache math and fail-closed
  gate are exercised as pure functions over an isolated table; the GenServer
  paths (boot rebuild from the durable projection, denial appends) run against
  an unnamed op-log with an injected clock so `async: true` stays safe.
  """
  use ExUnit.Case, async: true

  import ExUnit.CaptureLog

  alias Embervm.Metering
  alias Embervm.OpLog.Op
  alias Embervm.OpLog.SQLite

  # A fixed wall time on UTC day 5, so charge/within_quota agree on the day key
  # without depending on the real clock.
  @day5 5 * 86_400_000

  defp fresh_table do
    name = String.to_atom("embervm_usage_test_#{System.unique_integer([:positive, :monotonic])}")
    :ets.new(name, [:set, :public, :named_table, read_concurrency: true, write_concurrency: true])
    name
  end

  # -- quota math + fail-closed (pure) ---------------------------------------

  test "no budget configured => allowed (quota is opt-in), even with no table" do
    quota = %{budgets: %{}, default: nil}
    table = fresh_table()

    assert Metering.within_quota?("p1", @day5, quota, table)
    # Opt-in means a principal with no budget is allowed even when the cache is
    # absent: an unquota'd cluster is never bricked by a missing table.
    assert Metering.within_quota?("p1", @day5, quota, :embervm_no_such_table)
  end

  test "budget configured but table absent => fail-closed (denied)" do
    quota = %{budgets: %{"p1" => 100.0}, default: nil}
    refute Metering.within_quota?("p1", @day5, quota, :embervm_no_such_table)
  end

  test "a budget of exactly 0 is a hard stop (denies even at zero usage)" do
    table = fresh_table()
    quota = %{budgets: %{"p1" => 0.0}, default: nil}
    # No charge at all, yet denied: 0 used is not < 0 budget.
    refute Metering.within_quota?("p1", @day5, quota, table)

    # 0 as the default denies an unlisted principal too.
    default0 = %{budgets: %{}, default: 0.0}
    refute Metering.within_quota?("anon", @day5, default0, table)
  end

  test "charge accumulates cpu_ms and within_quota? enforces the vCPU-second budget" do
    table = fresh_table()
    quota = %{budgets: %{"p1" => 2.0}, default: nil}

    # Under budget: 1.5 vCPU-s < 2.0.
    Metering.charge(table, "p1", @day5, 1500)
    assert Metering.within_quota?("p1", @day5, quota, table)

    # Push over: total 2.1 vCPU-s >= 2.0.
    Metering.charge(table, "p1", @day5, 600)
    refute Metering.within_quota?("p1", @day5, quota, table)

    # A different principal is unaffected.
    assert Metering.within_quota?("p2", @day5, quota, table)
  end

  test "charge is bucketed by day: yesterday's usage does not gate today" do
    table = fresh_table()
    quota = %{budgets: %{"p1" => 1.0}, default: nil}

    Metering.charge(table, "p1", @day5, 5000)
    refute Metering.within_quota?("p1", @day5, quota, table)
    # Next day resets: no charge on day 6.
    assert Metering.within_quota?("p1", @day5 + 86_400_000, quota, table)
  end

  test "the default budget applies to principals not named in the map" do
    table = fresh_table()
    quota = %{budgets: %{"vip" => 1000.0}, default: 1.0}

    Metering.charge(table, "anon", @day5, 1500)
    # anon has no explicit budget, so the 1.0 default applies: 1.5 > 1.0.
    refute Metering.within_quota?("anon", @day5, quota, table)
    # vip's explicit budget overrides the default.
    assert Metering.within_quota?("vip", @day5, quota, table)
  end

  test "charge is a no-op for nil principal, non-positive cpu, and absent table" do
    table = fresh_table()

    Metering.charge(table, nil, @day5, 1000)
    Metering.charge(table, "p1", @day5, 0)
    Metering.charge(table, "p1", @day5, -5)
    Metering.charge(:embervm_no_such_table, "p1", @day5, 1000)

    assert :ets.tab2list(table) == []
  end

  test "on_metered charges via the completion hook and warns on all-zero usage" do
    table = fresh_table()
    quota = %{budgets: %{"p1" => 1.0}, default: nil}

    Metering.on_metered(table, %{principal: "p1", ts: @day5, stats: %{cpu_ms: 1500, peak_rss_mib: 8, wall_ms: 100}})
    refute Metering.within_quota?("p1", @day5, quota, table)

    log =
      capture_log(fn ->
        Metering.on_metered(table, %{principal: "z", ts: @day5, stats: %{cpu_ms: 0, peak_rss_mib: 0, wall_ms: 0}})
      end)

    assert log =~ "all-zero usage"
  end

  # -- GenServer paths -------------------------------------------------------

  defp start_oplog do
    path = Path.join(System.tmp_dir!(), "embervm_meter_test_#{System.unique_integer([:positive, :monotonic])}.db")
    on_exit(fn -> File.rm_rf!(path) end)
    {:ok, pid} = SQLite.start_link(path: path, name: nil)
    pid
  end

  test "boot rebuild seeds today's quota cache from the durable usage projection" do
    op_log = start_oplog()
    table = String.to_atom("embervm_usage_rebuild_#{System.unique_integer([:positive, :monotonic])}")

    # Seed a succeeded task with usage on day 5 (2.0 vCPU-s for p1).
    {:ok, _} =
      SQLite.append(op_log, %Op{kind: :submitted, tenant: "t1", principal: "p1", workload: "wl", task_id: "s1", ts: @day5, payload: %{}})

    stats = %{cpu_ms: 2000, peak_rss_mib: 1024, wall_ms: 2000}

    {:ok, _} =
      SQLite.append(op_log, %Op{
        kind: :succeeded,
        tenant: "t1",
        principal: "p1",
        workload: "wl",
        task_id: "s1",
        ts: @day5,
        payload: %{status_code: 200, size_bytes: 0, usage: Map.merge(stats, Embervm.Usage.billed(stats))}
      })

    # Start Metering with a clock pinned to day 5 so its rebuild loads day 5.
    {:ok, meter} = Metering.start_link(name: nil, op_log: op_log, table: table, clock: fn -> @day5 end)
    # Sync: handle_continue(:rebuild) runs before any call is served, so once
    # get_state replies the cache is seeded.
    _ = :sys.get_state(meter)

    quota = %{budgets: %{"p1" => 1.5}, default: nil}
    # The 2.0 vCPU-s seeded usage exceeds the 1.5 budget: rebuild carried it into
    # the cache, so a post-restart quota read is correct immediately.
    refute Metering.within_quota?("p1", @day5, quota, table)
  end

  test "record_denial appends a :quota_enforced op for quota and :denied otherwise" do
    op_log = start_oplog()
    table = String.to_atom("embervm_usage_denial_#{System.unique_integer([:positive, :monotonic])}")

    {:ok, meter} = Metering.start_link(name: nil, op_log: op_log, table: table, clock: fn -> @day5 end)

    :ok = Metering.record_denial(meter, "p1", "wl", :quota)
    :ok = Metering.record_denial(meter, "p2", nil, :forbidden)
    # A call flushes the preceding casts (mailbox order).
    _ = :sys.get_state(meter)

    {:ok, ops} = SQLite.read_from(op_log, 0)
    kinds = Map.new(ops, &{&1.principal, &1})

    assert kinds["p1"].kind == :quota_enforced
    assert kinds["p1"].payload["reason"] == "quota"
    assert kinds["p2"].kind == :denied
    assert kinds["p2"].payload["reason"] == "forbidden"
  end
end
