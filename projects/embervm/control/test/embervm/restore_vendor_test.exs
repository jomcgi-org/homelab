defmodule Embervm.RestoreVendorTest do
  @moduledoc """
  `Embervm.RestoreVendor` stamps the anchor node's CPUID vendor onto a
  restore-on-miss `RestoreArtifactRequest` (Bug B). noded's resolveRestorePrefix
  REJECTS a vendor-bound restore whose REQUEST has an empty vendor (the vendor
  rides `RestoreArtifactRequest.vendor`, field 3, read as `req.GetVendor()`, NOT
  the ArtifactRef); every vendor-bound kind (all but VOLUME) must therefore carry
  the node's reported vendor. VOLUME is vendor-portable and stays empty.
  """
  use ExUnit.Case, async: true

  alias Embervm.{NodeCapacity, RestoreVendor}
  alias Embervm.Node.V1.{ArtifactRef, CpuSku, RestoreArtifactRequest, Trace}

  defp table do
    t = :"rv_test_#{System.unique_integer([:positive])}"
    NodeCapacity.create(t)
    on_exit(fn -> if :ets.whereis(t) != :undefined, do: :ets.delete(t) end)
    t
  end

  defp put_node(t, node_id, vendor, template \\ "") do
    NodeCapacity.put(t, {node_id, ""}, %{
      node_id: node_id,
      pod_uid: "",
      instance_id: node_id,
      cpu_vendor: vendor,
      cpu_sku: %CpuSku{vendor: vendor, template: template},
      updated_at: 1
    })
  end

  defp request(kind) do
    %RestoreArtifactRequest{
      artifact: %ArtifactRef{kind: kind, workload: "wl-a", ref: "snap"},
      trace: %Trace{workload: "wl-a"}
    }
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

  test "stamp sets the anchor vendor on the REQUEST for a vendor-bound kind" do
    t = table()
    put_node(t, "node-4", "amd", "amd-default")

    stamped = RestoreVendor.stamp(t, "node-4", request(:ARTIFACT_KIND_STATEFUL))
    assert %RestoreArtifactRequest{vendor: "amd", cpu_sku: %CpuSku{vendor: "amd", template: "amd-default"}} = stamped
  end

  test "stamp carries the full SKU for every memory-bearing durable lane" do
    t = table()
    put_node(t, "node-4", "amd", "amd-v2")

    for kind <- [
          :ARTIFACT_KIND_BASE,
          :ARTIFACT_KIND_SESSION,
          :ARTIFACT_KIND_STATEFUL,
          :ARTIFACT_KIND_SERVING,
          :ARTIFACT_KIND_GROUP_SET
        ] do
      assert %RestoreArtifactRequest{
               vendor: "amd",
               cpu_sku: %CpuSku{vendor: "amd", template: "amd-v2"}
             } = RestoreVendor.stamp(t, "node-4", request(kind))
    end
  end

  test "stamp leaves a VOLUME request's vendor empty (vendor-portable)" do
    t = table()
    put_node(t, "node-4", "amd")

    stamped = RestoreVendor.stamp(t, "node-4", request(:ARTIFACT_KIND_VOLUME))
    assert %RestoreArtifactRequest{vendor: ""} = stamped
  end

  test "stamp yields an empty vendor when the anchor reports none (pre-R7 daemon), not a crash" do
    t = table()
    put_node(t, "node-4", "")

    stamped = RestoreVendor.stamp(t, "node-4", request(:ARTIFACT_KIND_SERVING))
    # noded refuses this CPU-bound restore, so missing discovery fails closed.
    assert %RestoreArtifactRequest{vendor: ""} = stamped
  end

  test "cpu_sku mismatch extraction preserves the daemon's clear failure" do
    message = "noded: cpu_sku mismatch on restore: artifact stamped amd/v2 != node amd/v1"

    assert RestoreVendor.cpu_sku_mismatch_reason({:error, %{status: 9, message: message}}) ==
             {:cpu_sku_mismatch, message}

    assert RestoreVendor.cpu_sku_mismatch_reason({:error, %{status: 9, message: "other precondition"}}) == nil
  end
end
