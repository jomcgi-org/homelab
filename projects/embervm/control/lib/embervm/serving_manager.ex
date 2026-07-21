defmodule Embervm.ServingManager do
  @moduledoc """
  The activator: the serving-class miss brain (R3, Task 8). It is the fallback
  endpoint of an empty `serve|<workload>` cluster, so a request Envoy routes to it
  IS the miss signal for that workload (standing decision 1: the control plane is
  OFF the hit path except this miss path). On a miss it wakes the workload (relight
  a banked serving snapshot, or cold-boot a fresh instance), publishes the fresh
  endpoint, and resolves the parked caller to that endpoint so the router proxies
  the one miss request to it; every SUBSEQUENT request reaches the VM
  node-Envoy-direct with zero control-plane involvement.

  ## single-flight wake (exactly one StartServing per concurrent miss burst)

  N concurrent misses for one workload must produce exactly ONE StartServing and N
  proxied responses. This is the SessionManager relight-ledger shape: `waking`
  maps `workload -> [{from, req}]`. The FIRST miss for a workload registers its
  caller AND kicks one wake worker; every concurrent miss finds the workload
  already in `waking` and only appends its caller. When the wake completes, ALL
  parked callers for that workload are resolved to the fresh endpoint. So
  exactly-one-start / N-responses is structural, not timing-dependent.

  ## the parked caller and the reply contract

  A miss parks as `GenServer.call(manager, {:miss, workload, req, principal}, :infinity)`
  replied later. The `:infinity` is bounded in practice because EVERY terminal path
  replies: a rate-limit or parked-cap denial replies immediately (429/503, never
  parked); a wake success replies `{:ok, endpoint}` (the router proxies); a wake
  failure or readiness timeout replies `{:error, {:wake_failed, reason}}` (503). No
  path drops a `from`, so a caller never blocks forever.

  ## wake failure keeps the activator published

  On a wake failure (StartServing error / readiness timeout) the parked callers get
  503, the instance is marked `failed` (serving_failed appended), and the activator
  endpoint STAYS the workload's cluster fallback (the publisher renders the
  activator whenever there are no healthy published endpoints, which is still true
  after a failed wake), so the NEXT request retries the wake, subject to the
  per-principal wake-rate limit.

  ## restart adoption (the #3517 lesson, third application)

  On boot and every sweep, `reconcile/1` reconciles the ServingStore projection
  against every node's reported `serving_vms` + `serving_snapshots` (the same
  node-is-truth adoption the SessionManager runs): a node-reported live serving VM
  rebinds the instance's endpoint (and marks it published so it re-enters the
  fan-out), a node-reported snapshot with no live VM heals the instance to banked,
  and an instance the node reports as NEITHER a VM nor a snapshot (its node IS
  reporting) is marked failed. Orphan snapshots (no live instance) are evicted.
  Then the EndpointPublisher re-derives + re-pushes. A control-plane restart with
  live serving VMs therefore republishes EXACTLY the same endpoints without
  touching any VM. NEVER reaps on a mere node disconnect (missing facts).

  ## scale-up (v1: miss-driven + minInstances only)

  A miss while live instances exist but are ALL draining, or a workload configured
  with `minInstances > 0` below its floor, may start an additional instance up to
  `maxInstances`. There is NO load-based autoscaling between 1 and max in v1; only
  miss-driven starts and (Task 9) idle-driven banks change the instance count.
  """

  use GenServer
  require Logger

  # Tracer.with_span/set_attributes are OpenTelemetry.Tracer MACROS, so the module
  # must be required even though it is called fully-qualified via the alias.
  require OpenTelemetry.Tracer, as: Tracer

  alias Embervm.{EndpointPublisher, NodeCapacity, ServingPlacement, ServingState, ServingStore, SessionTrace, WorkloadCatalog}

  alias Embervm.Node.V1.{
    ArtifactRef,
    FreshSource,
    RelightSource,
    RestoreArtifactRequest,
    StartServingRequest,
    StartServingResponse,
    Trace
  }

  # Wake-rate limit: miss-triggered wakes per WORKLOAD per window. A cold start or a
  # relight is asymmetric-cost (a full StartServing), so a burst of misses is a DoS
  # lever; excess wakes get 429 WITHOUT touching the node.
  #
  # KEYED BY WORKLOAD, NOT PRINCIPAL (and the parked-request cap below likewise):
  # this is a deliberate, plan-conformant choice, not a deviation. The R3 plan says
  # "capped per principal by the existing park caps", written for the AUTHENTICATED
  # task/session model where every request carries an ember ServiceAccount
  # principal. Serving hit-path traffic is ANONYMOUS end-user traffic (link
  # unfurlers, browsers) with NO bearer principal by construction (standing decision
  # 1: a hit never touches the control plane, so there is nothing to authenticate a
  # miss caller as). The workload is therefore the only stable identity available AND
  # the correct unit of abuse control: bounding StartServing thrash PER WORKLOAD is
  # exactly the ADR-001 asymmetric-cost guard (one workload's miss storm cannot
  # exhaust the node's live-VM budget). The router passes `serving:<workload>` as the
  # `principal` arg so the audit ops attribute to the workload. A future PR-4 closure
  # reviewer should read per-workload keying as the anonymous-traffic adaptation of
  # the plan's per-principal intent, not a gap.
  @default_wake_max 30
  @default_wake_window_ms 60_000

  # Parked-request cap per workload: how many callers may park behind one wake
  # before excess misses get 503 (the wake is slow / stuck; do not accumulate
  # unbounded callers on one workload).
  @default_park_cap 64

  # -- Client API ------------------------------------------------------------

  @spec start_link(keyword()) :: GenServer.on_start()
  def start_link(opts) do
    case Keyword.get(opts, :name, __MODULE__) do
      nil -> GenServer.start_link(__MODULE__, opts)
      name -> GenServer.start_link(__MODULE__, opts, name: name)
    end
  end

  @doc """
  Handles one activator miss for `workload` on behalf of `principal`. `req` is the
  proxy envelope (`%{method, path, headers, body}`). Blocks (`:infinity`) until the
  workload is woken and returns `{:ok, %{ip, port}}` for the router to proxy the
  request to, OR a denial the router maps to a status:

    * `{:ok, %{ip, port}}`           -> proxy (a fresh wake, OR the STRAGGLER path: a
      request reached the activator while a healthy instance already exists; it is
      resolved to that live endpoint and proxied, never errored).
    * `{:error, {:wake_rate, ...}}`  -> 429 (per-workload wake-rate limit)
    * `{:error, {:park_full, ...}}`  -> 503 (parked-request cap for the workload)
    * `{:error, {:wake_failed, r}}`  -> 503 (start error / readiness timeout)
    * `{:error, {:unknown_workload}}`-> 404
  """
  @spec miss(GenServer.server(), String.t(), map(), String.t()) ::
          {:ok, map()} | {:error, term()}
  def miss(server \\ __MODULE__, workload, req, principal) do
    GenServer.call(server, {:miss, workload, req, principal}, :infinity)
  end

  @doc """
  Runs one adoption reconcile synchronously (the boot continue + the periodic
  sweep run the same code) and returns after it completes. Reconciles the
  ServingStore projection against every node's reported serving inventory, then
  re-derives + re-pushes the snapshot. Tests drive adoption deterministically
  through this.
  """
  @spec reconcile(GenServer.server()) :: :ok
  def reconcile(server \\ __MODULE__) do
    GenServer.call(server, :reconcile, :infinity)
  end

  # -- GenServer callbacks ---------------------------------------------------

  @impl true
  def init(opts) do
    state = %{
      store: Keyword.get(opts, :store, ServingStore),
      publisher: Keyword.get(opts, :publisher, EndpointPublisher),
      capacity_table: Keyword.get(opts, :capacity_table, NodeCapacity.table()),
      catalog_table: Keyword.get(opts, :catalog_table, WorkloadCatalog.table()),
      clock: Keyword.get(opts, :clock, &default_clock/0),
      id_fun: Keyword.get(opts, :id_fun, nil),
      tenant: Keyword.get(opts, :tenant, "homelab"),
      # Daemon serving-verb seams (injected for tests; production dials the real
      # NodeService stub over the shared NodeChannel).
      channel_fun: Keyword.get(opts, :channel_fun, &Embervm.NodeChannel.get/1),
      invalidate_fun: Keyword.get(opts, :invalidate_fun, &Embervm.NodeChannel.invalidate/2),
      start_serving_fun: Keyword.get(opts, :start_serving_fun, &default_start_serving/2),
      # Restore-on-miss seam (R6, Task 8): (channel, %RestoreArtifactRequest{}) ->
      # {:ok, %RestoreArtifactResponse{}} | {:error, _}. Fetches a banked SERVING
      # bundle back onto local disk from the object store before a relight on a TRUE
      # local miss (the bundle is exported but no longer on the anchor node's disk).
      # Injected for tests; production dials the real NodeService stub.
      restore_artifact_fun: Keyword.get(opts, :restore_artifact_fun, &default_restore_artifact/2),
      # The op-log the restore audit record (:artifact_restored) is appended to.
      # Injected for tests; production uses the SQLite backend.
      op_log: Keyword.get(opts, :op_log, Embervm.OpLog.SQLite),
      # The backend module dispatched below, threaded alongside :op_log (the
      # server address) so a non-default backend never requires editing this
      # module. Defaults to the same SQLite module :op_log defaults to.
      op_log_mod: Keyword.get(opts, :op_log_mod, Embervm.OpLog.SQLite),
      # workload -> [{from, req, principal}] parked behind an in-flight wake, so
      # concurrent misses share ONE StartServing (single-flight). The first miss
      # kicks the worker; the rest append here.
      waking: %{},
      # workload -> per-miss tracing bundle (Task 10): the router's activate ROOT
      # traceparent plus the phase boundary timestamps (:opentelemetry.timestamp/0)
      # recorded on the serialized manager as the miss advances park -> placement ->
      # wake. finish_wake restores the root parent and emits the park/placement/wake/
      # publish CHILD spans retroactively with explicit start_times (the session
      # queue_wait idiom), so the whole miss is one connected trace even though the
      # wake RPC ran async in a spawned worker. Seeded by the first miss, cleared by
      # finish_wake. Absent (tracing off in CI) => spans are a clean no-op.
      wake_traces: %{},
      # principal -> [wake timestamps within the window]. Sliding-window counter.
      wake_events: %{},
      wake_max: Keyword.get(opts, :wake_max, @default_wake_max),
      wake_window_ms: Keyword.get(opts, :wake_window_ms, @default_wake_window_ms),
      park_cap: Keyword.get(opts, :park_cap, @default_park_cap),
      # The adoption reconcile cadence (0 = off, tests drive reconcile/1).
      reconcile_interval_ms: Keyword.get(opts, :reconcile_interval_ms, 0)
    }

    if state.reconcile_interval_ms > 0 do
      {:ok, state, {:continue, :boot}}
    else
      {:ok, state}
    end
  end

  # Boot: one adoption reconcile against whatever the node registry has already
  # populated, then arm the periodic reconcile timer.
  @impl true
  def handle_continue(:boot, state) do
    state = do_reconcile(state)
    schedule(:reconcile, state.reconcile_interval_ms)
    {:noreply, state}
  end

  @impl true
  def handle_call({:miss, workload, req, principal}, from, state) do
    handle_miss(state, workload, req, principal, from)
  end

  def handle_call(:reconcile, _from, state) do
    {:reply, :ok, do_reconcile(state)}
  end

  # The async wake worker finished: complete the durable transition + publish, then
  # resolve every parked caller for the workload.
  @impl true
  def handle_info({:wake_done, workload, outcome}, state) do
    {:noreply, finish_wake(state, workload, outcome)}
  end

  def handle_info(:reconcile, state) do
    state = do_reconcile(state)
    schedule(:reconcile, state.reconcile_interval_ms)
    {:noreply, state}
  end

  def handle_info(_msg, state), do: {:noreply, state}

  # -- miss handling ---------------------------------------------------------

  defp handle_miss(state, workload, req, principal, from) do
    # A straggler: a request reached the activator while a healthy published
    # instance already exists for the workload (a race with a just-published wake,
    # or an Envoy config lag). Proxy it to the live endpoint, do NOT wake or error.
    case first_published_endpoint(state, workload) do
      {:ok, endpoint} ->
        {:reply, {:ok, endpoint}, state}

      :none ->
        handle_cold_miss(state, workload, req, principal, from)
    end
  end

  defp handle_cold_miss(state, workload, req, principal, from) do
    cond do
      not serving_workload?(state, workload) ->
        {:reply, {:error, {:unknown_workload}}, state}

      # Already a wake in flight for this workload: park behind it (single-flight),
      # subject to the parked-request cap. Does NOT consult the wake-rate limit
      # (the wake was already counted by the first miss).
      Map.has_key?(state.waking, workload) ->
        park_behind_wake(state, workload, req, principal, from)

      # First miss: apply the per-principal wake-rate limit, then park + kick ONE
      # wake worker.
      wake_allowed?(state, principal) ->
        state = record_wake(state, principal)
        state = park_new_wake(state, workload, req, principal, from)
        {:noreply, start_wake(state, workload)}

      true ->
        audit_denial(state, principal, workload, :wake_rate)
        {:reply, {:error, {:wake_rate, "per-principal wake-rate limit exceeded"}}, state}
    end
  end

  # Append a caller to an in-flight wake's parked list, enforcing the per-workload
  # parked-request cap (excess -> 503 without parking).
  defp park_behind_wake(state, workload, req, principal, from) do
    waiters = Map.get(state.waking, workload, [])

    if length(waiters) >= state.park_cap do
      audit_denial(state, principal, workload, :park_full)
      {:reply, {:error, {:park_full, "parked-request cap exceeded for workload"}}, state}
    else
      state = %{state | waking: Map.put(state.waking, workload, waiters ++ [{from, req, principal}])}
      {:noreply, state}
    end
  end

  # First miss for a workload: seed its parked list (the cap is never exceeded by
  # the first entry) AND its tracing bundle (the root traceparent this miss carried
  # from the router + the park_start, so finish_wake can emit the connected child
  # spans). park_start is stamped now: the parked callers wait from here until the
  # wake resolves.
  defp park_new_wake(state, workload, req, principal, from) do
    trace = %{
      traceparent: Map.get(req, :traceparent),
      park_start: :opentelemetry.timestamp()
    }

    %{
      state
      | waking: Map.put(state.waking, workload, [{from, req, principal}]),
        wake_traces: Map.put(state.wake_traces, workload, trace)
    }
  end

  # -- wake worker -----------------------------------------------------------

  # Kick ONE async wake for a workload. The relight-vs-cold DECISION and, for a
  # relight, the ETS `banked -> relighting` mark happen HERE on the serialized
  # manager process (a cheap pure placement read + one ETS mark), so the FSM edge
  # is taken in order and crash-consistently (the durable serving_relit lands only
  # AFTER the daemon returns, in finish_wake, exactly the session relight rule). The
  # StartServing RPC itself runs in a spawned worker so a multi-second boot never
  # head-of-line-blocks another workload's miss; the worker reports the RPC result
  # and finish_wake completes the durable transition + publish on this process.
  defp start_wake(state, workload) do
    owner = self()
    entry = catalog_entry(state, workload)

    # Placement phase: the pure plan_wake read. Stamp its boundaries + the cold bool
    # into the tracing bundle so finish_wake can emit the `placement` and `wake`
    # child spans with real durations. placement_end == wake_start (the RPC begins
    # the instant placement resolves).
    placement_start = :opentelemetry.timestamp()
    plan = plan_wake(state, workload)
    wake_start = :opentelemetry.timestamp()
    cold = match?({:cold, _, _}, plan)

    state =
      stamp_wake_trace(state, workload, %{
        placement_start: placement_start,
        placement_end: wake_start,
        wake_start: wake_start,
        cold: cold
      })

    case plan do
      {:relight, instance, node_id} ->
        # Instance selection (Step 4): a relight MUST land on the instance that
        # BANKED this serving snapshot on disk (per-instance-on-disk, PR-2.5), so
        # dial that instance's instance_id rather than the collapsing node-name
        # alias. No owning instance -> a mem-eligible one; none at all -> fail clean.
        case dial_instance(state, workload, entry, node_id, warmth_ref: instance.snapshot_ref) do
          {:ok, dial_id} ->
            # Move the banked instance to relighting (ETS-only, no op) before the RPC,
            # so a concurrent reconcile does not touch it and the later serving_relit
            # is a legal relighting -> starting edge.
            case ServingStore.mark(state.store, instance.instance_id, :relight) do
              {:ok, _} ->
                req = relight_request(entry, instance)
                spawn_wake(owner, workload, fn -> run_relight(state, instance, node_id, dial_id, req) end)

              {:error, reason} ->
                # The instance moved off banked concurrently: report a wake failure so
                # the parked callers 503 and the next miss retries.
                send(self(), {:wake_done, workload, {:error, {:relight_mark, reason}}})
            end

          {:error, reason} ->
            send(self(), {:wake_done, workload, {:error, reason}})
        end

      # Restore-on-miss (R6): the local snapshot is gone but its store copy is
      # recoverable. Restore the SERVING bundle inside the wake worker FIRST (so
      # park/single-flight semantics are unchanged), then relight exactly as the warm
      # path. The restore failing (store unreachable mid-wake, or the copy vanished)
      # degrades to the daemon's own cold-boot fallback (fail-open warmth).
      {:restore_then_relight, instance, node_id} ->
        # The local snapshot is gone, so no instance reports it: selection falls to a
        # mem-eligible instance the restore lands the bundle onto.
        case dial_instance(state, workload, entry, node_id, warmth_ref: instance.snapshot_ref) do
          {:ok, dial_id} ->
            case ServingStore.mark(state.store, instance.instance_id, :relight) do
              {:ok, _} ->
                req = relight_request(entry, instance)

                spawn_wake(owner, workload, fn ->
                  # Restore onto the SAME instance the boot dials (dial_id), not the
                  # node-name alias: serving snapshots are per-instance ON DISK
                  # (PR-2.5), so restoring onto an arbitrary co-located instance while
                  # the boot runs on another leaves the boot's local disk empty.
                  _ = restore_bundle(state, dial_id, node_id, workload, instance.snapshot_ref)
                  run_relight(state, instance, node_id, dial_id, req)
                end)

              {:error, reason} ->
                send(self(), {:wake_done, workload, {:error, {:relight_mark, reason}}})
            end

          {:error, reason} ->
            send(self(), {:wake_done, workload, {:error, reason}})
        end

      {:cold, node_id, base_ref} ->
        # A cold boot has no owning snapshot: select a mem-eligible instance on the
        # node (a too-small classed brick is skipped, the DS wildcard is always
        # eligible), failing cleanly if none has room.
        case dial_instance(state, workload, entry, node_id, []) do
          {:ok, dial_id} ->
            req = cold_request(entry, workload, base_ref)
            spawn_wake(owner, workload, fn -> run_cold(state, workload, node_id, dial_id, base_ref, req) end)

          {:error, select_reason} ->
            send(self(), {:wake_done, workload, {:error, select_reason}})
        end

      {:error, reason} ->
        # No capacity / snapshot lost: report it so the parked callers 503.
        send(self(), {:wake_done, workload, {:error, reason}})
    end

    state
  end

  # Select the SPECIFIC instance on the resolved node to dial (Step 4): prefer the
  # instance that banked this workload's serving snapshot (its serving_snapshots
  # report `warmth_ref`), else a mem-eligible instance sized for the workload's
  # mem_mib, else `{:error, :no_eligible_instance}`. Returns the dial key
  # (instance_id, or the node name for a legacy fact without one). A cold boot passes
  # no `:warmth_ref` and goes straight to the mem-eligible pick.
  defp dial_instance(state, workload, entry, node_id, opts) do
    Embervm.WakeInstance.select(
      node_id,
      [
        table: state.capacity_table,
        workload: workload,
        need_mib: Map.get(entry, :mem_mib) || 512,
        warmth_key: :serving_snapshots
      ] ++ opts
    )
  end

  # Spawn a wake worker that ALWAYS reports a {:wake_done} outcome, even if the RPC
  # body crashes: a worker that died without reporting would leave the parked
  # `:infinity` callers blocked forever (they only unblock when finish_wake replies).
  # The RPC seams are already `safe_*`-wrapped; this is the belt-and-suspenders that
  # guarantees the reply contract holds no matter what.
  defp spawn_wake(owner, workload, fun) do
    spawn(fn ->
      outcome =
        try do
          fun.()
        rescue
          e -> {:error, {:wake_crashed, e}}
        catch
          kind, reason -> {:error, {:wake_crashed, {kind, reason}}}
        end

      send(owner, {:wake_done, workload, outcome})
    end)
  end

  # Decide relight-vs-cold PURELY (ETS reads only): the freshest banked instance
  # whose serving snapshot a node still reports relights; otherwise a cold create on
  # a serving-capable node with the workload's base + budget. Returns
  # `{:relight, instance, node_id}` | `{:restore_then_relight, instance, node_id}` |
  # `{:cold, node_id, base_ref}` | `{:error, r}`.
  #
  # Restore-on-miss (R6, Task 8): a relight candidate's snapshot may be EXPORTED to
  # the object store but no longer on the anchor node's local disk (disk lost, or the
  # node rotated). When that happens AND the node's store is reachable, restore the
  # bundle first, then relight (fail-open warmth otherwise: an unreachable store or a
  # missing store copy falls through to the plain relight attempt, which the daemon
  # degrades to a cold boot if the local bundle is truly gone). Serving carries no
  # volume/generation pairing (unlike stateful), so there is only this one bundle-
  # restore case, never a restore_volume branch.
  defp plan_wake(state, workload) do
    case pick_bankable_instance(state, workload) do
      {:ok, instance, node_id} ->
        if bundle_local?(state, node_id, instance.snapshot_ref) or
             not store_reachable?(state, node_id) do
          {:relight, instance, node_id}
        else
          {:restore_then_relight, instance, node_id}
        end

      :none ->
        case ServingPlacement.node_for_create(workload, state.capacity_table) do
          {:ok, node_id, base_ref} -> {:cold, node_id, base_ref}
          {:error, :no_capacity} -> {:error, :no_capacity}
        end
    end
  end

  # -- restore-on-miss (R6, Task 8) -------------------------------------------

  # Whether the node still reports the instance's snapshot on LOCAL disk (its
  # snapshot_ref is in the node's serving_snapshots). A relight needs no restore when
  # true; a true local miss (false) may consult the store instead.
  defp bundle_local?(state, node_id, snapshot_ref) when is_binary(snapshot_ref) and snapshot_ref != "" do
    case NodeCapacity.fetch(state.capacity_table, node_id) do
      {:ok, fact} ->
        fact
        |> Map.get(:serving_snapshots, [])
        |> Enum.any?(&(&1.snapshot_ref == snapshot_ref))

      :error ->
        false
    end
  end

  defp bundle_local?(_state, _node_id, _ref), do: false

  # The node's latest object-store reachability verdict (R6). Absent/false (a node
  # with no store configured, or one that never reported) reads as NOT reachable, so
  # no restore is attempted and the wake degrades to the plain relight attempt (never
  # blocked: this only gates the store consultation, never a local-state wake).
  defp store_reachable?(state, node_id) do
    case NodeCapacity.fetch(state.capacity_table, node_id) do
      {:ok, fact} -> Map.get(fact, :store_reachable, false) == true
      :error -> false
    end
  end

  # A banked instance for the workload whose snapshot a node still reports AND whose
  # base lineage is still current: the freshest wins. Returns {:ok, instance, node_id}
  # or :none. The lineage check (D-R3.11.3 follow-up) rejects a snapshot born from a
  # SUPERSEDED base: after a runtime roll the node reports a new serving_image_ref, and
  # relighting an old-lineage snapshot would resume the old rootfs/handler (old code).
  # A skipped stale instance is swept/evicted separately; here we just refuse to relight
  # it, so the wake falls through to a cold boot on the current base.
  defp pick_bankable_instance(state, workload) do
    ServingStore.list(state.store, workload)
    |> Enum.filter(&(&1.state == :banked))
    |> Enum.sort_by(& &1.updated_at, :desc)
    |> Enum.find_value(:none, fn instance ->
      case ServingPlacement.node_for_relight(instance, state.capacity_table) do
        {:ok, node_id} ->
          if lineage_current?(state.capacity_table, node_id, instance) do
            {:ok, instance, node_id}
          else
            false
          end

        {:error, :snapshot_lost} ->
          # The snapshot is no longer on local disk (disk lost, node rotated). It may
          # still be recoverable from the object store (R6 restore-on-miss): if the
          # instance's anchor node is reporting a reachable store, keep it as a relight
          # candidate anchored there. plan_wake then routes it to :restore_then_relight
          # (bundle_local? is false, so it restores first). An unreachable store or a
          # gone node stays :none, so the wake cold-boots (fail-open warmth).
          restore_candidate_node(state, instance)
      end
    end)
  end

  # The instance's anchor node when it is reporting a reachable store (a
  # restore-on-miss candidate), else false (no restore possible, cold-boot).
  defp restore_candidate_node(state, %{node_id: node_id} = instance) when is_binary(node_id) and node_id != "" do
    if store_reachable?(state, node_id) and lineage_current?(state.capacity_table, node_id, instance) do
      {:ok, instance, node_id}
    else
      false
    end
  end

  defp restore_candidate_node(_state, _instance), do: false

  # Whether a banked instance's base lineage still matches the node's CURRENT
  # serving_image_ref for the workload. Fail-OPEN when the node reports no current ref
  # yet (nil/empty): the new base is not built, so the existing snapshot stays
  # relightable for warmth rather than forcing a cold boot with no base to boot from.
  defp lineage_current?(capacity_table, node_id, instance) do
    case ServingPlacement.current_serving_image_ref(capacity_table, node_id, instance.workload) do
      ref when is_binary(ref) and ref != "" -> ref == instance.base_snapshot_ref
      _ -> true
    end
  end

  defp relight_request(entry, instance) do
    %StartServingRequest{
      trace: %Trace{workload: instance.workload},
      source: {:relight, %RelightSource{snapshot_ref: instance.snapshot_ref}},
      # The GUEST port (spec.serving.port), which noded probes over the tap and the
      # guest listens on, NOT instance.port. Since the DNAT projection (D-R3.11.4)
      # StartServingResponse.port is the PUBLISHED endpoint port (podIP:vmPort, e.g.
      # 30002), which the store keeps as instance.port for routing; reusing it as the
      # guest port made relight probe tapIP:30002 -> connection refused (the guest is
      # on 8080). Cold boots always used serving_cfg.port and were unaffected.
      port: serving_cfg(entry).port,
      health_path: serving_cfg(entry).health_path
    }
  end

  defp cold_request(entry, workload, base_ref) do
    %StartServingRequest{
      trace: %Trace{workload: workload},
      # base_ref is the node's serving_image_ref (the cold-boot handler artifact),
      # NOT the base snapshot key: a fresh serving start cold-boots the handler
      # artifact, it does not resume the base memory snapshot (D-R3.4.2/D-R3.11.2).
      source: {:fresh, %FreshSource{serving_image_ref: base_ref || ""}},
      port: serving_cfg(entry).port,
      health_path: serving_cfg(entry).health_path
    }
  end

  # The relight RPC (in the spawned worker). Returns the outcome finish_wake
  # projects durably.
  defp run_relight(state, instance, node_id, dial_id, req) do
    case safe_start_serving(state, dial_id, req) do
      {:ok, %StartServingResponse{vm_id: vm_id, ip: ip, port: port}}
      when is_binary(vm_id) and vm_id != "" ->
        {:relit, instance.instance_id, node_id, %{vm_id: vm_id, ip: ip, port: port}}

      other ->
        {:error, {:relight_failed, instance.instance_id, other}}
    end
  end

  # The cold-create RPC (in the spawned worker).
  defp run_cold(state, workload, node_id, dial_id, base_ref, req) do
    case safe_start_serving(state, dial_id, req) do
      {:ok, %StartServingResponse{vm_id: vm_id, ip: ip, port: port}}
      when is_binary(vm_id) and vm_id != "" ->
        attrs = %{
          tenant: state.tenant,
          principal: wake_principal(state, workload),
          workload: workload,
          node_id: node_id,
          base_snapshot_ref: base_ref
        }

        {:created, attrs, %{vm_id: vm_id, ip: ip, port: port}}

      other ->
        {:error, {:start_failed, other}}
    end
  end

  # -- finish wake (serialized, durable) -------------------------------------

  # The wake completed (a {:wake_done} on the manager). On success: durably record
  # the instance (serving_started for a cold create, serving_relit for a relight)
  # then serving_published, publish the fan-out, and resolve every parked caller to
  # the fresh endpoint. On failure: mark the instance failed (if one exists),
  # 503 the parked callers, and leave the activator published so the next miss
  # retries.
  defp finish_wake(state, workload, outcome) do
    {waiters, state} = pop_waiters(state, workload)
    {trace, state} = pop_wake_trace(state, workload)

    # The wake RPC returned now: this closes the `wake` phase. Emit the retroactive
    # `park`/`placement`/`wake` child spans (Task 10) under the restored root: they
    # spanned the async wake worker and cannot wrap live code on this serialized
    # process, so they are emitted with explicit start_times (the session queue_wait
    # idiom). `wake` carries ember.wake_ms + ember.cold, the numbers the Task 12
    # wake-p95 + cold-vs-warm gate reads. The `publish` child span is opened LIVE
    # inside publish_and_resolve (it wraps real code) under the same root. Tracing
    # off (no traceparent) is a clean no-op.
    wake_ended = :opentelemetry.timestamp()
    emit_wake_phase_spans(trace, workload, wake_ended)

    case outcome do
      {:created, attrs, endpoint} ->
        finish_created(state, workload, attrs, endpoint, waiters, trace)

      {:relit, instance_id, node_id, endpoint} ->
        finish_relit(state, workload, instance_id, node_id, endpoint, waiters, trace)

      other ->
        finish_wake_failure(state, workload, other, waiters)
    end
  end

  # The wake-failure branches (no instance to publish, so no `publish` span): 503 the
  # parked callers and leave the activator published so the next miss retries. The
  # park/placement/wake child spans were already emitted by finish_wake (the wake
  # phase is timed whether or not it succeeded).
  defp finish_wake_failure(state, workload, outcome, waiters) do
    case outcome do
      # A relight RPC failure: the instance was marked `relighting` before the RPC.
      # The snapshot is intact (the proto never deletes it on a failed restore), so
      # return it `relighting -> banked` (ETS-only, no op) and 503 the callers; a
      # later miss re-relights. If the abort is itself illegal (a concurrent
      # destroy), leave it: the terminal state is authoritative.
      {:error, {:relight_failed, instance_id, reason}} ->
        Logger.warning("embervm serving relight failed", workload: workload, instance_id: instance_id, reason: inspect(reason))
        _ = ServingStore.mark(state.store, instance_id, :relight_abort)
        reply_all(waiters, {:error, {:wake_failed, reason}})
        state

      {:error, reason} ->
        Logger.warning("embervm serving wake failed", workload: workload, reason: inspect(reason))
        reply_all(waiters, {:error, {:wake_failed, reason}})
        # A cold-create start error recorded no instance (nothing to fail); the
        # publisher still renders the activator fallback (no healthy endpoint), so the
        # next miss retries.
        state
    end
  end

  defp finish_created(state, workload, attrs, endpoint, waiters, trace) do
    instance_id = mint_id(state)
    attrs = Map.put(attrs, :instance_id, instance_id)
    attrs = Map.merge(attrs, %{vm_id: endpoint.vm_id, ip: endpoint.ip, port: endpoint.port})

    case ServingStore.start(state.store, attrs) do
      {:ok, _instance} ->
        publish_and_resolve(state, instance_id, workload, endpoint, :started, waiters, trace)

      {:error, reason} ->
        Logger.error("embervm serving: start record failed", workload: workload, reason: inspect(reason))
        reply_all(waiters, {:error, {:wake_failed, {:store, reason}}})
        state
    end
  end

  defp finish_relit(state, workload, instance_id, node_id, endpoint, waiters, trace) do
    # A banked instance relit: serving_relit moves it back to starting with the
    # fresh vm/endpoint, then serving_published publishes it.
    relit =
      ServingStore.transition(
        state.store,
        instance_id,
        :relight_ready,
        :serving_relit,
        %{node_id: node_id, vm_id: endpoint.vm_id},
        %{node_id: node_id, vm_id: endpoint.vm_id, ip: endpoint.ip, port: endpoint.port}
      )

    case relit do
      {:ok, _} ->
        publish_and_resolve(state, instance_id, workload, endpoint, :relit, waiters, trace)

      {:error, reason} ->
        Logger.warning("embervm serving: relit transition failed",
          instance_id: instance_id,
          reason: inspect(reason)
        )

        reply_all(waiters, {:error, {:wake_failed, {:relit, reason}}})
        state
    end
  end

  # Publish the instance's endpoint (serving_published), ask the EndpointPublisher
  # to re-derive + re-push (the activator endpoint leaves the cluster in the same
  # update that adds the real endpoint), then resolve every parked caller to the
  # endpoint so the router proxies. The publish is the single point where the
  # workload leaves the activator fallback.
  defp publish_and_resolve(state, instance_id, workload, endpoint, reason, waiters, trace) do
    # The `publish` child span (Task 10): the control-plane publish step, wrapped
    # LIVE (it is real code on this process, unlike the retroactive wake phases).
    # ember.publish_ms is the span duration; the sidecar PUT ACK round-trip is
    # instrumented SEPARATELY on the EndpointPublisher flush (it is debounced/
    # coalesced and cannot be folded in here). ember.endpoint_count is the workload's
    # healthy-published set AFTER this publish.
    principal = wake_principal(state, workload)
    SessionTrace.restore_parent(trace_parent(trace))
    publish_start = :opentelemetry.timestamp()

    Tracer.with_span "embervm.serving.publish",
                     %{
                       attributes: %{
                         "ember.workload" => workload,
                         "ember.instance_id" => instance_id,
                         "ember.principal" => principal
                       }
                     } do
      result =
        case ServingStore.publish(state.store, instance_id, endpoint.ip, endpoint.port, reason) do
          {:ok, _} ->
            EndpointPublisher.publish(state.publisher)

            Logger.info("embervm serving woken",
              workload: workload,
              instance_id: instance_id,
              reason: reason
            )

            reply_all(waiters, {:ok, %{ip: endpoint.ip, port: endpoint.port}})
            state

          {:error, reason} ->
            Logger.error("embervm serving: publish failed", instance_id: instance_id, reason: inspect(reason))
            reply_all(waiters, {:error, {:wake_failed, {:publish, reason}}})
            state
        end

      publish_ms = span_ms(publish_start, :opentelemetry.timestamp())
      endpoint_count = length(ServingStore.published_endpoints(result.store, workload))

      Tracer.set_attributes(%{
        "ember.publish_ms" => publish_ms,
        "ember.endpoint_count" => endpoint_count
      })

      result
    end
    |> tap(fn _ ->
      # Reset the restored remote parent off the manager process dict (see
      # emit_wake_phase_spans): this runs on the long-lived manager, not a spawn.
      clear_current_span()
    end)
  end

  defp pop_waiters(state, workload) do
    waiters = Map.get(state.waking, workload, [])
    {waiters, %{state | waking: Map.delete(state.waking, workload)}}
  end

  defp reply_all(waiters, reply) do
    for {from, _req, _principal} <- waiters, do: GenServer.reply(from, reply)
    :ok
  end

  # -- miss tracing (Task 10) ------------------------------------------------

  # Merge boundary stamps into a workload's tracing bundle. A no-op when the miss
  # never seeded a bundle (a straggler proxied without a wake, or tracing off).
  defp stamp_wake_trace(state, workload, fields) do
    case Map.get(state.wake_traces, workload) do
      nil -> state
      trace -> %{state | wake_traces: Map.put(state.wake_traces, workload, Map.merge(trace, fields))}
    end
  end

  defp pop_wake_trace(state, workload) do
    {Map.get(state.wake_traces, workload), %{state | wake_traces: Map.delete(state.wake_traces, workload)}}
  end

  defp trace_parent(nil), do: nil
  defp trace_parent(trace), do: Map.get(trace, :traceparent)

  # Emit the retroactive `park`/`placement`/`wake` child spans under the router's
  # activate ROOT (the session queue_wait idiom: explicit start_times reconstruct a
  # phase that spanned another process). A nil bundle (straggler / tracing off) or a
  # nil traceparent restores no parent, so the spans are roots the exporter drops
  # when tracing is off -> a clean no-op. `wake` carries the gate numbers
  # (ember.wake_ms, ember.cold). placement/wake stamps are absent only if the wake
  # errored before start_wake stamped them (a bad plan); guard each phase.
  defp emit_wake_phase_spans(nil, _workload, _wake_ended), do: :ok

  defp emit_wake_phase_spans(trace, workload, wake_ended) do
    SessionTrace.restore_parent(Map.get(trace, :traceparent))
    attrs = %{"ember.workload" => workload}

    with %{park_start: park_start, placement_start: placement_start} <- trace do
      # park: the first miss parked here until the wake began the placement read.
      emit_phase_span("embervm.serving.park", park_start, placement_start, attrs)
      # placement: the pure plan_wake read (relight-vs-cold decision).
      emit_phase_span("embervm.serving.placement", placement_start, trace.placement_end, attrs)

      # wake: the StartServing RPC (cold boot or relight). Carries the gate numbers.
      wake_attrs = Map.merge(attrs, %{"ember.cold" => trace.cold, "ember.wake_ms" => span_ms(trace.wake_start, wake_ended)})
      emit_phase_span("embervm.serving.wake", trace.wake_start, wake_ended, wake_attrs)
    else
      # No placement stamp: the miss was denied/failed before start_wake ran. Emit
      # just the park span so the parked wait is still visible.
      _ ->
        if is_integer(trace[:park_start]) do
          emit_phase_span("embervm.serving.park", trace.park_start, wake_ended, attrs)
        end
    end

    # This runs on the long-lived manager process: reset the restored remote parent
    # off the process dict so it never leaks into the NEXT {:wake_done}. publish
    # re-restores the same root for its own span.
    clear_current_span()
    :ok
  end

  # A retroactive completed span: opened with an explicit start_time and closed
  # immediately (its wall duration is start_time..now). Mirrors the session
  # queue_wait span. Skipped if either boundary is missing.
  defp emit_phase_span(_name, nil, _stop, _attrs), do: :ok
  defp emit_phase_span(_name, _start, nil, _attrs), do: :ok

  defp emit_phase_span(name, start_time, _stop, attrs) do
    Tracer.with_span name, %{start_time: start_time, attributes: attrs} do
      :ok
    end
  end

  # :opentelemetry.timestamp/0 returns erlang NATIVE time units (what the span
  # start_time option expects). The *_ms gate attributes want milliseconds, so
  # convert the native delta (never assume nanoseconds).
  defp span_ms(start_native, stop_native) when is_integer(start_native) and is_integer(stop_native) do
    System.convert_time_unit(stop_native - start_native, :native, :millisecond)
  end

  defp span_ms(_, _), do: 0

  # Detach any restored remote parent from THIS process's OTel context. Guarded: a
  # trace hiccup must never crash finish_wake.
  defp clear_current_span do
    Tracer.set_current_span(:undefined)
    :ok
  rescue
    _ -> :ok
  catch
    _, _ -> :ok
  end

  # -- adoption --------------------------------------------------------------

  # Reconcile the ServingStore projection against every node's reported serving
  # inventory, then re-derive + re-push. Mirrors SessionManager.do_reconcile:
  #   * a node-reported LIVE serving VM (by vm_id) -> rebind the instance's endpoint
  #     and force it published (re-enters the fan-out), healing a control-plane
  #     restart (durable published/starting but process gone) and starting/
  #     relighting limbo whose node actually holds a live VM;
  #   * a node-reported SNAPSHOT (no live VM) -> heal the instance to banked;
  #   * NEITHER a VM nor a snapshot, but the instance's node IS reporting -> the VM
  #     and snapshot both vanished: mark it failed (the ONLY reaping, and only on
  #     node-confirmed absence, never on a disconnect);
  #   * orphan snapshots (no live instance) -> evict.
  # Then publish (a control-plane restart republishes exactly the same endpoints
  # without touching any VM).
  defp do_reconcile(state) do
    facts = NodeCapacity.all(state.capacity_table)
    live_vms = index_serving_vms(facts)
    snapshots = index_serving_snapshots(facts)

    state =
      ServingStore.all(state.store)
      |> Enum.reject(&ServingState.terminal?(&1.state))
      |> Enum.reduce(state, fn instance, acc ->
        adopt_one(acc, instance, live_vms, snapshots)
      end)

    state = evict_orphan_snapshots(state, facts)

    # Re-derive + re-push after adoption so the fan-out reflects the healed facts.
    EndpointPublisher.publish(state.publisher)
    state
  end

  # vm_id -> {node_id, vm, workload}; snapshot_ref -> {node_id, snapshot}. Serving
  # VMs carry no instance id, so correlation is by vm_id (against the instance's
  # recorded vm_id) and, for snapshots, by snapshot_ref.
  defp index_serving_vms(facts) do
    for f <- facts, v <- Map.get(f, :serving_vms, []) || [], into: %{} do
      {v.vm_id, {f.configured_id, v}}
    end
  end

  defp index_serving_snapshots(facts) do
    for f <- facts, s <- Map.get(f, :serving_snapshots, []) || [], into: %{} do
      {s.snapshot_ref, {f.configured_id, s}}
    end
  end

  defp adopt_one(state, instance, live_vms, snapshots) do
    cond do
      # This manager has an in-flight wake for the workload: it owns the transition,
      # so a periodic reconcile must NOT touch its instances. On BOOT `waking` is
      # empty (fresh process), so boot adoption still fully heals every limbo.
      Map.has_key?(state.waking, instance.workload) ->
        state

      # The node reports a LIVE serving VM matching the instance's vm_id: rebind the
      # endpoint + force published.
      is_binary(instance.vm_id) and Map.has_key?(live_vms, instance.vm_id) ->
        {node_id, vm} = Map.fetch!(live_vms, instance.vm_id)
        adopt_live(state, instance, node_id, vm)

      # No live VM, but the node reports the instance's SNAPSHOT: it is banked.
      is_binary(instance.snapshot_ref) and Map.has_key?(snapshots, instance.snapshot_ref) ->
        heal_to_banked(state, instance)

      # Neither a VM nor a snapshot for this instance, and its node IS reporting:
      # the state truly vanished -> failed. If the node is not reporting at all (a
      # disconnect), leave it untouched.
      node_reporting?(state, instance.node_id) ->
        fail_instance(state, instance.instance_id, "vm_and_snapshot_vanished")
        state

      true ->
        state
    end
  end

  # Rebind a live serving VM: force the instance published from node truth with the
  # node-reported endpoint + health. Idempotent.
  defp adopt_live(state, instance, node_id, vm) do
    ServingStore.adopt_state(state.store, instance.instance_id, :published)

    ServingStore.adopt_endpoint(state.store, instance.instance_id, node_id, vm.vm_id, %{
      ip: Map.get(vm, :ip),
      port: Map.get(vm, :port),
      healthy: Map.get(vm, :healthy, true)
    })

    state
  end

  defp heal_to_banked(state, %{state: :banked}), do: state

  defp heal_to_banked(state, instance) do
    ServingStore.adopt_state(state.store, instance.instance_id, :banked)
    Logger.info("embervm serving adopted (banked)", instance_id: instance.instance_id)
    state
  end

  defp node_reporting?(state, node_id) when is_binary(node_id) do
    match?({:ok, _}, NodeCapacity.fetch(state.capacity_table, node_id))
  end

  defp node_reporting?(_state, _node_id), do: false

  # Evict serving snapshots a node reports whose instance row is terminal or absent
  # (the instance is gone but its snapshot squats disk). StopServing is not the
  # eviction verb; serving snapshots reuse EvictSnapshot (R2) unchanged, but in v1
  # the durable serving_evicted op + the node's own GC handle it. Here we record the
  # durable eviction intent so the projection reflects it; the node's rescan drops
  # the file. A terminal/absent instance with a reported snapshot is the orphan.
  defp evict_orphan_snapshots(state, facts) do
    for f <- facts, snap <- Map.get(f, :serving_snapshots, []) || [] do
      case find_instance_by_snapshot(state, snap.snapshot_ref) do
        {:ok, %{state: st, instance_id: id}} when st in [:evicted, :destroyed, :failed] ->
          _ = evict_snapshot(state, id, snap.snapshot_ref)

        :none ->
          # A snapshot no live instance claims: nothing durable to update (no row),
          # the node's own GC reclaims it. Logged for visibility.
          Logger.info("embervm serving: orphan snapshot on node (no instance row)",
            snapshot_ref: snap.snapshot_ref
          )

        _ ->
          :ok
      end
    end

    state
  end

  defp find_instance_by_snapshot(state, snapshot_ref) do
    ServingStore.all(state.store)
    |> Enum.find(:none, fn i -> i.snapshot_ref == snapshot_ref end)
    |> case do
      :none -> :none
      instance -> {:ok, instance}
    end
  end

  defp evict_snapshot(state, instance_id, _snapshot_ref) do
    ServingStore.transition(
      state.store,
      instance_id,
      :evict,
      :serving_evicted,
      %{reason: "orphan"},
      %{}
    )
  end

  # -- failure ---------------------------------------------------------------

  # Mark an instance failed (a wake failure or a vanished VM): the terminal
  # serving_failed op. A no-op if the instance is already terminal or the
  # transition is illegal (a benign race).
  defp fail_instance(state, instance_id, reason) do
    case ServingStore.get(state.store, instance_id) do
      {:ok, instance} ->
        unless ServingState.terminal?(instance.state) do
          _ =
            ServingStore.transition(
              state.store,
              instance_id,
              :fail,
              :serving_failed,
              %{reason: reason},
              %{}
            )
        end

        :ok

      :error ->
        :ok
    end
  end

  # -- wake-rate limit -------------------------------------------------------

  defp wake_allowed?(%{wake_max: max}, _principal) when not is_integer(max) or max <= 0, do: true

  defp wake_allowed?(state, principal) do
    now = state.clock.()
    length(recent_wakes(state, principal, now)) < state.wake_max
  end

  defp record_wake(state, principal) do
    now = state.clock.()
    recent = recent_wakes(state, principal, now)
    %{state | wake_events: Map.put(state.wake_events, principal, [now | recent])}
  end

  defp recent_wakes(state, principal, now) do
    cutoff = now - state.wake_window_ms

    state.wake_events
    |> Map.get(principal, [])
    |> Enum.filter(&(&1 > cutoff))
  end

  # -- catalog + placement helpers -------------------------------------------

  defp serving_workload?(state, workload) do
    match?({:ok, %{class: "serving"}}, WorkloadCatalog.fetch(state.catalog_table, workload))
  end

  defp catalog_entry(state, workload) do
    case WorkloadCatalog.fetch(state.catalog_table, workload) do
      {:ok, entry} -> entry
      :error -> %{}
    end
  end

  defp serving_cfg(entry), do: Map.get(entry, :serving, %{port: 8080, health_path: "/healthz"})

  # The principal a cold-created serving instance is attributed to. Serving is
  # workload-scoped (one owner), so the workload's own owner principal is used; v1
  # has no per-CR owner field wired, so it falls back to a system principal. This is
  # the op's principal for usage/audit, not a caller identity.
  defp wake_principal(_state, workload), do: "system:serving:#{workload}"

  # The first HEALTHY published endpoint for a workload, for the straggler path.
  defp first_published_endpoint(state, workload) do
    case ServingStore.published_endpoints(state.store, workload) do
      [ep | _] -> {:ok, ep}
      [] -> :none
    end
  end

  # -- daemon seams ----------------------------------------------------------

  defp safe_start_serving(state, node_id, req) do
    with {:ok, channel} <- safe_channel(state.channel_fun, node_id) do
      case state.start_serving_fun.(channel, req) do
        {:error, reason} = err ->
          # A wake that failed because the channel's transport is dead (a replaced
          # noded pod, wrapped by the Mint adapter as an RPCError) must tear the cached
          # channel down so the NEXT miss re-dials; else serving stays wedged on that
          # node until the control plane restarts (Embervm.NodeChannel.transport_dead?/1).
          if Embervm.NodeChannel.transport_dead?(reason) do
            _ = state.invalidate_fun.(node_id, channel)
          end

          err

        other ->
          other
      end
    end
  rescue
    e -> {:error, {:start_serving_raised, e}}
  catch
    kind, reason -> {:error, {:start_serving_raised, {kind, reason}}}
  end

  defp safe_channel(channel_fun, node_id) do
    channel_fun.(node_id)
  rescue
    e -> {:error, {:channel_raised, e}}
  catch
    kind, reason -> {:error, {:channel_raised, {kind, reason}}}
  end

  defp default_start_serving(channel, req) do
    Embervm.Node.V1.NodeService.Stub.start_serving(channel, req)
  end

  # -- restore-on-miss RPC (R6, Task 8) ---------------------------------------

  # Restore the SERVING bundle for `workload` from the object store back onto the
  # node's disk (RestoreArtifact, kind SERVING), then record :artifact_restored.
  # Best-effort: a restore failure returns :error and the caller (the wake worker)
  # falls through to the relight, which the daemon degrades to a cold boot on a
  # truly-missing snapshot (fail-open warmth). Idempotent on the daemon side, so a
  # re-run of a partially-restored artifact is safe.
  # `dial_id` is the SELECTED instance's dial key (Step 4): the restore RPC must land
  # the bundle on the same instance the subsequent relight dials, since serving
  # snapshots are per-instance on disk (PR-2.5). `node_id` is the node-name anchor
  # kept only for the VENDOR stamp (RestoreVendor resolves the node's CPU vendor via
  # NodeCapacity.fetch, which keys on the node name; an instance_id string would not
  # resolve, and the vendor is identical across a node's instances anyway).
  defp restore_bundle(state, dial_id, node_id, workload, snapshot_ref) do
    ref = %ArtifactRef{kind: :ARTIFACT_KIND_SERVING, workload: workload, ref: snapshot_ref}

    case safe_restore_artifact(state, dial_id, node_id, ref) do
      {:ok, resp} ->
        record_restore(state, workload, :ARTIFACT_KIND_SERVING, snapshot_ref, resp)
        :ok

      other ->
        Logger.warning("embervm serving: bundle restore-on-miss failed, degrading to cold",
          workload: workload,
          snapshot_ref: snapshot_ref,
          reason: inspect(other)
        )

        :error
    end
  end

  # Dial the restore on `dial_id` (the relight's instance, Step 4) but stamp the
  # vendor off `node_id` (the node-name anchor RestoreVendor can resolve). Transport-
  # death invalidation is keyed on `dial_id` (the channel we actually dialled).
  defp safe_restore_artifact(state, dial_id, node_id, %ArtifactRef{} = ref) do
    req = %RestoreArtifactRequest{artifact: ref, trace: %Trace{workload: ref.workload}}
    req = Embervm.RestoreVendor.stamp(state.capacity_table, node_id, req)

    with {:ok, channel} <- safe_channel(state.channel_fun, dial_id) do
      # The `artifact_restore` span (Task 11): a child span around the
      # RestoreArtifact RPC (the restore-on-miss read path). Identity up front,
      # bytes-moved/skipped stamped from the response.
      Tracer.with_span "embervm.artifact_restore",
                       %{
                         attributes: %{
                           "ember.workload" => ref.workload,
                           "ember.artifact_kind" => artifact_kind_string(ref.kind),
                           "ember.artifact_ref" => ref.ref
                         }
                       } do
        result = restore_rpc(state, dial_id, channel, req)
        stamp_restore_span(result)
        result
      end
    end
  end

  # The RestoreArtifact RPC with transport-death channel invalidation. Extracted so
  # the `artifact_restore` span wraps exactly the call and its result. Invalidates by
  # `dial_id` (the instance the channel was dialled on, Step 4).
  defp restore_rpc(state, dial_id, channel, req) do
    try do
      case state.restore_artifact_fun.(channel, req) do
        {:error, reason} = err ->
          if Embervm.NodeChannel.transport_dead?(reason) do
            _ = state.invalidate_fun.(dial_id, channel)
          end

          err

        other ->
          other
      end
    rescue
      e -> {:error, {:restore_artifact_raised, e}}
    catch
      :exit, reason ->
        _ = state.invalidate_fun.(dial_id, channel)
        {:error, {:restore_artifact_raised, {:exit, reason}}}

      kind, reason ->
        {:error, {:restore_artifact_raised, {kind, reason}}}
    end
  end

  # Stamp bytes-moved/skipped onto the current `artifact_restore` span from a
  # successful RestoreArtifact response. A failure leaves only the identity attrs.
  defp stamp_restore_span({:ok, resp}) do
    Tracer.set_attributes(%{
      "ember.bytes_moved" => Map.get(resp, :bytes_moved, 0),
      "ember.skipped" => Map.get(resp, :skipped, false)
    })
  end

  defp stamp_restore_span(_other), do: :ok

  # Append the audit-only :artifact_restored op (no projection table; the log itself
  # is the record). Best-effort: an append failure must never fail the wake, which
  # already ran the restore RPC (the durable state is the restored bytes on disk, not
  # this audit row).
  defp record_restore(state, workload, kind, ref, resp) do
    op = %Embervm.OpLog.Op{
      kind: :artifact_restored,
      tenant: state.tenant,
      principal: wake_principal(state, workload),
      workload: workload,
      ts: state.clock.(),
      payload: %{
        kind: artifact_kind_string(kind),
        ref: ref,
        bytes_moved: Map.get(resp, :bytes_moved, 0),
        generation: Map.get(resp, :generation, 0),
        skipped: Map.get(resp, :skipped, false)
      }
    }

    _ = state.op_log_mod.append(state.op_log, op)
    :ok
  rescue
    e ->
      Logger.warning("embervm serving: artifact_restored append raised", workload: workload, error: inspect(e))
      :ok
  end

  defp artifact_kind_string(:ARTIFACT_KIND_SERVING), do: "serving"
  defp artifact_kind_string(other), do: to_string(other)

  defp default_restore_artifact(channel, req) do
    Embervm.Node.V1.NodeService.Stub.restore_artifact(channel, req)
  end

  # -- misc ------------------------------------------------------------------

  defp audit_denial(_state, principal, workload, reason) do
    Embervm.Metering.record_denial(principal, workload, reason)
  end

  defp mint_id(%{id_fun: fun}) when is_function(fun, 0), do: fun.()
  defp mint_id(state), do: "srv-" <> String.trim_leading(Embervm.SessionId.new(state.clock.()), "s-")

  defp schedule(msg, interval_ms) when interval_ms > 0 do
    Process.send_after(self(), msg, interval_ms)
  end

  defp schedule(_msg, _interval), do: :ok

  defp default_clock, do: System.system_time(:millisecond)
end
