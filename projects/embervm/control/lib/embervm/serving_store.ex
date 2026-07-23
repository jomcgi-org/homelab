defmodule Embervm.ServingStore do
  @moduledoc """
  ETS hot set over the op-log's durable `serving_instances` projection (R3),
  mirroring `Embervm.SessionStore`: every read the publisher and activator need
  is O(1)-to-bounded against ETS, while every durable write goes through the
  op-log FIRST and only lands in ETS once the op-log confirms it is durable. That
  ordering, "op-log append succeeds, then and only then update ETS", is the
  write-through invariant this module enforces: ETS never shows an instance in a
  state the op-log does not already agree with, and a crash between the two never
  loses a transition (worst case ETS is briefly stale until the next boot's
  rebuild replays it).

  On `init/1` this rebuilds its ETS table from
  `OpLog.load_serving_instances/1`, the recovery path: a fresh ServingStore
  against an existing op-log ends up with exactly the state the durable
  projection recorded, no replay logic beyond "read the projection". A projection
  rebuild followed by an `Embervm.EndpointPublisher` publish is therefore
  byte-identical to the pre-restart snapshot (the publisher is a pure function of
  these facts), which is the property test the plan requires. Adoption (Task 8)
  layers the NODE's reported serving inventory on top of this durable rebuild to
  heal residency and limbo states.

  ## what it owns

    * the serving hot set (`instance_id -> instance map`): the projected durable
      row plus the transient FSM state the lifecycle drives it through;
    * the publisher's FACTS: `published_endpoints/2` (the healthy, `published`
      endpoints for a workload, the exact set the publisher renders as an Envoy
      cluster's EDS assignment) and `serving_workloads/1` (every workload that has
      any live serving instance), both derived from the hot set so the publisher
      is a pure function of this store and never of the durable log.

  ## the endpoint fact and the fan-out

  An instance is in the fan-out (routable by the node Envoy) exactly when its FSM
  state is `published` AND the node's health probe reports it healthy. Health
  ejection (`set_health/3`) flips `healthy` on the ETS row WITHOUT an FSM
  transition (health is a lossy node fact, not durable lifecycle state), and the
  publisher re-derives the cluster's endpoints from the healthy-published set on
  its next flush, so an unhealthy instance simply drops out of the EDS assignment
  and a recovered one reappears. The durable `serving_published`/
  `serving_unpublished` ops are the endpoint-lifetime AUDIT record; the live
  routing fact is this ETS-derived healthy-published set.

  ## transient states + adoption

  `banking` and `relighting` are ETS-only markers (`mark/2`, no op-log append),
  exactly like the session store: a later durable completion op
  (`serving_banked`/`serving_relit`) or an adoption reconcile against the node's
  reported `serving_vms`/`serving_snapshots` resolves them. `adopt_state/3` and
  `adopt_endpoint/5` force the ETS view from authoritative node truth, bypassing
  the FSM (adoption is idempotent and total over limbo states the FSM cannot
  bridge) and never appending an op.
  """

  use GenServer

  alias Embervm.OpLog.Op
  alias Embervm.ServingState

  @instances_table :embervm_serving_instances

  # The FSM states that hold a LIVE serving VM (as opposed to a banked snapshot or
  # a terminal row). A live instance counts against per-workload live capacity and
  # is a candidate for the fan-out; `banked` holds disk, not a VM.
  @live_states [:starting, :published, :draining, :banking, :relighting]

  # -- Client API ------------------------------------------------------------

  @spec start_link(keyword()) :: GenServer.on_start()
  def start_link(opts) do
    case Keyword.get(opts, :name, __MODULE__) do
      nil -> GenServer.start_link(__MODULE__, opts)
      name -> GenServer.start_link(__MODULE__, opts, name: name)
    end
  end

  @doc """
  Starts a new serving instance, minting nothing (the caller supplies the
  `instance_id`, an activator- or adoption-derived id). `attrs` is
  `%{instance_id, tenant, principal, workload, node_id, vm_id, ip, port,
  base_snapshot_ref, base_digest}`. Appends `serving_started` (write-through),
  inserts the ETS hot-set row in `:starting` (the daemon returned a live
  `{vm_id, ip, port}` but the endpoint is not yet in the fan-out), and returns
  `{:ok, instance}` or `{:error, reason}`. The instance is NOT yet healthy or
  published; the paired `publish/6` moves it into the fan-out.
  """
  @spec start(GenServer.server(), map()) :: {:ok, map()} | {:error, term()}
  def start(store \\ __MODULE__, attrs) do
    GenServer.call(store, {:start, attrs})
  end

  @doc """
  Applies an FSM transition to an instance, appending the matching op
  write-through. `event` is a `ServingState` event; `op_kind` and `payload`
  describe the op; `updates` are extra fields merged into the ETS row AFTER the
  durable append succeeds. Terminal transitions record the reason. Returns the
  updated instance or `{:error, reason}` (including `{:illegal_transition, ...}`).
  """
  @spec transition(GenServer.server(), String.t(), atom(), atom(), map(), map()) ::
          {:ok, map()} | {:error, term()}
  def transition(store \\ __MODULE__, instance_id, event, op_kind, payload, updates) do
    GenServer.call(store, {:transition, instance_id, event, op_kind, payload, updates})
  end

  @doc """
  Convenience for the publish edge: transitions `starting`/`draining`/`published`
  -> `published` with a `serving_published` op, writing the fresh `{ip, port}`
  endpoint and marking the row healthy (the daemon health-gated readiness before
  StartServing returned, so a freshly published instance is healthy by
  construction; the node's ongoing probe can later flip it via `set_health/3`).
  `reason` is one of `:started | :relit | :healthy` (the audit reason).
  """
  @spec publish(GenServer.server(), String.t(), String.t(), non_neg_integer(), atom()) ::
          {:ok, map()} | {:error, term()}
  def publish(store \\ __MODULE__, instance_id, ip, port, reason) do
    GenServer.call(store, {:publish, instance_id, ip, port, reason})
  end

  @doc """
  Convenience for the unpublish edge: transitions `published` -> `draining` with a
  `serving_unpublished` op. `reason` is one of `:drain | :unhealthy | :banked |
  :destroyed | :failed` (the audit reason). Pulls the instance from the fan-out.
  """
  @spec unpublish(GenServer.server(), String.t(), atom()) :: {:ok, map()} | {:error, term()}
  def unpublish(store \\ __MODULE__, instance_id, reason) do
    GenServer.call(store, {:unpublish, instance_id, reason})
  end

  @doc """
  Applies a TRANSIENT FSM edge WITHOUT an op-log append: the ETS-only move into a
  mid-operation state (`banking`, `relighting`, or their `*_abort` recoveries)
  that a crash heals from node inventory rather than from the durable log. `event`
  must be a legal FSM edge from the instance's current state; an illegal edge is
  `{:error, {:illegal_transition, ...}}`.
  """
  @spec mark(GenServer.server(), String.t(), atom()) :: {:ok, map()} | {:error, term()}
  def mark(store \\ __MODULE__, instance_id, event) do
    GenServer.call(store, {:mark, instance_id, event})
  end

  @doc """
  Flips an instance's `healthy` flag from the node's probe fact (health ejection),
  WITHOUT an FSM transition or an op-log append: health is a lossy node fact, not
  durable lifecycle state. A no-op for an unknown instance. Returns the updated
  instance or `:error`. The publisher re-derives the fan-out from the
  healthy-published set on its next flush, so flipping this drops or restores the
  endpoint.
  """
  @spec set_health(GenServer.server(), String.t(), boolean()) :: {:ok, map()} | :error
  def set_health(store \\ __MODULE__, instance_id, healthy?) do
    GenServer.call(store, {:set_health, instance_id, healthy?})
  end

  @doc """
  Stamps an instance's `last_active_at` from the idle-signal sweep (Task 9), WITHOUT
  an FSM transition or an op-log append: activity is a lossy, high-frequency node
  fact (a request-count delta), not durable lifecycle state, exactly like
  `set_health/3`. A no-op for an unknown instance. Returns the updated instance or
  `:error`. Idle detection reads this back against `idleBankSeconds`; because it is
  ETS-only, a control-plane restart resets the idle clock to "just active" (the
  fresh sweep has no prior scrape to delta against anyway), which is the fail-open
  direction (never bank on a cold idle baseline).
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
  `GET /v1/serving/{workload}` management API. A bounded ETS scan.
  """
  @spec list(GenServer.server(), String.t()) :: [map()]
  def list(store \\ __MODULE__, workload) do
    GenServer.call(store, {:list, workload})
  end

  @doc """
  The publisher fact: the healthy, `published` endpoints for `workload` as a list
  of `%{ip, port}`, in a STABLE order (by instance_id) so the rendered snapshot is
  deterministic across rebuilds (the byte-identical-rebuild property). Empty when
  the workload has no healthy published instance (the publisher then swaps in the
  activator endpoint).
  """
  @spec published_endpoints(GenServer.server(), String.t()) :: [%{ip: String.t(), port: non_neg_integer()}]
  def published_endpoints(store \\ __MODULE__, workload) do
    GenServer.call(store, {:published_endpoints, workload})
  end

  @doc """
  The publisher fact: every workload that has at least one LIVE serving instance
  (any non-terminal, non-banked state), so the publisher knows which clusters to
  render. A banked-only workload has no live VM but still needs an activator
  endpoint the next miss wakes, so the publisher enumerates workloads from the
  catalog, not only from this set; this set is the "has a live instance" predicate
  the publisher uses to decide endpoints-vs-activator per workload.
  """
  @spec serving_workloads(GenServer.server()) :: [String.t()]
  def serving_workloads(store \\ __MODULE__) do
    GenServer.call(store, :serving_workloads)
  end

  @doc """
  Live and banked counts for `workload` (`%{live, banked}`), the O(1)
  scale-up/capacity read (Task 8). Live is any non-terminal, non-banked instance;
  banked is the `banked` state. Reads the maintained per-workload counter, never a
  scan.
  """
  @spec counts(GenServer.server(), String.t()) :: %{live: non_neg_integer(), banked: non_neg_integer()}
  def counts(store \\ __MODULE__, workload) do
    GenServer.call(store, {:counts, workload})
  end

  @doc """
  Adoption (Task 8): FORCE an instance's ETS state to `new_state` from
  authoritative node truth, bypassing the FSM (adoption is idempotent and derived
  from what the node actually holds, so it must be total over limbo states the FSM
  cannot bridge). Does NOT append an op. A no-op for an unknown or already-terminal
  instance (never resurrects a terminal row). Returns `:ok`.
  """
  @spec adopt_state(GenServer.server(), String.t(), :published | :starting | :banked) :: :ok
  def adopt_state(store \\ __MODULE__, instance_id, new_state) do
    GenServer.call(store, {:adopt_state, instance_id, new_state})
  end

  @doc """
  Adoption (Task 8): rebind an instance the node reports as a LIVE serving VM to
  `{node_id, vm_id, ip, port}`, writing the endpoint fact and marking it healthy
  WITHOUT an FSM transition or an op-log append (the endpoint is a lossy fact the
  node owns). A no-op for an unknown instance. Returns `:ok`.
  """
  @spec adopt_endpoint(GenServer.server(), String.t(), String.t(), String.t(), map()) :: :ok
  def adopt_endpoint(store \\ __MODULE__, instance_id, node_id, vm_id, endpoint) do
    GenServer.call(store, {:adopt_endpoint, instance_id, node_id, vm_id, endpoint})
  end

  @doc """
  Adoption of a NODE-woken serving VM (ADR embervm/018 Fork A): mint a `:published`
  instance row directly from node truth for an `origin: ACTIVATOR` VM the control
  plane has no row for (the brick minted the instance during a CP gap, so no CP row
  exists). ETS-only, like `adopt_state`/`adopt_endpoint`: the durable lifecycle ops
  are appended separately by `backfill_created/2` once the row is present. A no-op
  if a row already exists for `instance_id` (idempotent; the reconcile only calls
  this when no row claims the vm_id). `attrs` carries `:instance_id`, `:tenant`,
  `:principal`, `:workload`, `:node_id`, `:vm_id`, `:ip`, `:port`. Returns `:ok`.
  """
  @spec adopt_activator(GenServer.server(), map()) :: :ok
  def adopt_activator(store \\ __MODULE__, attrs) do
    GenServer.call(store, {:adopt_activator, attrs})
  end

  @doc """
  Re-drive the durable serving lifecycle appends (`serving_started`, then
  `serving_published` for a live-and-published instance) from the surviving ETS
  row, for an instance adopted from node truth whose ops the control plane never
  wrote (the ADR embervm/018 activator adoption, the serving analog of
  `SessionStore.backfill_created`). Appends SYNCHRONOUSLY (reconcile is not a hot
  path). Idempotent via an ETS `backfilled` flag: a re-drive after the ops have
  landed is a no-op (the serving projection's `serving_started` is a plain INSERT,
  so re-appending would collide on the instance_id UNIQUE constraint). A no-op /
  `:error` for an unknown instance.
  """
  @spec backfill_created(GenServer.server(), String.t()) :: :ok | {:error, term()}
  def backfill_created(store \\ __MODULE__, instance_id) do
    GenServer.call(store, {:backfill_created, instance_id})
  end

  # -- GenServer callbacks ---------------------------------------------------

  @impl true
  def init(opts) do
    op_log = Keyword.get(opts, :op_log, Embervm.OpLog.SQLite)
    # The backend module dispatched at every call site below, threaded alongside
    # :op_log (the server address) so a non-default backend never requires editing
    # this module. Defaults to the same SQLite module :op_log defaults to.
    op_log_mod = Keyword.get(opts, :op_log_mod, Embervm.OpLog.SQLite)
    clock = Keyword.get(opts, :clock, &default_clock/0)

    instances = :ets.new(@instances_table, [:set, :private])

    state = %{
      op_log: op_log,
      op_log_mod: op_log_mod,
      clock: clock,
      instances: instances,
      # workload -> %{live, banked}, kept in step with the hot set on every write.
      counts: %{}
    }

    case rebuild(state) do
      {:ok, state} -> {:ok, state}
      {:error, reason} -> {:stop, {:rebuild_failed, reason}}
    end
  end

  # Rebuild: read every durable serving-instance row and populate the hot set +
  # counts from scratch. No per-op replay: the projection already IS the current
  # state. `healthy` is NOT durable (it is a live node-probe fact); a rebuilt
  # `published` instance is marked healthy=true so the publisher re-publishes the
  # exact endpoint the projection recorded (byte-identical rebuild), and the node's
  # next probe (adoption/health) corrects it if it is actually unhealthy. A
  # non-published rebuilt instance is healthy=false (it is not in the fan-out
  # anyway).
  defp rebuild(state) do
    case state.op_log_mod.load_serving_instances(state.op_log) do
      {:ok, rows} ->
        state =
          Enum.reduce(rows, state, fn row, acc ->
            instance = row_to_instance(row)
            :ets.insert(acc.instances, {instance.instance_id, instance})
            bump_counts(acc, nil, instance.state, instance.workload)
          end)

        {:ok, state}

      {:error, reason} ->
        {:error, reason}
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
      # Not durable: a rebuilt published instance is assumed healthy so the boot
      # publish re-emits its recorded endpoint identically; the node probe corrects
      # a truly-unhealthy one on its next report.
      healthy: fsm_state == :published,
      # Not durable (a rebuilt draining instance is either health-ejected, which the
      # node probe re-asserts, or lost mid-bank, which adoption heals from node
      # inventory): default nil, which ServingHealth treats as republishable, the
      # safe direction. A bank-drain interrupted by a restart is re-driven by the
      # sweep, not resumed from this field.
      drain_reason: nil,
      base_snapshot_ref: row.base_snapshot_ref,
      base_digest: row.base_digest,
      generation: row.generation || 0,
      snapshot_ref: row.snapshot_ref,
      snapshot_size_bytes: row.snapshot_size_bytes,
      created_at: row.created_at,
      last_active_at: row.last_active_at,
      updated_at: row.updated_at,
      terminal_reason: row.terminal_reason
    }
  end

  # Explicit map (not String.to_existing_atom): fails loudly on an unknown string
  # and documents the exact projection strings the merged PR-1 projection writes,
  # exactly as SessionStore does. These SEVEN strings are the complete set
  # `serving_instances.state` can hold; `banking`/`relighting` are transient
  # ETS-only states never persisted, so they are absent here by construction.
  @state_strings %{
    "starting" => :starting,
    "published" => :published,
    "draining" => :draining,
    "banked" => :banked,
    "destroying" => :destroying,
    "evicted" => :evicted,
    "destroyed" => :destroyed,
    "failed" => :failed
  }

  defp state_from_string(str), do: Map.fetch!(@state_strings, str)

  @impl true
  def handle_call({:start, attrs}, _from, state) do
    do_start(attrs, state)
  end

  def handle_call({:transition, instance_id, event, op_kind, payload, updates}, _from, state) do
    do_transition(state, instance_id, event, op_kind, payload, updates)
  end

  def handle_call({:publish, instance_id, ip, port, reason}, _from, state) do
    payload = %{ip: ip, port: port, reason: to_string(reason)}
    # Publishing clears any prior drain_reason: a (re)published instance is in the
    # fan-out, not draining, so the drain-for-bank vs drain-for-health distinction
    # no longer applies until it is unpublished again.
    updates = %{ip: ip, port: port, healthy: true, drain_reason: nil}
    do_transition(state, instance_id, :publish, :serving_published, payload, updates)
  end

  def handle_call({:unpublish, instance_id, reason}, _from, state) do
    payload = %{reason: to_string(reason)}
    # Stamp the ETS row with WHY it drained (a transient, ETS-only fact, never an
    # op-log column): `:bank` when the idle-bank sweep is deliberately draining a
    # still-alive-and-healthy VM before StopServing(BANK); `:unhealthy` (or any
    # other reason) when it is health ejection or a lifecycle drain. ServingHealth's
    # republish-on-recovery branch republishes ONLY `:unhealthy` drains, so a health
    # sweep during a bank-drain never re-adds the endpoint the bank is removing.
    updates = %{healthy: false, drain_reason: drain_reason_of(reason)}
    do_transition(state, instance_id, :unpublish, :serving_unpublished, payload, updates)
  end

  def handle_call({:mark, instance_id, event}, _from, state) do
    do_mark(state, instance_id, event)
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

  def handle_call({:published_endpoints, workload}, _from, state) do
    endpoints =
      :ets.foldl(
        fn {_id, instance}, acc ->
          if publishable?(instance, workload), do: [instance | acc], else: acc
        end,
        [],
        state.instances
      )
      # Stable order by instance_id so the rendered EDS assignment is deterministic
      # across rebuilds (the byte-identical-rebuild property the publisher relies on).
      |> Enum.sort_by(& &1.instance_id)
      |> Enum.map(&%{ip: &1.ip, port: &1.port})

    {:reply, endpoints, state}
  end

  def handle_call(:serving_workloads, _from, state) do
    workloads =
      :ets.foldl(
        fn {_id, instance}, acc ->
          if instance.state in @live_states, do: MapSet.put(acc, instance.workload), else: acc
        end,
        MapSet.new(),
        state.instances
      )
      |> MapSet.to_list()

    {:reply, workloads, state}
  end

  def handle_call({:counts, workload}, _from, state) do
    {:reply, Map.get(state.counts, workload, %{live: 0, banked: 0}), state}
  end

  def handle_call({:adopt_state, instance_id, new_state}, _from, state)
      when new_state in [:published, :starting, :banked] do
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

  def handle_call({:adopt_activator, attrs}, _from, state) do
    instance_id = Map.fetch!(attrs, :instance_id)

    case fetch(state, instance_id) do
      # A row already exists (a prior pass minted it): the normal adopt_live path
      # heals it from here, so this is a no-op. Keeps the mint strictly once-only.
      {:ok, _instance} ->
        {:reply, :ok, state}

      {:error, _} ->
        ts = state.clock.()
        # Mint the row already :published from node truth: the brick woke the VM and
        # reports it live, so it is in the fan-out immediately (the ETS row is what
        # published_endpoints reads). Health is taken from the node fact, not assumed:
        # a VM the brick reports unhealthy is minted unhealthy and stays out of the
        # fan-out (publishable? requires healthy) until adopt_live heals it, rather
        # than routing traffic to a failing guest. backfilled=false gates the durable
        # backfill so it appends exactly once. backfill_created writes the durable
        # serving_started/serving_published ops from this row on the same pass.
        instance = %{
          instance_id: instance_id,
          tenant: Map.fetch!(attrs, :tenant),
          principal: Map.get(attrs, :principal),
          workload: Map.fetch!(attrs, :workload),
          state: :published,
          node_id: Map.get(attrs, :node_id),
          vm_id: Map.get(attrs, :vm_id),
          ip: Map.get(attrs, :ip),
          port: Map.get(attrs, :port),
          healthy: Map.get(attrs, :healthy, true),
          drain_reason: nil,
          base_snapshot_ref: nil,
          base_digest: nil,
          generation: 0,
          snapshot_ref: nil,
          snapshot_size_bytes: nil,
          backfilled: false,
          created_at: ts,
          last_active_at: nil,
          updated_at: ts,
          terminal_reason: nil
        }

        :ets.insert(state.instances, {instance_id, instance})
        state = bump_counts(state, nil, :published, instance.workload)
        {:reply, :ok, state}
    end
  end

  def handle_call({:backfill_created, instance_id}, _from, state) do
    case fetch(state, instance_id) do
      # Already backfilled: a no-op. The serving projection's serving_started is a
      # plain INSERT (unlike the session lane's INSERT OR IGNORE), so re-appending
      # would collide on the instance_id UNIQUE constraint; this flag is what makes a
      # re-drive idempotent instead. (In the reconcile flow the mint-once structure
      # already calls this once per instance; the flag guards a defensive re-drive.)
      {:ok, %{backfilled: true}} ->
        {:reply, :ok, state}

      {:ok, instance} ->
        # serving_started reconstructs the lifecycle birth op; serving_published
        # follows it when the row is live-and-published (the activator case always is).
        started = %Op{
          kind: :serving_started,
          tenant: instance.tenant,
          principal: instance.principal,
          workload: instance.workload,
          serving_instance_id: instance_id,
          ts: state.clock.(),
          payload: %{
            node_id: instance.node_id,
            vm_id: instance.vm_id,
            ip: instance.ip,
            port: instance.port,
            base_snapshot_ref: instance.base_snapshot_ref,
            base_digest: instance.base_digest,
            state: to_string(instance.state)
          }
        }

        result =
          with {:ok, _seq} <- state.op_log_mod.append(state.op_log, started) do
            maybe_backfill_published(state, instance)
          end

        case result do
          {:error, _} = error ->
            {:reply, backfill_reply(error), state}

          ok ->
            # Both durable ops landed: mark the row so a re-drive is a no-op. The flag
            # is ETS-only; on a CP restart the projection rebuilds a durable row and
            # the reconcile finds it (never re-minting), so backfill is never re-driven
            # against an already-durable instance across a restart either.
            :ets.insert(state.instances, {instance_id, Map.put(instance, :backfilled, true)})
            {:reply, backfill_reply(ok), state}
        end

      {:error, _reason} = error ->
        {:reply, error, state}
    end
  end

  # -- start -----------------------------------------------------------------

  defp do_start(attrs, state) do
    ts = state.clock.()
    instance_id = Map.fetch!(attrs, :instance_id)

    payload = %{
      node_id: Map.get(attrs, :node_id),
      vm_id: Map.get(attrs, :vm_id),
      ip: Map.get(attrs, :ip),
      port: Map.get(attrs, :port),
      base_snapshot_ref: Map.get(attrs, :base_snapshot_ref),
      base_digest: Map.get(attrs, :base_digest),
      # The projection defaults serving_started to "starting" (the endpoint is not
      # yet in the fan-out); recorded explicitly for clarity.
      state: "starting"
    }

    op = %Op{
      kind: :serving_started,
      tenant: Map.fetch!(attrs, :tenant),
      principal: Map.get(attrs, :principal),
      workload: Map.fetch!(attrs, :workload),
      serving_instance_id: instance_id,
      ts: ts,
      payload: payload
    }

    case state.op_log_mod.append(state.op_log, op) do
      {:ok, _seq} ->
        instance = %{
          instance_id: instance_id,
          tenant: op.tenant,
          principal: op.principal,
          workload: op.workload,
          state: :starting,
          node_id: Map.get(attrs, :node_id),
          vm_id: Map.get(attrs, :vm_id),
          ip: Map.get(attrs, :ip),
          port: Map.get(attrs, :port),
          # Not yet in the fan-out: healthy is meaningful only once published.
          healthy: false,
          # Transient (ETS-only) drain distinction, set on unpublish; nil until then.
          drain_reason: nil,
          base_snapshot_ref: Map.get(attrs, :base_snapshot_ref),
          base_digest: Map.get(attrs, :base_digest),
          generation: 0,
          snapshot_ref: nil,
          snapshot_size_bytes: nil,
          created_at: ts,
          last_active_at: nil,
          updated_at: ts,
          terminal_reason: nil
        }

        :ets.insert(state.instances, {instance_id, instance})
        state = bump_counts(state, nil, :starting, instance.workload)
        {:reply, {:ok, instance}, state}

      {:error, _reason} = error ->
        {:reply, error, state}
    end
  end

  # -- transition ------------------------------------------------------------

  defp do_transition(state, instance_id, event, op_kind, payload, updates) do
    with {:ok, instance} <- fetch(state, instance_id),
         {:ok, next} <- ServingState.transition(instance.state, event) do
      case append_and_update(state, instance, op_kind, next, payload, updates) do
        {:ok, updated, state} -> {:reply, {:ok, updated}, state}
        {:error, reason} -> {:reply, {:error, reason}, state}
      end
    else
      {:error, _reason} = error -> {:reply, error, state}
    end
  end

  # Transient ETS-only FSM move (no op-log append): banking/relighting entry
  # markers and their aborts a later completion op or adoption resolves. Keeps
  # per-workload counts in step exactly like append_and_update, minus the durable
  # write. Entering `banking` from `draining` keeps the instance out of the
  # fan-out (it was already unpublished); a `relight`/`relight_abort` toggles
  # banked<->relighting (neither in the fan-out).
  defp do_mark(state, instance_id, event) do
    with {:ok, instance} <- fetch(state, instance_id),
         {:ok, next} <- ServingState.transition(instance.state, event) do
      ts = state.clock.()
      updated = %{instance | state: next, updated_at: ts}
      :ets.insert(state.instances, {instance_id, updated})
      state = bump_counts(state, instance.state, next, instance.workload)
      {:reply, {:ok, updated}, state}
    else
      {:error, _reason} = error -> {:reply, error, state}
    end
  end

  # Append the durable serving_published for an adopted, already-published instance
  # (ADR embervm/018 activator backfill). A row that is not published (defensive:
  # the activator case always is) skips the second append and reports ok on the
  # serving_started already written. reason "adopted" names why the endpoint entered
  # the fan-out late.
  defp maybe_backfill_published(state, %{state: :published} = instance) do
    published = %Op{
      kind: :serving_published,
      tenant: instance.tenant,
      principal: instance.principal,
      workload: instance.workload,
      serving_instance_id: instance.instance_id,
      ts: state.clock.(),
      payload: %{ip: instance.ip, port: instance.port, reason: "adopted"}
    }

    state.op_log_mod.append(state.op_log, published)
  end

  defp maybe_backfill_published(_state, _instance), do: {:ok, :skipped}

  defp backfill_reply({:ok, _seq}), do: :ok
  defp backfill_reply({:error, _reason} = error), do: error

  # The write-through core: append to the op-log, and ONLY on {:ok, seq} update
  # ETS. On append failure ETS is left untouched (as durable as the op-log agrees)
  # and the error is returned. Terminal transitions record the reason. A terminal
  # transition also drops the endpoint fact (a terminal instance is never in the
  # fan-out), so the publisher's next flush ejects it.
  defp append_and_update(state, instance, op_kind, next_state, payload, updates) do
    ts = state.clock.()

    op = %Op{
      kind: op_kind,
      tenant: instance.tenant,
      principal: instance.principal,
      workload: instance.workload,
      serving_instance_id: instance.instance_id,
      ts: ts,
      payload: payload
    }

    case state.op_log_mod.append(state.op_log, op) do
      {:ok, _seq} ->
        terminal? = ServingState.terminal?(next_state)

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

        # A terminal instance holds no live endpoint: drop it from the fan-out fact.
        updated =
          if terminal? do
            %{base | healthy: false, ip: nil, port: nil}
          else
            base
          end

        :ets.insert(state.instances, {instance.instance_id, updated})
        state = bump_counts(state, instance.state, next_state, instance.workload)
        {:ok, updated, state}

      {:error, reason} ->
        {:error, reason}
    end
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
  defp bucket_of(state) when state in @live_states, do: :live
  defp bucket_of(_terminal), do: nil

  defp inc_bucket(counts, nil), do: counts
  defp inc_bucket(counts, bucket), do: Map.update!(counts, bucket, &(&1 + 1))

  defp dec_bucket(counts, nil), do: counts
  defp dec_bucket(counts, bucket), do: Map.update!(counts, bucket, &max(&1 - 1, 0))

  # -- helpers ---------------------------------------------------------------

  # An instance contributes an endpoint to the fan-out for `workload` exactly when
  # it is that workload's, in the `published` FSM state, healthy per the node
  # probe, and carries a routable {ip, port}. This is the single predicate the
  # publisher's cluster-vs-activator decision hinges on, kept here so the store is
  # the one owner of "what is in the fan-out".
  # Map the unpublish audit reason to the transient drain_reason ServingHealth
  # keys its republish decision on: only `:bank` marks a drain the idle-bank sweep
  # owns (a still-live VM being drained before StopServing(BANK)); every other
  # reason (`:unhealthy`, `:drain`, ...) is a health/lifecycle drain that a health
  # recovery MAY republish. Keeping this an explicit map (not passthrough) means a
  # future reason is treated as republishable-on-recovery by default, which is the
  # safe direction (a stuck endpoint is re-added, never silently banked).
  defp drain_reason_of(:bank), do: :bank
  defp drain_reason_of(_other), do: :unhealthy

  defp publishable?(instance, workload) do
    instance.workload == workload and instance.state == :published and instance.healthy and
      is_binary(instance.ip) and instance.ip != "" and is_integer(instance.port)
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
