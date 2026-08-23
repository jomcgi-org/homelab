defmodule Embervm.CustomerKMS do
  @moduledoc """
  Customer-owned data-key oracle used by `Embervm.KeyService`.

  The customer keeps the KEK behind an HTTPS service and gives EmberVM only a
  wrap/unwrap grant. The issue operation is artifact-aware so the customer can
  return the same data key for a stable artifact identity such as a stateful
  volume. EmberVM receives a plaintext data key only for the duration of an
  export or restore. The KEK never enters the platform.

  `EMBERVM_CUSTOMER_KMS_CONFIG` is JSON with this shape:

      {"principals":{"acct:alice":{"mode":"customer",
       "transition_from":"platform","endpoint":"https://kms.example",
       "key_ref":"customer-key-1","bearer_token":"grant"}}}

  `mode` defaults to `customer`. `transition_from` is optional and must name the
  opposite custody mode. It is the explicit, temporary permission to restore
  and rewrap old-custody envelopes while new exports use `mode`.

  The whole value is supplied from a Kubernetes Secret. Inspection of the key
  service state always redacts it.
  """

  @type config :: %{
          required(:adapter) => module(),
          required(:mode) => :customer | :platform,
          required(:transition_from) => :customer | :platform | nil,
          required(:endpoint) => String.t(),
          required(:key_ref) => String.t(),
          required(:bearer_token) => String.t()
        }

  @callback issue(config(), String.t(), map()) ::
              {:ok, binary(), binary()} | {:error, term()}
  @callback wrap(config(), String.t(), binary(), map()) ::
              {:ok, binary()} | {:error, term()}
  @callback unwrap(config(), String.t(), String.t(), binary()) ::
              {:ok, binary()} | {:error, term()}

  @spec parse_config(String.t() | nil) :: {:ok, %{String.t() => config()}} | {:error, term()}
  def parse_config(nil), do: {:ok, %{}}
  def parse_config(""), do: {:ok, %{}}

  def parse_config(raw) when is_binary(raw) do
    with decoded when is_map(decoded) <- :json.decode(raw),
         principals when is_map(principals) <- Map.get(decoded, "principals"),
         {:ok, configs} <- parse_principals(principals) do
      {:ok, configs}
    else
      {:error, reason} -> {:error, reason}
      _ -> {:error, :invalid_customer_kms_config}
    end
  rescue
    _ -> {:error, :invalid_customer_kms_config}
  catch
    _, _ -> {:error, :invalid_customer_kms_config}
  end

  def parse_config(_raw), do: {:error, :invalid_customer_kms_config}

  defp parse_principals(principals) do
    Enum.reduce_while(principals, {:ok, %{}}, fn {principal, raw}, {:ok, acc} ->
      case parse_principal(principal, raw) do
        {:ok, config} -> {:cont, {:ok, Map.put(acc, principal, config)}}
        {:error, reason} -> {:halt, {:error, reason}}
      end
    end)
  end

  defp parse_principal(principal, %{
         "endpoint" => endpoint,
         "key_ref" => key_ref,
         "bearer_token" => bearer_token
       } = raw)
       when is_binary(principal) and principal != "" and is_binary(endpoint) and
              is_binary(key_ref) and key_ref != "" and is_binary(bearer_token) and
              bearer_token != "" do
    uri = URI.parse(endpoint)

    with true <-
           uri.scheme == "https" and is_binary(uri.host) and uri.host != "" and
             is_nil(uri.userinfo) and is_nil(uri.query) and is_nil(uri.fragment),
         {:ok, mode, transition_from} <-
           parse_custody(Map.get(raw, "mode", "customer"), Map.get(raw, "transition_from")) do
      {:ok,
       %{
         adapter: Embervm.CustomerKMS.HTTP,
         mode: mode,
         transition_from: transition_from,
         endpoint: String.trim_trailing(endpoint, "/"),
         key_ref: key_ref,
         bearer_token: bearer_token
       }}
    else
      false -> {:error, :invalid_customer_kms_endpoint}
      {:error, reason} -> {:error, reason}
    end
  end

  defp parse_principal(_principal, _raw), do: {:error, :invalid_customer_kms_config}

  defp parse_custody("customer", nil), do: {:ok, :customer, nil}
  defp parse_custody("customer", :null), do: {:ok, :customer, nil}
  defp parse_custody("customer", "platform"), do: {:ok, :customer, :platform}
  defp parse_custody("platform", nil), do: {:ok, :platform, nil}
  defp parse_custody("platform", :null), do: {:ok, :platform, nil}
  defp parse_custody("platform", "customer"), do: {:ok, :platform, :customer}
  defp parse_custody(_mode, _transition_from), do: {:error, :invalid_custody_transition}
end
