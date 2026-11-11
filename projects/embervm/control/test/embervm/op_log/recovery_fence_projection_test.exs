defmodule Embervm.OpLog.RecoveryFenceProjectionTest do
  use ExUnit.Case, async: false
  alias Embervm.OpLog.{Op, Postgres, SQLite}

  @dsn System.get_env("EMBERVM_OPLOG_TEST_DSN")

  setup %{backend: backend} do
    {options, cleanup} =
      if backend == SQLite do
        path =
          Path.join(System.tmp_dir!(), "recovery-fence-#{System.unique_integer([:positive])}.db")

        {[name: nil, path: path, retention_ms: 100, journal_horizon_ms: 100_000],
         fn -> File.rm_rf!(path) end}
      else
        schema = "recovery_fence_#{System.unique_integer([:positive])}"
        options = Postgres.connect_opts(@dsn)
        {:ok, connection} = Postgrex.start_link(options)
        Postgrex.query!(connection, ~s(CREATE SCHEMA "#{schema}"), [])
        GenServer.stop(connection)
        options = Keyword.put(options, :parameters, search_path: schema)

        cleanup = fn ->
          {:ok, connection} = Postgrex.start_link(options)
          Postgrex.query!(connection, ~s(DROP SCHEMA "#{schema}" CASCADE), [])
          GenServer.stop(connection)
        end

        {[name: nil, dsn: options, retention_ms: 100, journal_horizon_ms: 100_000], cleanup}
      end

    {:ok, server} = backend.start_link(options)

    on_exit(fn ->
      Embervm.TestProcess.stop_safely(server)
      cleanup.()
    end)

    %{backend: backend, server: server, options: options}
  end

  defp append(ctx, kind, ts, payload) do
    ctx.backend.append(ctx.server, %Op{
      kind: kind,
      tenant: "test",
      principal: "owner",
      workload: "claude-runtime",
      session_id: "s-original",
      ts: ts,
      payload: payload
    })
  end

  defp seed(ctx) do
    assert {:ok, _} =
             append(ctx, :session_created, 100, %{
               lineage_id: "s-original",
               node_id: "node-original",
               vm_id: "vm-original",
               state: "running"
             })

    assert {:ok, _} =
             append(ctx, :session_invoke_started, 200, %{
               turn_seq: 1,
               dispatch_id: "exact-dispatch"
             })

    assert {:ok, _} = append(ctx, :session_invoked, 300, %{})

    assert {:ok, _} =
             append(ctx, :session_parking, 400, %{
               reason: "idled",
               volume_node_id: "node-original"
             })

    assert {:ok, _} =
             append(ctx, :session_parked, 500, %{reason: "idled", volume_node_id: "node-original"})

    assert {:ok, _} = append(ctx, :session_evicted, 600, %{reason: "node_gone"})
    ctx
  end

  defp fence do
    %{
      "schema" => 1,
      "session_id" => "s-original",
      "lineage_id" => "s-original",
      "principal" => "owner",
      "workload" => "claude-runtime",
      "generation" => 0,
      "invoke_started_at" => 200,
      "updated_at" => 600,
      "dispatch_id" => "exact-dispatch",
      "terminal_reason" => "node_gone",
      "request_key" => "factory-1070-recovery",
      "reason" => "factory_unknown_dispatch_recovery",
      "preview_sha256" => String.duplicate("a", 64),
      "evidence_sha256" => String.duplicate("b", 64)
    }
  end

  defp current(ctx) do
    assert {:ok, [row]} = ctx.backend.load_sessions(ctx.server)
    row
  end

  defp query(ctx, sql, params \\ []) do
    conn = :sys.get_state(ctx.server).conn

    if ctx.backend == Postgres do
      Postgrex.query!(conn, sql, params).rows
    else
      {:ok, stmt} = Exqlite.Sqlite3.prepare(conn, sql)

      try do
        :ok = Exqlite.Sqlite3.bind(stmt, params)

        collect = fn collect, acc ->
          case Exqlite.Sqlite3.step(conn, stmt) do
            {:row, row} -> collect.(collect, [row | acc])
            :done -> Enum.reverse(acc)
          end
        end

        collect.(collect, [])
      after
        Exqlite.Sqlite3.release(conn, stmt)
      end
    end
  end

  for backend <- [SQLite, Postgres] do
    @tag backend: backend
    if backend == Postgres and @dsn in [nil, ""],
      do: @tag(skip: "EMBERVM_OPLOG_TEST_DSN is unset")

    test "#{backend} immutable recovery projection survives restart without rewriting history",
         ctx do
      seed(ctx)
      before = current(ctx)
      assert :ok = ctx.backend.session_restore_allowed(ctx.server, "s-original")
      assert {:ok, _} = append(ctx, :session_recovery_fenced, 700, %{recovery_fence: fence()})

      assert {:error, :recovery_fenced} =
               ctx.backend.session_restore_allowed(ctx.server, "s-original")

      assert current(ctx) == %{before | recovery_fence: fence()}
      assert {:ok, _} = append(ctx, :session_recovery_fenced, 701, %{recovery_fence: fence()})
      assert current(ctx) == %{before | recovery_fence: fence()}
      GenServer.stop(ctx.server)
      {:ok, restarted} = ctx.backend.start_link(ctx.options)
      on_exit(fn -> Embervm.TestProcess.stop_safely(restarted) end)

      assert {:error, :recovery_fenced} =
               ctx.backend.session_restore_allowed(restarted, "s-original")

      assert current(%{ctx | server: restarted}) == %{before | recovery_fence: fence()}
    end

    @tag backend: backend
    if backend == Postgres and @dsn in [nil, ""],
      do: @tag(skip: "EMBERVM_OPLOG_TEST_DSN is unset")

    test "#{backend} exact identity mismatch rolls back audit and projection", ctx do
      seed(ctx)
      assert {:ok, before_ops} = ctx.backend.read_from(ctx.server, 0)
      before = current(ctx)

      for {key, wrong} <- [
            {"session_id", "other"},
            {"lineage_id", "other"},
            {"principal", "other"},
            {"workload", "other"},
            {"generation", 1},
            {"invoke_started_at", 201},
            {"updated_at", 601},
            {"dispatch_id", "other"},
            {"terminal_reason", "idle_ttl"}
          ] do
        assert {:error, :recovery_fence_identity_mismatch} =
                 append(ctx, :session_recovery_fenced, 700, %{
                   recovery_fence: Map.put(fence(), key, wrong)
                 })

        assert current(ctx) == before
        assert {:ok, ^before_ops} = ctx.backend.read_from(ctx.server, 0)
      end

      assert {:ok, _} = append(ctx, :session_recovery_fenced, 700, %{recovery_fence: fence()})

      assert {:error, :recovery_fence_identity_mismatch} =
               append(ctx, :session_recovery_fenced, 701, %{
                 recovery_fence: Map.put(fence(), "request_key", "changed-key")
               })

      assert current(ctx).recovery_fence == fence()
    end

    @tag backend: backend
    if backend == Postgres and @dsn in [nil, ""],
      do: @tag(skip: "EMBERVM_OPLOG_TEST_DSN is unset")

    test "#{backend} malformed fence and backend failures cannot permit restore", ctx do
      seed(ctx)

      query(ctx, "UPDATE sessions SET recovery_fence_json=$1 WHERE session_id=$2", [
        "not-json",
        "s-original"
      ])

      assert {:error, :recovery_fenced} =
               ctx.backend.session_restore_allowed(ctx.server, "s-original")

      query(ctx, "ALTER TABLE sessions DROP COLUMN recovery_fence_json")

      assert {:error, :recovery_store_unavailable} =
               ctx.backend.session_restore_allowed(ctx.server, "s-original")

      assert {:error, :recovery_store_unavailable} =
               ctx.backend.session_restore_allowed(:no_such_recovery_log, "s-original")
    end

    @tag backend: backend
    if backend == Postgres and @dsn in [nil, ""],
      do: @tag(skip: "EMBERVM_OPLOG_TEST_DSN is unset")

    test "#{backend} migration preserves existing terminal state and pruning removes restore holder",
         ctx do
      seed(ctx)
      before = current(ctx)
      query(ctx, "ALTER TABLE sessions DROP COLUMN recovery_fence_json")
      GenServer.stop(ctx.server)
      {:ok, migrated} = ctx.backend.start_link(ctx.options)
      on_exit(fn -> Embervm.TestProcess.stop_safely(migrated) end)
      ctx = %{ctx | server: migrated}
      assert current(ctx) == before
      assert {:ok, _} = append(ctx, :session_recovery_fenced, 700, %{recovery_fence: fence()})
      assert {:ok, _} = ctx.backend.compact(ctx.server, 1000)
      assert {:ok, []} = ctx.backend.load_sessions(ctx.server)
      # SessionManager's existing unknown_lineage admission denial now applies;
      # there is no surviving terminal holder whose authority can be restored.
      assert {:error, :unknown_lineage} =
               ctx.backend.session_restore_allowed(ctx.server, "s-original")
    end
  end
end
