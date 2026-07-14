defmodule Embervm.BaseBuilder do
  @moduledoc """
  Turns a `Workload`'s OCI image into a pristine base snapshot by driving the
  node daemon's `BuildBase` RPC, then reports the result back into the
  Workload's `status` subresource (`snapshotRef`, `snapshotDigest`, and the
  `Ready`/`BaseBuilt` conditions).

  This is the first control-plane component that DRIVES the node daemon (issues
  a mutating RPC and reconciles its result into CR status), where
  `Embervm.NodeRegistry` only CONSUMES node truth. Building a base is the
  on-ramp that makes a Workload eligible to ever run: a Workload never becomes
  `Ready` without a restorable base.

  ## trigger: the watcher hands us admissions and spec changes

  `Embervm.WorkloadWatcher` is already the informer that sees every Workload
  admission, spec change, and deletion. Rather than open a second watch, it
  calls `reconcile/2` (a cast) for every valid Workload it reconciles and
  `forget/2` for every invalid or deleted one. Those are `whereis`-guarded
  no-ops when this server is not running, so the watcher's own unit tests (which
  start no BaseBuilder) are unaffected, and a BaseBuilder crash cannot take the
  watcher down. Because the supervision tree starts this process BEFORE the
  watcher under `:rest_for_one`, a BaseBuilder restart also restarts the
  watcher, whose boot LIST re-casts every Workload into the fresh builder: the
  reconcile is idempotent, so re-driving is free and self-healing (no separate
  boot sweep needed).

  ## per-node serialization

  A base build is heavy (pull, bake, cold boot, health-gate, snapshot), so
  builds are serialized PER NODE: at most one build runs on a node at a time and
  concurrent admissions queue behind it. Serialization is structural, not a
  lock: each node has a FIFO `queue` and a single `building` slot, and the next
  queued build starts only when the current one finishes. The blocking
  `BuildBase` call runs in a `spawn_monitor`'d worker (the NodeRegistry streamer
  discipline) so it never freezes this GenServer, which owns the queues, the
  per-workload build state, and every status-write decision.

  In R0 there is exactly one node and every Workload's base is built on it (its
  single `status.snapshotRef` is that node's base handle). Multi-node base
  fan-out (a base per node a Workload may be primed on, with per-node snapshot
  refs) is the dispatcher's concern (Task 11); the per-node keying here means it
  needs more queue entries, not a reshape.

  ## change detection is on the spec signature, not the resolved digest

  A tag-pinned `image_ref` is resolved to a digest by the DAEMON at build time,
  so the control plane cannot compare digests before a build. Instead it detects
  change on a "base signature" derived from the spec fields that actually shape
  the base: `image_ref`, resources (`vcpus`, `mem_mib`), the guest contract
  (`guest_port`, `ready_path`), and `init_env`. A rebuild is triggered iff the
  signature differs from the one the recorded base was built from. Tag drift
  (same `image_ref` string, new upstream digest) does NOT rebuild: deploys are
  explicit CR updates, not tag drift. The digest the daemon resolves comes back
  in the response and is recorded in `status.snapshotDigest` for auditability.

  The daemon's own `BuildBase` idempotency (keyed on the resolved digest plus
  the `workload_revision` we send, the CR's `generation`) is the correctness
  backstop: re-driving a build we already made returns the existing snapshot
  with `already_built: true`, cheaply, so the signature map here is a
  latency optimization, not a source of truth we must persist across restarts.

  ## failure, backoff, and the Ready gate

  A failed build sets `BaseBuilt=False` with the daemon's error string in the
  condition message and retries with exponential backoff (capped at 10m). The
  `Ready` condition is derived purely from whether a restorable base exists
  (`snapshotRef` present), so:

    * first build in progress or failed -> no base -> `Ready=False`;
    * a rebuild (signature change) in progress or failed while an OLD base is
      still recorded -> `Ready=True` (the old base still serves; no dispatch
      gap), `BaseBuilt=False`.

  `status.snapshotRef` is only ever ADVANCED to a base the daemon has already
  confirmed built, never to an in-progress one, which is what makes the
  acceptance property hold: at any point, the `snapshotRef` in status names a
  restorable base.

  ## turnover on a digest change (a documented seam, not built here)

  When a rebuild produces a new snapshot, the old snapshot ref is recorded in
  the workload's `superseded_refs` for Task 11's PoolManager to reconcile:
  proactively destroy the old-base primed VMs and re-prime from the new base,
  destroying the old base file only after zero primed VMs reference it. There is
  no pool in R0, so this module only emits the new `snapshotRef` and records the
  old one; it does not destroy or re-prime anything.

  ## status write coordination with the watcher

  Status is patched with a JSON merge-patch, which REPLACES arrays wholesale, so
  two writers touching `conditions` would clobber each other. Ownership is split
  by key: this module owns `conditions` (`Ready` + `BaseBuilt`), `snapshotRef`,
  and `snapshotDigest` for valid task Workloads; the watcher owns
  `observedGeneration` and `primedFloorSatisfied` (disjoint keys, no lost
  update) and keeps `conditions` only for the invalid-CR validation lane, which
  never reaches this module.
  """

  use GenServer
  require Logger

  alias Embervm.Node.V1.{BuildBaseRequest, BuildBaseResponse, NodeService, ResourceSpec, Trace}

  # Backoff for a failed build: exponential from 1s, capped at the spec's 10m.
  @base_backoff_ms 1_000
  @max_backoff_ms 600_000

  # -- Client API ------------------------------------------------------------

  # :name defaults to __MODULE__ for the application's supervised singleton;
  # tests pass name: nil to run PID-addressed instances concurrently (the same
  # idiom as the other GenServers in this app).
  @spec start_link(keyword()) :: GenServer.on_start()
  def start_link(opts) do
    case Keyword.get(opts, :name, __MODULE__) do
      nil -> GenServer.start_link(__MODULE__, opts)
      name -> GenServer.start_link(__MODULE__, opts, name: name)
    end
  end

  @typedoc """
  The build descriptor the watcher hands us for a valid Workload: the spec
  fields that shape a base plus the identity needed to write status back.
  """
  @type desc :: %{
          required(:name) => String.t(),
          required(:namespace) => String.t(),
          required(:generation) => integer() | nil,
          required(:image_ref) => String.t(),
          required(:guest_port) => integer() | nil,
          required(:ready_path) => String.t(),
          required(:vcpus) => integer() | nil,
          required(:mem_mib) => integer() | nil,
          required(:init_env) => %{String.t() => String.t()}
        }

  @doc """
  Reconcile one Workload's desired base against what has been built. A cast so
  the watcher never blocks on a build. A `whereis`-guarded no-op when this
  server is not running (the watcher's own unit tests start no BaseBuilder, and
  the trigger fails open: the watcher's boot re-LIST re-drives us anyway).
  """
  @spec reconcile(GenServer.server(), desc()) :: :ok
  def reconcile(server \\ __MODULE__, desc) do
    cast_if_alive(server, {:reconcile, desc})
  end

  @doc """
  Drop a Workload the watcher no longer considers a valid build target (it was
  deleted, or an edit made it invalid). Cancels any pending retry and removes it
  from its node queue; a build already in flight is left to finish and its
  result discarded (the workload is gone from state). `whereis`-guarded like
  `reconcile/2`.
  """
  @spec forget(GenServer.server(), String.t()) :: :ok
  def forget(server \\ __MODULE__, name) do
    cast_if_alive(server, {:forget, name})
  end

  @doc """
  A snapshot of every tracked Workload's build state, for tests and operational
  visibility. Includes queue/building state per node so an operator can see WHY
  a base is not built yet.
  """
  @spec status(GenServer.server()) :: map()
  def status(server \\ __MODULE__) do
    GenServer.call(server, :status)
  end

  defp cast_if_alive(server, msg) do
    case GenServer.whereis(server) do
      nil -> :ok
      _pid -> GenServer.cast(server, msg)
    end
  end

  # -- GenServer callbacks ---------------------------------------------------

  @impl true
  def init(opts) do
    nodes = Keyword.get(opts, :nodes, [])
    # Injected seams (defaults are the real gRPC + K8s status patch + wall clock):
    build_fun = Keyword.get(opts, :build_fun, &default_build/2)
    connect_fun = Keyword.get(opts, :connect_fun, &default_connect/1)
    disconnect_fun = Keyword.get(opts, :disconnect_fun, &default_disconnect/1)
    status_writer = Keyword.get(opts, :status_writer, &Embervm.K8s.patch_workload_status/3)
    clock = Keyword.get(opts, :clock, fn -> System.system_time(:millisecond) end)
    base_backoff = Keyword.get(opts, :base_backoff_ms, @base_backoff_ms)
    max_backoff = Keyword.get(opts, :max_backoff_ms, @max_backoff_ms)

    node_ids = Enum.map(nodes, & &1.id)

    node_addr = for %{id: id, address: address} <- nodes, into: %{}, do: {id, address}

    node_runtime =
      for id <- node_ids, into: %{} do
        {id, %{building: nil, queue: [], worker: nil}}
      end

    state = %{
      node_ids: node_ids,
      node_addr: node_addr,
      nodes: node_runtime,
      workloads: %{},
      # pid -> %{node_id, name, signature} for the CURRENT build worker, so a
      # result from a superseded worker (or for a since-changed spec) is dropped.
      workers: %{},
      build_fun: build_fun,
      connect_fun: connect_fun,
      disconnect_fun: disconnect_fun,
      status_writer: status_writer,
      clock: clock,
      base_backoff_ms: base_backoff,
      max_backoff_ms: max_backoff
    }

    {:ok, state}
  end

  @impl true
  def handle_cast({:reconcile, desc}, state) do
    {:noreply, reconcile_desc(state, desc)}
  end

  def handle_cast({:forget, name}, state) do
    {:noreply, forget_workload(state, name)}
  end

  @impl true
  def handle_call(:status, _from, state) do
    workloads =
      for {name, w} <- state.workloads, into: %{} do
        {name,
         %{
           node_id: w.node_id,
           image_ref: w.image_ref,
           snapshot_ref: w.snapshot_ref,
           snapshot_digest: w.snapshot_digest,
           built: w.built_signature != nil and w.built_signature == signature(w),
           superseded_refs: w.superseded_refs,
           backoff_ms: w.backoff_ms
         }}
      end

    nodes =
      for {id, n} <- state.nodes, into: %{} do
        {id, %{building: n.building, queued: n.queue, connected: n.worker != nil}}
      end

    {:reply, %{workloads: workloads, nodes: nodes}, state}
  end

  # A build worker reported its result. Drop it if the pid is not our current
  # worker (defensive; builds are serial so this is rare), then apply.
  @impl true
  def handle_info({:build_result, pid, result}, state) do
    case Map.get(state.workers, pid) do
      nil -> {:noreply, state}
      meta -> {:noreply, finish_build(state, pid, meta, result)}
    end
  end

  # A worker died WITHOUT reporting a result (it always sends one in the normal
  # path, so a bare DOWN is an abnormal exit). Treat as a build error.
  def handle_info({:DOWN, _ref, :process, pid, reason}, state) do
    case Map.get(state.workers, pid) do
      nil -> {:noreply, state}
      meta -> {:noreply, finish_build(state, pid, meta, {:error, {:worker_down, reason}})}
    end
  end

  # A backoff retry timer fired for a failed build.
  def handle_info({:retry, name}, state) do
    {:noreply, retry_workload(state, name)}
  end

  def handle_info(_msg, state), do: {:noreply, state}

  # Best-effort: stop orphaned build workers from outliving the GenServer that
  # owns their gRPC connections. Not load-bearing (a monitored worker exits when
  # its owner dies anyway), just tidy.
  @impl true
  def terminate(_reason, state) do
    for {pid, _meta} <- state.workers, do: Process.exit(pid, :shutdown)
    :ok
  end

  # -- reconcile ---------------------------------------------------------------

  # Store/update a Workload's desired base and, if the desired signature differs
  # from what has been built (or is being built), enqueue a build. No node
  # configured yet means we cannot build: record the intent and report it.
  defp reconcile_desc(state, %{name: name} = desc) do
    prev = Map.get(state.workloads, name)
    node_id = placement(state, prev)

    w = merge_desc(prev, desc, node_id)
    state = put_in(state.workloads[name], w)

    cond do
      node_id == nil ->
        # No node wired (empty config, e.g. CI): hold the desc and report why.
        write_base_status(state, w, {:pending, :no_node})

      w.built_signature == signature(w) and w.snapshot_ref != nil ->
        # Desired base already built and recorded: idempotent no-op. (The watcher
        # separately writes observedGeneration for a generation-only change.)
        state

      already_targeting?(state, node_id, name, signature(w)) ->
        # A build for this exact signature is already queued or in flight.
        state

      true ->
        state
        |> cancel_pending_retry(name)
        |> enqueue(node_id, name)
        |> write_base_status(w, :building)
        |> maybe_start_build(node_id)
    end
  end

  # Cancel a workload's pending backoff-retry timer (if any) when a build is
  # about to be enqueued by another path, so the timer cannot later fire and
  # enqueue a redundant second build for the same target.
  defp cancel_pending_retry(state, name) do
    case Map.get(state.workloads, name) do
      %{retry_timer: ref} when ref != nil ->
        cancel_timer(ref)
        put_in(state.workloads[name].retry_timer, nil)

      _ ->
        state
    end
  end

  # Placement: in R0 every Workload's base is built on the single configured
  # node; a Workload keeps whichever node it was first placed on. Returns nil
  # when no node is configured. Multi-node placement is Task 11's job.
  defp placement(_state, %{node_id: node_id}) when is_binary(node_id), do: node_id
  defp placement(state, _prev), do: List.first(state.node_ids)

  # Fold a fresh desc into the workload's build state, preserving the built base
  # (built_signature/snapshot_ref/digest/superseded) across spec edits so a
  # rebuild-in-progress keeps serving the old base.
  defp merge_desc(nil, desc, node_id) do
    %{
      name: desc.name,
      namespace: desc.namespace,
      generation: desc.generation,
      node_id: node_id,
      image_ref: desc.image_ref,
      guest_port: desc.guest_port,
      ready_path: desc.ready_path,
      vcpus: desc.vcpus,
      mem_mib: desc.mem_mib,
      init_env: desc.init_env || %{},
      built_signature: nil,
      snapshot_ref: nil,
      snapshot_digest: nil,
      superseded_refs: [],
      backoff_ms: nil,
      retry_timer: nil
    }
  end

  defp merge_desc(prev, desc, node_id) do
    %{
      prev
      | namespace: desc.namespace,
        generation: desc.generation,
        node_id: node_id,
        image_ref: desc.image_ref,
        guest_port: desc.guest_port,
        ready_path: desc.ready_path,
        vcpus: desc.vcpus,
        mem_mib: desc.mem_mib,
        init_env: desc.init_env || %{}
    }
  end

  # The base signature: the spec fields that actually shape the base. A rebuild
  # is triggered iff this differs from the signature the recorded base was built
  # from. Deliberately excludes concurrency/invocation/retry (they do not change
  # the base VM), so a cap-only edit never rebuilds.
  defp signature(w) do
    {w.image_ref, w.vcpus, w.mem_mib, w.guest_port, w.ready_path, w.init_env}
  end

  # Is a build for this signature already queued or in flight on the node? Avoids
  # enqueuing a duplicate while an identical build is pending.
  defp already_targeting?(state, node_id, name, sig) do
    n = state.nodes[node_id]
    building_this = n.building == name and worker_signature(state, node_id) == sig
    queued = name in n.queue
    building_this or queued
  end

  defp worker_signature(state, node_id) do
    case state.nodes[node_id].worker do
      {pid, _ref} -> get_in(state.workers, [pid, :signature])
      nil -> nil
    end
  end

  defp enqueue(state, node_id, name) do
    update_in(state.nodes[node_id].queue, fn q -> if name in q, do: q, else: q ++ [name] end)
  end

  # -- forget ------------------------------------------------------------------

  defp forget_workload(state, name) do
    case Map.get(state.workloads, name) do
      nil ->
        state

      w ->
        cancel_timer(w.retry_timer)

        state
        |> update_in([:workloads], &Map.delete(&1, name))
        |> dequeue_everywhere(name)
    end
  end

  defp dequeue_everywhere(state, name) do
    update_in(state.nodes, fn nodes ->
      for {id, n} <- nodes, into: %{}, do: {id, %{n | queue: List.delete(n.queue, name)}}
    end)
  end

  # -- serial build execution --------------------------------------------------

  # If the node is idle and its queue is non-empty, pop the next Workload and
  # start its build. Skips (and drops) any queued name whose Workload was
  # forgotten while it waited.
  defp maybe_start_build(state, node_id) do
    n = state.nodes[node_id]

    cond do
      n.building != nil ->
        state

      n.queue == [] ->
        state

      true ->
        [name | rest] = n.queue
        state = put_in(state.nodes[node_id].queue, rest)

        case Map.get(state.workloads, name) do
          nil -> maybe_start_build(state, node_id)
          w -> start_worker(state, node_id, w)
        end
    end
  end

  # Spawn the monitored worker that owns the blocking BuildBase call for one
  # Workload. spawn_monitor (not link): a worker crash surfaces as a :DOWN we
  # treat as a build error, never escalating to this GenServer.
  defp start_worker(state, node_id, w) do
    owner = self()
    address = state.node_addr[node_id]
    build_fun = state.build_fun
    connect_fun = state.connect_fun
    disconnect_fun = state.disconnect_fun
    request = build_request(w)
    sig = signature(w)

    {pid, ref} =
      spawn_monitor(fn ->
        me = self()

        result =
          case connect_fun.(address) do
            {:ok, channel} ->
              try do
                build_fun.(channel, request)
              catch
                kind, reason -> {:error, {kind, reason}}
              after
                disconnect_fun.(channel)
              end

            {:error, reason} ->
              {:error, {:connect, reason}}
          end

        send(owner, {:build_result, me, result})
      end)

    state
    |> put_in([:nodes, node_id, :building], w.name)
    |> put_in([:nodes, node_id, :worker], {pid, ref})
    |> put_in([:workers, pid], %{node_id: node_id, name: w.name, signature: sig})
  end

  defp build_request(w) do
    %BuildBaseRequest{
      trace: %Trace{workload: w.name},
      image_ref: w.image_ref,
      workload_revision: to_string(w.generation || 0),
      guest_port: w.guest_port || 0,
      ready_path: w.ready_path,
      resources: %ResourceSpec{vcpus: w.vcpus || 0, mem_mib: w.mem_mib || 0},
      init_env: w.init_env
    }
  end

  # A build finished. Clear the node's building slot and worker index, apply the
  # result to the Workload's state and status (unless it was forgotten or its
  # spec changed under us), then start the next queued build on that node.
  defp finish_build(state, pid, %{node_id: node_id, name: name, signature: built_sig}, result) do
    state =
      state
      |> update_in([:workers], &Map.delete(&1, pid))
      |> put_in([:nodes, node_id, :building], nil)
      |> put_in([:nodes, node_id, :worker], nil)

    state =
      case Map.get(state.workloads, name) do
        nil ->
          # Forgotten while building: discard the result.
          state

        w ->
          if signature(w) == built_sig do
            apply_result(state, w, built_sig, result)
          else
            # Spec changed under us: this result is for a stale signature. Discard
            # it and re-enqueue the current desired build.
            state
            |> enqueue(node_id, name)
            |> write_base_status(w, :building)
          end
      end

    maybe_start_build(state, node_id)
  end

  # A successful build: record the new base, advance status.snapshotRef (the
  # first time the acceptance property's "always restorable" ref is set/moved),
  # push any superseded ref onto the turnover list (Task 11 seam), and reset
  # backoff.
  defp apply_result(state, w, built_sig, {:ok, %BuildBaseResponse{} = resp}) do
    superseded =
      if w.snapshot_ref && w.snapshot_ref != resp.snapshot_ref,
        do: [w.snapshot_ref | w.superseded_refs],
        else: w.superseded_refs

    w = %{
      w
      | built_signature: built_sig,
        snapshot_ref: resp.snapshot_ref,
        snapshot_digest: resp.image_digest,
        superseded_refs: superseded,
        backoff_ms: nil,
        retry_timer: nil
    }

    state = put_in(state.workloads[w.name], w)
    write_base_status(state, w, :built)
  end

  # A failed build: keep any existing base (Ready stays True if one is recorded),
  # report the daemon error, and schedule a backed-off retry.
  defp apply_result(state, w, _built_sig, {:error, reason}) do
    message = format_build_error(reason)

    Logger.warning("embervm base builder: BuildBase for #{w.namespace}/#{w.name} failed: #{message}")

    next_backoff = min((w.backoff_ms || state.base_backoff_ms) * backoff_factor(w), state.max_backoff_ms)
    timer = Process.send_after(self(), {:retry, w.name}, next_backoff)

    w = %{w | backoff_ms: next_backoff, retry_timer: timer}
    state = put_in(state.workloads[w.name], w)
    write_base_status(state, w, {:failed, message})
  end

  # Backoff doubles from the base on each consecutive failure. The first failure
  # (backoff_ms nil) starts at the base; thereafter it doubles the last value.
  defp backoff_factor(%{backoff_ms: nil}), do: 1
  defp backoff_factor(_), do: 2

  defp retry_workload(state, name) do
    case Map.get(state.workloads, name) do
      nil ->
        state

      w ->
        w = %{w | retry_timer: nil}
        state = put_in(state.workloads[name], w)

        cond do
          w.built_signature == signature(w) and w.snapshot_ref != nil ->
            # The desired base got built by some other path meanwhile; nothing to do.
            state

          already_targeting?(state, w.node_id, name, signature(w)) ->
            # A build for the current signature is already queued or in flight
            # (a reconcile beat this stale timer message to it, or the timer
            # double-fired). Re-driving would enqueue a redundant heavy build,
            # so no-op. Same guard reconcile_desc uses, mirroring the streamer
            # guard in Embervm.NodeRegistry's reconnect handler.
            state

          true ->
            state
            |> enqueue(w.node_id, name)
            |> write_base_status(w, :building)
            |> maybe_start_build(w.node_id)
        end
    end
  end

  # -- status writing ----------------------------------------------------------

  # Build and patch the two conditions this module owns. Ready is derived purely
  # from whether a restorable base exists (snapshot_ref present); BaseBuilt
  # tracks the DESIRED base. Only a :built result writes snapshotRef/Digest, so a
  # build-in-progress or failure never clears an existing (still restorable) ref.
  defp write_base_status(state, w, phase) do
    ready = ready_condition(state, w)
    base_built = base_built_condition(state, phase)

    status_map =
      %{"conditions" => [ready, base_built]}
      |> maybe_put_snapshot(w, phase)

    case state.status_writer.(w.namespace, w.name, status_map) do
      :ok ->
        :ok

      {:error, reason} ->
        # Visibility-only: a status-write failure must not crash the loop or lose
        # the build state (already recorded above); log and swallow.
        Logger.warning(
          "embervm base builder: status patch failed for #{w.namespace}/#{w.name}: #{inspect(reason)}"
        )
    end

    state
  end

  defp maybe_put_snapshot(status_map, w, :built) do
    status_map
    |> Map.put("snapshotRef", w.snapshot_ref || "")
    |> Map.put("snapshotDigest", w.snapshot_digest || "")
  end

  defp maybe_put_snapshot(status_map, _w, _phase), do: status_map

  defp ready_condition(state, w) do
    if w.snapshot_ref do
      condition(state, "Ready", "True", "BaseReady", "a restorable base snapshot is available")
    else
      condition(state, "Ready", "False", "BaseNotBuilt", "base snapshot not built yet")
    end
  end

  defp base_built_condition(state, :building) do
    condition(state, "BaseBuilt", "False", "BaseBuilding", "base build in progress")
  end

  defp base_built_condition(state, :built) do
    condition(state, "BaseBuilt", "True", "BaseBuilt", "base snapshot built and recorded")
  end

  defp base_built_condition(state, {:failed, message}) do
    condition(state, "BaseBuilt", "False", "BuildFailed", message)
  end

  defp base_built_condition(state, {:pending, :no_node}) do
    condition(state, "BaseBuilt", "Unknown", "NoNodeAvailable", "no node daemon is configured to build the base")
  end

  defp condition(state, type, status, reason, message) do
    %{
      "type" => type,
      "status" => status,
      "reason" => reason,
      "message" => message,
      "lastTransitionTime" => iso8601(state.clock.())
    }
  end

  defp iso8601(ms) do
    ms
    |> DateTime.from_unix!(:millisecond)
    |> DateTime.to_iso8601()
  end

  defp cancel_timer(nil), do: :ok
  defp cancel_timer(ref), do: Process.cancel_timer(ref)

  # -- default (production) seams ---------------------------------------------

  # Plaintext h2c to the noded Service over the Mint adapter, opened per build
  # (builds are infrequent and serialized). Same pattern as Embervm.NodeRegistry.
  defp default_connect(address) do
    GRPC.Stub.connect(address, adapter: GRPC.Client.Adapters.Mint)
  end

  defp default_disconnect(channel) do
    _ = GRPC.Stub.disconnect(channel)
    :ok
  end

  defp default_build(channel, %BuildBaseRequest{} = request) do
    NodeService.Stub.build_base(channel, request)
  end

  # A gRPC-level failure (e.g. FAILED_PRECONDITION for an image the daemon does
  # not know, which is every image in R0 until guest-image provisioning lands)
  # arrives as a GRPC.RPCError; surface its message verbatim in the condition.
  defp format_build_error(%GRPC.RPCError{message: message}), do: message
  defp format_build_error(reason), do: inspect(reason)
end
