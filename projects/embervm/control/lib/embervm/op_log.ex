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
              ts: nil,
              payload: %{}

    @type t :: %__MODULE__{
            seq: pos_integer() | nil,
            kind: atom(),
            tenant: String.t(),
            principal: String.t() | nil,
            workload: String.t() | nil,
            task_id: String.t() | nil,
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
    :drain
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
