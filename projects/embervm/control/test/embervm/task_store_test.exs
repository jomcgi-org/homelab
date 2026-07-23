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
  alias Embervm.WorkloadCatalog

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
    # Default the on_queued hook to a no-op so an unnamed test store never casts
    # into the application's supervised (global) dispatcher; tests that assert on
    # the hook inject their own.
    on_queued = Keyword.get(opts, :on_queued, fn _ -> :ok end)

    {:ok, store} =
      TaskStore.start_link(op_log: op_log, name: nil, id_fun: id_fun, clock: clock, on_queued: on_queued)

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

  test "guest response headers round-trip through succeed + get_result", %{path: path} do
    {_op_log, store} = start_pair(path)

    {:ok, :created, task_id} =
      TaskStore.submit(store, %{tenant: "t1", principal: "p1", workload: "wl-a"})

    {:ok, _} = TaskStore.assign(store, task_id)
    {:ok, _} = TaskStore.start(store, task_id)

    {:ok, _} =
      TaskStore.succeed(store, task_id, %{
        status_code: 200,
        body: "PNGDATA",
        size_bytes: 7,
        truncated: false,
        headers: %{"content-type" => "image/png", "x-custom" => "yes"}
      })

    assert {:ok, %{status_code: 200, body: "PNGDATA", headers: headers}} =
             TaskStore.get_result(store, task_id)

    assert headers["content-type"] == "image/png"
    assert headers["x-custom"] == "yes"
  end

  test "a succeeded result with no headers key reads back with headers defaulted to %{}", %{
    path: path
  } do
    {_op_log, store} = start_pair(path)

    {:ok, :created, task_id} =
      TaskStore.submit(store, %{tenant: "t1", principal: "p1", workload: "wl-a"})

    {:ok, _} = TaskStore.assign(store, task_id)
    {:ok, _} = TaskStore.start(store, task_id)

    # A result map shaped like a PRE-CHANGE record: no :headers key at all. The store
    # must default it, the projection must store NULL, and the read-back must not
    # crash and must surface %{} (durability / backward compatibility).
    {:ok, _} =
      TaskStore.succeed(store, task_id, %{
        status_code: 204,
        body: "",
        size_bytes: 0,
        truncated: false
      })

    assert {:ok, %{status_code: 204, headers: %{}}} = TaskStore.get_result(store, task_id)
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

  test "cfg_for reads Embervm.WorkloadCatalog: a workload-specific retry config drives classify/backoff", %{
    path: path
  } do
    # This proves the cfg_for -> WorkloadCatalog wiring end to end, not just
    # that WorkloadCatalog.retry_config/1 works in isolation (that is already
    # covered by workload_catalog_test.exs). It writes into the DEFAULT
    # :embervm_workloads table (the same one Embervm.WorkloadWatcher owns in
    # the running application) rather than a private one, because cfg_for/1
    # always reads the default table; a unique workload NAME within that
    # shared table, plus on_exit cleanup, is what keeps this test isolated
    # from the application's own watcher and from other async tests.
    default_table = WorkloadCatalog.table()

    if :ets.whereis(default_table) == :undefined do
      WorkloadCatalog.create(default_table)
    end

    workload = "wl-cfg-for-probe-#{System.unique_integer([:positive])}"

    # max_attempts: 1 means the very first failure on attempt 1 already
    # exhausts the budget, so a :transport failure (normally retryable under
    # the default config) must go straight to permanent/dead-lettered here
    # instead of retryable. That divergence from the default is only
    # observable if cfg_for/1 actually reads this catalog entry.
    custom_retry = %{max_attempts: 1, backoff_ms: 1, backoff_cap_ms: 1, retry_on: [:transport]}
    WorkloadCatalog.upsert(default_table, workload, %{name: workload, retry: custom_retry})
    on_exit(fn -> WorkloadCatalog.drop(default_table, workload) end)

    {_op_log, store} = start_pair(path)

    {:ok, :created, task_id} =
      TaskStore.submit(store, %{tenant: "t1", principal: "p1", workload: workload})

    {:ok, _} = TaskStore.assign(store, task_id)

    {:ok, task} = TaskStore.fail(store, task_id, :transport)
    assert task.state == :dead_lettered

    {:ok, ets_task} = TaskStore.get(store, task_id)
    assert ets_task.state == :dead_lettered
    assert ets_task.attempt == 1
  end

  # -- read-time result TTL + dedupe expiry (ADR embervm/002) ----------------

  # A clock we can move: get_result/dedupe read state.clock.() at call time, so a
  # controllable clock lets one test observe "before expiry -> served, after ->
  # 404" deterministically without sleeping.
  defp movable_clock(initial) do
    {:ok, agent} = Agent.start_link(fn -> initial end)
    fun = fn -> Agent.get(agent, & &1) end
    set = fn v -> Agent.update(agent, fn _ -> v end) end
    {fun, set}
  end

  test "get_result 404s (returns nil) once the result is past its injected expiry", %{path: path} do
    {clock, set_clock} = movable_clock(1_000)
    {_op_log, store} = start_pair(path, clock: clock)

    {:ok, :created, task_id} =
      TaskStore.submit(store, %{tenant: "t1", principal: "p1", workload: "wl-a"})

    {:ok, _} = TaskStore.assign(store, task_id)
    {:ok, _} = TaskStore.start(store, task_id)

    # Result expires at 5_000.
    {:ok, _} =
      TaskStore.succeed(store, task_id, %{
        status_code: 200,
        body: "DATA",
        size_bytes: 4,
        truncated: false,
        expires_at: 5_000
      })

    # Clock 4_999 < expiry: served.
    set_clock.(4_999)
    assert {:ok, %{status_code: 200, body: "DATA"}} = TaskStore.get_result(store, task_id)

    # Clock 5_001 > expiry: 404, even though no sweep ran.
    set_clock.(5_001)
    assert {:ok, nil} = TaskStore.get_result(store, task_id)
  end

  test "dedupe returns :existing for an in-flight (non-terminal) duplicate", %{path: path} do
    {_op_log, store} = start_pair(path)

    attrs = %{tenant: "t1", principal: "p1", workload: "wl-a", idempotency_key: "inflight-1"}

    {:ok, :created, id1} = TaskStore.submit(store, attrs)
    # Move it in-flight (assigned): still non-terminal, so dedupe must suppress.
    {:ok, _} = TaskStore.assign(store, id1)

    {:ok, :existing, id2} = TaskStore.submit(store, attrs)
    assert id1 == id2
  end

  test "dedupe resubmits fresh for a terminal task whose result has expired", %{path: path} do
    {clock, set_clock} = movable_clock(1_000)
    {op_log, store} = start_pair(path, clock: clock)

    attrs = %{tenant: "t1", principal: "p1", workload: "wl-a", idempotency_key: "exp-1"}

    {:ok, :created, id1} = TaskStore.submit(store, attrs)
    {:ok, _} = TaskStore.assign(store, id1)
    {:ok, _} = TaskStore.start(store, id1)

    {:ok, _} =
      TaskStore.succeed(store, id1, %{
        status_code: 200,
        body: "OLD",
        size_bytes: 3,
        truncated: false,
        expires_at: 5_000
      })

    # Past the result TTL: dedupe treats the task as absent and runs a fresh submit.
    set_clock.(6_000)
    {:ok, :created, id2} = TaskStore.submit(store, attrs)
    refute id2 == id1

    # The old projection row is gone (evicted), so the fresh :submitted did not
    # collide on the unique (workload, key) index, and its result is cleared.
    assert {:ok, nil} = TaskStore.get_result(store, id1)
    {:ok, task2} = TaskStore.get(store, id2)
    assert task2.state == :queued

    # Two :submitted ops now exist in the immutable journal (old + fresh), proving
    # eviction pruned only the projection, not the ops.
    {:ok, ops} = SQLite.read_from(op_log, 0)
    assert Enum.count(ops, &(&1.kind == :submitted)) == 2
  end

  test "dedupe still returns :existing for a terminal task whose result is still live", %{path: path} do
    {clock, _set} = movable_clock(1_000)
    {_op_log, store} = start_pair(path, clock: clock)

    attrs = %{tenant: "t1", principal: "p1", workload: "wl-a", idempotency_key: "live-1"}

    {:ok, :created, id1} = TaskStore.submit(store, attrs)
    {:ok, _} = TaskStore.assign(store, id1)
    {:ok, _} = TaskStore.start(store, id1)

    # Result expires far in the future relative to the (fixed 1_000) clock.
    {:ok, _} =
      TaskStore.succeed(store, id1, %{
        status_code: 200,
        body: "LIVE",
        size_bytes: 4,
        truncated: false,
        expires_at: 1_000_000
      })

    {:ok, :existing, id2} = TaskStore.submit(store, attrs)
    assert id1 == id2
  end

  test "expire transitions a queued task to failed_permanent with reason expired", %{path: path} do
    {op_log, store} = start_pair(path)

    {:ok, :created, task_id} =
      TaskStore.submit(store, %{tenant: "t1", principal: "p1", workload: "wl-a"})

    {:ok, expired} = TaskStore.expire(store, task_id)
    assert expired.state == :failed_permanent

    # Not chained into dead-letter: terminal at failed_permanent.
    {:ok, ets_task} = TaskStore.get(store, task_id)
    assert ets_task.state == :failed_permanent

    # Recorded via the existing :failed op kind with reason expired, no new op kind.
    {:ok, ops} = SQLite.read_from(op_log, 0)
    failed = Enum.find(ops, &(&1.kind == :failed and &1.task_id == task_id))
    assert failed.payload["reason"] == "expired"

    # Expiring a non-queued task is an illegal transition.
    assert {:error, {:illegal_transition, :failed_permanent, :expire}} =
             TaskStore.expire(store, task_id)
  end

  # -- Task 11 dispatcher seams ----------------------------------------------

  test "get_request returns the guest-request envelope stored in the submitted op", %{path: path} do
    {_op_log, store} = start_pair(path)

    request = %{path: "/run", headers: %{"x-a" => "1"}, body_b64: Base.encode64("hi"), content_type: "application/json"}

    {:ok, :created, task_id} =
      TaskStore.submit(store, %{tenant: "t1", principal: "p1", workload: "wl-a", request: request})

    assert {:ok, env} = TaskStore.get_request(store, task_id)
    # JSON round-trips atom keys to strings.
    assert env["path"] == "/run"
    assert env["headers"] == %{"x-a" => "1"}
    assert env["body_b64"] == Base.encode64("hi")
    assert env["content_type"] == "application/json"

    assert {:ok, nil} = TaskStore.get_request(store, "no-such-task")
  end

  test "list_backlog returns queued and failed_retryable tasks only", %{path: path} do
    # max_attempts 3 so the first transport failure is retryable (not dead-lettered).
    {_op_log, store} = start_pair(path)

    {:ok, :created, queued_id} =
      TaskStore.submit(store, %{tenant: "t1", principal: "p1", workload: "wl-a"})

    {:ok, :created, retry_id} =
      TaskStore.submit(store, %{tenant: "t1", principal: "p2", workload: "wl-a"})

    {:ok, :created, done_id} =
      TaskStore.submit(store, %{tenant: "t1", principal: "p3", workload: "wl-a"})

    # retry_id -> failed_retryable; done_id -> succeeded (excluded).
    {:ok, _} = TaskStore.assign(store, retry_id)
    {:ok, %{state: :failed_retryable}, _backoff} = TaskStore.fail(store, retry_id, :transport)

    {:ok, _} = TaskStore.assign(store, done_id)
    {:ok, _} = TaskStore.start(store, done_id)
    {:ok, _} = TaskStore.succeed(store, done_id, %{status_code: 200, body: "", size_bytes: 0})

    {:ok, backlog} = TaskStore.list_backlog(store)
    ids = backlog |> Enum.map(& &1.task_id) |> Enum.sort()
    assert ids == Enum.sort([queued_id, retry_id])
    assert Enum.find(backlog, &(&1.task_id == retry_id)).state == :failed_retryable
    assert Enum.find(backlog, &(&1.task_id == queued_id)).state == :queued
  end

  test "on_queued hook fires on submit-created, retry, and redrive", %{path: path} do
    test_pid = self()
    hook = fn %{task_id: tid, workload: wl, principal: pr} -> send(test_pid, {:queued, tid, wl, pr}) end

    # wl_def is left uncataloged (cfg_for falls back to default_config, where
    # transport is retryable at 3 attempts); wl_dlq is cataloged with
    # max_attempts 1 so one failure dead-letters, reaching the redrive path.
    # Unique names keep this async-safe against the shared global catalog table.
    suffix = System.unique_integer([:positive])
    wl_def = "wl-def-#{suffix}"
    wl_dlq = "wl-dlq-#{suffix}"
    default_table = WorkloadCatalog.table()
    WorkloadCatalog.upsert(default_table, wl_dlq, %{name: wl_dlq, retry: %{max_attempts: 1, backoff_ms: 1, backoff_cap_ms: 1, retry_on: [:transport]}})
    on_exit(fn -> WorkloadCatalog.drop(default_table, wl_dlq) end)

    {_op_log, store} = start_pair(path, on_queued: hook)

    # 1. submit-created fires the hook.
    {:ok, :created, retry_id} =
      TaskStore.submit(store, %{tenant: "t1", principal: "p1", workload: wl_def})

    assert_receive {:queued, ^retry_id, ^wl_def, "p1"}

    # 2. retry (failed_retryable -> queued) fires the hook.
    {:ok, _} = TaskStore.assign(store, retry_id)
    {:ok, %{state: :failed_retryable}, _backoff} = TaskStore.fail(store, retry_id, :transport)
    {:ok, %{state: :queued}} = TaskStore.retry(store, retry_id)
    assert_receive {:queued, ^retry_id, ^wl_def, "p1"}

    # 3. redrive (dead_lettered -> queued) fires the hook.
    {:ok, :created, dlq_id} =
      TaskStore.submit(store, %{tenant: "t1", principal: "p2", workload: wl_dlq})

    assert_receive {:queued, ^dlq_id, ^wl_dlq, "p2"}
    {:ok, _} = TaskStore.assign(store, dlq_id)
    {:ok, %{state: :dead_lettered}} = TaskStore.fail(store, dlq_id, :transport)
    {:ok, %{state: :queued}} = TaskStore.redrive(store, dlq_id)
    assert_receive {:queued, ^dlq_id, ^wl_dlq, "p2"}
  end

  # -- async lifecycle writes (ADR embervm/014 decision 2) -------------------

  # Count ops of a kind for a task in the durable log (counter-based, not
  # identity/seq-based, per the repo's hash-fragile test lesson).
  defp durable_kind_count(op_log, task_id, kind) do
    {:ok, ops} = SQLite.read_from(op_log, 0)
    ops |> Enum.filter(&(&1.task_id == task_id and &1.kind == kind)) |> length()
  end

  test "gate ON: assign/start advance ETS immediately but defer the durable append", %{path: path} do
    {:ok, op_log} = SQLite.start_link(path: path, name: nil)
    {:ok, writer} = Embervm.AsyncWriter.start_link(name: nil)

    {:ok, store} =
      TaskStore.start_link(
        op_log: op_log,
        name: nil,
        id_fun: sequential_id_fun(),
        clock: sequential_clock(),
        on_queued: fn _ -> :ok end,
        async_writer: writer,
        async_lifecycle_writes: true
      )

    {:ok, :created, task_id} =
      TaskStore.submit(store, %{tenant: "t1", principal: "p1", workload: "wl-a"})

    # assign/start return the advanced ETS state synchronously...
    {:ok, %{state: :assigned}} = TaskStore.assign(store, task_id, "vm-1")
    {:ok, %{state: :running}} = TaskStore.start(store, task_id, "vm-1")
    assert {:ok, %{state: :running}} = TaskStore.get(store, task_id)

    # ...while the durable :assigned/:started appends are deferred: drain the
    # writer, THEN they are present exactly once each.
    :ok = Embervm.AsyncWriter.drain(writer)
    assert durable_kind_count(op_log, task_id, :assigned) == 1
    assert durable_kind_count(op_log, task_id, :started) == 1
  end

  test "gate OFF: assign/start are write-through (durable append lands synchronously)", %{path: path} do
    {:ok, op_log} = SQLite.start_link(path: path, name: nil)
    {:ok, writer} = Embervm.AsyncWriter.start_link(name: nil)

    {:ok, store} =
      TaskStore.start_link(
        op_log: op_log,
        name: nil,
        id_fun: sequential_id_fun(),
        clock: sequential_clock(),
        on_queued: fn _ -> :ok end,
        async_writer: writer,
        async_lifecycle_writes: false
      )

    {:ok, :created, task_id} =
      TaskStore.submit(store, %{tenant: "t1", principal: "p1", workload: "wl-a"})

    {:ok, _} = TaskStore.assign(store, task_id)
    {:ok, _} = TaskStore.start(store, task_id)

    # No drain: the appends are already durable (write-through), and the writer was
    # never used (gate genuinely bypasses AsyncWriter).
    assert durable_kind_count(op_log, task_id, :assigned) == 1
    assert durable_kind_count(op_log, task_id, :started) == 1
    refute Embervm.AsyncWriter.pending?(writer, "vm-1")
  end

  test "gate ON metering fail-closed: the terminal :succeeded op is synchronous, never deferred", %{
    path: path
  } do
    {:ok, op_log} = SQLite.start_link(path: path, name: nil)
    {:ok, writer} = Embervm.AsyncWriter.start_link(name: nil)

    # Record every metering charge and the durable-op count AT charge time: the
    # guarantee is the charge never precedes the terminal :succeeded append.
    test_pid = self()

    {:ok, store} =
      TaskStore.start_link(
        op_log: op_log,
        name: nil,
        id_fun: sequential_id_fun(),
        clock: sequential_clock(),
        on_queued: fn _ -> :ok end,
        on_metered: fn event ->
          {:ok, ops} = SQLite.read_from(op_log, 0)
          succeeded = Enum.count(ops, &(&1.kind == :succeeded))
          send(test_pid, {:charged, event.principal, succeeded})
        end,
        async_writer: writer,
        async_lifecycle_writes: true
      )

    {:ok, :created, task_id} =
      TaskStore.submit(store, %{tenant: "t1", principal: "p1", workload: "wl-a"})

    {:ok, _} = TaskStore.assign(store, task_id, "vm-1")
    {:ok, _} = TaskStore.start(store, task_id, "vm-1")

    {:ok, _} =
      TaskStore.succeed(
        store,
        task_id,
        %{status_code: 200, body: "ok", size_bytes: 2, truncated: false},
        %{cpu_ms: 100, peak_rss_mib: 64, wall_ms: 100}
      )

    # The metering charge fired only AFTER the terminal :succeeded op was durable
    # (>= 1 succeeded op visible at charge time): fail-closed preserved even under
    # the async gate.
    assert_receive {:charged, "p1", succeeded_at_charge}
    assert succeeded_at_charge >= 1
  end
end
