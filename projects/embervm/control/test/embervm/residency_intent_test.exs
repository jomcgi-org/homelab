defmodule Embervm.ResidencyIntentTest do
  use ExUnit.Case, async: true
  alias Embervm.{ResidencyIntent, SessionStore}
  alias Embervm.OpLog.SQLite

  setup do
    path = Path.join(System.tmp_dir!(), "residency-#{System.unique_integer([:positive])}.db")
    on_exit(fn -> File.rm_rf!(path) end)
    {:ok, clock} = Agent.start_link(fn -> 1000 end)
    {:ok, log} = SQLite.start_link(name: nil, path: path)
    opts = [name: nil, op_log: log, clock: fn -> Agent.get(clock, & &1) end]
    {:ok, store} = SessionStore.start_link(opts)
    {:ok, session} = SessionStore.create(store, %{tenant: "t", principal: "p", workload: "w",
      node_id: "node", vm_id: "vm", base_snapshot_ref: "base", base_digest: "digest", expires_at: 1_000_000})
    attrs = %{"key" => "turn-1", "generation" => 0, "deadline" => 800_000, "task_deadline" => 900_000, "first_wait_at" => 1000}
    %{path: path, log: log, store: store, opts: opts, clock: clock, session: session, attrs: attrs}
  end

  defp submit(ctx, attrs \\ nil) do
    SessionStore.residency_intent(ctx.store, {:submit, ctx.session.session_id, attrs || ctx.attrs, %{global: 10, principal: 10}})
  end

  defp ready(ctx, intent) do
    {:ok, session} = SessionStore.get(ctx.store, ctx.session.session_id)
    identity = ResidencyIntent.identity(session, "node/pod-a")
    {:ok, intent} = SessionStore.residency_intent(ctx.store, {:change, intent["id"], intent["version"],
      %{"state" => "ready", "identity" => identity, "residency_epoch" => "epoch-a"}})
    {intent, identity}
  end

  test "simultaneous same-key enqueue commits once; replay survives a wait and restart", ctx do
    results = 1..12 |> Task.async_stream(fn _ -> submit(ctx) end) |> Enum.map(fn {:ok, {:ok, intent}} -> intent end)
    assert length(Enum.uniq_by(results, & &1["id"])) == 1
    [intent | _] = results
    Agent.update(ctx.clock, &(&1 + 240_000))
    GenServer.stop(ctx.store)
    {:ok, store} = SessionStore.start_link(ctx.opts)
    assert {:ok, ^intent} = submit(%{ctx | store: store})
    assert {:ok, %{invoke_started_at: nil, turn_seq: 0}} = SessionStore.get(store, ctx.session.session_id)
    assert {:ok, ops} = SQLite.read_from(ctx.log, 0)
    assert Enum.count(ops, &(&1.kind == :residency_changed)) == 1
    refute Enum.any?(ops, &(&1.kind == :session_invoke_started))
  end

  test "key reuse conflicts, even with a different session owned by the same principal", ctx do
    assert {:ok, _} = submit(ctx)
    assert {:error, :idempotency_conflict} = submit(ctx, Map.put(ctx.attrs, "deadline", 700_000))
    {:ok, other} = SessionStore.create(ctx.store, %{tenant: "t", principal: "p", workload: "w", node_id: "n", vm_id: "v", expires_at: 1_000_000})
    assert {:error, :idempotency_conflict} = submit(%{ctx | session: other})
  end

  test "invalid later deadlines and generations cannot extend a lifetime", ctx do
    for attrs <- [Map.put(ctx.attrs, "deadline", 1_100_000), Map.put(ctx.attrs, "task_deadline", 5000), Map.put(ctx.attrs, "generation", 1)] do
      assert {:error, _} = submit(ctx, attrs)
    end
    assert {:ok, []} = SQLite.load_residency_intents(ctx.log)
  end

  test "cancellation wins before handoff and cannot invoke a guest", ctx do
    {:ok, initial} = submit(ctx)
    {intent, identity} = ready(ctx, initial)
    assert {:ok, %{"state" => "cancelled"}} = SessionStore.residency_intent(ctx.store, {:cancel, ctx.session.session_id, intent["id"]})
    assert {:error, {:not_dispatched, _, "dispatch-1", "cancelled"}} =
      SessionStore.record_residency_handoff(ctx.store, ctx.session.session_id, "dispatch-1", %{operation_id: intent["id"], epoch: "epoch-a"}, identity)
    assert {:ok, %{turn_seq: 0, invoke_started_at: nil}} = SessionStore.get(ctx.store, ctx.session.session_id)
  end

  test "winning handoff and invoke stamp are one durable fact, never replayed", ctx do
    {:ok, initial} = submit(ctx)
    {intent, identity} = ready(ctx, initial)
    request = %{operation_id: intent["id"], epoch: "epoch-a"}
    assert {:ok, %{turn_seq: 1, inflight_dispatch_id: "dispatch-1"}} = SessionStore.record_residency_handoff(ctx.store, ctx.session.session_id, "dispatch-1", request, identity)
    GenServer.stop(ctx.store)
    {:ok, store} = SessionStore.start_link(ctx.opts)
    assert {:error, :already_consumed} = SessionStore.record_residency_handoff(store, ctx.session.session_id, "dispatch-1", request, identity)
    assert {:error, :already_consumed} = SessionStore.residency_intent(store, {:cancel, ctx.session.session_id, intent["id"]})
    assert {:ok, [%{"state" => "consumed", "dispatch_id" => "dispatch-1"}]} = SQLite.load_residency_intents(ctx.log)
    assert {:ok, [%{turn_seq: 1, inflight_dispatch_id: "dispatch-1"}]} = SQLite.load_sessions(ctx.log)
  end

  test "stale incarnation, bank and expiry all produce exact negative proof", ctx do
    for {key, change} <- [{"incarnation", :identity}, {"bank", :bank}, {"expiry", :expiry}] do
      {:ok, initial} = submit(ctx, Map.put(ctx.attrs, "key", key))
      {intent, identity} = ready(ctx, initial)
      identity = if change == :identity, do: Map.put(identity, "dial_id", "node/pod-b"), else: identity
      if change == :bank, do: SessionStore.adopt_state(ctx.store, ctx.session.session_id, :banked)
      if change == :expiry, do: Agent.update(ctx.clock, fn _ -> 800_000 end)
      assert {:error, {:not_dispatched, _, ^key, _}} = SessionStore.record_residency_handoff(ctx.store, ctx.session.session_id, key, %{operation_id: intent["id"], epoch: "epoch-a"}, identity)
      SessionStore.adopt_state(ctx.store, ctx.session.session_id, :running)
    end
    assert {:ok, %{turn_seq: 0}} = SessionStore.get(ctx.store, ctx.session.session_id)
  end

  test "old observations cannot undo cancellation or a newer restore epoch", ctx do
    {:ok, intent} = submit(ctx)
    {:ok, admitting} = SessionStore.residency_intent(ctx.store, {:change, intent["id"], 0, %{"state" => "admitting", "attempt_epoch" => 1}})
    {:ok, cancelling} = SessionStore.residency_intent(ctx.store, {:cancel, ctx.session.session_id, intent["id"]})
    assert cancelling["state"] == "cancelling"
    assert {:error, :stale_residency_observation} = SessionStore.residency_intent(ctx.store, {:change, intent["id"], admitting["version"], %{"state" => "ready"}})
    assert {:ok, ^cancelling} = SessionStore.residency_intent(ctx.store, {:get, ctx.session.session_id, intent["id"]})
  end

  test "queue bounds, pagination and session isolation", ctx do
    {:ok, intent} = submit(ctx)
    assert {:error, :residency_queue_full} = SessionStore.residency_intent(ctx.store, {:submit, ctx.session.session_id, Map.put(ctx.attrs, "key", "next"), %{global: 1, principal: 1}})
    {:ok, other} = SessionStore.create(ctx.store, %{tenant: "t", principal: "other", workload: "w", node_id: "n", vm_id: "v", expires_at: 1_000_000})
    assert {:error, :not_found} = SessionStore.residency_intent(ctx.store, {:get, other.session_id, intent["id"]})
    assert {:error, :not_found} = SessionStore.residency_intent(ctx.store, {:cancel, other.session_id, intent["id"]})
    assert {:ok, %{items: [], total: 1}} = SessionStore.residency_intent(ctx.store, {:list, ctx.session.session_id, 1, 1})
    assert {:ok, %{items: [^intent]}} = SessionStore.residency_intent(ctx.store, {:list, ctx.session.session_id, 1, 0})
    refute inspect(intent) =~ ctx.session.token
  end

  test "gate rollback retains replay and cancellation", ctx do
    {:ok, intent} = submit(ctx)
    limits = %{global: 1, principal: 1, enabled: false}
    assert {:ok, ^intent} = SessionStore.residency_intent(ctx.store, {:submit, ctx.session.session_id, ctx.attrs, limits})
    assert {:error, :async_residency_unsupported} = SessionStore.residency_intent(ctx.store, {:submit, ctx.session.session_id, Map.put(ctx.attrs, "key", "new"), limits})
    assert {:ok, %{"state" => "cancelled"}} = SessionStore.residency_intent(ctx.store, {:cancel, ctx.session.session_id, intent["id"]})
  end

  test "fair rotation preserves FIFO age while scanning beyond oversized heads" do
    intents = for {principal, time, id} <- [{"a", 1, "big"}, {"a", 2, "small"}, {"b", 3, "b1"}, {"b", 4, "b2"}] do
      %{"id" => id, "principal" => principal, "state" => "queued", "first_wait_at" => time, "created_at" => time}
    end
    assert Enum.map(ResidencyIntent.candidates(intents, nil, 4), & &1["id"]) == ["big", "b1", "small", "b2"]
    assert Enum.map(ResidencyIntent.candidates(intents, "a", 3), & &1["id"]) == ["b1", "big", "b2"]
    assert hd(intents)["first_wait_at"] == 1
  end

  test "one successful exact handoff delivers exactly one guest RPC", ctx do
    test_pid = self()
    {:ok, intent} = submit(ctx)
    {intent, _identity} = ready(ctx, intent)
    {:ok, process} = Embervm.Session.start_link(session_id: ctx.session.session_id, workload: "w", principal: "p",
      node_id: "node", dial_id: "node/pod-a", vm_id: "vm", queue_cap: 4, session_store: ctx.store,
      channel_fun: fn _ -> {:ok, :channel} end,
      assign_fun: fn _, _ ->
        send(test_pid, :guest_delivery)
        {:ok, %Embervm.Node.V1.SessionAssignResponse{response: %Embervm.Node.V1.GuestResponse{status_code: 200, body: "ok"}}}
      end)
    req = %{method: "POST", path: "/invoke", headers: %{}, body: "{}", dispatch_id: "exact",
      residency: %{operation_id: intent["id"], epoch: "epoch-a"}}
    assert {:ok, _} = Embervm.Session.invoke(process, req)
    assert_receive :guest_delivery
    assert {:error, {:invoke_start_not_recorded, :already_consumed}} = Embervm.Session.invoke(process, req)
    refute_receive :guest_delivery
  end
  defmodule LostReplyLog do
    def load_sessions({log, _}), do: SQLite.load_sessions(log)
    def load_residency_intents({log, _}), do: SQLite.load_residency_intents(log)
    def append({log, mode}, op) do
      case Agent.get_and_update(mode, fn value -> {value, :ok} end) do
        :reject -> {:error, :unavailable}
        :lose ->
          {:ok, _} = SQLite.append(log, op)
          {:error, :unavailable}
        :ok -> SQLite.append(log, op)
      end
    end
  end

  test "storage rejection does not enqueue and a lost commit reply reattaches without a second dispatch", ctx do
    GenServer.stop(ctx.store)
    {:ok, mode} = Agent.start_link(fn -> :reject end)
    opts = ctx.opts |> Keyword.put(:op_log, {ctx.log, mode}) |> Keyword.put(:op_log_mod, LostReplyLog)
    {:ok, store} = SessionStore.start_link(opts)
    ctx = %{ctx | store: store}
    assert {:error, :unavailable} = submit(ctx)
    assert {:ok, []} = SQLite.load_residency_intents(ctx.log)
    Agent.update(mode, fn _ -> :lose end)
    assert {:error, :unavailable} = submit(ctx)
    assert {:ok, [committed]} = SQLite.load_residency_intents(ctx.log)
    assert {:ok, ^committed} = submit(ctx)
    {intent, identity} = ready(ctx, committed)
    Agent.update(mode, fn _ -> :lose end)
    request = %{operation_id: intent["id"], epoch: "epoch-a"}
    assert {:error, :unavailable} = SessionStore.record_residency_handoff(store, ctx.session.session_id, "once", request, identity)
    assert {:error, :already_consumed} = SessionStore.record_residency_handoff(store, ctx.session.session_id, "once", request, identity)
    assert {:ok, %{turn_seq: 1, inflight_dispatch_id: "once"}} = SessionStore.get(store, ctx.session.session_id)
  end

  test "terminal history is excluded from the bounded working set but remains replayable", ctx do
    for i <- 1..120 do
      attrs = Map.put(ctx.attrs, "key", "history-#{i}")
      {:ok, intent} = SessionStore.residency_intent(ctx.store, {:submit, ctx.session.session_id, attrs, %{global: 2, principal: 2}})
      {:ok, _} = SessionStore.residency_intent(ctx.store, {:cancel, ctx.session.session_id, intent["id"]})
    end
    {:ok, current} = submit(ctx)
    assert {:ok, [^current]} = SessionStore.residency_intent(ctx.store, :working)
    assert {:ok, %{"state" => "cancelled"}} = SessionStore.residency_intent(ctx.store, {:submit, ctx.session.session_id, Map.put(ctx.attrs, "key", "history-1"), %{global: 2, principal: 2}})
    assert {:ok, %{total: 121, items: page}} = SessionStore.residency_intent(ctx.store, {:list, ctx.session.session_id, 100, 0})
    assert length(page) == 100
  end

end
