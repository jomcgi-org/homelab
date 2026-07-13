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
  @callback read_from(server(), seq :: non_neg_integer()) :: {:ok, [Op.t()]} | {:error, term()}
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
  @callback compact(server(), now_ms :: integer()) ::
              {:ok, %{results_deleted: non_neg_integer(), tasks_compacted: non_neg_integer()}}
              | {:error, term()}
end
