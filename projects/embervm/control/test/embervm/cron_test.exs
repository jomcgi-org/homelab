defmodule Embervm.CronTest do
  @moduledoc """
  Unit tests for the dependency-free 5-field cron parser + next-fire calculator
  (`Embervm.Cron`). Everything is pure, so these are plain assertions with fixed
  UTC datetimes, no processes or clocks.
  """
  use ExUnit.Case, async: true

  alias Embervm.Cron

  defp dt(y, mo, d, h, mi), do: DateTime.new!(Date.new!(y, mo, d), Time.new!(h, mi, 0), "Etc/UTC")

  test "parses the five field forms" do
    assert {:ok, _} = Cron.parse("* * * * *")
    assert {:ok, _} = Cron.parse("*/5 0 1 1 0")
    assert {:ok, _} = Cron.parse("0,15,30,45 9-17 * * 1-5")
    assert {:error, _} = Cron.parse("* * * *")
    assert {:error, _} = Cron.parse("60 * * * *")
    assert {:error, _} = Cron.parse("* 24 * * *")
    assert {:error, _} = Cron.parse("bad")
  end

  test "next fires at the next matching minute, strictly after now" do
    {:ok, cron} = Cron.parse("*/15 * * * *")
    # 10:07 -> next quarter hour is 10:15.
    assert {:ok, next} = Cron.next(cron, dt(2026, 7, 13, 10, 7))
    assert next == dt(2026, 7, 13, 10, 15)

    # Exactly on a boundary still advances (strictly after): 10:15 -> 10:30.
    assert {:ok, later} = Cron.next(cron, dt(2026, 7, 13, 10, 15))
    assert later == dt(2026, 7, 13, 10, 30)
  end

  test "next rolls the hour and day" do
    {:ok, cron} = Cron.parse("0 * * * *")
    assert {:ok, next} = Cron.next(cron, dt(2026, 7, 13, 10, 30))
    assert next == dt(2026, 7, 13, 11, 0)

    {:ok, daily} = Cron.parse("30 2 * * *")
    assert {:ok, tomorrow} = Cron.next(daily, dt(2026, 7, 13, 3, 0))
    assert tomorrow == dt(2026, 7, 14, 2, 30)
  end

  test "day-of-week matches (0 = Sunday)" do
    # 2026-07-13 is a Monday. "0 9 * * 1" = 09:00 on Mondays.
    {:ok, cron} = Cron.parse("0 9 * * 1")
    assert {:ok, next} = Cron.next(cron, dt(2026, 7, 13, 8, 0))
    assert next == dt(2026, 7, 13, 9, 0)

    # From Monday 10:00, the next Monday 09:00 is a week later.
    assert {:ok, next_week} = Cron.next(cron, dt(2026, 7, 13, 10, 0))
    assert next_week == dt(2026, 7, 20, 9, 0)
  end

  test "Vixie rule: restricted dom AND dow match on EITHER" do
    # "0 0 13 * 5" = midnight on the 13th OR any Friday.
    {:ok, cron} = Cron.parse("0 0 13 * 5")
    # 2026-07-13 is a Monday: matches on day-of-month 13.
    assert Cron.matches?(cron, dt(2026, 7, 13, 0, 0))
    # 2026-07-17 is a Friday: matches on day-of-week even though it is the 17th.
    assert Cron.matches?(cron, dt(2026, 7, 17, 0, 0))
    # 2026-07-14 (Tuesday, not the 13th): no match.
    refute Cron.matches?(cron, dt(2026, 7, 14, 0, 0))
  end
end
