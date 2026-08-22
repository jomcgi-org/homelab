defmodule Embervm.S3Client.SigV4 do
  @moduledoc false

  @algorithm "AWS4-HMAC-SHA256"
  @region "us-east-1"
  @service "s3"
  @unsigned_payload "UNSIGNED-PAYLOAD"

  @spec sign(atom(), String.t(), [{String.t(), String.t()}], String.t(), map(), DateTime.t()) ::
          [{String.t(), String.t()}]
  def sign(method, url, headers, body_sha, %{access_key_id: id, secret_access_key: secret}, %DateTime{} = now)
      when is_binary(id) and id != "" and is_binary(secret) and secret != "" do
    amz_date = Calendar.strftime(now, "%Y%m%dT%H%M%SZ")
    date = Calendar.strftime(now, "%Y%m%d")

    headers =
      headers
      |> put_header("x-amz-content-sha256", body_sha)
      |> put_header("x-amz-date", amz_date)

    signed_headers =
      (["host", "x-amz-content-sha256", "x-amz-date"] ++
         if(header(headers, "content-type") == "", do: [], else: ["content-type"]))
      |> Enum.sort()

    canonical = canonical_request(method, url, headers, signed_headers, body_sha)
    scope = "#{date}/#{@region}/#{@service}/aws4_request"
    string_to_sign = "#{@algorithm}\n#{amz_date}\n#{scope}\n#{sha256_hex(canonical)}"
    signature = signature(secret, date, @region, @service, string_to_sign)

    authorization =
      "#{@algorithm} Credential=#{id}/#{scope}, SignedHeaders=#{Enum.join(signed_headers, ";")}, Signature=#{signature}"

    put_header(headers, "authorization", authorization)
  end

  def sign(_method, _url, headers, _body_sha, _creds, _now), do: headers

  @doc false
  def canonical_request(method, url, headers, signed_headers, body_sha) do
    uri = URI.parse(url)

    canonical_headers =
      Enum.map_join(signed_headers, "", fn name ->
        value = if name == "host", do: uri.authority || "", else: header(headers, name)
        "#{name}:#{normalize(value)}\n"
      end)

    [
      method |> to_string() |> String.upcase(),
      canonical_uri(uri.path),
      canonical_query(uri.query),
      canonical_headers,
      Enum.join(signed_headers, ";"),
      body_sha
    ]
    |> Enum.join("\n")
  end

  @doc false
  def signature(secret, date, region, service, string_to_sign) do
    k_date = mac("AWS4" <> secret, date)
    k_region = mac(k_date, region)
    k_service = mac(k_region, service)
    k_signing = mac(k_service, "aws4_request")
    mac(k_signing, string_to_sign) |> Base.encode16(case: :lower)
  end

  def unsigned_payload, do: @unsigned_payload

  defp canonical_uri(nil), do: "/"
  defp canonical_uri(""), do: "/"

  defp canonical_uri(path) do
    path
    |> String.split("/", trim: false)
    |> Enum.map_join("/", fn segment -> segment |> URI.decode() |> URI.encode(&URI.char_unreserved?/1) end)
  end

  defp canonical_query(nil), do: ""
  defp canonical_query(""), do: ""

  defp canonical_query(query) do
    query
    |> URI.query_decoder()
    |> Enum.map(fn {key, value} -> {URI.encode(key, &URI.char_unreserved?/1), URI.encode(value, &URI.char_unreserved?/1)} end)
    |> Enum.sort()
    |> Enum.map_join("&", fn {key, value} -> "#{key}=#{value}" end)
  end

  defp header(headers, name) do
    case Enum.find(headers, fn {key, _value} -> String.downcase(to_string(key)) == name end) do
      nil -> ""
      {_key, value} -> to_string(value)
    end
  end

  defp put_header(headers, name, value) do
    [{name, value} | Enum.reject(headers, fn {key, _} -> String.downcase(to_string(key)) == name end)]
  end

  defp normalize(value), do: value |> String.split() |> Enum.join(" ")
  defp sha256_hex(value), do: :crypto.hash(:sha256, value) |> Base.encode16(case: :lower)
  defp mac(key, value), do: :crypto.mac(:hmac, :sha256, key, value)
end
