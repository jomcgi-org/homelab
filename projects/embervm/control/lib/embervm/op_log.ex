defmodule Embervm.OpLog do
  @moduledoc """
  The op-log seam: every task-lifecycle transition in the control plane is
  appended as one `Op` before anything else observes it. This module defines
  the shared `Op` struct, the closed set of op kinds, and the behaviour that
  a backend (the `SQLite` GenServer today, a Raft-replicated `ra` tier later)
  must implement. Callers never talk to a backend module directly by name;
  they go through whichever module is configured as the op-log, which is what
  makes the backend swap in Task-later-than-6 a config change, not a rewrite.

  `read_from/2` and `load_tasks/1` exist for two different rebuild paths: a
  peer control-plane replica catching up from a seq (future), and a single
  node rebuilding its in-memory ETS task index from the durable `tasks`
  projection on boot (Task 7).
  """

  defmodule Op do
    @moduledoc """
    One op-log entry. `seq` is nil until the backend assigns it on append
    (backends assign it via the storage engine's own monotonic counter, e.g.
    SQLite's `AUTOINCREMENT` rowid) and is always populated on read. `ts` is
    injected by the caller in integer milliseconds since epoch: the backend
    never calls `System.os_time/1` itself, so ordering and TTL logic stay
    deterministic and testable.
    """
    @enforce_keys [:kind, :tenant, :ts]
    defstruct seq: nil,
              kind: nil,
              tenant: nil,
              principal: nil,
              workload: nil,
              task_id: nil,
              session_id: nil,
              serving_instance_id: nil,
              stateful_instance_id: nil,
              group_instance_id: nil,
              ts: nil,
              payload: %{}

    @type t :: %__MODULE__{
            seq: pos_integer() | nil,
            kind: atom(),
            tenant: String.t(),
            principal: String.t() | nil,
            workload: String.t() | nil,
            task_id: String.t() | nil,
            # Set on session_* ops (nil on task ops); the durable `ops.session_id`
            # column, added additively. A session op owns its `sessions`
            # projection row the way a task op owns its `tasks` row.
            session_id: String.t() | nil,
            # Set on serving_* ops (nil on task/session ops); the durable
            # `ops.serving_instance_id` column (R3), added additively the same
            # way session_id was in R2. A serving op owns its `serving_instances`
            # projection row the way a session op owns its `sessions` row.
            serving_instance_id: String.t() | nil,
            # Set on stateful_* ops (nil on every other op); the durable
            # `ops.stateful_instance_id` column (R4), added additively the same
            # way serving_instance_id was in R3. A stateful op owns its
            # `stateful_instances` projection row. volume_* ops leave it nil (they
            # own a `volumes` row keyed by workload, not an instance).
            stateful_instance_id: String.t() | nil,
            # Set on group_* ops (nil on every other op); the durable
            # `ops.group_instance_id` column (R5), added additively the same way
            # stateful_instance_id was in R4. A group op owns its `group_instances`
            # projection row (one per group) and, via the member refs it carries,
            # its `group_members` rows. group_stats leaves it nil (it is
            # workload-scoped, like serving_stats/stateful_stats).
            group_instance_id: String.t() | nil,
            ts: integer(),
            payload: map()
          }
  end

  # Closed enum: every op the control plane can ever emit. append/2 rejects
  # anything outside this list so a typo'd kind fails loudly at the write
  # site instead of silently skipping its projection.
  @kinds [
    :submitted,
    :assigned,
    :started,
    :succeeded,
    :failed,
    :retried,
    :dead_lettered,
    :redrive,
    :denied,
    :base_built,
    :primed,
    :vm_destroyed,
    :quota_enforced,
    :drain,
    # Session lifecycle (R2). Additive to the closed enum; sessions are durable,
    # ordered, and auditable exactly like tasks (ADR embervm/001) and project
    # into the `sessions` table (see Embervm.OpLog.SQLite). session_invoked
    # carries usage only (no request/response bodies) and upserts the same
    # (principal, day) usage projection tasks do, the D12.1 pattern.
    :session_created,
    :session_invoked,
    :session_banked,
    :session_relit,
    :session_expired,
    :session_evicted,
    # session_destroying is the durable destroy INTENT (ADR embervm/014 decision 5):
    # appended BEFORE the node-confirmed teardown RPC, so a CP crash mid-destroy
    # rebuilds as destroying and re-drives; session_destroyed follows only on node
    # confirmation. Only written under the EMBERVM_NODE_CONFIRMED_DESTROY gate.
    :session_destroying,
    :session_destroyed,
    :session_failed,
    # Serving lifecycle (R3). Additive to the closed enum, mirroring the R2
    # session kinds: serving instances are durable, ordered, and auditable
    # exactly like sessions (ADR embervm/001) and project into the
    # `serving_instances` table (see Embervm.OpLog.SQLite). serving_published/
    # serving_unpublished are the endpoint-lifetime audit record (an
    # enforcement-adjacent fact: it decides who traffic reaches), distinct from
    # serving_started/serving_banked/serving_destroyed which are VM lifecycle.
    # serving_stats carries request-count deltas from the idle-signal scrape and
    # upserts the (principal, day) usage projection's request_count column
    # (D-R3.2.1, distinct from the D12.1 task_count/vcpu/gb accrual path).
    :serving_started,
    :serving_published,
    :serving_unpublished,
    :serving_banked,
    :serving_relit,
    :serving_evicted,
    # serving_destroying: the durable destroy INTENT (ADR embervm/014 decision 5), the
    # serving counterpart of session_destroying. Only written under the
    # EMBERVM_NODE_CONFIRMED_DESTROY gate.
    :serving_destroying,
    :serving_destroyed,
    :serving_failed,
    :serving_stats,
    # Stateful lifecycle (R4). Additive to the closed enum, mirroring the R3
    # serving kinds: stateful instances and their volumes are durable, ordered,
    # and auditable exactly like serving instances (ADR embervm/001) and project
    # into the `stateful_instances` and `volumes` tables (see Embervm.OpLog.SQLite).
    # volume_created/volume_deleted are the durable-data audit record keyed by
    # workload (the volume outlives every instance by design). stateful_published/
    # stateful_unpublished are the L4-endpoint-lifetime audit (who a connection
    # reaches), distinct from the VM-lifecycle kinds. stateful_cold_booted carries
    # a reason (generation_mismatch|no_bundle|ledger_unreadable|explicit) so every
    # discarded-warmth event is reconstructable from the log alone; stateful_relit
    # carries the matched generation. stateful_stats carries per-listener
    # connection deltas from the idle-signal scrape (D-R3.2.1 shape at L4).
    :volume_created,
    :volume_deleted,
    :stateful_started,
    :stateful_published,
    :stateful_unpublished,
    :stateful_banked,
    :stateful_relit,
    :stateful_cold_booted,
    :stateful_evicted,
    # stateful_destroying: the durable destroy INTENT (ADR embervm/014 decision 5),
    # the stateful counterpart of session_destroying. Only written under the
    # EMBERVM_NODE_CONFIRMED_DESTROY gate.
    :stateful_destroying,
    :stateful_destroyed,
    :stateful_failed,
    :stateful_stats,
    # generation_blessed (R7, ADR embervm/011, standing decision 4): the control
    # plane durably records the volume generation it is ABOUT to issue for a
    # workload's next writable attach, appended BEFORE the boot request carrying
    # that value is dispatched (the fence: a crash between the two leaves a
    # harmlessly-unused blessed number, the reverse order would leave a hole).
    # Projects into the `volumes.blessed_generation` column. Carries
    # `{generation}`; stateful_instance_id is nil (it is workload-scoped, like
    # volume_created/volume_deleted, not instance-scoped).
    :generation_blessed,
    # Checkpoint-abort auto-heal (R7, ADR embervm/017). The interruptible-bank
    # CHECKPOINT (ADR embervm/008) arms a noded resolve-timeout auto-abort that
    # self-bumps the volume generation with no control plane reachable to bless it,
    # which quarantines the volume on the next report. To auto-heal only THAT
    # provably self-inflicted case, the control plane durably records each
    # checkpoint it dispatched: checkpoint_dispatched carries `{vm_id, generation}`
    # (workload-scoped, like generation_blessed) and projects into the
    # `checkpoint_dispatch` table (one row per workload; the stop-serialization
    # guard means one in-flight checkpoint per workload). checkpoint_resolved clears
    # it when the control plane itself drives the resolve (COMMIT or ABORT) or when
    # update_quarantine consumes the record to auto-bless a matching +1. An
    # unresolved row is what a recovered control plane replays to recognize its own
    # auto-aborted checkpoint after the very restart that triggered the auto-abort.
    :checkpoint_dispatched,
    :checkpoint_resolved,
    # Composite-group lifecycle (R5). Additive to the closed enum, mirroring the R4
    # stateful kinds: a composite group is a set of member microVMs that live, bank,
    # relight, and die as ONE unit (ADR embervm/001) and project into the
    # `group_instances` (one row per group) and `group_members` (one row per member)
    # tables (see Embervm.OpLog.SQLite). group_net_created/group_net_deleted are the
    # per-group private subnet audit (the group's own /29 the members share).
    # group_member_started records each member VM coming up; group_running is the
    # whole-group readiness edge (every member health-gated). group_published/
    # group_unpublished are the entry-endpoint-lifetime audit (who traffic reaches),
    # distinct from the member/group VM-lifecycle kinds. group_banked records the
    # ENTIRE member set atomically in one append (decision 3's atomicity): the bundle
    # set is one durable fact, never a member at a time. group_relit is a warm wake
    # of the whole set; group_fresh_booted is a cold boot that discarded warmth,
    # carrying a reason (no_set|partial_set|set_unreadable|clock_resync_failed|
    # explicit) so every discarded-warmth event reconstructs from the log alone.
    # group_set_evicted carries the reason and the member refs it discarded (the
    # partner event to a later fresh boot). group_degraded records a member falling
    # unhealthy while the group stays up (crash-consistency is per-VM, never across
    # members). The terminal `expired` state has NO dedicated kind: it rides
    # group_destroyed{reason: expired} (write-through discipline: expiry is a destroy
    # with a reason). group_stats rides the R4 stats-sweep shape but bills PER MEMBER
    # (a 3-member group bills 3 VMs' worth), upserted in the op's own transaction.
    :group_created,
    :group_net_created,
    :group_net_deleted,
    :group_member_started,
    :group_running,
    :group_published,
    :group_unpublished,
    :group_banked,
    :group_relit,
    :group_fresh_booted,
    :group_set_evicted,
    :group_degraded,
    # group_destroying: the durable destroy INTENT (ADR embervm/014 decision 5), the
    # group counterpart of session_destroying. Only written under the
    # EMBERVM_NODE_CONFIRMED_DESTROY gate.
    :group_destroying,
    :group_destroyed,
    :group_failed,
    :group_stats,
    # Continuity: node drain (R6, ADR embervm/009). Audit-only kinds recording a
    # bounded-preemption drain edge and its outcome: node_drain_started stamps the
    # published deadline and the per-class live-instance counts the control plane
    # will force-bank; node_drain_finished stamps the per-class banked counts. No
    # projection table (like :drain), the log itself is the audit record.
    :node_drain_started,
    :node_drain_finished,
    # Continuity: off-node artifact durability (R6, ADR embervm/009). Audit-only
    # kinds recording an artifact moving between node disk and the object store:
    # artifact_exported when a bank commit's write-back completes, artifact_restored
    # when a restore-on-miss wake copies an artifact back before relight/cold boot,
    # artifact_evicted_remote when a local eviction trigger (banked TTL, superseded
    # generation, workload deletion) also drops the store copy. No projection table
    # (like :node_drain_*), the log itself is the audit record; the payload carries
    # the ArtifactRef {kind, workload, ref} and, for a volume, its generation.
    :artifact_exported,
    :artifact_restored,
    :artifact_evicted_remote
  ]

  @spec kinds() :: [atom()]
  def kinds, do: @kinds

  @type server :: GenServer.server()

  @callback append(server(), Op.t()) :: {:ok, seq :: pos_integer()} | {:error, term()}
  # Reads every op strictly after `seq` in ascending seq order. Because the ops
  # journal is now prefix-compacted (ADR embervm/002), a caller asking for a
  # `seq` that has already fallen below the durable `compacted_through_seq`
  # marker gets `{:error, {:compacted, marker}}`, DISTINCT from `{:ok, []}` (an
  # empty-but-intact log): the requested history is gone, replaced by projected
  # state, so a replayer starting there must instead consult `compacted_through/1`
  # and rebuild from the projection snapshot (`load_tasks/1`) rather than assume
  # it saw the whole log. A `seq >= marker` behaves as before.
  @callback read_from(server(), seq :: non_neg_integer()) ::
              {:ok, [Op.t()]} | {:error, {:compacted, non_neg_integer()}} | {:error, term()}
  @callback load_tasks(server()) :: {:ok, [map()]} | {:error, term()}
  # Loads every session row from the durable `sessions` projection (R2), for the
  # SessionStore's boot/adoption rebuild (Task 5/8): the ETS hot set of sessions
  # is reconstructed from this exactly as the task index is from load_tasks/1. A
  # projection read, never the raw ops log.
  @callback load_sessions(server()) :: {:ok, [map()]} | {:error, term()}
  # Loads every serving instance row from the durable `serving_instances`
  # projection (R3), for the future ServingStore's boot/adoption rebuild
  # (Tasks 7/8), exactly mirroring load_sessions/1. A projection read, never
  # the raw ops log.
  @callback load_serving_instances(server()) :: {:ok, [map()]} | {:error, term()}
  # Loads every stateful instance row from the durable `stateful_instances`
  # projection (R4), for the StatefulStore's boot/adoption rebuild (Tasks 7/8),
  # exactly mirroring load_serving_instances/1. A projection read, never the raw
  # ops log.
  @callback load_stateful_instances(server()) :: {:ok, [map()]} | {:error, term()}
  # Loads every volume row from the durable `volumes` projection (R4): the
  # per-workload durable-data facts (generation, sizes) the StatefulStore rebuilds
  # its pair-validity view from on boot. A volume row lives until volume_deleted,
  # outliving every instance by design. A projection read, never the raw ops log.
  @callback load_volumes(server()) :: {:ok, [map()]} | {:error, term()}
  # Loads every generation-blessing row from the durable `volume_blessing`
  # projection (R7, ADR embervm/011): the per-workload `blessed_generation`
  # watermark this control plane's blessing ledger issued. A SEPARATE
  # projection from load_volumes/1 (see the `volume_blessing` table's comment
  # in Embervm.OpLog.SQLite for why a shared table would corrupt
  # StatefulStore.get_volume/2's nil-means-no-volume-yet contract). A
  # projection read, never the raw ops log.
  @callback load_volume_blessing(server()) :: {:ok, [map()]} | {:error, term()}
  # Loads every in-flight checkpoint-dispatch row from the durable
  # `checkpoint_dispatch` projection (R7, ADR embervm/017): the per-workload
  # `{vm_id, generation}` of a CHECKPOINT the control plane dispatched but has not
  # yet resolved. Rebuilt into StatefulStore ETS on boot so a recovered control
  # plane can auto-heal its own auto-aborted checkpoint. A projection read, never
  # the raw ops log.
  @callback load_checkpoint_dispatches(server()) :: {:ok, [map()]} | {:error, term()}
  # Loads every group-instance row from the durable `group_instances` projection
  # (R5), for the future GroupStore's boot/adoption rebuild, exactly mirroring
  # load_stateful_instances/1. One row per composite group. A projection read,
  # never the raw ops log.
  @callback load_group_instances(server()) :: {:ok, [map()]} | {:error, term()}
  # Loads every group-member row from the durable `group_members` projection (R5):
  # one row per (group instance, member name), the per-member lifecycle/health the
  # GroupStore rebuilds its member view from on boot. A member row lives with its
  # group instance (pruned when the group instance is). A projection read, never
  # the raw ops log.
  @callback load_group_members(server()) :: {:ok, [map()]} | {:error, term()}
  # Reads one task's stored result from the durable `results` projection, or
  # {:ok, nil} when there is none (never ran, or the TTL sweeper reaped it).
  # This is the result-store read the submit API (Task 8) serves `GET
  # /v1/tasks/{id}/result` from; it is a projection read, NOT the ops log, so
  # it does not violate "the API never exposes op-log internals". The stored
  # copy may be truncated to the workload's resultMaxBytes (the `truncated`
  # flag says so); sync callers get the full untruncated response a different
  # way (streamed straight through at request time, never via this store).
  @callback load_result(server(), task_id :: String.t()) ::
              {:ok, map() | nil} | {:error, term()}
  # Reads the opaque guest-request envelope captured in a task's `submitted`
  # op payload (path, headers, base64 body, content type), or {:ok, nil} when
  # the task has no submitted op (unknown id). Unlike load_result/2 this reads
  # the immutable `ops` log (the submitted record is never projected into a
  # column), which is exactly why the dispatcher (Task 11) needs a dedicated
  # read: it rebuilds the `AssignRequest` from this at dispatch time rather
  # than carrying the (up to 8 MiB) body in the ETS hot set or the fair queue.
  @callback load_request(server(), task_id :: String.t()) ::
              {:ok, map() | nil} | {:error, term()}
  # Pages the `usage` projection (Task 12): per-`(principal, day)` accumulated
  # vCPU-seconds / GB-seconds / task_count, written transactionally with each
  # `:succeeded`/`:failed` op that carried usage. This is the metering read the
  # API serves `GET /v1/usage` from and the source `Embervm.Metering` rebuilds
  # its quota cache from on boot. Opts: `:since_day` (integer epoch-day floor,
  # default 0), `:principal` (optional exact filter), `:limit` (integer or
  # `:infinity`, default 100), `:offset` (default 0). It is a projection read,
  # never the raw ops log.
  @callback list_usage(server(), opts :: keyword()) ::
              {:ok, %{items: [map()], total: non_neg_integer(), limit: term(), offset: non_neg_integer()}}
              | {:error, term()}
  # Runs ONE bounded compaction batch as of `now_ms` and returns the counts it
  # deleted plus the current ops-journal marker and whether the sweep is drained.
  # `results_deleted`/`tasks_compacted`/`ops_compacted` are rows removed from
  # each table THIS batch; `compacted_through` is the (possibly advanced) durable
  # `compacted_through_seq` marker; `done` is false when any table hit the batch
  # ceiling (more rows remain, call again). The scheduled sweeper
  # (`Embervm.OpLog.Compactor`) loops until `done`, so appends interleave between
  # batches (the 5ms-append-budget guard). GC does not emit ops.
  @callback compact(server(), now_ms :: integer()) ::
              {:ok,
               %{
                 results_deleted: non_neg_integer(),
                 tasks_compacted: non_neg_integer(),
                 sessions_compacted: non_neg_integer(),
                 serving_instances_compacted: non_neg_integer(),
                 stateful_instances_compacted: non_neg_integer(),
                 group_instances_compacted: non_neg_integer(),
                 ops_compacted: non_neg_integer(),
                 compacted_through: non_neg_integer(),
                 done: boolean()
               }}
              | {:error, term()}
  # The durable ops-journal prefix marker: every op with `seq <= this` has been
  # (or is eligible to be) compacted away and is available only as projected
  # state. Absent (never compacted) reads as 0. See `read_from/2`.
  @callback compacted_through(server()) :: {:ok, non_neg_integer()} | {:error, term()}
  # Prunes ONE task's projection rows (the `tasks` row and, via ON DELETE CASCADE,
  # its `results` row) WITHOUT emitting an op. Used by the submit dedupe path when
  # a terminal task's result has expired: the projection must be cleared so a fresh
  # resubmit under the same idempotency key does not collide on the unique
  # `(workload, idempotency_key)` index. The task's immutable `ops` remain in the
  # journal until horizon compaction; only the projection is pruned early.
  @callback evict_task(server(), task_id :: String.t()) :: :ok | {:error, term()}
end
