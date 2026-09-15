defmodule Embervm.RestoreVendor do
  @moduledoc """
  Stamps the full CPU SKU onto a restore-on-miss `RestoreArtifactRequest`.

  noded's `resolveRestorePrefix` composes the store read prefix for a restore as
  `<kind>/<vendor>/<workload>/<ref>` for every vendor-bound artifact kind, and
  REJECTS the restore with `InvalidArgument: "vendor required to restore this
  artifact kind"` when the vendor is empty. The vendor rides the REQUEST, not the
  `ArtifactRef`: the proto is
  `RestoreArtifactRequest{artifact, trace, vendor, cpu_sku}`
  (`vendor = 3`), and noded reads `req.GetVendor()` (server/store.go), never
  `ref.vendor` (the `ArtifactRef` has no `vendor` field). Every restore-on-miss
  call site in the control plane (stateful/serving/session/group wake planners)
  must therefore resolve the anchor node's vendor and set `req.vendor` before the
  RPC, or the daemon fails the restore closed and the wake needlessly degrades to a
  cold boot.

  ## which kinds are vendor-bound

  Every artifact kind EXCEPT `VOLUME` and `SESSION_WORKSPACE` is vendor-bound
  (volume and workspace data are vendor-portable; noded's
  `artifactVendorSegment` mirrors this). These two restores leave `vendor`
  empty; every other kind gets the anchor node's reported vendor.

  ## empty identity fails closed

  When the anchor node reports no vendor (a pre-R7 daemon, or a node not currently
  dispatchable), the resolved vendor is `""`. noded refuses a CPU-bound restore
  with that unresolved target. This keeps legacy discovery from silently crossing
  a compatibility boundary. An artifact whose own metadata has no SKU remains
  grandfathered only after the target node has supplied a usable identity.
  """

  alias Embervm.NodeCapacity

  @doc """
  Return `req` with its `vendor` and `cpu_sku` set to the anchor `node_key`'s
  reported identity when the request's artifact kind is CPU-bound, or unchanged
  for portable filesystem artifacts. The kind is read off `req.artifact.kind`. `node_key` is whatever the
  caller anchors the restore on (a node-name string or an instance tuple), resolved
  through `NodeCapacity.fetch/2`. `table` is the capacity table the caller holds.
  """
  @spec stamp(atom(), String.t() | {String.t(), String.t()}, struct()) :: struct()
  def stamp(table, node_key, %{artifact: %{kind: kind}} = req) do
    if vendor_bound?(kind) do
      sku = NodeCapacity.sku_for(table, node_key)
      %{req | vendor: sku.vendor, cpu_sku: sku}
    else
      req
    end
  end

  @doc "Extract a clear CPU SKU mismatch reason from a restore RPC failure."
  @spec cpu_sku_mismatch_reason(term()) :: {:cpu_sku_mismatch, String.t()} | nil
  def cpu_sku_mismatch_reason({:error, error}), do: cpu_sku_mismatch_reason(error)

  def cpu_sku_mismatch_reason(%{status: 9, message: message}) when is_binary(message) do
    if String.contains?(message, "cpu_sku mismatch"),
      do: {:cpu_sku_mismatch, message},
      else: nil
  end

  def cpu_sku_mismatch_reason(_), do: nil

  @doc "Whether an artifact kind is vendor-bound (except VOLUME and SESSION_WORKSPACE)."
  @spec vendor_bound?(atom()) :: boolean()
  def vendor_bound?(:ARTIFACT_KIND_VOLUME), do: false
  def vendor_bound?(:ARTIFACT_KIND_SESSION_WORKSPACE), do: false
  def vendor_bound?(_kind), do: true
end
