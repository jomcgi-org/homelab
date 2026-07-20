defmodule Embervm.RestoreVendorTest do
  @moduledoc """
  `Embervm.RestoreVendor` stamps the anchor node's CPUID vendor onto a
  restore-on-miss ArtifactRef (Bug B). noded's resolveRestorePrefix REJECTS a
  vendor-bound restore whose ref has an empty vendor; every vendor-bound kind
  (all but VOLUME) must therefore carry the node's reported vendor. VOLUME is
  vendor-portable and stays empty.
  """
  use ExUnit.Case, async: true

  alias Embervm.{NodeCapacity, RestoreVendor}
  alias Embervm.Node.V1.ArtifactRef

  defp table do
    t = :"rv_test_#{System.unique_integer([:positive])}"
    NodeCapacity.create(t)
    on_exit(fn -> if :ets.whereis(t) != :undefined, do: :ets.delete(t) end)
    t
  end

  defp put_node(t, node_id, vendor) do
    NodeCapacity.put(t, {node_id, ""}, %{
      node_id: node_id,
      pod_uid: "",
      instance_id: node_id,
      cpu_vendor: vendor,
      updated_at: 1
    })
  end

  test "vendor_bound? is true for every kind except VOLUME" do
    for kind <- [
          :ARTIFACT_KIND_STATEFUL,
          :ARTIFACT_KIND_SERVING,
          :ARTIFACT_KIND_SESSION,
          :ARTIFACT_KIND_GROUP_SET
        ] do
      assert RestoreVendor.vendor_bound?(kind)
    end

    refute RestoreVendor.vendor_bound?(:ARTIFACT_KIND_VOLUME)
  end

  test "NodeCapacity.vendor_for reads the anchor node's cpu_vendor" do
    t = table()
    put_node(t, "node-4", "amd")

    assert NodeCapacity.vendor_for(t, "node-4") == "amd"
    # A node not in the table (not dispatchable) resolves to empty, never raises.
    assert NodeCapacity.vendor_for(t, "node-nope") == ""
  end

  test "stamp sets the anchor vendor on a vendor-bound ref" do
    t = table()
    put_node(t, "node-4", "amd")

    ref = %ArtifactRef{kind: :ARTIFACT_KIND_STATEFUL, workload: "wl-a", ref: "snap"}
    stamped = RestoreVendor.stamp(t, "node-4", ref)
    assert stamped.vendor == "amd"
  end

  test "stamp leaves a VOLUME ref's vendor empty (vendor-portable)" do
    t = table()
    put_node(t, "node-4", "amd")

    ref = %ArtifactRef{kind: :ARTIFACT_KIND_VOLUME, workload: "wl-a", ref: "wl-a"}
    stamped = RestoreVendor.stamp(t, "node-4", ref)
    assert stamped.vendor == ""
  end

  test "stamp yields an empty vendor when the anchor reports none (pre-R7 daemon), not a crash" do
    t = table()
    put_node(t, "node-4", "")

    ref = %ArtifactRef{kind: :ARTIFACT_KIND_SERVING, workload: "wl-a", ref: "snap"}
    stamped = RestoreVendor.stamp(t, "node-4", ref)
    # noded maps an empty vendor to the node-4 legacy alias, so this still restores.
    assert stamped.vendor == ""
  end
end
