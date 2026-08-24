defmodule Embervm.OpLog.SessionProjectionTest do
  @moduledoc """
  Exercises the R2 session records, lineage projection, and retention discipline
  on the SQLite op-log backend directly (each test opens its own GenServer over a
  fresh temp file, so it can stop/restart the process to simulate crash recovery,
  the same idiom as sqlite_test.exs). Covers:

    * projection rebuild from a scripted session op sequence reproduces exact
      session states (create -> invoke -> bank -> relight, and each terminal kind);
    * the (principal, day) usage projection accumulates from session_invoked ops
      identically to task compute (D12.1);
    * retention never prunes a non-terminal session, prunes a terminal one past
      the 7-day window;
    * the ops-journal prefix marker never advances past a LIVE session's ops;
    * a kill/restart rebuilds session state exactly from the durable projection.
  """
  use ExUnit.Case, async: true

  alias Embervm.OpLog.Op
  alias Embervm.OpLog.SQLite

  setup do
    path =
      Path.join(
        System.tmp_dir!(),
        "embervm_oplog_session_test_#{System.unique_integer([:positive, :monotonic])}.db"
      )

    on_exit(fn -> File.rm_rf!(path) end)
    %{path: path}
  end

  defp start_server(path, extra_opts \\ []) do
    opts = Keyword.merge([path: path, name: nil], extra_opts)
    {:ok, pid} = SQLite.start_link(opts)
    pid
  end

  defp created_op(session_id, principal, ts, extra \\ %{}) do
    %Op{
      kind: :session_created,
      tenant: "t1",
      principal: principal,
      workload: "sandbox-session",
      session_id: session_id,
      ts: ts,
      payload:
        Map.merge(
          %{
            node_id: "node-4",
            base_snapshot_ref: "base:sha256:abc",
            base_digest: "sha256:abc",
            token_sha256: "hash-#{session_id}",
            expires_at: ts + 21_600_000
          },
          extra
        )
    }
  end

  defp session_by_id(server) do
    {:ok, sessions} = SQLite.load_sessions(server)
    Map.new(sessions, &{&1.session_id, &1})
  end

  test "a scripted create -> invoke -> bank -> relight sequence projects exact state", %{path: path} do
    server = start_server(path)

    {:ok, _} = SQLite.append(server, created_op("s-1", "p1", 100))

    {:ok, _} =
      SQLite.append(server, %Op{
        kind: :session_banked,
        tenant: "t1",
        principal: "p1",
        workload: "sandbox-session",
        session_id: "s-1",
        ts: 200,
        payload: %{
          snapshot_ref: "sessions/s-1",
          size_bytes: 2_147_483_648,
          generation: 1,
          parent_base_ref: "base:sha256:abc"
        }
      })

    {:ok, _} =
      SQLite.append(server, %Op{
        kind: :session_relit,
        tenant: "t1",
        principal: "p1",
        workload: "sandbox-session",
        session_id: "s-1",
        ts: 300,
        payload: %{snapshot_ref: "sessions/s-1", generation: 1, relight_ms: 420}
      })

    s = session_by_id(server)["s-1"]

    # Relit -> running; lineage recorded from create; generation set at bank.
    assert s.state == "running"
    assert s.base_snapshot_ref == "base:sha256:abc"
    assert s.base_digest == "sha256:abc"
    assert s.generation == 1
    assert s.snapshot_ref == "sessions/s-1"
    assert s.snapshot_size_bytes == 2_147_483_648
    assert s.token_sha256 == "hash-s-1"
    assert s.created_at == 100
    # lineage_id (#4306 slice 1): created_op/4's payload carries none (the
    # old-style shape), so the projection defaults it to session_id.
    assert s.lineage_id == "s-1"

    :ok = GenServer.stop(server)
  end

  test "session_rejoined projects a parked filesystem lineage session as running", %{path: path} do
    server = start_server(path)

    {:ok, _} = SQLite.append(server, created_op("s-rejoin", "p1", 100))

    {:ok, _} =
      SQLite.append(server, %Op{
        kind: :session_rejoined,
        tenant: "t1",
        principal: "p1",
        workload: "sandbox-session",
        session_id: "s-rejoin",
        ts: 200,
        payload: %{volume_node_id: "node-9"}
      })

    s = session_by_id(server)["s-rejoin"]
    assert s.state == "running"

    :ok = GenServer.stop(server)
  end

  test "a session_created op with an explicit lineage_id in its payload projects it verbatim, not defaulted", %{path: path} do
    server = start_server(path)

    {:ok, _} = SQLite.append(server, created_op("s-2", "p1", 100, %{lineage_id: "lin-custom"}))

    s = session_by_id(server)["s-2"]
    assert s.lineage_id == "lin-custom"
    refute s.lineage_id == s.session_id

    :ok = GenServer.stop(server)
  end

  test "each terminal kind projects its terminal state and reason", %{path: path} do
    server = start_server(path)

    for {sid, kind, expected_state} <- [
          {"s-exp", :session_expired, "expired"},
          {"s-evi", :session_evicted, "evicted"},
          {"s-des", :session_destroyed, "destroyed"},
          {"s-fai", :session_failed, "failed"}
        ] do
      {:ok, _} = SQLite.append(server, created_op(sid, "p1", 100))

      {:ok, _} =
        SQLite.append(server, %Op{
          kind: kind,
          tenant: "t1",
          principal: "p1",
          workload: "sandbox-session",
          session_id: sid,
          ts: 500,
          payload: %{reason: expected_state}
        })

      s = session_by_id(server)[sid]
      assert s.state == expected_state
      assert s.terminal_reason == expected_state
    end

    :ok = GenServer.stop(server)
  end

  test "session_invoked accumulates the (principal, day) usage projection like a task", %{path: path} do
    server = start_server(path)

    day5 = 5 * 86_400_000
    {:ok, _} = SQLite.append(server, created_op("s-u", "p1", day5))

    stats = %{cpu_ms: 2000, peak_rss_mib: 1024, wall_ms: 4000}
    usage = Map.merge(stats, Embervm.Usage.billed(stats))

    {:ok, _} =
      SQLite.append(server, %Op{
        kind: :session_invoked,
        tenant: "t1",
        principal: "p1",
        workload: "sandbox-session",
        session_id: "s-u",
        ts: day5 + 10,
        payload: %{status_code: 200, usage: usage}
      })

    {:ok, page} = SQLite.list_usage(server, since_day: 0)
    row = Enum.find(page.items, &(&1.principal == "p1"))

    # 2000ms -> 2.0 vcpu-s; (1024/1024)*(4000/1000) -> 4.0 gb-s.
    assert row.vcpu_seconds == 2.0
    assert row.gb_seconds == 4.0
    assert row.task_count == 1

    # last_invoke_at advanced; state unchanged by an invoke.
    s = session_by_id(server)["s-u"]
    assert s.last_invoke_at == day5 + 10
    assert s.state == "running"

    :ok = GenServer.stop(server)
  end

  test "retention never prunes a non-terminal session and prunes a terminal one past the window", %{path: path} do
    # retention_ms 7 days (default). now far past both sessions' updated_at.
    server = start_server(path)

    # Live session, old.
    {:ok, _} = SQLite.append(server, created_op("s-live", "p1", 100))

    # Terminal session, old.
    {:ok, _} = SQLite.append(server, created_op("s-term", "p1", 100))

    {:ok, _} =
      SQLite.append(server, %Op{
        kind: :session_destroyed,
        tenant: "t1",
        principal: "p1",
        workload: "sandbox-session",
        session_id: "s-term",
        ts: 200,
        payload: %{reason: "destroyed"}
      })

    # now = 100 + 8 days: both rows are older than the 7-day retention, but only
    # the terminal one is eligible.
    eight_days = 8 * 24 * 60 * 60 * 1000
    {:ok, res} = SQLite.compact(server, 100 + eight_days)
    assert res.sessions_compacted == 1

    ids = session_by_id(server) |> Map.keys() |> Enum.sort()
    assert ids == ["s-live"]

    :ok = GenServer.stop(server)
  end

  test "the ops-journal marker never advances past a live session's ops", %{path: path} do
    server = start_server(path, journal_horizon_ms: 0)

    # A LIVE (running) session: its create op must pin the prefix.
    {:ok, live_seq} = SQLite.append(server, created_op("s-live", "p1", 100))

    # A standalone old audit op AFTER it (so the whole log would otherwise be
    # compactable at horizon 0).
    {:ok, _} = SQLite.append(server, %Op{kind: :denied, tenant: "t1", ts: 101, payload: %{}})

    {:ok, res} = SQLite.compact(server, 10_000)

    # The marker can only reach live_seq - 1 (the live session's create op is the
    # smallest blocked seq).
    assert res.compacted_through == live_seq - 1

    # After the session terminates, the whole prefix becomes eligible.
    {:ok, _} =
      SQLite.append(server, %Op{
        kind: :session_destroyed,
        tenant: "t1",
        principal: "p1",
        workload: "sandbox-session",
        session_id: "s-live",
        ts: 102,
        payload: %{reason: "destroyed"}
      })

    {:ok, res2} = SQLite.compact(server, 10_000)
    {:ok, max_seq} = SQLite.compacted_through(server)
    assert res2.compacted_through == max_seq
    assert max_seq >= live_seq

    :ok = GenServer.stop(server)
  end

  test "kill/restart rebuilds live and terminal session state from the projection", %{path: path} do
    server = start_server(path)

    {:ok, _} = SQLite.append(server, created_op("s-a", "p1", 100))

    {:ok, _} =
      SQLite.append(server, %Op{
        kind: :session_banked,
        tenant: "t1",
        principal: "p1",
        workload: "sandbox-session",
        session_id: "s-a",
        ts: 200,
        payload: %{snapshot_ref: "sessions/s-a", size_bytes: 1024, generation: 1}
      })

    {:ok, _} = SQLite.append(server, created_op("s-b", "p1", 300))

    {:ok, _} =
      SQLite.append(server, %Op{
        kind: :session_failed,
        tenant: "t1",
        principal: "p1",
        workload: "sandbox-session",
        session_id: "s-b",
        ts: 400,
        payload: %{reason: "snapshot_lost"}
      })

    :ok = GenServer.stop(server)

    server2 = start_server(path)
    by_id = session_by_id(server2)

    assert by_id["s-a"].state == "banked"
    assert by_id["s-a"].generation == 1
    assert by_id["s-a"].snapshot_ref == "sessions/s-a"

    assert by_id["s-b"].state == "failed"
    assert by_id["s-b"].terminal_reason == "snapshot_lost"

    :ok = GenServer.stop(server2)
  end

  test "a fresh DB reports session_id on session ops through read_from", %{path: path} do
    server = start_server(path)

    {:ok, _} = SQLite.append(server, created_op("s-r", "p1", 100))
    {:ok, ops} = SQLite.read_from(server, 0)
    created = Enum.find(ops, &(&1.kind == :session_created))

    assert created.session_id == "s-r"
    assert created.task_id == nil

    :ok = GenServer.stop(server)
  end

  # -- idempotency-key binding (#4919) ----------------------------------------

  test "a duplicate (principal, idempotency_key) session_created rolls the WHOLE append back", %{path: path} do
    server = start_server(path)

    {:ok, _} = SQLite.append(server, created_op("s-k1", "p1", 100, %{idempotency_key: "dup"}))

    # Same key, same principal, different session: the append must fail with the
    # EXISTING session id, and the failed op must leave no trace (the ops row is
    # rolled back with the projection write, the pinned transactional semantics).
    assert {:error, {:duplicate_session_idempotency_key, "s-k1"}} =
             SQLite.append(server, created_op("s-k2", "p1", 200, %{idempotency_key: "dup"}))

    {:ok, ops} = SQLite.read_from(server, 0)
    refute Enum.any?(ops, &(&1.session_id == "s-k2"))
    refute Map.has_key?(session_by_id(server), "s-k2")

    :ok = GenServer.stop(server)
  end

  test "the same key under a DIFFERENT principal is a distinct binding", %{path: path} do
    server = start_server(path)

    {:ok, _} = SQLite.append(server, created_op("s-pa", "pA", 100, %{idempotency_key: "shared"}))
    {:ok, _} = SQLite.append(server, created_op("s-pb", "pB", 110, %{idempotency_key: "shared"}))

    ids = session_by_id(server) |> Map.keys() |> Enum.sort()
    assert ids == ["s-pa", "s-pb"]

    :ok = GenServer.stop(server)
  end

  test "a terminal session keeps its binding until retention compaction frees the key", %{path: path} do
    server = start_server(path, retention_ms: 500)

    {:ok, _} = SQLite.append(server, created_op("s-gone", "p1", 100, %{idempotency_key: "freed"}))

    {:ok, _} =
      SQLite.append(server, %Op{
        kind: :session_destroyed,
        tenant: "t1",
        principal: "p1",
        workload: "sandbox-session",
        session_id: "s-gone",
        ts: 200,
        payload: %{reason: "destroyed"}
      })

    # While the row survives (terminal or not), the binding still conflicts.
    assert {:error, {:duplicate_session_idempotency_key, "s-gone"}} =
             SQLite.append(server, created_op("s-new", "p1", 300, %{idempotency_key: "freed"}))

    # Retention prunes the terminal row past its window; only then does a fresh
    # create under the same key succeed (the documented bounded lifetime).
    {:ok, res} = SQLite.compact(server, 200 + 501)
    assert res.sessions_compacted == 1

    {:ok, _} = SQLite.append(server, created_op("s-fresh", "p1", 800, %{idempotency_key: "freed"}))
    assert Map.has_key?(session_by_id(server), "s-fresh")

    :ok = GenServer.stop(server)
  end

  test "the binding is enforced again after a kill/restart (rebuild keeps it)", %{path: path} do
    server = start_server(path)

    {:ok, _} = SQLite.append(server, created_op("s-durable", "p1", 100, %{idempotency_key: "durable"}))
    :ok = GenServer.stop(server)

    server2 = start_server(path)

    assert {:error, {:duplicate_session_idempotency_key, "s-durable"}} =
             SQLite.append(server2, created_op("s-other", "p1", 200, %{idempotency_key: "durable"}))

    :ok = GenServer.stop(server2)
  end

  test "load_sessions carries idempotency_key through the rebuild rows", %{path: path} do
    server = start_server(path)

    {:ok, _} = SQLite.append(server, created_op("s-carry", "p1", 100, %{idempotency_key: "carry-me"}))

    {:ok, [row]} = SQLite.load_sessions(server)
    assert row.session_id == "s-carry"
    assert row.idempotency_key == "carry-me"

    :ok = GenServer.stop(server)
  end
end
