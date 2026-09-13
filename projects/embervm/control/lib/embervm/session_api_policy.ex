defmodule Embervm.SessionApiPolicy do
  @moduledoc """
  Application-layer method and path allow-list for the session API.

  This plug runs inside the only Bandit listener, before route matching and
  authentication. That placement makes the policy identical for ClusterIP,
  pod-IP, and loopback callers. It also prevents a malformed session path from
  falling through to the serving activator catch-all.

  The policy deliberately covers only the two session namespaces. Other
  control-plane routes keep their existing router and authentication behavior.
  """

  @behaviour Plug

  import Plug.Conn

  require OpenTelemetry.Tracer, as: Tracer

  @session_segment ~r/\A[A-Za-z0-9][A-Za-z0-9._:-]*\z/
  @denial_body ~s({"error":"session API method or path not allowed","retryable":false})

  @impl true
  def init(opts), do: opts

  @impl true
  def call(conn, _opts) do
    case classify(conn) do
      :outside ->
        conn

      {:allowed, route} ->
        observe(conn, route, true)

      {:denied, route} ->
        conn
        |> observe(route, false)
        |> put_resp_content_type("application/json")
        |> send_resp(403, @denial_body)
        |> halt()
    end
  end

  @doc false
  def classify(%Plug.Conn{} = conn) do
    # This plug runs before Plug.Router's :match plug, so path_info is still
    # percent-encoded here. Decode it with the same primitive Plug.Router uses
    # before deciding whether the request belongs to the guarded namespace.
    decoded_path_info = Enum.map(conn.path_info, &URI.decode/1)

    if session_surface?(decoded_path_info) do
      route = route_template(decoded_path_info)

      if canonical_path?(conn, decoded_path_info) and allowed?(conn.method, decoded_path_info) do
        {:allowed, route}
      else
        {:denied, route}
      end
    else
      :outside
    end
  end

  # Join decoded path_info before classifying the namespace. This catches an
  # encoded slash that turns one raw segment into `v1/sessions` as well as the
  # ordinary segmented form. It is only a detector: canonical_path?/2 still
  # refuses every encoded alias.
  defp session_surface?(path_info) do
    decoded_path = Enum.join(path_info, "/")

    decoded_path == "v1/sessions" or
      String.starts_with?(decoded_path, "v1/sessions/") or
      Regex.match?(~r/\Av1\/workloads\/[^\/]+\/sessions(?:\/|\z)/, decoded_path)
  end

  defp allowed?(method, ["v1", "workloads", workload, "sessions"])
       when method in ["GET", "POST"],
       do: safe_segment?(workload)

  defp allowed?("POST", ["v1", "sessions", session_id, "invoke"]),
    do: safe_segment?(session_id)

  defp allowed?(method, ["v1", "sessions", session_id])
       when method in ["GET", "DELETE"],
       do: safe_segment?(session_id)

  defp allowed?(_method, _path_info), do: false

  # Workload and session identifiers are generated from this conservative
  # alphabet today. Requiring the raw path to be the canonical rendering of
  # path_info rejects percent-encoded separators, encoded aliases, duplicate
  # slashes, dot segments, and trailing slashes before Plug.Router can
  # reinterpret them.
  defp canonical_path?(conn, decoded_path_info) do
    conn.request_path == "/" <> Enum.join(decoded_path_info, "/")
  end

  defp safe_segment?(segment), do: Regex.match?(@session_segment, segment)

  defp route_template(["v1", "workloads", _workload, "sessions"]),
    do: "/v1/workloads/:name/sessions"

  defp route_template(["v1", "sessions", _session_id, "invoke"]),
    do: "/v1/sessions/:id/invoke"

  defp route_template(["v1", "sessions", _session_id]), do: "/v1/sessions/:id"
  defp route_template(_path_info), do: "unmatched"

  defp observe(conn, route, allowed) do
    started_at = :opentelemetry.timestamp()

    register_before_send(conn, fn response ->
      record_request(response, route, allowed, started_at)
      response
    end)
  end

  # Each observation is a one-span root trace. This keeps the tail sampler's
  # session-api policy bounded and prevents a deep invoke trace from consuming
  # the request policy's whole per-second allocation. start_time makes the span
  # duration itself agree with ember.http.duration_ms.
  defp record_request(conn, route, allowed, started_at) do
    duration_ms =
      started_at
      |> elapsed_native()
      |> System.convert_time_unit(:native, :microsecond)
      |> Kernel./(1_000)

    attributes = %{
      "ember.http.duration_ms" => duration_ms,
      "ember.policy.allowed" => allowed,
      "ember.surface" => "session_api",
      "http.request.method" => conn.method,
      "http.response.status_code" => conn.status,
      "http.route" => route
    }

    Tracer.with_span OpenTelemetry.Ctx.new(), "embervm.http.request", %{
      kind: :server,
      start_time: started_at,
      attributes: attributes
    } do
      if conn.status >= 500 do
        Tracer.set_status(:error, "HTTP #{conn.status}")
      end
    end
  end

  defp elapsed_native(started_at), do: max(:opentelemetry.timestamp() - started_at, 0)
end
