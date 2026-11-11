defmodule Embervm.RouterResidencyTest do
  use ExUnit.Case, async: false
  alias Embervm.{Router, SessionStore}
  alias Embervm.OpLog.SQLite

  defmodule ManagementAuth do
    def authenticate("management"), do: {:ok, "management"}
    def authenticate(_), do: {:error, :unauthenticated}
  end

  defmodule NoGuestManager do
    def invoke(store, session_id, request) do
      SessionStore.record_residency_handoff(store, session_id, request.dispatch_id, request.residency, nil)
    end
  end

  setup do
    path = Path.join(System.tmp_dir!(), "residency-router-#{System.unique_integer([:positive])}.db")
    on_exit(fn -> File.rm_rf!(path) end)
    {:ok, log} = SQLite.start_link(name: nil, path: path)
    {:ok, store} = SessionStore.start_link(name: nil, op_log: log, clock: fn -> 1000 end)
    overrides = [authenticator: ManagementAuth, session_store: store, session_manager: NoGuestManager,
      session_manager_server: store, async_residency_enabled: true]
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
    {:ok, first} = SessionStore.create(store, %{tenant: "t", principal: "p1", workload: "w", node_id: "n", vm_id: "v", expires_at: 100_000})
    {:ok, second} = SessionStore.create(store, %{tenant: "t", principal: "p2", workload: "w", node_id: "n", vm_id: "v2", expires_at: 100_000})
    %{store: store, log: log, first: first, second: second}
  end

  defp request(method, path, token, body \\ "", headers \\ []) do
    conn = Plug.Test.conn(method, path, body) |> Plug.Conn.put_req_header("authorization", "Bearer " <> token)
    conn = Enum.reduce(headers, conn, fn {key, value}, acc -> Plug.Conn.put_req_header(acc, key, value) end)
    Router.call(conn, Router.init([]))
  end

  test "only the owning capability can submit, inspect or cancel; no secret payload is retained", ctx do
    path = "/v1/sessions/#{ctx.first.session_id}/residency"
    payload = Jason.encode!(%{generation: 0, deadline: 90_000, task_deadline: 95_000, first_wait_at: 1000})
    for token <- ["management", ctx.second.token, "bad"] do
      assert request(:post, path, token, payload, [{"idempotency-key", "key"}]).status == 403
    end
    assert request(:post, path, ctx.first.token, "{\"prompt\":\"private\"}").status == 400
    accepted = request(:post, path, ctx.first.token, payload, [{"idempotency-key", "key"}])
    assert accepted.status == 202
    intent = Jason.decode!(accepted.resp_body)
    assert request(:get, path <> "/" <> intent["id"], ctx.second.token).status == 403
    assert request(:delete, path <> "/" <> intent["id"], "management").status == 403
    assert request(:get, path <> "?limit=1", ctx.first.token).status == 200
    assert {:ok, ops} = SQLite.read_from(ctx.log, 0)
    refute inspect(Enum.filter(ops, &(&1.kind == :residency_changed))) =~ ctx.first.token
    refute inspect(ops) =~ "private"
    Application.put_env(:embervm, :async_residency_enabled, false)
    assert request(:post, path, ctx.first.token, payload, [{"idempotency-key", "key"}]).status == 202
    assert request(:post, path, ctx.first.token, payload, [{"idempotency-key", "new"}]).status == 501
    assert request(:delete, path <> "/" <> intent["id"], ctx.first.token).status == 200
    response = request(:post, "/v1/sessions/#{ctx.first.session_id}/invoke", ctx.first.token, "private",
      [{"x-ember-residency-operation", intent["id"]}, {"x-ember-residency-epoch", "old"}, {"x-ember-dispatch-id", "exact"}])
    assert response.status == 409
    assert Plug.Conn.get_resp_header(response, "x-ember-invoke-outcome") == ["not_dispatched"]
    assert Jason.decode!(response.resp_body)["dispatch_id"] == "exact"
    assert {:ok, %{turn_seq: 0}} = SessionStore.get(ctx.store, ctx.first.session_id)
  end

  test "session capability view supplies the existing identity used by factory preflight", ctx do
    path = "/v1/sessions/#{ctx.first.session_id}"
    response = request(:get, path, ctx.first.token)
    assert response.status == 200
    view = Jason.decode!(response.resp_body)
    assert view["session_id"] == ctx.first.session_id
    assert view["lineage_id"] == ctx.first.lineage_id
    assert view["generation"] == 0
    assert view["expires_at"] == 100_000
    payload = Jason.encode!(%{generation: view["generation"], deadline: view["expires_at"], task_deadline: 100_000})
    assert request(:post, path <> "/residency", ctx.first.token, payload, [{"idempotency-key", "from-view"}]).status == 202
  end
end
