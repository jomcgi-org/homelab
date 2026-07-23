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
  alias Exqlite.Sqlite3

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

  test "a succeeded op's guest headers project into results and load back", %{path: path} do
    server = start_server(path)

    {:ok, _} =
      SQLite.append(server, %Op{
        kind: :submitted,
        tenant: "t1",
        principal: "p1",
        workload: "wl-h",
        task_id: "task-h",
        ts: 100,
        payload: %{expires_at: nil}
      })

    {:ok, _} =
      SQLite.append(server, %Op{
        kind: :succeeded,
        tenant: "t1",
        task_id: "task-h",
        ts: 103,
        payload: %{
          status_code: 200,
          body: "PNG",
          size_bytes: 3,
          truncated: false,
          expires_at: nil,
          headers: %{"content-type" => "image/png"}
        }
      })

    assert {:ok, %{status_code: 200, headers: %{"content-type" => "image/png"}}} =
             SQLite.load_result(server, "task-h")

    :ok = GenServer.stop(server)
  end

  test "a succeeded op with no headers key loads back with headers %{} (backward compat)", %{
    path: path
  } do
    server = start_server(path)

    {:ok, _} =
      SQLite.append(server, %Op{
        kind: :submitted,
        tenant: "t1",
        principal: "p1",
        workload: "wl-h",
        task_id: "task-old",
        ts: 100,
        payload: %{expires_at: nil}
      })

    # A :succeeded op shaped like a pre-change record: no :headers key. It stores a
    # NULL headers column, which must read back as %{} without crashing.
    {:ok, _} =
      SQLite.append(server, %Op{
        kind: :succeeded,
        tenant: "t1",
        task_id: "task-old",
        ts: 103,
        payload: %{status_code: 200, body: "ok", size_bytes: 2, truncated: false, expires_at: nil}
      })

    assert {:ok, %{status_code: 200, headers: %{}}} = SQLite.load_result(server, "task-old")

    :ok = GenServer.stop(server)
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

  # -- retention + prefix compaction (ADR embervm/002) -----------------------

  test "evict_task deletes the task projection row and cascades its result", %{path: path} do
    server = start_server(path)

    {:ok, _} =
      SQLite.append(server, %Op{
        kind: :submitted,
        tenant: "t1",
        principal: "p1",
        workload: "wl-ev",
        task_id: "task-ev",
        ts: 100,
        payload: %{idempotency_key: "ev-key"}
      })

    {:ok, _} =
      SQLite.append(server, %Op{
        kind: :succeeded,
        tenant: "t1",
        task_id: "task-ev",
        ts: 101,
        payload: %{status_code: 200, body: "r", size_bytes: 1, truncated: false, expires_at: 9_999}
      })

    # Present before eviction.
    assert {:ok, %{status_code: 200}} = SQLite.load_result(server, "task-ev")
    {:ok, tasks} = SQLite.load_tasks(server)
    assert Enum.any?(tasks, &(&1.task_id == "task-ev"))

    assert :ok = SQLite.evict_task(server, "task-ev")

    # Task row gone AND its result cascaded (FK ON DELETE CASCADE).
    {:ok, tasks2} = SQLite.load_tasks(server)
    refute Enum.any?(tasks2, &(&1.task_id == "task-ev"))
    assert {:ok, nil} = SQLite.load_result(server, "task-ev")

    # The immutable submitted op is untouched (only the projection was pruned).
    {:ok, ops} = SQLite.read_from(server, 0)
    assert Enum.any?(ops, &(&1.kind == :submitted and &1.task_id == "task-ev"))

    :ok = GenServer.stop(server)
  end

  test "compacted_through starts at 0 and advances after horizon compaction", %{path: path} do
    # journal_horizon_ms 0: every op is immediately past the horizon, so a fully
    # terminal log compacts its whole prefix.
    server = start_server(path, journal_horizon_ms: 0)

    assert {:ok, 0} = SQLite.compacted_through(server)

    # A complete terminal task (submitted -> succeeded), all ops old (ts 100/101).
    {:ok, _} =
      SQLite.append(server, %Op{
        kind: :submitted,
        tenant: "t1",
        principal: "p1",
        workload: "wl-c",
        task_id: "task-c",
        ts: 100,
        payload: %{}
      })

    {:ok, last_seq} =
      SQLite.append(server, %Op{
        kind: :succeeded,
        tenant: "t1",
        task_id: "task-c",
        ts: 101,
        payload: %{status_code: 200, body: "", size_bytes: 0, truncated: false, expires_at: nil}
      })

    # now well past ts, horizon 0: the whole prefix is eligible; task is terminal.
    {:ok, res} = SQLite.compact(server, 10_000)
    assert res.compacted_through == last_seq
    assert res.ops_compacted == 2
    assert {:ok, ^last_seq} = SQLite.compacted_through(server)

    :ok = GenServer.stop(server)
  end

  test "marker never advances past a live task's ops nor past the horizon", %{path: path} do
    server = start_server(path, journal_horizon_ms: 1_000)

    # Task L: submitted then assigned, still LIVE (non-terminal). Old ts.
    {:ok, live_submit_seq} =
      SQLite.append(server, %Op{
        kind: :submitted,
        tenant: "t1",
        principal: "p1",
        workload: "wl-l",
        task_id: "task-l",
        ts: 100,
        payload: %{}
      })

    {:ok, _} =
      SQLite.append(server, %Op{kind: :assigned, tenant: "t1", task_id: "task-l", ts: 101, payload: %{}})

    # Task D: fully terminal, also old.
    {:ok, _} =
      SQLite.append(server, %Op{
        kind: :submitted,
        tenant: "t1",
        principal: "p1",
        workload: "wl-d",
        task_id: "task-d",
        ts: 102,
        payload: %{}
      })

    {:ok, _} =
      SQLite.append(server, %Op{
        kind: :succeeded,
        tenant: "t1",
        task_id: "task-d",
        ts: 103,
        payload: %{status_code: 200, body: "", size_bytes: 0, truncated: false, expires_at: nil}
      })

    # A RECENT op (ts 10_000), within the horizon at now=10_500 (cutoff 9_500).
    {:ok, _} =
      SQLite.append(server, %Op{kind: :denied, tenant: "t1", ts: 10_000, payload: %{}})

    # cutoff = 10_500 - 1_000 = 9_500. The live task's submitted op (seq
    # live_submit_seq) is the smallest blocked seq (owned by a non-terminal task),
    # so the marker can only reach live_submit_seq - 1.
    {:ok, res} = SQLite.compact(server, 10_500)
    assert res.compacted_through == live_submit_seq - 1

    # The live task's ops and the recent op are all still readable.
    {:ok, ops} = SQLite.read_from(server, live_submit_seq - 1)
    kinds = Enum.map(ops, & &1.kind)
    assert :assigned in kinds
    assert :denied in kinds

    :ok = GenServer.stop(server)
  end

  test "read_from below the marker errors as {:compacted, marker}, at/above returns ops", %{path: path} do
    server = start_server(path, journal_horizon_ms: 0)

    {:ok, _} =
      SQLite.append(server, %Op{
        kind: :submitted,
        tenant: "t1",
        principal: "p1",
        workload: "wl-r",
        task_id: "task-r",
        ts: 100,
        payload: %{}
      })

    {:ok, last_seq} =
      SQLite.append(server, %Op{
        kind: :succeeded,
        tenant: "t1",
        task_id: "task-r",
        ts: 101,
        payload: %{status_code: 200, body: "", size_bytes: 0, truncated: false, expires_at: nil}
      })

    {:ok, res} = SQLite.compact(server, 10_000)
    marker = res.compacted_through
    assert marker == last_seq

    # A seq below the marker is compacted-away history, distinguishable from empty.
    assert {:error, {:compacted, ^marker}} = SQLite.read_from(server, marker - 1)

    # At the marker: intact (empty here, since everything <= marker is gone), NOT
    # an error.
    assert {:ok, []} = SQLite.read_from(server, marker)

    :ok = GenServer.stop(server)
  end

  test "compaction is bounded: a small batch_size finishes across successive calls", %{path: path} do
    # batch_size 2, horizon 0: with 5 fully-terminal single-op-ish tasks whose ops
    # all qualify, one compact/2 deletes at most 2 ops and reports done:false until
    # the prefix is drained.
    server = start_server(path, journal_horizon_ms: 0, compact_batch_size: 2)

    # Five standalone audit ops (no task ownership blocks compaction), all old.
    for i <- 1..5 do
      {:ok, _} = SQLite.append(server, %Op{kind: :denied, tenant: "t1", ts: 100 + i, payload: %{}})
    end

    # First batch: 2 ops, not done.
    {:ok, b1} = SQLite.compact(server, 10_000)
    assert b1.ops_compacted == 2
    assert b1.done == false
    assert b1.compacted_through == 5

    # Second batch: 2 more, still not done.
    {:ok, b2} = SQLite.compact(server, 10_000)
    assert b2.ops_compacted == 2
    assert b2.done == false

    # Third batch: last 1, now done (under the ceiling on every table).
    {:ok, b3} = SQLite.compact(server, 10_000)
    assert b3.ops_compacted == 1
    assert b3.done == true

    :ok = GenServer.stop(server)
  end

  test "latency guard: an append between two compact batches lands and is present", %{path: path} do
    # batch_size 1 forces multiple batches; because each compact/2 is a discrete
    # GenServer call, an append issued between them is processed and its op survives
    # (it is a fresh op, above the compacted prefix).
    server = start_server(path, journal_horizon_ms: 0, compact_batch_size: 1)

    for i <- 1..3 do
      {:ok, _} = SQLite.append(server, %Op{kind: :denied, tenant: "t1", ts: 100 + i, payload: %{}})
    end

    {:ok, b1} = SQLite.compact(server, 10_000)
    assert b1.done == false

    # Interleaved append (a live op, ts newer): completes and is durable.
    {:ok, mid_seq} =
      SQLite.append(server, %Op{kind: :drain, tenant: "t1", ts: 20_000, payload: %{}})

    {:ok, _b2} = SQLite.compact(server, 10_000)

    # The interleaved op is still present (it is above the compacted prefix).
    # Read from the marker, not seq 0: compaction advanced the prefix past the
    # seed ops, so read_from(0) correctly errors {:compacted, marker}; the live
    # interleaved op lives above the marker and read_from(marker) returns it.
    {:ok, marker} = SQLite.compacted_through(server)
    {:ok, ops} = SQLite.read_from(server, marker)
    assert Enum.any?(ops, &(&1.seq == mid_seq and &1.kind == :drain))

    :ok = GenServer.stop(server)
  end

  # -- usage projection (Task 12) --------------------------------------------

  # ts on the same UTC day so both charges land in one (principal, day) row.
  @day5 5 * 86_400_000

  defp submit_task(server, task_id, principal, ts) do
    {:ok, _} =
      SQLite.append(server, %Op{
        kind: :submitted,
        tenant: "t1",
        principal: principal,
        workload: "wl",
        task_id: task_id,
        ts: ts,
        payload: %{}
      })
  end

  defp usage_payload(base, cpu_ms, peak_rss_mib, wall_ms) do
    stats = %{cpu_ms: cpu_ms, peak_rss_mib: peak_rss_mib, wall_ms: wall_ms}
    Map.put(base, :usage, Map.merge(stats, Embervm.Usage.billed(stats)))
  end

  test "succeeded/failed ops with usage accumulate into the usage projection", %{path: path} do
    server = start_server(path)

    submit_task(server, "u-a", "p1", @day5)
    submit_task(server, "u-b", "p1", @day5 + 1)
    submit_task(server, "u-c", "p2", @day5 + 2)

    # p1: two succeeded tasks. cpu 2000ms -> 2.0 vcpu-s; rss 1024MiB, wall 4000ms
    # -> (1024/1024)*(4000/1000) = 4.0 gb-s. Second: 1.0 vcpu-s, 1.0 gb-s.
    succeed = %{status_code: 200, body: "", size_bytes: 0, truncated: false, expires_at: nil}

    {:ok, _} =
      SQLite.append(server, %Op{
        kind: :succeeded,
        tenant: "t1",
        principal: "p1",
        workload: "wl",
        task_id: "u-a",
        ts: @day5,
        payload: usage_payload(succeed, 2000, 1024, 4000)
      })

    {:ok, _} =
      SQLite.append(server, %Op{
        kind: :succeeded,
        tenant: "t1",
        principal: "p1",
        workload: "wl",
        task_id: "u-b",
        ts: @day5 + 1,
        payload: usage_payload(succeed, 1000, 512, 2000)
      })

    # p2: a guest 4xx FAILURE that still did work is charged (1.5 vcpu-s).
    {:ok, _} =
      SQLite.append(server, %Op{
        kind: :failed,
        tenant: "t1",
        principal: "p2",
        workload: "wl",
        task_id: "u-c",
        ts: @day5 + 2,
        payload: usage_payload(%{state: :failed_permanent, reason: :guest4xx}, 1500, 256, 1000)
      })

    {:ok, page} = SQLite.list_usage(server, since_day: 0)

    by_principal = Map.new(page.items, &{&1.principal, &1})

    assert page.total == 2
    assert by_principal["p1"].day == 5
    assert by_principal["p1"].vcpu_seconds == 3.0
    assert by_principal["p1"].gb_seconds == 5.0
    assert by_principal["p1"].task_count == 2
    assert by_principal["p2"].vcpu_seconds == 1.5
    assert by_principal["p2"].task_count == 1

    :ok = GenServer.stop(server)
  end

  test "ops without usage do not touch the usage projection", %{path: path} do
    server = start_server(path)

    submit_task(server, "n-a", "p1", @day5)

    # A transport failure carries no usage: no :usage key, so no usage row.
    {:ok, _} =
      SQLite.append(server, %Op{
        kind: :failed,
        tenant: "t1",
        principal: "p1",
        workload: "wl",
        task_id: "n-a",
        ts: @day5,
        payload: %{state: :failed_retryable, reason: :transport}
      })

    {:ok, page} = SQLite.list_usage(server, since_day: 0)
    assert page.total == 0

    :ok = GenServer.stop(server)
  end

  test "list_usage filters by principal, floors by since_day, and pages", %{path: path} do
    server = start_server(path)

    succeed = %{status_code: 200, body: "", size_bytes: 0, truncated: false, expires_at: nil}

    # p1 on day 5, p2 on day 5, p3 on day 6.
    for {tid, pr, ts} <- [{"f-a", "p1", @day5}, {"f-b", "p2", @day5}, {"f-c", "p3", @day5 + 86_400_000}] do
      submit_task(server, tid, pr, ts)

      {:ok, _} =
        SQLite.append(server, %Op{
          kind: :succeeded,
          tenant: "t1",
          principal: pr,
          workload: "wl",
          task_id: tid,
          ts: ts,
          payload: usage_payload(succeed, 1000, 1024, 1000)
        })
    end

    # Principal filter.
    {:ok, only_p2} = SQLite.list_usage(server, principal: "p2")
    assert only_p2.total == 1
    assert [%{principal: "p2"}] = only_p2.items

    # since_day floor: day 6 only.
    {:ok, from6} = SQLite.list_usage(server, since_day: 6)
    assert from6.total == 1
    assert [%{principal: "p3", day: 6}] = from6.items

    # Paging: three rows total, one per page, stable (principal, day) order.
    {:ok, p0} = SQLite.list_usage(server, limit: 1, offset: 0)
    {:ok, p1} = SQLite.list_usage(server, limit: 1, offset: 1)
    assert p0.total == 3
    assert [%{principal: "p1"}] = p0.items
    assert [%{principal: "p2"}] = p1.items

    :ok = GenServer.stop(server)
  end

  # -- binary payloads (op payloads are ETF blobs, not JSON) -----------------

  test "a binary (non-UTF-8) result body round-trips instead of crashing the writer",
       %{path: path} do
    server = start_server(path)
    # First byte 0x89 (137) is exactly the byte that crashed the old JSON encoder
    # ({:invalid_byte, 137}) and cascaded a control-plane restart; the body also
    # carries a NUL and 0xFF so any UTF-8-assuming path would corrupt it.
    png = <<0x89, "PNG", 13, 10, 26, 10, 0, 1, 2, 0xFA, 0xFF>>

    {:ok, _} =
      SQLite.append(server, %Op{
        kind: :submitted,
        tenant: "t1",
        principal: "p1",
        workload: "og-image",
        task_id: "task-bin",
        ts: 100,
        payload: %{idempotency_key: "k", expires_at: nil}
      })

    {:ok, _} =
      SQLite.append(server, %Op{
        kind: :succeeded,
        tenant: "t1",
        task_id: "task-bin",
        ts: 101,
        payload: %{
          status_code: 200,
          body: png,
          size_bytes: byte_size(png),
          truncated: false,
          headers: %{"Content-Type" => "image/png"},
          expires_at: 9_999
        }
      })

    # The immutable ops journal keeps the exact bytes and the string-keyed shape.
    {:ok, ops} = SQLite.read_from(server, 0)
    succeeded = Enum.find(ops, &(&1.kind == :succeeded))
    assert succeeded.payload["body"] == png
    assert succeeded.payload["status_code"] == 200
    assert succeeded.payload["headers"] == %{"Content-Type" => "image/png"}

    # The projected result row also carries the binary body back verbatim.
    {:ok, result} = SQLite.load_result(server, "task-bin")
    assert result.body == png
    assert result.status_code == 200

    # Durable across a reopen (persisted on disk as an ETF blob).
    :ok = GenServer.stop(server)
    server2 = start_server(path)
    {:ok, ops2} = SQLite.read_from(server2, 0)
    assert Enum.find(ops2, &(&1.kind == :succeeded)).payload["body"] == png
    :ok = GenServer.stop(server2)
  end

  test "a binary request body round-trips through load_request", %{path: path} do
    server = start_server(path)
    raw = <<0x89, 0, 0xFF, "req-bytes", 200>>

    {:ok, _} =
      SQLite.append(server, %Op{
        kind: :submitted,
        tenant: "t1",
        principal: "p1",
        workload: "og-image",
        task_id: "task-req",
        ts: 100,
        payload: %{
          idempotency_key: "k",
          expires_at: nil,
          request: %{
            method: "POST",
            path: "/invoke",
            body: raw,
            headers: %{"Content-Type" => "application/octet-stream"}
          }
        }
      })

    {:ok, request} = SQLite.load_request(server, "task-req")
    # Keys come back as strings (the dispatcher's contract) and the binary body
    # survives intact through the nested map.
    assert request["method"] == "POST"
    assert request["body"] == raw
    assert request["headers"] == %{"Content-Type" => "application/octet-stream"}
    :ok = GenServer.stop(server)
  end

  test "a legacy JSON payload row still decodes to string keys (upgrade compatibility)",
       %{path: path} do
    # Seed the schema with a real (ETF) op, stop, then inject a row whose
    # payload_json is a JSON object the way a pre-upgrade build wrote it. On
    # reopen, decode_payload must read it via the JSON branch (first byte "{" =
    # 123, not the ETF version byte 131), so tasks in flight across the upgrade
    # are not lost.
    server = start_server(path)

    {:ok, _} =
      SQLite.append(server, %Op{kind: :denied, tenant: "t1", ts: 1, payload: %{reason: "seed"}})

    :ok = GenServer.stop(server)

    {:ok, conn} = Sqlite3.open(path)
    json = ~s({"status_code":200,"body":"legacy","size_bytes":6})

    {:ok, stmt} =
      Sqlite3.prepare(
        conn,
        "INSERT INTO ops (ts,tenant,principal,workload,task_id,kind,payload_json) VALUES (?,?,?,?,?,?,?)"
      )

    :ok = Sqlite3.bind(stmt, [2, "t1", "p1", "wl", "task-legacy", "succeeded", json])
    :done = Sqlite3.step(conn, stmt)
    :ok = Sqlite3.release(conn, stmt)
    :ok = Sqlite3.close(conn)

    server2 = start_server(path)
    {:ok, ops} = SQLite.read_from(server2, 0)
    legacy = Enum.find(ops, &(&1.task_id == "task-legacy"))
    assert legacy.payload == %{"status_code" => 200, "body" => "legacy", "size_bytes" => 6}
    :ok = GenServer.stop(server2)
  end

  # -- monotonic lifecycle-projection guard (ADR embervm/014 decision 2) -----

  defp append_task_op(server, kind, task_id, ts, payload \\ %{}) do
    {:ok, _} =
      SQLite.append(server, %Op{kind: kind, tenant: "t1", task_id: task_id, ts: ts, payload: payload})

    :ok
  end

  defp task_state(server, task_id) do
    {:ok, tasks} = SQLite.load_tasks(server)
    tasks |> Map.new(&{&1.task_id, &1}) |> Map.get(task_id) |> Map.get(:state)
  end

  test "a stale :assigned appended AFTER :succeeded does NOT regress the durable state, but IS still recorded",
       %{path: path} do
    server = start_server(path)

    append_task_op(server, :submitted, "task-x", 100, %{idempotency_key: nil, expires_at: nil})
    append_task_op(server, :assigned, "task-x", 101)
    append_task_op(server, :started, "task-x", 102)

    append_task_op(server, :succeeded, "task-x", 103, %{
      status_code: 200,
      body: "ok",
      size_bytes: 2,
      truncated: false,
      expires_at: 9_999
    })

    # A DEFERRED async :assigned lands late (out of order relative to :succeeded).
    append_task_op(server, :assigned, "task-x", 104)

    # Projection did NOT regress: the monotonic guard made the late append a no-op.
    assert task_state(server, "task-x") == "succeeded"

    # ...but the op is STILL in the durable log (audit trail): two :assigned ops, and
    # the state survives a full reopen (rebuild reads the projected table).
    {:ok, ops} = SQLite.read_from(server, 0)
    assert Enum.count(ops, &(&1.task_id == "task-x" and &1.kind == :assigned)) == 2

    :ok = GenServer.stop(server)
    server2 = start_server(path)
    assert task_state(server2, "task-x") == "succeeded"
    :ok = GenServer.stop(server2)
  end

  test "a :started whose own :assigned was lost still advances queued -> running (forward jump)", %{
    path: path
  } do
    server = start_server(path)

    append_task_op(server, :submitted, "task-y", 100, %{idempotency_key: nil, expires_at: nil})
    # :assigned append was LOST (never arrived); :started lands against a queued row.
    append_task_op(server, :started, "task-y", 102)

    # The partial-order guard advances queued -> running (queued ranks below running),
    # rather than stalling at queued the way an exact-prior guard would.
    assert task_state(server, "task-y") == "running"

    :ok = GenServer.stop(server)
  end

  test "replaying [submitted, assigned, started, succeeded, LATE assigned] in log order lands succeeded",
       %{path: path} do
    server = start_server(path)

    # This is exactly the rebuild-replay order the reviewer asked to pin: an out-of-
    # order async append at the tail must not undo the terminal projection.
    append_task_op(server, :submitted, "task-z", 100, %{idempotency_key: nil, expires_at: nil})
    append_task_op(server, :assigned, "task-z", 101)
    append_task_op(server, :started, "task-z", 102)

    append_task_op(server, :succeeded, "task-z", 103, %{
      status_code: 200,
      body: "ok",
      size_bytes: 2,
      truncated: false,
      expires_at: 9_999
    })

    append_task_op(server, :assigned, "task-z", 104)

    # Reopen forces a fresh rebuild from the projected table (the recovery path).
    :ok = GenServer.stop(server)
    server2 = start_server(path)
    assert task_state(server2, "task-z") == "succeeded"
    :ok = GenServer.stop(server2)
  end
end
