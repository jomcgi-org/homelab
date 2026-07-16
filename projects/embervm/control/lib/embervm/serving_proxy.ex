defmodule Embervm.ServingProxy do
  @moduledoc """
  The activator's one proxy hop: stream a MISS request to the freshly-woken
  serving VM's tap `ip:port` over HTTP and stream the guest response back to the
  caller. This runs ONLY on the activator miss path (the first request to a cold
  workload, standing decision 1); every SUBSEQUENT request reaches the VM
  node-Envoy-direct with zero control-plane involvement, so this hop is
  lifecycle-rate, not per-request.

  ## streaming the RESPONSE (what must not buffer)

  The request body is already fully received by the router under the 8 MiB
  envelope cap, so it is passed as an in-memory binary (bounded, fine). The
  RESPONSE is the part that must not be buffered: a serving guest may stream a
  large or long-lived body, so this uses `Finch.stream/5` to receive the upstream
  status + headers, open a chunked response to the caller (`Plug.Conn.send_chunked`),
  and forward each body chunk as it arrives (`Plug.Conn.chunk/2`). Nothing beyond
  one chunk is held at a time.

  ## header carry (R1 guest-response rules)

  The guest's response headers are replayed onto the caller under the same
  defense-in-depth deny-list the router's `send_guest_result` uses for session/
  task results: framing and hop-by-hop headers are the connection's business, not
  the guest's, so they are dropped and re-derived. A guest `content-type` wins;
  absent, `application/octet-stream`. Because the response is chunked, the server
  owns transfer-encoding/content-length regardless.
  """

  require Logger

  # Framing / hop-by-hop headers the server MUST own (the guest may not set them),
  # matching Embervm.Router.@denied_guest_headers. Compared lowercased. We also
  # drop content-length here because the response is re-framed as chunked.
  @denied_headers MapSet.new([
                    "content-length",
                    "transfer-encoding",
                    "connection",
                    "keep-alive",
                    "upgrade",
                    "host",
                    "te",
                    "trailer",
                    "proxy-authorization",
                    "proxy-authenticate"
                  ])

  @doc """
  Proxies `req` (`%{method, path, headers, body}`) to the serving VM at `ip:port`
  and streams the response back onto `conn`. Returns the (sent) `conn` on success,
  or `{:error, reason}` when the upstream could not be reached or the stream
  failed before any byte was sent (the caller then 502/503s). `opts` carries
  `:finch` (the pool name, default `Embervm.Finch`) and `:receive_timeout`.

  Once the first chunk has been sent the response is committed; a mid-stream
  upstream error can only truncate it (logged), never convert to a clean error
  status, exactly as any streaming proxy.
  """
  @spec proxy(Plug.Conn.t(), %{ip: String.t(), port: non_neg_integer()}, map(), keyword()) ::
          {:ok, Plug.Conn.t()} | {:error, term()}
  def proxy(conn, %{ip: ip, port: port}, req, opts \\ []) do
    finch = Keyword.get(opts, :finch, Embervm.Finch)
    receive_timeout = Keyword.get(opts, :receive_timeout, 60_000)

    url = "http://#{ip}:#{port}#{Map.get(req, :path) || "/"}"
    method = req |> Map.get(:method, "POST") |> to_method()
    headers = forward_request_headers(Map.get(req, :headers, %{}))
    body = Map.get(req, :body) || ""

    finch_req = Finch.build(method, url, headers, body)

    # The stream reducer threads {conn, sent?}: on the first :headers it opens the
    # chunked response; on each :data it forwards a chunk; :status is captured to set
    # the response code before send_chunked. A pre-first-byte error returns {:error}
    # so the caller can still choose a clean status.
    acc0 = %{conn: conn, status: 200, resp_headers: [], sent?: false, error: nil}

    result =
      Finch.stream(finch_req, finch, acc0, &stream_reducer/2, receive_timeout: receive_timeout)

    case result do
      {:ok, %{sent?: true, conn: sent_conn}} ->
        {:ok, sent_conn}

      {:ok, %{sent?: false, error: nil, conn: c, status: status, resp_headers: hdrs}} ->
        # The upstream completed with headers but no body chunk (an empty 204/200):
        # open + close a chunked response so the caller still gets the status+headers.
        c = open_chunked(c, status, hdrs)
        {:ok, c}

      {:ok, %{error: reason}} when not is_nil(reason) ->
        {:error, {:proxy_stream, reason}}

      {:error, reason} ->
        {:error, {:proxy_connect, reason}}
    end
  end

  # -- stream reducer --------------------------------------------------------

  defp stream_reducer({:status, status}, acc), do: %{acc | status: status}

  defp stream_reducer({:headers, headers}, acc) do
    %{acc | resp_headers: acc.resp_headers ++ headers}
  end

  defp stream_reducer({:data, chunk}, %{sent?: false} = acc) do
    # First data chunk: commit the response (status + carried headers) as chunked,
    # then write this chunk.
    conn = open_chunked(acc.conn, acc.status, acc.resp_headers)
    write_chunk(%{acc | conn: conn, sent?: true}, chunk)
  end

  defp stream_reducer({:data, chunk}, %{sent?: true} = acc) do
    write_chunk(acc, chunk)
  end

  defp stream_reducer(_other, acc), do: acc

  defp write_chunk(acc, chunk) do
    case Plug.Conn.chunk(acc.conn, chunk) do
      {:ok, conn} ->
        %{acc | conn: conn}

      {:error, reason} ->
        # The caller connection dropped mid-stream: record it (the response is
        # already committed, so this only truncates) and stop writing.
        Logger.warning("embervm serving proxy: chunk write failed", reason: inspect(reason))
        %{acc | error: reason}
    end
  end

  # Open the chunked response: set the carried (allow-listed) headers, ensure a
  # content-type, and send_chunked with the guest status.
  defp open_chunked(conn, status, headers) do
    allowed =
      Enum.filter(headers, fn {k, _v} ->
        not MapSet.member?(@denied_headers, String.downcase(to_string(k)))
      end)

    conn =
      Enum.reduce(allowed, conn, fn {k, v}, acc ->
        Plug.Conn.put_resp_header(acc, String.downcase(to_string(k)), to_string(v))
      end)

    conn =
      if has_header?(allowed, "content-type") do
        conn
      else
        Plug.Conn.put_resp_content_type(conn, "application/octet-stream")
      end

    Plug.Conn.send_chunked(conn, status)
  end

  # Only the guest-safe request headers reach the VM: strip framing/hop-by-hop, so
  # a stale content-length or connection header from the envelope cannot corrupt
  # the proxied request. content-type + the x-ember-* routing headers pass through.
  defp forward_request_headers(headers) when is_map(headers) do
    for {k, v} <- headers,
        not MapSet.member?(@denied_headers, String.downcase(to_string(k))),
        do: {String.downcase(to_string(k)), to_string(v)}
  end

  defp forward_request_headers(_), do: []

  defp has_header?(pairs, name) do
    Enum.any?(pairs, fn {k, _v} -> String.downcase(to_string(k)) == name end)
  end

  defp to_method(m) when is_atom(m), do: m

  defp to_method(m) when is_binary(m) do
    case String.upcase(m) do
      "GET" -> :get
      "POST" -> :post
      "PUT" -> :put
      "PATCH" -> :patch
      "DELETE" -> :delete
      "HEAD" -> :head
      "OPTIONS" -> :options
      _ -> :post
    end
  end
end
