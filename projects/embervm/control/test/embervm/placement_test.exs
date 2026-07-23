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
      free_slots: Keyword.get(opts, :free_slots, 4),
      workloads: Keyword.get(opts, :workloads, %{})
    }
  end

  # A workloads submap advertising `wl` at the given base_state (default the READY
  # atom the registry projects). `:absent` omits the workload entry entirely.
  defp advertising(wl, base_state \\ :BASE_BUILD_STATE_READY)
  defp advertising(_wl, :absent), do: %{}
  defp advertising(wl, base_state), do: %{wl => %{base_state: base_state, snapshot_ref: "snap"}}

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

  describe "base_ready?/2" do
    test "true when the workload's base_state is the READY atom" do
      assert Placement.base_ready?(brick(workloads: advertising("wl")), "wl")
    end

    test "true for the defensive integer READY form (3)" do
      assert Placement.base_ready?(brick(workloads: advertising("wl", 3)), "wl")
    end

    test "false when the workload entry is absent (fresh, not yet advertised)" do
      refute Placement.base_ready?(brick(workloads: advertising("wl", :absent)), "wl")
    end

    test "false when base_state is a non-READY state (still building)" do
      refute Placement.base_ready?(brick(workloads: advertising("wl", :BASE_BUILD_STATE_BUILDING)), "wl")
    end

    test "false when the workloads map is missing entirely" do
      refute Placement.base_ready?(%{instance_id: "n/x"}, "wl")
    end
  end

  describe "pick_ready/3 (adds the base-readiness gate to pick/3)" do
    test "skips a mem-eligible brick that has NOT advertised the base, picks the ready one" do
      # not_ready: mem-eligible + free slot, but base_state absent (fresh/rolled).
      not_ready =
        brick(instance_id: "n/fresh", mem_headroom_mib: 16_000, mem_budget_mib: 16_384, workloads: advertising("wl", :absent))

      # ready: mem-eligible AND advertises the base READY.
      ready =
        brick(instance_id: "n/ready", mem_headroom_mib: 16_000, mem_budget_mib: 16_384, workloads: advertising("wl"))

      for _key <- ~w(a b c d e f) do
        assert Placement.pick_ready([not_ready, ready], "wl", 4_000).instance_id == "n/ready"
      end
    end

    test "only base-ready instance is too small AND only mem-eligible ones are not ready -> nil (retryable no-eligible)" do
      # base-ready but too small for the need (classed 2gi, 100 MiB headroom).
      ready_small =
        brick(instance_id: "n/small", size_class: "2gi", mem_headroom_mib: 100, mem_budget_mib: 2_048, workloads: advertising("wl"))

      # mem-eligible (big) but has NOT advertised the base yet.
      big_not_ready =
        brick(instance_id: "n/big", mem_headroom_mib: 16_000, mem_budget_mib: 16_384, workloads: advertising("wl", :absent))

      assert Placement.pick_ready([ready_small, big_not_ready], "wl", 4_000) == nil
    end

    test "single-instance fleet: the sole advertised instance is picked (output-equivalent)" do
      sole = brick(instance_id: "n/only", workloads: advertising("wl"))
      assert Placement.pick_ready([sole], "wl", 4_000).instance_id == "n/only"
    end

    test "wildcard DS that advertises the base is always the (only) candidate" do
      ds =
        brick(instance_id: "n/ds", size_class: "", mem_headroom_mib: 0, mem_budget_mib: 0, workloads: advertising("wl"))

      assert Placement.pick_ready([ds], "wl", 16_000).instance_id == "n/ds"
    end

    test "a wildcard DS that has NOT advertised the base is skipped -> nil" do
      ds =
        brick(instance_id: "n/ds", size_class: "", mem_headroom_mib: 0, mem_budget_mib: 0, workloads: advertising("wl", :absent))

      assert Placement.pick_ready([ds], "wl", 16_000) == nil
    end
  end

  describe "candidates_ready/3 (the reject/retry frontier)" do
    test "HEAD == pick_ready/3 result (single-attempt and retry pick the same brick first)" do
      # Several ready+eligible bricks; the ordered candidate head must equal the
      # deterministic single pick for the same key, so gate-off is output-equivalent.
      bricks =
        for id <- ~w(n/a n/b n/c n/d),
            do: brick(instance_id: id, workloads: advertising("wl"))

      chosen = Placement.pick_ready(bricks, "wl", 4_000)
      [head | _] = Placement.candidates_ready(bricks, "wl", 4_000)
      assert head.instance_id == chosen.instance_id
    end

    test "returns ALL ready+eligible bricks (the full frontier), too-small ones excluded" do
      big1 = brick(instance_id: "n/b1", workloads: advertising("wl"))
      big2 = brick(instance_id: "n/b2", workloads: advertising("wl"))

      small =
        brick(
          instance_id: "n/small",
          size_class: "2gi",
          mem_headroom_mib: 100,
          mem_budget_mib: 2_048,
          workloads: advertising("wl")
        )

      ids =
        [big1, big2, small]
        |> Placement.candidates_ready("wl", 4_000)
        |> Enum.map(& &1.instance_id)
        |> Enum.sort()

      assert ids == ["n/b1", "n/b2"]
    end

    test "a not-base-ready brick is excluded from the frontier" do
      ready = brick(instance_id: "n/ready", workloads: advertising("wl"))
      not_ready = brick(instance_id: "n/nr", workloads: advertising("wl", :absent))

      ids =
        [ready, not_ready]
        |> Placement.candidates_ready("wl", 4_000)
        |> Enum.map(& &1.instance_id)

      assert ids == ["n/ready"]
    end

    test "no eligible brick yields an empty frontier" do
      small = brick(instance_id: "n/s", size_class: "2gi", mem_headroom_mib: 100, mem_budget_mib: 2_048, workloads: advertising("wl"))
      assert Placement.candidates_ready([small], "wl", 4_000) == []
    end

    test "the whole frontier is a permutation of the ready bricks (no dropped or duplicated candidate)" do
      bricks = for id <- ~w(n/a n/b n/c), do: brick(instance_id: id, workloads: advertising("wl"))

      frontier_ids =
        bricks
        |> Placement.candidates_ready("wl", 4_000)
        |> Enum.map(& &1.instance_id)

      assert Enum.sort(frontier_ids) == ["n/a", "n/b", "n/c"]
      assert length(frontier_ids) == length(Enum.uniq(frontier_ids))
    end
  end
end
