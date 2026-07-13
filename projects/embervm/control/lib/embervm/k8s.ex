defmodule Embervm.K8s do
  @moduledoc """
  Minimal in-cluster Kubernetes API client over Finch/Mint. R0 needs exactly
  three calls: the `TokenReview` POST here (submit-API auth, Task 8) and the
  Workload CRD list + status-patch (Task 5, `Embervm.WorkloadWatcher`'s
  reconciler). Hand-rolling over Finch rather than pulling a full `k8s` hex
  library is deliberate: that library drags a large transitive closure, and
  closure-completeness is the whole risk the bandit/plug/finch de-risk PR set
  out to bound. Three endpoints do not justify it.

  `do_request/4` is the shared low-level helper every public call builds on:
  it re-reads the SA token, sets the auth/accept headers, optionally sets
  content-type when a body is present, and maps Finch's own error shape to
  ours. `review_token/1`'s request plumbing predates this refactor and now
  routes through it too, with its return contract unchanged.

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
  @workloads_path "/apis/embervm.dev/v1alpha1/workloads"
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

    case do_request(:post, @tokenreview_path, body, "application/json") do
      {:ok, status, resp_body} -> parse_review(status, resp_body)
      {:error, reason} -> {:error, reason}
    end
  end

  @doc """
  Lists every `Workload` custom resource cluster-wide (the ClusterRole grants
  `list` across namespaces, not scoped to one). Poll-based, not a streaming
  watch: `Embervm.WorkloadWatcher` calls this on a timer, so the list-level
  `resourceVersion` is ignored entirely, there is nothing to resume from.
  Returns the raw `items` array (binary-keyed CR maps, exactly as the API
  server encodes them) so the watcher does its own validation/shaping.
  """
  @spec list_workloads() :: {:ok, [map()]} | {:error, term()}
  def list_workloads do
    case do_request(:get, @workloads_path, nil, nil) do
      {:ok, 200, resp_body} ->
        decoded = :json.decode(resp_body)
        {:ok, Map.get(decoded, "items", [])}

      {:ok, status, _resp_body} ->
        {:error, {:apiserver_status, status}}

      {:error, reason} ->
        {:error, reason}
    end
  end

  @doc """
  Patches one `Workload`'s `/status` subresource with `status_map` (a
  binary-keyed map the caller already built). Uses a JSON merge patch
  (`application/merge-patch+json`), so unspecified status fields are left
  alone, only the keys present in `status_map` are overwritten. This is the
  ONLY write `Embervm.WorkloadWatcher` performs: it never touches `spec`.
  """
  @spec patch_workload_status(String.t(), String.t(), map()) :: :ok | {:error, term()}
  def patch_workload_status(namespace, name, status_map) do
    path =
      "/apis/embervm.dev/v1alpha1/namespaces/#{URI.encode(namespace)}/workloads/#{URI.encode(name)}/status"

    body = :json.encode(%{"status" => status_map}) |> :erlang.iolist_to_binary()

    case do_request(:patch, path, body, "application/merge-patch+json") do
      {:ok, 200, _resp_body} -> :ok
      {:ok, status, _resp_body} -> {:error, {:apiserver_status, status}}
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

  # Shared request plumbing for every API-server call: re-read the SA token
  # fresh (so the hourly projected-token rotation needs no restart), set the
  # bearer + accept headers, and add content-type ONLY when a body is being
  # sent (a bodyless GET must not send content-type). `content_type` is a
  # caller-supplied param rather than inferred from `method` because PATCH
  # needs `application/merge-patch+json` while POST needs plain
  # `application/json`, and inferring from the verb would bake that coupling
  # in here instead of leaving it to each caller.
  defp do_request(method, path, body, content_type) do
    with {:ok, sa_token} <- read_sa_token() do
      headers =
        [{"authorization", "Bearer " <> sa_token}, {"accept", "application/json"}] ++
          if(content_type, do: [{"content-type", content_type}], else: [])

      req = Finch.build(method, api_url(path), headers, body || "")

      case Finch.request(req, Embervm.Finch, receive_timeout: @receive_timeout) do
        {:ok, %Finch.Response{status: status, body: resp_body}} -> {:ok, status, resp_body}
        {:error, reason} -> {:error, reason}
      end
    end
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
