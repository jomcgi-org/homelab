defmodule Embervm.SchedulerTest do
  use ExUnit.Case, async: true

  alias Embervm.{Brick, Scheduler}
  alias Embervm.Scheduler.{Request, Score}

  defp brick(opts \\ []) do
    %{
      instance_id: Keyword.get(opts, :instance_id, "node/pod"),
      node_id: Keyword.get(opts, :node_id, "node"),
      configured_id: Keyword.get(opts, :configured_id, "node"),
      size_class: Keyword.get(opts, :size_class, "8gi"),
      mem_headroom_mib: Keyword.get(opts, :mem_headroom_mib, 8_000),
      mem_reject_floor_mib: Keyword.get(opts, :mem_reject_floor_mib, 0),
      mem_budget_mib: Keyword.get(opts, :mem_budget_mib, 8_192),
      live_vms: Keyword.get(opts, :live_vms, 0),
      max_live_vms: Keyword.get(opts, :max_live_vms, 8),
      free_slots: Keyword.get(opts, :free_slots, 4),
      serving_subnet_cidr: Keyword.get(opts, :serving_subnet_cidr, ""),
      workloads: Keyword.get(opts, :workloads, %{})
    }
  end

  defp ready(wl, fields \\ %{}), do: %{wl => Map.merge(%{base_state: :BASE_BUILD_STATE_READY}, fields)}

  test "eligible and memory predicates preserve their semantics" do
    refute Scheduler.mem_eligible?(brick(mem_headroom_mib: 1_933, mem_reject_floor_mib: 512), 1_536)
    assert Scheduler.mem_eligible?(brick(mem_headroom_mib: 2_048, mem_reject_floor_mib: 512), 1_536)
    assert Scheduler.eligible?(brick(free_slots: 1), 4_000)
    refute Scheduler.eligible?(brick(free_slots: 0), 4_000)
  end

  test "mem eligible accepts the exact need plus floor boundary" do
    assert Scheduler.mem_eligible?(brick(mem_headroom_mib: 2_048, mem_reject_floor_mib: 512), 1_536)
  end

  test "wildcard is mem eligible for any need at zero headroom and any floor" do
    wildcard = brick(size_class: "", mem_budget_mib: 0, mem_headroom_mib: 0, mem_reject_floor_mib: 512)
    assert Scheduler.mem_eligible?(wildcard, 16_000)
  end

  test "wildcard with no free slot is not eligible" do
    refute Scheduler.eligible?(brick(size_class: "", mem_budget_mib: 0, free_slots: 0), 4_000)
  end

  test "node anchor filters independently" do
    bricks = [brick(instance_id: "a", node_id: "n1"), brick(instance_id: "b", node_id: "n2")]
    assert Enum.map(Scheduler.place(%Request{bricks: bricks, node_id: "n2"}), & &1.instance_id) == ["b"]
  end

  test "subnet requirement filters empty and missing values" do
    bricks = [brick(instance_id: "a", serving_subnet_cidr: "10.0.0.0/24"), brick(instance_id: "b")]
    assert Enum.map(Scheduler.place(%Request{bricks: bricks, require_subnet: true}), & &1.instance_id) == ["a"]
  end

  test "slots filter uses Brick.free_slots" do
    bricks = [brick(instance_id: "full", free_slots: 0), brick(instance_id: "open", free_slots: 1)]
    assert Enum.map(Scheduler.place(%Request{bricks: bricks}), & &1.instance_id) == ["open"]
  end

  test "nil memory need skips the gate, while zero still applies the floor" do
    low = brick(mem_headroom_mib: 1, mem_reject_floor_mib: 512)
    assert Scheduler.place(%Request{bricks: [low], need_mib: nil}) == [low]
    assert Scheduler.place(%Request{bricks: [low], need_mib: 0}) == []
  end

  test "base none applies no gate" do
    brick = brick(workloads: %{})
    assert Scheduler.place(%Request{bricks: [brick], workload: "wl", base: :none}) == [brick]
  end

  test "base ready requires the workload base" do
    ready_brick = brick(workloads: ready("wl"))
    not_ready = brick(instance_id: "b", workloads: %{"wl" => %{base_state: :BASE_BUILD_STATE_BUILDING}})
    assert Enum.map(Scheduler.place(%Request{bricks: [not_ready, ready_brick], workload: "wl", base: :ready}), & &1.instance_id) == ["node/pod"]
  end

  test "base ready accepts the defensive integer READY form" do
    assert Scheduler.base_ready?(brick(workloads: ready("wl", %{base_state: 3})), "wl")
  end

  test "base ready is false when workloads is missing" do
    refute Scheduler.base_ready?(%{instance_id: "node/pod"}, "wl")
  end

  test "base ready is false when the workload entry is absent" do
    refute Scheduler.base_ready?(brick(workloads: %{}), "wl")
  end

  test "base-gated placement distinguishes a provisioning gap from capacity" do
    missing_base = brick(configured_id: "node-base", workloads: %{})
    no_capacity = brick(configured_id: "node-full", free_slots: 0, workloads: %{})

    request = fn bricks ->
      Scheduler.place_with_demand(%Request{
        bricks: bricks,
        workload: "wl",
        need_mib: 512,
        base: {:ready, :snapshot_ref}
      })
    end

    assert {:error, {:base_missing, "node-base"}} = request.([missing_base])
    assert {:error, :capacity} = request.([no_capacity])
  end

  test "snapshot and serving base variants require a non-empty field" do
    snapshot = brick(instance_id: "snapshot", workloads: ready("wl", %{snapshot_ref: "snap"}))
    serving = brick(instance_id: "serving", workloads: ready("wl", %{serving_image_ref: "image"}))
    assert Scheduler.place(%Request{bricks: [snapshot], workload: "wl", base: {:ready, :snapshot_ref}}) == [snapshot]
    assert Scheduler.place(%Request{bricks: [snapshot], workload: "wl", base: {:ready, :serving_image_ref}}) == []
    assert Scheduler.place(%Request{bricks: [serving], workload: "wl", base: {:ready, :serving_image_ref}}) == [serving]
  end

  test "place returns ordered brick maps" do
    empty = brick(instance_id: "a", live_vms: 0, max_live_vms: 8)
    fuller = brick(instance_id: "b", live_vms: 4, max_live_vms: 8)
    assert Score.score(empty) != Score.score(fuller)

    bricks = [empty, fuller]
    assert [head | _] = Scheduler.place(%Request{bricks: bricks, key: "wl"})
    assert head.instance_id == "b"
  end

  describe "place translated from pick/3" do
    test "session fleet placement escapes a too-small rendezvous winner" do
      too_small = brick(instance_id: "node-a/small", configured_id: "node-a", size_class: "2gi", mem_headroom_mib: 100, mem_budget_mib: 2_048)
      fits = brick(instance_id: "node-b/big", configured_id: "node-b", size_class: "8gi", mem_headroom_mib: 8_000, mem_budget_mib: 8_192)

      assert [%{configured_id: "node-b", instance_id: "node-b/big"}] =
               Scheduler.place(%Request{bricks: [too_small, fits], workload: "wl", key: "wl", need_mib: 4_000, base: :none})
    end

    test "serving frontier entries retain each brick's serving image ref" do
      a = brick(instance_id: "node-a/pod", configured_id: "node-a", serving_subnet_cidr: "10.0.0.0/24", workloads: ready("wl", %{serving_image_ref: "vendor-a"}))
      b = brick(instance_id: "node-b/pod", configured_id: "node-b", serving_subnet_cidr: "10.0.1.0/24", workloads: ready("wl", %{serving_image_ref: "vendor-b"}))

      frontier =
        Scheduler.place(%Request{bricks: [a, b], workload: "wl", key: "wl", need_mib: 512, require_subnet: true, base: {:ready, :serving_image_ref}})
        |> Enum.map(fn brick -> %{instance_id: brick.instance_id, node_id: brick.configured_id, base_ref: brick.workloads["wl"].serving_image_ref} end)

      assert Enum.sort_by(frontier, & &1.node_id) == [
               %{instance_id: "node-a/pod", node_id: "node-a", base_ref: "vendor-a"},
               %{instance_id: "node-b/pod", node_id: "node-b", base_ref: "vendor-b"}
             ]
    end

    test "stateful fresh placement excludes a brick too small for the workload" do
      small = brick(instance_id: "node-a/small", configured_id: "node-a", size_class: "2gi", mem_headroom_mib: 100, mem_budget_mib: 2_048, serving_subnet_cidr: "10.0.0.0/24")
      big = brick(instance_id: "node-b/big", configured_id: "node-b", size_class: "8gi", mem_headroom_mib: 8_000, mem_budget_mib: 8_192, serving_subnet_cidr: "10.0.1.0/24")

      assert [%{configured_id: "node-b"}] =
               Scheduler.place(%Request{bricks: [small, big], workload: "wl", key: "wl", need_mib: 4_000, require_subnet: true, base: :none})
    end

    test "skips a too-small classed brick and lands on the big one for every key" do
      small = brick(instance_id: "n/small", size_class: "2gi", mem_headroom_mib: 100, mem_budget_mib: 2_048)
      big = brick(instance_id: "n/big", size_class: "8gi", mem_headroom_mib: 8_000, mem_budget_mib: 8_192)

      for key <- ~w(a b c d e f) do
        assert [picked | _] = Scheduler.place(%Request{bricks: [small, big], key: key, need_mib: 4_000})
        assert picked.instance_id == "n/big"
      end
    end

    test "the wildcard DS is always a candidate" do
      ds = brick(instance_id: "n/ds", size_class: "", mem_headroom_mib: 0, mem_budget_mib: 0)
      assert [picked] = Scheduler.place(%Request{bricks: [ds], key: "k", need_mib: 16_000})
      assert picked.instance_id == "n/ds"
    end

    test "no eligible brick yields an empty list" do
      small = brick(size_class: "2gi", mem_headroom_mib: 100, mem_budget_mib: 2_048)
      assert Scheduler.place(%Request{bricks: [small], key: "k", need_mib: 4_000}) == []
    end

    test "distinct keys spread across two eligible bricks" do
      a = brick(instance_id: "n/a", mem_headroom_mib: 8_000, mem_budget_mib: 8_192)
      b = brick(instance_id: "n/b", mem_headroom_mib: 8_000, mem_budget_mib: 8_192)

      picked =
        for key <- ~w(k1 k2 k3 k4 k5 k6 k7 k8),
            [head | _] <- [Scheduler.place(%Request{bricks: [a, b], key: key, need_mib: 1_000})],
            do: head.instance_id

      assert "n/a" in picked and "n/b" in picked
    end
  end

  describe "place translated from pick_ready/3" do
    test "skips a mem-eligible brick that has NOT advertised the base, picks the ready one" do
      not_ready = brick(instance_id: "n/fresh", workloads: %{})
      ready_brick = brick(instance_id: "n/ready", workloads: ready("wl"))
      request = %Request{bricks: [not_ready, ready_brick], workload: "wl", key: "wl", need_mib: 4_000, base: :ready}

      for key <- ~w(a b c d e f) do
        assert [%{instance_id: "n/ready"}] = Scheduler.place(%{request | key: key})
      end
    end

    test "only base-ready instance is too small and only mem-eligible ones are not ready yields an empty list" do
      ready_small =
        brick(instance_id: "n/small", size_class: "2gi", mem_headroom_mib: 100, mem_budget_mib: 2_048, workloads: ready("wl"))

      big_not_ready = brick(instance_id: "n/big", workloads: %{})
      request = %Request{bricks: [ready_small, big_not_ready], workload: "wl", key: "wl", need_mib: 4_000, base: :ready}
      assert Scheduler.place(request) == []
    end

    test "single-instance fleet picks the sole advertised instance" do
      sole = brick(instance_id: "n/only", workloads: ready("wl"))
      request = %Request{bricks: [sole], workload: "wl", key: "wl", need_mib: 4_000, base: :ready}
      assert [%{instance_id: "n/only"}] = Scheduler.place(request)
    end

    test "wildcard DS that advertises the base is always the only candidate" do
      ds = brick(instance_id: "n/ds", size_class: "", mem_headroom_mib: 0, mem_budget_mib: 0, workloads: ready("wl"))
      request = %Request{bricks: [ds], workload: "wl", key: "wl", need_mib: 16_000, base: :ready}
      assert [%{instance_id: "n/ds"}] = Scheduler.place(request)
    end

    test "a wildcard DS that has NOT advertised the base is skipped" do
      ds = brick(instance_id: "n/ds", size_class: "", mem_headroom_mib: 0, mem_budget_mib: 0, workloads: %{})
      request = %Request{bricks: [ds], workload: "wl", key: "wl", need_mib: 16_000, base: :ready}
      assert Scheduler.place(request) == []
    end
  end

  describe "place translated from candidates_ready/3" do
    test "returns all ready and eligible bricks, excluding too-small ones" do
      big1 = brick(instance_id: "n/b1", workloads: ready("wl"))
      big2 = brick(instance_id: "n/b2", workloads: ready("wl"))
      small = brick(instance_id: "n/small", size_class: "2gi", mem_headroom_mib: 100, mem_budget_mib: 2_048, workloads: ready("wl"))
      request = %Request{bricks: [big1, big2, small], workload: "wl", key: "wl", need_mib: 4_000, base: :ready}

      ids = request |> Scheduler.place() |> Enum.map(& &1.instance_id) |> Enum.sort()
      assert ids == ["n/b1", "n/b2"]
    end

    test "a not-base-ready brick is excluded from the frontier" do
      ready_brick = brick(instance_id: "n/ready", workloads: ready("wl"))
      not_ready = brick(instance_id: "n/nr", workloads: %{})
      request = %Request{bricks: [ready_brick, not_ready], workload: "wl", key: "wl", need_mib: 4_000, base: :ready}
      assert Enum.map(Scheduler.place(request), & &1.instance_id) == ["n/ready"]
    end

    test "no eligible brick yields an empty frontier" do
      small = brick(instance_id: "n/s", size_class: "2gi", mem_headroom_mib: 100, mem_budget_mib: 2_048, workloads: ready("wl"))
      request = %Request{bricks: [small], workload: "wl", key: "wl", need_mib: 4_000, base: :ready}
      assert Scheduler.place(request) == []
    end

    test "the whole frontier is a permutation of the ready bricks" do
      bricks = for id <- ~w(n/a n/b n/c), do: brick(instance_id: id, workloads: ready("wl"))
      request = %Request{bricks: bricks, workload: "wl", key: "wl", need_mib: 4_000, base: :ready}
      frontier_ids = Scheduler.place(request) |> Enum.map(& &1.instance_id)

      assert Enum.sort(frontier_ids) == ["n/a", "n/b", "n/c"]
      assert length(frontier_ids) == length(Enum.uniq(frontier_ids))
    end

    # Guards against anyone reintroducing a separate single-pick path beside the
    # frontier. They diverged once (the dispatcher committed the scored winner but
    # Primed the head of a differently ordered frontier), so the head being the
    # commit is now true by construction and this pins it.
    test "the head of the frontier is the fuller brick, not input order or hash" do
      fuller = brick(instance_id: "n/full", live_vms: 4, max_live_vms: 8, workloads: ready("wl"))
      emptier = brick(instance_id: "n/empty", live_vms: 0, max_live_vms: 8, workloads: ready("wl"))
      request = %Request{bricks: [emptier, fuller], workload: "wl", key: "wl", need_mib: 1_000, base: :ready}

      # The two must actually score apart, otherwise the sticky hash decides and
      # this test would pass with the score function deleted.
      assert Score.score(fuller) > Score.score(emptier)

      assert [head | _] = Scheduler.place(request)
      assert head.instance_id == "n/full"
      # Reversing the input must not change the answer.
      assert [%{instance_id: "n/full"} | _] = Scheduler.place(%{request | bricks: [fuller, emptier]})
    end
  end

  test "wildcard remains a Brick property" do
    assert Brick.wildcard?(brick(size_class: "", mem_budget_mib: 8_192))
    refute Brick.wildcard?(brick(size_class: "8gi", mem_budget_mib: 8_192))
  end
end
