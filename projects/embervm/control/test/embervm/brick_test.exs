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

  test "free_slots supports normalized, raw, and PoolManager shapes" do
    assert Brick.free_slots(%{free_slots: 0, max_live_vms: 8, live_vms: 1}) == 0
    assert Brick.free_slots(%{max_live_vms: 8, live_vms: 3}) == 5
    assert Brick.free_slots(%{free_slots: 2, max_live_vms: 8, live_vms: 8}) == 2
  end
end
