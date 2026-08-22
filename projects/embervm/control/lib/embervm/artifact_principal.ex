defmodule Embervm.ArtifactPrincipal do
  @moduledoc """
  Resolves the principal that owns a mutable artifact.

  Sessions retain the authenticated principal stored on their durable row.
  Other lifecycle classes keep their current synthetic principals.
  """

  alias Embervm.SessionStore

  @spec resolve(atom() | String.t(), String.t(), String.t()) ::
          {:ok, String.t()} | {:error, :unknown_artifact}
  def resolve(kind, workload, ref) do
    case normalize_kind(kind) do
      :session -> session_principal(ref)
      :session_workspace -> lineage_principal(ref)
      :stateful -> {:ok, "system:stateful:" <> workload}
      :volume -> {:ok, "system:stateful:" <> workload}
      :group_set -> {:ok, "system:group:" <> workload}
      :serving -> {:ok, "system:serving:" <> workload}
      :unknown -> {:error, :unknown_artifact}
    end
  end

  defp session_principal(snapshot_ref) do
    session_store_mod()
    |> apply(:all, [session_store()])
    |> Enum.find(&(Map.get(&1, :snapshot_ref) == snapshot_ref))
    |> principal_of()
  rescue
    _ -> {:error, :unknown_artifact}
  catch
    _, _ -> {:error, :unknown_artifact}
  end

  defp lineage_principal(lineage) do
    case apply(session_store_mod(), :get_latest_by_lineage, [session_store(), lineage]) do
      {:ok, session} -> principal_of(session)
      _ -> {:error, :unknown_artifact}
    end
  rescue
    _ -> {:error, :unknown_artifact}
  catch
    _, _ -> {:error, :unknown_artifact}
  end

  defp principal_of(%{principal: principal}) when is_binary(principal) and principal != "",
    do: {:ok, principal}

  defp principal_of(_session), do: {:error, :unknown_artifact}

  defp session_store_mod, do: Application.get_env(:embervm, :session_store_mod, SessionStore)
  defp session_store, do: Application.get_env(:embervm, :session_store, SessionStore)

  defp normalize_kind(kind) when kind in [:ARTIFACT_KIND_SESSION, "session"], do: :session

  defp normalize_kind(kind)
       when kind in [:ARTIFACT_KIND_SESSION_WORKSPACE, "session-workspace"],
       do: :session_workspace

  defp normalize_kind(kind) when kind in [:ARTIFACT_KIND_STATEFUL, "stateful"], do: :stateful
  defp normalize_kind(kind) when kind in [:ARTIFACT_KIND_VOLUME, "volume"], do: :volume
  defp normalize_kind(kind) when kind in [:ARTIFACT_KIND_GROUP_SET, "group_set"], do: :group_set
  defp normalize_kind(kind) when kind in [:ARTIFACT_KIND_SERVING, "serving"], do: :serving
  defp normalize_kind(_kind), do: :unknown
end
