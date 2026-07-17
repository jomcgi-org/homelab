defmodule Embervm.StatefulStore do
  @moduledoc """
  ETS hot set over the op-log's durable `stateful_instances` + `volumes`
  projections (R4), mirroring `Embervm.ServingStore`: every read the publisher
  and activator need is O(1)-to-bounded against ETS, while every durable write
  goes through the op-log FIRST and only lands in ETS once the op-log confirms it
  is durable. That ordering, "op-log append succeeds, then and only then update
  ETS", is the write-through invariant this module enforces: ETS never shows an
  instance in a state the op-log does not already agree with, and a crash between
  the two never loses a transition (worst case ETS is briefly stale until the next
  boot's rebuild replays it).

  On `init/1` this rebuilds its two ETS tables from
  `OpLog.load_stateful_instances/1` and `OpLog.load_volumes/1`, the recovery
  path: a fresh StatefulStore against an existing op-log ends up with exactly the
  state the durable projections recorded, no replay logic beyond "read the
  projection". A projection rebuild followed by an `Embervm.EndpointPublisher`
  publish is therefore byte-identical to the pre-restart snapshot (the publisher
  is a pure function of these facts). Adoption (a later task) layers the NODE's
  reported stateful inventory on top of this durable rebuild to heal residency
  and limbo states.

  ## the singleton invariant

  A stateful workload is a SINGLETON (decision 3): at most ONE live VM at a time.
  `start/2` enforces this at the write boundary: if a live (non-terminal,
  non-banked) instance already exists for the workload, it returns
  `{:error, :already_live}` WITHOUT appending an op, so the durable log never
  records two concurrent live boots for one workload. A `banked` instance is NOT
  live (it holds a snapshot, not a VM), so a workload with only a banked instance
  can still `start` a fresh cold boot (the wake path); the paired eviction of the
  stale bundle rides its own op.

  ## the endpoint fact

  An instance contributes the workload's SINGLE L4 endpoint exactly when its FSM
  state is `serving` AND the node's health probe reports it healthy. Because the
  class is a singleton, `published_endpoint/1` returns ONE `%{ip, port}` (or nil),
  NOT a list like serving's `published_endpoints/2`. Health ejection
  (`set_health/2`) flips `healthy` on the ETS row WITHOUT an FSM transition (health
  is a lossy node fact, not durable lifecycle state), and the publisher re-derives
  the endpoint on its next flush, so an unhealthy instance drops out and a
  recovered one reappears.

  ## the pair: warmth vs the volume ledger

  A banked instance stamps `snapshot_generation` into its bundle (the pair key);
  the volume's `generation` is the on-disk ledger's current value, bumped on every
  writable attach (start / relight / cold-boot). The pair is VALID iff
  `banked.snapshot_generation == volume.generation`. `pair_valid?/1` reads that;
  `eager_evict_broken_pairs/0` is the sweep PRIMITIVE that evicts every banked
  instance whose pair is broken (reason `pair_broken`). This module owns the
  primitive only; a later task owns the sweep cadence that calls it.

  ## unpublish is ETS-only (a decision)

  Unlike `ServingStore.unpublish/3`, which is durable (`serving_unpublished` ->
  the `draining` projection state), stateful `unpublish/2` is ETS-only (a `mark`):
  the FSM edge `serving -> banking` is transient by construction (`banking` is not
  a persisted projection state, so a durable transition into it would break the
  totality of `state_from_string/1` that the byte-identical rebuild depends on).
  The endpoint-pull is followed immediately by the durable `bank_ready` (or a
  destroy/fail), which IS the persisted record; a crash between them is healed
  from node inventory by adoption, exactly like serving's transient `banking`.
  The `stateful_unpublished` op kind (audit-only in the projection) is therefore
  reserved for a later task that wants an explicit endpoint-lifetime audit record;
  the store does not append it. This is the simpler consistent option: no durable
  op ever writes a non-projection state.

  ## transient states + adoption

  `banking`, `relighting`, and `cold_booting` are ETS-only markers (`mark/2`, no
  op-log append): a later durable completion op (`stateful_banked` /
  `stateful_relit` / `stateful_cold_booted`) or an adoption reconcile resolves
  them. `adopt_state/3` and `adopt_endpoint/5` force the ETS view from
  authoritative node truth, bypassing the FSM (adoption is idempotent and total
  over limbo states the FSM cannot bridge) and never appending an op.
  """

  use GenServer
  require Logger

  alias Embervm.OpLog.Op
  alias Embervm.StatefulState

  @instances_table :embervm_stateful_instances
  @volumes_table :embervm_stateful_volumes

  # -- Client API ------------------------------------------------------------

  @spec start_link(keyword()) :: GenServer.on_start()
  def start_link(opts) do
    case Keyword.get(opts, :name, __MODULE__) do
      nil -> GenServer.start_link(__MODULE__, opts)
      name -> GenServer.start_link(__MODULE__, opts, name: name)
    end
  end

  @doc """
  Starts a new stateful instance (a fresh first boot OR an explicit cold boot that
  begins a new lifecycle). `attrs` is `%{instance_id, tenant, principal, workload,
  node_id, vm_id, generation}` (the `generation` is the volume generation this
  attach booted with, the pair-key baseline; it comes from the payload, not
  minted here). Appends `stateful_started` (write-through), inserts the ETS
  hot-set row in `:starting`, and returns `{:ok, instance}`.

  Enforces the SINGLETON invariant: if a live (non-terminal, non-banked) instance
  already exists for the workload, returns `{:error, :already_live}` WITHOUT
  appending an op. A workload with only a banked instance is NOT blocked (the
  banked instance holds no live VM), so the cold-boot wake path can start on it.
  """
  @spec start(GenServer.server(), map()) :: {:ok, map()} | {:error, term()}
  def start(store \\ __MODULE__, attrs) do
    GenServer.call(store, {:start, attrs})
  end

  @doc """
  Records a WAKE that discarded warmth and cold-booted, a NEW instance replacing a
  retired banked one. Identical to `start/2` (singleton gate, mints a fresh
  lifecycle) EXCEPT it appends `stateful_cold_booted` carrying `attrs.reason`
  (`generation_mismatch | no_bundle | ledger_unreadable`) instead of
  `stateful_started`, so the op-log alone reconstructs why the warmth was
  discarded (R4 gate 2). Used by the wake path when the pair broke (the control
  plane saw it in plan_wake, or the daemon fell back at StartStateful time); a
  genuine FRESH first boot still uses `start/2`.
  """
  @spec cold_boot(GenServer.server(), map()) :: {:ok, map()} | {:error, term()}
  def cold_boot(store \\ __MODULE__, attrs) do
    GenServer.call(store, {:cold_boot, attrs})
  end

  @doc """
  Durably records a freshly created volume: appends `volume_created` (write-
  through) carrying `{node_id, generation, size_bytes, allocated_bytes}` and
  upserts the `volumes` ETS row, so the durable volumes projection is populated
  the first time a FRESH boot creates the workload's volume file (without this the
  boot ops' `bump_volume_generation` projection has no row to update and the
  volumes table stays empty). Idempotent-ish: a re-create upserts the row.
  """
  @spec create_volume(GenServer.server(), String.t(), map()) :: {:ok, map()} | {:error, term()}
  def create_volume(store \\ __MODULE__, workload, fields) do
    GenServer.call(store, {:create_volume, workload, fields})
  end

  @doc """
  Applies an FSM transition to an instance, appending the matching op
  write-through. `event` is a `StatefulState` event; `op_kind` and `payload`
  describe the op; `updates` are extra fields merged into the ETS row AFTER the
  durable append succeeds. Terminal transitions record the reason. Returns the
  updated instance or `{:error, reason}` (including `{:illegal_transition, ...}`).
  Mirrors `ServingStore.transition/6` exactly.
  """
  @spec transition(GenServer.server(), String.t(), atom(), atom(), map(), map()) ::
          {:ok, map()} | {:error, term()}
  def transition(store \\ __MODULE__, instance_id, event, op_kind, payload, updates) do
    GenServer.call(store, {:transition, instance_id, event, op_kind, payload, updates})
  end

  @doc """
  Convenience for the publish edge: transitions `starting` -> `serving` with a
  `stateful_published` op, writing the fresh `{ip, port}` L4 endpoint and marking
  the row healthy (the daemon health-gated readiness before StartStateful
  returned, so a freshly published instance is healthy by construction; the node's
  ongoing probe can later flip it via `set_health/2`). `reason` is one of
  `:started | :relit | :cold_booted` (the audit reason).
  """
  @spec publish(GenServer.server(), String.t(), String.t(), non_neg_integer(), atom()) ::
          {:ok, map()} | {:error, term()}
  def publish(store \\ __MODULE__, instance_id, ip, port, reason) do
    GenServer.call(store, {:publish, instance_id, ip, port, reason})
  end

  @doc """
  The endpoint-pull ACTION: transitions `serving` -> `banking` (ETS-ONLY, no
  op-log append; see the module doc's "unpublish is ETS-only" decision), pulling
  the instance from the fan-out and beginning the bank sequence. A later durable
  `bank_ready` (via `transition/6`) completes it. Returns the updated instance or
  `{:error, reason}`. `reason` is carried only in the transient ETS `drain_reason`
  field, never a durable column.
  """
  @spec unpublish(GenServer.server(), String.t(), atom()) :: {:ok, map()} | {:error, term()}
  def unpublish(store \\ __MODULE__, instance_id, reason) do
    GenServer.call(store, {:unpublish, instance_id, reason})
  end

  @doc """
  Applies a TRANSIENT FSM edge WITHOUT an op-log append: the ETS-only move into a
  mid-operation state (`banking`, `relighting`, `cold_booting`, or their `*_abort`
  recoveries) that a crash heals from node inventory rather than from the durable
  log. `event` must be a legal FSM edge from the instance's current state; an
  illegal edge is `{:error, {:illegal_transition, ...}}`.
  """
  @spec mark(GenServer.server(), String.t(), atom()) :: {:ok, map()} | {:error, term()}
  def mark(store \\ __MODULE__, instance_id, event) do
    GenServer.call(store, {:mark, instance_id, event})
  end

  @doc """
  Like `mark/2` (a TRANSIENT ETS-only FSM edge, no op-log append) but also merges
  `updates` into the ETS row after the state move, exactly as `unpublish/3` does.
  The interruptible-bank checkpoint uses this to stamp the `checkpoint_token` and
  `vm_id` onto the row when marking `banking -> checkpointed`, so adoption and the
  resolve step can read them from ETS without a durable op (the checkpoint outcome
  is persisted only later by the `:commit` transition or an `:abort` republish).
  """
  @spec mark_with(GenServer.server(), String.t(), atom(), map()) :: {:ok, map()} | {:error, term()}
  def mark_with(store \\ __MODULE__, instance_id, event, updates) do
    GenServer.call(store, {:mark_with, instance_id, event, updates})
  end

  @doc """
  Flips an instance's `healthy` flag from the node's probe fact (health ejection),
  WITHOUT an FSM transition or an op-log append: health is a lossy node fact, not
  durable lifecycle state. A no-op for an unknown instance. Returns the updated
  instance or `:error`. The publisher re-derives the endpoint from the
  healthy-serving fact on its next flush, so flipping this drops or restores it.
  """
  @spec set_health(GenServer.server(), String.t(), boolean()) :: {:ok, map()} | :error
  def set_health(store \\ __MODULE__, instance_id, healthy?) do
    GenServer.call(store, {:set_health, instance_id, healthy?})
  end

  @doc """
  Stamps an instance's `last_active_at` from the idle-signal sweep, WITHOUT an FSM
  transition or an op-log append: activity is a lossy, high-frequency node fact (a
  connection-count delta), not durable lifecycle state, exactly like
  `set_health/2`. A no-op for an unknown instance. Returns the updated instance or
  `:error`. Because it is ETS-only, a control-plane restart resets the idle clock
  to "just active", the fail-open direction (never bank on a cold idle baseline).
  """
  @spec touch_active(GenServer.server(), String.t(), integer()) :: {:ok, map()} | :error
  def touch_active(store \\ __MODULE__, instance_id, ts) do
    GenServer.call(store, {:touch_active, instance_id, ts})
  end

  @doc "The instance's hot-set row, or `:error` if unknown."
  @spec get(GenServer.server(), String.t()) :: {:ok, map()} | :error
  def get(store \\ __MODULE__, instance_id) do
    GenServer.call(store, {:get, instance_id})
  end

  @doc """
  Every instance in the hot set (a full ETS scan), for the adoption reconcile and
  the management API. Rare (boot + registry sweep + management reads), never on a
  request path.
  """
  @spec all(GenServer.server()) :: [map()]
  def all(store \\ __MODULE__) do
    GenServer.call(store, :all)
  end

  @doc """
  Every instance for `workload`, newest-created first, for the
  `GET /v1/stateful/{workload}` management API. A bounded ETS scan. Because the
  class is a singleton there is at most one LIVE instance, but a workload can carry
  a live instance plus terminal history, so this returns a list.
  """
  @spec list(GenServer.server(), String.t()) :: [map()]
  def list(store \\ __MODULE__, workload) do
    GenServer.call(store, {:list, workload})
  end

  @doc """
  The publisher fact: the workload's SINGLE live endpoint as `%{ip, port}`, or nil
  when the workload has no healthy `serving` instance (the singleton, so ONE, not
  a list). nil is the signal the publisher swaps in the activator endpoint (or,
  when no activator is configured, skips the workload entirely).
  """
  @spec published_endpoint(GenServer.server(), String.t()) ::
          %{ip: String.t(), port: non_neg_integer()} | nil
  def published_endpoint(store \\ __MODULE__, workload) do
    GenServer.call(store, {:published_endpoint, workload})
  end

  @doc """
  The publisher fact: every workload that has at least one instance in the hot set
  (live, banked, or terminal), so the publisher can enumerate the stateful
  clusters. The publisher actually enumerates from the CATALOG (a workload with no
  instance still needs an activator-backed listener to wake it), so this is a
  supplementary "has any instance" set, not the sole source.
  """
  @spec stateful_workloads(GenServer.server()) :: [String.t()]
  def stateful_workloads(store \\ __MODULE__) do
    GenServer.call(store, :stateful_workloads)
  end

  @doc """
  Live and banked counts, `%{workload => %{live, banked}}` over every workload in
  the hot set (the O(1) capacity/GC read). Live is any non-terminal, non-banked
  instance; banked is the `banked` state. Reads the maintained per-workload
  counters, never a scan. Mirrors `ServingStore.counts/2`'s bucket shape but keyed
  by workload for the whole set (the router's per-workload view slices one key).
  """
  @spec counts(GenServer.server()) :: %{optional(String.t()) => %{live: non_neg_integer(), banked: non_neg_integer()}}
  def counts(store \\ __MODULE__) do
    GenServer.call(store, :counts)
  end

  @doc """
  The volume row for `workload` (`%{workload, node_id, generation, size_bytes,
  allocated_bytes, updated_at}`), or nil if the workload has no volume. Read by the
  pairing check and the router's `volume_bytes` field.
  """
  @spec get_volume(GenServer.server(), String.t()) :: map() | nil
  def get_volume(store \\ __MODULE__, workload) do
    GenServer.call(store, {:get_volume, workload})
  end

  @doc """
  Upsert the volume fact for `workload` (ETS-only: the durable volume row is
  written by the op-log's `volume_created` projection; this is the live-fact
  primitive Task 8 drives from NodeStatus scrapes, the way `set_health/2` /
  `touch_active/3` update lossy node facts). Merges the given fields over any
  existing row (or inserts a fresh one). Returns the merged volume row.
  """
  @spec upsert_volume(GenServer.server(), String.t(), map()) :: map()
  def upsert_volume(store \\ __MODULE__, workload, fields) do
    GenServer.call(store, {:upsert_volume, workload, fields})
  end

  @doc """
  Durably deletes `workload`'s volume row: appends `volume_deleted` (write-
  through) and ONLY on success removes the ETS volume fact. The ONLY destructive
  data verb in the system (a CR deletion never reaches this); `Embervm.StatefulManager`
  calls it from the explicit `DELETE /v1/stateful/:name/volume` management act,
  never automatically. Idempotent: deleting an already-absent volume still
  appends the op (the durable record of the act) and returns `:ok`. Returns
  `{:error, reason}` on an append failure, leaving the ETS row untouched (as
  durable as the op-log agrees).
  """
  @spec delete_volume(GenServer.server(), String.t()) :: :ok | {:error, term()}
  def delete_volume(store \\ __MODULE__, workload) do
    GenServer.call(store, {:delete_volume, workload})
  end

  @doc """
  Whether `workload`'s banked bundle is VALID against the current volume: true iff
  the workload has a `banked` instance whose `snapshot_generation` equals the
  workload's volume `generation`. False when either side is missing (no banked
  instance, or no volume row) and false when the generations diverge (the volume
  moved out from under the bundle). This is the wake-path decision (relight a valid
  pair, cold-boot a broken one).
  """
  @spec pair_valid?(GenServer.server(), String.t()) :: boolean()
  def pair_valid?(store \\ __MODULE__, workload) do
    GenServer.call(store, {:pair_valid, workload})
  end

  @doc """
  Sweep PRIMITIVE: evict every `banked` instance whose pair is broken
  (`snapshot_generation != volume.generation`, or the volume row is gone),
  transitioning each to `evicted` with reason `pair_broken` via the DURABLE path
  (a `stateful_evicted` op then the ETS update). Returns the list of evicted
  instance_ids. This is a primitive ONLY: it wires no timer/scheduler; a later
  task owns the sweep cadence that calls it.
  """
  @spec eager_evict_broken_pairs(GenServer.server()) :: [String.t()]
  def eager_evict_broken_pairs(store \\ __MODULE__) do
    GenServer.call(store, :eager_evict_broken_pairs)
  end

  @doc """
  Adoption: FORCE an instance's ETS state to `new_state` from authoritative node
  truth, bypassing the FSM (adoption is idempotent and derived from what the node
  actually holds, so it must be total over limbo states the FSM cannot bridge).
  Does NOT append an op. A no-op for an unknown or already-terminal instance (never
  resurrects a terminal row). Returns `:ok`.
  """
  @spec adopt_state(GenServer.server(), String.t(), :serving | :starting | :banked) :: :ok
  def adopt_state(store \\ __MODULE__, instance_id, new_state) do
    GenServer.call(store, {:adopt_state, instance_id, new_state})
  end

  @doc """
  Adoption: rebind an instance the node reports as a LIVE stateful VM to
  `{node_id, vm_id, ip, port}`, writing the endpoint fact and marking it healthy
  WITHOUT an FSM transition or an op-log append (the endpoint is a lossy fact the
  node owns). A no-op for an unknown instance. Returns `:ok`.
  """
  @spec adopt_endpoint(GenServer.server(), String.t(), String.t(), String.t(), map()) :: :ok
  def adopt_endpoint(store \\ __MODULE__, instance_id, node_id, vm_id, endpoint) do
    GenServer.call(store, {:adopt_endpoint, instance_id, node_id, vm_id, endpoint})
  end

  # -- GenServer callbacks ---------------------------------------------------

  @impl true
  def init(opts) do
    op_log = Keyword.get(opts, :op_log, Embervm.OpLog.SQLite)
    clock = Keyword.get(opts, :clock, &default_clock/0)

    instances = :ets.new(@instances_table, [:set, :private])
    volumes = :ets.new(@volumes_table, [:set, :private])

    state = %{
      op_log: op_log,
      clock: clock,
      instances: instances,
      volumes: volumes,
      # workload -> %{live, banked}, kept in step with the hot set on every write.
      counts: %{}
    }

    case rebuild(state) do
      {:ok, state} -> {:ok, state}
      {:error, reason} -> {:stop, {:rebuild_failed, reason}}
    end
  end

  # Rebuild: read every durable stateful-instance + volume row and populate the two
  # hot sets + counts from scratch. No per-op replay: the projections already ARE
  # the current state. `healthy` is NOT durable (it is a live node-probe fact); a
  # rebuilt `serving` instance is marked healthy=true so the publisher re-publishes
  # the exact endpoint the projection recorded (byte-identical rebuild), and the
  # node's next probe corrects it if it is actually unhealthy. A non-serving rebuilt
  # instance is healthy=false (it is not in the fan-out anyway).
  defp rebuild(state) do
    with {:ok, rows} <- Embervm.OpLog.SQLite.load_stateful_instances(state.op_log),
         {:ok, volumes} <- Embervm.OpLog.SQLite.load_volumes(state.op_log) do
      state =
        Enum.reduce(rows, state, fn row, acc ->
          instance = row_to_instance(row)
          :ets.insert(acc.instances, {instance.instance_id, instance})
          bump_counts(acc, nil, instance.state, instance.workload)
        end)

      Enum.each(volumes, fn vol ->
        :ets.insert(state.volumes, {vol.workload, volume_row(vol)})
      end)

      {:ok, state}
    end
  end

  defp row_to_instance(row) do
    fsm_state = state_from_string(row.state)

    %{
      instance_id: row.instance_id,
      tenant: row.tenant,
      principal: row.principal,
      workload: row.workload,
      state: fsm_state,
      node_id: row.node_id,
      vm_id: row.vm_id,
      ip: row.ip,
      port: row.port,
      # Not durable: a rebuilt serving instance is assumed healthy so the boot
      # publish re-emits its recorded endpoint identically; the node probe corrects
      # a truly-unhealthy one on its next report.
      healthy: fsm_state == :serving,
      # Transient (ETS-only) endpoint-pull marker, set on unpublish; nil after a
      # rebuild (a bank interrupted by a restart is re-driven by adoption, not
      # resumed from this field).
      drain_reason: nil,
      generation: row.generation || 0,
      snapshot_ref: row.snapshot_ref,
      snapshot_generation: row.snapshot_generation,
      snapshot_size_bytes: row.snapshot_size_bytes,
      created_at: row.created_at,
      last_active_at: row.last_active_at,
      updated_at: row.updated_at,
      terminal_reason: row.terminal_reason
    }
  end

  defp volume_row(vol) do
    %{
      workload: vol.workload,
      node_id: vol.node_id,
      generation: vol.generation || 0,
      size_bytes: vol.size_bytes,
      allocated_bytes: vol.allocated_bytes,
      updated_at: vol.updated_at
    }
  end

  # Explicit map (not String.to_existing_atom): fails loudly on an unknown string
  # and documents the exact projection strings the merged PR-1 projection writes,
  # exactly as ServingStore does. These SIX strings are the complete set
  # `stateful_instances.state` can hold; `banking`/`relighting`/`cold_booting` are
  # transient ETS-only states never persisted, so they are absent here by
  # construction.
  @state_strings %{
    "starting" => :starting,
    "serving" => :serving,
    "banked" => :banked,
    "evicted" => :evicted,
    "destroyed" => :destroyed,
    "failed" => :failed
  }

  defp state_from_string(str), do: Map.fetch!(@state_strings, str)

  @impl true
  def handle_call({:start, attrs}, _from, state) do
    do_start(attrs, state, :stateful_started)
  end

  def handle_call({:cold_boot, attrs}, _from, state) do
    do_start(attrs, state, :stateful_cold_booted)
  end

  def handle_call({:transition, instance_id, event, op_kind, payload, updates}, _from, state) do
    do_transition(state, instance_id, event, op_kind, payload, updates)
  end

  def handle_call({:publish, instance_id, ip, port, reason}, _from, state) do
    payload = %{ip: ip, port: port, reason: to_string(reason)}
    # Publishing clears any prior drain_reason: a (re)published instance is serving,
    # not being banked, so the endpoint-pull marker no longer applies.
    updates = %{ip: ip, port: port, healthy: true, drain_reason: nil}
    do_transition(state, instance_id, :publish, :stateful_published, payload, updates)
  end

  def handle_call({:unpublish, instance_id, reason}, _from, state) do
    # ETS-only (a mark), NOT durable: see the module doc's "unpublish is ETS-only"
    # decision. The endpoint-pull moves serving -> banking transiently; the durable
    # record is the paired bank_ready (or destroy/fail) that follows. Stamp the
    # transient drain_reason (never a durable column) and drop the endpoint fact.
    do_mark_with_updates(state, instance_id, :unpublish, %{
      healthy: false,
      drain_reason: to_string(reason),
      ip: nil,
      port: nil
    })
  end

  def handle_call({:mark, instance_id, event}, _from, state) do
    do_mark_with_updates(state, instance_id, event, %{})
  end

  def handle_call({:mark_with, instance_id, event, updates}, _from, state) do
    do_mark_with_updates(state, instance_id, event, updates)
  end

  def handle_call({:set_health, instance_id, healthy?}, _from, state) do
    case fetch(state, instance_id) do
      {:ok, instance} ->
        updated = %{instance | healthy: healthy?, updated_at: state.clock.()}
        :ets.insert(state.instances, {instance_id, updated})
        {:reply, {:ok, updated}, state}

      {:error, _} ->
        {:reply, :error, state}
    end
  end

  def handle_call({:touch_active, instance_id, ts}, _from, state) do
    case fetch(state, instance_id) do
      {:ok, instance} ->
        updated = %{instance | last_active_at: ts, updated_at: ts}
        :ets.insert(state.instances, {instance_id, updated})
        {:reply, {:ok, updated}, state}

      {:error, _} ->
        {:reply, :error, state}
    end
  end

  def handle_call({:get, instance_id}, _from, state) do
    {:reply, get_view(state, instance_id), state}
  end

  def handle_call(:all, _from, state) do
    all = :ets.foldl(fn {_id, instance}, acc -> [instance | acc] end, [], state.instances)
    {:reply, all, state}
  end

  def handle_call({:list, workload}, _from, state) do
    items =
      :ets.foldl(
        fn {_id, instance}, acc ->
          if instance.workload == workload, do: [instance | acc], else: acc
        end,
        [],
        state.instances
      )
      |> Enum.sort_by(& &1.created_at, :desc)

    {:reply, items, state}
  end

  def handle_call({:published_endpoint, workload}, _from, state) do
    # The singleton: at most ONE serving+healthy instance for the workload. Fold to
    # the first match (stable: there is only one live by the class invariant). nil
    # when there is none, the signal for the activator swap.
    endpoint =
      :ets.foldl(
        fn {_id, instance}, acc ->
          if acc == nil and servable?(instance, workload) do
            %{ip: instance.ip, port: instance.port}
          else
            acc
          end
        end,
        nil,
        state.instances
      )

    {:reply, endpoint, state}
  end

  def handle_call(:stateful_workloads, _from, state) do
    workloads =
      :ets.foldl(
        fn {_id, instance}, acc -> MapSet.put(acc, instance.workload) end,
        MapSet.new(),
        state.instances
      )
      |> MapSet.to_list()

    {:reply, workloads, state}
  end

  def handle_call(:counts, _from, state) do
    {:reply, state.counts, state}
  end

  def handle_call({:get_volume, workload}, _from, state) do
    {:reply, fetch_volume(state, workload), state}
  end

  def handle_call({:upsert_volume, workload, fields}, _from, state) do
    ts = state.clock.()

    base =
      fetch_volume(state, workload) ||
        %{
          workload: workload,
          node_id: nil,
          generation: 0,
          size_bytes: nil,
          allocated_bytes: nil,
          updated_at: ts
        }

    merged = base |> Map.merge(fields) |> Map.put(:workload, workload) |> Map.put(:updated_at, ts)
    :ets.insert(state.volumes, {workload, merged})
    {:reply, merged, state}
  end

  def handle_call({:create_volume, workload, fields}, _from, state) do
    ts = state.clock.()

    op = %Op{
      kind: :volume_created,
      tenant: "homelab",
      # The volume-create act has no per-request caller crossing this boundary; the
      # op's principal records the workload's synthesized system owner, matching
      # every other stateful lifecycle op (StatefulManager.wake_principal/1).
      principal: "system:stateful:#{workload}",
      workload: workload,
      ts: ts,
      payload: %{
        node_id: Map.get(fields, :node_id),
        generation: Map.get(fields, :generation, 0),
        size_bytes: Map.get(fields, :size_bytes),
        allocated_bytes: Map.get(fields, :allocated_bytes)
      }
    }

    case Embervm.OpLog.SQLite.append(state.op_log, op) do
      {:ok, _seq} ->
        base = fetch_volume(state, workload) || %{workload: workload}

        merged =
          base
          |> Map.merge(fields)
          |> Map.put(:workload, workload)
          |> Map.put(:updated_at, ts)

        :ets.insert(state.volumes, {workload, merged})
        {:reply, {:ok, merged}, state}

      {:error, _reason} = error ->
        {:reply, error, state}
    end
  end

  def handle_call({:delete_volume, workload}, _from, state) do
    ts = state.clock.()

    op = %Op{
      kind: :volume_deleted,
      tenant: "homelab",
      # The volume delete act has no per-request caller identity crossing this
      # store boundary (the router's management auth already authorized the
      # caller; the op's principal records the workload's synthesized system
      # owner, matching every other stateful lifecycle op, e.g.
      # StatefulManager.wake_principal/1's `system:stateful:<workload>`).
      principal: "system:stateful:#{workload}",
      workload: workload,
      ts: ts,
      payload: %{}
    }

    case Embervm.OpLog.SQLite.append(state.op_log, op) do
      {:ok, _seq} ->
        :ets.delete(state.volumes, workload)
        {:reply, :ok, state}

      {:error, _reason} = error ->
        {:reply, error, state}
    end
  end

  def handle_call({:pair_valid, workload}, _from, state) do
    {:reply, do_pair_valid?(state, workload), state}
  end

  def handle_call(:eager_evict_broken_pairs, _from, state) do
    do_eager_evict_broken_pairs(state)
  end

  def handle_call({:adopt_state, instance_id, new_state}, _from, state)
      when new_state in [:serving, :starting, :banked] do
    case fetch(state, instance_id) do
      {:ok, %{state: cur}} when cur in [:evicted, :destroyed, :failed] ->
        # Never resurrect a terminal instance from a stale node fact.
        {:reply, :ok, state}

      {:ok, instance} ->
        if instance.state == new_state do
          {:reply, :ok, state}
        else
          ts = state.clock.()
          # Entering `banked` clears the live-VM endpoint (a banked instance holds a
          # snapshot, not a VM); entering a live state leaves the endpoint fact to
          # adopt_endpoint. A banked instance is never in the fan-out, so healthy=false.
          updated =
            if new_state == :banked do
              %{instance | state: :banked, healthy: false, ip: nil, port: nil, vm_id: nil, updated_at: ts}
            else
              %{instance | state: new_state, updated_at: ts}
            end

          :ets.insert(state.instances, {instance_id, updated})
          state = bump_counts(state, instance.state, new_state, instance.workload)
          {:reply, :ok, state}
        end

      {:error, _} ->
        {:reply, :ok, state}
    end
  end

  def handle_call({:adopt_endpoint, instance_id, node_id, vm_id, endpoint}, _from, state) do
    case fetch(state, instance_id) do
      {:ok, instance} ->
        updated = %{
          instance
          | node_id: node_id,
            vm_id: vm_id,
            ip: Map.get(endpoint, :ip),
            port: Map.get(endpoint, :port),
            healthy: Map.get(endpoint, :healthy, instance.healthy)
        }

        :ets.insert(state.instances, {instance_id, updated})
        {:reply, :ok, state}

      {:error, _} ->
        {:reply, :ok, state}
    end
  end

  # -- start -----------------------------------------------------------------

  # A cold-boot birth op carries its discarded-warmth reason; a fresh start carries
  # none (and a nil reason on a cold boot is dropped rather than recorded as nil).
  defp maybe_put_reason(payload, :stateful_cold_booted, reason) when not is_nil(reason),
    do: Map.put(payload, :reason, to_string(reason))

  defp maybe_put_reason(payload, _op_kind, _reason), do: payload

  defp do_start(attrs, state, op_kind) do
    workload = Map.fetch!(attrs, :workload)

    # Singleton gate BEFORE any durable write: refuse a second live instance for the
    # workload so the op-log never records two concurrent live boots. `banked` is
    # NOT live (it holds a snapshot, not a VM), so a banked-only workload passes and
    # can cold-boot a fresh lifecycle.
    if has_live_instance?(state, workload) do
      {:reply, {:error, :already_live}, state}
    else
      append_start(attrs, workload, state, op_kind)
    end
  end

  # Appends the birth op for a new instance lifecycle and inserts its ETS row.
  # op_kind is :stateful_started (a FRESH first boot) or :stateful_cold_booted (a
  # wake that discarded warmth); the latter carries the discarded-warmth reason so
  # the op-log alone reconstructs the pairing decision (gate 2). Both project into
  # a "starting" instance identically; only the op kind and the reason differ.
  defp append_start(attrs, workload, state, op_kind) do
    ts = state.clock.()
    instance_id = Map.fetch!(attrs, :instance_id)
    generation = Map.get(attrs, :generation, 0)

    payload =
      %{
        node_id: Map.get(attrs, :node_id),
        vm_id: Map.get(attrs, :vm_id),
        # The projection defaults the birth op to "starting"; recorded explicitly
        # for clarity. The volume generation this attach booted with (the pair-key
        # baseline) rides the payload so the projection can bump volumes.generation.
        generation: generation,
        state: "starting"
      }
      # A cold boot carries the reason it discarded warmth (generation_mismatch |
      # no_bundle | ledger_unreadable); a fresh start has none.
      |> maybe_put_reason(op_kind, Map.get(attrs, :reason))

    op = %Op{
      kind: op_kind,
      tenant: Map.fetch!(attrs, :tenant),
      principal: Map.get(attrs, :principal),
      workload: workload,
      stateful_instance_id: instance_id,
      ts: ts,
      payload: payload
    }

    case Embervm.OpLog.SQLite.append(state.op_log, op) do
      {:ok, _seq} ->
        instance = %{
          instance_id: instance_id,
          tenant: op.tenant,
          principal: op.principal,
          workload: workload,
          state: :starting,
          node_id: Map.get(attrs, :node_id),
          vm_id: Map.get(attrs, :vm_id),
          # Not yet in the fan-out: the endpoint is reported at publish.
          ip: nil,
          port: nil,
          # Not yet serving: healthy is meaningful only once published.
          healthy: false,
          drain_reason: nil,
          generation: generation,
          snapshot_ref: nil,
          snapshot_generation: nil,
          snapshot_size_bytes: nil,
          created_at: ts,
          last_active_at: nil,
          updated_at: ts,
          terminal_reason: nil
        }

        :ets.insert(state.instances, {instance_id, instance})
        state = bump_counts(state, nil, :starting, workload)
        {:reply, {:ok, instance}, state}

      {:error, _reason} = error ->
        {:reply, error, state}
    end
  end

  # -- transition ------------------------------------------------------------

  defp do_transition(state, instance_id, event, op_kind, payload, updates) do
    with {:ok, instance} <- fetch(state, instance_id),
         {:ok, next} <- StatefulState.transition(instance.state, event) do
      case append_and_update(state, instance, op_kind, next, payload, updates) do
        {:ok, updated, state} -> {:reply, {:ok, updated}, state}
        {:error, reason} -> {:reply, {:error, reason}, state}
      end
    else
      {:error, _reason} = error -> {:reply, error, state}
    end
  end

  # Transient ETS-only FSM move (no op-log append): banking/relighting/cold_booting
  # entry markers, their aborts, and the unpublish endpoint-pull, which a later
  # completion op or adoption resolves. Keeps per-workload counts in step exactly
  # like append_and_update, minus the durable write. `updates` are extra ETS fields
  # merged after the state move (unpublish uses this to drop the endpoint + stamp
  # drain_reason).
  defp do_mark_with_updates(state, instance_id, event, updates) do
    with {:ok, instance} <- fetch(state, instance_id),
         {:ok, next} <- StatefulState.transition(instance.state, event) do
      ts = state.clock.()
      updated = instance |> Map.merge(updates) |> Map.merge(%{state: next, updated_at: ts})
      :ets.insert(state.instances, {instance_id, updated})
      state = bump_counts(state, instance.state, next, instance.workload)
      {:reply, {:ok, updated}, state}
    else
      {:error, _reason} = error -> {:reply, error, state}
    end
  end

  # The write-through core: append to the op-log, and ONLY on {:ok, seq} update
  # ETS. On append failure ETS is left untouched (as durable as the op-log agrees)
  # and the error is returned. Terminal transitions record the reason and drop the
  # endpoint fact (a terminal instance is never in the fan-out), so the publisher's
  # next flush ejects it. A `stateful_banked` (next == :banked) also clears the
  # live endpoint and stamps the bundle fields the projection recorded, so the ETS
  # row matches a boot rebuild exactly.
  defp append_and_update(state, instance, op_kind, next_state, payload, updates) do
    ts = state.clock.()

    op = %Op{
      kind: op_kind,
      tenant: instance.tenant,
      principal: instance.principal,
      workload: instance.workload,
      stateful_instance_id: instance.instance_id,
      ts: ts,
      payload: payload
    }

    case Embervm.OpLog.SQLite.append(state.op_log, op) do
      {:ok, _seq} ->
        terminal? = StatefulState.terminal?(next_state)

        terminal_reason =
          if terminal? do
            to_string(Map.get(payload, :reason, next_state))
          else
            instance.terminal_reason
          end

        base =
          instance
          |> Map.merge(updates)
          |> Map.merge(%{state: next_state, updated_at: ts, terminal_reason: terminal_reason})

        updated = post_transition_endpoint(base, next_state, terminal?)

        :ets.insert(state.instances, {instance.instance_id, updated})
        state = bump_counts(state, instance.state, next_state, instance.workload)
        {:ok, updated, state}

      {:error, reason} ->
        {:error, reason}
    end
  end

  # After a durable transition, reconcile the endpoint fact with the new state:
  #   * terminal -> no live endpoint (drop ip/port, healthy=false);
  #   * banked   -> the VM is snapshotted and destroyed, so no live endpoint either,
  #     but keep the bundle fields the caller's `updates` stamped (snapshot_*);
  #   * any other -> leave the caller's `updates` as-is (publish set ip/port/healthy).
  defp post_transition_endpoint(base, next_state, terminal?) do
    cond do
      terminal? -> %{base | healthy: false, ip: nil, port: nil}
      next_state == :banked -> %{base | healthy: false, ip: nil, port: nil}
      true -> base
    end
  end

  # -- pairing + eager eviction ----------------------------------------------

  # The pair is valid iff the workload's banked instance's snapshot_generation
  # equals the workload's volume generation. False if either the banked instance or
  # the volume row is missing, and false when the generations diverge.
  defp do_pair_valid?(state, workload) do
    banked = banked_instance(state, workload)
    volume = fetch_volume(state, workload)

    result =
      with %{snapshot_generation: sg} when is_integer(sg) <- banked,
           %{generation: vg} when is_integer(vg) <- volume do
        sg == vg
      else
        _ -> false
      end

    # TEMP diagnostic (debug/embervm-stateful-pair-logging): log the exact pair
    # inputs so a persistent false (demo-postgres never relighting) can be
    # attributed to a missing volume row vs a generation mismatch. Only when a
    # banked instance exists, so a scaled-to-zero-no-bundle workload stays quiet.
    if is_map(banked) do
      vol_gen = if is_map(volume), do: Map.get(volume, :generation), else: :no_volume_row
      vol_node = if is_map(volume), do: Map.get(volume, :node_id), else: nil

      Logger.info(
        "embervm stateful pair check " <>
          "workload=#{workload} banked_snap_gen=#{inspect(Map.get(banked, :snapshot_generation))} " <>
          "volume_gen=#{inspect(vol_gen)} volume_node=#{inspect(vol_node)} " <>
          "banked_node=#{inspect(Map.get(banked, :node_id))} pair_valid=#{result}"
      )
    end

    result
  end

  # Evict every banked instance whose pair is broken, through the DURABLE path (so
  # the op-log records the eviction and a rebuild agrees). Reason `pair_broken`.
  # Returns the evicted instance_ids. A banked instance whose volume row is missing
  # is ALSO pair-broken (no current generation to match): the general
  # do_pair_valid?/2 already returns false for that, so scanning banked instances
  # and evicting the invalid ones covers both divergence and a missing volume.
  defp do_eager_evict_broken_pairs(state) do
    banked =
      :ets.foldl(
        fn {_id, instance}, acc ->
          if instance.state == :banked, do: [instance | acc], else: acc
        end,
        [],
        state.instances
      )

    {evicted_ids, state} =
      Enum.reduce(banked, {[], state}, fn instance, {ids, acc} ->
        if do_pair_valid?(acc, instance.workload) do
          {ids, acc}
        else
          payload = %{reason: "pair_broken"}

          case append_and_update(acc, instance, :stateful_evicted, :evicted, payload, %{}) do
            {:ok, _updated, acc} -> {[instance.instance_id | ids], acc}
            # A durable append failure leaves the instance banked (as durable as the
            # op-log agrees); a later sweep retries it. Never partially evict in ETS.
            {:error, _reason} -> {ids, acc}
          end
        end
      end)

    {:reply, Enum.reverse(evicted_ids), state}
  end

  # -- counts ----------------------------------------------------------------

  # Maintain the per-workload {live, banked} counters as an instance moves between
  # buckets. `prior` nil means "entering" (rebuild/start); otherwise decrement the
  # prior bucket and increment the next. A terminal state is neither live nor
  # banked, so it decrements without a matching increment.
  defp bump_counts(state, prior, next, workload) do
    counts = Map.get(state.counts, workload, %{live: 0, banked: 0})

    counts =
      counts
      |> dec_bucket(bucket_of(prior))
      |> inc_bucket(bucket_of(next))

    %{state | counts: Map.put(state.counts, workload, counts)}
  end

  defp bucket_of(nil), do: nil
  defp bucket_of(:banked), do: :banked
  defp bucket_of(state) when state in [:starting, :serving, :banking, :checkpointed, :relighting, :cold_booting], do: :live
  defp bucket_of(_terminal), do: nil

  defp inc_bucket(counts, nil), do: counts
  defp inc_bucket(counts, bucket), do: Map.update!(counts, bucket, &(&1 + 1))

  defp dec_bucket(counts, nil), do: counts
  defp dec_bucket(counts, bucket), do: Map.update!(counts, bucket, &max(&1 - 1, 0))

  # -- helpers ---------------------------------------------------------------

  # An instance is the workload's SINGLE servable endpoint exactly when it is that
  # workload's, in the `serving` FSM state, healthy per the node probe, and carries
  # a routable {ip, port}. The publisher's endpoint-vs-activator decision hinges on
  # this, kept here so the store is the one owner of "what is in the fan-out".
  defp servable?(instance, workload) do
    instance.workload == workload and instance.state == :serving and instance.healthy and
      is_binary(instance.ip) and instance.ip != "" and is_integer(instance.port)
  end

  # Whether the workload already has a LIVE (non-terminal, non-banked) instance,
  # the singleton gate `start/2` reads. A bounded ETS scan on a cold path (start is
  # rare relative to reads), so no separate index is warranted.
  defp has_live_instance?(state, workload) do
    :ets.foldl(
      fn {_id, instance}, acc ->
        acc or (instance.workload == workload and StatefulState.live?(instance.state))
      end,
      false,
      state.instances
    )
  end

  # The workload's banked instance (there is at most one by the singleton
  # invariant), or nil. Read by the pairing check.
  defp banked_instance(state, workload) do
    :ets.foldl(
      fn {_id, instance}, acc ->
        if acc == nil and instance.workload == workload and instance.state == :banked do
          instance
        else
          acc
        end
      end,
      nil,
      state.instances
    )
  end

  defp fetch_volume(state, workload) do
    case :ets.lookup(state.volumes, workload) do
      [{^workload, volume}] -> volume
      [] -> nil
    end
  end

  defp fetch(state, instance_id) do
    case :ets.lookup(state.instances, instance_id) do
      [{^instance_id, instance}] -> {:ok, instance}
      [] -> {:error, {:not_found, instance_id}}
    end
  end

  defp get_view(state, instance_id) do
    case fetch(state, instance_id) do
      {:ok, instance} -> {:ok, instance}
      {:error, _} -> :error
    end
  end

  defp default_clock, do: System.system_time(:millisecond)
end
