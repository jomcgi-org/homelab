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

  @session_segment ~r/\A[A-Za-z0-9][A-Za-z0-9._:-]*\z/
  @denial_body ~s({"error":"session API method or path not allowed","retryable":false})

  @impl true
  def init(opts), do: opts

  @impl true
  def call(conn, _opts) do
    case classify(conn) do
      :outside ->
        conn

      {:allowed, _route} ->
        conn

      {:denied, _route} ->
        conn
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
end
