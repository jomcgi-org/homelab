defmodule Embervm.K8s do
  @moduledoc """
  Minimal in-cluster Kubernetes API client over Finch/Mint. R0 needs exactly two
  calls: the `TokenReview` POST here (submit-API auth, Task 8) and the Workload
  CRD list+watch (Task 5, added alongside the watcher). Hand-rolling over Finch
  rather than pulling a full `k8s` hex library is deliberate: that library drags a
  large transitive closure, and closure-completeness is the whole risk the
  bandit/plug/finch de-risk PR set out to bound. Two endpoints do not justify it.

  ## In-cluster config

  We dial `https://kubernetes.default.svc:443` by its DNS NAME, not the
  `KUBERNETES_SERVICE_HOST` IP: the apiserver serving cert always carries
  `kubernetes.default.svc` as a SAN, so TLS hostname verification against the
  pod's mounted `ca.crt` passes cleanly, whereas connecting by ClusterIP would
  depend on the IP being in the SAN. The bearer credential is the pod's own
  ServiceAccount token, RE-READ from disk on every call so the hourly projected
  token rotation is picked up with no restart (the auth cache keeps this call
  rate near zero, so per-call file reads cost nothing).

  The Finch pool's TLS is configured by `finch_child_spec/0`: when the SA CA file
  is present (in-cluster) the default pool verifies the peer against it; when it
  is absent (local `mix`, ExUnit) the plain default pool is used, which is why the
  loopback HTTP smoke and the request tests need no cluster.
  """
  require Logger

  @sa_dir "/var/run/secrets/kubernetes.io/serviceaccount"
  @token_file "#{@sa_dir}/token"
  @ca_file "#{@sa_dir}/ca.crt"
  @tokenreview_path "/apis/authentication.k8s.io/v1/tokenreviews"
  @receive_timeout 5_000

  @doc """
  The Finch child spec for the control plane's shared HTTP pool. In-cluster it
  pins the default pool to verify TLS against the SA CA bundle; elsewhere it is a
  plain pool. TLS transport opts are ignored for plaintext (loopback) requests,
  so one pool safely serves both the K8s calls and the health/loopback smoke.
  """
  @spec finch_child_spec() :: Supervisor.child_spec() | {module(), keyword()}
  def finch_child_spec do
    pools =
      case File.exists?(@ca_file) do
        true ->
          %{
            default: %{
              conn_opts: [transport_opts: [verify: :verify_peer, cacertfile: @ca_file]]
            }
          }

        false ->
          %{}
      end

    {Finch, name: Embervm.Finch, pools: pools}
  end

  @doc """
  Submits `token` to the Kubernetes `TokenReview` API and returns the
  authenticated ServiceAccount username. This is the raw reviewer `Embervm.Auth`
  wraps with caching + singleflight; it does NO caching itself and applies NO
  allow-list (that is the Auth layer's job). Requires `create` on
  `tokenreviews.authentication.k8s.io` (granted in the chart RBAC).
  """
  @spec review_token(String.t()) :: {:ok, String.t()} | {:error, term()}
  def review_token(token) do
    body =
      :json.encode(%{
        "apiVersion" => "authentication.k8s.io/v1",
        "kind" => "TokenReview",
        "spec" => %{"token" => token}
      })
      |> :erlang.iolist_to_binary()

    with {:ok, sa_token} <- read_sa_token(),
         url = api_url(@tokenreview_path),
         headers = [
           {"authorization", "Bearer " <> sa_token},
           {"content-type", "application/json"},
           {"accept", "application/json"}
         ],
         req = Finch.build(:post, url, headers, body),
         {:ok, %Finch.Response{status: status, body: resp_body}} <-
           Finch.request(req, Embervm.Finch, receive_timeout: @receive_timeout) do
      parse_review(status, resp_body)
    else
      {:error, reason} -> {:error, reason}
    end
  end

  # TokenReview create returns 201 (some clusters 200); anything else is an
  # API-server error we surface without trusting the token.
  defp parse_review(status, body) when status in [200, 201] do
    decoded = :json.decode(body)
    review_status = Map.get(decoded, "status", %{})

    if Map.get(review_status, "authenticated", false) do
      username = get_in(review_status, ["user", "username"])

      case username do
        nil -> {:error, :no_username}
        name -> {:ok, name}
      end
    else
      {:error, :unauthenticated}
    end
  end

  defp parse_review(status, _body) do
    {:error, {:apiserver_status, status}}
  end

  defp read_sa_token do
    case File.read(@token_file) do
      {:ok, token} -> {:ok, String.trim(token)}
      {:error, reason} -> {:error, {:sa_token_unreadable, reason}}
    end
  end

  defp api_url(path) do
    port = System.get_env("KUBERNETES_SERVICE_PORT_HTTPS") || "443"
    "https://kubernetes.default.svc:#{port}#{path}"
  end
end
