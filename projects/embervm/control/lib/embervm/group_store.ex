defmodule Embervm.GroupStore do
  @moduledoc """
  ETS hot set over the op-log's durable `group_instances` + `group_members`
  projections (R5), the composite-group generalization of `Embervm.StatefulStore`:
  every read the publisher, the GroupManager, and the management API need is
  O(1)-to-bounded against ETS, while every durable write goes through the op-log
  FIRST and only lands in ETS once the op-log confirms it is durable. That
  ordering, "op-log append succeeds, then and only then update ETS", is the
  write-through invariant this module enforces: ETS never shows a group in a state
  the op-log does not already agree with, and a crash between the two never loses a
  transition (worst case ETS is briefly stale until the next boot's rebuild replays
  it).

  ## the two tables

  This is the FIRST multi-row-per-instance store (serving/stateful are one row per
  instance): a group instance has ONE `@instances_table` row (by `instance_id`) and
  N `@members_table` rows (by `{instance_id, member_name}`), matching the durable
  projection shape. On `init/1` this rebuilds both from
  `OpLog.load_group_instances/1` and `OpLog.load_group_members/1`, the recovery
  path: a fresh GroupStore against an existing op-log ends up with exactly the state
  the durable projections recorded, no replay logic beyond "read the projection". A
  projection rebuild followed by an `Embervm.EndpointPublisher` publish is therefore
  byte-identical to the pre-restart snapshot (the publisher is a pure function of
  these facts). Adoption (Task 7) layers the NODE's reported group inventory on top
  of this durable rebuild to heal residency and limbo states.

  ## the singleton invariant

  A composite workload is a GROUP-LEVEL singleton (mirroring stateful decision 3):
  at most ONE live group INSTANCE per composite CR. `create/2` enforces this at the
  write boundary: if a live (non-terminal, non-banked) instance already exists for
  the workload, it returns `{:error, :already_live}` WITHOUT appending an op, so the
  durable log never records two concurrent live group boots for one workload. A
  `banked` instance is NOT live (it holds a snapshot set, not VMs), so a workload
  with only a banked instance can still `create` a fresh cold boot (the wake path).

  ## degraded is a FLAG, not a state

  A member falling unhealthy while the group stays up is crash-consistency per-VM
  (never across members): the group stays `running` and the instance carries a
  `degraded_member` field naming the dead member (nil when whole). `set_member_health/4`
  flips a member row's `healthy` and recomputes the flag; `degraded?/2` reads it.
  The FSM has no `degraded` node (see `Embervm.GroupState`); the `"degraded"`
  projection string rebuilds as `running` and the flag is reconstructed from the
  member rows' health.

  ## the entry endpoint fact

  A composite group contributes the workload's SINGLE L4 entry endpoint exactly
  when its FSM state is `running` AND the entry member is healthy AND the entry's
  published `{ip, port}` is recorded. Because the class is a group-level singleton,
  `entry_endpoint/2` returns ONE `%{ip, port}` (or nil), NOT a list. The entry
  endpoint is the entry member's DNAT projection (`{node pod IP, vmPort}`, the
  D-R3.11.4 lane the noded `EnsureEntryDNAT` installs), recorded by the GroupManager
  when the group publishes; nil is the signal the publisher swaps in the activator
  endpoint.

  ## set completeness is a DERIVED fact (eager eviction)

  A banked set is COMPLETE iff the node reports a bundle for EVERY member of the
  group (each expected member_name present in the reported set). Completeness is
  recomputed on every NodeStatus sweep, never discovered lazily at wake:
  `evict_partial_sets/2` takes the node-reported bundle sets, and for each `banked`
  instance whose reported set is missing any member's bundle, evicts it eagerly
  through the DURABLE path (`group_set_evicted` reason `partial_set`), clearing its
  `set_id` so the next wake fresh-boots. This is the primitive; Task 7 owns the
  sweep cadence that feeds it node facts.

  ## transient states + adoption

  `banking`, `relighting`, and `fresh_booting` are ETS-only markers (`mark/2`, no
  op-log append): a later durable completion op (`group_banked` / `group_relit` /
  `group_fresh_booted`) or an adoption reconcile (Task 7) resolves them.
  `adopt_state/3` forces the ETS view from authoritative node truth, bypassing the
  FSM (adoption is idempotent and total over limbo states the FSM cannot bridge) and
  never appending an op.
  """

  use GenServer

  alias Embervm.OpLog.Op
  alias Embervm.GroupState

  @instances_table :embervm_group_instances
  @members_table :embervm_group_members

  # -- Client API ------------------------------------------------------------

  @spec start_link(keyword()) :: GenServer.on_start()
  def start_link(opts) do
    case Keyword.get(opts, :name, __MODULE__) do
      nil -> GenServer.start_link(__MODULE__, opts)
      name -> GenServer.start_link(__MODULE__, opts, name: name)
    end
  end

  @doc """
  Creates a new group instance (a fresh first boot of the whole group). `attrs` is
  `%{instance_id, tenant, principal, workload, node_id, subnet_cidr, entry_member,
  entry_port, listen_port, secret}` (the `secret` is the minted-or-sourced
  EMBER_GROUP_SECRET recorded in the create op so the log alone reconstructs it).
  Appends `group_created` (write-through), inserts the ETS instance row in
  `:creating`, and returns `{:ok, instance}`.

  Enforces the SINGLETON invariant: if a live (non-terminal, non-banked) instance
  already exists for the workload, returns `{:error, :already_live}` WITHOUT
  appending an op. A workload with only a banked instance is NOT blocked.
  """
  @spec create(GenServer.server(), map()) :: {:ok, map()} | {:error, term()}
  def create(store \\ __MODULE__, attrs) do
    GenServer.call(store, {:create, attrs})
  end

  @doc """
  Records the group's private subnet is up: appends `group_net_created` (write-
  through) carrying the `subnet_cidr` and upserts the instance's ETS `subnet_cidr`.
  Idempotent-ish: a re-record upserts the field. Returns `{:ok, instance}` or an
  error.
  """
  @spec net_created(GenServer.server(), String.t(), String.t()) :: {:ok, map()} | {:error, term()}
  def net_created(store \\ __MODULE__, instance_id, subnet_cidr) do
    GenServer.call(store, {:net_created, instance_id, subnet_cidr})
  end

  @doc """
  Records one member VM came up (write-through `group_member_started`): upserts the
  member's ETS row (member_name, member_index, vm_id, ip) in `:starting`, not yet
  healthy. Returns `{:ok, member}` or an error. A member for an unknown instance is
  `{:error, {:not_found, instance_id}}`.
  """
  @spec member_started(GenServer.server(), String.t(), map()) :: {:ok, map()} | {:error, term()}
  def member_started(store \\ __MODULE__, instance_id, fields) do
    GenServer.call(store, {:member_started, instance_id, fields})
  end

  @doc """
  Applies an FSM transition to a group instance, appending the matching op write-
  through. `event` is a `GroupState` event; `op_kind` and `payload` describe the op;
  `updates` are extra fields merged into the ETS instance row AFTER the durable
  append succeeds. Terminal transitions record the reason. Returns the updated
  instance or `{:error, reason}` (including `{:illegal_transition, ...}`). Mirrors
  `StatefulStore.transition/6`.
  """
  @spec transition(GenServer.server(), String.t(), atom(), atom(), map(), map()) ::
          {:ok, map()} | {:error, term()}
  def transition(store \\ __MODULE__, instance_id, event, op_kind, payload, updates) do
    GenServer.call(store, {:transition, instance_id, event, op_kind, payload, updates})
  end

  @doc """
  The whole-group readiness edge: transitions `creating` -> `running` with a
  `group_running` op, marking every member healthy and recording the entry's
  published `{ip, port}` L4 endpoint on the instance (the entry member is
  health-gated before the group publishes, so a freshly-published group's entry is
  healthy by construction; a member falling unhealthy later flips it via
  `set_member_health/4` + the degraded flag). Clears any degraded flag.
  """
  @spec publish(GenServer.server(), String.t(), String.t(), non_neg_integer()) ::
          {:ok, map()} | {:error, term()}
  def publish(store \\ __MODULE__, instance_id, entry_ip, entry_port) do
    GenServer.call(store, {:publish, instance_id, entry_ip, entry_port})
  end

  @doc """
  Records the whole set was banked (write-through `group_banked`): transitions
  `banking` -> `banked`, stamps `set_id` on the instance and each member's
  `snapshot_ref` from `members` (a list of `%{name, snapshot_ref}`) ATOMICALLY in
  one append (decision 3), and drops the entry endpoint (the VMs are gone). Returns
  the updated instance or `{:error, reason}`.
  """
  @spec bank_ready(GenServer.server(), String.t(), String.t(), [map()]) ::
          {:ok, map()} | {:error, term()}
  def bank_ready(store \\ __MODULE__, instance_id, set_id, members) do
    GenServer.call(store, {:bank_ready, instance_id, set_id, members})
  end

  @doc """
  Applies a TRANSIENT FSM edge WITHOUT an op-log append: the ETS-only move into a
  mid-operation state (`banking`, `relighting`, `fresh_booting`, or their `*_abort`
  recoveries) that a crash heals from node inventory rather than from the durable
  log. `event` must be a legal FSM edge from the instance's current state; an
  illegal edge is `{:error, {:illegal_transition, ...}}`.
  """
  @spec mark(GenServer.server(), String.t(), atom()) :: {:ok, map()} | {:error, term()}
  def mark(store \\ __MODULE__, instance_id, event) do
    GenServer.call(store, {:mark, instance_id, event})
  end

  @doc """
  Flips one member's `healthy` flag from the node's probe fact (health ejection),
  WITHOUT an FSM transition or an op-log append (health is a lossy node fact). Also
  recomputes the instance's `degraded_member` flag: if the flipped member is the
  only unhealthy one it names it; whole again clears it. A no-op for an unknown
  member. Returns the updated instance or `:error`.
  """
  @spec set_member_health(GenServer.server(), String.t(), String.t(), boolean()) ::
          {:ok, map()} | :error
  def set_member_health(store \\ __MODULE__, instance_id, member_name, healthy?) do
    GenServer.call(store, {:set_member_health, instance_id, member_name, healthy?})
  end

  @doc """
  Touch a group instance's `last_active_at` (and `updated_at`) to `ts`, WITHOUT an
  FSM transition or an op-log append (activity is a live node-Envoy stats fact, not a
  durable lifecycle event). `Embervm.GroupSweeper` calls this on every tick a
  workload's entry listener showed connection activity, so the banked-TTL and
  idle-age baselines measure from the last real traffic. A no-op for an unknown
  instance. Returns the updated instance or `:error`. Mirrors
  `StatefulStore.touch_active/3`.
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

  @doc "Every member row of a group instance, member-index order."
  @spec members(GenServer.server(), String.t()) :: [map()]
  def members(store \\ __MODULE__, instance_id) do
    GenServer.call(store, {:members, instance_id})
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
  `GET /v1/groups/{workload}` management API. A bounded ETS scan. Because the class
  is a group-level singleton there is at most one LIVE instance, but a workload can
  carry a live instance plus terminal history, so this returns a list.
  """
  @spec list(GenServer.server(), String.t()) :: [map()]
  def list(store \\ __MODULE__, workload) do
    GenServer.call(store, {:list, workload})
  end

  @doc """
  The publisher fact: the workload's SINGLE live entry endpoint as `%{ip, port}`,
  or nil when the workload has no `running` group with a healthy entry member and a
  recorded entry endpoint. nil is the signal the publisher swaps in the activator
  endpoint (or, when no activator is configured, skips the workload entirely).
  """
  @spec entry_endpoint(GenServer.server(), String.t()) ::
          %{ip: String.t(), port: non_neg_integer()} | nil
  def entry_endpoint(store \\ __MODULE__, workload) do
    GenServer.call(store, {:entry_endpoint, workload})
  end

  @doc """
  Whether the workload's running group is DEGRADED (carrying a dead member flag),
  and which member. Returns `{true, member_name}` or `false`. Read by the
  management API and the degraded-group alert path.
  """
  @spec degraded?(GenServer.server(), String.t()) :: {true, String.t()} | false
  def degraded?(store \\ __MODULE__, workload) do
    GenServer.call(store, {:degraded, workload})
  end

  @doc """
  The set of /24 subnet CIDRs currently held by a LIVE-or-BANKED group instance
  (an instance still owning its subnet). The lowest-free allocator reads this to
  pick a /24 not in use; a terminal instance's subnet is freed (its
  `group_net_deleted`/terminal op cleared `subnet_cidr`, and a terminal instance is
  excluded here regardless).
  """
  @spec held_subnets(GenServer.server()) :: MapSet.t(String.t())
  def held_subnets(store \\ __MODULE__) do
    GenServer.call(store, :held_subnets)
  end

  @doc """
  Adoption: FORCE an instance's ETS state to `new_state` from authoritative node
  truth, bypassing the FSM (adoption is idempotent and derived from what the node
  actually holds, so it must be total over limbo states the FSM cannot bridge).
  Does NOT append an op. A no-op for an unknown or already-terminal instance (never
  resurrects a terminal row). Returns `:ok`. Task 7 drives this from the reconcile.
  """
  @spec adopt_state(GenServer.server(), String.t(), :running | :creating | :banked) :: :ok
  def adopt_state(store \\ __MODULE__, instance_id, new_state) do
    GenServer.call(store, {:adopt_state, instance_id, new_state})
  end

  @doc """
  Adoption: FORCE an instance's published entry endpoint (`entry_ip` /
  `entry_port_published`) from authoritative node truth, bypassing the FSM and NOT
  appending an op (the ETS-force sibling of `adopt_state/3`, mirroring
  `StatefulStore.adopt_endpoint`). The reconcile re-derives the DNAT `{pod_ip,
  vm_port}` the LIVE publish recorded (which the op-log projection does not persist a
  column for; a rebuild reconstructs the fallback `{entry tap IP, entry guest port}`
  instead) and forces it here so a control-plane restart republishes the IDENTICAL
  endpoint. A no-op for an unknown or terminal instance. Returns `:ok`.
  """
  @spec adopt_endpoint(GenServer.server(), String.t(), String.t(), non_neg_integer()) :: :ok
  def adopt_endpoint(store \\ __MODULE__, instance_id, entry_ip, entry_port) do
    GenServer.call(store, {:adopt_endpoint, instance_id, entry_ip, entry_port})
  end

  @doc """
  Set-completeness sweep PRIMITIVE (eager eviction). `reported_sets` maps
  `instance_id -> MapSet` of member_names the node reports a bundle for. For every
  `banked` instance whose reported set is missing ANY of its group's member names
  (a PARTIAL set), evict it through the DURABLE path (a `group_set_evicted` op with
  reason `partial_set`), clearing `set_id`. A banked instance with NO reported set
  at all (absent from `reported_sets`) is also partial (nothing to relight). Returns
  the list of evicted instance_ids. This is a primitive ONLY: Task 7 owns the sweep
  cadence + the node-fact plumbing that calls it.
  """
  @spec evict_partial_sets(GenServer.server(), %{optional(String.t()) => MapSet.t(String.t())}) ::
          [String.t()]
  def evict_partial_sets(store \\ __MODULE__, reported_sets) do
    GenServer.call(store, {:evict_partial_sets, reported_sets})
  end

  @doc """
  Evict ONE instance's banked set through the durable path (a `group_set_evicted`
  op, `reason`), clearing its `set_id` and each member row's `snapshot_ref` (the
  warmth is gone). NOT an FSM transition (the instance's lifecycle move to
  `fresh_booting` is a separate `mark/2`); this is the single-instance sibling of
  `evict_partial_sets/2`, used by the wake path's relight -> fresh fallback so a
  rebuild agrees the set is spent. A no-op for an unknown instance. Returns `:ok`.
  """
  @spec evict_set(GenServer.server(), String.t(), String.t()) :: :ok
  def evict_set(store \\ __MODULE__, instance_id, reason) do
    GenServer.call(store, {:evict_set, instance_id, reason})
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
    members = :ets.new(@members_table, [:set, :private])

    state = %{
      op_log: op_log,
      op_log_mod: op_log_mod,
      clock: clock,
      instances: instances,
      members: members
    }

    case rebuild(state) do
      {:ok, state} -> {:ok, state}
      {:error, reason} -> {:stop, {:rebuild_failed, reason}}
    end
  end

  # Rebuild: read every durable group-instance + member row and populate the two
  # hot sets from scratch. No per-op replay: the projections already ARE the current
  # state. `healthy` is NOT durable on the instance (it is a live node-probe fact);
  # a rebuilt `running` instance's members keep their projected `healthy` so the
  # publisher re-derives the exact entry endpoint the projection recorded
  # (byte-identical rebuild), and the node's next probe corrects a stale flag.
  defp rebuild(state) do
    with {:ok, instance_rows} <- state.op_log_mod.load_group_instances(state.op_log),
         {:ok, member_rows} <- state.op_log_mod.load_group_members(state.op_log) do
      Enum.each(member_rows, fn row ->
        member = row_to_member(row)
        :ets.insert(state.members, {{row.instance_id, row.member_name}, member})
      end)

      Enum.each(instance_rows, fn row ->
        instance = row_to_instance(row, member_rows)
        :ets.insert(state.instances, {instance.instance_id, instance})
      end)

      {:ok, state}
    end
  end

  defp row_to_instance(row, member_rows) do
    fsm_state = state_from_string(row.state)

    # The degraded flag is reconstructed from the member rows (health is lossy, not
    # a durable instance column): the projected "degraded" state, or any unhealthy
    # member on a running instance, names the dead member. On rebuild the entry
    # endpoint is re-derived from the running-and-published fact + the entry member's
    # row; a rebuilt running instance keeps its recorded listen_port so the publisher
    # re-emits identically until the node's next report.
    degraded_member =
      if fsm_state == :running do
        member_rows
        |> Enum.filter(&(&1.instance_id == row.instance_id and &1.healthy == false))
        |> Enum.map(& &1.member_name)
        |> List.first()
      else
        nil
      end

    entry_member_row =
      Enum.find(member_rows, &(&1.instance_id == row.instance_id and &1.member_name == row.entry_member))

    %{
      instance_id: row.instance_id,
      tenant: row.tenant,
      principal: row.principal,
      workload: row.workload,
      state: fsm_state,
      node_id: row.node_id,
      subnet_cidr: row.subnet_cidr,
      entry_member: row.entry_member,
      entry_port: row.entry_port,
      listen_port: row.listen_port,
      set_id: row.set_id,
      # The published entry endpoint on rebuild is reconstructed from the durable
      # facts alone: the entry member's tap IP (durable in its member row) and the
      # group's entry.port. This is the FALLBACK reconstruction (the LIVE publish
      # records the DNAT {pod IP, vmPort} the node Envoy dials, which the group
      # projection does not persist a column for); Task 7's adoption reconcile
      # re-derives + re-publishes the DNAT endpoint on the next sweep, overwriting
      # these same fields, exactly the stateful adoption posture. A non-running
      # instance has no live entry endpoint.
      entry_ip:
        if(fsm_state == :running and entry_member_row, do: entry_member_row.ip, else: nil),
      entry_port_published: if(fsm_state == :running, do: row.entry_port, else: nil),
      degraded_member: degraded_member,
      created_at: row.created_at,
      last_active_at: row.last_active_at,
      updated_at: row.updated_at,
      terminal_reason: row.terminal_reason
    }
  end

  defp row_to_member(row) do
    %{
      instance_id: row.instance_id,
      member_name: row.member_name,
      member_index: row.member_index,
      vm_id: row.vm_id,
      ip: row.ip,
      state: row.state,
      snapshot_ref: row.snapshot_ref,
      # Not durable in intent (a live node-probe fact), but the projection persists
      # the last-known flag; keep it so a rebuilt group reproduces its degraded flag.
      healthy: row.healthy,
      updated_at: row.updated_at
    }
  end

  # Explicit map (not String.to_existing_atom): fails loudly on an unknown string
  # and documents the exact projection strings the merged PR-1 projection writes.
  # These strings are the complete set `group_instances.state` can hold;
  # `banking`/`relighting`/`fresh_booting` are transient ETS-only states never
  # persisted, so they are absent here. `"starting"` is the projection's name for
  # the FSM's `:creating` node (create/relight/fresh-boot all project "starting");
  # `"degraded"` is a running instance carrying the degraded flag, so it rebuilds as
  # `:running` and the flag is reconstructed from the unhealthy member rows.
  @state_strings %{
    "starting" => :creating,
    "running" => :running,
    "degraded" => :running,
    "banked" => :banked,
    "destroying" => :destroying,
    "destroyed" => :destroyed,
    "failed" => :failed
  }

  defp state_from_string(str), do: Map.fetch!(@state_strings, str)

  @impl true
  def handle_call({:create, attrs}, _from, state) do
    do_create(attrs, state)
  end

  def handle_call({:net_created, instance_id, subnet_cidr}, _from, state) do
    with {:ok, instance} <- fetch(state, instance_id) do
      op = %Op{
        kind: :group_net_created,
        tenant: instance.tenant,
        principal: instance.principal,
        workload: instance.workload,
        group_instance_id: instance_id,
        ts: state.clock.(),
        payload: %{subnet_cidr: subnet_cidr}
      }

      case state.op_log_mod.append(state.op_log, op) do
        {:ok, _seq} ->
          updated = %{instance | subnet_cidr: subnet_cidr, updated_at: op.ts}
          :ets.insert(state.instances, {instance_id, updated})
          {:reply, {:ok, updated}, state}

        {:error, _reason} = error ->
          {:reply, error, state}
      end
    else
      {:error, _reason} = error -> {:reply, error, state}
    end
  end

  def handle_call({:member_started, instance_id, fields}, _from, state) do
    do_member_started(state, instance_id, fields)
  end

  def handle_call({:transition, instance_id, event, op_kind, payload, updates}, _from, state) do
    do_transition(state, instance_id, event, op_kind, payload, updates)
  end

  def handle_call({:publish, instance_id, entry_ip, entry_port}, _from, state) do
    payload = %{listen_port: entry_port}

    updates = %{
      entry_ip: entry_ip,
      entry_port_published: entry_port,
      listen_port: entry_port,
      # A (re)published group is running and whole: clear any degraded flag (a member
      # that came back up on a relight is healthy again).
      degraded_member: nil
    }

    case do_transition(state, instance_id, :publish, :group_running, payload, updates) do
      {:reply, {:ok, instance}, state} ->
        # Mark every member row healthy (the group_running readiness edge): the
        # publish is gated on every member being health-checked, mirroring the
        # projection's mark_all_members_healthy.
        state = mark_all_members_healthy(state, instance_id, true)
        {:reply, {:ok, instance}, state}

      other ->
        other
    end
  end

  def handle_call({:bank_ready, instance_id, set_id, members}, _from, state) do
    payload = %{set_id: set_id, members: members}
    updates = %{set_id: set_id, entry_ip: nil, entry_port_published: nil, degraded_member: nil}

    case do_transition(state, instance_id, :bank_ready, :group_banked, payload, updates) do
      {:reply, {:ok, instance}, state} ->
        # Stamp each member's snapshot_ref and clear its live VM facts (the VMs are
        # gone), mirroring the projection's bank_group_members.
        state = bank_members(state, instance_id, members)
        {:reply, {:ok, instance}, state}

      other ->
        other
    end
  end

  def handle_call({:mark, instance_id, event}, _from, state) do
    do_mark(state, instance_id, event)
  end

  def handle_call({:set_member_health, instance_id, member_name, healthy?}, _from, state) do
    do_set_member_health(state, instance_id, member_name, healthy?)
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

  def handle_call({:members, instance_id}, _from, state) do
    {:reply, member_list(state, instance_id), state}
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

  def handle_call({:entry_endpoint, workload}, _from, state) do
    endpoint =
      :ets.foldl(
        fn {_id, instance}, acc ->
          if acc == nil and entry_servable?(instance, workload) do
            %{ip: instance.entry_ip, port: instance.entry_port_published}
          else
            acc
          end
        end,
        nil,
        state.instances
      )

    {:reply, endpoint, state}
  end

  def handle_call({:degraded, workload}, _from, state) do
    reply =
      :ets.foldl(
        fn {_id, instance}, acc ->
          cond do
            acc != false ->
              acc

            instance.workload == workload and instance.state == :running and
                is_binary(instance.degraded_member) ->
              {true, instance.degraded_member}

            true ->
              acc
          end
        end,
        false,
        state.instances
      )

    {:reply, reply, state}
  end

  def handle_call(:held_subnets, _from, state) do
    held =
      :ets.foldl(
        fn {_id, instance}, acc ->
          if not GroupState.terminal?(instance.state) and is_binary(instance.subnet_cidr) and
               instance.subnet_cidr != "" do
            MapSet.put(acc, instance.subnet_cidr)
          else
            acc
          end
        end,
        MapSet.new(),
        state.instances
      )

    {:reply, held, state}
  end

  def handle_call({:adopt_state, instance_id, new_state}, _from, state)
      when new_state in [:running, :creating, :banked] do
    case fetch(state, instance_id) do
      {:ok, %{state: cur}} when cur in [:destroyed, :failed] ->
        # Never resurrect a terminal instance from a stale node fact.
        {:reply, :ok, state}

      {:ok, instance} ->
        if instance.state == new_state do
          {:reply, :ok, state}
        else
          ts = state.clock.()

          updated =
            if new_state == :banked do
              %{instance | state: :banked, entry_ip: nil, entry_port_published: nil, degraded_member: nil, updated_at: ts}
            else
              %{instance | state: new_state, updated_at: ts}
            end

          :ets.insert(state.instances, {instance_id, updated})
          {:reply, :ok, state}
        end

      :error ->
        {:reply, :ok, state}
    end
  end

  def handle_call({:adopt_endpoint, instance_id, entry_ip, entry_port}, _from, state) do
    case fetch(state, instance_id) do
      {:ok, %{state: cur} = instance}
      when cur not in [:destroyed, :failed] and is_binary(entry_ip) and entry_ip != "" and is_integer(entry_port) ->
        updated = %{instance | entry_ip: entry_ip, entry_port_published: entry_port, updated_at: state.clock.()}
        :ets.insert(state.instances, {instance_id, updated})
        {:reply, :ok, state}

      _ ->
        {:reply, :ok, state}
    end
  end

  def handle_call({:evict_partial_sets, reported_sets}, _from, state) do
    do_evict_partial_sets(state, reported_sets)
  end

  def handle_call({:evict_set, instance_id, reason}, _from, state) do
    case fetch(state, instance_id) do
      {:ok, instance} ->
        case append_set_evicted(state, instance, reason) do
          {:ok, state} ->
            state = clear_member_snapshots(state, instance_id)
            {:reply, :ok, state}

          {:error, _reason} ->
            {:reply, :ok, state}
        end

      {:error, _} ->
        {:reply, :ok, state}
    end
  end

  # -- create ----------------------------------------------------------------

  defp do_create(attrs, state) do
    workload = Map.fetch!(attrs, :workload)

    if has_live_instance?(state, workload) do
      {:reply, {:error, :already_live}, state}
    else
      append_create(attrs, workload, state)
    end
  end

  defp append_create(attrs, workload, state) do
    ts = state.clock.()
    instance_id = Map.fetch!(attrs, :instance_id)

    payload = %{
      node_id: Map.get(attrs, :node_id),
      subnet_cidr: Map.get(attrs, :subnet_cidr),
      entry_member: Map.get(attrs, :entry_member),
      entry_port: Map.get(attrs, :entry_port),
      listen_port: Map.get(attrs, :listen_port),
      # The EMBER_GROUP_SECRET this group boots with (sourced from secretRef, or
      # minted at create): recorded here so the op-log alone reconstructs it and a
      # relight/adoption re-derives the same value without re-reading K8s.
      secret: Map.get(attrs, :secret),
      state: "starting"
    }

    op = %Op{
      kind: :group_created,
      tenant: Map.fetch!(attrs, :tenant),
      principal: Map.get(attrs, :principal),
      workload: workload,
      group_instance_id: instance_id,
      ts: ts,
      payload: payload
    }

    case state.op_log_mod.append(state.op_log, op) do
      {:ok, _seq} ->
        instance = %{
          instance_id: instance_id,
          tenant: op.tenant,
          principal: op.principal,
          workload: workload,
          state: :creating,
          node_id: Map.get(attrs, :node_id),
          subnet_cidr: Map.get(attrs, :subnet_cidr),
          entry_member: Map.get(attrs, :entry_member),
          entry_port: Map.get(attrs, :entry_port),
          listen_port: Map.get(attrs, :listen_port),
          set_id: nil,
          entry_ip: nil,
          entry_port_published: nil,
          degraded_member: nil,
          created_at: ts,
          last_active_at: nil,
          updated_at: ts,
          terminal_reason: nil
        }

        :ets.insert(state.instances, {instance_id, instance})
        {:reply, {:ok, instance}, state}

      {:error, _reason} = error ->
        {:reply, error, state}
    end
  end

  # -- member started --------------------------------------------------------

  defp do_member_started(state, instance_id, fields) do
    with {:ok, instance} <- fetch(state, instance_id),
         :ok <- refuse_terminal(instance) do
      ts = state.clock.()
      member_name = Map.fetch!(fields, :member_name)

      op = %Op{
        kind: :group_member_started,
        tenant: instance.tenant,
        principal: instance.principal,
        workload: instance.workload,
        group_instance_id: instance_id,
        ts: ts,
        payload: %{
          member_name: member_name,
          member_index: Map.get(fields, :member_index),
          vm_id: Map.get(fields, :vm_id),
          ip: Map.get(fields, :ip)
        }
      }

      case state.op_log_mod.append(state.op_log, op) do
        {:ok, _seq} ->
          member = %{
            instance_id: instance_id,
            member_name: member_name,
            member_index: Map.get(fields, :member_index),
            vm_id: Map.get(fields, :vm_id),
            ip: Map.get(fields, :ip),
            # The daemon-reported entry-endpoint projection ({noded pod IP, vmPort}),
            # set only on the ENTRY member's report. Held in ETS only (not projected
            # to SQLite): it is consumed by the same in-flight wake that recorded it
            # (the publish that follows), and a rebuilt CP never resumes that wake.
            endpoint_ip: Map.get(fields, :endpoint_ip, ""),
            endpoint_port: Map.get(fields, :endpoint_port, 0),
            state: "starting",
            # A fresh member boot clears any prior banked slice (the warmth is spent).
            snapshot_ref: nil,
            healthy: false,
            updated_at: ts
          }

          :ets.insert(state.members, {{instance_id, member_name}, member})
          {:reply, {:ok, member}, state}

        {:error, _reason} = error ->
          {:reply, error, state}
      end
    else
      {:error, _reason} = error -> {:reply, error, state}
    end
  end

  # A member report against a terminal instance is a zombie worker (its wake
  # expired at the bound and the instance was rolled while a StartGroupMember hung):
  # refuse rather than resurrecting rows on a destroyed/failed instance.
  defp refuse_terminal(%{state: st}) do
    if GroupState.terminal?(st), do: {:error, {:instance_terminal, st}}, else: :ok
  end

  # -- transition ------------------------------------------------------------

  defp do_transition(state, instance_id, event, op_kind, payload, updates) do
    with {:ok, instance} <- fetch(state, instance_id),
         {:ok, next} <- GroupState.transition(instance.state, event) do
      case append_and_update(state, instance, op_kind, next, payload, updates) do
        {:ok, updated, state} -> {:reply, {:ok, updated}, state}
        {:error, reason} -> {:reply, {:error, reason}, state}
      end
    else
      {:error, _reason} = error -> {:reply, error, state}
    end
  end

  # The write-through core: append to the op-log, and ONLY on {:ok, seq} update ETS.
  # On append failure ETS is left untouched (as durable as the op-log agrees) and
  # the error is returned. Terminal transitions record the reason and drop the entry
  # endpoint; a bank (next == :banked) also drops the entry endpoint.
  defp append_and_update(state, instance, op_kind, next_state, payload, updates) do
    ts = state.clock.()

    op = %Op{
      kind: op_kind,
      tenant: instance.tenant,
      principal: instance.principal,
      workload: instance.workload,
      group_instance_id: instance.instance_id,
      ts: ts,
      payload: payload
    }

    case state.op_log_mod.append(state.op_log, op) do
      {:ok, _seq} ->
        terminal? = GroupState.terminal?(next_state)

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
        {:ok, updated, state}

      {:error, reason} ->
        {:error, reason}
    end
  end

  # After a durable transition, reconcile the entry endpoint fact with the new state:
  #   * terminal -> no live entry endpoint;
  #   * banked   -> the set is snapshotted and destroyed, so no live entry endpoint;
  #   * any other -> leave the caller's `updates` as-is (publish set the endpoint).
  defp post_transition_endpoint(base, next_state, terminal?) do
    cond do
      terminal? -> %{base | entry_ip: nil, entry_port_published: nil, degraded_member: nil}
      next_state == :banked -> %{base | entry_ip: nil, entry_port_published: nil, degraded_member: nil}
      true -> base
    end
  end

  # Transient ETS-only FSM move (no op-log append): banking/relighting/fresh_booting
  # entry markers and their aborts, which a later completion op or adoption resolves.
  defp do_mark(state, instance_id, event) do
    with {:ok, instance} <- fetch(state, instance_id),
         {:ok, next} <- GroupState.transition(instance.state, event) do
      ts = state.clock.()
      updated = %{instance | state: next, updated_at: ts}
      :ets.insert(state.instances, {instance_id, updated})
      {:reply, {:ok, updated}, state}
    else
      {:error, _reason} = error -> {:reply, error, state}
    end
  end

  # -- member health + degraded flag -----------------------------------------

  defp do_set_member_health(state, instance_id, member_name, healthy?) do
    case fetch_member(state, instance_id, member_name) do
      {:ok, member} ->
        ts = state.clock.()
        updated_member = %{member | healthy: healthy?, updated_at: ts}
        :ets.insert(state.members, {{instance_id, member_name}, updated_member})

        # Recompute the instance's degraded flag from the full member set. On a
        # running instance, any unhealthy member names it degraded (first one, stable);
        # whole again clears it. A non-running instance carries no degraded flag.
        state = recompute_degraded(state, instance_id)
        {:reply, get_view(state, instance_id), state}

      :error ->
        {:reply, :error, state}
    end
  end

  defp recompute_degraded(state, instance_id) do
    case fetch(state, instance_id) do
      {:ok, %{state: :running} = instance} ->
        dead =
          member_list(state, instance_id)
          |> Enum.filter(&(&1.healthy == false))
          |> Enum.map(& &1.member_name)
          |> List.first()

        updated = %{instance | degraded_member: dead, updated_at: state.clock.()}
        :ets.insert(state.instances, {instance_id, updated})
        state

      _ ->
        state
    end
  end

  defp mark_all_members_healthy(state, instance_id, healthy?) do
    ts = state.clock.()

    for {{^instance_id, _name} = key, member} <- member_entries(state, instance_id) do
      :ets.insert(state.members, {key, %{member | healthy: healthy?, updated_at: ts}})
    end

    state
  end

  defp bank_members(state, instance_id, members) do
    ts = state.clock.()
    by_name = Map.new(members, fn m -> {member_field(m, :name), member_field(m, :snapshot_ref)} end)

    for {{^instance_id, name} = key, member} <- member_entries(state, instance_id) do
      case Map.fetch(by_name, name) do
        {:ok, snapshot_ref} ->
          updated = %{
            member
            | snapshot_ref: snapshot_ref,
              vm_id: nil,
              ip: nil,
              state: "banked",
              healthy: false,
              updated_at: ts
          }

          :ets.insert(state.members, {key, updated})

        :error ->
          :ok
      end
    end

    state
  end

  # Clear every member row's banked snapshot_ref (a set eviction: the warmth is
  # gone). Leaves the member rows otherwise intact so the fresh boot re-records live
  # facts over them via member_started.
  defp clear_member_snapshots(state, instance_id) do
    ts = state.clock.()

    for {{^instance_id, _name} = key, member} <- member_entries(state, instance_id) do
      :ets.insert(state.members, {key, %{member | snapshot_ref: nil, updated_at: ts}})
    end

    state
  end

  # A member payload entry may carry atom or string keys (freshly-appended atom-keyed
  # op vs a value rebuilt from durable payload_json), so read either.
  defp member_field(m, key) when is_map(m), do: Map.get(m, key) || Map.get(m, Atom.to_string(key))
  defp member_field(_m, _key), do: nil

  # -- eager set eviction ----------------------------------------------------

  defp do_evict_partial_sets(state, reported_sets) do
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
        expected = expected_member_names(acc, instance.instance_id)
        reported = Map.get(reported_sets, instance.instance_id, MapSet.new())

        if MapSet.subset?(expected, reported) do
          {ids, acc}
        else
          # PARTIAL set: the node is missing at least one member's bundle. Evict
          # through the durable path (so a rebuild agrees the warmth is gone),
          # clearing set_id so the next wake fresh-boots. A banked instance stays
          # `banked` (the FSM has no evict edge off banked in R5: eviction of the
          # SET is a bundle-audit clear, not a lifecycle transition), so this is an
          # ETS field update paired with the durable group_set_evicted op.
          case append_set_evicted(acc, instance, "partial_set") do
            {:ok, acc} -> {[instance.instance_id | ids], acc}
            {:error, _reason} -> {ids, acc}
          end
        end
      end)

    {:reply, Enum.reverse(evicted_ids), state}
  end

  # Append group_set_evicted (durable) and clear the instance's set_id in ETS.
  # group_set_evicted is a bundle-audit clear, not an FSM transition (the instance
  # stays banked / is separately marked fresh_booting by the wake path); a rebuild
  # replays the cleared set_id.
  defp append_set_evicted(state, instance, reason) do
    ts = state.clock.()

    op = %Op{
      kind: :group_set_evicted,
      tenant: instance.tenant,
      principal: instance.principal,
      workload: instance.workload,
      group_instance_id: instance.instance_id,
      ts: ts,
      payload: %{reason: reason}
    }

    case state.op_log_mod.append(state.op_log, op) do
      {:ok, _seq} ->
        updated = %{instance | set_id: nil, updated_at: ts}
        :ets.insert(state.instances, {instance.instance_id, updated})
        {:ok, state}

      {:error, _reason} = error ->
        error
    end
  end

  # -- helpers ---------------------------------------------------------------

  # A group is the workload's SINGLE servable entry endpoint exactly when it is that
  # workload's, in the `running` FSM state, and carries a routable entry {ip, port}.
  # In R5 core a running group publishes a HEALTHY entry by construction (the entry
  # member is health-gated before publish, and a bank/terminal transition drops the
  # endpoint), so a running instance with a recorded entry {ip, port} is servable. A
  # NON-entry member falling unhealthy only sets the degraded FLAG and keeps the entry
  # endpoint live. Health-DRIVEN withdrawal of a live entry member's endpoint (pulling
  # the endpoint when the entry member itself later goes unhealthy) is a LATER path,
  # not this predicate: this check reads only the recorded {ip, port}, it does not
  # re-probe the entry member's current health.
  defp entry_servable?(instance, workload) do
    instance.workload == workload and instance.state == :running and
      is_binary(instance.entry_ip) and instance.entry_ip != "" and
      is_integer(instance.entry_port_published)
  end

  defp has_live_instance?(state, workload) do
    :ets.foldl(
      fn {_id, instance}, acc ->
        acc or (instance.workload == workload and GroupState.live?(instance.state))
      end,
      false,
      state.instances
    )
  end

  # The expected member names of a group = every member row's name (the members were
  # recorded at boot; a complete banked set must have a bundle for each). Used by the
  # set-completeness check.
  defp expected_member_names(state, instance_id) do
    member_list(state, instance_id)
    |> Enum.map(& &1.member_name)
    |> MapSet.new()
  end

  defp member_list(state, instance_id) do
    member_entries(state, instance_id)
    |> Enum.map(fn {_key, member} -> member end)
    |> Enum.sort_by(&(&1.member_index || 0))
  end

  defp member_entries(state, instance_id) do
    :ets.foldl(
      fn {{id, _name} = key, member}, acc ->
        if id == instance_id, do: [{key, member} | acc], else: acc
      end,
      [],
      state.members
    )
  end

  defp fetch(state, instance_id) do
    case :ets.lookup(state.instances, instance_id) do
      [{^instance_id, instance}] -> {:ok, instance}
      [] -> {:error, {:not_found, instance_id}}
    end
  end

  defp fetch_member(state, instance_id, member_name) do
    case :ets.lookup(state.members, {instance_id, member_name}) do
      [{{^instance_id, ^member_name}, member}] -> {:ok, member}
      [] -> :error
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
