defmodule Embervm.ActivatorSplicesTest do
  @moduledoc """
  Exercises Embervm.ActivatorSplices (the R5 Task 8 per-workload live-splice
  counter): incr/decr bracket, the zero-clamp (an over-decrement never fakes
  idleness), live?/2, and the absent-table no-op (a sweeper wired without an
  activator reads no-splice).
  """
  use ExUnit.Case, async: true

  alias Embervm.ActivatorSplices

  defp start_counter do
    table = :"splices_#{System.unique_integer([:positive])}"
    _ = start_supervised!({ActivatorSplices, [table: table]})
    table
  end

  test "incr then decr brackets a splice back to not-live" do
    t = start_counter()

    refute ActivatorSplices.live?(t, "grp-a")

    ActivatorSplices.incr(t, "grp-a")
    assert ActivatorSplices.live?(t, "grp-a")

    ActivatorSplices.decr(t, "grp-a")
    refute ActivatorSplices.live?(t, "grp-a")
  end

  test "concurrent splices: still live until the LAST decrements" do
    t = start_counter()

    ActivatorSplices.incr(t, "grp-a")
    ActivatorSplices.incr(t, "grp-a")
    assert ActivatorSplices.live?(t, "grp-a")

    ActivatorSplices.decr(t, "grp-a")
    assert ActivatorSplices.live?(t, "grp-a"), "one splice still open"

    ActivatorSplices.decr(t, "grp-a")
    refute ActivatorSplices.live?(t, "grp-a")
  end

  test "an over-decrement clamps at zero (never fakes idleness by going negative)" do
    t = start_counter()

    # Decrement with no prior incr: clamps at 0, still not live.
    ActivatorSplices.decr(t, "grp-a")
    refute ActivatorSplices.live?(t, "grp-a")

    # A single incr after the spurious decr reads live (not cancelled by a negative).
    ActivatorSplices.incr(t, "grp-a")
    assert ActivatorSplices.live?(t, "grp-a")
  end

  test "distinct workloads count independently" do
    t = start_counter()

    ActivatorSplices.incr(t, "grp-a")
    assert ActivatorSplices.live?(t, "grp-a")
    refute ActivatorSplices.live?(t, "grp-b")
  end

  test "an absent table reads not-live and no-ops incr/decr (sweeper without an activator)" do
    absent = :"never_started_#{System.unique_integer([:positive])}"

    refute ActivatorSplices.live?(absent, "grp-a")
    assert :ok = ActivatorSplices.incr(absent, "grp-a")
    assert :ok = ActivatorSplices.decr(absent, "grp-a")
    refute ActivatorSplices.live?(absent, "grp-a")
  end
end
