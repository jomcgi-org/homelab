defmodule Embervm.SyncWaitTest do
  @moduledoc """
  Exercises the sync-submit parking primitives against the application's global
  waiter registry and park-count table (both booted by the supervision tree).
  Keys are made unique per test so these stay `async` despite the shared tables.
  """
  use ExUnit.Case, async: true

  alias Embervm.SyncWait

  defp unique(prefix), do: "#{prefix}-#{System.unique_integer([:positive, :monotonic])}"

  # 1000 tries x 5ms = a 5s ceiling (was 200 tries, 1s), well under ExUnit's
  # 60s per-test timeout: the full async suite's scheduler contention can
  # occasionally push a wait past a 1s budget, so the ceiling absorbs that CI
  # load rather than flaking. The poll still returns the instant the
  # condition holds, so this only spends time on a genuine failure.
  defp wait_until(fun, tries \\ 1000)
  defp wait_until(_fun, 0), do: flunk("condition never became true")

  defp wait_until(fun, tries) do
    if fun.() do
      :ok
    else
      Process.sleep(5)
      wait_until(fun, tries - 1)
    end
  end

  test "reserve enforces the per-principal cap; release frees a slot" do
    p = unique("principal")

    assert :ok = SyncWait.reserve(p, 2)
    assert :ok = SyncWait.reserve(p, 2)
    assert {:error, :park_cap_exceeded} = SyncWait.reserve(p, 2)

    assert :ok = SyncWait.release(p)
    assert :ok = SyncWait.reserve(p, 2)
  end

  test "release clamps at zero and never goes negative" do
    p = unique("principal")
    assert :ok = SyncWait.release(p)
    assert :ok = SyncWait.release(p)
    # After spurious releases the count is still 0, so a fresh reserve at cap 1
    # succeeds exactly once.
    assert :ok = SyncWait.reserve(p, 1)
    assert {:error, :park_cap_exceeded} = SyncWait.reserve(p, 1)
  end

  test "await wakes on a terminal notify with the settled state" do
    t = unique("task")
    parent = self()

    spawn(fn ->
      send(parent, {:result, SyncWait.await(t, 1_000, fn -> :pending end)})
    end)

    # Wait until the waiter has registered, then notify.
    wait_until(fn -> Registry.lookup(Embervm.TaskWaiters, t) != [] end)
    SyncWait.notify(t, :succeeded)

    assert_receive {:result, {:terminal, :succeeded}}, 1_000
  end

  test "await times out when nothing settles" do
    t = unique("task")
    assert :timeout = SyncWait.await(t, 40, fn -> :pending end)
  end

  test "await short-circuits when the task is already terminal (submit/settle race)" do
    t = unique("task")
    # No notify is ever sent; the re-check after registration catches the task
    # that settled between submit and registration.
    assert {:terminal, :dead_lettered} =
             SyncWait.await(t, 5_000, fn -> {:terminal, :dead_lettered} end)
  end

  test "await unregisters the waiter on return (no leak for a reused process)" do
    t = unique("task")
    assert :timeout = SyncWait.await(t, 20, fn -> :pending end)
    assert Registry.lookup(Embervm.TaskWaiters, t) == []
  end
end
