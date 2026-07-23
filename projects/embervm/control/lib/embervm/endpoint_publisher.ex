defmodule Embervm.EndpointPublisher do
  @moduledoc """
  The ONLY writer to the xDS sidecar's snapshot API. Holds the desired Envoy
  routing state as a PURE FUNCTION of `Embervm.ServingStore` + `Embervm.StatefulStore`
  facts + the `Embervm.WorkloadCatalog`, and PUTs it (per serving node) to the
  loopback snapshot API the sidecar serves as CDS/RDS/EDS/LDS to the node Envoys
  (Task 6/7). Serving is the L7 (host/path) fan-out; stateful (R4) adds the L4
  (raw TCP) singleton-sandbox listeners + clusters, both rendered from ETS facts.

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
      can resolve which workload a request targets;
    * for each STATEFUL-class workload (R4): an L4 listener (`state-<listen_port>`)
      plus a cluster named `state|<workload>` whose single endpoint is the live
      instance (`StatefulStore.published_endpoint/1`) OR the stateful activator's
      TCP fallback at `{activator_ip, workload.stateful.listen_port}` (Task 8: the
      activator resolves the workload by the LOCAL ACCEPT PORT it is dialed on,
      so the fallback endpoint MUST be per-workload, not one fixed
      `{ip, port}`; see the `activator_ip` option below). A cold stateful
      workload with NO `activator_ip` configured emits NOTHING (no listener, no
      cluster), so the sidecar never receives an empty-endpoints cluster its
      validate rejects; the `listeners` key is omitted entirely when there are
      no L4 listeners, so a serving-only node's payload is byte-identical to the
      pre-R4 wire.

  Because the render reads only ServingStore + StatefulStore + the catalog (never
  the durable op-log), a projection rebuild followed by a publish is byte-identical
  to the pre-restart snapshot: this is the property test the plan requires and the
  reason the version's ONLY moving part is the counter, not any fact.

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

  # Tracer.with_span/set_attributes are OpenTelemetry.Tracer MACROS, so the module
  # must be required even though it is called fully-qualified via the alias.
  require OpenTelemetry.Tracer, as: Tracer

  alias Embervm.{GroupStore, NodeCapacity, ServingStore, StatefulStore, WorkloadCatalog}

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

  # The cluster name for a STATEFUL workload's single L4 endpoint (R4). The
  # `state|` prefix namespaces stateful clusters from serving (`serve|`) ones so a
  # workload named the same in both classes never collides on a cluster name; it is
  # an opaque Envoy cluster name, never parsed.
  @stateful_cluster_prefix "state|"
  # The listener name prefix for a stateful workload's L4 TCP listener. The name is
  # `state-<listen_port>` (the bind port disambiguates, and the listen ports are
  # unique across stateful workloads by the WorkloadWatcher's validation), so two
  # stateful workloads never collide on a listener name either.
  @stateful_listener_prefix "state-"

  # The cluster name for a COMPOSITE group's single L4 entry endpoint (R5). The
  # `group|` prefix namespaces group clusters from serving (`serve|`) and stateful
  # (`state|`) ones so a workload named the same in more than one class never
  # collides on a cluster name; it is an opaque Envoy cluster name, never parsed.
  @group_cluster_prefix "group|"
  # The listener name prefix for a composite group's L4 TCP entry listener. The name
  # is `group-<listen_port>` (the entry.listenPort disambiguates, unique across
  # composite workloads by the WorkloadWatcher's validation), so two groups never
  # collide on a listener name either.
  @group_listener_prefix "group-"

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
  The PURE desired-state document for one node at `version`, rendered from `ctx`,
  a render context bundling the fact sources: `%{store, stateful_store,
  catalog_table, activator_endpoint, activator_ip, connect_timeout_ms}`
  (built by `render_ctx/1` from the publisher's state). Reads ONLY the
  ServingStore + StatefulStore facts + the WorkloadCatalog it names, never the
  durable op-log. Exposed for the pure-function tests (facts in, snapshot map out)
  and used by the flush path.
  """
  @spec desired_for_node(map(), String.t()) :: map()
  def desired_for_node(ctx, version) do
    workloads = serving_catalog_workloads(ctx.catalog_table)

    serving_clusters = Enum.map(workloads, &cluster_for(ctx, &1))
    routes = workloads |> Enum.map(&route_for(ctx, &1)) |> Enum.reject(&is_nil/1)

    # Stateful (L4) workloads (R4): for each stateful-class workload, a listener +
    # a cluster whose single endpoint is the live instance OR the activator TCP
    # fallback. A workload with NEITHER a live instance NOR a configured activator
    # emits nothing (see stateful_render/1), so a cold, un-wakeable stateful
    # workload contributes no empty-endpoints cluster the sidecar's validate rejects.
    {stateful_clusters, stateful_listeners} = stateful_render(ctx)

    # Composite (L4) workloads (R5): for each composite-class workload, a listener +
    # a cluster whose single endpoint is the live ENTRY member (GroupStore.entry_endpoint/2)
    # OR the activator TCP fallback (banked OR no instance at all, for the LIFE of the
    # CR, decision 8). Same cold-and-no-activator omission as stateful, so a group with
    # no live entry and no activator contributes no empty-endpoints cluster. The
    # rendered wire is byte-identical to R4 when there are no composite workloads (the
    # clusters/listeners lists are empty), preserving the no-composite regression.
    {group_clusters, group_listeners} = group_render(ctx)

    listeners = stateful_listeners ++ group_listeners

    base = %{
      version: version,
      # Serving clusters first, then stateful, then composite: the serving-only slice
      # (stateful + group clusters == []) is byte-identical to the pre-R4 document,
      # and the serving+stateful slice (group clusters == []) is byte-identical to R4.
      clusters: serving_clusters ++ stateful_clusters ++ group_clusters,
      routes: routes
    }

    # Only add the `listeners` key when there is at least one L4 listener to emit.
    # A serving-only node (no stateful/composite workloads, or none wakeable)
    # therefore emits the EXACT map it emitted before R4, no `listeners` key at all.
    # (The JSON encoder does not honour Go's struct-tag omitempty, so emitting
    # `listeners: []` WOULD change the wire bytes; the Go struct's omitempty means the
    # absent key decodes to a nil slice, which is what the serving-only path needs.)
    if listeners == [] do
      base
    else
      Map.put(base, :listeners, listeners)
    end
  end

  # -- GenServer callbacks ---------------------------------------------------

  @impl true
  def init(opts) do
    state = %{
      store: Keyword.get(opts, :store, ServingStore),
      # The stateful (L4) hot-set store, read purely for stateful workloads'
      # published_endpoint/1 (the single live endpoint or nil). Injected in tests;
      # production supervises the singleton Embervm.StatefulStore.
      stateful_store: Keyword.get(opts, :stateful_store, StatefulStore),
      # The composite (L4) group hot-set store, read purely for composite workloads'
      # entry_endpoint/2 (the single live entry endpoint or nil). Injected in tests;
      # production supervises the singleton Embervm.GroupStore.
      group_store: Keyword.get(opts, :group_store, GroupStore),
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
      # The STATEFUL activator's IP (Task 8), a SINGLE address the node Envoy's
      # stateful listener dials on the workload's OWN `stateful.listen_port` when
      # a workload has no live instance (the wake-on-connect fallback). This is
      # deliberately NOT a fixed {ip, port} pair like activator_endpoint: the L4
      # activator resolves the workload from the LOCAL ACCEPT PORT it is dialed
      # on (there is no header at L4, decision 5), so every stateful workload's
      # fallback cluster must carry a DIFFERENT port (its own listen_port) at the
      # SAME activator ip. String or nil; nil means a cold stateful workload
      # emits NO listener/cluster at all (it cannot be woken yet), so the
      # sidecar never sees an empty-endpoints cluster. Distinct from
      # activator_endpoint (the L7 serving fallback, one fixed {ip, port}
      # because the serving activator resolves the workload from the injected
      # x-ember-workload HTTP header instead).
      activator_ip: Keyword.get(opts, :activator_ip, nil),
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
      # The OTel timestamp (native units) of the OLDEST pending fact-change since the
      # last flush, set when a debounce window opens and cleared on flush. The
      # publish_flush span (Task 10) measures fact-change -> sidecar ACK from here, so
      # gate 4's "fact change to Envoy ACK p95" is derivable. nil = a periodic re-push
      # with no pending change (the publish_flush span is then a re-push, start=now).
      dirty_since: nil,
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
    # Stamp the oldest pending fact-change (the window's first request) so the flush's
    # publish_flush span measures the true fact-change -> ACK latency, not just the
    # PUT wall time. Coalesced later requests in the window keep this oldest stamp.
    %{state | debounce_ref: ref, dirty_since: state.dirty_since || :opentelemetry.timestamp()}
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
    # The oldest pending fact-change this flush resolves (nil on a pure re-push). Used
    # as the publish_flush span start so gate 4's fact-change -> ACK p95 is derivable.
    dirty_since = state.dirty_since
    endpoint_count = total_endpoint_count(ctx)

    state =
      Enum.reduce(nodes, state, fn node_id, acc ->
        {version, acc} = next_version(acc, node_id)
        desired = desired_for_node(ctx, version)
        put_result = safe_put(acc.put_fun, node_id, desired)
        emit_publish_flush_span(node_id, version, endpoint_count, dirty_since, put_result)

        case put_result do
          :ok ->
            acc

          {:error, reason} ->
            Logger.error("embervm endpoint publisher: PUT to node #{node_id} failed",
              reason: inspect(reason),
              version: version
            )

            # ALWAYS-INCREMENT: the counter is NOT rolled back on a failed PUT, so the
            # next flush strictly ADVANCES the version. Rolling back would re-send the
            # same version, which the sidecar 409s if the "failed" PUT was actually
            # ACCEPTED but its HTTP ACK was lost (accept-then-lost-ACK) -- a wedge that
            # only the periodic re-push could unstick. A gap in the version sequence is
            # harmless: monotonicity needs only strictly-greater, and the next flush's
            # higher version is accepted whether or not this PUT landed. Endpoints keep
            # serving on Envoy's last-ACKed config until the retry lands.
            acc
        end
      end)

    # The pending fact-change window is now resolved: clear the oldest-dirty stamp so
    # the next window opens a fresh one.
    %{state | dirty_since: nil}
  end

  # The publish_flush span (Task 10): one span per node PUT, its start pinned to the
  # OLDEST pending fact-change (dirty_since) so its duration is the true fact-change
  # -> sidecar ACK latency the gate-4 p95 reads, NOT just the PUT wall time. On a
  # pure re-push (no pending change) it starts now (a ~PUT-latency span). ember.
  # ack_ok is the sidecar ACK success the publication-failure alert reads; ember.
  # publish_ms is the same duration as a queryable attribute. A re-push flush with
  # tracing off is a clean no-op.
  defp emit_publish_flush_span(node_id, version, endpoint_count, dirty_since, put_result) do
    now = :opentelemetry.timestamp()
    start_time = dirty_since || now
    ack_ok = put_result == :ok

    Tracer.with_span "embervm.serving.publish_flush",
                     %{
                       start_time: start_time,
                       attributes: %{
                         "ember.node_id" => node_id,
                         "ember.version" => version,
                         "ember.endpoint_count" => endpoint_count,
                         "ember.ack_ok" => ack_ok,
                         "ember.publish_ms" => System.convert_time_unit(now - start_time, :native, :millisecond)
                       }
                     } do
      # ember.ack_ok is the queryable success/fail signal the publication-failure
      # alert reads; a failed PUT keeps ack_ok=false and publish_ms captures the
      # latency to the failing ACK. (No span status API is used here to stay on the
      # OTel surface the rest of the control plane already exercises.)
      :ok
    end
  end

  # The render context: the fact-source handles the pure projection reads against.
  # Bundled so desired_for_node/2 takes one struct, not five loose args.
  defp render_ctx(state) do
    %{
      store: state.store,
      stateful_store: state.stateful_store,
      group_store: state.group_store,
      catalog_table: state.catalog_table,
      activator_endpoint: state.activator_endpoint,
      activator_ip: state.activator_ip,
      # Node facts snapshotted once per render (ADR embervm/018 Fork A): the serving
      # activator fallback PREFERS a node's advertised activator_endpoint over the
      # CP-injected address, so the wake target survives a CP Recreate. Captured in
      # the ctx so the render stays a pure function of the ctx (the byte-identical
      # rebuild property EDS relies on).
      node_facts: NodeCapacity.all(state.capacity_table),
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

  # Total REAL healthy-published endpoints across every serving workload (the
  # activator fallback is not counted: it is not a served instance). The
  # publish_flush span carries this as ember.endpoint_count so a publication is
  # correlatable to how many endpoints it fanned out.
  defp total_endpoint_count(ctx) do
    serving_catalog_workloads(ctx.catalog_table)
    |> Enum.reduce(0, fn workload, acc ->
      acc + length(ServingStore.published_endpoints(ctx.store, workload))
    end)
  end

  # One cluster per workload: the healthy published endpoints, OR the activator
  # endpoint when there are none (the empty-cluster activator swap). The endpoints
  # are already in a stable (instance-id) order from the store, so the rendered
  # cluster is deterministic across rebuilds.
  defp cluster_for(ctx, workload) do
    endpoints =
      case ServingStore.published_endpoints(ctx.store, workload) do
        [] -> activator_endpoints(ctx, workload)
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
  # The activator fallback for `workload`, PREFERRING a node's own advertised
  # activator endpoint (ADR embervm/018 Fork A) over the CP-injected address. A
  # node's advertised endpoint is a node address (stable across a CP Recreate),
  # so a scaled-to-zero workload's first request survives a CP roll; the CP
  # address is the fallback for nodes that do not advertise one (pre-018 daemons),
  # which keeps a half-rolled fleet correct with no flag day.
  defp activator_endpoints(ctx, workload) do
    case node_advertised_activator(ctx, workload) do
      %{ip: ip, port: port} when is_binary(ip) and is_integer(port) ->
        [%{ip: ip, port: port}]

      _ ->
        cp_activator_endpoint(ctx)
    end
  end

  # Pick a node-advertised activator endpoint for the workload. Among nodes that
  # advertise one, PREFER a node whose base for this workload is READY (it can
  # actually cold-boot the workload; #3993 flagged this as easy to get subtly
  # wrong), falling back to any advertiser. Deterministic: candidates are sorted by
  # configured_id and the first is taken, so the rendered fallback is stable across
  # rebuilds (the byte-identical render property). nil when no node advertises one.
  defp node_advertised_activator(ctx, workload) do
    advertisers =
      ctx
      # A ctx built by a path that predates node-local activators (e.g. the group
      # render's own ctx) carries no node_facts: treat it as no advertisers, so the
      # CP-injected fallback is used and that render is unchanged.
      |> Map.get(:node_facts, [])
      |> Enum.filter(&is_map(Map.get(&1, :activator_endpoint)))
      |> Enum.sort_by(& &1.configured_id)

    ready = Enum.filter(advertisers, &workload_base_ready?(&1, workload))

    case List.first(ready) || List.first(advertisers) do
      nil -> nil
      fact -> fact.activator_endpoint
    end
  end

  defp workload_base_ready?(fact, workload) do
    case Map.get(Map.get(fact, :workloads, %{}), workload) do
      %{base_state: :BASE_BUILD_STATE_READY} -> true
      _ -> false
    end
  end

  # The CP-injected activator (the pre-018 fallback), rendered when no node
  # advertises its own. nil (none configured) yields an empty cluster, a valid CDS
  # entry Envoy 503s against until an instance publishes.
  defp cp_activator_endpoint(%{activator_endpoint: %{ip: ip, port: port}})
       when is_binary(ip) and is_integer(port),
       do: [%{ip: ip, port: port}]

  defp cp_activator_endpoint(_ctx), do: []

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

  # -- stateful (L4) projection (R4) -----------------------------------------

  # Render the L4 listeners + clusters for every stateful-class catalog workload.
  # Returns {clusters, listeners}. For each stateful workload the endpoint is the
  # single live instance (StatefulStore.published_endpoint/1) OR the activator TCP
  # fallback; a workload with NEITHER (cold AND no activator configured) emits
  # nothing at all, so the sidecar never receives an empty-endpoints cluster (its
  # validate() rejects a cluster whose endpoints are all invalid, and a listener
  # must reference a defined cluster). Both lists are catalog-ordered (the workloads
  # are sorted), so the render is deterministic across rebuilds.
  defp stateful_render(ctx) do
    stateful_catalog_entries(ctx.catalog_table)
    |> Enum.reduce({[], []}, fn {workload, cfg}, {clusters, listeners} ->
      case stateful_endpoint(ctx, workload, cfg.listen_port) do
        nil ->
          # Cold workload with no activator wired: it cannot be woken yet, so emit no
          # listener/cluster. Logged so an operator can see WHY a declared stateful
          # workload is absent from the fan-out (it appears once the activator lands).
          Logger.debug(
            "embervm endpoint publisher: stateful workload #{workload} has no live instance " <>
              "and no activator_ip configured; emitting no listener/cluster (cannot wake yet)"
          )

          {clusters, listeners}

        endpoint ->
          cluster = %{
            name: @stateful_cluster_prefix <> workload,
            endpoints: [%{ip: endpoint.ip, port: endpoint.port}],
            connect_timeout_ms: ctx.connect_timeout_ms
          }

          listen_port = cfg.listen_port

          listener = %{
            name: @stateful_listener_prefix <> Integer.to_string(listen_port),
            port: listen_port,
            cluster: @stateful_cluster_prefix <> workload
          }

          {clusters ++ [cluster], listeners ++ [listener]}
      end
    end)
  end

  # Every stateful-class workload in the catalog paired with its stateful config
  # (for the listen_port), catalog-ordered by name so the render is deterministic.
  # A stateful entry with no listenPort is skipped (it cannot form a listener); the
  # WorkloadWatcher rejects such a spec, so this is defence-in-depth, not a live path.
  defp stateful_catalog_entries(catalog_table) do
    WorkloadCatalog.all_names(catalog_table)
    |> Enum.sort()
    |> Enum.flat_map(fn name ->
      case WorkloadCatalog.fetch(catalog_table, name) do
        {:ok, %{class: "stateful", stateful: %{listen_port: lp} = cfg}} when is_integer(lp) ->
          [{name, cfg}]

        _ ->
          []
      end
    end)
  end

  # The endpoint the stateful workload's L4 cluster should carry: the single live
  # instance if one is serving-and-healthy, else the activator's fallback endpoint
  # AT THIS WORKLOAD'S OWN listen_port (the activator resolves the workload from
  # the local accept port, so it must be dialed on the workload's port, never a
  # single shared one), else nil (cold and no activator_ip => skipped upstream).
  defp stateful_endpoint(ctx, workload, listen_port) do
    case StatefulStore.published_endpoint(ctx.stateful_store, workload) do
      %{ip: ip, port: port} when is_binary(ip) and ip != "" and is_integer(port) ->
        %{ip: ip, port: port}

      _ ->
        activator_tcp_endpoint(ctx, workload, listen_port)
    end
  end

  # The L4 activator fallback for a cold stateful workload (ADR embervm/018 Phase
  # 2): PREFER the workload's ANCHOR node's advertised activator_ip, falling back
  # to the CP-injected activator_ip. Unlike serving (any READY node can cold-boot),
  # only the brick that physically holds the volume can relight a stateful workload,
  # so the fallback must point at the volume's anchor node. Rendered at the
  # workload's OWN listen_port (the activator resolves the workload from the local
  # accept port). nil (no anchor advert and no CP address) => skipped upstream.
  defp activator_tcp_endpoint(ctx, workload, listen_port) when is_integer(listen_port) do
    ip = anchor_activator_ip(ctx, workload) || Map.get(ctx, :activator_ip)

    if is_binary(ip) and ip != "" do
      %{ip: ip, port: listen_port}
    else
      nil
    end
  end

  defp activator_tcp_endpoint(_ctx, _workload, _listen_port), do: nil

  # Arity-2 form for the COMPOSITE lane (group_endpoint): a composite has no single
  # volume anchor, so it keeps the pre-018 CP-injected activator_ip fallback
  # unchanged (node-local composite relight is Phase 3, out of scope here). The
  # stateful lane uses the arity-3 form above with its anchor-node preference.
  defp activator_tcp_endpoint(%{activator_ip: ip}, listen_port)
       when is_binary(ip) and ip != "" and is_integer(listen_port),
       do: %{ip: ip, port: listen_port}

  defp activator_tcp_endpoint(_ctx, _listen_port), do: nil

  # The advertised activator_ip of the volume's anchor node, or nil when the
  # workload has no volume yet (never woken, so no anchor) or the anchor node
  # advertises no activator (pre-018 daemon => the CP address is used instead).
  defp anchor_activator_ip(ctx, workload) do
    with %{node_id: anchor} when is_binary(anchor) <-
           StatefulStore.get_volume(ctx.stateful_store, workload),
         fact when is_map(fact) <-
           Enum.find(Map.get(ctx, :node_facts, []), &(Map.get(&1, :configured_id) == anchor)),
         ip when is_binary(ip) and ip != "" <- Map.get(fact, :activator_ip) do
      ip
    else
      _ -> nil
    end
  end

  # -- composite (L4) projection (R5) ----------------------------------------

  # Render the L4 listeners + clusters for every composite-class catalog workload.
  # Returns {clusters, listeners}. For each composite workload the endpoint is the
  # single live ENTRY member (GroupStore.entry_endpoint/2) OR the activator TCP
  # fallback at the group's entry.listenPort (banked OR no instance at all, decision
  # 8: the activator is the fallback for the LIFE of the CR); a workload with NEITHER
  # (cold AND no activator configured) emits nothing, so the sidecar never receives an
  # empty-endpoints cluster. Both lists are catalog-ordered (the workloads are sorted),
  # so the render is deterministic across rebuilds. Byte-identical to the R4 shape when
  # there are no composite workloads (both lists empty).
  defp group_render(ctx) do
    group_catalog_entries(ctx.catalog_table)
    |> Enum.reduce({[], []}, fn {workload, cfg}, {clusters, listeners} ->
      case group_endpoint(ctx, workload, cfg.listen_port) do
        nil ->
          Logger.debug(
            "embervm endpoint publisher: composite workload #{workload} has no running entry " <>
              "and no activator_ip configured; emitting no listener/cluster (cannot wake yet)"
          )

          {clusters, listeners}

        endpoint ->
          cluster = %{
            name: @group_cluster_prefix <> workload,
            endpoints: [%{ip: endpoint.ip, port: endpoint.port}],
            connect_timeout_ms: ctx.connect_timeout_ms
          }

          listen_port = cfg.listen_port

          listener = %{
            name: @group_listener_prefix <> Integer.to_string(listen_port),
            port: listen_port,
            cluster: @group_cluster_prefix <> workload
          }

          {clusters ++ [cluster], listeners ++ [listener]}
      end
    end)
  end

  # Every composite-class workload in the catalog paired with its entry config (for
  # the entry listenPort), catalog-ordered by name so the render is deterministic. A
  # composite entry with no entry.listenPort is skipped (it cannot form a listener);
  # the WorkloadWatcher rejects such a spec, so this is defence-in-depth.
  defp group_catalog_entries(catalog_table) do
    WorkloadCatalog.all_names(catalog_table)
    |> Enum.sort()
    |> Enum.flat_map(fn name ->
      case WorkloadCatalog.fetch(catalog_table, name) do
        {:ok, %{class: "composite", group: %{entry: %{listen_port: lp} = entry}}} when is_integer(lp) ->
          [{name, entry}]

        _ ->
          []
      end
    end)
  end

  # The endpoint the composite workload's L4 cluster should carry: the single live
  # entry member if the group is running-and-entry-healthy, else the activator's
  # fallback endpoint AT THIS WORKLOAD'S OWN entry.listenPort (the activator resolves
  # the workload from the local accept port), else nil (cold and no activator_ip =>
  # skipped upstream).
  defp group_endpoint(ctx, workload, listen_port) do
    case GroupStore.entry_endpoint(ctx.group_store, workload) do
      %{ip: ip, port: port} when is_binary(ip) and ip != "" and is_integer(port) ->
        %{ip: ip, port: port}

      _ ->
        activator_tcp_endpoint(ctx, listen_port)
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
