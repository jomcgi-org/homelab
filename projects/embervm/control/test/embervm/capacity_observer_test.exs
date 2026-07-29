defmodule Embervm.CapacityObserverTest do
  use ExUnit.Case, async: true

  alias Embervm.{CapacityObserver, NodeCapacity}

  defp table(prefix) do
    name = String.to_atom("#{prefix}_#{System.unique_integer([:positive])}")
    NodeCapacity.create(name)
    name
  end

  defp put_brick(capacity_table, pod, opts \\ []) do
    node_id = Keyword.get(opts, :node_id, "node-#{pod}")

    NodeCapacity.put(capacity_table, {node_id, pod}, %{
      node_id: node_id,
      pod_uid: pod,
      instance_id: "#{node_id}/#{pod}",
      size_class: Keyword.get(opts, :size_class, "2gi"),
      mem_budget_mib: Keyword.get(opts, :mem_budget_mib, 1_536),
      mem_headroom_mib: Keyword.get(opts, :mem_headroom_mib, 1_111),
      mem_reserved_mib: Keyword.get(opts, :mem_reserved_mib, 0),
      admits_on_reservation: Keyword.get(opts, :admits_on_reservation, false),
      live_vms: Keyword.get(opts, :live_vms, 0),
      max_live_vms: Keyword.get(opts, :max_live_vms, 8),
      configured_id: node_id,
      serving_subnet_cidr: "10.0.0.0/24"
    })
  end

  test "produces one record per brick and carries raw and derived fields" do
    capacity_table = table("capacity_observer_capacity")
    reservation_table = table("capacity_observer_reservation")
    put_brick(capacity_table, "a", mem_headroom_mib: 777, mem_reserved_mib: 321, live_vms: 2)
    put_brick(capacity_table, "b", size_class: "4gi", mem_headroom_mib: 2_222)

    records = CapacityObserver.records(capacity_table, reservation_table)
    assert length(records) == 2

    record = Enum.find(records, &(&1.instance_id == "node-a/a"))
    assert record.node_id == "node-a"
    assert record.size_class == "2gi"
    assert record.mem_budget_mib == 1_536
    assert record.mem_headroom_mib == 777
    assert record.mem_reserved_mib == 321
    assert record.admits_on_reservation == false
    assert record.live_vms == 2
    assert record.max_live_vms == 8
    assert record.nameplate_mib == 2_048
    assert record.total_working_set_mib == 1_271
    assert record.guest_free? == false
    assert record.cp_reserved_mib == 0
  end

  test "guest_free? requires both an empty declared sum and no live VMs" do
    capacity_table = table("capacity_observer_guest_free")

    for {pod, opts, expected} <- [
          {"both-empty", [mem_reserved_mib: 0, live_vms: 0], true},
          {"reserved", [mem_reserved_mib: 1, live_vms: 0], false},
          {"live", [mem_reserved_mib: 0, live_vms: 1], false},
          {"both-present", [mem_reserved_mib: 1, live_vms: 1], false}
        ] do
      put_brick(capacity_table, pod, opts)
      record =
        capacity_table
        |> CapacityObserver.records(table("capacity_observer_absent"))
        |> Enum.find(&(&1.instance_id == "node-#{pod}/#{pod}"))
      assert record.guest_free? == expected
    end
  end

  test "preserves explicit admission model independently of declared sum" do
    capacity_table = table("capacity_observer_admission")
    put_brick(capacity_table, "true", mem_reserved_mib: 0, admits_on_reservation: true)
    put_brick(capacity_table, "false", mem_reserved_mib: 0, admits_on_reservation: false)

    records = CapacityObserver.records(capacity_table, table("capacity_observer_admission_reservation"))
    assert Enum.find(records, &(&1.instance_id == "node-true/true")).admits_on_reservation
    refute Enum.find(records, &(&1.instance_id == "node-false/false")).admits_on_reservation
  end

  test "missing reservation table yields nil without crashing" do
    capacity_table = table("capacity_observer_missing_reservation")
    put_brick(capacity_table, "one")

    missing_table = String.to_atom("capacity_observer_missing_#{System.unique_integer([:positive])}")
    [record] = CapacityObserver.records(capacity_table, missing_table)
    assert record.cp_reserved_mib == nil
  end
end
