defmodule Embervm.SessionCessationFixtures do
  import ExUnit.Assertions
  import ExUnit.Callbacks
  alias Embervm.OpLog.{Op, SQLite}

  def database do
    path = Path.join(System.tmp_dir!(), "cessation-#{System.unique_integer([:positive])}.db")

    log =
      start_supervised!(
        {SQLite, name: nil, path: path, retention_ms: 100, journal_horizon_ms: 100_000}
      )

    on_exit(fn -> File.rm_rf!(path) end)

    %{
      log: log,
      backend: SQLite,
      id: "s-original",
      expected: %{generation: 0, invoke_started_at: 200, updated_at: 600}
    }
  end

  def chain(ctx, changes \\ %{}) do
    events = [
      {:session_created, 100,
       %{
         lineage_id: ctx.id,
         node_id: "node-original",
         vm_id: "vm-original",
         state: "running",
         token_sha256: "NEVER-RETURN",
         expires_at: 100_000
       }},
      {:session_invoke_started, 200, %{turn_seq: 1}},
      {:session_invoked, 300, %{turn: %{result: "NEVER-RETURN-RESULT"}}},
      {:session_parking, 400, %{reason: "idled", volume_node_id: "node-original"}},
      {:session_parked, 500, %{reason: "idled", volume_node_id: "node-original"}},
      {:session_evicted, 600, %{reason: :idle_ttl}}
    ]

    for {kind, ts, payload} <- events do
      {kind, ts, payload} = Map.get(changes, kind, {kind, ts, payload})
      append(ctx, kind, ts, payload)
    end

    ctx
  end

  def append(ctx, kind, ts, payload, id \\ nil) do
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

  def prune(ctx) do
    assert {:ok, _} = ctx.backend.compact(ctx.log, 1000)
    ctx
  end

  def proof(ctx, principal \\ "owner"),
    do: ctx.backend.session_cessation_evidence(ctx.log, ctx.id, ctx.expected, principal)

  def query(ctx, sql, params \\ []) do
    conn = :sys.get_state(ctx.log).conn

    if ctx.backend == SQLite do
      {:ok, statement} = Exqlite.Sqlite3.prepare(conn, sql)

      try do
        :ok = Exqlite.Sqlite3.bind(statement, params)

        collect = fn collect, acc ->
          case Exqlite.Sqlite3.step(conn, statement) do
            {:row, row} -> collect.(collect, [row | acc])
            :done -> Enum.reverse(acc)
          end
        end

        collect.(collect, [])
      after
        Exqlite.Sqlite3.release(conn, statement)
      end
    else
      Postgrex.query!(conn, sql, params).rows
    end
  end

  def assert_residency_aliases_refuse(ctx) do
    aliases = [
      %{"lineage_id" => ctx.id},
      %{"identity" => %{"session_id" => ctx.id}},
      %{"identity" => %{"vm_id" => "vm-original"}},
      %{"restore" => %{"vm_id" => "vm-original"}, "claim_reconciled" => false}
    ]

    # These terminal claims use another session id, so the proof must inspect
    # retained identity/physical aliases rather than only the SQL session column.
    for {alias_fields, index} <- Enum.with_index(aliases) do
      id = ctx.id <> "-claim-#{index}"

      sql =
        "INSERT INTO residency_intents(id,principal,idempotency_key,session_id,state,updated_at,record_json) VALUES($1,$2,$3,$4,$5,$6,$7)"

      params = [id, "owner", id, "s-other", "failed", 700, Jason.encode!(alias_fields)]
      query(ctx, sql, params)

      try do
        assert {:error, :historical_current_owner, _} = proof(ctx)
      after
        query(ctx, "DELETE FROM residency_intents WHERE id=$1", [id])
      end
    end

    assert {:ok, _} = proof(ctx)
  end

  def manager(ctx) do
    store =
      start_supervised!(
        {Embervm.SessionStore, name: nil, op_log: ctx.log, op_log_mod: ctx.backend}
      )

    registry = :"cessation_registry_#{System.unique_integer([:positive])}"
    start_supervised!({Registry, keys: :unique, name: registry})
    supervisor = start_supervised!({DynamicSupervisor, strategy: :one_for_one})
    capacity = :"cessation_capacity_#{System.unique_integer([:positive])}"
    Embervm.NodeCapacity.create(capacity)
    catalog = :"cessation_catalog_#{System.unique_integer([:positive])}"
    Embervm.WorkloadCatalog.create(catalog)

    manager =
      start_supervised!(
        {Embervm.SessionManager,
         name: nil,
         session_store: store,
         op_log: ctx.log,
         op_log_mod: ctx.backend,
         registry: registry,
         supervisor: supervisor,
         capacity_table: capacity,
         catalog_table: catalog,
         clock: fn -> 1000 end,
         reconcile_interval_ms: 0,
         sweep_interval_ms: 0}
      )

    Map.merge(ctx, %{store: store, manager: manager, registry: registry})
  end
end

defmodule Embervm.SessionCessationEvidenceTest do
  use ExUnit.Case, async: false
  import Embervm.SessionCessationFixtures
  alias Embervm.{SessionCessationEvidence, SessionManager, SessionStore}

  setup do
    database()
  end

  test "retained positive chain survives pruning and restart without inventing a result", ctx do
    ctx = ctx |> chain() |> prune()
    assert {:ok, saved} = proof(ctx)
    assert saved.generation == 0
    assert saved.last_invoke_at == 300
    assert saved.preceding_state == "parked"
    assert saved.evidence_sha256 =~ ~r/^[0-9a-f]{64}$/
    ctx = manager(ctx)

    assert {:ok, view} =
             SessionManager.session_cessation_evidence(ctx.manager, ctx.id, ctx.expected, "owner")

    assert view.historical_cessation.complete
    assert view.historical_cessation.no_successor
    refute inspect(view) =~ "NEVER-RETURN"
    assert {:ok, []} = ctx.backend.load_sessions(ctx.log)
    assert {:ok, unchanged} = proof(ctx)
    assert unchanged.evidence_sha256 == saved.evidence_sha256
  end

  test "original terminal row and later terminal hot holder cannot authorize proof", ctx do
    ctx = chain(ctx)
    assert {:error, :historical_current_owner, _} = proof(ctx)
    ctx = manager(ctx)
    prune(ctx)
    assert {:ok, _} = proof(ctx)

    assert {:error, :historical_current_owner, _} =
             SessionStore.session_cessation_evidence(ctx.store, ctx.id, ctx.expected, "owner")
  end

  test "owner scope and exact historical timestamps are mandatory", ctx do
    ctx = ctx |> chain() |> prune()
    assert {:error, :historical_not_found, _} = proof(ctx, "other")
    assert {:ok, _} = proof(ctx, :admin)

    assert {:error, :historical_identity_mismatch, _} =
             proof(%{ctx | expected: %{ctx.expected | invoke_started_at: 201}})

    assert {:error, :historical_invalid_identity, _} =
             proof(%{ctx | expected: %{ctx.expected | generation: 1}})
  end

  test "pruned successor is found in complete creation history", ctx do
    ctx = ctx |> chain() |> prune()

    append(
      ctx,
      :session_created,
      700,
      %{lineage_id: ctx.id, node_id: "n2", vm_id: "v2", state: "running"},
      "s-heir"
    )

    append(ctx, :session_destroyed, 800, %{reason: :destroyed}, "s-heir")
    prune(ctx)
    assert {:ok, []} = ctx.backend.load_sessions(ctx.log)
    assert {:error, :historical_successor, _} = proof(ctx)
  end

  test "advanced marker invalidates physically retained rows", ctx do
    ctx = ctx |> chain() |> prune()
    conn = :sys.get_state(ctx.log).conn

    assert :ok =
             Exqlite.Sqlite3.execute(
               conn,
               "INSERT OR REPLACE INTO meta(key,value) VALUES('compacted_through_seq',2)"
             )

    assert {:error, :historical_unavailable, _} = proof(ctx)
  end

  test "missing journal is not cessation", ctx do
    assert {:error, :historical_not_found, _} = proof(ctx)
  end

  test "terminal residency aliases remain ownership evidence", ctx do
    ctx |> chain() |> prune() |> assert_residency_aliases_refuse()
  end

  test "malformed retained payload fails closed", ctx do
    ctx = ctx |> chain() |> prune()
    query(ctx, "UPDATE ops SET payload_json='not json' WHERE kind='session_parked'")
    assert {:error, :historical_malformed_history, _} = proof(ctx)
  end

  test "brick disappearance cannot replace positive parking", ctx do
    ctx =
      ctx
      |> chain(%{session_parking: {:session_brick_gone, 400, %{reason: "brick_gone"}}})
      |> prune()

    assert {:error, :historical_unsupported_transition,
            %{event_kind: "session_brick_gone", event_reason: "brick_gone"}} = proof(ctx)
  end

  test "changed physical owner and later dispatch remain fenced", ctx do
    ctx =
      ctx
      |> chain(%{
        session_parked: {:session_parked, 500, %{reason: "idled", volume_node_id: "different"}}
      })
      |> prune()

    assert {:error, :historical_unsupported_transition, _} = proof(ctx)
  end

  test "later lifecycle after terminal is rejected", ctx do
    ctx = ctx |> chain() |> prune()
    append(ctx, :session_invoke_started, 700, %{turn_seq: 2})
    assert {:error, :historical_reordered_or_later_event, _} = proof(ctx)
  end

  test "pending restore before successor registration refuses serialized proof", ctx do
    ctx = ctx |> chain() |> prune() |> manager()

    :sys.replace_state(ctx.manager, fn state ->
      %{
        state
        | inflight_restore_lineages: MapSet.new([ctx.id]),
          create_inflight: %{1 => %{lineage_id: ctx.id, restore_lineage: ctx.id}}
      }
    end)

    assert {:error, :historical_pending_work, _} =
             SessionManager.session_cessation_evidence(ctx.manager, ctx.id, ctx.expected, "owner")
  end

  test "a queued proof sees a prior serialized ownership change", ctx do
    ctx = ctx |> chain() |> prune() |> manager()
    :sys.suspend(ctx.manager)

    task =
      Task.async(fn ->
        SessionManager.session_cessation_evidence(ctx.manager, ctx.id, ctx.expected, "owner")
      end)

    :sys.replace_state(ctx.manager, fn state ->
      %{state | relighting: %{ctx.id => [{self(), %{}}]}}
    end)

    :sys.resume(ctx.manager)
    assert {:error, :historical_pending_work, _} = Task.await(task)
  end

  test "unrelated new creation leaves exact evidence digest stable", ctx do
    ctx = ctx |> chain() |> prune()
    assert {:ok, before} = proof(ctx)

    append(
      ctx,
      :session_created,
      700,
      %{lineage_id: "s-other", node_id: "n2", vm_id: "v2", state: "running"},
      "s-other"
    )

    assert {:ok, after_proof} = proof(ctx)
    assert before.evidence_sha256 == after_proof.evidence_sha256
  end

  test "oversized payload and overflowing event count fail before loading bodies", ctx do
    ctx = ctx |> chain() |> prune()
    append(ctx, :session_invoked, 700, %{large: String.duplicate("x", 65_537)})
    assert {:error, :historical_history_bound, _} = proof(ctx)

    query = fn
      "SELECT principal" <> _, _ -> {:ok, [["owner"]]}
      "SELECT value" <> _, _ -> {:ok, [[0]]}
      "SELECT COUNT" <> _, _ -> {:ok, [[257, 10, 10]]}
      _, _ -> flunk("overflow must not fetch payloads")
    end

    assert {:error, :historical_history_bound, _} =
             SessionCessationEvidence.read(query, :sqlite, & &1, ctx.id, ctx.expected, "owner")
  end

  test "store failures cannot become empty ownership", ctx do
    query = fn _, _ -> {:error, :connection_down} end

    assert {:error, :historical_store_unavailable, _} =
             SessionCessationEvidence.read(query, :postgres, & &1, ctx.id, ctx.expected, "owner")
  end
end

defmodule Embervm.SessionCessationRouterTest do
  use ExUnit.Case, async: false
  import Embervm.SessionCessationFixtures

  defmodule Auth do
    def authenticate(token) when token in ["owner", "other", "admin"], do: {:ok, token}
    def authenticate(_), do: {:error, :unauthenticated}
  end

  setup do
    ctx = database() |> chain() |> prune() |> manager()

    overrides = [
      authenticator: Auth,
      session_manager: Embervm.SessionManager,
      session_manager_server: ctx.manager,
      usage_admins: ["admin"]
    ]

    old = for {key, _} <- overrides, do: {key, Application.fetch_env(:embervm, key)}
    for {key, value} <- overrides, do: Application.put_env(:embervm, key, value)

    on_exit(fn ->
      for {key, value} <- old do
        case value do
          {:ok, v} -> Application.put_env(:embervm, key, v)
          :error -> Application.delete_env(:embervm, key)
        end
      end
    end)

    ctx
  end

  defp request(ctx, token, query \\ "generation=0&invoke_started_at=200&updated_at=600") do
    Plug.Test.conn(:get, "/v1/sessions/#{ctx.id}/cessation-evidence?#{query}")
    |> Plug.Conn.put_req_header("authorization", "Bearer " <> token)
    |> Embervm.Router.call(Embervm.Router.init([]))
  end

  test "management owner and existing admin can read; retired capability cannot", ctx do
    assert request(ctx, "owner").status == 200
    assert request(ctx, "admin").status == 200
    denied = request(ctx, "other")
    assert denied.status == 404
    refute denied.resp_body =~ "historical_cessation"
    assert request(ctx, "retired-session-token").status == 401
    assert {:ok, []} = ctx.backend.load_sessions(ctx.log)
  end

  test "bad identity and unsupported evidence return bounded refusal", ctx do
    assert request(ctx, "owner", "generation=0&invoke_started_at=200").status == 400

    assert request(ctx, "owner", "generation[nested]=0&invoke_started_at=200&updated_at=600").status ==
             400

    assert request(ctx, "owner", "generation=0&invoke_started_at=200&updated_at=600&bypass=true").status ==
             400

    response = request(ctx, "owner", "generation=0&invoke_started_at=201&updated_at=600")
    assert response.status == 409
    body = Jason.decode!(response.resp_body)
    assert body["reason"] == "historical_identity_mismatch"

    assert Map.keys(body["diagnostics"])
           |> Enum.all?(
             &(&1 in ~w(history_complete creation_history_complete event_count terminal_state preceding_state terminal_reason event_kind event_reason))
           )

    refute response.resp_body =~ "NEVER-RETURN"
  end
end

defmodule Embervm.SessionCessationPostgresTest do
  use ExUnit.Case, async: false
  import Embervm.SessionCessationFixtures
  alias Embervm.OpLog.Postgres
  @dsn System.get_env("EMBERVM_TEST_POSTGRES_DSN")
  if is_nil(@dsn),
    do: @moduletag(skip: "set EMBERVM_TEST_POSTGRES_DSN to an isolated test database")

  test "PostgreSQL snapshot queries reject durable aliases and pruned successors" do
    log =
      start_supervised!(
        {Postgres, name: nil, dsn: @dsn, retention_ms: 100, journal_horizon_ms: 100_000}
      )

    ctx = %{
      backend: Postgres,
      log: log,
      id: "s-pg-#{System.unique_integer([:positive])}",
      expected: %{generation: 0, invoke_started_at: 200, updated_at: 600}
    }

    ctx = chain(ctx)
    assert {:error, :historical_current_owner, _} = proof(ctx)
    ctx = prune(ctx)
    assert {:ok, saved} = proof(ctx)
    assert saved.history_complete
    assert saved.creation_history_complete
    assert_residency_aliases_refuse(ctx)
    [[first_seq]] = query(ctx, "SELECT MIN(seq) FROM ops WHERE session_id=$1", [ctx.id])
    old_marker = query(ctx, "SELECT value FROM meta WHERE key='compacted_through_seq'")

    query(
      ctx,
      "INSERT INTO meta(key,value) VALUES('compacted_through_seq',$1) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
      [first_seq]
    )

    try do
      assert {:error, :historical_unavailable, _} = proof(ctx)
    after
      case old_marker do
        [] ->
          query(ctx, "DELETE FROM meta WHERE key='compacted_through_seq'")

        [[value]] ->
          query(ctx, "UPDATE meta SET value=$1 WHERE key='compacted_through_seq'", [value])
      end
    end

    # Compressed ETF can expand beyond the predecode byte cap. It is rejected
    # even when the database row itself is small and structurally valid ETF.
    [[original]] =
      query(ctx, "SELECT payload_blob FROM ops WHERE session_id=$1 AND kind='session_parked'", [
        ctx.id
      ])

    compressed = :erlang.term_to_binary(%{large: String.duplicate("x", 100_000)}, [:compressed])

    query(ctx, "UPDATE ops SET payload_blob=$1 WHERE session_id=$2 AND kind='session_parked'", [
      compressed,
      ctx.id
    ])

    try do
      assert {:error, :historical_malformed_history, _} = proof(ctx)
    after
      query(ctx, "UPDATE ops SET payload_blob=$1 WHERE session_id=$2 AND kind='session_parked'", [
        original,
        ctx.id
      ])
    end

    append(
      ctx,
      :session_created,
      700,
      %{lineage_id: ctx.id, node_id: "pg-other", vm_id: "pg-heir", state: "running"},
      ctx.id <> "-heir"
    )

    append(ctx, :session_destroyed, 800, %{reason: :destroyed}, ctx.id <> "-heir")
    prune(ctx)
    assert {:error, :historical_successor, _} = proof(ctx)
  end
end
