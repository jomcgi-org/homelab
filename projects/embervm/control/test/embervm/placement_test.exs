defmodule Embervm.PlacementTest do
  @moduledoc """
  Exercises Embervm.Placement (brick co-location foundation, Step 5): the ONE shared
  eligibility predicate + deterministic pick every NEW-placement path (dispatcher
  miss tier, session/serving create, the wake cold pick) reuses so the memory gate
  can never drift.

  Covers the DS-wildcard-always-eligible rule (empty class OR zero budget), the
  headroom gate for a classed brick, the free-slot gate, and that `pick/3` skips a
  too-small brick and spreads deterministically over the eligible ones.
  """
  use ExUnit.Case, async: true

  alias Embervm.Placement

  defp brick(opts) do
    %{
      instance_id: Keyword.get(opts, :instance_id, "node/pod"),
      node_id: Keyword.get(opts, :node_id, "node"),
      size_class: Keyword.get(opts, :size_class, "8gi"),
      mem_headroom_mib: Keyword.get(opts, :mem_headroom_mib, 8_000),
      mem_budget_mib: Keyword.get(opts, :mem_budget_mib, 8_192),
      free_slots: Keyword.get(opts, :free_slots, 4)
    }
  end

  describe "wildcard?/1" do
    test "empty size_class is a wildcard" do
      assert Placement.wildcard?(brick(size_class: "", mem_budget_mib: 8_192))
    end

    test "zero mem_budget_mib is a wildcard even with a non-empty class" do
      assert Placement.wildcard?(brick(size_class: "8gi", mem_budget_mib: 0))
    end

    test "a classed brick with a real budget is not a wildcard" do
      refute Placement.wildcard?(brick(size_class: "8gi", mem_budget_mib: 8_192))
    end
  end

  describe "mem_eligible?/2" do
    test "a wildcard is eligible for any need, even at zero headroom" do
      assert Placement.mem_eligible?(brick(size_class: "", mem_headroom_mib: 0, mem_budget_mib: 0), 4_000)
    end

    test "a classed brick needs headroom >= need" do
      assert Placement.mem_eligible?(brick(mem_headroom_mib: 8_000), 4_000)
      refute Placement.mem_eligible?(brick(mem_headroom_mib: 100), 4_000)
    end
  end

  describe "eligible?/2 (folds in the free-slot gate)" do
    test "a mem-eligible brick with a free slot is eligible" do
      assert Placement.eligible?(brick(mem_headroom_mib: 8_000, free_slots: 1), 4_000)
    end

    test "a mem-eligible brick with no free slot is NOT eligible" do
      refute Placement.eligible?(brick(mem_headroom_mib: 8_000, free_slots: 0), 4_000)
    end

    test "a wildcard with no free slot is NOT eligible (slots still gate)" do
      refute Placement.eligible?(brick(size_class: "", mem_budget_mib: 0, free_slots: 0), 4_000)
    end
  end

  describe "pick/3" do
    test "skips a too-small classed brick and lands on the big one for every key" do
      small = brick(instance_id: "n/small", size_class: "2gi", mem_headroom_mib: 100, mem_budget_mib: 2_048)
      big = brick(instance_id: "n/big", size_class: "8gi", mem_headroom_mib: 8_000, mem_budget_mib: 8_192)

      for key <- ~w(a b c d e f) do
        picked = Placement.pick([small, big], key, 4_000)
        assert picked.instance_id == "n/big"
      end
    end

    test "the wildcard DS is always a candidate" do
      ds = brick(instance_id: "n/ds", size_class: "", mem_headroom_mib: 0, mem_budget_mib: 0)
      assert Placement.pick([ds], "k", 16_000).instance_id == "n/ds"
    end

    test "no eligible brick yields nil" do
      small = brick(size_class: "2gi", mem_headroom_mib: 100, mem_budget_mib: 2_048)
      assert Placement.pick([small], "k", 4_000) == nil
    end

    test "distinct keys spread across two eligible bricks" do
      a = brick(instance_id: "n/a", mem_headroom_mib: 8_000, mem_budget_mib: 8_192)
      b = brick(instance_id: "n/b", mem_headroom_mib: 8_000, mem_budget_mib: 8_192)

      picked = for k <- ~w(k1 k2 k3 k4 k5 k6 k7 k8), do: Placement.pick([a, b], k, 1_000).instance_id
      # Both bricks are hit at least once across the key space (deterministic spread).
      assert "n/a" in picked and "n/b" in picked
    end
  end
end
