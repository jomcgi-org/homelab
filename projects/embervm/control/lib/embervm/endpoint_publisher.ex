defmodule Embervm.EndpointPublisher do
  @moduledoc """
  The ONLY writer to the xDS sidecar's snapshot API. Holds the desired Envoy
  routing state as a PURE FUNCTION of `Embervm.ServingStore` facts + the
  `Embervm.WorkloadCatalog`, and PUTs it (per serving node) to the loopback
  snapshot API the sidecar serves as CDS/RDS/EDS to the node Envoys (Task 6/7).

  ## the sole-writer boundary (reviewer-enforced)

  No module other than this one references the sidecar URL or HTTP client. The
  lifecycle modules (ServingStore, the Task 8 Activator) mutate FACTS in
  ServingStore and then call `publish/1`; publication follows from the facts. A
  grep for the snapshot path (`/snapshot/`) or the PUT hits exactly this module,
  which is the boundary the plan's standing decision 2 requires.

  ## the pure-function projection

  `desired_for_node/2` renders the snapshot for one node from facts alone:

    * for each serving-class workload in the catalog: a cluster named
      `serve|<workload>`, whose EDS endpoints are that workload's HEALTHY
      `published` instances (`ServingStore.published_endpoints/2`), OR the single
      activator endpoint when the workload has none live-and-healthy (the
      empty-cluster activator swap: the activator is the fallback endpoint that
      wakes the workload on the next request);
    * a route per workload: host from the catalog's `serving.host`, prefix `/`,
      injecting `x-ember-workload: <workload>` so the woken VM (and the activator)
      can resolve which workload a request targets.

  Because the render reads only ServingStore + the catalog (never the durable
  op-log), a projection rebuild followed by a publish is byte-identical to the
  pre-restart snapshot: this is the property test the plan requires and the reason
  the version's ONLY moving part is the counter, not any fact.

  ## version = fixed-width monotonic string (D-R3.5.1)

  The sidecar orders versions by STRING comparison and rejects any PUT not
  strictly greater than the current one (per node). So the version must be
  fixed-width zero-padded, so lexical order equals numeric order. This emits a
  40-char string: a 20-digit zero-padded boot-epoch-millis (captured once at
  init) followed by a 20-digit zero-padded per-node monotonic counter. A
  control-plane restart re-pushes at a higher epoch, so every counter value is
  accepted afresh off Envoy's last-ACKed config; within a boot the counter
  strictly increases. A bare or variable-width integer would break lexically
  ("...10" < "...9"); the zero-padding is load-bearing.

  ## debounce + level-triggered re-push

    * `publish/1` coalesces: it marks the desired state dirty and arms a 50ms
      timer; many transitions in a window flush ONCE, and the flush computes the
      snapshot from the CURRENT facts (not the triggering event), so a burst never
      pushes a stale intermediate.
    * a low-frequency periodic re-push (default 45s) re-PUTs the current desired
      snapshot at a fresh version even when nothing changed. This is the
      level-triggered safety net (ADR embervm/001): the sidecar holds no durable
      state, so if the sidecar CONTAINER restarts on its own, its cache empties and
      the node Envoy is left on its last-ACKed config with no fresh push until the
      next fact change. The periodic re-push makes a sidecar restart self-healing
      without coupling to the control plane's own lifecycle.
    * on BOOT the publisher does ONE synchronous publish before the process
      reports ready, so the fan-out reflects the rebuilt facts before any request
      can arrive (a control-plane restart republishes exactly the same endpoints).

  ## the sidecar PUT never blocks lifecycle

  A PUT failure (sidecar down, a transport error, a version conflict) is logged
  loudly and retried on the next debounce/periodic tick; endpoints keep serving on
  Envoy's LAST-ACKED config meanwhile. The publisher never raises into a
  lifecycle caller: `publish/1` is a cast, and the flush swallows PUT errors into
  a retry, so a wedged sidecar can never stall a bank/relight/destroy.
  """

  use GenServer
  require Logger

  alias Embervm.{NodeCapacity, ServingStore, WorkloadCatalog}

  # Debounce window: coalesce a burst of fact changes into one PUT.
  @default_debounce_ms 50
  # Level-triggered re-push cadence: re-PUT the current desired even if unchanged,
  # so a sidecar-container restart (which empties its volatile cache) self-heals.
  @default_repush_ms 45_000
  # Fixed field width for the version's epoch and counter halves. 20 digits holds
  # any 64-bit value zero-padded, so lexical order equals numeric order.
  @version_width 20

  # The cluster name for a serving workload's endpoints. The `serve|` prefix
  # namespaces serving clusters from any future cluster kind and is the string the
  # node Envoy's route targets; it is an opaque Envoy cluster name, never parsed.
  @cluster_prefix "serve|"
  # The header injected on every serving route so the woken VM / activator resolves
  # which workload a request targets (standing decision 3).
  @workload_header "x-ember-workload"

  # -- Client API ------------------------------------------------------------

  @spec start_link(keyword()) :: GenServer.on_start()
  def start_link(opts) do
    case Keyword.get(opts, :name, __MODULE__) do
      nil -> GenServer.start_link(__MODULE__, opts)
      name -> GenServer.start_link(__MODULE__, opts, name: name)
    end
  end

  @doc """
  Requests a (debounced) republish. A cast: the caller (a ServingStore
  transition, a health flip, the activator) never blocks on the PUT. Coalesces
  with any other request in the 50ms window into one flush over the current facts.
  """
  @spec publish(GenServer.server()) :: :ok
  def publish(server \\ __MODULE__) do
    GenServer.cast(server, :publish)
  end

  @doc """
  Runs one publish SYNCHRONOUSLY (compute desired from current facts, PUT to every
  serving node) and returns after it completes. The boot path and tests drive
  publication deterministically through this; production also debounces via
  `publish/1`.
  """
  @spec flush(GenServer.server()) :: :ok
  def flush(server \\ __MODULE__) do
    GenServer.call(server, :flush)
  end

  @doc """
  The PURE desired-state document for one node, rendered from `store`'s facts +
  the workload `catalog`, at `version`. Exposed for the pure-function tests
  (facts in, snapshot map out) and used by the flush path. Reads ONLY the two
  fact sources, never the durable op-log.
  """
  @spec desired_for_node(map(), String.t()) :: map()
  def desired_for_node(ctx, version) do
    workloads = serving_catalog_workloads(ctx.catalog_table)

    clusters = Enum.map(workloads, &cluster_for(ctx, &1))
    routes = workloads |> Enum.map(&route_for(ctx, &1)) |> Enum.reject(&is_nil/1)

    %{
      version: version,
      clusters: clusters,
      routes: routes
    }
  end

  # -- GenServer callbacks ---------------------------------------------------

  @impl true
  def init(opts) do
    state = %{
      store: Keyword.get(opts, :store, ServingStore),
      catalog_table: Keyword.get(opts, :catalog_table, WorkloadCatalog.table()),
      capacity_table: Keyword.get(opts, :capacity_table, NodeCapacity.table()),
      # The PUT seam: (node_id, desired_map) -> :ok | {:error, reason}. Production
      # dials the loopback sidecar over Finch; tests inject a recorder/fault.
      put_fun: Keyword.get(opts, :put_fun, &default_put/2),
      # The activator's endpoint, reached by the node Envoy when a workload has no
      # healthy published instance (the empty-cluster fallback). %{ip, port} or nil.
      # nil renders an empty cluster (Envoy 503s until an instance publishes), which
      # is correct before the activator (Task 8) is wired.
      activator_endpoint: Keyword.get(opts, :activator_endpoint, nil),
      connect_timeout_ms: Keyword.get(opts, :connect_timeout_ms, 1_000),
      debounce_ms: Keyword.get(opts, :debounce_ms, @default_debounce_ms),
      repush_ms: Keyword.get(opts, :repush_ms, @default_repush_ms),
      clock: Keyword.get(opts, :clock, &default_clock/0),
      # The version epoch: captured ONCE here so a restart re-pushes at a higher
      # epoch (a fresh boot's wall clock), accepted off Envoy's last-ACKed config.
      epoch: Keyword.get(opts, :epoch, default_clock()),
      # Per-node monotonic counter (node_id -> non_neg_integer), advanced on every
      # PUT to that node (change-driven OR periodic), so even an unchanged re-push
      # carries a strictly greater version the sidecar accepts.
      counters: %{},
      # Debounce bookkeeping: a pending timer ref (or nil) and whether a publish is
      # dirty (requested since the last flush).
      debounce_ref: nil,
      # Whether to arm the periodic re-push + run the boot publish. Tests set
      # active: false to drive flush/1 deterministically with no timers.
      active: Keyword.get(opts, :active, true)
    }

    if state.active do
      {:ok, state, {:continue, :boot}}
    else
      {:ok, state}
    end
  end

  # Boot: one SYNCHRONOUS publish so the fan-out reflects the rebuilt facts before
  # readiness, then arm the periodic re-push. A boot PUT failure is logged but does
  # NOT crash the publisher (the periodic tick retries): a sidecar not yet up must
  # not wedge the control plane's boot.
  @impl true
  def handle_continue(:boot, state) do
    state = do_flush(state)
    schedule_repush(state)
    {:noreply, state}
  end

  @impl true
  def handle_cast(:publish, state) do
    {:noreply, arm_debounce(state)}
  end

  @impl true
  def handle_call(:flush, _from, state) do
    {:reply, :ok, do_flush(state)}
  end

  @impl true
  def handle_info(:debounce_flush, state) do
    {:noreply, do_flush(%{state | debounce_ref: nil})}
  end

  def handle_info(:repush, state) do
    state = do_flush(state)
    schedule_repush(state)
    {:noreply, state}
  end

  def handle_info(_msg, state), do: {:noreply, state}

  # Arm the 50ms debounce if none is pending; otherwise coalesce (the existing
  # timer will flush the accumulated changes over current facts). Idempotent under
  # a burst: N casts in a window arm exactly one timer.
  defp arm_debounce(%{debounce_ref: ref} = state) when is_reference(ref), do: state

  defp arm_debounce(state) do
    ref = Process.send_after(self(), :debounce_flush, state.debounce_ms)
    %{state | debounce_ref: ref}
  end

  defp schedule_repush(%{repush_ms: ms}) when ms > 0 do
    Process.send_after(self(), :repush, ms)
  end

  defp schedule_repush(_state), do: :ok

  # -- flush -----------------------------------------------------------------

  # Compute the desired snapshot from CURRENT facts and PUT it to every serving
  # node, each at a freshly-incremented per-node version. A per-node PUT failure is
  # logged and the node's counter is NOT advanced (so a retry re-PUTs at the same
  # next version, still strictly greater than the last ACCEPTED one), leaving other
  # nodes unaffected. Cancels any pending debounce timer it subsumes.
  defp do_flush(state) do
    state = cancel_debounce(state)
    ctx = render_ctx(state)
    nodes = serving_nodes(state)

    Enum.reduce(nodes, state, fn node_id, acc ->
      {version, acc} = next_version(acc, node_id)
      desired = desired_for_node(ctx, version)

      case safe_put(acc.put_fun, node_id, desired) do
        :ok ->
          acc

        {:error, reason} ->
          Logger.error("embervm endpoint publisher: PUT to node #{node_id} failed",
            reason: inspect(reason),
            version: version
          )

          # Roll the counter back so the retry re-uses this version (the sidecar
          # never accepted it, so re-using it is still strictly greater than the
          # last ACCEPTED version there). Endpoints keep serving on Envoy's
          # last-ACKed config until the retry lands.
          rollback_version(acc, node_id)
      end
    end)
  end

  # The render context: the fact-source handles the pure projection reads against.
  # Bundled so desired_for_node/2 takes one struct, not five loose args.
  defp render_ctx(state) do
    %{
      store: state.store,
      catalog_table: state.catalog_table,
      activator_endpoint: state.activator_endpoint,
      connect_timeout_ms: state.connect_timeout_ms
    }
  end

  defp cancel_debounce(%{debounce_ref: ref} = state) when is_reference(ref) do
    Process.cancel_timer(ref)
    %{state | debounce_ref: nil}
  end

  defp cancel_debounce(state), do: state

  # -- serving-node derivation (no hardcoded node-4) -------------------------

  # The set of Envoy node ids to push to = the CONFIGURED id (== the k8s node name
  # the serving Envoy sends as its xDS node-id via --service-node $(NODE_NAME)) of
  # every node currently reporting a serving subnet, i.e. a serving-capable node.
  # Data-driven: v1 yields {node-4} from the node facts, never a constant. Empty
  # (no serving node up yet) is a clean no-op: a workload defined before any
  # serving node exists is valid, it simply has nowhere to publish until a node
  # appears.
  defp serving_nodes(state) do
    NodeCapacity.all(state.capacity_table)
    |> Enum.filter(&serving_capable?/1)
    |> Enum.map(& &1.configured_id)
  end

  defp serving_capable?(fact) do
    cidr = Map.get(fact, :serving_subnet_cidr)
    is_binary(cidr) and cidr != ""
  end

  # -- pure projection -------------------------------------------------------

  # Every serving-class workload in the catalog, so the render enumerates clusters
  # from the DECLARED workloads (a workload with zero live instances still needs an
  # activator-backed cluster to wake it), not only from live instances.
  defp serving_catalog_workloads(catalog_table) do
    WorkloadCatalog.all_names(catalog_table)
    |> Enum.filter(fn name ->
      case WorkloadCatalog.fetch(catalog_table, name) do
        {:ok, %{class: "serving"}} -> true
        _ -> false
      end
    end)
    |> Enum.sort()
  end

  # One cluster per workload: the healthy published endpoints, OR the activator
  # endpoint when there are none (the empty-cluster activator swap). The endpoints
  # are already in a stable (instance-id) order from the store, so the rendered
  # cluster is deterministic across rebuilds.
  defp cluster_for(ctx, workload) do
    endpoints =
      case ServingStore.published_endpoints(ctx.store, workload) do
        [] -> activator_endpoints(ctx)
        eps -> eps
      end

    %{
      name: @cluster_prefix <> workload,
      endpoints: Enum.map(endpoints, &%{ip: &1.ip, port: &1.port}),
      connect_timeout_ms: ctx.connect_timeout_ms
    }
  end

  # The activator fallback: a single endpoint (the control plane's activator
  # listener) the node Envoy routes to when a workload has no live-and-healthy VM,
  # so the first request wakes it. nil (no activator configured) renders an empty
  # cluster, which is a valid CDS entry Envoy 503s against until an instance
  # publishes (correct before Task 8 wires the activator).
  defp activator_endpoints(%{activator_endpoint: %{ip: ip, port: port}})
       when is_binary(ip) and is_integer(port),
       do: [%{ip: ip, port: port}]

  defp activator_endpoints(_ctx), do: []

  # One route per workload: exact host from the catalog, prefix `/`, injecting the
  # workload header. A serving workload with no host in its catalog entry yields no
  # route (nil, filtered out): without a host Envoy cannot match, and rendering a
  # hostless virtual host would be rejected by the sidecar's validate (host is
  # required). The cluster is still rendered so an instance can publish; only the
  # route waits on a host.
  defp route_for(ctx, workload) do
    case serving_host(ctx.catalog_table, workload) do
      host when is_binary(host) and host != "" ->
        %{
          host: host,
          path_prefix: "/",
          cluster: @cluster_prefix <> workload,
          request_headers: %{@workload_header => workload}
        }

      _ ->
        nil
    end
  end

  defp serving_host(catalog_table, workload) do
    case WorkloadCatalog.fetch(catalog_table, workload) do
      {:ok, %{class: "serving", serving: %{host: host}}} -> host
      _ -> nil
    end
  end

  # -- version ---------------------------------------------------------------

  # The next version for a node: bump its counter and format epoch+counter as a
  # fixed-width (40-char) zero-padded string so lexical order equals numeric order
  # (D-R3.5.1). Returns {version_string, state_with_bumped_counter}.
  defp next_version(state, node_id) do
    counter = Map.get(state.counters, node_id, 0) + 1
    state = %{state | counters: Map.put(state.counters, node_id, counter)}
    {format_version(state.epoch, counter), state}
  end

  # Undo the counter bump for a failed PUT so the retry re-uses the same (still
  # strictly-greater-than-last-accepted) version.
  defp rollback_version(state, node_id) do
    counter = max(Map.get(state.counters, node_id, 1) - 1, 0)
    %{state | counters: Map.put(state.counters, node_id, counter)}
  end

  @doc """
  Formats the xDS snapshot version as a 40-char fixed-width string: a
  #{@version_width}-digit zero-padded epoch concatenated with a
  #{@version_width}-digit zero-padded counter. Exposed for the version-format
  test (D-R3.5.1: lexical order must equal numeric order).
  """
  @spec format_version(non_neg_integer(), non_neg_integer()) :: String.t()
  def format_version(epoch, counter) do
    pad(epoch) <> pad(counter)
  end

  defp pad(n) do
    n |> Integer.to_string() |> String.pad_leading(@version_width, "0")
  end

  # -- sidecar PUT (the sole writer) -----------------------------------------

  # Wrap the PUT seam so a raised/exited HTTP client (a wedged sidecar, a dial
  # crash) becomes an {:error, _} the flush retries, never an exception into the
  # publisher's loop.
  defp safe_put(put_fun, node_id, desired) do
    put_fun.(node_id, desired)
  rescue
    e -> {:error, {:put_raised, e}}
  catch
    kind, reason -> {:error, {:put_raised, {kind, reason}}}
  end

  # Production PUT: encode the desired document and PUT it to the loopback sidecar
  # snapshot API over the shared Finch pool. The sidecar binds 127.0.0.1 only, so
  # the URL is always loopback; the port comes from EMBERVM_XDS_HTTP_PORT (the
  # chart wires it from values.xds.httpPort onto the control-plane container too).
  # A 2xx is success; a 409 is a version conflict (a stale/racing PUT), logged and
  # left for the next tick; any other status or transport error is a retryable
  # failure.
  defp default_put(node_id, desired) do
    url = "http://127.0.0.1:#{xds_http_port()}/snapshot/#{node_id}"
    body = encode_json(desired)
    headers = [{"content-type", "application/json"}]
    req = Finch.build(:put, url, headers, body)

    case Finch.request(req, Embervm.Finch, receive_timeout: 5_000) do
      {:ok, %Finch.Response{status: status}} when status in 200..299 ->
        :ok

      {:ok, %Finch.Response{status: status, body: resp}} ->
        {:error, {:sidecar_status, status, resp}}

      {:error, reason} ->
        {:error, reason}
    end
  end

  defp xds_http_port do
    case System.get_env("EMBERVM_XDS_HTTP_PORT") do
      nil -> "18001"
      "" -> "18001"
      port -> port
    end
  end

  defp encode_json(map), do: map |> :json.encode() |> :erlang.iolist_to_binary()

  defp default_clock, do: System.system_time(:millisecond)
end
