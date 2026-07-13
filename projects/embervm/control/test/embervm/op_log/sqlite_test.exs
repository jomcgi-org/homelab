defmodule Embervm.OpLog.SQLiteTest do
  @moduledoc """
  Exercises the SQLite-WAL op-log backend directly (bypassing the supervised
  `Embervm.OpLog.SQLite` singleton the application starts): every test opens
  its own GenServer against a fresh temp file so tests stay independent and
  can freely stop/restart the process to simulate a crash and recovery.
  """
  use ExUnit.Case, async: true

  alias Embervm.OpLog.Op
  alias Embervm.OpLog.SQLite

  setup do
    path = Path.join(System.tmp_dir!(), "embervm_oplog_test_#{System.unique_integer([:positive, :monotonic])}.db")
    on_exit(fn -> File.rm_rf!(path) end)
    %{path: path}
  end

  # name: nil: tests run async and each needs its own unnamed, PID-addressed
  # process (naming would collide with concurrently-running tests and with
  # the application's own supervised singleton).
  defp start_server(path, extra_opts \\ []) do
    opts = Keyword.merge([path: path, name: nil], extra_opts)
    {:ok, pid} = SQLite.start_link(opts)
    pid
  end

  test "monotonic seq under concurrent interleaving, survives reopen", %{path: path} do
    server = start_server(path)

    n = 5
    k = 20

    tasks =
      for i <- 1..n do
        Task.async(fn ->
          for j <- 1..k do
            op = %Op{
              kind: :denied,
              tenant: "t1",
              ts: 1_000 + i * 1000 + j,
              payload: %{reason: "quota"}
            }

            {:ok, seq} = SQLite.append(server, op)
            seq
          end
        end)
      end

    seqs = tasks |> Enum.flat_map(&Task.await/1) |> Enum.sort()

    assert seqs == Enum.to_list(1..(n * k))

    :ok = GenServer.stop(server)

    # Reopen on the same file (a fresh process, same durable data) and confirm
    # the persisted seqs are strictly increasing with no gaps.
    server2 = start_server(path)
    {:ok, ops} = SQLite.read_from(server2, 0)
    read_seqs = Enum.map(ops, & &1.seq)

    assert read_seqs == Enum.to_list(1..(n * k))
    assert read_seqs == Enum.sort(read_seqs)

    :ok = GenServer.stop(server2)
  end

  test "kill/restart recovery replays task state and results from the projection", %{path: path} do
    server = start_server(path)

    # Task A: submitted -> assigned -> started -> succeeded.
    {:ok, _} =
      SQLite.append(server, %Op{
        kind: :submitted,
        tenant: "t1",
        principal: "p1",
        workload: "wl-a",
        task_id: "task-a",
        ts: 100,
        payload: %{idempotency_key: "key-a", expires_at: nil}
      })

    {:ok, _} =
      SQLite.append(server, %Op{
        kind: :assigned,
        tenant: "t1",
        task_id: "task-a",
        ts: 101,
        payload: %{}
      })

    {:ok, _} =
      SQLite.append(server, %Op{
        kind: :started,
        tenant: "t1",
        task_id: "task-a",
        ts: 102,
        payload: %{}
      })

    {:ok, _} =
      SQLite.append(server, %Op{
        kind: :succeeded,
        tenant: "t1",
        task_id: "task-a",
        ts: 103,
        payload: %{status_code: 200, body: "ok", size_bytes: 2, truncated: false, expires_at: 9_999}
      })

    # Task B: submitted -> assigned -> failed(failed_retryable) -> retried.
    {:ok, _} =
      SQLite.append(server, %Op{
        kind: :submitted,
        tenant: "t1",
        principal: "p1",
        workload: "wl-b",
        task_id: "task-b",
        ts: 200,
        payload: %{}
      })

    {:ok, _} =
      SQLite.append(server, %Op{
        kind: :assigned,
        tenant: "t1",
        task_id: "task-b",
        ts: 201,
        payload: %{}
      })

    {:ok, _} =
      SQLite.append(server, %Op{
        kind: :failed,
        tenant: "t1",
        task_id: "task-b",
        ts: 202,
        payload: %{state: :failed_retryable}
      })

    {:ok, _} =
      SQLite.append(server, %Op{
        kind: :retried,
        tenant: "t1",
        task_id: "task-b",
        ts: 203,
        payload: %{}
      })

    # Simulate a crash: stop without a clean flush beyond what the transaction
    # commits already guaranteed (every append/2 above already committed).
    :ok = GenServer.stop(server)

    server2 = start_server(path)
    {:ok, tasks} = SQLite.load_tasks(server2)
    by_id = Map.new(tasks, &{&1.task_id, &1})

    assert by_id["task-a"].state == "succeeded"
    assert by_id["task-a"].attempt == 0

    assert by_id["task-b"].state == "queued"
    assert by_id["task-b"].attempt == 1

    # The succeeded task's result row survived the restart.
    {:ok, ops} = SQLite.read_from(server2, 0)
    assert Enum.any?(ops, &(&1.kind == :succeeded and &1.task_id == "task-a"))

    :ok = GenServer.stop(server2)
  end

  test "result TTL sweep only deletes results past their injected expiry", %{path: path} do
    server = start_server(path)

    {:ok, _} =
      SQLite.append(server, %Op{
        kind: :submitted,
        tenant: "t1",
        principal: "p1",
        workload: "wl-ttl",
        task_id: "task-ttl",
        ts: 100,
        payload: %{}
      })

    {:ok, _} =
      SQLite.append(server, %Op{
        kind: :succeeded,
        tenant: "t1",
        task_id: "task-ttl",
        ts: 101,
        payload: %{status_code: 200, body: "ok", size_bytes: 2, truncated: false, expires_at: 1_000}
      })

    # now < expires_at: result survives.
    {:ok, %{results_deleted: 0}} = SQLite.compact(server, 500)

    # now > expires_at: result is swept.
    {:ok, %{results_deleted: 1}} = SQLite.compact(server, 1_001)

    :ok = GenServer.stop(server)
  end

  test "idempotency conflict on duplicate (workload, idempotency_key)", %{path: path} do
    server = start_server(path)

    {:ok, _} =
      SQLite.append(server, %Op{
        kind: :submitted,
        tenant: "t1",
        principal: "p1",
        workload: "wl-idem",
        task_id: "task-first",
        ts: 100,
        payload: %{idempotency_key: "dup-key"}
      })

    result =
      SQLite.append(server, %Op{
        kind: :submitted,
        tenant: "t1",
        principal: "p1",
        workload: "wl-idem",
        task_id: "task-second",
        ts: 200,
        payload: %{idempotency_key: "dup-key"}
      })

    assert {:error, {:duplicate_idempotency_key, "task-first"}} = result

    :ok = GenServer.stop(server)
  end

  test "unknown op kind is rejected before any write", %{path: path} do
    server = start_server(path)

    result =
      SQLite.append(server, %Op{
        kind: :bogus,
        tenant: "t1",
        ts: 100,
        payload: %{}
      })

    assert {:error, {:unknown_kind, :bogus}} = result

    :ok = GenServer.stop(server)
  end
end
