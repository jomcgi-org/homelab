defmodule Embervm.LineageFenceTest do
  # The fence table is a single named ETS table owned by the application's
  # supervision tree, so these tests share it with every other test module and
  # use unique lineage ids rather than their own server.
  use ExUnit.Case, async: true

  alias Embervm.LineageFence

  defp lineage, do: "lf-#{System.unique_integer([:positive])}"

  test "a restore claim blocks the GC until released, then the GC may claim" do
    l = lineage()
    assert :ok = LineageFence.claim_restore(l)
    assert LineageFence.held?(l, :restore)
    assert {:error, :restore_in_flight} = LineageFence.claim_gc(l)
    refute LineageFence.held?(l, :gc)

    assert :ok = LineageFence.release_restore(l)
    refute LineageFence.held?(l, :restore)
    assert :ok = LineageFence.claim_gc(l)
    assert LineageFence.held?(l, :gc)
    assert :ok = LineageFence.release_gc(l)
  end

  test "a GC claim denies a restore until released" do
    l = lineage()
    assert :ok = LineageFence.claim_gc(l)
    assert {:error, :gc_in_progress} = LineageFence.claim_restore(l)
    # The denied restore left no claim of its own behind.
    refute LineageFence.held?(l, :restore)
    assert LineageFence.held?(l, :gc)

    assert :ok = LineageFence.release_gc(l)
    assert :ok = LineageFence.claim_restore(l)
    assert :ok = LineageFence.release_restore(l)
  end

  test "a second restore of the same lineage is refused while the first holds it" do
    l = lineage()
    assert :ok = LineageFence.claim_restore(l)
    assert {:error, :restore_in_flight} = LineageFence.claim_restore(l)
    assert :ok = LineageFence.release_restore(l)
  end

  test "releasing the other role's claim is a no-op" do
    l = lineage()
    assert :ok = LineageFence.claim_gc(l)
    assert :ok = LineageFence.release_restore(l)
    assert LineageFence.held?(l, :gc)
    assert :ok = LineageFence.release_gc(l)
    refute LineageFence.held?(l, :gc)
  end

  test "nil and empty lineages are always claimable and never recorded" do
    assert :ok = LineageFence.claim_restore(nil)
    assert :ok = LineageFence.claim_restore("")
    assert :ok = LineageFence.claim_gc(nil)
    assert :ok = LineageFence.claim_gc("")
    assert :ok = LineageFence.release_restore(nil)
    assert :ok = LineageFence.release_gc("")
  end

  test "exactly one of two racing claimants wins" do
    l = lineage()
    parent = self()

    tasks =
      for role <- [:restore, :gc] do
        Task.async(fn ->
          result =
            if role == :restore, do: LineageFence.claim_restore(l), else: LineageFence.claim_gc(l)

          send(parent, {role, result})
          result
        end)
      end

    results = Enum.map(tasks, &Task.await/1)
    assert Enum.count(results, &(&1 == :ok)) == 1
    LineageFence.release_restore(l)
    LineageFence.release_gc(l)
  end

  test "clear/1 drops only that role's claims" do
    a = lineage()
    b = lineage()
    assert :ok = LineageFence.claim_restore(a)
    assert :ok = LineageFence.claim_gc(b)
    assert :ok = LineageFence.clear(:restore)
    refute LineageFence.held?(a, :restore)
    assert LineageFence.held?(b, :gc)
    assert :ok = LineageFence.release_gc(b)
  end
end
