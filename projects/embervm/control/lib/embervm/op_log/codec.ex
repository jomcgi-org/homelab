defmodule Embervm.OpLog.Codec do
  @moduledoc false

  # Payloads use ETF so non-UTF-8 and NUL body bytes survive unchanged. These
  # are the node's own trusted storage records, never untrusted external input;
  # binary_to_term/1 retains the adapters' existing trust assumption.
  # SQLite supplies its own {:blob, bytes} binding; PostgreSQL stores raw BYTEA.
  def encode_payload(payload) when is_map(payload) do
    :erlang.term_to_binary(payload)
  end

  # The ETF version byte distinguishes new records from historical JSON rows.
  # Only ETF is normalized; legacy JSON keeps :json.decode/1's null semantics.
  def decode_payload(<<131, _::binary>> = term) do
    term |> :erlang.binary_to_term() |> stringify()
  end

  def decode_payload(json) when is_binary(json) do
    :json.decode(json)
  end

  # Preserve the stored contract: string keys and non-boolean atom values,
  # omitted nil-valued map entries, and byte-exact binary bodies. In lists nil
  # follows the atom clause and becomes "nil".
  defp stringify(map) when is_map(map) do
    for {k, v} <- map, not is_nil(v), into: %{}, do: {to_string(k), stringify(v)}
  end

  defp stringify(list) when is_list(list), do: Enum.map(list, &stringify/1)
  defp stringify(v) when is_atom(v) and not is_boolean(v), do: Atom.to_string(v)
  defp stringify(v), do: v

  # Response headers remain JSON objects. Empty and absent headers store NULL.
  def encode_headers(headers) when is_map(headers) and map_size(headers) == 0, do: nil

  def encode_headers(headers) when is_map(headers) do
    headers |> :json.encode() |> :erlang.iolist_to_binary()
  end

  def encode_headers(_), do: nil

  # Old NULL rows and malformed/non-object headers retain their empty fallback.
  def decode_headers(nil), do: %{}

  def decode_headers(json) when is_binary(json) do
    case :json.decode(json) do
      map when is_map(map) -> map
      _ -> %{}
    end
  rescue
    _ -> %{}
  end

  def decode_headers(_), do: %{}
end
