defmodule Embervm.OpLog.PostgresLiveTest do
  @moduledoc """
  Live-Postgres conformance tests for `Embervm.OpLog.Postgres`.

  The tests use one temporary schema per test. The schema is created before the
  adapter starts, and the adapter connection receives Postgrex's
  `parameters: [search_path: schema]` option, so its unqualified DDL and queries
  stay isolated. The schema is dropped with `CASCADE` during cleanup.

  Run with, for example:

      EMBERVM_OPLOG_TEST_DSN=postgres://user:pass@localhost:5432/embervm mix test test/embervm/op_log/postgres_live_test.exs

  `//bazel/erlang:mix_postgres_test` starts an ephemeral PostgreSQL instance and
  sets `EMBERVM_OPLOG_TEST_DSN` so Linux CI executes this module. The ordinary
  all-ExUnit lane leaves the variable unset and skips it.
  """

  use ExUnit.Case, async: true

  alias Embervm.OpLog.Op
  alias Embervm.OpLog.Postgres

  @moduletag :live_postgres
  @dsn System.get_env("EMBERVM_OPLOG_TEST_DSN")

  if is_nil(@dsn) or @dsn == "" do
    @moduletag skip: "EMBERVM_OPLOG_TEST_DSN is unset"
  end

  setup do
    suffix = System.unique_integer([:positive, :monotonic])
    schema = "test_#{suffix}"
    role = "embervm_cell_test_#{suffix}"
    opts = dsn_opts(@dsn)
    {:ok, setup_conn} = Postgrex.start_link(Keyword.put(opts, :name, nil))
    {:ok, _} = Postgrex.query(setup_conn, ~s(CREATE ROLE "#{role}" NOSUPERUSER NOBYPASSRLS LOGIN), [])
    {:ok, _} = Postgrex.query(setup_conn, ~s(CREATE SCHEMA "#{schema}" AUTHORIZATION "#{role}"), [])
    :ok = GenServer.stop(setup_conn)

    adapter_opts =
      opts
      |> Keyword.put(:name, nil)
      |> Keyword.put(:username, role)
      |> Keyword.put(:parameters, [search_path: schema])

    superuser_opts =
      opts
      |> Keyword.put(:name, nil)
      |> Keyword.put(:parameters, [search_path: schema])

    {:ok, server} = Postgres.start_link(dsn: adapter_opts, name: nil, journal_horizon_ms: 0)

    on_exit(fn ->
      Embervm.TestProcess.stop_safely(server)
      {:ok, cleanup_conn} = Postgrex.start_link(Keyword.put(opts, :name, nil))
      {:ok, _} = Postgrex.query(cleanup_conn, ~s(DROP SCHEMA "#{schema}" CASCADE), [])
      {:ok, _} = Postgrex.query(cleanup_conn, ~s(DROP ROLE "#{role}"), [])
      # Keep every on_exit stop on the race-safe #4078 path.
      Embervm.TestProcess.stop_safely(cleanup_conn)
    end)

    %{server: server, adapter_opts: adapter_opts, superuser_opts: superuser_opts}
  end

  test "two cells share ownership but isolate replay and projections", %{
    server: cell_0,
    adapter_opts: adapter_opts
  } do
    {:ok, cell_b} =
      Postgres.start_link(
        dsn: adapter_opts,
        name: nil,
        cell_id: "cell-b",
        journal_horizon_ms: 0
      )

    on_exit(fn -> Embervm.TestProcess.stop_safely(cell_b) end)

    cell_b_conn = :sys.get_state(cell_b).conn

    assert {:ok, %Postgrex.Result{rows: [["cell-b"]]}} =
             Postgrex.query(cell_b_conn, "SELECT current_setting('embervm.cell_id')", [])

    assert Postgres.claim_workload(cell_0, "owned", "cell-0") == {:ok, "cell-0"}
    assert Postgres.claim_workload(cell_b, "owned", "cell-b") == {:ok, "cell-0"}
    assert Postgres.claim_workload(cell_b, "cell-b-workload", "cell-b") == {:ok, "cell-b"}

    assert {:ok,
            [
              %{workload: "cell-b-workload", cell_id: "cell-b"},
              %{workload: "owned", cell_id: "cell-0"}
            ]} = Postgres.load_workload_cells(cell_0)

    assert {:ok, _} =
             append(cell_0, :submitted, 100,
               tenant: "t",
               principal: "p",
               workload: "owned",
               task_id: "task-cell-0"
             )

    assert {:ok, _} =
             append(cell_b, :submitted, 101,
               tenant: "t",
               principal: "p",
               workload: "cell-b-workload",
               task_id: "task-cell-b"
             )

    assert {:ok, [%{task_id: "task-cell-0"}]} = Postgres.load_tasks(cell_0)
    assert {:ok, [%{task_id: "task-cell-b"}]} = Postgres.load_tasks(cell_b)
    assert {:ok, [%Op{cell_id: "cell-0", task_id: "task-cell-0"}]} = Postgres.read_from(cell_0, 0)
    assert {:ok, [%Op{cell_id: "cell-b", task_id: "task-cell-b"}]} = Postgres.read_from(cell_b, 0)
  end

  test "a non-default cell refuses a role that bypasses row-level security", %{
    superuser_opts: superuser_opts
  } do
    Process.flag(:trap_exit, true)

    assert {:error, {:connect_failed, :cell_role_bypasses_rls}} =
             Postgres.start_link(
               dsn: superuser_opts,
               name: nil,
               cell_id: "cell-b",
               journal_horizon_ms: 0
             )
  end

  test "strict session stop intent and completion project with nullable invocation identity", %{server: server} do
    expected = %{"session_id" => "s-proof", "generation" => 0, "invoke_started_at" => nil,
      "vm_id" => "vm-original", "node_id" => "node-4", "instance_id" => "node-4/pod-1",
      "pod_uid" => "pod-1", "boot_id" => "boot-original"}
    intent = Embervm.SessionStopProof.new_intent(expected, 200)
    completion = Map.put(intent, "completed_at_unix_ms", 201)
    assert {:ok, _} = append(server, :session_created, 100, session_id: "s-proof",
      tenant: "t1", principal: "p1", workload: "wl", payload: %{node_id: "node-4", token_sha256: "hash"})
    assert {:ok, _} = append(server, :session_destroying, 200, session_id: "s-proof", payload: %{stop_intent: intent})
    assert {:ok, [%{stop_intent: ^intent, stop_completion: nil}]} = Postgres.load_sessions(server)
    assert {:ok, _} = append(server, :session_destroyed, 202, session_id: "s-proof", payload: %{stop_completion: completion})
    assert {:ok, [%{stop_intent: ^intent, stop_completion: ^completion}]} = Postgres.load_sessions(server)
  end

  test "append and read_from preserve monotonic ordering and read after a sequence", %{server: server} do
    seqs =
      for {kind, ts} <- [{:denied, 10}, {:drain, 20}, {:quota_enforced, 30}] do
        {:ok, seq} = append(server, kind, ts, payload: %{kind: kind})
        seq
      end

    assert seqs == Enum.sort(seqs)
    assert {:ok, ops} = Postgres.read_from(server, 0)
    assert Enum.map(ops, & &1.seq) == seqs
    assert {:ok, later} = Postgres.read_from(server, Enum.at(seqs, 0))
    assert Enum.map(later, & &1.kind) == [:drain, :quota_enforced]
  end

  test "task, session, serving, stateful, volume, checkpoint, and group projections agree", %{
    server: server
  } do
    append(server, :submitted, 100,
      tenant: "t1", principal: "p1", workload: "wl-task", task_id: "task-1",
      payload: %{idempotency_key: "key-1", expires_at: nil}
    )

    append(server, :assigned, 101, task_id: "task-1")
    append(server, :started, 102, task_id: "task-1")
    append(server, :succeeded, 103, task_id: "task-1",
      payload: %{status_code: 201, body: "ok", size_bytes: 2, truncated: false,
        expires_at: nil, headers: %{"content-type" => "text/plain"}}
    )

    append(server, :session_created, 110,
      tenant: "t1", principal: "p1", workload: "wl-session", session_id: "session-1",
      payload: %{state: "running", node_id: "node-1", base_snapshot_ref: "base", base_digest: "digest",
        token_sha256: "token", expires_at: 999}
    )
    append(server, :session_banked, 111, session_id: "session-1",
      payload: %{snapshot_ref: "snap", size_bytes: 42, generation: 3}
    )

    append(server, :serving_started, 120,
      tenant: "t1", principal: "p1", workload: "wl-serving", serving_instance_id: "serving-1",
      payload: %{node_id: "node-1", vm_id: "vm-1", ip: "10.0.0.2", port: 8080,
        base_snapshot_ref: "base", base_digest: "digest"}
    )
    append(server, :serving_published, 121, serving_instance_id: "serving-1",
      payload: %{ip: "10.0.0.3", port: 8081}
    )

    append(server, :volume_created, 130, tenant: "t1", workload: "wl-stateful",
      payload: %{node_id: "node-1", generation: 2, size_bytes: 100, allocated_bytes: 40}
    )
    append(server, :generation_blessed, 131, workload: "wl-stateful", payload: %{generation: 2})
    append(server, :checkpoint_dispatched, 132, workload: "wl-stateful",
      payload: %{vm_id: "vm-stateful", generation: 2}
    )
    append(server, :stateful_started, 133,
      tenant: "t1", principal: "p1", workload: "wl-stateful", stateful_instance_id: "stateful-1",
      payload: %{node_id: "node-1", vm_id: "vm-stateful", generation: 2}
    )
    append(server, :stateful_published, 134, stateful_instance_id: "stateful-1",
      payload: %{ip: "10.0.0.4", port: 9090}
    )

    append(server, :group_created, 140,
      tenant: "t1", principal: "p1", workload: "wl-group", group_instance_id: "group-1",
      payload: %{node_id: "node-1", entry_member: "api", entry_port: 80, listen_port: 8080}
    )
    append(server, :group_net_created, 141, group_instance_id: "group-1",
      payload: %{subnet_cidr: "10.20.0.0/29"}
    )
    append(server, :group_member_started, 142, group_instance_id: "group-1",
      payload: %{member_name: "api", member_index: 0, vm_id: "vm-api", ip: "10.20.0.2"}
    )
    append(server, :group_running, 143, group_instance_id: "group-1")

    assert {:ok, [%{task_id: "task-1", state: "succeeded", attempt: 0}]} = Postgres.load_tasks(server)
    # lineage_id (#4306 slice 1): the append above carries none, so the
    # projection must default it to session_id, exactly like an old-style op.
    assert {:ok, [%{session_id: "session-1", state: "banked", generation: 3, snapshot_ref: "snap", lineage_id: "session-1"}]} =
             Postgres.load_sessions(server)
    assert {:ok, [%{instance_id: "serving-1", state: "published", ip: "10.0.0.3", port: 8081}]} = Postgres.load_serving_instances(server)
    assert {:ok, [%{instance_id: "stateful-1", state: "serving", ip: "10.0.0.4", port: 9090}]} = Postgres.load_stateful_instances(server)
    assert {:ok, [%{workload: "wl-stateful", generation: 2, size_bytes: 100, allocated_bytes: 40}]} = Postgres.load_volumes(server)
    assert {:ok, [%{workload: "wl-stateful", blessed_generation: 2}]} = Postgres.load_volume_blessing(server)
    assert {:ok, [%{workload: "wl-stateful", vm_id: "vm-stateful", generation: 2}]} = Postgres.load_checkpoint_dispatches(server)
    assert {:ok, [%{instance_id: "group-1", state: "running", subnet_cidr: "10.20.0.0/29"}]} = Postgres.load_group_instances(server)
    assert {:ok, [%{instance_id: "group-1", member_name: "api", vm_id: "vm-api", healthy: true}]} = Postgres.load_group_members(server)
    assert {:ok, %{status_code: 201, body: "ok", headers: %{"content-type" => "text/plain"}}} = Postgres.load_result(server, "task-1")
    assert {:ok, nil} = Postgres.load_result(server, "missing")
    assert :ok = Postgres.evict_task(server, "task-1")
    assert {:ok, nil} = Postgres.load_result(server, "task-1")
  end

  test "load_request returns the submitted request and nil for an unknown task", %{server: server} do
    request = %{method: "POST", path: "/invoke", body: <<0, 255>>, headers: %{"x-test" => "yes"}}
    append(server, :submitted, 200, tenant: "t1", principal: "p1", workload: "wl-request",
      task_id: "task-request", payload: %{request: request}
    )

    assert {:ok, loaded} = Postgres.load_request(server, "task-request")
    assert loaded["method"] == "POST"
    assert loaded["path"] == "/invoke"
    assert loaded["body"] == <<0, 255>>
    assert {:ok, nil} = Postgres.load_request(server, "missing")
  end

  # #4919: the session-side mirror of the tasks table's duplicate-idempotency-key
  # transactional rollback, unique per principal.
  test "a duplicate (principal, idempotency_key) session_created rolls back and names the holder", %{
    server: server
  } do
    append(server, :session_created, 300,
      tenant: "t1", principal: "p9", workload: "wl-session", session_id: "sess-k1",
      payload: %{state: "running", node_id: "node-1", base_snapshot_ref: "base", base_digest: "digest",
        token_sha256: "token-1", expires_at: 999, idempotency_key: "dup"}
    )

    assert {:error, {:duplicate_session_idempotency_key, "sess-k1"}} =
             append(server, :session_created, 310,
               tenant: "t1", principal: "p9", workload: "wl-session", session_id: "sess-k2",
               payload: %{state: "running", node_id: "node-1", base_snapshot_ref: "base", base_digest: "digest",
                 token_sha256: "token-2", expires_at: 999, idempotency_key: "dup"}
             )

    # The failed append left no row (and no ops trace): the transaction rolled back.
    assert {:ok, [%{session_id: "sess-k1", idempotency_key: "dup"}]} = Postgres.load_sessions(server)
  end

  test "list_usage returns accumulated task metering", %{server: server} do
    for {task_id, ts, cpu_ms} <- [{"usage-1", 5 * 86_400_000, 2_000}, {"usage-2", 5 * 86_400_000 + 1, 1_000}] do
      append(server, :submitted, ts, tenant: "t1", principal: "p1", workload: "wl-usage", task_id: task_id)
      append(server, :succeeded, ts + 1, principal: "p1", workload: "wl-usage", task_id: task_id,
        payload: %{status_code: 200, body: "", size_bytes: 0, truncated: false, expires_at: nil,
          usage: %{cpu_ms: cpu_ms, peak_rss_mib: 1024, wall_ms: 2_000, vcpu_seconds: cpu_ms / 1_000,
            gb_seconds: 2.0}}
      )
    end

    assert {:ok, %{total: 1, items: [%{principal: "p1", day: 5, vcpu_seconds: 3.0,
      gb_seconds: 4.0, task_count: 2}]}} = Postgres.list_usage(server, since_day: 0)
  end

  test "compact advances the durable marker and removes only the old prefix", %{server: server} do
    {:ok, first} = append(server, :denied, 1)
    {:ok, second} = append(server, :drain, 2)
    {:ok, later} = append(server, :quota_enforced, 10_000)

    assert {:ok, result} = Postgres.compact(server, 100)
    assert result.compacted_through == second
    assert {:ok, ^second} = Postgres.compacted_through(server)
    assert {:error, {:compacted, ^second}} = Postgres.read_from(server, first - 1)
    assert {:ok, [%Op{seq: ^later, kind: :quota_enforced}]} = Postgres.read_from(server, second)
  end

  test "db_size is not supported by the Postgres adapter" do
    assert Postgres.db_size(:unused) == {:error, :not_supported}
  end

  defp append(server, kind, ts, opts \\ []) do
    fields = Keyword.merge([tenant: "t1", payload: %{}], opts)
    op = struct!(Op, Map.merge(Map.new(fields), %{kind: kind, ts: ts}))
    Postgres.append(server, op)
  end

  defp dsn_opts(dsn) do
    uri = URI.parse(dsn)
    [user, pass] = String.split(uri.userinfo || ":", ":", parts: 2)

    [
      hostname: uri.host,
      port: uri.port || 5432,
      username: empty_to_nil(user),
      password: empty_to_nil(pass),
      database: empty_to_nil(String.trim_leading(uri.path || "", "/"))
    ]
  end

  defp empty_to_nil(""), do: nil
  defp empty_to_nil(value), do: value
end
