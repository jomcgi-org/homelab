defmodule Embervm.CustomerKMS.HTTP do
  @moduledoc """
  HTTPS adapter for a customer-owned KMS data-key oracle.

  The oracle exposes three JSON operations:

    * `POST /v1/data-keys/issue` accepts `key_ref`, `principal`, `artifact`, and
      `stable`, then returns base64 `data_key` plus opaque base64 `wrapped_key`.
    * `POST /v1/data-keys/wrap` additionally accepts a base64 `data_key` and
      returns opaque base64 `wrapped_key`.
    * `POST /v1/data-keys/unwrap` accepts the envelope fields and returns a
      base64 `data_key`.

  When `stable` is true, the customer service must return the same data key for
  repeated issue calls with the same principal, key reference, and artifact.
  The service must also bind those fields into its authenticated encryption
  context. A disabled key or revoked grant returns 401, 403, or 404, which
  EmberVM treats as unrestorable warmth.
  """

  @behaviour Embervm.CustomerKMS

  @timeout_ms 5_000
  @data_key_size 32

  @impl true
  def issue(config, principal, artifact) do
    body = %{
      key_ref: config.key_ref,
      principal: principal,
      artifact: artifact_view(artifact),
      stable: artifact_kind(artifact) == "volume"
    }

    with {:ok, response} <- request(config, "/v1/data-keys/issue", body),
         {:ok, data_key} <- decode_key(response, "data_key", @data_key_size),
         {:ok, wrapped_key} <- decode_key(response, "wrapped_key", :nonempty) do
      {:ok, data_key, wrapped_key}
    end
  end

  @impl true
  def wrap(config, principal, data_key, artifact) do
    body = %{
      key_ref: config.key_ref,
      principal: principal,
      artifact: artifact_view(artifact),
      data_key: Base.encode64(data_key)
    }

    with {:ok, response} <- request(config, "/v1/data-keys/wrap", body),
         {:ok, wrapped_key} <- decode_key(response, "wrapped_key", :nonempty) do
      {:ok, wrapped_key}
    end
  end

  @impl true
  def unwrap(config, principal, key_ref, wrapped_key) do
    body = %{
      key_ref: key_ref,
      principal: principal,
      wrapped_key: Base.encode64(wrapped_key)
    }

    with {:ok, response} <- request(config, "/v1/data-keys/unwrap", body),
         {:ok, data_key} <- decode_key(response, "data_key", @data_key_size) do
      {:ok, data_key}
    end
  end

  defp request(config, path, payload) do
    body = Jason.encode!(payload)

    headers = [
      {"authorization", "Bearer " <> config.bearer_token},
      {"content-type", "application/json"},
      {"accept", "application/json"}
    ]

    Finch.build(:post, config.endpoint <> path, headers, body)
    |> Finch.request(Embervm.Finch, receive_timeout: @timeout_ms)
    |> decode_response()
  rescue
    _ -> {:error, :kms_unavailable}
  catch
    _, _ -> {:error, :kms_unavailable}
  end

  defp decode_response({:ok, %Finch.Response{status: status, body: body}})
       when status in 200..299 do
    case :json.decode(body) do
      decoded when is_map(decoded) -> {:ok, decoded}
      _ -> {:error, :kms_bad_response}
    end
  rescue
    _ -> {:error, :kms_bad_response}
  catch
    _, _ -> {:error, :kms_bad_response}
  end

  defp decode_response({:ok, %Finch.Response{status: status}}) when status in [401, 403, 404],
    do: {:error, :kms_refused}

  defp decode_response({:ok, %Finch.Response{}}), do: {:error, :kms_unavailable}
  defp decode_response({:error, _reason}), do: {:error, :kms_unavailable}

  defp decode_key(response, field, expected) do
    case Map.get(response, field) do
      encoded when is_binary(encoded) ->
        case Base.decode64(encoded) do
          {:ok, key} when expected == :nonempty and byte_size(key) > 0 -> {:ok, key}
          {:ok, key} when is_integer(expected) and byte_size(key) == expected -> {:ok, key}
          _ -> {:error, :kms_bad_response}
        end

      _ ->
        {:error, :kms_bad_response}
    end
  end

  defp artifact_view(artifact) do
    %{
      kind: Map.get(artifact, "kind") || Map.get(artifact, :kind) || "",
      workload: Map.get(artifact, "workload") || Map.get(artifact, :workload) || "",
      ref: Map.get(artifact, "ref") || Map.get(artifact, :ref) || ""
    }
  end

  defp artifact_kind(artifact),
    do: Map.get(artifact, "kind") || Map.get(artifact, :kind) || ""
end
