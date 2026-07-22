defmodule Embervm.S3Client do
  @moduledoc """
  Minimal raw-HTTP S3 client for the control plane's S3-direct warmth GC
  (task #39): ListObjectsV2 (with pagination), GET, PUT, and single-key DELETE
  against the in-cluster SeaweedFS S3 gateway, over the shared `Embervm.Finch`
  pool. The structural mirror of noded's non-SDK store client
  (noded/store/store.go): plain HTTP verbs on `<endpoint>/<bucket>/<key>`,
  anonymous in-cluster (no SigV4 in v1), nil-safe on a disabled store.

  ## why hand-rolled, and why here

  This is the FIRST time the control plane talks to the object store directly:
  every prior S3 touch went through a noded RPC (Export/Restore/EvictArtifact),
  which composes prefixes only from its own vendor + the vendored layout and so
  can never even ENUMERATE the pre-sidecar orphan backlog (workload binding
  lost, see Embervm.S3WarmthGc). The GC needs List, which noded's client never
  grew, so a small CP-side client is the whole cost of the design. No SDK: the
  four verbs over Finch are ~as many lines as an SDK's config, match the noded
  precedent, and add zero deps to the hermetic hex closure.

  ## retries and fail-closed listing

  SeaweedFS is known to throw intermittent 500s (task #30/#10), so every verb
  retries transient failures (transport errors + 5xx) with exponential backoff.
  A retry-exhausted failure is returned as `{:error, reason}`, never rescued
  into a partial success: the GC treats ANY list/pagination error as an abort of
  the whole sweep, because a partial listing is indistinguishable from "these
  objects do not exist" and this client's caller DELETES based on absence.

  ## XML parsing

  ListObjectsV2 responses are parsed with anchored regex scans rather than a
  full XML parser: the response grammar is fixed (SeaweedFS emits one
  `<Contents>`/`<CommonPrefixes>` element shape, verified live 2026-07-22), the
  keys this repo writes are filesystem-safe (`[a-z0-9._/-]`, no XML metachars),
  and it keeps `:xmerl` out of the OTP release closure. The five standard XML
  entities are still unescaped defensively.
  """

  require Logger

  @enforce_keys [:endpoint, :bucket]
  defstruct [:endpoint, :bucket]

  @type t :: %__MODULE__{endpoint: String.t(), bucket: String.t()}

  @typedoc "One listed object: key, byte size, Last-Modified as unix ms."
  @type entry :: %{key: String.t(), size: non_neg_integer(), last_modified_ms: integer()}

  # Transient-failure retry policy (SeaweedFS 500s are known): total attempts,
  # base backoff doubling per retry (250ms, 500ms). Kept small: the GC is a slow
  # background sweep and aborts cleanly on exhaustion, so patience buys little.
  @attempts 3
  @backoff_base_ms 250

  # One ListObjectsV2 page. SeaweedFS honours max-keys and returns
  # NextContinuationToken (the last key of the page) when IsTruncated.
  @page_size 1000

  @receive_timeout 15_000

  @doc """
  Build a client for endpoint + bucket, or nil when endpoint is empty ("the
  store is disabled"), mirroring noded's `store.New`. The endpoint is normalised
  to no trailing slash so key joins are unambiguous.
  """
  @spec new(String.t(), String.t()) :: t() | nil
  def new(endpoint, bucket)
  def new("", _bucket), do: nil
  def new(nil, _bucket), do: nil

  def new(endpoint, bucket) when is_binary(endpoint) and is_binary(bucket) do
    %__MODULE__{endpoint: String.trim_trailing(endpoint, "/"), bucket: bucket}
  end

  @doc """
  Every object under `prefix`, fully paginated (ListObjectsV2, no delimiter).
  Returns `{:ok, [entry]}` only when EVERY page listed successfully; any page
  failure (after retries) returns `{:error, reason}` with nothing partial, so
  the caller can never mistake a truncated listing for the full set.
  """
  @spec list_all(t(), String.t()) :: {:ok, [entry()]} | {:error, term()}
  def list_all(%__MODULE__{} = client, prefix) do
    list_pages(client, prefix, nil, [])
  end

  @doc """
  GET one object's body. `{:error, :not_found}` on 404 (distinct from a
  transport failure, mirroring noded's ErrNotPresent), `{:error, reason}` on
  anything else after retries.
  """
  @spec get(t(), String.t()) :: {:ok, binary()} | {:error, :not_found} | {:error, term()}
  def get(%__MODULE__{} = client, key) do
    case request(client, :get, object_url(client, key), "") do
      {:ok, %{status: 404}} -> {:error, :not_found}
      {:ok, %{status: status, body: body}} when status in 200..299 -> {:ok, body}
      {:ok, %{status: status}} -> {:error, {:unexpected_status, status}}
      {:error, reason} -> {:error, reason}
    end
  end

  @doc "PUT `body` at `key` (the GC's manifest persist). `:ok` or `{:error, reason}`."
  @spec put(t(), String.t(), iodata()) :: :ok | {:error, term()}
  def put(%__MODULE__{} = client, key, body) do
    case request(client, :put, object_url(client, key), body) do
      {:ok, %{status: status}} when status in 200..299 -> :ok
      {:ok, %{status: status}} -> {:error, {:unexpected_status, status}}
      {:error, reason} -> {:error, reason}
    end
  end

  @doc """
  DELETE one fully-qualified object key. Idempotent: a 404 (already gone) is
  success, matching noded's desired-end-state contract. NEVER takes a prefix or
  issues a bucket-wide operation: one call, one key.
  """
  @spec delete(t(), String.t()) :: :ok | {:error, term()}
  def delete(%__MODULE__{} = client, key) do
    case request(client, :delete, object_url(client, key), "") do
      {:ok, %{status: 404}} -> :ok
      {:ok, %{status: status}} when status in 200..299 -> :ok
      {:ok, %{status: status}} -> {:error, {:unexpected_status, status}}
      {:error, reason} -> {:error, reason}
    end
  end

  # -- pagination --------------------------------------------------------------

  defp list_pages(client, prefix, token, acc) do
    query =
      [{"list-type", "2"}, {"prefix", prefix}, {"max-keys", Integer.to_string(@page_size)}] ++
        if(token, do: [{"continuation-token", token}], else: [])

    url = client.endpoint <> "/" <> client.bucket <> "/?" <> URI.encode_query(query)

    with {:ok, %{status: status, body: body}} when status in 200..299 <- request(client, :get, url, ""),
         {:ok, entries, truncated?, next_token} <- parse_list_response(body) do
      acc = acc ++ entries

      cond do
        not truncated? -> {:ok, acc}
        # A truncated page MUST carry a token; without one another fetch would
        # loop on the same page forever, so fail the whole listing instead.
        next_token in [nil, ""] -> {:error, :truncated_without_token}
        true -> list_pages(client, prefix, next_token, acc)
      end
    else
      {:ok, %{status: status}} -> {:error, {:unexpected_status, status}}
      {:error, reason} -> {:error, reason}
    end
  end

  # -- ListObjectsV2 XML parsing ----------------------------------------------

  @doc false
  # Public (@doc false) so the unit tests can assert the parse against captured
  # SeaweedFS response bodies without a live store.
  @spec parse_list_response(binary()) ::
          {:ok, [entry()], boolean(), String.t() | nil} | {:error, term()}
  def parse_list_response(body) when is_binary(body) do
    if String.contains?(body, "<ListBucketResult") do
      entries =
        for [contents] <- Regex.scan(~r{<Contents>(.*?)</Contents>}s, body, capture: :all_but_first) do
          %{
            key: xml_unescape(field(contents, "Key") || ""),
            size: String.to_integer(field(contents, "Size") || "0"),
            last_modified_ms: parse_last_modified(field(contents, "LastModified"))
          }
        end

      truncated? = field(body, "IsTruncated") == "true"
      token = field(body, "NextContinuationToken")
      {:ok, entries, truncated?, token && xml_unescape(token)}
    else
      {:error, {:not_a_list_response, String.slice(body, 0, 200)}}
    end
  end

  defp field(xml, tag) do
    case Regex.run(~r{<#{tag}>(.*?)</#{tag}>}s, xml, capture: :all_but_first) do
      [value] -> value
      nil -> nil
    end
  end

  # ISO8601 Last-Modified -> unix ms; an unparseable timestamp reads as 0 (epoch),
  # which the GC's age gate treats as OLD. That direction is acceptable ONLY
  # because Last-Modified is the age FALLBACK for a prefix with no meta.json and
  # every other predicate condition still gates the delete.
  defp parse_last_modified(nil), do: 0

  defp parse_last_modified(value) do
    case DateTime.from_iso8601(value) do
      {:ok, dt, _offset} -> DateTime.to_unix(dt, :millisecond)
      _ -> 0
    end
  end

  defp xml_unescape(value) do
    value
    |> String.replace("&lt;", "<")
    |> String.replace("&gt;", ">")
    |> String.replace("&quot;", "\"")
    |> String.replace("&#34;", "\"")
    |> String.replace("&apos;", "'")
    |> String.replace("&#39;", "'")
    |> String.replace("&amp;", "&")
  end

  # -- transport ---------------------------------------------------------------

  defp object_url(client, key) do
    client.endpoint <> "/" <> client.bucket <> "/" <> String.trim_leading(key, "/")
  end

  # One verb with transient-failure retries: transport errors and 5xx retry with
  # exponential backoff; 4xx returns immediately (it will not get better). The
  # response body is always fully read (Finch does this for us).
  defp request(_client, method, url, body), do: request_with_retry(method, url, body, 1)

  defp request_with_retry(method, url, body, attempt) do
    req = Finch.build(method, url, [], body)

    case Finch.request(req, Embervm.Finch, receive_timeout: @receive_timeout) do
      {:ok, %Finch.Response{status: status}} when status >= 500 ->
        retry_or_fail(method, url, body, attempt, {:unexpected_status, status})

      {:ok, %Finch.Response{} = resp} ->
        {:ok, %{status: resp.status, body: resp.body}}

      {:error, reason} ->
        retry_or_fail(method, url, body, attempt, reason)
    end
  rescue
    e -> retry_or_fail(method, url, body, attempt, {:raised, e})
  end

  defp retry_or_fail(method, url, body, attempt, reason) do
    if attempt < @attempts do
      Process.sleep(@backoff_base_ms * Integer.pow(2, attempt - 1))
      request_with_retry(method, url, body, attempt + 1)
    else
      Logger.warning("embervm s3 client: #{method} #{url} failed after #{@attempts} attempts: #{inspect(reason)}")
      {:error, reason}
    end
  end
end
