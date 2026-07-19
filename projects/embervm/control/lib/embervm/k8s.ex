defmodule Embervm.K8s do
  @moduledoc """
  Minimal in-cluster Kubernetes API client over Finch/Mint. R0 needs exactly
  four calls: the `TokenReview` POST here (submit-API auth, Task 8) and the
  Workload CRD list + watch + status-patch (Task 5, `Embervm.WorkloadWatcher`'s
  informer). Hand-rolling over Finch rather than pulling a full `k8s` hex
  library is deliberate: that library drags a large transitive closure, and
  closure-completeness is the whole risk the bandit/plug/finch de-risk PR set
  out to bound. Four endpoints do not justify it.

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
  @namespace_file "#{@sa_dir}/namespace"
  @receive_timeout 5_000

  # Watch tuning. The apiserver serves watches from its cache, so the steady-
  # state cost of a held watch is near zero regardless of how many Workloads
  # exist. `timeoutSeconds` asks the apiserver to close the stream itself after
  # ~5 min (a normal, healthy close the watcher simply re-establishes), which
  # bounds how long any one connection lives and gives the apiserver a natural
  # point to rebalance. `@watch_receive_timeout` sits ABOVE that server budget
  # so that on a healthy-but-idle stream the server-side close always fires
  # first; the client-side receive timeout only trips on a genuinely wedged
  # connection (no bookmarks, no close), which is exactly when we want to give
  # up and reconnect. `allowWatchBookmarks` makes the apiserver periodically
  # emit BOOKMARK events carrying a fresh resourceVersion, so a long-idle watch
  # still advances the RV we would resume from after a disconnect.
  @watch_timeout_seconds 300
  @watch_receive_timeout 310_000

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
          # Each pool's value MUST be a keyword list, not a map: Finch runs it
          # through NimbleOptions.validate, which only accepts keyword lists (a
          # map crashes the pool at boot). This branch is deploy-only-reachable
          # (guarded by the in-cluster CA file), so CI, which always takes the
          # empty-pools branch below, cannot exercise it.
          %{default: [conn_opts: [transport_opts: [verify: :verify_peer, cacertfile: @ca_file]]]}

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
  `list` across namespaces, not scoped to one). This is the reconcile leg of
  `Embervm.WorkloadWatcher`'s list-then-watch informer: it runs on boot, and
  again on any watch invalidation (a 410 Expired, a parse/transport error) to
  re-establish current truth before resuming the watch.

  Requested with `resourceVersion=0`, which tells the apiserver "serve this
  from your watch cache, any recent version is fine" rather than doing a
  quorum read against etcd, so even a frequent resync is cheap. Returns the
  raw `items` array (binary-keyed CR maps, exactly as the apiserver encodes
  them) AND the collection-level `metadata.resourceVersion`: that RV is where
  the subsequent watch resumes, so the two must come from the same list
  response, which is why they are returned together here rather than fetched
  separately.
  """
  @spec list_workloads() :: {:ok, [map()], String.t() | nil} | {:error, term()}
  def list_workloads do
    case do_request(:get, @workloads_path <> "?resourceVersion=0", nil, nil) do
      {:ok, 200, resp_body} ->
        decoded = :json.decode(resp_body)
        items = Map.get(decoded, "items", [])
        rv = get_in(decoded, ["metadata", "resourceVersion"])
        {:ok, items, rv}

      {:ok, status, _resp_body} ->
        {:error, {:apiserver_status, status}}

      {:error, reason} ->
        {:error, reason}
    end
  end

  @doc """
  Opens a streaming watch on the `Workload` collection from `resource_version`
  and invokes `on_event` once per delta as it arrives. This is the steady-state
  leg of the informer: after the initial LIST establishes the catalog, the
  watch delivers only what changed, served from the apiserver's cache.

  The call is SYNCHRONOUS: `Finch.stream/5` blocks the calling process for the
  whole lifetime of the stream, so the watcher must run this in a dedicated
  process, never inside the GenServer that owns the catalog (that would freeze
  every catalog read for the stream's lifetime). Returns:

    * `{:ok, :closed}` when the stream ends cleanly (the normal path: the
      apiserver closed it at `timeoutSeconds`, or after emitting a terminal
      ERROR event). The caller decides whether to resume-watch or resync-LIST
      based on the events it saw (an ERROR event means the RV expired).
    * `{:error, reason}` on a non-200 status or a transport error, which the
      caller treats as "resync with backoff".

  `on_event` receives each event as a binary-keyed map
  (`%{"type" => "ADDED"|"MODIFIED"|"DELETED"|"BOOKMARK"|"ERROR", "object" =>
  ...}`), exactly as the apiserver frames it. It is invoked from THIS process
  (the streamer), so implementations hand the event off to the owning
  GenServer (a `send`) rather than mutating shared state directly.
  """
  @spec watch_workloads(String.t() | nil, (map() -> any())) :: {:ok, :closed} | {:error, term()}
  def watch_workloads(resource_version, on_event) do
    query =
      URI.encode_query([
        {"watch", "1"},
        {"allowWatchBookmarks", "true"},
        {"timeoutSeconds", Integer.to_string(@watch_timeout_seconds)},
        {"resourceVersion", resource_version || "0"}
      ])

    with {:ok, sa_token} <- read_sa_token() do
      headers = [{"authorization", "Bearer " <> sa_token}, {"accept", "application/json"}]
      req = Finch.build(:get, api_url(@workloads_path <> "?" <> query), headers, "")
      acc0 = %{status: nil, buffer: "", on_event: on_event}

      result =
        Finch.stream(req, Embervm.Finch, acc0, &watch_reducer/2, receive_timeout: @watch_receive_timeout)

      # Finch.stream/5 returns {:ok, acc} on a completed stream, or the THREE-
      # element {:error, exception, acc} on a transport error mid-stream (NOT a
      # 2-tuple: matching {:error, reason} alone would CaseClause-crash on every
      # disconnect). The acc carries the last-seen HTTP status; a non-200 there
      # means the watch establishment itself was rejected (the apiserver sent an
      # error object, not the NDJSON stream), which we surface as a resync.
      case result do
        {:ok, %{status: 200}} -> {:ok, :closed}
        {:ok, %{status: status}} -> {:error, {:apiserver_status, status}}
        {:error, reason, _acc} -> {:error, reason}
      end
    end
  end

  # Finch.stream/5 reducer. Threads a small acc holding the HTTP status, a
  # frame buffer, and the caller's on_event. We only parse the body once the
  # status is 200: a non-200 watch response carries a JSON error object, not
  # the NDJSON event stream, and must not be fed to the event path.
  defp watch_reducer({:status, status}, acc), do: %{acc | status: status}
  defp watch_reducer({:headers, _headers}, acc), do: acc
  defp watch_reducer({:trailers, _trailers}, acc), do: acc

  defp watch_reducer({:data, data}, %{status: 200} = acc) do
    {lines, rest} = frame_ndjson(acc.buffer, data)
    Enum.each(lines, &dispatch_watch_line(&1, acc.on_event))
    %{acc | buffer: rest}
  end

  defp watch_reducer({:data, _data}, acc), do: acc

  @doc false
  # Splits `buffer <> chunk` into complete newline-terminated lines plus the
  # trailing incomplete remainder (to be prepended to the next chunk). A watch
  # stream is newline-delimited JSON and TCP chunk boundaries fall anywhere, so
  # a single JSON event can straddle two `:data` frames; this reassembles them.
  # Exposed (rather than private) so it can be unit-tested directly, since the
  # streaming path itself needs a live apiserver.
  @spec frame_ndjson(binary(), binary()) :: {[binary()], binary()}
  def frame_ndjson(buffer, chunk) do
    parts = String.split(buffer <> chunk, "\n")
    {complete, [remainder]} = Enum.split(parts, length(parts) - 1)
    {complete, remainder}
  end

  # Blank lines (a stream's trailing newline) carry no event; skip them. A line
  # that fails to decode is skipped rather than raised on: the apiserver writes
  # one complete JSON object per line, so a decode failure would signal a
  # framing bug we do not want to crash the streamer over mid-flight.
  defp dispatch_watch_line("", _on_event), do: :ok

  defp dispatch_watch_line(line, on_event) do
    try do
      on_event.(:json.decode(line))
    catch
      kind, reason ->
        Logger.warning("embervm k8s watch: skipping undecodable event line: #{inspect({kind, reason})}")
        :ok
    end
  end

  @doc """
  Lists the noded pod endpoints backing the headless noded Service by reading its
  EndpointSlices (artifact-decoupling PR-C, C4). Now that noded is a DaemonSet
  behind a HEADLESS Service, the control plane dials each daemon INDIVIDUALLY (one
  WatchNode stream + one SyncRegistry push per pod), so it must discover per-pod
  IPs rather than a single ClusterIP. EndpointSlices are the discovery source: the
  apiserver labels each with `kubernetes.io/service-name=<service>`, and each
  carries `endpoints[].addresses` plus a `targetRef` naming the backing pod.

  Returns `{:ok, [%{id, address}]}` where `id` is the endpoint's node name (the
  stable per-node identity; falls back to the pod name, then the IP) and `address`
  is `"<ip>:<grpc_port>"`, exactly the shape `Embervm.NodeRegistry`/`NodeChannel`
  consume. `publishNotReadyAddresses: true` on the Service means a not-yet-ready
  (unsynced) pod IS listed, which is required: the control plane must dial a fresh
  pod to push the registry that makes it ready. A missing EndpointSlice (Service
  not up yet) returns `{:ok, []}`, so a boot before the DaemonSet is scheduled
  simply discovers nothing and the next reconcile picks them up.

  `service` is the noded Service name, `namespace` the release namespace, and
  `grpc_port` the daemon's gRPC port. Requires `list` on
  `endpointslices.discovery.k8s.io` (granted in the chart RBAC).
  """
  @spec list_noded_endpoints(String.t(), String.t(), non_neg_integer()) ::
          {:ok, [%{id: String.t(), address: String.t()}]} | {:error, term()}
  def list_noded_endpoints(service, namespace, grpc_port) do
    label = URI.encode_query([{"labelSelector", "kubernetes.io/service-name=#{service}"}])
    path = "/apis/discovery.k8s.io/v1/namespaces/#{URI.encode(namespace)}/endpointslices?" <> label

    case do_request(:get, path, nil, nil) do
      {:ok, 200, resp_body} ->
        decoded = :json.decode(resp_body)
        items = Map.get(decoded, "items", [])
        {:ok, endpoints_from_slices(items, grpc_port)}

      {:ok, status, _resp_body} ->
        {:error, {:apiserver_status, status}}

      {:error, reason} ->
        {:error, reason}
    end
  end

  @doc """
  Lists the pods matching `label_selector` in `namespace`, returning one
  `%{name, node_name, phase}` per pod (artifact-decoupling PR-I). The CP dynamic
  sizer (`Embervm.NodeSizer`) uses this to map a node's capacity facts (keyed by
  the K8s node NAME the daemon self-reports) to the noded pod NAME it must address
  the `pods/resize` subresource against, since a DaemonSet pod's name is not
  derivable from the node name alone.

  Requires `list` on `pods` (core API group) in the namespace (granted in the
  chart RBAC). A pod with no `spec.nodeName` (unscheduled) is still returned with
  `node_name: nil`; the sizer skips it. An empty result (no pods yet) is
  `{:ok, []}`, so a boot before the DaemonSet is scheduled simply finds nothing.
  """
  @spec list_pods(String.t(), String.t()) ::
          {:ok, [%{name: String.t(), node_name: String.t() | nil, phase: String.t() | nil}]}
          | {:error, term()}
  def list_pods(namespace, label_selector) do
    query = URI.encode_query([{"labelSelector", label_selector}])
    path = "/api/v1/namespaces/#{URI.encode(namespace)}/pods?" <> query

    case do_request(:get, path, nil, nil) do
      {:ok, 200, resp_body} ->
        decoded = :json.decode(resp_body)
        items = Map.get(decoded, "items", [])

        pods =
          for item <- items do
            %{
              name: get_in(item, ["metadata", "name"]),
              node_name: get_in(item, ["spec", "nodeName"]),
              phase: get_in(item, ["status", "phase"])
            }
          end

        {:ok, pods}

      {:ok, status, _resp_body} ->
        {:error, {:apiserver_status, status}}

      {:error, reason} ->
        {:error, reason}
    end
  end

  @doc """
  Resizes one pod's `container` resources IN PLACE via the `pods/resize`
  subresource (Kubernetes 1.35 InPlacePodVerticalScaling), the CP dynamic-sizing
  actuator (artifact-decoupling PR-I, ADR embervm/012). `requests` and `limits`
  are quantity-string maps (e.g. `%{"cpu" => "100m", "memory" => "8Gi"}`); a
  strategic-merge patch against the resize subresource updates only the named
  container's `resources`, adjusting request AND limit together so the scheduler's
  ledger stays honest.

  QoS INVARIANT (the kubelet REJECTS a resize that would change a pod's QoS class):
  noded is Burstable (CPU request with NO CPU limit, memory request == limit, per
  the repo sizing convention). The caller MUST pass `limits` WITHOUT a `cpu` key
  and keep `memory` present in both maps so the Burstable shape is preserved;
  introducing a CPU limit (making it Guaranteed on a single container) or dropping
  the memory limit would flip QoS and the kubelet returns 422, which this surfaces
  as `{:error, {:apiserver_status, 422}}` (the sizer treats a non-2xx as a
  placement refusal, never an overcommit).

  A 200 is a satisfied resize; a 4xx (e.g. 422 Infeasible / QoS-change) or a
  transport error is returned verbatim for the caller to classify. Requires
  `patch` on `pods/resize` (core API group) in the namespace (granted in the
  chart RBAC).
  """
  @spec resize_pod(String.t(), String.t(), String.t(), %{optional(String.t()) => String.t()}, %{
          optional(String.t()) => String.t()
        }) :: :ok | {:error, term()}
  def resize_pod(namespace, pod_name, container, requests, limits) do
    path =
      "/api/v1/namespaces/#{URI.encode(namespace)}/pods/#{URI.encode(pod_name)}/resize"

    patch = %{
      "spec" => %{
        "containers" => [
          %{
            "name" => container,
            "resources" => %{"requests" => requests, "limits" => limits}
          }
        ]
      }
    }

    body = :json.encode(patch) |> :erlang.iolist_to_binary()

    case do_request(:patch, path, body, "application/strategic-merge-patch+json") do
      {:ok, 200, _resp_body} -> :ok
      {:ok, status, _resp_body} -> {:error, {:apiserver_status, status}}
      {:error, reason} -> {:error, reason}
    end
  end

  @doc """
  The pod's own namespace, read from the projected ServiceAccount `namespace`
  file. Falls back to `default` when the file is absent (local `mix`, ExUnit),
  where EndpointSlice discovery is never exercised anyway.
  """
  @spec namespace() :: String.t()
  def namespace do
    case File.read(@namespace_file) do
      {:ok, ns} -> String.trim(ns)
      {:error, _} -> "default"
    end
  end

  @doc false
  # Flatten a list of EndpointSlice objects into [%{id, address}]. Each slice's
  # `endpoints[]` carries `addresses` (the pod IPs) and optionally a `targetRef`
  # (the pod) and `nodeName` (the host). We key each endpoint by its node name
  # (the stable per-node identity a DaemonSet gives one pod per node), falling back
  # to the pod name then the IP so an id is always present. An endpoint with no
  # address is skipped. Ports come from the slice's `ports[]`, but noded exposes a
  # single named gRPC port so we address by the known grpc_port for determinism.
  # Public (rather than private) so it can be unit-tested directly, since the
  # listing path itself needs a live apiserver (mirrors frame_ndjson/2).
  @spec endpoints_from_slices([map()], non_neg_integer()) :: [%{id: String.t(), address: String.t()}]
  def endpoints_from_slices(items, grpc_port) do
    for slice <- items,
        endpoint <- Map.get(slice, "endpoints", []),
        address <- Map.get(endpoint, "addresses", []),
        address not in [nil, ""] do
      node_name = Map.get(endpoint, "nodeName")
      pod_name = get_in(endpoint, ["targetRef", "name"])
      # Key by node name (the stable per-node identity), else pod name, else the
      # IP, which is always present (the comprehension guards address non-empty).
      id =
        Enum.find([node_name, pod_name, address], address, fn c -> is_binary(c) and c != "" end)

      %{id: id, address: "#{address}:#{grpc_port}"}
    end
  end

  @doc """
  Reads one K8s Secret and returns its `data` map with every value base64-
  decoded (the apiserver always base64-encodes Secret `data` values on the
  wire). This is the R4, D-R4.PR-7.1 MMDS-lite seam: `Embervm.StatefulManager`
  calls this on a FRESH/COLD wake when the workload's catalog entry carries
  `spec.stateful.secretRef`, and feeds the decoded map straight into
  `StartStatefulRequest.mmds_env`. Requires `get` on `secrets` (core API group,
  granted in the chart RBAC) for the Secret's namespace; a plain resource GET,
  mirroring `patch_workload_status`'s single-resource-path shape rather than
  `list_workloads`'s collection shape.

  Returns `{:error, {:apiserver_status, 404}}` for a missing Secret (the
  caller decides fail-open vs fail-closed; this function does no interpreting).
  A value that fails base64 decoding is dropped from the returned map rather
  than failing the whole call, matching the mmds_env boot-args seam's own
  posture of skipping a single malformed entry rather than discarding every
  other one.
  """
  @spec get_secret(String.t(), String.t()) :: {:ok, %{String.t() => String.t()}} | {:error, term()}
  def get_secret(namespace, name) do
    path = "/api/v1/namespaces/#{URI.encode(namespace)}/secrets/#{URI.encode(name)}"

    case do_request(:get, path, nil, nil) do
      {:ok, 200, resp_body} ->
        decoded = :json.decode(resp_body)
        data = Map.get(decoded, "data", %{})
        {:ok, decode_secret_data(data)}

      {:ok, status, _resp_body} ->
        {:error, {:apiserver_status, status}}

      {:error, reason} ->
        {:error, reason}
    end
  end

  # Base64-decodes every value in a Secret's `data` map. A value that is not
  # valid base64 (should not happen against a real apiserver, but a defensive
  # boundary against a malformed or hand-edited Secret) is dropped rather than
  # crashing the whole read, so one bad key does not deny every other secret
  # value the workload legitimately needs.
  defp decode_secret_data(data) do
    for {k, v} <- data, is_binary(v), {:ok, decoded} <- [Base.decode64(v)], into: %{} do
      {k, decoded}
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
