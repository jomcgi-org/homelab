defmodule Embervm.TaskStoreTest do
  @moduledoc """
  Exercises `Embervm.TaskStore` against a real (unnamed) `Embervm.OpLog.SQLite`
  on a fresh temp file per test, mirroring the op-log's own test idiom so each
  async test gets an independent op-log + task-store pair. `clock` and
  `id_fun` are always injected so state and IDs are deterministic and the
  restart-recovery test can assert exact values, not just "some timestamp".
  """
  use ExUnit.Case, async: true

  alias Embervm.OpLog.SQLite
  alias Embervm.TaskStore

  setup do
    path = Path.join(System.tmp_dir!(), "embervm_taskstore_test_#{System.unique_integer([:positive, :monotonic])}.db")
    on_exit(fn -> File.rm_rf!(path) end)
    %{path: path}
  end

  # Starts an unnamed op-log and an unnamed TaskStore wired to it, both
  # PID-addressed so concurrently-running async tests never collide.
  defp start_pair(path, opts \\ []) do
    {:ok, op_log} = SQLite.start_link(path: path, name: nil)

    id_fun = Keyword.get(opts, :id_fun, sequential_id_fun())
    clock = Keyword.get(opts, :clock, sequential_clock())

    {:ok, store} =
      TaskStore.start_link(op_log: op_log, name: nil, id_fun: id_fun, clock: clock)

    {op_log, store}
  end

  # Deterministic monotonically-increasing task IDs ("task-1", "task-2", ...)
  # so assertions can name exact IDs instead of pattern-matching opaque
  # random ones.
  defp sequential_id_fun do
    {:ok, counter} = Agent.start_link(fn -> 0 end)

    fn ->
      n = Agent.get_and_update(counter, fn n -> {n + 1, n + 1} end)
      "task-#{n}"
    end
  end

  # Deterministic monotonically-increasing millisecond clock, so op-log ts
  # ordering and ETS updated_at assertions are exact.
  defp sequential_clock do
    {:ok, counter} = Agent.start_link(fn -> 1_000 end)
    fn -> Agent.get_and_update(counter, fn n -> {n, n + 1} end) end
  end

  test "happy path: submit -> assign -> start -> succeed", %{path: path} do
    {op_log, store} = start_pair(path)

    {:ok, :created, task_id} =
      TaskStore.submit(store, %{tenant: "t1", principal: "p1", workload: "wl-a"})

    {:ok, task} = TaskStore.get(store, task_id)
    assert task.state == :queued
    assert task.attempt == 1

    {:ok, task} = TaskStore.assign(store, task_id)
    assert task.state == :assigned

    {:ok, task} = TaskStore.start(store, task_id)
    assert task.state == :running

    {:ok, task} =
      TaskStore.succeed(store, task_id, %{status_code: 200, body: "ok", size_bytes: 2, truncated: false})

    assert task.state == :succeeded

    {:ok, ets_task} = TaskStore.get(store, task_id)
    assert ets_task.state == :succeeded

    {:ok, ops} = SQLite.read_from(op_log, 0)
    kinds = ops |> Enum.filter(&(&1.task_id == task_id)) |> Enum.map(& &1.kind)
    assert kinds == [:submitted, :assigned, :started, :succeeded]
  end

  test "idempotency dedupe: same {workload, idempotency_key} returns the existing task, no new op", %{
    path: path
  } do
    {op_log, store} = start_pair(path)

    attrs = %{tenant: "t1", principal: "p1", workload: "wl-a", idempotency_key: "idem-1"}

    {:ok, :created, task_id_1} = TaskStore.submit(store, attrs)
    {:ok, :existing, task_id_2} = TaskStore.submit(store, attrs)

    assert task_id_1 == task_id_2

    {:ok, ops} = SQLite.read_from(op_log, 0)
    submitted = Enum.filter(ops, &(&1.kind == :submitted))
    assert length(submitted) == 1
  end

  test "retry path: retryable failures decrement toward permanent, then dead-letter", %{path: path} do
    {_op_log, store} = start_pair(path)

    {:ok, :created, task_id} =
      TaskStore.submit(store, %{tenant: "t1", principal: "p1", workload: "wl-a"})

    {:ok, _} = TaskStore.assign(store, task_id)

    # attempt 1 of 3 (default max_attempts): retryable, backoff returned.
    {:ok, task, backoff} = TaskStore.fail(store, task_id, :transport)
    assert task.state == :failed_retryable
    assert is_integer(backoff) and backoff >= 0

    {:ok, task} = TaskStore.retry(store, task_id)
    assert task.state == :queued
    assert task.attempt == 2

    {:ok, _} = TaskStore.assign(store, task_id)

    # attempt 2 of 3: still retryable.
    {:ok, task, _backoff} = TaskStore.fail(store, task_id, :transport)
    assert task.state == :failed_retryable

    {:ok, task} = TaskStore.retry(store, task_id)
    assert task.attempt == 3

    {:ok, _} = TaskStore.assign(store, task_id)

    # attempt 3 of 3 (== max_attempts): budget exhausted -> permanent, which
    # immediately chains into dead_lettered.
    {:ok, task} = TaskStore.fail(store, task_id, :transport)
    assert task.state == :dead_lettered

    {:ok, ets_task} = TaskStore.get(store, task_id)
    assert ets_task.state == :dead_lettered
    assert ets_task.attempt == 3
  end

  test "permanent-on-4xx: guest4xx is never retryable, goes straight to dead-lettered", %{path: path} do
    {_op_log, store} = start_pair(path)

    {:ok, :created, task_id} =
      TaskStore.submit(store, %{tenant: "t1", principal: "p1", workload: "wl-a"})

    {:ok, _} = TaskStore.assign(store, task_id)

    {:ok, task} = TaskStore.fail(store, task_id, :guest4xx)
    assert task.state == :dead_lettered

    {:ok, ets_task} = TaskStore.get(store, task_id)
    assert ets_task.state == :dead_lettered
    # Never retried: attempt is still 1 (first and only try).
    assert ets_task.attempt == 1
  end

  test "restart recovery: a fresh TaskStore against the same op-log file rebuilds exact state and attempt", %{
    path: path
  } do
    {op_log, store} = start_pair(path)

    # Task A: full happy path to a terminal state.
    {:ok, :created, task_a} =
      TaskStore.submit(store, %{tenant: "t1", principal: "p1", workload: "wl-a"})

    {:ok, _} = TaskStore.assign(store, task_a)
    {:ok, _} = TaskStore.start(store, task_a)

    {:ok, _} =
      TaskStore.succeed(store, task_a, %{status_code: 200, body: "ok", size_bytes: 2, truncated: false})

    # Task B: in-flight, sitting in :assigned when we kill the store.
    {:ok, :created, task_b} =
      TaskStore.submit(store, %{tenant: "t1", principal: "p1", workload: "wl-b"})

    {:ok, _} = TaskStore.assign(store, task_b)

    # Task C: retried once, sitting back in :queued at attempt 2.
    {:ok, :created, task_c} =
      TaskStore.submit(store, %{tenant: "t1", principal: "p1", workload: "wl-c"})

    {:ok, _} = TaskStore.assign(store, task_c)
    {:ok, _, _backoff} = TaskStore.fail(store, task_c, :transport)
    {:ok, _} = TaskStore.retry(store, task_c)

    {:ok, expected_a} = TaskStore.get(store, task_a)
    {:ok, expected_b} = TaskStore.get(store, task_b)
    {:ok, expected_c} = TaskStore.get(store, task_c)

    # Kill the store (simulating a crash/supervisor restart) but keep the
    # op-log file (and even the same op-log process, mirroring the
    # :rest_for_one wiring where the op-log survives its own restart's
    # sibling restart of TaskStore... here we just prove TaskStore alone
    # rebuilds correctly against the durable file).
    :ok = GenServer.stop(store)

    {:ok, store2} = TaskStore.start_link(op_log: op_log, name: nil)

    {:ok, rebuilt_a} = TaskStore.get(store2, task_a)
    {:ok, rebuilt_b} = TaskStore.get(store2, task_b)
    {:ok, rebuilt_c} = TaskStore.get(store2, task_c)

    assert rebuilt_a.state == expected_a.state
    assert rebuilt_a.attempt == expected_a.attempt
    assert rebuilt_a.state == :succeeded

    assert rebuilt_b.state == expected_b.state
    assert rebuilt_b.attempt == expected_b.attempt
    assert rebuilt_b.state == :assigned

    assert rebuilt_c.state == expected_c.state
    assert rebuilt_c.attempt == expected_c.attempt
    assert rebuilt_c.state == :queued
    assert rebuilt_c.attempt == 2
  end

  test "write-through: a rejected op-log append never touches ETS", %{path: path} do
    # A constant id_fun forces the SECOND submit's :submitted append to
    # collide on the op-log's tasks.task_id PRIMARY KEY, which is a real
    # append failure ({:error, _}) distinct from TaskStore's own idempotency
    # pre-check (that check only fires when idempotency_key is non-nil; both
    # submits below pass nil, so the collision is reached at the op-log
    # instead of being short-circuited earlier). This proves the
    # write-through ordering end to end: the op-log rejects the write, and
    # ETS must show exactly the pre-existing task, never a corrupted or
    # partially-applied second entry under the same key.
    {op_log, store} = start_pair(path, id_fun: fn -> "fixed-id" end)

    {:ok, :created, task_id} =
      TaskStore.submit(store, %{tenant: "t1", principal: "p1", workload: "wl-a"})

    assert task_id == "fixed-id"

    {:ok, before} = TaskStore.get(store, task_id)
    assert before.state == :queued

    result = TaskStore.submit(store, %{tenant: "t1", principal: "p1", workload: "wl-b"})
    assert match?({:error, _}, result)

    # ETS still has exactly the original task under "fixed-id": the rejected
    # append never created or overwrote an ETS entry.
    {:ok, after_attempt} = TaskStore.get(store, task_id)
    assert after_attempt == before

    # And the op-log itself only ever recorded the one successful :submitted
    # op; the rejected append left no trace.
    {:ok, ops} = SQLite.read_from(op_log, 0)
    assert Enum.count(ops, &(&1.kind == :submitted)) == 1
  end

  test "get_result returns the stored result, then nil for an unknown task", %{path: path} do
    {_op_log, store} = start_pair(path)

    {:ok, :created, task_id} =
      TaskStore.submit(store, %{tenant: "t1", principal: "p1", workload: "wl-a"})

    {:ok, _} = TaskStore.assign(store, task_id)
    {:ok, _} = TaskStore.start(store, task_id)

    {:ok, _} =
      TaskStore.succeed(store, task_id, %{
        status_code: 201,
        body: "FINDINGS",
        size_bytes: 8,
        truncated: false
      })

    assert {:ok, %{status_code: 201, body: "FINDINGS", truncated: false}} =
             TaskStore.get_result(store, task_id)

    assert {:ok, nil} = TaskStore.get_result(store, "does-not-exist")
  end

  test "redrive re-queues a dead-lettered task, resets attempt, and survives restart", %{
    path: path
  } do
    {op_log, store} = start_pair(path)

    {:ok, :created, task_id} =
      TaskStore.submit(store, %{tenant: "t1", principal: "p1", workload: "wl-a"})

    # Drive to dead-lettered via a permanent (guest4xx) failure.
    {:ok, _} = TaskStore.assign(store, task_id)
    {:ok, dl} = TaskStore.fail(store, task_id, :guest4xx)
    assert dl.state == :dead_lettered

    {:ok, redriven} = TaskStore.redrive(store, task_id)
    assert redriven.state == :queued
    assert redriven.attempt == 1

    # Redriving a task that is not dead-lettered is an illegal transition.
    assert {:error, {:illegal_transition, :queued, :redrive}} =
             TaskStore.redrive(store, task_id)

    # The reset persists: a fresh TaskStore rebuilt from the op-log sees queued
    # at attempt 1, proving the :redrive projection wrote through durably.
    :ok = GenServer.stop(store)
    {:ok, store2} = TaskStore.start_link(op_log: op_log, name: nil)

    {:ok, rebuilt} = TaskStore.get(store2, task_id)
    assert rebuilt.state == :queued
    assert rebuilt.attempt == 1
  end
end
