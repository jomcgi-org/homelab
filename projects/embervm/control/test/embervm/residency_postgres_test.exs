defmodule Embervm.ResidencyPostgresTest do
  use ExUnit.Case, async: false
  alias Embervm.{ResidencyIntent, SessionStore}
  alias Embervm.OpLog.Postgres

  @dsn System.get_env("EMBERVM_TEST_POSTGRES_DSN")
  @moduletag skip: is_nil(@dsn)

  test "real Postgres atomically projects enqueue and handoff, reloads after restart, and retains dedupe" do
    {:ok, log} = Postgres.start_link(name: nil, dsn: @dsn)
    clock = fn -> 1_000_000 end
    opts = [name: nil, op_log: log, op_log_mod: Postgres, clock: clock]
    {:ok, store} = SessionStore.start_link(opts)
    key = "pg-#{System.unique_integer([:positive])}"
    {:ok, created} = SessionStore.create(store, %{tenant: "test", principal: key, workload: "w",
      node_id: "node", vm_id: "vm", expires_at: 4_000_000})
    attrs = %{"key" => key, "generation" => 0, "deadline" => 3_000_000,
      "task_deadline" => 3_000_000, "first_wait_at" => 1_000_000}
    limits = %{global: 100, principal: 100}
    {:ok, intent} = SessionStore.residency_intent(store, {:submit, created.session_id, attrs, limits})
    {:ok, row} = SessionStore.get(store, created.session_id)
    identity = ResidencyIntent.identity(row, "node/pod")
    {:ok, intent} = SessionStore.residency_intent(store, {:change, intent["id"], intent["version"],
      %{"state" => "ready", "identity" => identity, "residency_epoch" => "epoch"}})
    request = %{operation_id: intent["id"], epoch: "epoch"}
    assert {:ok, %{turn_seq: 1}} = SessionStore.record_residency_handoff(store, created.session_id, key, request, identity)
    GenServer.stop(store)
    GenServer.stop(log)
    {:ok, log} = Postgres.start_link(name: nil, dsn: @dsn)
    {:ok, store} = SessionStore.start_link(Keyword.put(opts, :op_log, log))
    assert {:ok, %{"state" => "consumed", "dispatch_id" => ^key, "restore" => nil}} = SessionStore.residency_intent(store, {:submit, created.session_id, attrs, limits})
    assert {:error, :already_consumed} = SessionStore.record_residency_handoff(store, created.session_id, key, request, identity)
    assert {:ok, %{turn_seq: 1, inflight_dispatch_id: ^key}} = SessionStore.get(store, created.session_id)
    assert {:ok, _} = Postgres.compact(log, 100_000_000_000)
    assert {:ok, records} = Postgres.load_residency_intents(log)
    assert Enum.any?(records, &(&1["id"] == intent["id"]))
    GenServer.stop(store)
    GenServer.stop(log)
  end
end
