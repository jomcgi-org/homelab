defmodule Embervm.RestoreCapability do
  @moduledoc """
  Mints short-lived, brick-scoped capabilities for encrypted artifact restores.

  When artifact encryption is disabled, or when an artifact has no envelope,
  stamping is a no-op. An invalid, unauthenticated, or below-floor envelope is
  refused before the restore RPC is attempted.
  """

  require Logger

  alias Embervm.ArtifactPrefix
  alias Embervm.KeyService.Envelope

  @label "embervm-restore-cap-v1"
  @lease_ms 300_000

  @spec mint(binary(), binary(), non_neg_integer(), map()) :: binary()
  def mint(mac_key, data_key, expiry_ms, scope) do
    tuple_json = tuple_json(scope)

    before_mac =
      <<1, expiry_ms::unsigned-64, byte_size(data_key)::unsigned-16, data_key::binary,
        byte_size(tuple_json)::unsigned-16, tuple_json::binary>>

    mac = :crypto.mac(:hmac, :sha256, mac_key, @label <> before_mac)
    before_mac <> mac
  end

  @spec stamp(struct(), map(), map(), keyword()) ::
          {:ok, struct()} | {:error, :capability_refused | :no_capability_key}
  def stamp(req, brick, ctx, opts \\ []) do
    if Keyword.get(opts, :enabled?, Application.get_env(:embervm, :artifact_encryption, false)) do
      do_stamp(req, brick, ctx, opts)
    else
      {:ok, req}
    end
  end

  defp do_stamp(%{artifact: artifact} = req, brick, ctx, opts) do
    mac_key = Keyword.get(opts, :mac_key, Application.get_env(:embervm, :noded_bearer_token, ""))

    key_service =
      Keyword.get(
        opts,
        :key_service,
        Application.get_env(:embervm, :artifact_key_service, Embervm.KeyService)
      )

    if not is_binary(mac_key) or mac_key == "" do
      {:error, :no_capability_key}
    else
      principal = Map.fetch!(ctx, :principal)
      lineage = Map.get(ctx, :lineage, "") || ""
      vendor = Map.get(req, :vendor, "") || ""
      kind = Map.get(artifact, :kind)
      workload = Map.get(artifact, :workload, "")
      ref = Map.get(artifact, :ref, "") || ""

      with true <- valid_brick?(brick),
           prefix when is_binary(prefix) <-
             ArtifactPrefix.prefix(kind, workload, ref, vendor, lineage),
           {:ok, meta} <-
             fetch_meta(Keyword.get(opts, :s3_client, artifact_store_client()), prefix),
           {:enveloped, envelope_bytes} <- envelope_from_meta(meta),
           {:ok, envelope} <- Envelope.decode(envelope_bytes),
           true <- envelope.principal == principal,
           :ok <- ensure_envelope_epoch(key_service, envelope),
           {:ok, data_key} <- unwrap(key_service, envelope) do
        expiry = clock(opts).() + @lease_ms

        scope = %{
          principal: principal,
          lineage: lineage,
          node: Map.get(brick, :node_id, "") || "",
          pod_uid: Map.get(brick, :pod_uid, "") || "",
          workload: workload,
          ref: ref,
          kind: ArtifactPrefix.kind_string(kind),
          generation: Map.get(ctx, :generation, 0) || 0
        }

        {:ok, %{req | capability: mint(mac_key, data_key, expiry, scope)}}
      else
        :plaintext -> {:ok, req}
        {:error, :not_found} -> {:ok, req}
        _ -> refuse(ctx, artifact)
      end
    end
  rescue
    _ -> refuse(ctx, Map.get(req, :artifact, %{}))
  catch
    _, _ -> refuse(ctx, Map.get(req, :artifact, %{}))
  end

  defp fetch_meta(nil, _prefix), do: {:error, :no_store}

  defp fetch_meta(client, prefix) do
    case s3_get(client, prefix <> "/meta.json") do
      {:ok, body} -> decode_meta(body)
      other -> other
    end
  end

  defp s3_get({module, server}, key), do: module.get(server, key)
  defp s3_get(module, key) when is_atom(module), do: module.get(key)
  defp s3_get(client, key), do: Embervm.S3Client.get(client, key)

  defp decode_meta(body) when is_binary(body) do
    case :json.decode(body) do
      meta when is_map(meta) -> {:ok, meta}
      _ -> {:error, :bad_meta}
    end
  rescue
    _ -> {:error, :bad_meta}
  catch
    _, _ -> {:error, :bad_meta}
  end

  defp envelope_from_meta(meta) do
    case Map.get(meta, "envelope") || Map.get(meta, :envelope) do
      nil ->
        :plaintext

      "" ->
        :plaintext

      encoded when is_binary(encoded) ->
        case Base.decode64(encoded) do
          {:ok, bytes} -> {:enveloped, bytes}
          :error -> {:error, :bad_envelope}
        end

      _ ->
        {:error, :bad_envelope}
    end
  end

  defp ensure_first_epoch(key_service, principal) do
    case current_epoch(key_service, principal) do
      {:ok, 0} ->
        case set_epoch(key_service, principal, 1, "first_use") do
          {:ok, 1} -> :ok
          {:error, :epoch_not_increased} -> :ok
          other -> other
        end

      {:ok, _epoch} ->
        :ok

      other ->
        other
    end
  end

  defp ensure_envelope_epoch(key_service, %Envelope{version: version, principal: principal})
       when version in [1, 3],
    do: ensure_first_epoch(key_service, principal)

  defp ensure_envelope_epoch(_key_service, %Envelope{version: 2}), do: :ok

  defp current_epoch({module, server}, principal), do: module.current_epoch(server, principal)
  defp current_epoch(server, principal), do: Embervm.KeyService.current_epoch(server, principal)

  defp set_epoch({module, server}, principal, epoch, reason),
    do: module.set_epoch(server, principal, epoch, reason)

  defp set_epoch(server, principal, epoch, reason),
    do: Embervm.KeyService.set_epoch(server, principal, epoch, reason)

  defp unwrap({module, server}, envelope), do: module.unwrap(server, envelope)
  defp unwrap(server, envelope), do: Embervm.KeyService.unwrap(server, envelope)

  defp tuple_json(scope) do
    [
      ~s({"principal":),
      json_string(Map.fetch!(scope, :principal)),
      ~s(,"lineage":),
      json_string(Map.fetch!(scope, :lineage)),
      ~s(,"node":),
      json_string(Map.fetch!(scope, :node)),
      ~s(,"pod_uid":),
      json_string(Map.fetch!(scope, :pod_uid)),
      ~s(,"workload":),
      json_string(Map.fetch!(scope, :workload)),
      ~s(,"ref":),
      json_string(Map.fetch!(scope, :ref)),
      ~s(,"kind":),
      json_string(Map.fetch!(scope, :kind)),
      ~s(,"generation":),
      Integer.to_string(Map.fetch!(scope, :generation)),
      "}"
    ]
    |> IO.iodata_to_binary()
  end

  defp json_string(value), do: Jason.encode!(value)
  defp clock(opts), do: Keyword.get(opts, :clock, fn -> System.system_time(:millisecond) end)
  defp artifact_store_client, do: Application.get_env(:embervm, :artifact_store_client)

  defp valid_brick?(brick) do
    is_binary(Map.get(brick, :node_id)) and Map.get(brick, :node_id) != "" and
      is_binary(Map.get(brick, :pod_uid)) and Map.get(brick, :pod_uid) != ""
  end

  defp refuse(ctx, artifact) do
    Logger.warning("embervm restore capability refused",
      principal: Map.get(ctx, :principal),
      workload: Map.get(artifact, :workload),
      ref: Map.get(artifact, :ref),
      reason: "capability_refused"
    )

    {:error, :capability_refused}
  end
end
