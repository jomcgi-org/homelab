defmodule Embervm.ArtifactPrefix do
  @moduledoc """
  Composes object-store prefixes for EmberVM artifacts.

  The layout is the control-plane mirror of noded's `artifactPrefix`: volume
  and session-workspace artifacts are portable, while the other supported
  kinds carry the source CPU vendor.
  """

  @kind_strings %{
    :ARTIFACT_KIND_SESSION => "session",
    :ARTIFACT_KIND_SESSION_WORKSPACE => "session-workspace",
    :ARTIFACT_KIND_STATEFUL => "stateful",
    :ARTIFACT_KIND_VOLUME => "volume",
    :ARTIFACT_KIND_GROUP_SET => "group_set",
    :ARTIFACT_KIND_SERVING => "serving",
    "session" => "session",
    "session-workspace" => "session-workspace",
    "stateful" => "stateful",
    "volume" => "volume",
    "group_set" => "group_set",
    "serving" => "serving"
  }

  @portable_kinds MapSet.new([
                    :ARTIFACT_KIND_VOLUME,
                    :ARTIFACT_KIND_SESSION_WORKSPACE,
                    "volume",
                    "session-workspace"
                  ])

  @spec kind_string(atom() | String.t()) :: String.t() | nil
  def kind_string(kind), do: Map.get(@kind_strings, kind)

  @spec prefix(
          atom() | String.t(),
          String.t(),
          String.t() | nil,
          String.t() | nil,
          String.t() | nil
        ) ::
          String.t() | nil
  def prefix(kind, workload, ref, vendor, lineage)

  def prefix(kind, workload, ref, vendor, lineage) when is_binary(workload) and workload != "" do
    with kind_string when is_binary(kind_string) <- kind_string(kind),
         artifact_ref when is_binary(artifact_ref) <- artifact_ref(kind, ref, lineage) do
      segments =
        if MapSet.member?(@portable_kinds, kind) do
          [kind_string, workload, artifact_ref]
        else
          [kind_string, present(vendor), workload, artifact_ref]
        end

      if Enum.any?(segments, &is_nil/1) do
        nil
      else
        segments |> Enum.reject(&(&1 == "")) |> Enum.join("/")
      end
    else
      _ -> nil
    end
  end

  def prefix(_kind, _workload, _ref, _vendor, _lineage), do: nil

  defp artifact_ref(kind, _ref, lineage)
       when kind in [:ARTIFACT_KIND_SESSION_WORKSPACE, "session-workspace"] and
              is_binary(lineage) and lineage != "",
       do: lineage

  defp artifact_ref(_kind, ref, _lineage) when is_binary(ref), do: ref
  defp artifact_ref(_kind, nil, _lineage), do: ""
  defp artifact_ref(_kind, _ref, _lineage), do: nil

  defp present(value) when is_binary(value) and value != "", do: value
  defp present(_value), do: nil
end
