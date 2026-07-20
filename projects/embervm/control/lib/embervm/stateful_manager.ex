defmodule Embervm.StatefulManager do
  @moduledoc """
  The stateful wake brain (R4, Task 8): the L4 counterpart of
  `Embervm.ServingManager`, and the headline verb of this rung. An inbound TCP
  connection to a sleeping (banked or cold) stateful workload arrives at
  `Embervm.TcpActivator`, which resolves the workload from the LOCAL accept port
  (the listener port IS the workload identity at L4, decision 5: there is no
  header to read) and calls `wake/2` here. This module single-flights the wake
  (relight a banked bundle, or cold/fresh-boot on the volume), publishes the
  fresh endpoint via `Embervm.EndpointPublisher`, and hands the woken `{ip,
  port}` back to every parked connection so the activator can splice bytes to
  the VM. Every SUBSEQUENT connection reaches the VM node-Envoy-direct with zero
  control-plane involvement (the same off-hit-path shape as serving).

  ## single-flight wake (exactly one StartStateful per concurrent connect burst)

  N concurrent inbound connections for one workload must produce exactly ONE
  StartStateful and N spliced sessions. `waking` maps `workload -> [{from,
  principal}]`, the same shape as `ServingManager.waking` minus the request
  envelope (a stateful miss carries no HTTP request to replay, only the raw
  socket the activator already holds). The FIRST connection for a workload
  registers its caller AND kicks one wake worker; every concurrent connection
  finds the workload already in `waking` and only appends. When the wake
  completes, every parked caller is resolved to the fresh endpoint.

  Because the class is a SINGLETON (decision 3, `StatefulStore.start/2` refuses
  a second live instance), the single-flight here is not just an optimization
  against duplicate StartStateful calls, it is what KEEPS the class singleton
  under concurrent misses: without it, two connections racing the empty cluster
  could both attempt to boot the volume writable, and the daemon's own
  FAILED_PRECONDITION guard would only catch the loser after a wasted RPC.

  ## the reply contract (a parked connection never blocks forever)

  A caller parks as `GenServer.call(manager, {:wake, workload, principal},
  :infinity)`. Every terminal path replies: a rate-limit or parked-cap denial
  replies immediately; a wake success replies `{:ok, endpoint}`; a wake failure
  or the volume being gone replies `{:error, reason}`. No path drops a `from`.

  ## wake failure keeps the activator published (retryable)

  On a wake failure the parked callers get an error (the activator closes their
  sockets), the instance (if one was created) is marked `failed`, and the
  workload's L4 cluster STAYS on the activator fallback (the publisher renders
  it whenever `StatefulStore.published_endpoint/1` is nil, which is still true
  after a failed wake). The client's OWN retry (a reconnect) is therefore the
  retry mechanism: TCP has no request-level replay the way the serving
  activator can hold and resolve a parked HTTP request, so a failed miss simply
  closes and the NEXT inbound connection tries again, subject to the wake-rate
  limit. This is the "reconnect-once" caveat: a stateful client behind a
  connection pool that does not retry on a reset will see one failed dial per
  wake failure.

  ## plan_wake: relight vs cold, and the volume-anchored node

  `plan_wake/2` reads `StatefulStore` + `StatefulStore.pair_valid?/1` purely (ETS
  reads only, mirroring `ServingManager.plan_wake/2`):

    * a VALID pair (a banked bundle whose `snapshot_generation` matches the
      volume's current `generation`) relights: `{:relight, instance, node_id,
      snapshot_ref}`.
    * a banked bundle with a BROKEN pair is evicted first (the durable
      `stateful_evicted` op, reason `pair_broken`), then the wake falls through
      to cold: `{:cold, node_id, boot_ref, mode}`.
    * no bundle at all: cold or fresh, `{:cold, node_id, boot_ref, mode}`, `mode`
      is `FRESH` when the workload has never had a volume (no volume row yet:
      the daemon must create it) and `COLD` when a volume row already exists
      (an explicit cold boot against existing data, no bundle to resume).

  Unlike serving (whose cold placement is a rendezvous hash over any eligible
  node), a stateful wake is NOT a free placement choice once the workload has a
  volume: decision 11 anchors the wake to the NODE THAT HOLDS THE VOLUME (volume
  files are node-local NVMe, never migrated by this rung). `plan_wake/2` reads
  the volume's `node_id` from `StatefulStore.get_volume/2` and requires that
  node to still be reporting (`Embervm.NodeCapacity.fetch/2`); if the node is
  gone, the wake is `{:error, :volume_node_gone}` (a FAILED_PRECONDITION-shaped
  refusal, never a silent recreate on a different node). Only a workload with NO
  volume row yet (its true first boot) is free to place via
  `Embervm.ServingPlacement`-style eligibility, because there is no data to
  anchor to.

  ## wake-rate limit: per-workload (the per-principal intent, singleton-reduced)

  The plan's per-principal wake-rate default (10/min) is honored here KEYED BY
  WORKLOAD, exactly the `ServingManager` per-workload adaptation and for the
  same underlying reason stated more strongly: a stateful workload is a
  SINGLETON owned by exactly one principal (there is no concept of many
  concurrent tenants sharing one stateful sandbox), so "per principal" and "per
  workload" name the same set for this class. A future PR that adds a real
  per-CR owner principal to the catalog can key on it directly without changing
  this module's shape; today `wake_principal/1` synthesizes
  `system:stateful:<workload>` as the op-log's owner attribution, matching
  `ServingManager.wake_principal/2` in spirit (that one also takes a `state`
  arg it does not use; this one drops the unused param).

  ## the parked-connection cap (no 429 at L4; the audited op is the signal)

  `park_cap` bounds how many connections may queue behind one in-flight wake
  (default 16, an order of magnitude below serving's 64: a stateful sandbox is a
  singleton with one owner, so a burst this deep is already anomalous). There is
  no HTTP status to return at L4; `Embervm.TcpActivator` closes the excess
  connection after this module records an audit denial op
  (`Embervm.Metering.record_denial/3`), so the observable is the op, not a
  response code.

  ## restart adoption (the #3517 lesson, fourth application)

  `reconcile/1` (boot + periodic timer) reconciles the `StatefulStore`
  projection against every node's reported `stateful_vms` + `stateful_bundles` +
  `volumes` (mirrors `ServingManager.reconcile/1` exactly, adapted for the
  singleton + persistent-volume shape):

    * a node-reported LIVE stateful VM (by `vm_id`) -> `adopt_state(:serving)` +
      `adopt_endpoint` (rebind ip/port/healthy from node truth), healing a
      control-plane restart without touching the VM;
    * no live VM but the node reports the instance's BUNDLE (by
      `snapshot_ref`) -> heal to `:banked`;
    * neither, and the instance's node IS reporting -> the VM and bundle both
      vanished, mark it `failed` (the only reaping, only on node-confirmed
      absence, never a mere disconnect);
    * every reporting node's `volumes` facts are folded into
      `StatefulStore.upsert_volume/3` (the live node-ledger fact: generation +
      allocated_bytes), so the pair-validity check always compares against the
      CURRENT node-reported generation, not a stale boot-time snapshot;
    * `StatefulStore.eager_evict_broken_pairs/0` runs after the volume facts are
      refreshed, so a bundle whose pair broke while the control plane was down
      (a generation bump the daemon recorded but the control plane missed) is
      evicted on the very first reconcile rather than surviving until a client
      tries to relight it.

  A single-flight wake in progress for a workload is left untouched by a
  periodic reconcile (mirroring `ServingManager.adopt_one/4`'s `waking` guard):
  the in-flight wake owns that workload's transition, and the reconcile that
  runs after the wake's own `finish_wake` will see the live VM and adopt it
  cleanly (idempotent). After every reconcile pass, `EndpointPublisher.publish/1`
  re-derives the L4 fan-out so a control-plane restart with a live stateful VM
  republishes the identical endpoint without ever touching the VM.
  """

  use GenServer
  require Logger

  require OpenTelemetry.Tracer, as: Tracer

  alias Embervm.{EndpointPublisher, NodeCapacity, StatefulState, StatefulStore, WorkloadCatalog}

  alias Embervm.Node.V1.{
    ArtifactRef,
    EvictArtifactRequest,
    ResourceSpec,
    RestoreArtifactRequest,
    StartStatefulRequest,
    StartStatefulResponse,
    StopStatefulRequest,
    DeleteVolumeRequest,
    Trace
  }

  # Wake-rate limit: wakes per WORKLOAD per window. See the moduledoc's
  # "per-workload, singleton-reduced" note for why this is the per-principal
  # intent, not a deviation from it.
  @default_wake_max 10
  @default_wake_window_ms 60_000

  # Parked-connection cap per workload: an order of magnitude below serving's
  # default (64) because a stateful sandbox has exactly one owner, so a deep
  # burst is already anomalous rather than ordinary multi-tenant fan-in.
  @default_park_cap 16

  # Wake-worker bound (R6, Task 10). A wedged boot (a guest that never opens its
  # port, chaining `:infinity` waits) must NOT pin `waking` forever, starving
  # `adopt_one` and overflowing the park (the R5 drill symptom). So the WORKER is
  # bounded by the workload's `wakeTimeoutSeconds` plus this margin: on the bound the
  # wake FAILS (single-flight released, parked callers erred, the banked bundle left
  # re-wakeable via adoption), NOT held. The parked caller's own `:infinity`
  # GenServer.call is untouched: a caller waits as long as it chooses, the bound is on
  # the wake it waits behind. Default stateful wakeTimeoutSeconds is 60, so the bound
  # defaults to ~75s.
  @default_wake_timeout_margin_ms 15_000
  # Fallback wakeTimeoutSeconds when the catalog entry carries none (matches
  # WorkloadWatcher's @stateful_defaults.wake_timeout_seconds).
  @default_wake_timeout_seconds 60

  # -- Client API ------------------------------------------------------------

  @spec start_link(keyword()) :: GenServer.on_start()
  def start_link(opts) do
    case Keyword.get(opts, :name, __MODULE__) do
      nil -> GenServer.start_link(__MODULE__, opts)
      name -> GenServer.start_link(__MODULE__, opts, name: name)
    end
  end

  @doc """
  Handles one activator connect for `workload` on behalf of `principal` (the
  workload's synthesized owner; see the moduledoc). Blocks (`:infinity`) until
  the workload is woken and returns `{:ok, %{ip, port}}` for the
  `Embervm.TcpActivator` to splice the parked connection to, OR a denial:

    * `{:ok, %{ip, port}}`             -> splice (a fresh wake, OR the
      STRAGGLER path: a connection reached the activator while a healthy
      instance already exists, resolved without a wake). A fresh wake's map
      also carries `generation` (the volume generation the boot landed on,
      Task 10 observability); the straggler path replies with just `{ip, port}`
      since it never wakes anything.
    * `{:error, {:wake_rate, _}}`               -> close (per-workload wake-rate limit)
    * `{:error, {:park_full, _}}`                -> close (parked-connection cap)
    * `{:error, {:wake_failed, r}}`              -> close (start error / readiness
      timeout / a placement refusal, INCLUDING `{:wake_failed, :volume_node_gone}`
      when the volume's anchor node is down and `{:wake_failed, :no_capacity}`
      when no node is eligible for a true first boot: both ride the same
      generic wake-failure wrap as an RPC failure, since from the caller's
      perspective they are equally "this wake did not happen, try again")
    * `{:error, {:unknown_workload}}`            -> close (misconfig / race with catalog)
  """
  @spec wake(GenServer.server(), String.t(), String.t()) :: {:ok, map()} | {:error, term()}
  def wake(server \\ __MODULE__, workload, principal) do
    GenServer.call(server, {:wake, workload, principal}, :infinity)
  end

  @doc """
  Whether any connection is currently parked (in-flight wake) for `workload`. The
  interruptible-bank sweeper (ADR embervm/008) reads this the instant its
  CHECKPOINT completes to decide COMMIT vs ABORT: a parked connection means a
  client is waiting for this workload NOW, so aborting (resume hot) serves it
  faster than committing (bank then relight). A pure ETS-view read of
  `state.waking`.
  """
  @spec parked?(GenServer.server(), String.t()) :: boolean()
  def parked?(server \\ __MODULE__, workload) do
    GenServer.call(server, {:parked?, workload})
  end

  @doc """
  Runs one adoption reconcile synchronously (the boot continue + the periodic
  sweep run the same code) and returns after it completes. Reconciles the
  StatefulStore projection against every node's reported stateful inventory
  (live VMs, banked bundles, volume facts), then re-derives + re-pushes the L4
  fan-out. Tests drive adoption deterministically through this.
  """
  @spec reconcile(GenServer.server()) :: :ok
  def reconcile(server \\ __MODULE__) do
    GenServer.call(server, :reconcile, :infinity)
  end

  @doc """
  Destroys the live instance of `workload` (StopStateful DESTROY) AND evicts
  its banked bundle, so the next connection cold-boots the CURRENT image
  against the still-intact volume. The management verb behind `DELETE
  /v1/stateful/:name/instance`. Synchronous; returns `%{destroyed: n, evicted:
  m}` (each 0 or 1, the class is a singleton).
  """
  @spec destroy_instance(GenServer.server(), String.t()) :: %{destroyed: non_neg_integer(), evicted: non_neg_integer()}
  def destroy_instance(server \\ __MODULE__, workload) do
    GenServer.call(server, {:destroy_instance, workload}, :infinity)
  end

  @doc """
  Deletes `workload`'s volume file and generation ledger (the ONLY destructive
  data verb; a CR deletion never reaches this). The management verb behind
  `DELETE /v1/stateful/:name/volume`. REFUSES `{:error, :instance_exists}` while
  ANY non-terminal instance exists for the workload (live OR banked; a banked
  instance's bundle is paired to this volume's generation, so deleting the
  volume out from under it would silently orphan the bundle) so deletion is
  always an explicit, unambiguous act against a workload with nothing left
  attached. Synchronous; returns `{:ok, %{deleted: true}}` or the refusal.
  """
  @spec delete_volume(GenServer.server(), String.t()) :: {:ok, %{deleted: true}} | {:error, term()}
  def delete_volume(server \\ __MODULE__, workload) do
    GenServer.call(server, {:delete_volume, workload}, :infinity)
  end

  # -- GenServer callbacks ---------------------------------------------------

  @impl true
  def init(opts) do
    state = %{
      store: Keyword.get(opts, :store, StatefulStore),
      publisher: Keyword.get(opts, :publisher, EndpointPublisher),
      capacity_table: Keyword.get(opts, :capacity_table, NodeCapacity.table()),
      catalog_table: Keyword.get(opts, :catalog_table, WorkloadCatalog.table()),
      clock: Keyword.get(opts, :clock, &default_clock/0),
      id_fun: Keyword.get(opts, :id_fun, nil),
      tenant: Keyword.get(opts, :tenant, "homelab"),
      # Daemon stateful-verb seams (injected for tests; production dials the
      # real NodeService stub over the shared NodeChannel).
      channel_fun: Keyword.get(opts, :channel_fun, &Embervm.NodeChannel.get/1),
      invalidate_fun: Keyword.get(opts, :invalidate_fun, &Embervm.NodeChannel.invalidate/2),
      start_stateful_fun: Keyword.get(opts, :start_stateful_fun, &default_start_stateful/2),
      stop_stateful_fun: Keyword.get(opts, :stop_stateful_fun, &default_stop_stateful/2),
      delete_volume_fun: Keyword.get(opts, :delete_volume_fun, &default_delete_volume/2),
      # Restore-on-miss seam (R6, Task 8): (channel, %RestoreArtifactRequest{}) ->
      # {:ok, %RestoreArtifactResponse{}} | {:error, _}. Fetches a banked STATEFUL
      # bundle or a VOLUME back onto local disk from the object store before a wake
      # relights/cold-boots on a TRUE local miss. Injected for tests; production
      # dials the real NodeService stub.
      restore_artifact_fun: Keyword.get(opts, :restore_artifact_fun, &default_restore_artifact/2),
      # Remote artifact eviction seam (R6, Task 9): (channel, %EvictArtifactRequest{})
      # -> {:ok, %EvictArtifactResponse{}} | {:error, _}. Fired alongside DeleteVolume
      # so the store copy of a workload's VOLUME is dropped on the same workload-
      # deletion trigger. The generation guard holds by construction here: delete is
      # refused while ANY non-terminal instance exists, so no bundle still pairs with
      # the volume generation being evicted (standing decision 8). Injected for tests.
      evict_artifact_fun: Keyword.get(opts, :evict_artifact_fun, &default_evict_artifact/2),
      # The op-log the restore audit record (:artifact_restored) is appended to.
      # Injected for tests; production uses the SQLite backend.
      op_log: Keyword.get(opts, :op_log, Embervm.OpLog.SQLite),
      # R4, D-R4.PR-7.1 (MMDS-lite over boot-args): reads a K8s Secret into a
      # decoded key/value map for cold_request/2 to populate mmds_env from.
      # Injected so tests can fake the K8s round-trip; production defaults to
      # the real Embervm.K8s client.
      get_secret_fun: Keyword.get(opts, :get_secret_fun, &Embervm.K8s.get_secret/2),
      # workload -> [{from, principal}] parked behind an in-flight wake
      # (single-flight). No request envelope: a stateful miss carries no HTTP
      # request to replay, only the raw socket the activator already holds.
      waking: %{},
      # workload -> per-miss tracing bundle (Task 10), the stateful counterpart of
      # ServingManager's wake_traces. UNLIKE serving, a raw TCP accept carries no
      # HTTP request and so no router-issued traceparent to nest phases under: the
      # `park`/`wake` spans emitted from this bundle are therefore ROOTS (accept-
      # driven, the same "no caller trace" shape as the sweeper's stats_sweep), not
      # children of an upstream trace. Seeded by the first connection (park_start),
      # stamped by start_wake (wake_start + the cold bool), cleared by finish_wake.
      # Absent (tracing off in CI) => spans are a clean no-op.
      wake_traces: %{},
      # principal -> [wake timestamps within the window]. Sliding-window counter,
      # keyed by workload per the moduledoc (a stateful workload's principal IS
      # its workload).
      wake_events: %{},
      wake_max: Keyword.get(opts, :wake_max, @default_wake_max),
      wake_window_ms: Keyword.get(opts, :wake_window_ms, @default_wake_window_ms),
      park_cap: Keyword.get(opts, :park_cap, @default_park_cap),
      # workload -> monotonic ms the in-flight wake started (Task 10). Feeds the
      # adoption self-recovery + the park_full oldest-waiter age: a workload still
      # waking past 2 * wakeTimeoutSeconds is a wedged wake whose worker never
      # reported, recovered rather than skipped forever.
      wake_started: %{},
      # Wake-worker bound (Task 10): derived per-wake from wakeTimeoutSeconds + this
      # margin unless `wake_bound_ms` overrides it (tests inject a tiny bound). The
      # monotonic clock the bound + the stuck-check read is injectable too.
      wake_timeout_margin_ms: Keyword.get(opts, :wake_timeout_margin_ms, @default_wake_timeout_margin_ms),
      wake_bound_ms: Keyword.get(opts, :wake_bound_ms, nil),
      mono_clock: Keyword.get(opts, :mono_clock, &default_mono/0),
      # The adoption reconcile cadence (0 = off, tests drive reconcile/1).
      reconcile_interval_ms: Keyword.get(opts, :reconcile_interval_ms, 0)
    }

    if state.reconcile_interval_ms > 0 do
      {:ok, state, {:continue, :boot}}
    else
      {:ok, state}
    end
  end

  @impl true
  def handle_continue(:boot, state) do
    state = do_reconcile(state)
    schedule(:reconcile, state.reconcile_interval_ms)
    {:noreply, state}
  end

  @impl true
  def handle_call({:wake, workload, principal}, from, state) do
    handle_wake(state, workload, principal, from)
  end

  def handle_call({:parked?, workload}, _from, state) do
    {:reply, Map.has_key?(state.waking, workload), state}
  end

  def handle_call(:reconcile, _from, state) do
    {:reply, :ok, do_reconcile(state)}
  end

  def handle_call({:destroy_instance, workload}, _from, state) do
    {reply, state} = do_destroy_instance(state, workload)
    {:reply, reply, state}
  end

  def handle_call({:delete_volume, workload}, _from, state) do
    {reply, state} = do_delete_volume(state, workload)
    {:reply, reply, state}
  end

  # The async wake worker finished: complete the durable transition + publish,
  # then resolve every parked caller for the workload.
  @impl true
  def handle_info({:wake_done, workload, outcome}, state) do
    {:noreply, finish_wake(state, workload, outcome)}
  end

  # The wake-worker bound (Task 10) elapsed. If the wake for THIS workload is still in
  # flight (the worker never reported a {:wake_done}, the wedged-boot case), fail it:
  # finish_wake releases single-flight and errs the parked callers, leaving the banked
  # bundle re-wakeable (adoption heals a stranded :relighting mark back to :banked on
  # the next reconcile). A stale timer for a wake that already finished (or a newer
  # wake replaced it) is a no-op: `waking` no longer has the workload, so finish_wake
  # pops an empty waiter list and touches nothing.
  def handle_info({:wake_timeout, workload}, state) do
    if Map.has_key?(state.waking, workload) do
      Logger.warning("embervm stateful wake timed out at bound", workload: workload)
      {:noreply, finish_wake(state, workload, {:error, {:wake_timeout, workload}})}
    else
      {:noreply, state}
    end
  end

  def handle_info(:reconcile, state) do
    state = do_reconcile(state)
    schedule(:reconcile, state.reconcile_interval_ms)
    {:noreply, state}
  end

  # The sweeper resolved an interruptible-bank checkpoint (ADR embervm/008) and
  # tells us the outcome so any connection parked during the checkpointed window
  # (see park_during_checkpoint/4) is served.
  #
  #   :abort -> the sweeper resumed the SAME paused VM hot and republished it, so
  #     the instance is :serving with its endpoint intact. Reply every parked
  #     caller with that live endpoint directly (no wake needed) and clear the
  #     waiting list. Reading published_endpoint/2 is the same source a straggler
  #     resolves from, so the abort path replies exactly as a hot hit would.
  #   :commit -> the temp snapshot was published as the bundle and the VM
  #     destroyed, so the instance is :banked. Start a normal wake (start_wake/1),
  #     which plans a relight off the just-committed bundle and replies to the
  #     already-parked callers on completion via the existing finish_wake path.
  @impl true
  def handle_cast({:checkpoint_resolved, workload, :abort}, state) do
    case StatefulStore.published_endpoint(state.store, workload) do
      %{ip: ip, port: port} when is_binary(ip) and ip != "" and is_integer(port) ->
        {waiters, state} = pop_waiters(state, workload)
        {_trace, state} = pop_wake_trace(state, workload)
        reply_all(waiters, {:ok, %{ip: ip, port: port}})
        {:noreply, state}

      _ ->
        # The abort left no live endpoint (should not happen: an aborted
        # checkpoint republishes). Fall back to a fresh wake so the parked
        # callers are not stranded.
        {:noreply, resolve_parked_via_wake(state, workload)}
    end
  end

  def handle_cast({:checkpoint_resolved, workload, :commit}, state) do
    {:noreply, resolve_parked_via_wake(state, workload)}
  end

  def handle_cast(_msg, state), do: {:noreply, state}

  def handle_info(_msg, state), do: {:noreply, state}

  # -- wake handling -----------------------------------------------------------

  defp handle_wake(state, workload, principal, from) do
    # A straggler: the VM came up between the node Envoy's miss and this call
    # reaching us (a race with a just-published wake). Resolve to the live
    # endpoint directly, do NOT wake or error.
    case StatefulStore.published_endpoint(state.store, workload) do
      %{ip: ip, port: port} when is_binary(ip) and ip != "" and is_integer(port) ->
        {:reply, {:ok, %{ip: ip, port: port}}, state}

      _ ->
        handle_cold_wake(state, workload, principal, from)
    end
  end

  defp handle_cold_wake(state, workload, principal, from) do
    cond do
      not stateful_workload?(state, workload) ->
        {:reply, {:error, {:unknown_workload}}, state}

      # The workload's live instance is mid interruptible-bank checkpoint (ADR
      # embervm/008): the VM is PAUSED awaiting the sweeper's resolve, NOT gone.
      # Never cold-boot behind it (that would race a second live VM against the
      # singleton gate); PARK the caller and let the sweeper's
      # {:checkpoint_resolved, workload, outcome} cast serve it (hot on abort, or
      # a fresh relight wake on commit). Parking here reuses the same waking cap
      # and reply machinery as a normal wake, so the sweeper's resolve resolves
      # every parked caller in one shot. The first park also seeds the tracing
      # bundle so an eventual relight (on commit) still emits its wake spans.
      checkpointed?(state, workload) ->
        park_during_checkpoint(state, workload, principal, from)

      # Already a wake in flight for this workload: park behind it
      # (single-flight), subject to the parked-connection cap. Does NOT consult
      # the wake-rate limit (the wake was already counted by the first miss).
      Map.has_key?(state.waking, workload) ->
        park_behind_wake(state, workload, principal, from)

      # First miss: apply the per-workload wake-rate limit, then park + kick
      # ONE wake worker.
      wake_allowed?(state, principal) ->
        state = record_wake(state, principal)
        state = park_new_wake(state, workload, principal, from)
        {:noreply, start_wake(state, workload)}

      true ->
        audit_denial(state, principal, workload, :wake_rate)
        {:reply, {:error, {:wake_rate, "per-workload wake-rate limit exceeded"}}, state}
    end
  end

  # Whether the workload's single live instance is currently :checkpointed (a
  # paused interruptible-bank checkpoint awaiting resolve). A bounded store read;
  # the singleton invariant means at most one live instance per workload.
  defp checkpointed?(state, workload) do
    StatefulStore.list(state.store, workload)
    |> Enum.any?(&(&1.state == :checkpointed))
  end

  # Park a caller behind an in-flight checkpoint resolve. Respects park_cap
  # (audited denial + close on overflow), and seeds the tracing bundle on the
  # FIRST parker (so a commit -> relight still emits its wake spans) without
  # re-seeding a later one. Never kicks a wake worker: the sweeper's
  # {:checkpoint_resolved} cast is what resolves these callers.
  defp park_during_checkpoint(state, workload, principal, from) do
    waiters = Map.get(state.waking, workload, [])

    if length(waiters) >= state.park_cap do
      audit_denial(state, principal, workload, :park_full)
      log_park_full(state, workload, length(waiters))
      {:reply, {:error, {:park_full, "parked-connection cap exceeded for workload"}}, state}
    else
      state =
        if waiters == [] do
          # First parker: seed both the waiting list and a fresh tracing bundle
          # (park_start), exactly like park_new_wake but WITHOUT kicking a wake.
          %{
            state
            | waking: Map.put(state.waking, workload, [{from, principal}]),
              wake_traces: Map.put(state.wake_traces, workload, %{park_start: :opentelemetry.timestamp()})
          }
        else
          %{state | waking: Map.put(state.waking, workload, waiters ++ [{from, principal}])}
        end

      {:noreply, state}
    end
  end

  # Serve parked callers by kicking a normal wake (the commit path, and the
  # abort fallback when no live endpoint was found): only if callers are actually
  # parked, so a resolve with nothing waiting never boots spuriously. start_wake
  # plans the relight/cold off the resolved instance and finish_wake replies to
  # the parked callers already sitting in state.waking.
  defp resolve_parked_via_wake(state, workload) do
    if Map.has_key?(state.waking, workload) and Map.get(state.waking, workload) != [] do
      start_wake(state, workload)
    else
      state
    end
  end

  defp park_behind_wake(state, workload, principal, from) do
    waiters = Map.get(state.waking, workload, [])

    if length(waiters) >= state.park_cap do
      audit_denial(state, principal, workload, :park_full)
      log_park_full(state, workload, length(waiters))
      {:reply, {:error, {:park_full, "parked-connection cap exceeded for workload"}}, state}
    else
      state = %{state | waking: Map.put(state.waking, workload, waiters ++ [{from, principal}])}
      {:noreply, state}
    end
  end

  # Log a park overflow as STRUCTURED warning (Task 10) so the Task 11 alert can match
  # `park_full` with the oldest-waiter age: a park filling up means the in-flight wake
  # is not draining, the exact R5 symptom. The oldest-waiter age is how long the wake
  # this park sits behind has been in flight (mono now - wake_started); 0 when no start
  # was recorded (e.g. a park_during_checkpoint before a wake was armed). The alert is
  # NOT implemented here (PR-7); this only emits the signal.
  defp log_park_full(state, workload, depth) do
    Logger.warning("embervm stateful park_full",
      event: :park_full,
      workload: workload,
      park_depth: depth,
      oldest_waiter_age_ms: oldest_waiter_age_ms(state, workload)
    )
  end

  defp oldest_waiter_age_ms(state, workload) do
    case Map.get(state.wake_started, workload) do
      started when is_integer(started) -> max(state.mono_clock.() - started, 0)
      _ -> 0
    end
  end

  # First connection for a workload: seed its parked list (the cap is never
  # exceeded by the first entry) AND its tracing bundle (park_start), the
  # stateful counterpart of ServingManager.park_new_wake/4 minus the
  # traceparent (no HTTP request to carry one from).
  defp park_new_wake(state, workload, principal, from) do
    trace = %{park_start: :opentelemetry.timestamp()}

    %{
      state
      | waking: Map.put(state.waking, workload, [{from, principal}]),
        wake_traces: Map.put(state.wake_traces, workload, trace)
    }
  end

  # -- wake worker -------------------------------------------------------------

  # Kick ONE async wake for a workload. The relight-vs-cold DECISION and, for a
  # relight, the ETS `banked -> relighting` mark happen HERE on the serialized
  # manager process (a cheap pure placement read + one ETS mark), so the FSM
  # edge is taken in order and crash-consistently. The StartStateful RPC itself
  # runs in a spawned worker so a multi-second boot never head-of-line-blocks
  # another workload's wake; the worker reports the RPC result and finish_wake
  # completes the durable transition + publish on this process.
  defp start_wake(state, workload) do
    entry = catalog_entry(state, workload)

    # Stamp the wake phase's start + the cold bool into the tracing bundle now
    # (before the plan/RPC), so finish_wake can emit the `wake` child span with a
    # real duration. Mirrors ServingManager.start_wake/2's placement/wake stamps,
    # collapsed to one boundary here (plan_wake is a cheap pure ETS read, not
    # worth its own child span at this granularity).
    wake_start = :opentelemetry.timestamp()
    plan = plan_wake(state, workload)
    cold = match?({:cold, _, _, _, _}, plan) or match?({:restore_volume_then_cold, _, _, _}, plan)
    state = stamp_wake_trace(state, workload, %{wake_start: wake_start, cold: cold})

    # Generation blessing (R7, ADR embervm/011, standing decision 4): every plan
    # that will dispatch a WRITABLE attach (every arm except a bare {:error, _})
    # gets the next blessed generation issued and durably recorded HERE, on this
    # serialized process, BEFORE the boot request is built or dispatched. This is
    # the fence: bless_generation/3 appends the op-log entry first and only then
    # returns, so a crash between the append and the RPC leaves an unused (never
    # dispatched) blessed number, which is harmless; blessing AFTER dispatch would
    # instead risk a boot the op-log never witnessed, a real fence hole. A
    # bless_generation failure (an op-log append error) fails the wake outright
    # rather than dispatching an unblessed attach.
    case bless_wake_generation(state, workload, plan) do
      {:ok, blessed_generation} ->
        start_wake_dispatch(state, workload, entry, plan, blessed_generation)

      :none ->
        start_wake_dispatch(state, workload, entry, plan, 0)

      {:error, reason} ->
        send(self(), {:wake_done, workload, {:error, {:bless_generation, reason}}})
        state
    end
  end

  # Whether `plan` will dispatch a writable attach at all (every arm except
  # {:error, _}), and if so, issue + durably record the next blessed generation
  # for the workload via StatefulStore.bless_generation/3. Returns `:none` for a
  # plan that dispatches nothing (the {:error, _} arm: the wake already failed in
  # plan_wake/2, e.g. :volume_node_gone, so there is no attach to bless).
  defp bless_wake_generation(_state, _workload, {:error, _reason}), do: :none

  defp bless_wake_generation(state, workload, _plan) do
    next = StatefulStore.next_blessed_generation(state.store, workload)

    case StatefulStore.bless_generation(state.store, workload, next) do
      {:ok, _volume} -> {:ok, next}
      {:error, reason} -> {:error, reason}
    end
  end

  # Dispatches the wake worker for `plan`, threading `blessed_generation` (0 for
  # the {:error, _} arm, which never reaches a request builder) into every
  # StartStatefulRequest this wake sends.
  defp start_wake_dispatch(state, workload, entry, plan, blessed_generation) do
    owner = self()

    # Stamp the wake's start (for the adoption stuck-check + park_full age) and arm the
    # wake-worker bound: if the worker has not reported a {:wake_done} by then,
    # {:wake_timeout} fails the wake so single-flight releases (Task 10).
    schedule_wake_timeout(workload, wake_bound_ms(state, workload))
    state = %{state | wake_started: Map.put(state.wake_started, workload, state.mono_clock.())}

    case plan do
      {:relight, instance, node_id, snapshot_ref} ->
        case StatefulStore.mark(state.store, instance.instance_id, :relight) do
          {:ok, _} ->
            # boot_image_ref rides even a RELIGHT: if the daemon discovers the
            # generation pair is actually broken (a mismatch we could not see
            # from here, or an unreadable ledger) it falls back to a cold boot
            # from THIS ref rather than failing the call outright.
            fallback_ref = boot_image_ref(state, node_id, workload)
            req = relight_request(entry, snapshot_ref, fallback_ref, blessed_generation)
            spawn_wake(owner, workload, fn -> run_relight(state, instance, node_id, req) end)

          {:error, reason} ->
            # The instance moved off banked concurrently: report a wake failure
            # so the parked callers error and the next connection retries.
            send(self(), {:wake_done, workload, {:error, {:relight_mark, reason}}})
        end

      # Restore-on-miss (R6): the local bundle is gone but its store copy is
      # recoverable. Restore the STATEFUL bundle inside the wake worker FIRST (so
      # park/single-flight semantics are unchanged), then relight exactly as the
      # warm path. The restore failing (store unreachable mid-wake, or the copy
      # vanished) degrades to the daemon's cold-boot fallback via the boot_image_ref
      # that rides the relight request (fail-open warmth).
      {:restore_then_relight, instance, node_id, snapshot_ref} ->
        case StatefulStore.mark(state.store, instance.instance_id, :relight) do
          {:ok, _} ->
            fallback_ref = boot_image_ref(state, node_id, workload)
            req = relight_request(entry, snapshot_ref, fallback_ref, blessed_generation)

            spawn_wake(owner, workload, fn ->
              _ = restore_bundle(state, node_id, workload, snapshot_ref)
              run_relight(state, instance, node_id, req)
            end)

          {:error, reason} ->
            send(self(), {:wake_done, workload, {:error, {:relight_mark, reason}}})
        end

      {:cold, node_id, boot_ref, mode, reason} ->
        req = cold_request(state, entry, workload, boot_ref, mode, blessed_generation)
        spawn_wake(owner, workload, fn -> run_cold(state, workload, node_id, boot_ref, mode, reason, req) end)

      # Restore-on-miss (R6): the volume itself is gone but a (vol.img, gen) pair is
      # exported. Restore the VOLUME inside the wake worker FIRST, then cold-boot at
      # the restored generation (COLD mode: the volume already exists after the
      # restore, no create). A restore failure degrades to a plain cold boot, which
      # for a truly-absent volume the daemon fails closed on (data fails closed).
      {:restore_volume_then_cold, wl, node_id, volume} ->
        boot_ref = boot_image_ref(state, node_id, wl)
        req = cold_request(state, entry, wl, boot_ref, :cold, blessed_generation)
        reason = cold_reason(volume)

        spawn_wake(owner, workload, fn ->
          _ = restore_volume(state, node_id, wl, volume)
          run_cold(state, wl, node_id, boot_ref, :cold, reason, req)
        end)

      {:error, reason} ->
        send(self(), {:wake_done, workload, {:error, reason}})
    end

    state
  end

  # Spawn a wake worker that ALWAYS reports a {:wake_done} outcome, even if the
  # RPC body crashes: a worker that died without reporting would leave the
  # parked `:infinity` callers blocked forever.
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

  # -- wake-worker bound (Task 10) --------------------------------------------

  # The per-wake worker bound in ms: the explicit `wake_bound_ms` override (tests) or
  # the workload's wakeTimeoutSeconds + margin.
  defp wake_bound_ms(%{wake_bound_ms: ms}, _workload) when is_integer(ms) and ms > 0, do: ms

  defp wake_bound_ms(state, workload) do
    wake_timeout_seconds(state, workload) * 1_000 + state.wake_timeout_margin_ms
  end

  # The workload's wakeTimeoutSeconds from the catalog (stateful config), defaulting to
  # @default_wake_timeout_seconds when the entry carries none.
  defp wake_timeout_seconds(state, workload) do
    case WorkloadCatalog.fetch(state.catalog_table, workload) do
      {:ok, %{stateful: %{wake_timeout_seconds: secs}}} when is_integer(secs) and secs > 0 -> secs
      _ -> @default_wake_timeout_seconds
    end
  end

  defp schedule_wake_timeout(workload, bound_ms) when is_integer(bound_ms) and bound_ms > 0 do
    Process.send_after(self(), {:wake_timeout, workload}, bound_ms)
  end

  defp schedule_wake_timeout(_workload, _bound_ms), do: :ok

  # -- placement (relight vs cold, volume-anchored) ---------------------------

  # PURE (ETS reads only). A banked instance with a VALID pair relights; a
  # banked instance with a BROKEN pair is evicted first (so it never resurfaces
  # as a relight candidate) and the wake falls through to cold; no banked
  # instance at all goes straight to cold. Cold placement is ANCHORED to the
  # volume's node when a volume row already exists (decision 11: volume files
  # are node-local, never migrated); a workload with NO volume row yet is a true
  # first boot, free to place among any eligible node.
  defp plan_wake(state, workload) do
    volume = StatefulStore.get_volume(state.store, workload)

    cond do
      StatefulStore.quarantined?(state.store, workload) ->
        # R7, ADR embervm/011, standing decision 4: a node has reported a
        # generation past the last one this control plane blessed, with
        # generation_blessed: false. Fail closed -- park, never place a wake
        # (relight OR cold) against a volume whose current generation this
        # control plane cannot vouch for. No auto-heal: resolution (bless-and-
        # adopt, or discard) is a manual runbook decision.
        Logger.warning("embervm stateful wake refused: volume quarantined (unblessed generation)", workload: workload)
        {:error, :volume_quarantined}

      true ->
        plan_wake_unquarantined(state, workload, volume)
    end
  end

  defp plan_wake_unquarantined(state, workload, volume) do
    case banked_instance(state, workload) do
      nil ->
        # No bundle to resume: a genuine FRESH first boot (no volume yet, reason
        # nil -> stateful_started) or a COLD boot against an existing volume whose
        # bundle is gone (reason :no_bundle -> stateful_cold_booted{no_bundle}). If
        # the volume ITSELF is missing locally but a (vol.img, gen) pair is exported
        # (exported_generation > 0) and the store is reachable, restore the volume
        # first, then cold-boot at the restored generation (R6 restore-on-miss).
        restore_volume_or_cold(state, workload, volume)

      instance ->
        if StatefulStore.pair_valid?(state.store, workload) do
          case anchor_node(state, volume) do
            {:ok, node_id} ->
              # Local banked bundle present on the anchor node -> relight it (the
              # existing warm path). Local bundle GONE from disk (a TRUE local miss)
              # AND the anchor node's store is reachable -> attempt an OPTIMISTIC
              # restore first, then relight (R6 restore-on-miss, option b). The CP
              # does not track remote inventory: it attempts the restore whenever the
              # local copy is missing and the store is reachable, and the daemon fails
              # closed (FAILED_PRECONDITION) if no store copy exists, which degrades
              # to the relight's own cold-boot fallback. store_reachable == false
              # never blocks the wake, it only withholds the restore (fail-open
              # warmth, standing decision 7). Generation pairing is unchanged: a
              # restored bundle still only relights against its matching volume
              # generation, enforced by the daemon at RELIGHT time.
              if bundle_local?(state, node_id, instance.snapshot_ref) or
                   not store_reachable?(state, node_id) do
                {:relight, instance, node_id, instance.snapshot_ref}
              else
                {:restore_then_relight, instance, node_id, instance.snapshot_ref}
              end

            {:error, reason} ->
              {:error, reason}
          end
        else
          # Broken pair: evict the stale bundle through the durable path (so a
          # rebuild agrees it is gone) and fall through to a cold boot, carrying the
          # mismatch reason so the wake records stateful_cold_booted{generation_mismatch}
          # (gate 2: the op-log alone tells the whole story).
          _ =
            StatefulStore.transition(
              state.store,
              instance.instance_id,
              :evict,
              :stateful_evicted,
              %{reason: "pair_broken"},
              %{}
            )

          cold_plan(state, workload, volume, :generation_mismatch)
        end
    end
  end

  # The discarded-warmth reason for a cold boot with no banked instance: nil (a
  # genuine FRESH first boot, no volume yet) vs :no_bundle (a volume exists but no
  # bundle to resume, e.g. after a forced roll).
  defp cold_reason(nil), do: nil
  defp cold_reason(_volume), do: :no_bundle

  # -- restore-on-miss (R6, Task 8) -------------------------------------------

  # No banked bundle to resume. If the VOLUME itself is missing locally but a
  # (vol.img, gen) pair is exported and the store is reachable, restore the volume
  # first then cold-boot at the restored generation; otherwise the ordinary cold
  # plan (FRESH first boot or COLD against an existing volume). A missing volume
  # ROW whose exported_generation is unknown here (no fact at all) cannot restore,
  # so it stays a fresh boot: exported_generation only survives as a node fact while
  # the volume is at least reported, which is exactly the "disk present, bundle
  # lost" case restore-on-miss targets.
  defp restore_volume_or_cold(state, workload, volume) do
    cond do
      volume_restorable?(state, volume) ->
        # The anchor node still reports the volume row (so exported_generation is
        # known) but the local pair is unusable; restore the exported volume pair,
        # then cold-boot at that generation. anchor_node guards the node is present.
        case anchor_node(state, volume) do
          {:ok, node_id} -> {:restore_volume_then_cold, workload, node_id, volume}
          {:error, reason} -> {:error, reason}
        end

      true ->
        cold_plan(state, workload, volume, cold_reason(volume))
    end
  end

  # Whether the anchor node still reports the instance's bundle on LOCAL disk (its
  # snapshot_ref is in the node's stateful_bundles). A relight needs no restore when
  # true; a true local miss (false) may consult the store.
  defp bundle_local?(state, node_id, snapshot_ref) when is_binary(snapshot_ref) and snapshot_ref != "" do
    case NodeCapacity.fetch(state.capacity_table, node_id) do
      {:ok, fact} ->
        fact
        |> Map.get(:stateful_bundles, [])
        |> Enum.any?(&(&1.snapshot_ref == snapshot_ref))

      :error ->
        false
    end
  end

  defp bundle_local?(_state, _node_id, _ref), do: false

  # Whether a missing/broken-pair volume can be restored: the store is reachable
  # and a (vol.img, gen) pair is exported (exported_generation > 0). The node the
  # volume is anchored to owns the store reachability verdict.
  defp volume_restorable?(_state, nil), do: false

  defp volume_restorable?(state, %{node_id: node_id} = volume) when is_binary(node_id) do
    store_reachable?(state, node_id) and exported_generation(volume) > 0
  end

  defp volume_restorable?(_state, _volume), do: false

  defp exported_generation(nil), do: 0
  defp exported_generation(volume), do: Map.get(volume, :exported_generation, 0) || 0

  # The anchor node's latest object-store reachability verdict (R6). Absent/false
  # (a node with no store configured, or one that never reported) reads as NOT
  # reachable, so no restore is attempted and the wake degrades to cold (never
  # blocked: this only gates the store consultation, never a local-state wake).
  defp store_reachable?(state, node_id) do
    case NodeCapacity.fetch(state.capacity_table, node_id) do
      {:ok, fact} -> Map.get(fact, :store_reachable, false) == true
      :error -> false
    end
  end

  # Cold placement: FRESH (no volume row exists yet, the daemon must create it)
  # when `volume` is nil; COLD (an existing volume, no bundle to resume) otherwise.
  # `reason` is the discarded-warmth reason threaded into the boot op (nil for a
  # fresh first boot). Both modes are anchored to the resolved node.
  defp cold_plan(state, workload, nil, _reason) do
    case eligible_node_for_fresh(state, workload) do
      {:ok, node_id} -> {:cold, node_id, boot_image_ref(state, node_id, workload), :fresh, nil}
      {:error, reason} -> {:error, reason}
    end
  end

  defp cold_plan(state, workload, volume, reason) do
    case anchor_node(state, volume) do
      {:ok, node_id} -> {:cold, node_id, boot_image_ref(state, node_id, workload), :cold, reason}
      {:error, anchor_error} -> {:error, anchor_error}
    end
  end

  # The volume's anchor node MUST still be reporting (fail-closed: never
  # silently place a writable attach on a different node than the one holding
  # the volume file). A gone node is `{:error, :volume_node_gone}`, surfaced to
  # the caller so the activator closes the connection loudly rather than
  # hanging.
  defp anchor_node(_state, %{node_id: node_id}) when not is_binary(node_id) or node_id == "" do
    {:error, :volume_node_missing}
  end

  defp anchor_node(state, %{node_id: node_id}) do
    case NodeCapacity.fetch(state.capacity_table, node_id) do
      {:ok, _fact} -> {:ok, node_id}
      :error -> {:error, :volume_node_gone}
    end
  end

  defp anchor_node(_state, nil), do: {:error, :volume_node_missing}

  # A genuine first boot (no volume row anywhere): any node reporting a
  # stateful-capable subnet AND live-VM budget is eligible. Rendezvous-hashed on
  # the workload, mirroring ServingPlacement.node_for_create/2; with one node
  # this is trivially that node.
  defp eligible_node_for_fresh(state, workload) do
    NodeCapacity.all(state.capacity_table)
    |> Enum.filter(&stateful_capable?/1)
    |> Enum.filter(&has_budget?/1)
    |> rendezvous_pick(workload)
    |> case do
      nil -> {:error, :no_capacity}
      fact -> {:ok, fact.configured_id}
    end
  end

  defp stateful_capable?(fact) do
    cidr = Map.get(fact, :serving_subnet_cidr)
    is_binary(cidr) and cidr != ""
  end

  defp has_budget?(fact) do
    max = Map.get(fact, :max_live_vms, 0)
    max > 0 and Map.get(fact, :live_vms, 0) < max
  end

  defp rendezvous_pick([], _key), do: nil
  defp rendezvous_pick(facts, key), do: Enum.max_by(facts, fn fact -> :erlang.phash2({key, fact.configured_id}, 4_294_967_296) end)

  # The cold-boot source: the node's base SNAPSHOT ref for the workload. A
  # stateful workload is an image-lane, opaque-L4 guest (e.g. Postgres): its base
  # is built by the same BaseBuilder path but produces NO serving handler artifact
  # (that is a zip-serving-only mechanism, D-R3.11.2), so serving_image_ref is
  # always empty here. The daemon cold-boots the rootfs behind the base snapshot
  # key instead (resolving it via its base registry, not the serving-image
  # inventory). Empty when the node has not built the base yet; the daemon then
  # fails the RPC loudly rather than booting an empty rootfs.
  defp boot_image_ref(state, node_id, workload) do
    case NodeCapacity.fetch(state.capacity_table, node_id) do
      {:ok, fact} ->
        fact
        |> Map.get(:workloads, %{})
        |> Map.get(workload, %{})
        |> Map.get(:snapshot_ref)

      :error ->
        nil
    end
  end

  defp banked_instance(state, workload) do
    StatefulStore.list(state.store, workload)
    |> Enum.find(&(&1.state == :banked))
  end

  # -- request builders --------------------------------------------------------

  # RELIGHT never carries mmds_env (R4, D-R4.PR-7.1): a relight resumes the
  # running VM from its memory snapshot, so the kernel never re-inits and the
  # boot-args carrying a first-boot secret are never read again. The secret was
  # already consumed at the ORIGINAL FRESH/COLD boot and baked into the
  # volume's initialized data (e.g. Postgres's initdb password). mmds_env is
  # therefore unconditionally %{} here, with no secret read at all -- not even
  # a wasted one -- keeping a relight's K8s footprint identical to before this
  # feature.
  defp relight_request(entry, snapshot_ref, fallback_ref, blessed_generation) do
    cfg = stateful_cfg(entry)

    %StartStatefulRequest{
      trace: %Trace{workload: Map.get(entry, :name, "")},
      mode: :START_STATEFUL_MODE_RELIGHT,
      boot_image_ref: fallback_ref || "",
      relight_snapshot_ref: snapshot_ref,
      port: cfg.port,
      volume_size_bytes: gib_to_bytes(cfg.volume_size_gib),
      volume_mount: cfg.volume_mount_path,
      create_if_missing: false,
      resources: resource_spec(entry),
      mmds_env: %{},
      # blessed_generation (R7, ADR embervm/011): the generation this control
      # plane already durably blessed for this attach in bless_wake_generation/3,
      # BEFORE this request was built. The daemon records it verbatim rather than
      # self-bumping.
      blessed_generation: blessed_generation
    }
  end

  # FRESH/COLD only: when the workload's catalog stateful config carries a
  # secretRef, read it (via state.get_secret_fun) and populate mmds_env from
  # its decoded key/values (R4, D-R4.PR-7.1: MMDS-lite over boot-args). Absent
  # secretRef leaves mmds_env at %{} (no K8s call at all, matching the
  # zero-new-runtime-RBAC baseline for a workload that declares none).
  defp cold_request(state, entry, workload, boot_ref, mode, blessed_generation) do
    cfg = stateful_cfg(entry)

    %StartStatefulRequest{
      trace: %Trace{workload: workload},
      mode: start_mode(mode),
      boot_image_ref: boot_ref || "",
      relight_snapshot_ref: "",
      port: cfg.port,
      volume_size_bytes: gib_to_bytes(cfg.volume_size_gib),
      volume_mount: cfg.volume_mount_path,
      # FRESH creates the volume if it is absent (the true first boot); COLD
      # never creates (a volume row already exists, per plan_wake/2's cold_plan
      # branch selection).
      create_if_missing: mode == :fresh,
      resources: resource_spec(entry),
      mmds_env: cold_boot_mmds_env(state, entry, workload),
      # blessed_generation (R7, ADR embervm/011): see relight_request/4's note;
      # identical contract for a FRESH/COLD writable attach.
      blessed_generation: blessed_generation
    }
  end

  # Fail-open on a secret-read failure (DECISION, D-R4.PR-7.1): a scratch-tier
  # stateful workload with an unreadable secretRef proceeds to boot with an
  # EMPTY mmds_env rather than failing the whole wake. The guest's own
  # readiness probe (waitStatefulReady's TCP CONNECT) is the loud failure
  # surface: a Postgres image that requires POSTGRES_PASSWORD and does not get
  # it will fail to start and never open its listen port, so the wake times out
  # and the caller sees {:wake_failed, ...} -- the same operator-visible signal
  # a hard secret-read failure would have produced, just one hop later and with
  # a clearer "guest never came up" symptom than an opaque K8s error would have
  # been. Chosen over fail-closed because a K8s API blip (a transient 5xx, a
  # slow apiserver) must not take down an otherwise-healthy scratch datastore
  # wake; the RELIGHT path (the common case once seeded) never reads the secret
  # at all, so this only matters on the rare first-boot/cold-boot-after-evict
  # path.
  defp cold_boot_mmds_env(_state, %{stateful: %{secret_ref: nil}}, _workload), do: %{}
  defp cold_boot_mmds_env(_state, %{stateful: %{secret_ref: ""}}, _workload), do: %{}

  defp cold_boot_mmds_env(state, %{namespace: namespace, stateful: %{secret_ref: secret_ref}}, workload)
       when is_binary(secret_ref) do
    case state.get_secret_fun.(namespace, secret_ref) do
      {:ok, data} ->
        data

      {:error, reason} ->
        Logger.warning(
          "embervm stateful: secretRef read failed, proceeding with empty mmds_env (fail-open)",
          workload: workload,
          namespace: namespace,
          secret_ref: secret_ref,
          reason: inspect(reason)
        )

        %{}
    end
  end

  defp cold_boot_mmds_env(_state, _entry, _workload), do: %{}

  defp start_mode(:fresh), do: :START_STATEFUL_MODE_FRESH
  defp start_mode(:cold), do: :START_STATEFUL_MODE_COLD

  defp resource_spec(entry) do
    resources = Map.get(entry, :resources, %{})
    %ResourceSpec{vcpus: Map.get(resources, :vcpus, 1), mem_mib: Map.get(resources, :mem_mib, 512)}
  end

  defp gib_to_bytes(nil), do: 0
  defp gib_to_bytes(gib) when is_integer(gib), do: gib * 1024 * 1024 * 1024

  # -- wake RPC workers ---------------------------------------------------------

  defp run_relight(state, instance, node_id, req) do
    case safe_start_stateful(state, node_id, req) do
      {:ok, %StartStatefulResponse{vm_id: vm_id, ip: ip, port: port, generation: generation, was_relight: true}}
      when is_binary(vm_id) and vm_id != "" ->
        {:relit, instance.instance_id, node_id, %{vm_id: vm_id, ip: ip, port: port, generation: generation}}

      # A RELIGHT call that fell back to a cold boot on the daemon side
      # (generation mismatch discovered only at the daemon, or an unreadable
      # ledger): treat it as a fresh instance, not a relit one, and let the
      # ORIGINAL banked row be evicted (it never resumed). was_relight=false on
      # a RELIGHT-mode response is exactly that fallback signal.
      {:ok, %StartStatefulResponse{vm_id: vm_id, ip: ip, port: port, generation: generation, was_relight: false, cold_boot_reason: reason}}
      when is_binary(vm_id) and vm_id != "" ->
        {:relight_fell_back, instance.instance_id, node_id, %{vm_id: vm_id, ip: ip, port: port, generation: generation}, reason}

      other ->
        {:error, {:relight_failed, instance.instance_id, other}}
    end
  end

  defp run_cold(state, workload, node_id, boot_ref, mode, reason, req) do
    case safe_start_stateful(state, node_id, req) do
      {:ok, %StartStatefulResponse{vm_id: vm_id, ip: ip, port: port, generation: generation}}
      when is_binary(vm_id) and vm_id != "" ->
        attrs = %{
          tenant: state.tenant,
          principal: wake_principal(workload),
          workload: workload,
          node_id: node_id,
          generation: generation,
          # The volume the daemon reported it created/attached at (size the request
          # asked for); FRESH boots record a volume_created off this (see finish_created).
          volume_size_bytes: cold_volume_size_bytes(state, workload)
        }

        {:created, attrs, %{vm_id: vm_id, ip: ip, port: port}, mode, boot_ref, reason}

      other ->
        {:error, {:start_failed, other}}
    end
  end

  # The declared volume size (bytes) for a workload, from its catalog stateful
  # config, recorded on the volume_created op a FRESH boot emits.
  defp cold_volume_size_bytes(state, workload) do
    case catalog_entry(state, workload) do
      %{stateful: %{volume_size_gib: gib}} when is_integer(gib) -> gib * 1024 * 1024 * 1024
      _ -> nil
    end
  end

  # -- finish wake (serialized, durable) ---------------------------------------

  defp finish_wake(state, workload, outcome) do
    {waiters, state} = pop_waiters(state, workload)
    {trace, state} = pop_wake_trace(state, workload)

    # The wake RPC returned now: this closes the `wake` phase. Emit the
    # retroactive `park`/`wake` root spans (Task 10) with explicit start_times
    # (the session queue_wait / serving wake idiom): they spanned the async wake
    # worker and cannot wrap live code on this serialized process. `wake` carries
    # ember.wake_ms/ember.cold, the numbers the Task 12 wake-p95 + cold-vs-warm
    # gate reads. relight/cold_boot_reason are folded in by the caller once the
    # outcome is known (finish_created/finish_relit/finish_relight_fallback).
    # The `publish` child span is opened LIVE inside publish_and_resolve. Tracing
    # off (no OTel exporter) is a clean no-op throughout.
    wake_ended = :opentelemetry.timestamp()

    case outcome do
      {:created, attrs, endpoint, mode, _boot_ref, reason} ->
        emit_wake_phase_spans(trace, workload, wake_ended, false, cold_boot_reason_string(reason))
        finish_created(state, workload, attrs, endpoint, mode, reason, waiters)

      {:relit, instance_id, node_id, endpoint} ->
        emit_wake_phase_spans(trace, workload, wake_ended, true, "")
        finish_relit(state, workload, instance_id, node_id, endpoint, waiters)

      {:relight_fell_back, instance_id, node_id, endpoint, reason} ->
        emit_wake_phase_spans(trace, workload, wake_ended, false, cold_boot_reason_string(reason))
        finish_relight_fallback(state, workload, instance_id, node_id, endpoint, reason, waiters)

      other ->
        emit_wake_phase_spans(trace, workload, wake_ended, false, "")
        finish_wake_failure(state, workload, other, waiters)
    end
  end

  # The wire cold_boot_reason as a plain string attribute (empty when there was
  # none, e.g. a genuine fresh first boot): a nil value must never reach a span
  # attribute (the OTel Elixir SDK expects a concrete type per key).
  defp cold_boot_reason_string(nil), do: ""
  defp cold_boot_reason_string(reason) when is_atom(reason), do: Atom.to_string(reason)
  defp cold_boot_reason_string(reason) when is_binary(reason), do: reason
  defp cold_boot_reason_string(reason), do: inspect(reason)

  defp finish_wake_failure(state, workload, outcome, waiters) do
    case outcome do
      {:error, {:relight_failed, instance_id, reason}} ->
        Logger.warning("embervm stateful relight failed", workload: workload, instance_id: instance_id, reason: inspect(reason))
        _ = StatefulStore.mark(state.store, instance_id, :relight_abort)
        reply_all(waiters, {:error, {:wake_failed, reason}})
        state

      {:error, reason} ->
        Logger.warning("embervm stateful wake failed", workload: workload, reason: inspect(reason))
        reply_all(waiters, {:error, {:wake_failed, reason}})
        state
    end
  end

  defp finish_created(state, workload, attrs, endpoint, mode, reason, waiters) do
    instance_id = mint_id(state)

    attrs =
      attrs
      |> Map.put(:instance_id, instance_id)
      |> Map.put(:vm_id, endpoint.vm_id)
      |> Map.put(:reason, reason)

    # A FRESH boot is the first time the daemon created the workload's volume file:
    # record volume_created so the durable volumes projection has a row (a boot op's
    # bump_volume_generation only UPDATEs an existing row, so without this the
    # volumes table would stay empty). COLD boots reuse an existing volume.
    if mode == :fresh do
      _ =
        StatefulStore.create_volume(state.store, workload, %{
          node_id: Map.get(attrs, :node_id),
          generation: Map.get(attrs, :generation, 0),
          size_bytes: Map.get(attrs, :volume_size_bytes),
          allocated_bytes: 0
        })
    end

    # A discarded-warmth cold boot records stateful_cold_booted{reason} (gate 2); a
    # genuine FRESH first boot records stateful_started.
    record =
      if is_nil(reason) do
        StatefulStore.start(state.store, attrs)
      else
        StatefulStore.cold_boot(state.store, attrs)
      end

    audit = if is_nil(reason), do: :started, else: :cold_booted

    case record do
      {:ok, _instance} ->
        # The {:created} endpoint carries only {vm_id, ip, port}; surface the boot
        # generation (from attrs) on the reply so it matches the relit/fallback paths.
        endpoint = Map.put(endpoint, :generation, Map.get(attrs, :generation, 0))
        publish_and_resolve(state, instance_id, workload, endpoint, audit, waiters)

      {:error, store_reason} ->
        Logger.error("embervm stateful: start record failed", workload: workload, reason: inspect(store_reason))
        reply_all(waiters, {:error, {:wake_failed, {:store, store_reason}}})
        state
    end
  end

  defp finish_relit(state, workload, instance_id, node_id, endpoint, waiters) do
    relit =
      StatefulStore.transition(
        state.store,
        instance_id,
        :relight_ready,
        :stateful_relit,
        %{node_id: node_id, vm_id: endpoint.vm_id, generation: endpoint.generation},
        %{node_id: node_id, vm_id: endpoint.vm_id, generation: endpoint.generation}
      )

    case relit do
      {:ok, _} ->
        publish_and_resolve(state, instance_id, workload, endpoint, :relit, waiters)

      {:error, reason} ->
        Logger.warning("embervm stateful: relit transition failed", instance_id: instance_id, reason: inspect(reason))
        reply_all(waiters, {:error, {:wake_failed, {:relit, reason}}})
        state
    end
  end

  # The daemon itself fell back RELIGHT -> cold at StartStateful time (a generation
  # mismatch or unreadable ledger it discovered that we could not see from here):
  # the ORIGINAL banked instance never resumed. It is currently in :relighting (the
  # wake marked it so when it planned the relight), and :evict is legal ONLY from
  # :banked, so we first abort the relight (:relight_abort is ETS-only, back to
  # :banked) THEN evict (the legal banked->evicted durable edge). Both MUST land
  # before the new instance's boot, or the singleton gate would see the stranded
  # :relighting instance as live and wedge the workload forever. The new instance
  # is recorded as a stateful_cold_booted carrying the wire cold_boot_reason (gate
  # 2: the op-log alone reconstructs the discarded warmth).
  defp finish_relight_fallback(state, workload, old_instance_id, node_id, endpoint, reason, waiters) do
    _ = StatefulStore.mark(state.store, old_instance_id, :relight_abort)

    _ =
      StatefulStore.transition(
        state.store,
        old_instance_id,
        :evict,
        :stateful_evicted,
        %{reason: "pair_broken"},
        %{}
      )

    instance_id = mint_id(state)

    attrs = %{
      instance_id: instance_id,
      tenant: state.tenant,
      principal: wake_principal(workload),
      workload: workload,
      node_id: node_id,
      vm_id: endpoint.vm_id,
      generation: endpoint.generation,
      reason: reason
    }

    case StatefulStore.cold_boot(state.store, attrs) do
      {:ok, _instance} ->
        publish_and_resolve(state, instance_id, workload, endpoint, :cold_booted, waiters)

      {:error, store_reason} ->
        Logger.error("embervm stateful: relight-fallback cold_boot record failed", workload: workload, reason: inspect(store_reason))
        reply_all(waiters, {:error, {:wake_failed, {:store, store_reason}}})
        state
    end
  end

  # endpoint.generation rides the success reply (additive keys past {ip, port}):
  # ember.generation on the wake span AND the volume generation the caller booted
  # against are the same number, so surfacing it here means the reply itself is
  # gate-derivable without a second store read.
  defp publish_and_resolve(state, instance_id, workload, endpoint, reason, waiters) do
    Tracer.with_span "embervm.stateful.publish",
                     %{attributes: %{"ember.workload" => workload, "ember.instance_id" => instance_id}} do
      case StatefulStore.publish(state.store, instance_id, endpoint.ip, endpoint.port, reason) do
        {:ok, _} ->
          EndpointPublisher.publish(state.publisher)

          Logger.info("embervm stateful woken", workload: workload, instance_id: instance_id, reason: reason)

          reply_all(waiters, {:ok, %{ip: endpoint.ip, port: endpoint.port, generation: Map.get(endpoint, :generation, 0)}})
          state

        {:error, publish_reason} ->
          Logger.error("embervm stateful: publish failed", instance_id: instance_id, reason: inspect(publish_reason))
          reply_all(waiters, {:error, {:wake_failed, {:publish, publish_reason}}})
          state
      end
    end
  end

  defp pop_waiters(state, workload) do
    waiters = Map.get(state.waking, workload, [])
    {waiters, %{state | waking: Map.delete(state.waking, workload), wake_started: Map.delete(state.wake_started, workload)}}
  end

  defp reply_all(waiters, reply) do
    for {from, _principal} <- waiters, do: GenServer.reply(from, reply)
    :ok
  end

  # -- wake tracing (Task 10) --------------------------------------------------

  # Merge boundary stamps into a workload's tracing bundle. A no-op when the
  # workload never seeded a bundle (should not happen on the wake path, since
  # park_new_wake always seeds one before start_wake runs, but guarded exactly
  # like ServingManager.stamp_wake_trace/3 for symmetry).
  defp stamp_wake_trace(state, workload, fields) do
    case Map.get(state.wake_traces, workload) do
      nil -> state
      trace -> %{state | wake_traces: Map.put(state.wake_traces, workload, Map.merge(trace, fields))}
    end
  end

  defp pop_wake_trace(state, workload) do
    {Map.get(state.wake_traces, workload), %{state | wake_traces: Map.delete(state.wake_traces, workload)}}
  end

  # Emit the retroactive `park`/`wake` ROOT spans (Task 10) with explicit
  # start_times reconstructing phases that spanned another process (the
  # session queue_wait / serving wake idiom). UNLIKE serving these are roots,
  # not children of a restored remote parent: a raw TCP accept carries no W3C
  # traceparent to nest under. A nil bundle (should not happen; guarded anyway)
  # is a clean no-op.
  defp emit_wake_phase_spans(nil, _workload, _wake_ended, _relight, _cold_boot_reason), do: :ok

  defp emit_wake_phase_spans(trace, workload, wake_ended, relight, cold_boot_reason) do
    attrs = %{"ember.workload" => workload}

    if is_integer(trace[:park_start]) do
      # park: the first connection parked here until the wake began.
      park_stop = trace[:wake_start] || wake_ended
      emit_phase_span("embervm.stateful.park", trace.park_start, park_stop, attrs)
    end

    if is_integer(trace[:wake_start]) do
      # wake: the StartStateful RPC (relight, cold, or a relight-fell-back-to-cold).
      # Carries the gate numbers the Task 12 wake-p95 + relight-vs-cold-boot gate
      # reads: ember.wake_ms (park-to-first-response), ember.relight (true only for
      # a clean relight; a fallback cold boot is NOT a relight), and
      # ember.cold_boot_reason (empty unless a cold-boot fallback discarded warmth).
      wake_attrs =
        Map.merge(attrs, %{
          "ember.cold" => Map.get(trace, :cold, false),
          "ember.wake_ms" => span_ms(trace.wake_start, wake_ended),
          "ember.relight" => relight,
          "ember.cold_boot_reason" => cold_boot_reason
        })

      emit_phase_span("embervm.stateful.wake", trace.wake_start, wake_ended, wake_attrs)
    end

    :ok
  end

  # A retroactive completed span: opened with an explicit start_time and closed
  # immediately (its wall duration is start_time..now). Mirrors
  # ServingManager.emit_phase_span/4. Skipped if either boundary is missing.
  defp emit_phase_span(_name, nil, _stop, _attrs), do: :ok
  defp emit_phase_span(_name, _start, nil, _attrs), do: :ok

  defp emit_phase_span(name, start_time, _stop, attrs) do
    Tracer.with_span name, %{start_time: start_time, attributes: attrs} do
      :ok
    end
  end

  # :opentelemetry.timestamp/0 returns erlang NATIVE time units (what the span
  # start_time option expects). The *_ms gate attributes want milliseconds, so
  # convert the native delta (never assume nanoseconds). Mirrors
  # ServingManager.span_ms/2.
  defp span_ms(start_native, stop_native) when is_integer(start_native) and is_integer(stop_native) do
    System.convert_time_unit(stop_native - start_native, :native, :millisecond)
  end

  defp span_ms(_, _), do: 0

  # -- destroy (management verb) -----------------------------------------------

  # DELETE /v1/stateful/:name/instance: destroy the live instance AND evict the
  # banked bundle, so the next connection cold-boots the current image against
  # the still-intact volume (the volume itself is untouched; deletion is only
  # ever the separate explicit DELETE /volume act). No drain wait: an operator
  # override, mirroring ServingSweeper.force_roll's forced-destroy semantics
  # (accepts the in-flight drop).
  defp do_destroy_instance(state, workload) do
    # The `forced_roll` span (Task 10): a ROOT span around the whole operator-
    # override destroy (no caller trace, timer/API-driven, mirrors
    # ServingSweeper.force_roll's span shape). Bounds the StopStateful(DESTROY)
    # RPC + the durable transitions so a slow or stuck forced destroy is visible.
    Tracer.with_span "embervm.stateful.forced_roll", %{attributes: %{"ember.workload" => workload}} do
      instances = StatefulStore.list(state.store, workload)

      {state, destroyed, evicted} =
        Enum.reduce(instances, {state, 0, 0}, fn instance, {acc, d, e} ->
          cond do
            StatefulState.terminal?(instance.state) ->
              {acc, d, e}

            instance.state == :banked ->
              {evict_banked(acc, instance), d, e + 1}

            true ->
              {force_destroy_live(acc, instance), d + 1, e}
          end
        end)

      EndpointPublisher.publish(state.publisher)
      Logger.info("embervm stateful: instance destroyed", workload: workload, destroyed: destroyed, evicted: evicted)
      {%{destroyed: destroyed, evicted: evicted}, state}
    end
  end

  defp force_destroy_live(state, instance) do
    _ = stop_stateful_destroy(state, instance)

    _ =
      StatefulStore.transition(
        state.store,
        instance.instance_id,
        :destroy,
        :stateful_destroyed,
        %{reason: "forced_destroy"},
        %{}
      )

    state
  end

  defp evict_banked(state, instance) do
    _ =
      StatefulStore.transition(
        state.store,
        instance.instance_id,
        :evict,
        :stateful_evicted,
        %{reason: "forced_destroy"},
        %{}
      )

    state
  end

  defp stop_stateful_destroy(state, %{node_id: node_id, vm_id: vm_id}) when is_binary(node_id) and is_binary(vm_id) do
    req = %StopStatefulRequest{trace: %Trace{}, vm_id: vm_id, mode: :STOP_STATEFUL_MODE_DESTROY}

    with {:ok, channel} <- safe_channel(state.channel_fun, node_id) do
      try do
        state.stop_stateful_fun.(channel, req)
      rescue
        _ -> :error
      catch
        # Dead Mint ConnectionProcess: invalidate so the next verb re-dials
        # (best-effort verb, but leaving the corpse cached wedges later wakes).
        :exit, _ ->
          _ = state.invalidate_fun.(node_id, channel)
          :error

        _, _ ->
          :error
      end
    end

    :ok
  end

  defp stop_stateful_destroy(_state, _instance), do: :ok

  # -- delete volume (management verb) -----------------------------------------

  # DELETE /v1/stateful/:name/volume: refused while ANY non-terminal instance
  # exists (live or banked; see the doc). On a clean workload, calls the
  # daemon's DeleteVolume (best-effort; the durable volume_deleted append lands
  # regardless, exactly the destroy_instance pattern of "the record is
  # authoritative even if the RPC silently no-ops on an already-gone file").
  defp do_delete_volume(state, workload) do
    instances = StatefulStore.list(state.store, workload)

    if Enum.any?(instances, &(not StatefulState.terminal?(&1.state))) do
      {{:error, :instance_exists}, state}
    else
      volume = StatefulStore.get_volume(state.store, workload)
      _ = safe_delete_volume(state, volume, workload)
      # R6, Task 9: the store copy of the volume follows the local deletion. Safe
      # without a further pairing guard: delete was refused above while any
      # non-terminal instance existed, so no banked bundle still pairs with this
      # volume's generation (standing decision 8). Best-effort.
      _ = evict_remote_volume(state, volume, workload)

      case StatefulStore.delete_volume(state.store, workload) do
        :ok ->
          Logger.info("embervm stateful: volume deleted", workload: workload)
          {{:ok, %{deleted: true}}, state}

        {:error, reason} ->
          Logger.error("embervm stateful: volume_deleted append failed", workload: workload, reason: inspect(reason))
          {{:error, {:store, reason}}, state}
      end
    end
  end

  defp safe_delete_volume(state, %{node_id: node_id}, workload) when is_binary(node_id) do
    req = %DeleteVolumeRequest{trace: %Trace{workload: workload}, workload: workload}

    with {:ok, channel} <- safe_channel(state.channel_fun, node_id) do
      try do
        state.delete_volume_fun.(channel, req)
      rescue
        _ -> :error
      catch
        # Same dead-ConnectionProcess invalidation as stop_stateful_destroy.
        :exit, _ ->
          _ = state.invalidate_fun.(node_id, channel)
          :error

        _, _ ->
          :error
      end
    end

    :ok
  end

  defp safe_delete_volume(_state, _volume, _workload), do: :ok

  # -- adoption ------------------------------------------------------------

  # Reconcile the StatefulStore projection against every node's reported
  # stateful inventory, refresh volume facts, eager-evict any pair the reconcile
  # itself just broke, then re-derive + re-push. Mirrors
  # ServingManager.do_reconcile/1; see the moduledoc's adoption section.
  defp do_reconcile(state) do
    facts = NodeCapacity.all(state.capacity_table)
    live_vms = index_stateful_vms(facts)
    bundles = index_stateful_bundles(facts)

    state =
      StatefulStore.all(state.store)
      |> Enum.reject(&StatefulState.terminal?(&1.state))
      |> Enum.reduce(state, fn instance, acc -> adopt_one(acc, instance, live_vms, bundles) end)

    state = refresh_volume_facts(state, facts)

    _ = StatefulStore.eager_evict_broken_pairs(state.store)

    EndpointPublisher.publish(state.publisher)
    state
  end

  defp index_stateful_vms(facts) do
    for f <- facts, v <- Map.get(f, :stateful_vms, []) || [], into: %{} do
      {v.vm_id, {f.configured_id, v}}
    end
  end

  defp index_stateful_bundles(facts) do
    for f <- facts, b <- Map.get(f, :stateful_bundles, []) || [], into: %{} do
      {b.snapshot_ref, {f.configured_id, b}}
    end
  end

  defp adopt_one(state, instance, live_vms, bundles) do
    cond do
      # A wake stuck waking past 2 * wakeTimeoutSeconds (Task 10): the worker never
      # reported and the {:wake_timeout} timer is lost (the in-process wedge a timer
      # somehow missed). Recover it: drop the stale waking bookkeeping + err the parked
      # callers, then fall through to the normal live/bundle/vanished logic below (a
      # stranded :relighting mark heals back to :banked when the node still reports the
      # bundle) rather than skipping it forever. A wake within the bound still OWNS its
      # transition and is skipped (the next case).
      Map.has_key?(state.waking, instance.workload) and wake_stuck?(state, instance.workload) ->
        Logger.warning("embervm stateful wake stuck past bound, recovering",
          workload: instance.workload,
          instance_id: instance.instance_id
        )

        state = clear_stuck_wake(state, instance.workload)
        adopt_one(state, instance, live_vms, bundles)

      # An in-flight wake (within the bound) for the workload owns its transition; a
      # periodic reconcile must not touch it (the wake's own finish_wake will land, and
      # the NEXT reconcile adopts the result cleanly). On boot `waking` is
      # empty, so boot adoption still fully heals every limbo.
      Map.has_key?(state.waking, instance.workload) ->
        state

      # A stranded interruptible-bank checkpoint (ADR embervm/008): the store
      # shows the instance :checkpointed (or a :banking it never finished
      # resolving) and the node still reports its VM as checkpoint_pending. A
      # control-plane restart can strand this across the sweeper's resolve. The
      # SAFE DEFAULT is ABORT: mark the paused VM back to :serving and republish
      # (the caller wanted it hot, not banked; a commit needs a parked
      # connection this reconcile cannot observe, and noded auto-aborts on its
      # own timeout anyway, so this is belt-and-suspenders). If the node no
      # longer reports the VM at all the checkpoint vanished with a noded
      # restart, so we fall through to the live-VM / bundle / vanished logic
      # below rather than resurrecting a gone VM.
      instance.state in [:checkpointed, :banking] and
          is_binary(instance.vm_id) and checkpoint_pending?(live_vms, instance.vm_id) ->
        adopt_abort_stranded_checkpoint(state, instance, live_vms)

      is_binary(instance.vm_id) and Map.has_key?(live_vms, instance.vm_id) ->
        {node_id, vm} = Map.fetch!(live_vms, instance.vm_id)
        adopt_live(state, instance, node_id, vm)

      is_binary(instance.snapshot_ref) and Map.has_key?(bundles, instance.snapshot_ref) ->
        heal_to_banked(state, instance)

      node_reporting?(state, instance.node_id) ->
        fail_instance(state, instance.instance_id, "vm_and_bundle_vanished")
        state

      true ->
        state
    end
  end

  # Whether the node reports the instance's VM as an interruptible-bank
  # checkpoint awaiting resolve (checkpoint_pending, ADR embervm/008). False when
  # the VM is not reported at all or the field is absent/false.
  defp checkpoint_pending?(live_vms, vm_id) do
    case Map.get(live_vms, vm_id) do
      {_node_id, vm} -> Map.get(vm, :checkpoint_pending, false) == true
      _ -> false
    end
  end

  # Resolve a stranded checkpoint the SAFE way (ABORT): return the paused VM to
  # :serving and rebind its endpoint from node truth, then let the caller's
  # publish re-derive the fan-out. Uses the FSM-legal event for the instance's
  # current state (:checkpointed -> :abort, :banking -> :bank_abort; both land on
  # :serving). ETS-only (no durable op): the projection was already at/before
  # serving, so no new op is owed, mirroring the sweeper's abort path.
  defp adopt_abort_stranded_checkpoint(state, instance, live_vms) do
    {node_id, vm} = Map.fetch!(live_vms, instance.vm_id)
    event = if instance.state == :checkpointed, do: :abort, else: :bank_abort
    _ = StatefulStore.mark(state.store, instance.instance_id, event)

    StatefulStore.adopt_endpoint(state.store, instance.instance_id, node_id, instance.vm_id, %{
      ip: Map.get(vm, :ip),
      port: Map.get(vm, :port),
      healthy: Map.get(vm, :healthy, true)
    })

    Logger.info("embervm stateful: aborted stranded checkpoint on adoption",
      instance_id: instance.instance_id,
      workload: instance.workload
    )

    state
  end

  defp adopt_live(state, instance, node_id, vm) do
    StatefulStore.adopt_state(state.store, instance.instance_id, :serving)

    StatefulStore.adopt_endpoint(state.store, instance.instance_id, node_id, vm.vm_id, %{
      ip: Map.get(vm, :ip),
      port: Map.get(vm, :port),
      healthy: Map.get(vm, :healthy, true)
    })

    state
  end

  defp heal_to_banked(state, %{state: :banked}), do: state

  defp heal_to_banked(state, instance) do
    StatefulStore.adopt_state(state.store, instance.instance_id, :banked)
    Logger.info("embervm stateful adopted (banked)", instance_id: instance.instance_id)
    state
  end

  defp node_reporting?(state, node_id) when is_binary(node_id) do
    match?({:ok, _}, NodeCapacity.fetch(state.capacity_table, node_id))
  end

  defp node_reporting?(_state, _node_id), do: false

  # -- stuck-wake recovery (Task 10) -----------------------------------------

  # A workload is stuck iff its in-flight wake has been waking past 2 *
  # wakeTimeoutSeconds. The bound itself (start_wake's {:wake_timeout} timer) is the
  # primary release; this is the backstop for a wedge the timer missed. No start
  # timestamp (never happens on the wake path) reads as NOT stuck.
  defp wake_stuck?(state, workload) do
    case Map.get(state.wake_started, workload) do
      started when is_integer(started) ->
        state.mono_clock.() - started >= 2 * wake_timeout_seconds(state, workload) * 1_000

      _ ->
        false
    end
  end

  # Drop the stale waking bookkeeping for a stuck wake and err its parked callers so
  # they stop blocking (their own retry re-enters the wake path once the workload is
  # recovered). Single-flight is released; the reconcile then heals the workload's
  # state through the normal live/bundle/vanished path.
  defp clear_stuck_wake(state, workload) do
    {waiters, state} = pop_waiters(state, workload)
    {_trace, state} = pop_wake_trace(state, workload)
    reply_all(waiters, {:error, {:wake_failed, :wake_stuck}})
    state
  end

  # Fold every reporting node's `volumes` facts into the StatefulStore's live
  # volume ledger (generation + allocated_bytes), so pair-validity always
  # compares against the CURRENT node-reported generation. A volume is
  # node-anchored (decision 11), so each reported row is keyed by its own
  # workload; no cross-node merge is needed (at most one node reports a given
  # workload's volume).
  defp refresh_volume_facts(state, facts) do
    for f <- facts, v <- Map.get(f, :volumes, []) || [] do
      # R7 grandfather seed (ADR embervm/011): a volume this control plane has
      # NEVER blessed (blessed_generation nil, e.g. every pre-R7 volume, or a
      # brand-new one adopted before its first bless_generation call landed)
      # gets its ledger seeded from THIS eager first report before quarantine is
      # derived below, so it is never punished for predating blessing. A no-op
      # once the volume has ever been blessed (seed_blessed_generation_if_unset
      # only acts on an unset watermark).
      _ = StatefulStore.seed_blessed_generation_if_unset(state.store, v.workload, v.generation)

      StatefulStore.upsert_volume(state.store, v.workload, %{
        node_id: f.configured_id,
        generation: v.generation,
        size_bytes: v.size_bytes,
        allocated_bytes: v.allocated_bytes,
        # exported_generation (R6): the volume generation whose (vol.img, gen) pair
        # the store currently holds. Carried into the ETS volume fact so a wake can
        # decide restore-on-miss (a lost local bundle whose snapshot_generation
        # equals this can be recovered from the store) even after the local bundle
        # fact is gone. 0/absent when no store copy exists.
        exported_generation: Map.get(v, :exported_generation, 0),
        # generation_blessed (R7): the node's per-report wire fact, feeding
        # StatefulStore's quarantine derivation (see its moduledoc).
        generation_blessed: Map.get(v, :generation_blessed, false)
      })
    end

    state
  end

  defp fail_instance(state, instance_id, reason) do
    case StatefulStore.get(state.store, instance_id) do
      {:ok, instance} ->
        unless StatefulState.terminal?(instance.state) do
          _ =
            StatefulStore.transition(
              state.store,
              instance_id,
              :fail,
              :stateful_failed,
              %{reason: reason},
              %{}
            )
        end

        :ok

      :error ->
        :ok
    end
  end

  # -- wake-rate limit -----------------------------------------------------

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

  # -- catalog helpers -------------------------------------------------------

  defp stateful_workload?(state, workload) do
    match?({:ok, %{class: "stateful"}}, WorkloadCatalog.fetch(state.catalog_table, workload))
  end

  defp catalog_entry(state, workload) do
    case WorkloadCatalog.fetch(state.catalog_table, workload) do
      {:ok, entry} -> Map.put(entry, :name, workload)
      :error -> %{name: workload}
    end
  end

  defp stateful_cfg(entry),
    do: Map.get(entry, :stateful, %{port: 5432, volume_size_gib: 1, volume_mount_path: "/data"})

  defp wake_principal(workload), do: "system:stateful:#{workload}"

  # -- daemon seams ------------------------------------------------------------

  defp safe_start_stateful(state, node_id, req) do
    with {:ok, channel} <- safe_channel(state.channel_fun, node_id) do
      try do
        case state.start_stateful_fun.(channel, req) do
          {:error, reason} = err ->
            # A wake that failed because the channel's transport is dead must
            # tear the cached channel down so the NEXT wake re-dials; see
            # Embervm.NodeChannel.transport_dead?/1 (the wrapped-RPCError case).
            if Embervm.NodeChannel.transport_dead?(reason) do
              _ = state.invalidate_fun.(node_id, channel)
            end

            err

          other ->
            other
        end
      rescue
        e -> {:error, {:start_stateful_raised, e}}
      catch
        :exit, reason ->
          # The Mint ConnectionProcess behind the cached channel died (a noded
          # rollout kills in-flight connections), so GenServer.call exits
          # :noproc instead of returning a transport error. Without this
          # invalidation the dead channel stays cached and EVERY subsequent
          # wake fails until the control plane restarts, wedging the workload
          # (observed live on demo-postgres, 2026-07-18). Mirrors
          # Embervm.GroupManager.over_channel/3.
          _ = state.invalidate_fun.(node_id, channel)
          {:error, {:start_stateful_raised, {:exit, reason}}}

        kind, reason ->
          {:error, {:start_stateful_raised, {kind, reason}}}
      end
    end
  end

  defp safe_channel(channel_fun, node_id) do
    channel_fun.(node_id)
  rescue
    e -> {:error, {:channel_raised, e}}
  catch
    kind, reason -> {:error, {:channel_raised, {kind, reason}}}
  end

  # -- restore-on-miss RPC (R6, Task 8) ---------------------------------------

  # Restore the STATEFUL bundle for `workload` from the object store back onto the
  # anchor node's disk (RestoreArtifact, kind STATEFUL), then record :artifact_restored.
  # Best-effort: a restore failure returns :error and the caller (the wake worker)
  # falls through to the relight, which the daemon degrades to a cold boot via the
  # boot_image_ref that rides the request (fail-open warmth). Idempotent on the
  # daemon side, so a re-run of a partially-restored artifact is safe.
  defp restore_bundle(state, node_id, workload, snapshot_ref) do
    ref = %ArtifactRef{kind: :ARTIFACT_KIND_STATEFUL, workload: workload, ref: snapshot_ref}

    case safe_restore_artifact(state, node_id, ref) do
      {:ok, resp} ->
        record_restore(state, workload, :ARTIFACT_KIND_STATEFUL, snapshot_ref, resp)
        :ok

      other ->
        Logger.warning("embervm stateful: bundle restore-on-miss failed, degrading to cold",
          workload: workload,
          snapshot_ref: snapshot_ref,
          reason: inspect(other)
        )

        :error
    end
  end

  # Restore the VOLUME for `workload` (kind VOLUME, ref is the workload's own name,
  # per the ArtifactRef contract for a singleton volume) from the exported
  # (vol.img, gen) pair, then record :artifact_restored. Best-effort, same as
  # restore_bundle: a failure degrades to a plain cold boot (which the daemon fails
  # closed on for a truly-absent volume).
  defp restore_volume(state, node_id, workload, _volume) do
    ref = %ArtifactRef{kind: :ARTIFACT_KIND_VOLUME, workload: workload, ref: workload}

    case safe_restore_artifact(state, node_id, ref) do
      {:ok, resp} ->
        record_restore(state, workload, :ARTIFACT_KIND_VOLUME, workload, resp)
        :ok

      other ->
        Logger.warning("embervm stateful: volume restore-on-miss failed, degrading to cold",
          workload: workload,
          reason: inspect(other)
        )

        :error
    end
  end

  defp safe_restore_artifact(state, node_id, %ArtifactRef{} = ref) do
    req = %RestoreArtifactRequest{artifact: ref, trace: %Trace{workload: ref.workload}}
    req = Embervm.RestoreVendor.stamp(state.capacity_table, node_id, req)

    with {:ok, channel} <- safe_channel(state.channel_fun, node_id) do
      # The `artifact_restore` span (Task 11): a child span around the
      # RestoreArtifact RPC (the restore-on-miss read path). Carries the artifact
      # identity up front and stamps bytes-moved/skipped from the response, so the
      # Task 12 restore-on-miss gate reads its evidence off the span alone.
      Tracer.with_span "embervm.artifact_restore",
                       %{
                         attributes: %{
                           "ember.workload" => ref.workload,
                           "ember.artifact_kind" => artifact_kind_string(ref.kind),
                           "ember.artifact_ref" => ref.ref
                         }
                       } do
        result = restore_rpc(state, node_id, channel, req)
        stamp_restore_span(result)
        result
      end
    end
  end

  # The RestoreArtifact RPC with transport-death channel invalidation. Extracted so
  # the `artifact_restore` span wraps exactly the call and its result.
  defp restore_rpc(state, node_id, channel, req) do
    try do
      case state.restore_artifact_fun.(channel, req) do
        {:error, reason} = err ->
          if Embervm.NodeChannel.transport_dead?(reason) do
            _ = state.invalidate_fun.(node_id, channel)
          end

          err

        other ->
          other
      end
    rescue
      e -> {:error, {:restore_artifact_raised, e}}
    catch
      :exit, reason ->
        _ = state.invalidate_fun.(node_id, channel)
        {:error, {:restore_artifact_raised, {:exit, reason}}}

      kind, reason ->
        {:error, {:restore_artifact_raised, {kind, reason}}}
    end
  end

  # Stamp bytes-moved/skipped onto the current `artifact_restore` span from a
  # successful RestoreArtifact response. A failure result leaves the span with only
  # its identity attributes (the error itself surfaces in the caller's warning log).
  defp stamp_restore_span({:ok, resp}) do
    Tracer.set_attributes(%{
      "ember.bytes_moved" => Map.get(resp, :bytes_moved, 0),
      "ember.skipped" => Map.get(resp, :skipped, false)
    })
  end

  defp stamp_restore_span(_other), do: :ok

  # Append the audit-only :artifact_restored op (no projection table; the log itself
  # is the record). Best-effort: an append failure must never fail the wake, which
  # already ran the restore RPC (the durable state is the restored bytes on disk,
  # not this audit row).
  defp record_restore(state, workload, kind, ref, resp) do
    op = %Embervm.OpLog.Op{
      kind: :artifact_restored,
      tenant: state.tenant,
      principal: wake_principal(workload),
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

    _ = Embervm.OpLog.SQLite.append(state.op_log, op)
    :ok
  rescue
    e ->
      Logger.warning("embervm stateful: artifact_restored append raised", workload: workload, error: inspect(e))
      :ok
  end

  defp artifact_kind_string(:ARTIFACT_KIND_STATEFUL), do: "stateful"
  defp artifact_kind_string(:ARTIFACT_KIND_VOLUME), do: "volume"
  defp artifact_kind_string(other), do: to_string(other)

  # -- remote volume eviction (R6, Task 9) ------------------------------------

  # Drop the store copy of a workload's VOLUME (EvictArtifact, remote=true, kind
  # VOLUME) alongside DeleteVolume. Best-effort; the daemon refuses the evict if a
  # bundle still pairs (its own generation guard), and here delete was already
  # refused while any instance existed, so the guard is doubly held.
  defp evict_remote_volume(state, %{node_id: node_id}, workload) when is_binary(node_id) do
    artifact = %ArtifactRef{kind: :ARTIFACT_KIND_VOLUME, workload: workload, ref: workload}
    req = %EvictArtifactRequest{artifact: artifact, remote: true, trace: %Trace{workload: workload}}

    with {:ok, channel} <- safe_channel(state.channel_fun, node_id) do
      try do
        state.evict_artifact_fun.(channel, req)
      rescue
        _ -> :error
      catch
        :exit, _ ->
          _ = state.invalidate_fun.(node_id, channel)
          :error

        _, _ ->
          :error
      end
    end

    :ok
  end

  defp evict_remote_volume(_state, _volume, _workload), do: :ok

  defp default_start_stateful(channel, req) do
    Embervm.Node.V1.NodeService.Stub.start_stateful(channel, req)
  end

  defp default_stop_stateful(channel, req) do
    Embervm.Node.V1.NodeService.Stub.stop_stateful(channel, req)
  end

  defp default_delete_volume(channel, req) do
    Embervm.Node.V1.NodeService.Stub.delete_volume(channel, req)
  end

  defp default_restore_artifact(channel, req) do
    Embervm.Node.V1.NodeService.Stub.restore_artifact(channel, req)
  end

  defp default_evict_artifact(channel, req) do
    Embervm.Node.V1.NodeService.Stub.evict_artifact(channel, req)
  end

  # -- misc --------------------------------------------------------------------

  defp audit_denial(_state, principal, workload, reason) do
    Embervm.Metering.record_denial(principal, workload, reason)
  end

  defp mint_id(%{id_fun: fun}) when is_function(fun, 0), do: fun.()
  defp mint_id(state), do: "stf-" <> String.trim_leading(Embervm.SessionId.new(state.clock.()), "s-")

  defp schedule(msg, interval_ms) when interval_ms > 0 do
    Process.send_after(self(), msg, interval_ms)
  end

  defp schedule(_msg, _interval), do: :ok

  defp default_clock, do: System.system_time(:millisecond)

  # Monotonic ms for the wake-worker bound + the adoption stuck-check (never wall
  # time: the bound is a duration, immune to clock steps). Tests inject a fake.
  defp default_mono, do: System.monotonic_time(:millisecond)
end
