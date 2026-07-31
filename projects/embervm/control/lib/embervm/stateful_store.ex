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

  ## generation blessing and quarantine (R7, ADR embervm/011)

  Standing decision 4 inverts who issues a volume's generation: the control
  plane is now the SOLE issuer (`bless_generation/3`), and the daemon records
  what it is told rather than inventing a value. The blessing ledger
  (`blessed_generation` + `quarantined`) lives in a table SEPARATE from the
  volume facts `get_volume/2`/`upsert_volume/3` expose (`@blessing_table`, not
  `@volumes_table`): a workload can be blessed BEFORE it has a real volume row
  at all (the very first wake blesses generation 1 before the daemon's FRESH
  boot creates the volume), and `get_volume/2` returning nil for "no volume
  yet" vs a map for "an existing volume" is load-bearing for
  `Embervm.StatefulManager.plan_wake/2`'s FRESH-vs-anchored placement decision.
  Folding blessing into the volume row would make the very first blessing
  fabricate a phantom, node_id-less volume that both defeats that nil check
  and crashes the anchor-to-node placement logic that pattern-matches on it.

  `next_blessed_generation/2` reads the ledger's `blessed_generation` (the last
  value THIS control plane blessed, durable via the `generation_blessed` op,
  absent for a never-blessed workload) and returns one past it;
  `bless_generation/3` appends that op BEFORE the caller dispatches the boot
  request (see `Embervm.StatefulManager.plan_wake/2`'s ordering comment:
  op-log-before-dispatch is the fence, a crash between the two leaves a
  harmlessly-unused blessed number, never a hole).

  A volume is QUARANTINED when a node reports its generation has moved PAST
  the last value this control plane blessed, with `generation_blessed: false`
  on the wire (a self-bump the daemon made outside the blessing ledger, e.g. a
  legacy noded that has not yet enabled `EMBERVM_NODED_REQUIRE_BLESSING`, or a
  split-brain write). `upsert_volume/3` re-derives `quarantined` in the
  blessing table on every node refresh that carries a `generation_blessed`
  fact: true when the reported generation exceeds `blessed_generation` AND the
  node's own `generation_blessed` fact is false AND the forward jump comes from
  a DIFFERENT node than the volume's current anchor. When the volume's OWN anchor
  node (the single writer Longhorn RWO fencing, ADR embervm/011, guarantees) is
  the one running ahead of the watermark, that is watermark lag after a CP roll,
  not split-brain, so `update_quarantine` ADOPTS the reported generation (advances
  the watermark, clears quarantine) rather than deadlocking the workload; only a
  forward jump from a non-anchor node stays fail-closed. This is ADR embervm/014's
  node-authoritative reconciliation applied to the R7 blessing watermark. A node
  reporting `generation_blessed: true` at or below the blessed watermark clears it.
  Once quarantined, `Embervm.StatefulManager.plan_wake/2` parks the wake rather than
  placing it (fail closed: resolution is either the anchor-node adoption above or a
  runbook decision). A workload that has NEVER been blessed (no CP generation-blessing
  op ever landed, e.g. every pre-R7 volume) has an absent `blessed_generation`
  and is NEVER quarantined by this alone: `quarantined?/2` treats absent as
  "not yet under CP governance" rather than "behind it", so an existing volume
  is grandfathered the first time it is reported (adoption seeds the ledger
  from that eager first NodeStatus via `seed_blessed_generation_if_unset/3`
  before any quarantine check runs), never punished retroactively for
  predating blessing. `quarantined` itself is NOT durable (a live
  node-report-derived fact, exactly like a stateful instance's `healthy`): a
  rebuild always starts un-quarantined and the next NodeStatus refresh
  re-derives it.

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
  # The blessing ledger (R7) is DELIBERATELY a separate table from @volumes_table,
  # keyed by workload exactly like it. A workload can be blessed before it has a
  # real volume row at all (the very first wake blesses generation 1 BEFORE the
  # daemon's FRESH boot creates the volume), and get_volume/2's nil/non-nil result
  # is load-bearing: plan_wake reads nil as "no volume yet, free FRESH placement"
  # and non-nil as "an existing volume, anchor to its node_id". Folding the
  # blessing ledger into @volumes_table would make bless_generation/3 fabricate a
  # non-nil, node_id-less volume row for a workload with no real volume, which
  # both defeats that nil check AND crashes anchor_node/2 (no node_id to match).
  @blessing_table :embervm_stateful_blessing
  @blessing_lease_table :embervm_stateful_blessing_leases
  @blessing_lease_size 50
  # In-flight checkpoint-dispatch records (R7, ADR embervm/017): workload ->
  # %{vm_id, generation} for an interruptible-bank CHECKPOINT the control plane
  # dispatched but has not yet resolved. Rebuilt from the durable
  # `checkpoint_dispatch` projection on boot (unlike `quarantined`, this IS durable:
  # it is the proof a recovered control plane uses to recognize its OWN auto-aborted
  # checkpoint and auto-heal the resulting quarantine, so it must survive the very
  # restart that triggers the auto-abort). Consulted by update_quarantine.
  @checkpoint_dispatch_table :embervm_stateful_checkpoint_dispatch

  # Eager broken-pair eviction hysteresis: a banked instance's pair must be
  # observed broken on this many CONSECUTIVE eager_evict_broken_pairs sweeps
  # before it is evicted (reason pair_broken). The sweep runs at ~1 Hz, so this
  # is a ~2 s grace: enough that a single transient blip (a racy reconcile that
  # briefly advanced the volume before the bundle's bank op landed, or a one-tick
  # stale node report) does not drop a warm bundle that is about to become valid
  # again, while a GENUINELY broken pair (the volume permanently moved on) still
  # evicts within a few sweeps. The monotonic upsert_volume guard (see
  # handle_call({:upsert_volume, ...})) removes the most common trigger (a
  # backward generation regression); this hysteresis covers the remaining
  # forward-race window.
  @broken_evict_threshold 3

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

  @doc "Every volume row in the hot set, for reconciliation."
  @spec all_volumes(GenServer.server()) :: [map()]
  def all_volumes(store \\ __MODULE__) do
    GenServer.call(store, :all_volumes)
  end

  @doc """
  Upsert the volume fact for `workload` (ETS-only: the durable volume row is
  written by the op-log's `volume_created` projection; this is the live-fact
  primitive Task 8 drives from NodeStatus scrapes, the way `set_health/2` /
  `touch_active/3` update lossy node facts). Merges the given fields over any
  existing row (or inserts a fresh one) and returns the merged volume row.
  `generation_blessed` (R7, the node's per-volume wire fact) is NOT stored on
  this row (see the moduledoc's "generation blessing and quarantine" section
  for why): when present it instead re-derives `quarantined` in the SEPARATE
  blessing ledger (a reported generation past the last CP-blessed one, with
  `generation_blessed: false`, quarantines; anything else clears it), readable
  via `quarantined?/2`.
  """
  @spec upsert_volume(GenServer.server(), String.t(), map()) :: map()
  def upsert_volume(store \\ __MODULE__, workload, fields) do
    GenServer.call(store, {:upsert_volume, workload, fields})
  end

  @doc """
  The next generation this control plane should bless for `workload`'s volume:
  one past the blessing ledger's durable `blessed_generation` (absent reads as
  0, so the very first blessing for a never-blessed workload is 1). A PURE ETS
  read against the SEPARATE blessing table (never `@volumes_table`; see the
  moduledoc); it does not append anything (see `bless_generation/3`).
  """
  @spec next_blessed_generation(GenServer.server(), String.t()) :: pos_integer()
  def next_blessed_generation(store \\ __MODULE__, workload) do
    GenServer.call(store, {:next_blessed_generation, workload})
  end

  def blessing_watermark(store \\ __MODULE__, workload) do
    GenServer.call(store, {:blessing_watermark, workload})
  end
  def grant_blessing_lease(store \\ __MODULE__, workload, node_id, size \\ @blessing_lease_size) do
    GenServer.call(store, {:grant_blessing_lease, workload, node_id, size})
  end

  def blessing_leases_for_node(store \\ __MODULE__, node_id) do
    GenServer.call(store, {:blessing_leases_for_node, node_id})
  end

  @doc """
  Durably records that this control plane is about to issue `generation` as
  `workload`'s next writable-attach generation (R7, ADR embervm/011): appends
  the `generation_blessed` op (write-through) and, only on success, bumps the
  SEPARATE blessing ledger's `blessed_generation` to `generation` and clears
  `quarantined` (a fresh CP-issued blessing is by definition not behind
  itself). Never touches `@volumes_table` (see the moduledoc: a workload may
  not have a real volume row yet). MUST be called BEFORE the caller dispatches
  the boot request carrying this value as `blessed_generation` (the
  op-log-before-dispatch fence; see `Embervm.StatefulManager.plan_wake/2`).
  Returns `{:ok, fact}` (the ledger fact, `%{blessed_generation, quarantined}`)
  or `{:error, reason}` on an append failure (the caller must not dispatch a
  boot whose blessing never durably landed).
  """
  @spec bless_generation(GenServer.server(), String.t(), pos_integer()) :: {:ok, map()} | {:error, term()}
  def bless_generation(store \\ __MODULE__, workload, generation) do
    GenServer.call(store, {:bless_generation, workload, generation})
  end

  @doc """
  Whether `workload`'s volume is currently quarantined (R7): a node reported a
  generation past this control plane's last blessed one with
  `generation_blessed: false` on the wire. False for an unknown volume (nothing
  to quarantine) and false for a volume that has never been blessed (see the
  moduledoc's grandfather note). A PURE ETS read of the flag `upsert_volume/3`
  derives; this never recomputes it.
  """
  @spec quarantined?(GenServer.server(), String.t()) :: boolean()
  def quarantined?(store \\ __MODULE__, workload) do
    GenServer.call(store, {:quarantined?, workload})
  end

  @doc """
  Durably records an in-flight interruptible-bank CHECKPOINT the control plane
  dispatched for `workload` (R7, ADR embervm/017): appends `checkpoint_dispatched`
  (write-through) carrying `{vm_id, generation}` and, on success, UPSERTs the ETS
  record (one per workload). `generation` is the volume generation the checkpoint
  was taken at, so noded's resolve-timeout auto-abort will advance it to
  `generation + 1` on the SAME `vm_id`; that is exactly the signature
  `update_quarantine/4` auto-heals. Best-effort: on an op-log append failure the
  record is not written and the workload falls back to the manual break-glass on a
  later auto-abort (correct fail-closed), so the caller does not crash.
  """
  @spec record_checkpoint_dispatch(GenServer.server(), String.t(), String.t(), non_neg_integer()) ::
          :ok | {:error, term()}
  def record_checkpoint_dispatch(store \\ __MODULE__, workload, vm_id, generation) do
    GenServer.call(store, {:record_checkpoint_dispatch, workload, vm_id, generation})
  end

  @doc """
  Clears `workload`'s in-flight checkpoint-dispatch record (R7, ADR embervm/017):
  appends `checkpoint_resolved` and removes the ETS record, called when the control
  plane itself drives the resolve (COMMIT or ABORT) so a resolved checkpoint can
  never auto-heal a later unrelated `+1`. A no-op (no op appended) when no record
  exists, to keep op-log noise down.
  """
  @spec clear_checkpoint_dispatch(GenServer.server(), String.t()) :: :ok | {:error, term()}
  def clear_checkpoint_dispatch(store \\ __MODULE__, workload) do
    GenServer.call(store, {:clear_checkpoint_dispatch, workload})
  end

  @doc """
  Seeds `workload`'s blessed-generation watermark to `generation` WITHOUT
  appending an op-log entry, iff the volume has never been blessed
  (`blessed_generation` nil/absent). This is the adoption grandfather path (R7):
  the FIRST NodeStatus report for a pre-R7 (or otherwise never-blessed) volume
  seeds the ledger from the node's own eager report rather than quarantining a
  volume that predates blessing entirely. A no-op (returns the existing row
  unchanged) once a volume has ever been blessed, so it can never roll a real
  watermark backward or paper over a genuine post-blessing divergence.
  """
  @spec seed_blessed_generation_if_unset(GenServer.server(), String.t(), non_neg_integer()) :: map() | nil
  def seed_blessed_generation_if_unset(store \\ __MODULE__, workload, generation) do
    GenServer.call(store, {:seed_blessed_generation_if_unset, workload, generation})
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
    # The backend module dispatched at every call site below, threaded alongside
    # :op_log (the server address) so a non-default backend never requires editing
    # this module. Defaults to the selected backend module.
    op_log_mod = Keyword.get(opts, :op_log_mod, Embervm.OpLog.SQLite)
    op_log = Keyword.get(opts, :op_log, op_log_mod)
    clock = Keyword.get(opts, :clock, &default_clock/0)

    instances = :ets.new(@instances_table, [:set, :private])
    volumes = :ets.new(@volumes_table, [:set, :private])
    blessing = :ets.new(@blessing_table, [:set, :private])
    blessing_leases = :ets.new(@blessing_lease_table, [:set, :private])
    checkpoint_dispatch = :ets.new(@checkpoint_dispatch_table, [:set, :private])

    state = %{
      op_log: op_log,
      op_log_mod: op_log_mod,
      clock: clock,
      instances: instances,
      volumes: volumes,
      blessing: blessing,
      blessing_leases: blessing_leases,
      checkpoint_dispatch: checkpoint_dispatch,
      # workload -> %{live, banked}, kept in step with the hot set on every write.
      counts: %{},
      # instance_id -> count of CONSECUTIVE eager_evict_broken_pairs observations
      # that found this banked instance's pair broken. Eviction only fires once the
      # streak reaches @broken_evict_threshold, so a single transient blip (a racy
      # reconcile that briefly advanced the volume before the bundle's bank op
      # landed) does not drop a warm bundle that is about to become valid again.
      # An observation that finds the pair VALID clears the instance's streak. Not
      # durable (a live hygiene fact, exactly like healthy): a rebuild starts every
      # streak at zero and re-derives it over the next few sweeps.
      broken_streak: %{}
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
    with {:ok, rows} <- state.op_log_mod.load_stateful_instances(state.op_log),
         {:ok, volumes} <- state.op_log_mod.load_volumes(state.op_log),
         {:ok, blessing_rows} <- state.op_log_mod.load_volume_blessing(state.op_log),
         {:ok, dispatch_rows} <- state.op_log_mod.load_checkpoint_dispatches(state.op_log),
         {:ok, lease_rows} <- state.op_log_mod.load_blessing_leases(state.op_log) do
      state =
        Enum.reduce(rows, state, fn row, acc ->
          instance = row_to_instance(row)
          :ets.insert(acc.instances, {instance.instance_id, instance})
          bump_counts(acc, nil, instance.state, instance.workload)
        end)

      Enum.each(volumes, fn vol ->
        :ets.insert(state.volumes, {vol.workload, volume_row(vol)})
      end)

      # quarantined is NOT durable (a live node-report-derived fact, exactly like
      # a stateful instance's `healthy`): a rebuild always starts un-quarantined
      # and the next NodeStatus refresh re-derives it.
      Enum.each(blessing_rows, fn row ->
        :ets.insert(state.blessing, {row.workload, %{blessed_generation: row.blessed_generation, quarantined: false}})
      end)

      # Durable, unlike quarantined above: an unresolved checkpoint-dispatch record
      # is the proof a recovered control plane uses to auto-heal its OWN auto-aborted
      # checkpoint (ADR embervm/017), so it must survive the restart that caused the
      # auto-abort. A resolved checkpoint's projection row was deleted, so it is
      # absent here by construction.
      Enum.each(dispatch_rows, fn row ->
        :ets.insert(state.checkpoint_dispatch, {row.workload, %{vm_id: row.vm_id, generation: row.generation}})
      end)

      Enum.each(lease_rows, fn row ->
        :ets.insert(state.blessing_leases, {{row.workload, row.node_id}, row})
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
    "destroying" => :destroying,
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

  def handle_call(:all_volumes, _from, state) do
    volumes = :ets.foldl(fn {_workload, volume}, acc -> [volume | acc] end, [], state.volumes)
    {:reply, volumes, state}
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

    # The STORED pair-key generation is MONOTONIC: a node volume report must never
    # move it backward. bump_volume_ets (the boot/relight writer) already guards
    # this; upsert_volume (the periodic refresh_volume_facts writer) must too, or a
    # LAGGING report (a node still reporting the pre-bank generation, or under
    # co-location a sibling brick's stale report) regresses the volume generation
    # below a just-banked bundle's snapshot_generation, so the next sweep tick
    # sees snapshot_generation != volume.generation, declares the pair broken, and
    # evicts the warm bundle: the recurring demo-postgres pair_broken flap. A
    # genuine FORWARD divergence (the volume legitimately advances past a stranded
    # old bundle) still lands, so real broken pairs are still detected. Other
    # fields (size/allocated/node_id/exported_generation) always take the latest
    # report; only the stored generation is floored to the current value.
    #
    # The floor applies ONLY to the stored pair-key. Quarantine derivation below
    # reads the RAW reported generation (reported_gen), not the floored one: an
    # unblessed forward jump must still be caught, and a report SETTLING BACK to
    # the blessed watermark must still CLEAR the quarantine even though the stored
    # pair-key does not regress. The two concerns are independent (warm-bundle
    # protection vs generation-blessing safety) and only happened to share a field.
    reported_gen = Map.get(fields, :generation)
    current_gen = Map.get(base, :generation, 0)

    fields =
      if is_integer(reported_gen) and is_integer(current_gen) and reported_gen < current_gen do
        Map.put(fields, :generation, current_gen)
      else
        fields
      end

    merged =
      base
      |> Map.merge(Map.delete(fields, :generation_blessed))
      |> Map.put(:workload, workload)
      |> Map.put(:updated_at, ts)

    :ets.insert(state.volumes, {workload, merged})

    # generation_blessed (R7) is a node-report wire fact feeding the SEPARATE
    # blessing ledger's quarantine derivation (never stored on the volume row
    # itself; see @blessing_table's comment). Only touch the blessing table
    # when this upsert actually carries a fresh report of it.
    state =
      case Map.get(fields, :generation_blessed) do
        nil ->
          state

        blessed_on_wire ->
          # Quarantine reads the RAW reported generation, not the floored stored
          # pair-key: an unblessed forward jump must be caught, and a settle-back
          # to the blessed watermark must clear it (see the floor comment above).
          quarantine_gen = if is_integer(reported_gen), do: reported_gen, else: Map.get(merged, :generation, 0)
          # reporting_node is who sent THIS report (always carried, see
          # refresh_volume_facts); anchor_node is the volume's prior holder (the
          # pre-merge row's node_id). update_quarantine compares them to decide
          # adopt-vs-quarantine on an unblessed forward jump (see its doc).
          reporting_node = Map.get(fields, :node_id)
          anchor_node = Map.get(base, :node_id)
          update_quarantine(state, workload, quarantine_gen, blessed_on_wire, reporting_node, anchor_node)
      end

    {:reply, merged, state}
  end

  def handle_call({:next_blessed_generation, workload}, _from, state) do
    {:reply, handle_call_next_blessed(state, workload), state}
  end
  def handle_call({:blessing_watermark, workload}, _from, state) do
    watermark = fetch_blessing(state, workload) |> Map.get(:blessed_generation, 0) || 0
    {:reply, watermark, state}
  end

  def handle_call({:blessing_leases_for_node, node_id}, _from, state) do
    leases =
      state.blessing_leases
      |> :ets.tab2list()
      |> Enum.filter(fn {{_workload, node}, row} -> node == node_id and row.next_generation < row.lease_end end)
      |> Enum.map(fn {{workload, _node}, row} -> %{workload_name: workload, next_generation: row.next_generation, lease_end: row.lease_end} end)

    {:reply, leases, state}
  end

  def handle_call({:grant_blessing_lease, workload, node_id, size}, _from, state) do
    start = handle_call_next_blessed(state, workload)
    lease_end = start + max(size, 1)
    op = %Op{kind: :blessing_lease_granted, tenant: "homelab", principal: "system:stateful:#{workload}", workload: workload, ts: state.clock.(), payload: %{node_id: node_id, next_generation: start, lease_end: lease_end}}

    case state.op_log_mod.append(state.op_log, op) do
      {:ok, _seq} ->
        row = %{workload: workload, node_id: node_id, next_generation: start, lease_end: lease_end, created_at: state.clock.(), updated_at: state.clock.()}
        :ets.insert(state.blessing_leases, {{workload, node_id}, row})
        {:reply, {:ok, %{start_generation: start, next_generation: start, lease_end: lease_end}}, state}
      {:error, reason} -> {:reply, {:error, reason}, state}
    end
  end

  def handle_call({:bless_generation, workload, generation}, _from, state) do
    current = fetch_blessing(state, workload) |> Map.get(:blessed_generation)

    # Monotonicity guard (R7): the blessing watermark must never regress. Every real
    # caller blesses next_blessed_generation (strictly greater than the current
    # watermark), so a request at or below it is a stale or duplicated call; treat it
    # as an idempotent no-op rather than rewinding the ledger, which would append a
    # regressing op AND re-open a quarantine the higher watermark had already cleared
    # (the quad {reported > blessed} would flip back true). Callers ignore the
    # returned fact (they proceed on their own computed generation), so returning the
    # unchanged fact is transparent.
    if is_integer(current) and generation <= current do
      Logger.warning("embervm stateful: bless_generation ignored, would not advance watermark",
        workload: workload,
        requested_generation: generation,
        blessed_generation: current
      )

      {:reply, {:ok, fetch_blessing(state, workload)}, state}
    else
      bless_generation_append(state, workload, generation)
    end
  end

  defp bless_generation_append(state, workload, generation) do
    case do_bless_append(state, workload, generation) do
      {:ok, state} -> {:reply, {:ok, fetch_blessing(state, workload)}, state}
      {:error, _reason} = error -> {:reply, error, state}
    end
  end

  # The op-append + ETS write half of a blessing, returning `{:ok, state}` /
  # `{:error, reason}` so it is usable from both the `:bless_generation` handle_call
  # (via bless_generation_append/3) and the update_quarantine auto-heal (ADR
  # embervm/017), which is inside another handle_call and must not emit a reply.
  defp do_bless_append(state, workload, generation) do
    op = %Op{
      kind: :generation_blessed,
      tenant: "homelab",
      # No per-request caller crosses this boundary (the wake plan issues the
      # blessing internally); the op's principal records the workload's
      # synthesized system owner, matching every other stateful lifecycle op.
      principal: "system:stateful:#{workload}",
      workload: workload,
      ts: state.clock.(),
      payload: %{generation: generation}
    }

    case state.op_log_mod.append(state.op_log, op) do
      {:ok, _seq} ->
        # A fresh CP-issued blessing is by definition not behind itself: clear
        # any prior quarantine. This writes ONLY the separate blessing table,
        # never @volumes_table (see that table's comment): a volume row for
        # this workload may not exist yet (the very first wake blesses before
        # a FRESH boot's volume_created lands).
        :ets.insert(state.blessing, {workload, %{blessed_generation: generation, quarantined: false}})
        {:ok, state}

      {:error, _reason} = error ->
        error
    end
  end

  def handle_call({:quarantined?, workload}, _from, state) do
    {:reply, Map.get(fetch_blessing(state, workload), :quarantined, false) || false, state}
  end

  def handle_call({:record_checkpoint_dispatch, workload, vm_id, generation}, _from, state) do
    op = %Op{
      kind: :checkpoint_dispatched,
      tenant: "homelab",
      principal: "system:stateful:#{workload}",
      workload: workload,
      ts: state.clock.(),
      payload: %{vm_id: vm_id, generation: generation}
    }

    case state.op_log_mod.append(state.op_log, op) do
      {:ok, _seq} ->
        :ets.insert(state.checkpoint_dispatch, {workload, %{vm_id: vm_id, generation: generation}})
        {:reply, :ok, state}

      {:error, _reason} = error ->
        {:reply, error, state}
    end
  end

  def handle_call({:clear_checkpoint_dispatch, workload}, _from, state) do
    case :ets.lookup(state.checkpoint_dispatch, workload) do
      [] ->
        # Nothing in flight: skip the op-log append entirely to keep noise down
        # (a COMMIT/ABORT of a workload that never recorded a dispatch, or a
        # double-clear).
        {:reply, :ok, state}

      [_ | _] ->
        {:reply, clear_checkpoint_dispatch_write(state, workload), state}
    end
  end

  def handle_call({:seed_blessed_generation_if_unset, workload, generation}, _from, state) do
    case fetch_blessing(state, workload) do
      %{blessed_generation: bg} = fact when is_integer(bg) ->
        {:reply, fact, state}

      _never_blessed ->
        fact = %{blessed_generation: generation, quarantined: false}
        :ets.insert(state.blessing, {workload, fact})
        {:reply, fact, state}
    end
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

    case state.op_log_mod.append(state.op_log, op) do
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

    case state.op_log_mod.append(state.op_log, op) do
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

    case state.op_log_mod.append(state.op_log, op) do
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
        # Mirror the node's volume-generation bump for this boot in real time, so a
        # fast-churning workload's volume pair-key never lags its next banked bundle.
        state = bump_volume_ets(state, workload, Map.get(attrs, :node_id), generation)
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

    case state.op_log_mod.append(state.op_log, op) do
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
        # A relight also bumps the volume ledger on the node (every StartStateful
        # mode does), so mirror it in real time exactly as the cold/fresh boot path
        # does, or the next bank after a relight would strand its bundle against a
        # stale volume pair-key.
        state =
          if op_kind == :stateful_relit do
            bump_volume_ets(state, instance.workload, Map.get(updates, :node_id), Map.get(updates, :generation))
          else
            state
          end

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
    with %{snapshot_generation: sg} when is_integer(sg) <- banked_instance(state, workload),
         %{generation: vg} when is_integer(vg) <- fetch_volume(state, workload) do
      sg == vg
    else
      _ -> false
    end
  end

  # Evict every banked instance whose pair is broken, through the DURABLE path (so
  # the op-log records the eviction and a rebuild agrees). Reason `pair_broken`.
  # Returns the evicted instance_ids. A banked instance whose volume row is missing
  # is ALSO pair-broken (no current generation to match): the general
  # do_pair_valid?/2 already returns false for that, so scanning banked instances
  # and evicting the invalid ones covers both divergence and a missing volume.
  #
  # HYSTERESIS: an invalid pair is not evicted on the first observation. Its
  # broken_streak is incremented, and it is evicted only once the streak reaches
  # @broken_evict_threshold consecutive sweeps. A VALID observation clears the
  # instance's streak. This makes the eviction tolerant of a single transient
  # blip (a racy reconcile that briefly advanced the volume before the bundle's
  # bank op landed) while still evicting a genuinely broken pair within a few
  # sweeps. The streak map is pruned to the currently-banked instances so a
  # short-lived one leaves no residue.
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
        id = instance.instance_id

        if do_pair_valid?(acc, instance.workload) do
          # Valid pair: clear any streak this instance had accrued.
          {ids, put_broken_streak(acc, id, 0)}
        else
          streak = Map.get(acc.broken_streak, id, 0) + 1

          if streak >= @broken_evict_threshold do
            payload = %{reason: "pair_broken"}

            case append_and_update(acc, instance, :stateful_evicted, :evicted, payload, %{}) do
              # Evicted: drop the streak entry (the instance is now terminal, no
              # longer banked, so it will never be re-scanned here).
              {:ok, _updated, acc} -> {[id | ids], put_broken_streak(acc, id, 0)}
              # A durable append failure leaves the instance banked (as durable as
              # the op-log agrees); keep the streak so a later sweep retries it
              # WITHOUT resetting the grace already served. Never partially evict.
              {:error, _reason} -> {ids, put_broken_streak(acc, id, streak)}
            end
          else
            # Still within the grace window: record the observation, do not evict.
            {ids, put_broken_streak(acc, id, streak)}
          end
        end
      end)

    # Prune streaks for instances no longer banked (evicted above, or transitioned
    # out from under us), so the map never grows unbounded.
    banked_ids = MapSet.new(banked, & &1.instance_id)
    pruned = Map.filter(state.broken_streak, fn {id, _} -> MapSet.member?(banked_ids, id) end)
    state = %{state | broken_streak: pruned}

    {:reply, Enum.reverse(evicted_ids), state}
  end

  # Set or clear an instance's consecutive-broken-observation streak. A 0 clears
  # the entry entirely (the common case: most banked pairs are valid every sweep,
  # so the map stays empty).
  defp put_broken_streak(state, instance_id, 0) do
    %{state | broken_streak: Map.delete(state.broken_streak, instance_id)}
  end

  defp put_broken_streak(state, instance_id, streak) do
    %{state | broken_streak: Map.put(state.broken_streak, instance_id, streak)}
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

  # The blessing ledger fact for `workload`: `%{blessed_generation, quarantined}`,
  # defaulting to `%{quarantined: false}` (no `blessed_generation` key) for a
  # workload never blessed at all. Never nil, so callers Map.get straight off
  # the result without a fetch_volume-style `|| %{}` guard.
  defp fetch_blessing(state, workload) do
    case :ets.lookup(state.blessing, workload) do
      [{^workload, fact}] -> fact
      [] -> %{quarantined: false}
    end
  end

  defp handle_call_next_blessed(state, workload) do
    current = fetch_blessing(state, workload) |> Map.get(:blessed_generation, 0) || 0
    ends =
      state.blessing_leases
      |> :ets.tab2list()
      |> Enum.filter(fn {{w, _node}, row} -> w == workload and row.next_generation < row.lease_end end)
      |> Enum.map(fn {_key, row} -> row.lease_end end)

    Enum.max([current + 1 | ends])
  end

  # Re-derive and persist `workload`'s quarantine flag in the SEPARATE blessing
  # table (never @volumes_table) from a fresh node report: `reported_gen` is the
  # volume's CURRENT generation this upsert just recorded, `blessed_on_wire` is
  # the node's own generation_blessed fact for that generation. Quarantined iff
  # the wire says NOT blessed AND `reported_gen` is STRICTLY PAST the last
  # generation this control plane blessed. A workload never blessed at all
  # (`blessed_generation` absent) never quarantines here (the moduledoc's
  # grandfather rule: absent means "not yet under CP governance", not "behind
  # it"). Logs a structured warning on a false -> true transition only (Task
  # 16's alert wiring reads the event name), never on an already-quarantined or
  # a never-quarantined report, so a stuck-quarantined workload does not spam.
  # Derives the blessing ledger's quarantine flag from a fresh node report, and
  # SELF-HEALS a lagging watermark by adopting a forward generation reported by the
  # volume's own fenced writer (ADR embervm/014's node-authoritative reconciliation,
  # applied to the R7 blessing watermark).
  #
  # An unblessed forward jump (node reports generation > our blessed watermark with
  # generation_blessed:false) has two causes that look identical on the wire but are
  # opposite in danger:
  #
  #   * The volume's OWN anchor node (the single writer that Longhorn RWO fencing,
  #     ADR embervm/011, guarantees is the only one able to attach it RW) running
  #     ahead of a watermark that REWOUND on a control-plane roll. The op-log replay
  #     lands the last durably-blessed generation, but the node bumps its on-disk
  #     generation locally on every attach, so a roll can leave blessed < reported
  #     for a volume that has exactly one writer. This is watermark lag, not
  #     split-brain. Quarantining it deadlocks the workload forever: a quarantined
  #     volume can never wake, so it can never re-bless to catch the watermark up
  #     (the recurring demo-postgres quarantine after a CP roll). ADOPT it: advance
  #     the watermark to the reported generation and clear quarantine. The adoption
  #     is ETS-only (like `quarantined` itself, it is re-derived from node reports,
  #     never durable); the next real wake blesses next_blessed_generation past it
  #     durably, and any later roll simply re-adopts from the node's report.
  #
  #   * A DIFFERENT node than the fenced anchor reporting a forward jump. That is the
  #     only shape that is genuine split-brain evidence, so it stays FAIL-CLOSED
  #     (quarantine). A legitimate RWO handoff whose new anchor reports an unblessed
  #     generation is the remaining fail-closed case, rare and manually recoverable.
  defp update_quarantine(state, workload, reported_gen, blessed_on_wire, reporting_node, anchor_node) do
    fact = fetch_blessing(state, workload)
    blessed_gen = Map.get(fact, :blessed_generation)

    forward_unblessed? =
      not blessed_on_wire and is_integer(blessed_gen) and reported_gen > blessed_gen

    fenced_writer? =
      is_binary(reporting_node) and is_binary(anchor_node) and reporting_node == anchor_node

    cond do
      # Auto-heal (R7, ADR embervm/017): the unblessed forward jump is the control
      # plane's OWN auto-aborted checkpoint resuming the same VM, proven by an
      # unresolved dispatch record matching this vm_id at exactly reported_gen - 1.
      # Bless it forward (durably) instead of quarantining. Checked before the
      # fenced-writer adoption because it is the most specific, provable signature;
      # anything not proven falls through to adoption / fail-closed quarantine.
      forward_unblessed? and checkpoint_abort_signature?(state, workload, reported_gen) ->
        auto_heal_checkpoint_abort(state, workload, reported_gen, blessed_gen)

      # Fail-closed under a live checkpoint context (ADR embervm/017): an unresolved
      # checkpoint-dispatch record exists but the forward jump did NOT match its
      # signature (wrong vm_id, or a jump past +1). A pending checkpoint that didn't
      # resume cleanly is exactly the second-writer hazard quarantine guards against,
      # so this stays quarantined even from the anchor node. This is checked BEFORE
      # fenced-writer adoption: with checkpoint context that doesn't add up, RWO
      # anchor identity is not enough to prove the generation is safe.
      forward_unblessed? and has_checkpoint_dispatch?(state, workload) ->
        if not Map.get(fact, :quarantined, false) do
          Logger.warning(
            "embervm stateful volume quarantined: unblessed generation with a non-matching checkpoint dispatch",
            event: :generation_quarantined,
            workload: workload,
            generation: reported_gen,
            blessed_generation: blessed_gen,
            node_id: reporting_node,
            anchor_node: anchor_node
          )
        end

        :ets.insert(state.blessing, {workload, Map.put(fact, :quarantined, true)})
        state

      # Fenced-writer adoption (ADR embervm/014): no checkpoint context, and the
      # reporting node IS the fenced anchor, so the fenced writer simply ran ahead of
      # the blessed watermark (the recurring demo-postgres quarantine after a CP roll).
      # Adopt it: advance the watermark to the reported generation and clear quarantine
      # (ETS-only, re-derived from node reports; the next real wake blesses past it durably).
      forward_unblessed? and fenced_writer? ->
        Logger.info(
          "embervm stateful volume generation adopted: fenced writer ran ahead of the blessed watermark",
          event: :generation_adopted,
          workload: workload,
          generation: reported_gen,
          blessed_generation: blessed_gen,
          node_id: reporting_node
        )

        :ets.insert(
          state.blessing,
          {workload, Map.merge(fact, %{blessed_generation: reported_gen, quarantined: false})}
        )

        state

      forward_unblessed? ->
        if not Map.get(fact, :quarantined, false) do
          Logger.warning(
            "embervm stateful volume quarantined: unblessed generation reported past the last blessed one",
            event: :generation_quarantined,
            workload: workload,
            generation: reported_gen,
            blessed_generation: blessed_gen,
            node_id: reporting_node,
            anchor_node: anchor_node
          )
        end

        :ets.insert(state.blessing, {workload, Map.put(fact, :quarantined, true)})
        state

      true ->
        :ets.insert(state.blessing, {workload, Map.put(fact, :quarantined, false)})
        state
    end
  end

  # The benign checkpoint-abort fingerprint (ADR embervm/017): an unresolved
  # checkpoint-dispatch record exists for the workload, the report is exactly one
  # past the recorded checkpoint generation, and the recorded vm_id is still a live
  # (serving or checkpointed) instance of the workload. The last condition is the
  # real discriminator: noded's auto-abort resumes the SAME vm_id, so a fresh second
  # writer (a different vm_id, or the recorded VM already gone) fails it and stays
  # quarantined even if it happens to land at reported_gen.
  # Whether an unresolved checkpoint-dispatch record exists for the workload at all,
  # regardless of whether it matches the current report. Its presence means the
  # control plane is mid-checkpoint for this volume, so an unblessed forward jump that
  # is NOT the clean resume (checkpoint_abort_signature?) is treated as the second-writer
  # hazard and fails closed rather than being adopted as a plain fenced-writer advance.
  defp has_checkpoint_dispatch?(state, workload) do
    fetch_checkpoint_dispatch(state, workload) != nil
  end

  defp checkpoint_abort_signature?(state, workload, reported_gen) do
    case fetch_checkpoint_dispatch(state, workload) do
      %{vm_id: vm_id, generation: gen} ->
        reported_gen == gen + 1 and workload_has_live_vm?(state, workload, vm_id)

      nil ->
        false
    end
  end

  # Bless the reported generation forward (the same write-through path a CP-driven
  # abort uses) and consume the dispatch record. A bless-append failure falls back
  # to quarantine (fail-closed): the auto-heal is an optimization over the manual
  # break-glass, never a reason to admit an unblessed generation.
  defp auto_heal_checkpoint_abort(state, workload, reported_gen, blessed_gen) do
    case do_bless_append(state, workload, reported_gen) do
      {:ok, state} ->
        Logger.info("embervm stateful volume auto-healed: blessed the control plane's own checkpoint-abort generation",
          event: :generation_auto_healed,
          workload: workload,
          generation: reported_gen,
          previous_blessed_generation: blessed_gen
        )

        # Best-effort clear: the watermark has already advanced past reported_gen,
        # so a lingering record can never re-heal (the report is no longer > blessed
        # next tick); the resolved op is bookkeeping.
        _ = clear_checkpoint_dispatch_write(state, workload)
        state

      {:error, reason} ->
        Logger.warning("embervm stateful: checkpoint-abort auto-heal bless failed, quarantining (fail-closed)",
          workload: workload,
          generation: reported_gen,
          reason: inspect(reason)
        )

        fact = fetch_blessing(state, workload)
        :ets.insert(state.blessing, {workload, Map.put(fact, :quarantined, true)})
        state
    end
  end

  # The in-flight checkpoint-dispatch record for `workload` (`%{vm_id, generation}`)
  # or nil. A pure ETS read of the durable record rebuilt on boot.
  defp fetch_checkpoint_dispatch(state, workload) do
    case :ets.lookup(state.checkpoint_dispatch, workload) do
      [{^workload, rec}] -> rec
      [] -> nil
    end
  end

  # Whether the workload has a LIVE (serving/checkpointed, not terminal or banked)
  # instance with this exact vm_id, i.e. the resumed VM from the auto-aborted
  # checkpoint is still the one holding the volume.
  defp workload_has_live_vm?(state, workload, vm_id) do
    :ets.foldl(
      fn {_id, instance}, acc ->
        acc or
          (instance.workload == workload and instance.vm_id == vm_id and
             StatefulState.live?(instance.state))
      end,
      false,
      state.instances
    )
  end

  # Append checkpoint_resolved (write-through) and, on success, drop the ETS record.
  # Returns :ok | {:error, reason}. On an append failure the ETS row is left in
  # place (harmless: see auto_heal_checkpoint_abort on why a lingering record cannot
  # cause a spurious heal once the watermark has advanced).
  defp clear_checkpoint_dispatch_write(state, workload) do
    op = %Op{
      kind: :checkpoint_resolved,
      tenant: "homelab",
      principal: "system:stateful:#{workload}",
      workload: workload,
      ts: state.clock.(),
      payload: %{}
    }

    case state.op_log_mod.append(state.op_log, op) do
      {:ok, _seq} ->
        :ets.delete(state.checkpoint_dispatch, workload)
        :ok

      {:error, _reason} = error ->
        error
    end
  end

  # Bump the ETS volume row's pair-key generation to the value an attach (a
  # cold/fresh boot or a relight) just booted the volume at, in REAL TIME. Every
  # StartStateful mode bumps the volume ledger on the node before boot and returns
  # the new generation; the control plane must mirror that immediately, because the
  # pair check (do_pair_valid?) compares a freshly-banked bundle's
  # snapshot_generation to this volume generation. Before this, the ETS volume
  # generation only advanced on the periodic refresh_volume_facts reconcile, so a
  # workload that banks faster than the reconcile interval (demo-postgres at
  # idleBankSeconds:1) had its volume pair-key perpetually lag its just-banked
  # bundle by >= 1, so pair_valid? was always false and every wake cold-booted
  # (never relit). Merges (preserving size/allocated from the last node refresh),
  # creates the row if absent, and never moves the generation backward (a stale
  # refresh must not undo a newer boot's bump).
  defp bump_volume_ets(state, workload, node_id, generation) when is_integer(generation) do
    ts = state.clock.()

    base =
      fetch_volume(state, workload) ||
        %{workload: workload, node_id: nil, generation: 0, size_bytes: nil, allocated_bytes: nil, updated_at: ts}

    if generation >= Map.get(base, :generation, 0) do
      merged =
        base
        |> Map.merge(%{generation: generation, node_id: node_id || Map.get(base, :node_id), updated_at: ts})
        |> Map.put(:workload, workload)

      :ets.insert(state.volumes, {workload, merged})
    end

    state
  end

  defp bump_volume_ets(state, _workload, _node_id, _generation), do: state

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
