defmodule Embervm.RestoreVendor do
  @moduledoc """
  Stamps the CPUID vendor onto a restore-on-miss `RestoreArtifactRequest`
  (R7, ADR embervm/011).

  noded's `resolveRestorePrefix` composes the store read prefix for a restore as
  `<kind>/<vendor>/<workload>/<ref>` for every vendor-bound artifact kind, and
  REJECTS the restore with `InvalidArgument: "vendor required to restore this
  artifact kind"` when the vendor is empty. The vendor rides the REQUEST, not the
  `ArtifactRef`: the proto is `RestoreArtifactRequest{artifact, trace, vendor}`
  (`vendor = 3`), and noded reads `req.GetVendor()` (server/store.go), never
  `ref.vendor` (the `ArtifactRef` has no `vendor` field). Every restore-on-miss
  call site in the control plane (stateful/serving/session/group wake planners)
  must therefore resolve the anchor node's vendor and set `req.vendor` before the
  RPC, or the daemon fails the restore closed and the wake needlessly degrades to a
  cold boot.

  ## which kinds are vendor-bound

  Every artifact kind EXCEPT `VOLUME` is vendor-bound (volume data is
  vendor-portable, standing decision 1; noded's `artifactVendorSegment` mirrors
  this). A VOLUME restore leaves `vendor` empty; every other kind gets the anchor
  node's reported vendor.

  ## empty vendor is safe, not fatal

  When the anchor node reports no vendor (a pre-R7 daemon, or a node not currently
  dispatchable), the resolved vendor is `""`. For a vendor-bound kind that would
  reproduce the bug this module fixes, EXCEPT that noded maps the node-4 vendor
  alias to the legacy un-vendored prefix (standing decision 11), so today's
  single-vendor fleet still restores. The stamp is best-effort by design: the wake
  path already degrades a failed restore to a cold boot (fail-open warmth), so a
  missing vendor is never worse than the pre-fix behaviour, and on a vendor-reporting
  fleet it is correct.
  """

  alias Embervm.NodeCapacity

  @doc """
  Return `req` with its `vendor` set to the anchor `node_key`'s reported CPU vendor
  when the request's artifact kind is vendor-bound, or unchanged (empty vendor) for a
  `VOLUME`. The kind is read off `req.artifact.kind`. `node_key` is whatever the
  caller anchors the restore on (a node-name string or an instance tuple), resolved
  through `NodeCapacity.fetch/2`. `table` is the capacity table the caller holds.
  """
  @spec stamp(atom(), String.t() | {String.t(), String.t()}, struct()) :: struct()
  def stamp(table, node_key, %{artifact: %{kind: kind}} = req) do
    if vendor_bound?(kind) do
      %{req | vendor: NodeCapacity.vendor_for(table, node_key)}
    else
      req
    end
  end

  @doc "Whether an artifact kind is vendor-bound (every kind except VOLUME)."
  @spec vendor_bound?(atom()) :: boolean()
  def vendor_bound?(:ARTIFACT_KIND_VOLUME), do: false
  def vendor_bound?(_kind), do: true
end
