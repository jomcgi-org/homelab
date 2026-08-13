defmodule Embervm.TaskStore do
  @moduledoc """
  ETS hot set over the op-log's durable `tasks` projection: every read the
  dispatch hot path needs (Task 11) is O(1) against ETS, while every write
  goes through the op-log FIRST and only lands in ETS once the op-log confirms
  it's durable. That ordering, "op-log append succeeds, then and only then
  update ETS", is the write-through invariant this whole module exists to
  enforce: ETS never shows a task in a state the op-log doesn't already agree
  with, and a crash between the two never loses a transition (worst case ETS
  is briefly stale until the next boot's rebuild replays it).

  On `init/1` this rebuilds both ETS tables from `OpLog.load_tasks/1`, which
  is the recovery path: a fresh TaskStore against an existing op-log file (a
  crash-restart, or a `:rest_for_one` supervisor restart triggered by the
  op-log itself) ends up with exactly the state the durable log recorded,
  with no replay logic beyond "read the projection".

  Attempt bookkeeping note: the op-log's SQL `tasks.attempt` column counts
  *retries*, starting at 0 for a freshly submitted task and incrementing by
  one on each `:retried` op (see `Embervm.OpLog.SQLite`'s projection). The
  task-lifecycle contract callers see here (and the spec for this task) counts
  *attempts*, 1-based, so the first try is attempt 1. This module is the
  translation seam: ETS (and every public read) always holds the 1-based
  attempt count, computed as `sql_attempt + 1` when rebuilding from
  `load_tasks/1`, and incremented by exactly 1 in ETS on every successful
  `retry/2` call, mirroring the op-log's own `attempt = attempt + 1`.
  """

  use GenServer

  alias Embervm.OpLog.Op
  alias Embervm.TaskState

  @tasks_table :embervm_tasks
  @idem_table :embervm_task_idempotency

  # -- Client API ----------------------------------------------------------

  @spec start_link(keyword()) :: GenServer.on_start()
  def start_link(opts) do
    case Keyword.get(opts, :name, __MODULE__) do
      nil -> GenServer.start_link(__MODULE__, opts)
      name -> GenServer.start_link(__MODULE__, opts, name: name)
    end
  end

  @doc """
  Submits a new task, or returns the existing one if `idempotency_key` was
  already seen for this workload. `attrs` is `%{tenant, principal, workload,
  idempotency_key \\ nil, expires_at \\ nil, request \\ nil}`.

  `request` is the opaque guest-request envelope (path, content_type, guest
  headers, base64 body) the submit API (Task 8) captures verbatim. It is stored
  in the `submitted` op's payload, NOT projected into any column, so the durable
  record is complete for the dispatcher (Task 11) to build its `AssignRequest`
  from: an async 202 must not drop the body it accepted just because nothing
  dispatches it yet.
  """
  @spec submit(GenServer.server(), map()) ::
          {:ok, :created, String.t()} | {:ok, :existing, String.t()} | {:error, term()}
  def submit(store \\ __MODULE__, attrs) do
    GenServer.call(store, {:submit, attrs})
  end

  @doc """
  Move a queued task to `:assigned`. The 3-arity form carries the `vm_id` the
  dispatch reserved: under async lifecycle writes it registers the deferred append
  with Embervm.AsyncWriter so the reconciler's adopt-and-backfill discriminator can
  see a write in flight for that VM. The 1/2-arity form passes `nil` (no vm to
  register), keeping every existing caller unchanged.
  """
  @spec assign(GenServer.server(), String.t()) :: {:ok, map()} | {:error, term()}
  def assign(store \\ __MODULE__, task_id) do
    GenServer.call(store, {:assign, task_id, nil})
  end

  @spec assign(GenServer.server(), String.t(), binary() | nil, binary() | nil) :: {:ok, map()} | {:error, term()}
  def assign(store, task_id, vm_id, node_id) do
    GenServer.call(store, {:assign, task_id, vm_id, node_id})
  end

  def assign(store, task_id, vm_id) do
    GenServer.call(store, {:assign, task_id, vm_id, nil})
  end

  @spec start(GenServer.server(), String.t()) :: {:ok, map()} | {:error, term()}
  def start(store \\ __MODULE__, task_id) do
    GenServer.call(store, {:start, task_id, nil})
  end

  @spec start(GenServer.server(), String.t(), binary() | nil, binary() | nil) :: {:ok, map()} | {:error, term()}
  def start(store, task_id, vm_id, node_id) do
    GenServer.call(store, {:start, task_id, vm_id, node_id})
  end

  def start(store, task_id, vm_id) do
    GenServer.call(store, {:start, task_id, vm_id, nil})
  end

  @spec succeed(GenServer.server(), String.t(), map()) :: {:ok, map()} | {:error, term()}
  def succeed(store \\ __MODULE__, task_id, result) do
    GenServer.call(store, {:succeed, task_id, result, nil})
  end

  @doc """
  Like `succeed/3`, plus the task's raw usage stats (`%{cpu_ms, peak_rss_mib,
  wall_ms}`, from `AssignResponse.usage`) which are billed into the op payload
  and charged to the quota cache via the `on_metered` hook. `nil` usage charges
  nothing. The dispatcher calls this on the success path.
  """
  @spec succeed(GenServer.server(), String.t(), map(), map() | nil) ::
          {:ok, map()} | {:error, term()}
  def succeed(store, task_id, result, usage) do
    GenServer.call(store, {:succeed, task_id, result, usage})
  end

  @doc """
  Classifies `reason` against the task's workload retry config and applies the
  resulting transition (`:fail_retryable` or `:fail_permanent`). A permanent
  failure immediately chains into `:dead_letter` too (DLQ is on by default),
  so callers never need to issue a separate dead-letter call for the common
  path. Returns the updated task plus, for a retryable failure, the computed
  backoff so the (future) dispatcher knows when to call `retry/2`.
  """
  @spec fail(GenServer.server(), String.t(), atom()) ::
          {:ok, map()} | {:ok, map(), backoff_ms :: non_neg_integer()} | {:error, term()}
  def fail(store \\ __MODULE__, task_id, reason) do
    GenServer.call(store, {:fail, task_id, reason, nil})
  end

  @doc """
  Like `fail/3`, plus the task's raw usage stats when the failure still did
  measured work (a guest 4xx/5xx returns a well-formed response WITH usage);
  those are billed into the `:failed` op payload and charged to quota, so a
  workload that burns CPU then errors is not free. `nil` usage (transport /
  timeout failures, which report no usage) charges nothing.
  """
  @spec fail(GenServer.server(), String.t(), atom(), map() | nil) ::
          {:ok, map()} | {:ok, map(), backoff_ms :: non_neg_integer()} | {:error, term()}
  def fail(store, task_id, reason, usage) do
    GenServer.call(store, {:fail, task_id, reason, usage})
  end

  @spec retry(GenServer.server(), String.t()) :: {:ok, map()} | {:error, term()}
  def retry(store \\ __MODULE__, task_id) do
    GenServer.call(store, {:retry, task_id})
  end

  @spec get(GenServer.server(), String.t()) :: {:ok, map()} | :error
  def get(store \\ __MODULE__, task_id) do
    GenServer.call(store, {:get, task_id})
  end

  @doc """
  Reads a task's stored result (until its TTL) from the durable result store,
  returning `{:ok, map}`, `{:ok, nil}` (no result yet or TTL-reaped), or
  `{:error, reason}`. Serves `GET /v1/tasks/{id}/result`; this is a result-store
  read, never the ops log.
  """
  @spec get_result(GenServer.server(), String.t()) :: {:ok, map() | nil} | {:error, term()}
  def get_result(store \\ __MODULE__, task_id) do
    GenServer.call(store, {:get_result, task_id})
  end

  @doc """
  Reads the opaque guest-request envelope (path, headers, base64 body, content
  type) captured in the task's `submitted` op, or `{:ok, nil}` if the task is
  unknown. The dispatcher (Task 11) calls this at assign time to rebuild the
  `AssignRequest`. It reads the immutable op-log (the request is never projected
  into a column), so it is deliberately NOT held in the ETS hot set: a queued
  task keeps its (up to 8 MiB) body out of memory and out of the fair queue
  until the moment it is actually dispatched.
  """
  @spec get_request(GenServer.server(), String.t()) :: {:ok, map() | nil} | {:error, term()}
  def get_request(store \\ __MODULE__, task_id) do
    GenServer.call(store, {:get_request, task_id})
  end

  @doc """
  Pages the metering `usage` projection (Task 12), serving `GET /v1/usage`. A
  projection read delegated to the op-log, mirroring `get_result/2`: the router
  talks only to this store, never the op-log directly. See
  `Embervm.OpLog.list_usage/2` for the `opts` shape.
  """
  @spec list_usage(GenServer.server(), keyword()) :: {:ok, map()} | {:error, term()}
  def list_usage(store \\ __MODULE__, opts \\ []) do
    GenServer.call(store, {:list_usage, opts})
  end

  @doc """
  Every task the dispatcher's sweep must be able to reconcile: `queued` (waiting
  for a primed VM), `failed_retryable` (awaiting a retry), and the in-flight
  states `assigned`/`running`. Returns lightweight records (`task_id`,
  `workload`, `principal`, `state`, `attempt`), sorted by `task_id`, from the ETS
  hot set (never the op-log).

  This is the dispatcher's boot + safety-sweep reconcile source. It must include
  in-flight states because a task can be durably `assigned`/`running` with NO
  live worker owning it: a control-plane (or dispatcher-only) restart drops every
  worker while the op-log rebuild leaves the task in-flight; a partial
  `assign`-then-`start` commit leaves it `assigned` with no worker spawned; a
  completion whose terminal op-log append failed leaves it `running`. The
  dispatcher distinguishes such ORPHANS (no tracked worker) from genuinely
  running tasks and reclaims only the former, so no task is lost. `queued` tasks
  a dropped `on_queued` cast missed and `failed_retryable` tasks
  `reassign_in_flight/0` produced out of band are re-driven the same way.
  """
  @spec list_backlog(GenServer.server()) :: {:ok, [map()]}
  def list_backlog(store \\ __MODULE__) do
    GenServer.call(store, :list_backlog)
  end

  @doc """
  Lists dead-lettered tasks for `workload`, newest first, paged by `:limit`
  (default 50) and `:offset` (default 0). Serves `GET
  /v1/workloads/{name}/dead-letters` from the ETS hot set (bounded and rare, so
  a full scan is acceptable), never the op-log.
  """
  @spec list_dead_letters(GenServer.server(), String.t(), keyword()) :: {:ok, map()}
  def list_dead_letters(store \\ __MODULE__, workload, opts \\ []) do
    GenServer.call(store, {:list_dead_letters, workload, opts})
  end

  @doc """
  Re-queues a dead-lettered task (`dead_lettered -> queued`), resetting its
  attempt counter to 1 (a full fresh retry budget) and appending an audited
  `redrive` op. Serves `POST /v1/tasks/{id}/redrive`. Fails `:illegal_transition`
  for a task that is not dead-lettered.
  """
  @spec redrive(GenServer.server(), String.t()) :: {:ok, map()} | {:error, term()}
  def redrive(store \\ __MODULE__, task_id) do
    GenServer.call(store, {:redrive, task_id})
  end

  @doc """
  Expires a queued task whose `expires_at` has passed: `queued -> failed_permanent`
  with reason `expired`, appending the existing `:failed` op (no new op kind, no
  schema change). The dispatcher calls this when it pops a queued task past its TTL,
  so an over-budget parked task never dispatches after its deadline (ADR embervm/002,
  closing the D12 queued-task-TTL gap). Fails `:illegal_transition` for any task not
  currently `:queued` (only queued tasks are popped for dispatch). Expiry is not a
  processing failure, so it does NOT chain into the dead-letter queue: `failed_permanent`
  is terminal and sufficient.
  """
  @spec expire(GenServer.server(), String.t()) :: {:ok, map()} | {:error, term()}
  def expire(store \\ __MODULE__, task_id) do
    GenServer.call(store, {:expire, task_id})
  end

  @doc """
  Reassigns every in-flight task (`assigned` or `running`) as a retryable
  transport failure and returns how many were reassigned. This is the
  at-least-once path `Embervm.NodeRegistry` calls when a node goes down: a task
  that was on the node when its daemon stopped answering must be retried because
  we cannot know whether the guest completed. Each task flows through the exact
  same `Embervm.Retry` classification as any other transport failure (`:transport`
  is retryable by default), so a task with budget left becomes `failed_retryable`
  (the dispatcher's `retry/2` later moves it back to `queued`) and one whose
  budget is exhausted becomes `failed_permanent` and is dead-lettered, no
  special node-down code path. In v1 there is exactly one node, so "in-flight" is
  precisely "on the downed node"; multi-node filtering by assigned node is Task
  11's concern once tasks record where they were dispatched.
  """
  @spec reassign_in_flight(GenServer.server()) :: {:ok, non_neg_integer()}
  def reassign_in_flight(store \\ __MODULE__) do
    GenServer.call(store, {:reassign_in_flight, :transport})
  end

  # -- GenServer callbacks ---------------------------------------------------

  @impl true
  def init(opts) do
    # The backend module dispatched at every call site below, threaded alongside
    # :op_log (the server address) so a non-default backend never requires editing
    # this module. Defaults to the selected backend module.
    op_log_mod = Keyword.get(opts, :op_log_mod, Embervm.OpLog.SQLite)
    op_log = Keyword.get(opts, :op_log, op_log_mod)
    id_fun = Keyword.get(opts, :id_fun, &default_id/0)
    clock = Keyword.get(opts, :clock, &default_clock/0)
    # Fired on every transition INTO :queued (submit-created, retry, redrive) with
    # %{task_id, workload, principal}, so the dispatcher (Task 11) can enqueue the
    # task into its fair queue. Defaults to the dispatcher's own whereis-guarded
    # cast, a no-op when no dispatcher is running (this store's own unit tests
    # start none), mirroring the WorkloadWatcher -> BaseBuilder trigger seam.
    on_queued = Keyword.get(opts, :on_queued, &Embervm.Dispatcher.enqueue/1)
    # Fired AFTER a :succeeded/:failed op that carried usage lands durably, with
    # %{principal, ts, stats}, so Embervm.Metering can charge the quota cache off
    # the same write that recorded the durable usage. Defaults to a no-op so a
    # store with no metering wired (unit tests) drops the signal; the durable
    # `usage` projection is unaffected either way.
    on_metered = Keyword.get(opts, :on_metered, fn _event -> :ok end)
    # Off-hot-path lifecycle writes (ADR embervm/014 decision 2). When
    # async_lifecycle_writes is on, the :assigned/:started transitions advance ETS
    # synchronously (so the FSM's later terminal transition is legal and reads are
    # correct) but hand their durable oplog append to Embervm.AsyncWriter instead of
    # blocking the caller on it. Off (default): exact write-through ordering (append
    # THEN ETS, caller blocked on the durable write), today's behaviour. Never
    # applies to :submitted or any terminal/usage op (those gate metering and stay
    # synchronous), only the two dispatch-path transitions below.
    async_writer = Keyword.get(opts, :async_writer, Embervm.AsyncWriter)
    async_lifecycle_writes = Keyword.get(opts, :async_lifecycle_writes, false)

    tasks = :ets.new(@tasks_table, [:set, :private])
    idem = :ets.new(@idem_table, [:set, :private])

    state = %{
      op_log: op_log,
      op_log_mod: op_log_mod,
      id_fun: id_fun,
      clock: clock,
      on_queued: on_queued,
      on_metered: on_metered,
      async_writer: async_writer,
      async_lifecycle_writes: async_lifecycle_writes,
      tasks: tasks,
      idem: idem
    }

    case rebuild(state) do
      :ok ->
        {:ok, state}

      {:error, reason} ->
        {:stop, {:rebuild_failed, reason}}
    end
  end

  # Rebuild path: read every row the op-log's projection currently has (queued
  # through terminal; terminal rows stay queryable until the op-log's own
  # compaction prunes them) and populate both ETS tables from scratch. This is
  # the entire recovery story: no per-op replay, just the projection's current
  # snapshot, because the op-log projection already IS the authoritative
  # current state.
  defp rebuild(%{op_log: op_log, op_log_mod: op_log_mod, tasks: tasks, idem: idem}) do
    case op_log_mod.load_tasks(op_log) do
      {:ok, rows} ->
        Enum.each(rows, fn row ->
          task = row_to_task(row)
          :ets.insert(tasks, {task.task_id, task})

          if task.idempotency_key do
            :ets.insert(idem, {{task.workload, task.idempotency_key}, task.task_id})
          end
        end)

        :ok

      {:error, reason} ->
        {:error, reason}
    end
  end

  defp row_to_task(row) do
    %{
      task_id: row.task_id,
      tenant: row.tenant,
      principal: row.principal,
      workload: row.workload,
      state: state_from_string(row.state),
      attempt: row.attempt + 1,
      idempotency_key: row.idempotency_key,
      submitted_at: row.submitted_at,
      updated_at: row.updated_at,
      expires_at: row.expires_at
    }
  end

  # Explicit map rather than String.to_existing_atom/1: every TaskState atom
  # is already loaded (this module references them all directly above), so
  # to_existing_atom would work too, but an explicit map fails loudly with a
  # clear error on an unrecognized string instead of raising ArgumentError
  # deep in the atom table, and doubles as documentation of the exact string
  # values the SQL projection writes.
  @state_strings %{
    "queued" => :queued,
    "assigned" => :assigned,
    "running" => :running,
    "succeeded" => :succeeded,
    "failed_retryable" => :failed_retryable,
    "failed_permanent" => :failed_permanent,
    "dead_lettered" => :dead_lettered
  }

  defp state_from_string(str) do
    Map.fetch!(@state_strings, str)
  end

  @impl true
  def handle_call({:submit, attrs}, _from, state) do
    workload = Map.fetch!(attrs, :workload)
    idempotency_key = Map.get(attrs, :idempotency_key)

    case idempotency_key && :ets.lookup(state.idem, {workload, idempotency_key}) do
      [{_key, existing_task_id}] ->
        dedupe_or_resubmit(attrs, workload, idempotency_key, existing_task_id, state)

      _ ->
        do_submit(attrs, workload, idempotency_key, state)
    end
  end

  # An idempotency-key ETS hit. In-flight (non-terminal) duplicate suppression is
  # ABSOLUTE: a still-queued/assigned/running/failed_retryable existing task means
  # the resubmit is a duplicate of live work, so return :existing unchanged. For a
  # TERMINAL existing task, dedupe holds only while its result is still serveable:
  # we key the dedupe on the SAME read-time expiry signal as get_result/2 (ADR
  # embervm/002 rule 1), so "the GET 404s" and "the resubmit runs fresh" stay
  # consistent. When the cached result is gone (never had one, or past its TTL) the
  # task is treated as ABSENT: evict its stale projection row (so the fresh
  # :submitted append does not collide on the unique (workload, key) index), drop
  # its ETS entries, and submit anew.
  defp dedupe_or_resubmit(attrs, workload, idempotency_key, existing_task_id, state) do
    case fetch_task(state, existing_task_id) do
      {:ok, %{state: task_state}} ->
        cond do
          task_state not in TaskState.terminal_states() ->
            {:reply, {:ok, :existing, existing_task_id}, state}

          match?({:ok, result} when is_map(result), live_result(state, existing_task_id)) ->
            {:reply, {:ok, :existing, existing_task_id}, state}

          true ->
            fresh_resubmit(attrs, workload, idempotency_key, existing_task_id, state)
        end

      {:error, _reason} ->
        # ETS lost the id (a compaction/eviction race): submit fresh.
        do_submit(attrs, workload, idempotency_key, state)
    end
  end

  # Prune the stale existing task's durable projection row (results cascade) and its
  # ETS entries, then submit fresh under the same key. The old task's immutable ops
  # stay in the journal until horizon compaction; only the projection is pruned early.
  defp fresh_resubmit(attrs, workload, idempotency_key, old_task_id, state) do
    case state.op_log_mod.evict_task(state.op_log, old_task_id) do
      :ok ->
        :ets.delete(state.tasks, old_task_id)
        :ets.delete(state.idem, {workload, idempotency_key})
        do_submit(attrs, workload, idempotency_key, state)

      {:error, _reason} = error ->
        {:reply, error, state}
    end
  end

  def handle_call({:assign, task_id, vm_id, node_id}, _from, state) do
    apply_lifecycle_transition(state, task_id, :assign, :assigned, vm_id, node_id)
  end

  def handle_call({:assign, task_id, vm_id}, _from, state) do
    apply_lifecycle_transition(state, task_id, :assign, :assigned, vm_id, nil)
  end

  def handle_call({:start, task_id, vm_id, node_id}, _from, state) do
    apply_lifecycle_transition(state, task_id, :start, :started, vm_id, node_id)
  end

  def handle_call({:start, task_id, vm_id}, _from, state) do
    apply_lifecycle_transition(state, task_id, :start, :started, vm_id, nil)
  end

  def handle_call({:succeed, task_id, result, usage}, _from, state) do
    payload =
      %{
        status_code: Map.fetch!(result, :status_code),
        body: Map.get(result, :body),
        size_bytes: Map.fetch!(result, :size_bytes),
        truncated: Map.get(result, :truncated, false),
        expires_at: Map.get(result, :expires_at),
        headers: Map.get(result, :headers, %{})
      }
      |> maybe_put_usage(usage)

    case apply_payload_transition(state, task_id, :succeed, :succeeded, payload) do
      {:reply, {:ok, updated}, _state} = reply ->
        notify_metered(state, updated, usage)
        reply

      other ->
        other
    end
  end

  def handle_call({:fail, task_id, reason, usage}, _from, state) do
    with {:ok, task} <- fetch_task(state, task_id) do
      cfg = cfg_for(task.workload)
      event = Embervm.Retry.classify(reason, task.attempt, cfg)
      next = TaskState.transition!(task.state, event)
      payload = maybe_put_usage(%{state: next, reason: reason}, usage)

      case append_and_update(state, task, :failed, next, payload) do
        {:ok, updated} ->
          notify_metered(state, updated, usage)
          reply_after_fail(state, updated, event, task.attempt, cfg)

        {:error, _reason} = error ->
          {:reply, error, state}
      end
    else
      {:error, _reason} = error -> {:reply, error, state}
    end
  end

  def handle_call({:retry, task_id}, _from, state) do
    with {:ok, task} <- fetch_task(state, task_id),
         {:ok, next} <- TaskState.transition(task.state, :retry) do
      ts = state.clock.()

      op = %Op{
        kind: :retried,
        tenant: task.tenant,
        principal: task.principal,
        workload: task.workload,
        task_id: task_id,
        ts: ts,
        payload: %{}
      }

      case state.op_log_mod.append(state.op_log, op) do
        {:ok, _seq} ->
          updated = %{task | state: next, attempt: task.attempt + 1, updated_at: ts}
          :ets.insert(state.tasks, {task_id, updated})
          notify_queued(state, updated)
          {:reply, {:ok, updated}, state}

        {:error, _reason} = error ->
          {:reply, error, state}
      end
    else
      {:error, _reason} = error -> {:reply, error, state}
    end
  end

  def handle_call({:get, task_id}, _from, state) do
    case :ets.lookup(state.tasks, task_id) do
      [{^task_id, task}] -> {:reply, {:ok, task}, state}
      [] -> {:reply, :error, state}
    end
  end

  def handle_call({:get_result, task_id}, _from, state) do
    {:reply, live_result(state, task_id), state}
  end

  def handle_call({:get_request, task_id}, _from, state) do
    {:reply, state.op_log_mod.load_request(state.op_log, task_id), state}
  end

  def handle_call({:list_usage, opts}, _from, state) do
    {:reply, state.op_log_mod.list_usage(state.op_log, opts), state}
  end

  def handle_call(:list_backlog, _from, state) do
    backlog =
      :ets.foldl(
        fn {_id, task}, acc ->
          if task.state in [:queued, :failed_retryable, :assigned, :running] do
            [%{
               task_id: task.task_id,
               workload: task.workload,
               principal: task.principal,
               state: task.state,
               attempt: task.attempt
             }
             | acc]
          else
            acc
          end
        end,
        [],
        state.tasks
      )
      |> Enum.sort_by(& &1.task_id)

    {:reply, {:ok, backlog}, state}
  end

  def handle_call({:list_dead_letters, workload, opts}, _from, state) do
    limit = Keyword.get(opts, :limit, 50)
    offset = Keyword.get(opts, :offset, 0)

    all =
      :ets.foldl(
        fn {_id, task}, acc ->
          if task.workload == workload and task.state == :dead_lettered do
            [task | acc]
          else
            acc
          end
        end,
        [],
        state.tasks
      )
      |> Enum.sort_by(& &1.submitted_at, :desc)

    page = all |> Enum.drop(offset) |> Enum.take(limit)
    {:reply, {:ok, %{items: page, total: length(all), limit: limit, offset: offset}}, state}
  end

  def handle_call({:redrive, task_id}, _from, state) do
    with {:ok, task} <- fetch_task(state, task_id),
         {:ok, next} <- TaskState.transition(task.state, :redrive) do
      ts = state.clock.()

      op = %Op{
        kind: :redrive,
        tenant: task.tenant,
        principal: task.principal,
        workload: task.workload,
        task_id: task_id,
        ts: ts,
        payload: %{}
      }

      case state.op_log_mod.append(state.op_log, op) do
        {:ok, _seq} ->
          updated = %{task | state: next, attempt: 1, updated_at: ts}
          :ets.insert(state.tasks, {task_id, updated})
          notify_queued(state, updated)
          {:reply, {:ok, updated}, state}

        {:error, _reason} = error ->
          {:reply, error, state}
      end
    else
      {:error, _reason} = error -> {:reply, error, state}
    end
  end

  def handle_call({:expire, task_id}, _from, state) do
    with {:ok, task} <- fetch_task(state, task_id),
         {:ok, next} <- TaskState.transition(task.state, :expire) do
      case append_and_update(state, task, :failed, next, %{state: next, reason: :expired}) do
        {:ok, updated} -> {:reply, {:ok, updated}, state}
        {:error, _reason} = error -> {:reply, error, state}
      end
    else
      {:error, _reason} = error -> {:reply, error, state}
    end
  end

  def handle_call({:reassign_in_flight, reason}, _from, state) do
    in_flight =
      :ets.foldl(
        fn {_id, task}, acc ->
          if task.state in [:assigned, :running], do: [task | acc], else: acc
        end,
        [],
        state.tasks
      )

    Enum.each(in_flight, fn task -> reassign_one(state, task, reason) end)
    {:reply, {:ok, length(in_flight)}, state}
  end

  # -- submit helpers --------------------------------------------------------

  defp do_submit(attrs, workload, idempotency_key, state) do
    task_id = state.id_fun.()
    ts = state.clock.()
    expires_at = Map.get(attrs, :expires_at)

    op = %Op{
      kind: :submitted,
      tenant: Map.fetch!(attrs, :tenant),
      principal: Map.get(attrs, :principal),
      workload: workload,
      task_id: task_id,
      ts: ts,
      payload: %{
        idempotency_key: idempotency_key,
        expires_at: expires_at,
        request: Map.get(attrs, :request)
      }
    }

    case state.op_log_mod.append(state.op_log, op) do
      {:ok, _seq} ->
        task = %{
          task_id: task_id,
          tenant: op.tenant,
          principal: op.principal,
          workload: workload,
          state: :queued,
          attempt: 1,
          idempotency_key: idempotency_key,
          submitted_at: ts,
          updated_at: ts,
          expires_at: expires_at
        }

        :ets.insert(state.tasks, {task_id, task})

        if idempotency_key do
          :ets.insert(state.idem, {{workload, idempotency_key}, task_id})
        end

        notify_queued(state, task)
        {:reply, {:ok, :created, task_id}, state}

      {:error, _reason} = error ->
        {:reply, error, state}
    end
  end

  # -- transition helpers -----------------------------------------------------

  defp apply_payload_transition(state, task_id, event, op_kind, payload) do
    with {:ok, task} <- fetch_task(state, task_id),
         {:ok, next} <- TaskState.transition(task.state, event) do
      case append_and_update(state, task, op_kind, next, payload) do
        {:ok, updated} -> {:reply, {:ok, updated}, state}
        {:error, _reason} = error -> {:reply, error, state}
      end
    else
      {:error, _reason} = error -> {:reply, error, state}
    end
  end

  # Shared path for the dispatch lifecycle transitions (assign, start): look up
  # the task, ask the FSM for the next state, append, and update ETS on success.
  defp apply_transition(state, task_id, event, op_kind, vm_id, node_id) do
    with {:ok, task} <- fetch_task(state, task_id),
         {:ok, next} <- TaskState.transition(task.state, event) do
      payload =
        if op_kind in [:assigned, :started] do
          # node_id is the K8s node where the VM primed/started.
          %{epoch: task.attempt, vm_id: vm_id, node_id: node_id}
        else
          %{}
        end

      case append_and_update(state, task, op_kind, next, payload) do
        {:ok, updated} -> {:reply, {:ok, updated}, state}
        {:error, _reason} = error -> {:reply, error, state}
      end
    else
      {:error, _reason} = error -> {:reply, error, state}
    end
  end

  # The two dispatch-path lifecycle transitions (:assign, :start). Gate off:
  # identical to apply_transition (write-through, caller blocked on the durable
  # append). Gate on (ADR embervm/014 decision 2): advance ETS SYNCHRONOUSLY so the
  # task is immediately :assigned/:running for reads and for the FSM's later terminal
  # transition, then hand the durable oplog append to Embervm.AsyncWriter, so the
  # caller (the dispatch hot path) never blocks on the durable write. A CP crash
  # before the async append lands loses the op: the task's durable row stays
  # :queued, and the dispatcher's boot backlog sweep re-drives it (no task is ever
  # reconcile-DESTROYED, so a lost task lifecycle write is self-healing without a
  # discriminator). vm_id is registered as a pending write anyway, for parity with
  # the session path and so a shared reconciler could consult it if ever needed.
  defp apply_lifecycle_transition(state, task_id, event, op_kind, vm_id, node_id) do
    if state.async_lifecycle_writes do
      apply_transition_async(state, task_id, event, op_kind, vm_id, node_id)
    else
      apply_transition(state, task_id, event, op_kind, vm_id, node_id)
    end
  end

  defp apply_transition_async(state, task_id, event, op_kind, vm_id, node_id) do
    with {:ok, task} <- fetch_task(state, task_id),
         {:ok, next} <- TaskState.transition(task.state, event) do
      ts = state.clock.()
      updated = %{task | state: next, updated_at: ts}
      # ETS advances NOW (synchronous), so reads and the later terminal transition
      # see the advanced state; the durable append is deferred.
      :ets.insert(state.tasks, {task_id, updated})

      op = %Op{
        kind: op_kind,
        tenant: task.tenant,
        principal: task.principal,
        workload: task.workload,
        task_id: task_id,
        ts: ts,
        # The dispatch attempt (1-based ETS `attempt`, unchanged by :assign/:start)
        # this deferred op belongs to. The op-log projection's monotonic guard
        # (advance_task_state/5) applies it only while the durable row has not yet
        # `:retried` past this attempt, so a stale attempt-N :assigned/:started that
        # lands after a retry re-queued the task (attempt N+1) is dropped instead of
        # re-assigning a worker that no longer exists.
        # node_id is the K8s node where the VM primed/started.
        payload: %{epoch: task.attempt, vm_id: vm_id, node_id: node_id}
      }

      Embervm.AsyncWriter.enqueue(state.async_writer, %{
        op: op,
        op_log_mod: state.op_log_mod,
        op_log: state.op_log,
        vm_id: vm_id
      })

      {:reply, {:ok, updated}, state}
    else
      {:error, _reason} = error -> {:reply, error, state}
    end
  end

  # The write-through core: append to the op-log, and ONLY on {:ok, seq} write
  # the new state into ETS. On append failure ETS is left untouched (the task
  # stays exactly as durable as the op-log currently agrees it is) and the
  # error is handed back to the caller.
  defp append_and_update(state, task, op_kind, next_state, payload) do
    ts = state.clock.()

    op = %Op{
      kind: op_kind,
      tenant: task.tenant,
      principal: task.principal,
      workload: task.workload,
      task_id: task.task_id,
      ts: ts,
      payload: payload
    }

    case state.op_log_mod.append(state.op_log, op) do
      {:ok, _seq} ->
        updated = %{task | state: next_state, updated_at: ts}
        :ets.insert(state.tasks, {task.task_id, updated})
        # Wake any sync-submit caller parked on this task the moment it settles
        # terminal (succeeded or dead_lettered). No-op when nobody is parked, so
        # it is safe to call on every transition; guarded to terminal states so
        # intermediate assign/start transitions do not spuriously wake waiters.
        if TaskState.terminal?(next_state) do
          Embervm.SyncWait.notify(task.task_id, next_state)
        end

        {:ok, updated}

      {:error, reason} ->
        {:error, reason}
    end
  end

  # After a :failed op lands, a permanent failure immediately chains into
  # :dead_letter (DLQ enabled by default) so the common permanent-failure path
  # is one `fail/2` call, not two. A retryable failure instead reports the
  # backoff so a future dispatcher (Task 11) knows when to call retry/2.
  defp reply_after_fail(state, task, :fail_permanent, _prior_attempt, _cfg) do
    case TaskState.transition(task.state, :dead_letter) do
      {:ok, next} ->
        case append_and_update(state, task, :dead_lettered, next, %{}) do
          {:ok, updated} -> {:reply, {:ok, updated}, state}
          {:error, _reason} = error -> {:reply, error, state}
        end

      {:error, _reason} = error ->
        {:reply, error, state}
    end
  end

  defp reply_after_fail(state, task, :fail_retryable, prior_attempt, cfg) do
    backoff = Embervm.Retry.backoff_ms(prior_attempt, cfg)
    {:reply, {:ok, task, backoff}, state}
  end

  # Reassigns one in-flight task as a failure of `reason` (see
  # reassign_in_flight/1). Mirrors the {:fail, ...} handler's classify ->
  # transition -> append path, including the permanent-failure dead-letter chain,
  # but discards the reply/backoff (the caller reassigns a whole batch and only
  # needs the count). Best-effort per task: an op-log append error for one task is
  # logged-by-omission and does not abort the batch, since a downed node's other
  # tasks must still be reassigned.
  defp reassign_one(state, task, reason) do
    cfg = cfg_for(task.workload)
    event = Embervm.Retry.classify(reason, task.attempt, cfg)
    next = TaskState.transition!(task.state, event)

    case append_and_update(state, task, :failed, next, %{state: next, reason: reason}) do
      {:ok, updated} when event == :fail_permanent ->
        case TaskState.transition(updated.state, :dead_letter) do
          {:ok, dl} -> append_and_update(state, updated, :dead_lettered, dl, %{})
          {:error, _} -> :ok
        end

      {:ok, _updated} ->
        :ok

      {:error, _reason} ->
        :ok
    end
  end

  # Signal the dispatcher that a task is (freshly, or again) queued. Best-effort
  # and fire-and-forget: the hook is a cast the dispatcher whereis-guards, so a
  # store with no dispatcher wired (unit tests) or a dispatcher mid-restart just
  # drops the signal, and the dispatcher's boot/periodic backlog sweep re-drives
  # anything a dropped signal left behind. A raise in the hook must never fail
  # the transition (already durable), so it is caught.
  defp notify_queued(state, task) do
    try do
      state.on_queued.(%{task_id: task.task_id, workload: task.workload, principal: task.principal})
    rescue
      _ -> :ok
    catch
      _, _ -> :ok
    end

    :ok
  end

  # Bill raw usage stats into an op payload: the raw counters are kept per task
  # (so the billing basis can be rebased later without losing history) alongside
  # the computed vcpu_seconds/gb_seconds the `usage` projection accumulates. No
  # `:usage` key is added when usage is nil, so the projection stays a no-op for
  # transport/timeout failures and for any caller that reports nothing.
  defp maybe_put_usage(payload, nil), do: payload

  defp maybe_put_usage(payload, stats) when is_map(stats) do
    Map.put(payload, :usage, Map.merge(stats, Embervm.Usage.billed(stats)))
  end

  # Signal the metering process that a task with usage settled durably. Like
  # notify_queued: best-effort, caught, fire-and-forget, so a raise or missing
  # hook never fails the (already durable) transition. The durable `usage`
  # projection is written transactionally regardless of this hook; this only
  # updates the advisory quota cache.
  defp notify_metered(_state, _task, nil), do: :ok

  defp notify_metered(state, task, stats) when is_map(stats) do
    try do
      state.on_metered.(%{principal: task.principal, ts: task.updated_at, stats: stats})
    rescue
      _ -> :ok
    catch
      _, _ -> :ok
    end

    :ok
  end

  # Read-time result TTL (ADR embervm/002 rule 1): loads the stored result and
  # treats one whose integer `expires_at` is already past (against the injected
  # clock) as absent, replying {:ok, nil}. This makes GET /v1/tasks/{id}/result
  # 404 the moment the TTL lapses, independent of whether the sweeper has run, and
  # is the SAME signal the submit dedupe path keys on so the two stay consistent.
  # The clock lives here in the store, so the filter stays here rather than in the
  # op-log's load_result (whose behaviour signature is unchanged).
  defp live_result(state, task_id) do
    case state.op_log_mod.load_result(state.op_log, task_id) do
      {:ok, %{expires_at: expires_at} = result} when is_integer(expires_at) ->
        if expires_at < state.clock.(), do: {:ok, nil}, else: {:ok, result}

      other ->
        other
    end
  end

  defp fetch_task(state, task_id) do
    case :ets.lookup(state.tasks, task_id) do
      [{^task_id, task}] -> {:ok, task}
      [] -> {:error, {:not_found, task_id}}
    end
  end

  # Reads the per-workload retry config from Embervm.WorkloadCatalog (kept up
  # to date by Embervm.WorkloadWatcher's reconcile loop, Task 5).
  # WorkloadCatalog.retry_config/1 already falls back to
  # Embervm.Retry.default_config/0 both for a workload that is not (yet, or no
  # longer) cataloged and for the case the catalog table does not exist at all
  # (the watcher has not booted), so this call site needs no fallback logic
  # of its own.
  defp cfg_for(workload), do: Embervm.WorkloadCatalog.retry_config(workload)

  defp default_id do
    16
    |> :crypto.strong_rand_bytes()
    |> Base.encode32(case: :lower, padding: false)
  end

  defp default_clock, do: System.system_time(:millisecond)
end
