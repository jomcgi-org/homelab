defmodule Embervm.BrickTest do
  use ExUnit.Case, async: true

  alias Embervm.{Brick, NodeCapacity}

  defp table do
    name = String.to_atom("brick_test_#{System.unique_integer([:positive])}")
    NodeCapacity.create(name)
    name
  end

  defp put_brick(t, pod, opts \\ []) do
    node = Keyword.get(opts, :node, "node")
    NodeCapacity.put(t, {node, pod}, %{
      node_id: node,
      pod_uid: pod,
      instance_id: "#{node}/#{pod}",
      size_class: Keyword.get(opts, :size_class, "8gi"),
      mem_headroom_mib: Keyword.get(opts, :mem_headroom_mib, 8_000),
      mem_reject_floor_mib: 0,
      mem_budget_mib: 8_192,
      live_vms: Keyword.get(opts, :live_vms, 0),
      max_live_vms: Keyword.get(opts, :max_live_vms, 8),
      configured_id: "configured-#{pod}",
      serving_subnet_cidr: "10.0.0.0/24"
    })
  end

  test "bricks normalizes facts and by_class buckets them" do
    t = table()
    put_brick(t, "small", live_vms: 3)
    put_brick(t, "full", live_vms: 8)

    bricks = Brick.bricks(t)
    assert Enum.find(bricks, &(&1.pod_uid == "small")).free_slots == 5
    assert Enum.find(bricks, &(&1.pod_uid == "full")).free_slots == 0
    assert Map.keys(Brick.by_class(t)) == ["8gi"]
  end

  test "to_brick carries configured id and serving subnet" do
    t = table()
    put_brick(t, "one")
    [brick] = Brick.bricks(t)
    assert brick.configured_id == "configured-one"
    assert brick.serving_subnet_cidr == "10.0.0.0/24"
  end

  test "empty table yields no bricks (fail-closed default)" do
    assert Brick.bricks(table()) == []
  end

  test "to_brick carries the admission ledger and jailer facts, defaulting a daemon that predates them" do
    t = table()
    put_brick(t, "old")
    [old] = Brick.bricks(t)
    assert old.vm_overhead_mib == 0
    assert old.admission_reported == false
    assert old.mem_admittable_mib == 0
    assert old.mem_inflight_mib == 0
    assert old.inflight_vms == 0
    assert old.mem_inflight_placed_mib == 0
    assert old.inflight_placed_vms == 0
    assert old.admission_refusals == %{}
    assert old.jailer_armed == false
    assert old.launch_modes == %{}

    new =
      Brick.to_brick(%{
        size_class: "8gi",
        mem_budget_mib: 7_936,
        vm_overhead_mib: 192,
        admission_reported: true,
        mem_admittable_mib: -64,
        mem_inflight_mib: 3_072,
        inflight_vms: 1,
        mem_inflight_placed_mib: 2_048,
        inflight_placed_vms: 1,
        admission_refusals: %{"pressure:mem" => 2},
        jailer_armed: true,
        launch_modes: %{"jailed" => 5}
      })

    assert new.vm_overhead_mib == 192
    assert new.admission_reported
    assert new.mem_admittable_mib == -64
    assert new.mem_inflight_mib == 3_072
    assert new.inflight_vms == 1
    assert new.mem_inflight_placed_mib == 2_048
    assert new.inflight_placed_vms == 1
    assert new.admission_refusals == %{"pressure:mem" => 2}
    assert new.jailer_armed
    assert new.launch_modes == %{"jailed" => 5}
  end

  test "to_brick reads a numeric fact present but nil as zero" do
    brick = Brick.to_brick(%{size_class: "4gi", mem_headroom_mib: nil, mem_budget_mib: nil, live_vms: nil})
    assert brick.mem_headroom_mib == 0
    assert brick.mem_budget_mib == 0
    assert Brick.blind?(brick)
  end

  describe "admittable_mib/1" do
    test "a daemon that reports its admission is taken at its word" do
      brick = %{admission_reported: true, mem_admittable_mib: 1_234, admits_on_reservation: true,
        mem_budget_mib: 8_000, mem_reserved_mib: 0, mem_headroom_mib: 8_000}

      assert Brick.admittable_mib(brick) == 1_234
      assert Brick.admittable_mib(%{brick | mem_admittable_mib: -10}) == -10
    end

    test "the reserved fallback is budget minus declared minus overhead for every VM plus the new one" do
      brick = %{admits_on_reservation: true, mem_budget_mib: 3_840, mem_reserved_mib: 1_024,
        vm_overhead_mib: 192, live_vms: 2, mem_headroom_mib: 50, mem_reject_floor_mib: 512}

      # 3840 - 1024 - 192 * 3; the observed headroom and floor play no part.
      assert Brick.admittable_mib(brick) == 2_240
    end

    test "the observed fallback is headroom minus the reject floor" do
      brick = %{admits_on_reservation: false, mem_headroom_mib: 4_085, mem_reject_floor_mib: 512,
        mem_budget_mib: 3_840, mem_reserved_mib: 3_072}

      assert Brick.admittable_mib(brick) == 3_573
    end

    test "charge_mib adds the per-VM overhead to the need only under the reserved model" do
      reserved = %{admits_on_reservation: true, vm_overhead_mib: 192}
      assert Brick.charge_mib(reserved, 3_072) == 3_264
      assert Brick.charged_overhead_mib(reserved) == 192

      # noded reports the overhead under observed too, but charges none.
      observed = %{reserved | admits_on_reservation: false}
      assert Brick.charge_mib(observed, 3_072) == 3_072
      assert Brick.charged_overhead_mib(observed) == 0
      assert Brick.charge_mib(%{vm_overhead_mib: 192}, 3_072) == 3_072

      assert Brick.charge_mib(%{}, 3_072) == 3_072
      assert Brick.charge_mib(%{}, nil) == 0
    end
  end

  test "wildcard is an empty class only; a classed zero budget is blind" do
    assert Brick.wildcard?(%{size_class: ""})
    assert Brick.wildcard?(%{})
    refute Brick.blind?(%{size_class: "", mem_budget_mib: 0})
    refute Brick.wildcard?(%{size_class: "8gi", mem_budget_mib: 0})
    assert Brick.blind?(%{size_class: "8gi", mem_budget_mib: 0})
    refute Brick.blind?(%{size_class: "8gi", mem_budget_mib: 1})
  end

  test "free_slots supports normalized, raw, and PoolManager shapes" do
    assert Brick.free_slots(%{free_slots: 0, max_live_vms: 8, live_vms: 1}) == 0
    assert Brick.free_slots(%{max_live_vms: 8, live_vms: 3}) == 5
    assert Brick.free_slots(%{free_slots: 2, max_live_vms: 8, live_vms: 8}) == 2
  end
end
