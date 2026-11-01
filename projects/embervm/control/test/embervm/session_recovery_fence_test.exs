defmodule Embervm.SessionRecoveryFenceTest do
  use ExUnit.Case, async: false
  alias Embervm.{SessionManager, SessionStore}
  alias Embervm.OpLog.{Op, SQLite, Postgres}
  @dsn System.get_env("EMBERVM_OPLOG_TEST_DSN")

  defmodule Auth do
    def authenticate(token) when token in ["owner", "other", "admin"], do: {:ok, token}
    def authenticate(_), do: {:error, :unauthenticated}
  end

  setup %{backend: backend} do
    unique = System.unique_integer([:positive])

    options =
      if backend == SQLite do
        path = Path.join(System.tmp_dir!(), "session-recovery-#{unique}.db")
        on_exit(fn -> File.rm_rf!(path) end)
        [name: nil, path: path]
      else
        schema = "session_recovery_#{unique}"
        opts = Postgres.connect_opts(@dsn)
        {:ok, connection} = Postgrex.start_link(opts)
        Postgrex.query!(connection, ~s(CREATE SCHEMA "#{schema}"), [])
        GenServer.stop(connection)

        on_exit(fn ->
          {:ok, connection} = Postgrex.start_link(opts)
          Postgrex.query!(connection, ~s(DROP SCHEMA "#{schema}" CASCADE), [])
          GenServer.stop(connection)
        end)

        [name: nil, dsn: Keyword.put(opts, :parameters, search_path: schema)]
      end

    options = options ++ [retention_ms: 100, journal_horizon_ms: 100_000]
    log = start_supervised!({backend, options})
    clock = start_supervised!({Agent, fn -> 1000 end})

    %{
      backend: backend,
      log: log,
      clock: clock,
      id: "s-recovery-#{unique}",
      request: %{
        "operation" => "preview",
        "generation" => 0,
        "invoke_started_at" => 200,
        "updated_at" => 600,
        "dispatch_id" => "dispatch-first",
        "reason" => "factory_unknown_dispatch_recovery",
        "request_key" => "factory-1070-recovery"
      }
    }
  end

  defp append(ctx, kind, ts, payload, id \\ nil) do
    assert {:ok, _} =
             ctx.backend.append(ctx.log, %Op{
               kind: kind,
               ts: ts,
               tenant: "test",
               principal: "owner",
               workload: "claude-runtime",
               session_id: id || ctx.id,
               payload: payload
             })
  end

  defp seed(ctx, changes \\ %{}) do
    events = [
      {:session_created, 100,
       %{
         lineage_id: ctx.id,
         node_id: "node-original",
         vm_id: "vm-original",
         state: "running",
         token_sha256: "NEVER-RETURN-TOKEN"
       }},
      {:session_invoke_started, 200, %{turn_seq: 1, dispatch_id: "dispatch-first"}},
      {:session_invoked, 300,
       %{
         interrupted_turn: %{
           "seq" => 1,
           "dispatch_id" => "dispatch-first",
           "cli_session_id" => "cli-original",
           "transcript_path" => "/private/NEVER-RETURN-TRANSCRIPT"
         }
       }},
      {:session_parking, 400, %{reason: "idled", volume_node_id: "node-original"}},
      {:session_parked, 500, %{reason: "idled", volume_node_id: "node-original"}},
      {:session_evicted, 600, %{reason: "node_gone"}}
    ]

    for {kind, ts, payload} <- events do
      {kind, ts, payload} = Map.get(changes, kind, {kind, ts, payload})
      append(ctx, kind, ts, payload)
    end

    ctx
  end

  defp manager(ctx) do
    store = start_supervised!({SessionStore, name: nil, op_log: ctx.log, op_log_mod: ctx.backend})
    registry = :"recovery_registry_#{System.unique_integer([:positive])}"
    start_supervised!({Registry, keys: :unique, name: registry})
    supervisor = start_supervised!({DynamicSupervisor, strategy: :one_for_one})
    capacity = :"recovery_capacity_#{System.unique_integer([:positive])}"
    Embervm.NodeCapacity.create(capacity)
    catalog = :"recovery_catalog_#{System.unique_integer([:positive])}"
    Embervm.WorkloadCatalog.create(catalog)

    opts = [
      name: nil,
      session_store: store,
      op_log: ctx.log,
      op_log_mod: ctx.backend,
      registry: registry,
      supervisor: supervisor,
      capacity_table: capacity,
      catalog_table: catalog,
      clock: fn -> Agent.get(ctx.clock, & &1) end,
      reconcile_interval_ms: 0,
      sweep_interval_ms: 0
    ]

    mgr = start_supervised!({SessionManager, opts})
    Map.merge(ctx, %{store: store, manager: mgr, manager_opts: opts})
  end

  defp preview(ctx, request \\ nil, owner \\ "owner"),
    do: SessionManager.session_recovery_fence(ctx.manager, ctx.id, request || ctx.request, owner)

  defp apply_request(ctx, proof),
    do:
      Map.merge(ctx.request, %{
        "operation" => "apply",
        "expected_preview_sha256" => proof.preview_sha256
      })

  defp rows(ctx), do: elem(ctx.backend.load_sessions(ctx.log), 1)

  defp audits(ctx) do
    {:ok, ops} = ctx.backend.read_from(ctx.log, 0)
    Enum.filter(ops, &(&1.kind == :session_recovery_fenced))
  end

  for backend <- [SQLite, Postgres] do
    @tag backend: backend
    if backend == Postgres and @dsn in [nil, ""],
      do: @tag(skip: "EMBERVM_OPLOG_TEST_DSN is unset")

    test "#{backend} preview/apply/lost-response replay preserves historical result and accounting",
         ctx do
      ctx = ctx |> seed() |> manager()
      [before] = rows(ctx)
      assert {:ok, proposed} = preview(ctx)
      refute proposed.fenced
      refute proposed.recovery_fence.restore_excluded
      assert proposed.recovery_fence.complete
      assert proposed.recovery_fence.terminal_reason == "node_gone"
      assert rows(ctx) == [before]
      assert audits(ctx) == []
      request = apply_request(ctx, proposed)
      assert {:ok, applied} = preview(ctx, request)
      assert applied.fenced and applied.recovery_fence.restore_excluded
      assert applied.preview_sha256 == proposed.preview_sha256
      assert applied.evidence_sha256 == proposed.evidence_sha256
      [after_row] = rows(ctx)
      assert Map.delete(after_row, :recovery_fence) == Map.delete(before, :recovery_fence)
      assert {:error, :recovery_fenced} = SessionStore.session_restore_allowed(ctx.store, ctx.id)
      Agent.update(ctx.clock, &(&1 + 1))
      assert {:ok, replay} = preview(ctx, request)
      assert replay.preview_sha256 == applied.preview_sha256
      assert replay.observed_at_unix_ms > applied.observed_at_unix_ms
      assert length(audits(ctx)) == 1
      refute inspect(replay) =~ "NEVER-RETURN"

      assert {:error, :recovery_fence_conflict, _} =
               preview(ctx, %{ctx.request | "request_key" => "another-key"})
    end

    @tag backend: backend
    if backend == Postgres and @dsn in [nil, ""],
      do: @tag(skip: "EMBERVM_OPLOG_TEST_DSN is unset")

    test "#{backend} restart and pruning cannot reopen a retained terminal lineage", ctx do
      ctx = ctx |> seed() |> manager()
      assert {:ok, proposed} = preview(ctx)
      assert {:ok, _} = preview(ctx, apply_request(ctx, proposed))

      rebuilt =
        start_supervised!({SessionStore, name: nil, op_log: ctx.log, op_log_mod: ctx.backend},
          id: :rebuilt_recovery_store
        )

      assert {:ok, %{recovery_fence: fence}} = SessionStore.get(rebuilt, ctx.id)
      assert is_map(fence)
      assert {:error, :recovery_fenced} = SessionStore.session_restore_allowed(rebuilt, ctx.id)
      assert {:ok, _} = ctx.backend.compact(ctx.log, 1000)
      assert rows(ctx) == []
      # The old hot holder still exists. Restore must consult durable authority.
      assert {:ok, _} = SessionStore.get(ctx.store, ctx.id)
      assert {:error, :unknown_lineage} = SessionStore.session_restore_allowed(ctx.store, ctx.id)
    end

    @tag backend: backend
    if backend == Postgres and @dsn in [nil, ""],
      do: @tag(skip: "EMBERVM_OPLOG_TEST_DSN is unset")

    test "#{backend} digest change, wrong owner and malformed request never append", ctx do
      ctx = ctx |> seed() |> manager()
      assert {:ok, proposed} = preview(ctx)

      wrong_digest =
        Map.put(
          apply_request(ctx, proposed),
          "expected_preview_sha256",
          String.duplicate("0", 64)
        )

      assert {:error, :recovery_preview_changed, _} = preview(ctx, wrong_digest)
      assert {:error, :historical_not_found, _} = preview(ctx, nil, "other")

      assert {:error, :recovery_invalid_request, _} =
               preview(ctx, Map.put(ctx.request, "bypass", true))

      assert {:error, :historical_multiple_dispatches, _} =
               preview(ctx, %{ctx.request | "dispatch_id" => "other"})

      assert audits(ctx) == []
    end

    @tag backend: backend
    if backend == Postgres and @dsn in [nil, ""],
      do: @tag(skip: "EMBERVM_OPLOG_TEST_DSN is unset")

    test "#{backend} late lifecycle and changed retained identity refuse apply", ctx do
      ctx = ctx |> seed() |> manager()
      assert {:ok, proposed} = preview(ctx)
      append(ctx, :session_invoke_started, 700, %{turn_seq: 2, dispatch_id: "later"})

      assert {:error, :historical_reordered_or_later_event, _} =
               preview(ctx, apply_request(ctx, proposed))

      assert audits(ctx) == []
    end

    @tag backend: backend
    if backend == Postgres and @dsn in [nil, ""],
      do: @tag(skip: "EMBERVM_OPLOG_TEST_DSN is unset")

    test "#{backend} serialized pending restore or export refuses a previously previewed fence",
         ctx do
      ctx = ctx |> seed() |> manager()
      assert {:ok, proposed} = preview(ctx)

      :sys.replace_state(ctx.manager, fn state ->
        %{state | inflight_restore_lineages: MapSet.new([ctx.id])}
      end)

      assert {:error, :recovery_pending_work, _} = preview(ctx, apply_request(ctx, proposed))

      :sys.replace_state(ctx.manager, fn state ->
        %{
          state
          | inflight_restore_lineages: MapSet.new(),
            handoff_exports: %{ctx.id => %{lineage_id: ctx.id, status: "pending"}}
        }
      end)

      assert {:error, :recovery_pending_export, _} = preview(ctx, apply_request(ctx, proposed))
      assert audits(ctx) == []
    end

    @tag backend: backend
    if backend == Postgres and @dsn in [nil, ""],
      do: @tag(skip: "EMBERVM_OPLOG_TEST_DSN is unset")

    test "#{backend} pruned successor and missing original history cannot authorize a fence",
         ctx do
      ctx = ctx |> seed() |> manager()

      append(
        ctx,
        :session_created,
        700,
        %{lineage_id: ctx.id, node_id: "other", vm_id: "heir-vm", state: "running"},
        "s-heir"
      )

      append(ctx, :session_destroyed, 800, %{reason: "destroyed"}, "s-heir")
      assert {:ok, _} = ctx.backend.compact(ctx.log, 1000)
      assert {:error, :historical_successor, _} = preview(ctx)
      assert {:ok, _} = ctx.backend.compact(ctx.log, 200_000)
      assert {:error, :historical_not_found, _} = preview(ctx)
    end

    @tag backend: backend
    if backend == Postgres and @dsn in [nil, ""],
      do: @tag(skip: "EMBERVM_OPLOG_TEST_DSN is unset")

    test "#{backend} brick-loss parking cannot substitute for positive idled cessation", ctx do
      ctx =
        ctx
        |> seed(%{
          session_parked:
            {:session_parked, 500, %{reason: "brick_gone", volume_node_id: "node-original"}}
        })
        |> manager()

      assert {:error, :historical_unsupported_transition, _} = preview(ctx)
      assert audits(ctx) == []
    end

    @tag backend: backend
    if backend == Postgres and @dsn in [nil, ""],
      do: @tag(skip: "EMBERVM_OPLOG_TEST_DSN is unset")

    test "#{backend} missing interruption proof does not fabricate prior delivery", ctx do
      ctx = ctx |> seed(%{session_invoked: {:session_invoked, 300, %{}}}) |> manager()
      assert {:error, :recovery_interruption_unavailable, _} = preview(ctx)
      assert audits(ctx) == []
    end
  end

  @tag backend: SQLite
  test "management owner only; session capability and admin substitution cannot seal a lineage",
       ctx do
    ctx = ctx |> seed() |> manager()

    config = [
      authenticator: Auth,
      session_manager: SessionManager,
      session_manager_server: ctx.manager,
      usage_admins: ["admin"]
    ]

    old = for {key, _} <- config, do: {key, Application.fetch_env(:embervm, key)}
    for {key, value} <- config, do: Application.put_env(:embervm, key, value)

    on_exit(fn ->
      for {key, value} <- old do
        case value do
          {:ok, value} -> Application.put_env(:embervm, key, value)
          :error -> Application.delete_env(:embervm, key)
        end
      end
    end)

    request = fn token, body ->
      Plug.Test.conn(:post, "/v1/sessions/#{ctx.id}/recovery-fence", body)
      |> Plug.Conn.put_req_header("authorization", "Bearer " <> token)
      |> Embervm.Router.call(Embervm.Router.init([]))
    end

    assert request.("owner", Jason.encode!(ctx.request)).status == 200
    assert request.("other", Jason.encode!(ctx.request)).status == 404
    assert request.("admin", Jason.encode!(ctx.request)).status == 404
    assert request.("retired-session-token", Jason.encode!(ctx.request)).status == 401
    assert request.("owner", "{}").status == 400
    assert request.("owner", String.duplicate("x", 4097)).status == 413
    assert audits(ctx) == []
  end
end
