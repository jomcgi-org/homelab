defmodule Embervm.ArtifactRestore do
  @moduledoc """
  The restore-on-miss RestoreArtifact RPC shared by the wake managers.

  `run/7` reads the manager state's `capacity_table`, `channel_fun` and
  `restore_artifact_fun`. It dials `dial_id` (the boot's selected instance, Step 4)
  but stamps the vendor off `node_id` (the node-name anchor `Embervm.RestoreVendor`
  can resolve; the vendor is identical across a node's instances), then runs the RPC
  inside the `embervm.artifact_restore` span (Task 11).
  """

  # Tracer.with_span/set_attributes are OpenTelemetry.Tracer MACROS.
  require OpenTelemetry.Tracer, as: Tracer

  alias Embervm.NodeCapacity
  alias Embervm.Node.V1.RestoreArtifactRequest

  @doc """
  Stamp vendor and capability onto `req`, dial `dial_id` and run RestoreArtifact.
  `invalidate_fun.(dial_id, channel)` drops the channel when the call reveals a dead
  transport. `kind` is the span's `ember.artifact_kind`.
  """
  @spec run(map(), term(), term(), %RestoreArtifactRequest{}, map(), String.t(), (term(), term() -> term())) ::
          {:ok, term()} | {:error, term()}
  def run(state, dial_id, node_id, %RestoreArtifactRequest{artifact: ref} = req, ctx, kind, invalidate_fun) do
    req = Embervm.RestoreVendor.stamp(state.capacity_table, node_id, req)
    brick = brick(state.capacity_table, dial_id, node_id)

    with {:ok, req} <- Embervm.RestoreCapability.stamp(req, brick, ctx),
         {:ok, channel} <- Embervm.NodeChannel.safe_dial(state.channel_fun, dial_id) do
      # Identity up front, bytes-moved/skipped stamped from the response, so the
      # restore-on-miss gate reads its evidence off the span alone.
      Tracer.with_span "embervm.artifact_restore",
                       %{
                         attributes: %{
                           "ember.workload" => ref.workload,
                           "ember.artifact_kind" => kind,
                           "ember.artifact_ref" => ref.ref
                         }
                       } do
        result = rpc(state.restore_artifact_fun, invalidate_fun, dial_id, channel, req)
        stamp_span(result)
        result
      end
    end
  end

  defp brick(table, dial_id, node_id) do
    case Enum.find(NodeCapacity.all(table), &(Map.get(&1, :instance_id) == dial_id)) do
      nil when dial_id == node_id ->
        case NodeCapacity.fetch(table, node_id) do
          {:ok, brick} -> brick
          :error -> %{node_id: node_id, pod_uid: ""}
        end

      nil ->
        %{node_id: node_id, pod_uid: ""}

      brick ->
        brick
    end
  end

  # The RPC with transport-death channel invalidation, keyed by `dial_id` (the
  # instance the channel was dialled on).
  defp rpc(restore_fun, invalidate_fun, dial_id, channel, req) do
    try do
      case restore_fun.(channel, req) do
        {:error, reason} = err ->
          if Embervm.NodeChannel.transport_dead?(reason) do
            _ = invalidate_fun.(dial_id, channel)
          end

          err

        other ->
          other
      end
    rescue
      e -> {:error, {:restore_artifact_raised, e}}
    catch
      :exit, reason ->
        _ = invalidate_fun.(dial_id, channel)
        {:error, {:restore_artifact_raised, {:exit, reason}}}

      kind, reason ->
        {:error, {:restore_artifact_raised, {kind, reason}}}
    end
  end

  # A failure result leaves the span with only its identity attributes (the error
  # itself surfaces in the caller's warning log).
  defp stamp_span({:ok, resp}) do
    Tracer.set_attributes(%{
      "ember.bytes_moved" => Map.get(resp, :bytes_moved, 0),
      "ember.skipped" => Map.get(resp, :skipped, false)
    })
  end

  defp stamp_span(_other), do: :ok
end
