defmodule Embervm.ServingPlacementTest do
  @moduledoc """
  Pure-function tests for Embervm.ServingPlacement over an isolated NodeCapacity
  table, mirroring session_placement_test. Proves node_for_create picks a
  serving-capable ready node with the workload's base + budget (and denies
  :no_capacity otherwise), and node_for_relight resolves only to a node still
  reporting the instance's serving snapshot (else :snapshot_lost).
  """
  use ExUnit.Case, async: true

  alias Embervm.{NodeCapacity, ServingPlacement}

  setup do
    t = :"splace_#{System.unique_integer([:positive])}"
    NodeCapacity.create(t)
    %{t: t}
  end

  defp put_serving_node(t, id, opts) do
    NodeCapacity.put(t, id, %{
      configured_id: id,
      node_id: id,
      serving_subnet_cidr: Keyword.get(opts, :cidr, "10.99.0.0/24"),
      max_live_vms: Keyword.get(opts, :max_live_vms, 4),
      live_vms: Keyword.get(opts, :live_vms, 0),
      workloads: Keyword.get(opts, :workloads, %{}),
      serving_snapshots: Keyword.get(opts, :serving_snapshots, [])
    })
  end

  # A ready serving workload advertises a serving_image_ref (the cold-boot handler
  # artifact placement cold-boots, D-R3.11.2), NOT just the base snapshot_ref. The
  # param names the serving image ref, which node_for_create returns for the cold boot.
  defp ready_workload(serving_image_ref \\ "base:img@sha256:abc") do
    %{
      "wl-a" => %{
        base_state: :BASE_BUILD_STATE_READY,
        snapshot_ref: "snap:img@sha256:abc",
        serving_image_ref: serving_image_ref
      }
    }
  end

  # -- node_for_create -------------------------------------------------------

  test "node_for_create picks a serving-capable ready node with base + budget", %{t: t} do
    put_serving_node(t, "node-4", workloads: ready_workload("base-a"))

    assert ServingPlacement.node_for_create("wl-a", t) == {:ok, "node-4", "base-a"}
  end

  test "node_for_create denies :no_capacity when no node reports a serving subnet", %{t: t} do
    # A node WITHOUT serving_subnet_cidr is not a serving target even with base+budget.
    NodeCapacity.put(t, "node-1", %{
      configured_id: "node-1",
      node_id: "node-1",
      max_live_vms: 4,
      live_vms: 0,
      workloads: ready_workload()
    })

    assert ServingPlacement.node_for_create("wl-a", t) == {:error, :no_capacity}
  end

  test "node_for_create denies when the base is not ready", %{t: t} do
    put_serving_node(t, "node-4",
      workloads: %{"wl-a" => %{base_state: :BASE_BUILD_STATE_BUILDING, snapshot_ref: ""}}
    )

    assert ServingPlacement.node_for_create("wl-a", t) == {:error, :no_capacity}
  end

  test "node_for_create denies when the node is at its live-VM cap", %{t: t} do
    put_serving_node(t, "node-4", live_vms: 4, max_live_vms: 4, workloads: ready_workload())
    assert ServingPlacement.node_for_create("wl-a", t) == {:error, :no_capacity}
  end

  # -- node_for_relight ------------------------------------------------------

  test "node_for_relight resolves to the node still reporting the snapshot", %{t: t} do
    put_serving_node(t, "node-4",
      serving_snapshots: [%{snapshot_ref: "serving/s-1", workload: "wl-a"}]
    )

    instance = %{node_id: "node-4", snapshot_ref: "serving/s-1"}
    assert ServingPlacement.node_for_relight(instance, t) == {:ok, "node-4"}
  end

  test "node_for_relight is :snapshot_lost when no node reports the snapshot", %{t: t} do
    put_serving_node(t, "node-4", serving_snapshots: [])

    instance = %{node_id: "node-4", snapshot_ref: "serving/s-gone"}
    assert ServingPlacement.node_for_relight(instance, t) == {:error, :snapshot_lost}
  end

  test "node_for_relight is :snapshot_lost when the node is down (absent from the table)", %{t: t} do
    instance = %{node_id: "node-dead", snapshot_ref: "serving/s-1"}
    assert ServingPlacement.node_for_relight(instance, t) == {:error, :snapshot_lost}
  end
end
