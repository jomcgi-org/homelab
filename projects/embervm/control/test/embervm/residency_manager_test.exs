defmodule Embervm.ResidencyManagerTest do
  use ExUnit.Case, async: false
  alias Embervm.{NodeCapacity, SessionManager, SessionStore, WorkloadCatalog}
  alias Embervm.Scheduler.Inflight
  alias Embervm.OpLog.SQLite
  alias Embervm.Node.V1.{PrimeResponse, RestoreArtifactResponse}

  defp stack(overrides \\ []) do
    n = System.unique_integer([:positive])
    path = Path.join(System.tmp_dir!(), "residency-manager-#{n}.db")
    on_exit(fn -> File.rm_rf!(path) end)
    {:ok, clock} = Agent.start_link(fn -> 1_000_000 end)
    clock_fun = fn -> Agent.get(clock, & &1) end
    {:ok, log} = SQLite.start_link(name: nil, path: path)
    store_opts = [name: nil, op_log: log, clock: clock_fun]
    {:ok, store} = SessionStore.start_link(store_opts)
    registry = :"residency_registry_#{n}"
    {:ok, _} = Registry.start_link(keys: :unique, name: registry)
    {:ok, sup} = DynamicSupervisor.start_link(strategy: :one_for_one)
    cap = :"residency_capacity_#{n}"
    cat = :"residency_catalog_#{n}"
    inflight = :"residency_inflight_#{n}"
    NodeCapacity.create(cap)
    WorkloadCatalog.create(cat)
    Inflight.create(inflight)
    WorkloadCatalog.upsert(cat, "w", %{name: "w", class: "session", mem_mib: 512, vcpus: 1,
      cap: 2, floor: 0, invoke_path: "/", timeout_ms: 90_000,
      persistence: %{memory: false, filesystem: %{enabled: true, size_bytes: 1_000_000}},
      session: %{idle_bank_seconds: 300, max_lifetime_seconds: 3600, banked_ttl_seconds: 3600,
        max_sessions: 16, invoke_queue_cap: 4}})
    test_pid = self()
    mgr_opts = [name: nil, session_store: store, op_log: log, supervisor: sup, registry: registry,
      capacity_table: cap, catalog_table: cat, inflight_table: inflight, clock: clock_fun,
      monotonic_clock: clock_fun, wake_max: 100, residency_restore_limit: 2,
      reconcile_interval_ms: 0, sweep_interval_ms: 0, expected_instances_fun: fn -> %{} end,
      brick_status_fun: fn ids -> Map.new(ids, &{&1, %{registered: true}}) end,
      channel_fun: fn _ -> {:ok, :channel} end,
      reserve_session_vm_fun: fn _, _ -> :ok end,
      commit_session_vm_fun: fn _, _ -> :ok end,
      restore_artifact_fun: fn _, _ -> {:ok, %RestoreArtifactResponse{}} end,
      prime_fun: fn _, request ->
        send(test_pid, {:prime, self(), request})
        receive do {:finish, result} -> result end
      end,
      session_opts: [channel_fun: fn _ -> {:ok, :channel} end,
        assign_fun: fn _, _ -> send(test_pid, :unexpected_invoke); {:error, :unexpected} end]]
    mgr_opts = Keyword.merge(mgr_opts, overrides)
    ctx = %{clock: clock, clock_fun: clock_fun, log: log, store: store, store_opts: store_opts,
      mgr_opts: mgr_opts, registry: registry, sup: sup, cap: cap, cat: cat, inflight: inflight}
    report(ctx, [])
    {:ok, mgr} = SessionManager.start_link(mgr_opts)
    Map.put(ctx, :mgr, mgr)
  end

  defp report(ctx, vm_ids) do
    NodeCapacity.put(ctx.cap, "node/pod", %{node_id: "node", configured_id: "node", instance_id: "node/pod",
      size_class: "two", admits_on_reservation: true, mem_budget_mib: 1152,
      mem_reserved_mib: 512 * length(vm_ids), vm_overhead_mib: 64,
      live_vms: length(vm_ids), max_live_vms: 2, draining: false,
      updated_at: ctx.clock_fun.(), observed_at_unix_ms: ctx.clock_fun.(),
      session_vms: [], session_snapshots: [], session_volumes: [],
      workloads: %{"w" => %{snapshot_ref: "base", base_state: :BASE_BUILD_STATE_READY,
        primed_vm_ids: vm_ids, free_primed_slots: 0}}})
  end

  defp enqueue(ctx, key, principal \\ "p") do
    {:ok, created} = SessionStore.create(ctx.store, %{tenant: "t", principal: principal, workload: "w",
      node_id: "node", volume_node_id: "node", vm_id: "old-#{key}", expires_at: 4_000_000})
    {:ok, _} = SessionStore.mark(ctx.store, created.session_id, :park)
    {:ok, _} = SessionStore.transition(ctx.store, created.session_id, :park_complete, :session_parked, %{volume_node_id: "node"}, %{})
    {:ok, intent} = SessionStore.residency_intent(ctx.store, {:submit, created.session_id,
      %{"key" => key, "generation" => 0, "deadline" => 3_000_000, "task_deadline" => 3_000_000,
        "first_wait_at" => ctx.clock_fun.()}, %{global: 100, principal: 100}})
    intent
  end

  defp tick(ctx) do
    send(ctx.mgr, :residency_tick)
    :sys.get_state(ctx.mgr)
  end

  defp intents(ctx) do
    {:ok, %{items: rows}} = SessionStore.residency_intent(ctx.store, {:list, nil, 100, 0})
    rows
  end

  defp await(fun, tries \\ 200)
  defp await(fun, 0), do: assert(fun.())
  defp await(fun, tries) do
    if fun.(), do: :ok, else: (Process.sleep(5); await(fun, tries - 1))
  end

  test "ten parked sessions progress through two declared-memory slots after a long wait and restart" do
    ctx = stack()
    report(ctx, ["unrelated-1", "unrelated-2"])
    original = for i <- 1..10, do: enqueue(ctx, "key-#{i}", if(rem(i, 2) == 0, do: "a", else: "b"))
    tick(ctx)
    refute_receive {:prime, _, _}
    assert Enum.all?(intents(ctx), &(&1["state"] == "queued"))
    assert Inflight.pending_mib("node/pod", table: ctx.inflight) == 0
    Agent.update(ctx.clock, &(&1 + 240_000))
    GenServer.stop(ctx.mgr)
    GenServer.stop(ctx.store)
    {:ok, store} = SessionStore.start_link(ctx.store_opts)
    opts = Keyword.put(ctx.mgr_opts, :session_store, store)
    {:ok, mgr} = SessionManager.start_link(opts)
    ctx = %{ctx | mgr: mgr, store: store}
    assert Enum.sort_by(intents(ctx), & &1["id"]) == Enum.sort_by(original, & &1["id"])
    report(ctx, [])

    for round <- 1..5 do
      tick(ctx)
      assert_receive {:prime, first, _}
      assert_receive {:prime, second, _}
      refute_receive {:prime, _, _}
      assert Inflight.pending_mib("node/pod", table: ctx.inflight) == 1152
      assert Enum.count(intents(ctx), &(&1["state"] == "admitting")) == 2
      send(first, {:finish, {:ok, %PrimeResponse{vm_id: "vm-#{round}-1"}}})
      send(second, {:finish, {:ok, %PrimeResponse{vm_id: "vm-#{round}-2"}}})
      await(fn -> Enum.count(intents(ctx), &(&1["state"] == "ready")) == 2 end)
      report(ctx, ["vm-#{round}-1", "vm-#{round}-2"])
      tick(ctx)
      assert Inflight.pending_mib("node/pod", table: ctx.inflight) == 0
      assert {:ok, [%{mem_reserved_mib: 1024}]} = {:ok, NodeCapacity.all(ctx.cap)}
      # Complete interest, then simulate the existing confirmed park boundary.
      # DELETE itself does not kill either VM.
      for intent <- intents(ctx), intent["state"] == "ready" do
        assert {:ok, %{"state" => "cancelled"}} = SessionStore.residency_intent(ctx.store, {:cancel, intent["session_id"], intent["id"]})
        assert Registry.lookup(ctx.registry, intent["session_id"]) != []
        [{pid, _}] = Registry.lookup(ctx.registry, intent["session_id"])
        DynamicSupervisor.terminate_child(ctx.sup, pid)
        {:ok, _} = SessionStore.mark(ctx.store, intent["session_id"], :park)
        {:ok, _} = SessionStore.transition(ctx.store, intent["session_id"], :park_complete, :session_parked, %{volume_node_id: "node"}, %{})
      end
      report(ctx, [])
    end
    assert Enum.count(intents(ctx), &(&1["state"] == "cancelled")) == 10
    assert Enum.all?(SessionStore.all(ctx.store), &(&1.turn_seq == 0 and is_nil(&1.invoke_started_at)))
    refute_receive :unexpected_invoke
  end

  test "lost restore response stays charged beyond lease expiry, coalesces and survives restart" do
    ctx = stack()
    intent = enqueue(ctx, "leader")
    {:ok, waiter} = SessionStore.residency_intent(ctx.store, {:submit, intent["session_id"],
      %{"key" => "waiter", "generation" => 0, "deadline" => 3_000_000, "task_deadline" => 3_000_000}, %{global: 100, principal: 100}})
    tick(ctx)
    assert_receive {:prime, worker, _}
    refute_receive {:prime, _, _}
    assert Enum.count(intents(ctx), &(&1["state"] == "admitting")) == 2
    send(worker, {:finish, {:error, %GRPC.RPCError{status: 4, message: "lost reply"}}})
    assert {:ok, %{"state" => "cancelling"}} = SessionStore.residency_intent(ctx.store, {:cancel, intent["session_id"], intent["id"]})
    Agent.update(ctx.clock, &(&1 + 500_000))
    tick(ctx)
    assert Inflight.pending_mib("node/pod", table: ctx.inflight, now_ms: System.monotonic_time(:millisecond) + 1_000_000) == 576
    GenServer.stop(ctx.mgr)
    :ets.delete_all_objects(ctx.inflight)
    {:ok, mgr} = SessionManager.start_link(ctx.mgr_opts)
    ctx = %{ctx | mgr: mgr}
    tick(ctx)
    assert Inflight.pending_mib("node/pod", table: ctx.inflight) == 576
    assert {:ok, %{"state" => "admitting"}} = SessionStore.residency_intent(ctx.store, {:get, intent["session_id"], waiter["id"]})
    refute_receive {:prime, _, _}
  end
  for commit_running <- [false, true] do
  @commit_running commit_running
  test "acknowledged exact Prime is adopted after a crash, running committed: #{commit_running}" do
    ctx = stack()
    intent = enqueue(ctx, "restart-after-prime")
    tick(ctx)
    assert_receive {:prime, worker, _}
    {:ok, admitting} = SessionStore.residency_intent(ctx.store, {:get, intent["session_id"], intent["id"]})
    restore = Map.put(admitting["restore"], "vm_id", "known-vm")
    {:ok, _} = SessionStore.residency_intent(ctx.store, {:change, intent["id"], admitting["version"], %{"restore" => restore}})
    if @commit_running do
      updates = %{node_id: "node", vm_id: "known-vm", volume_node_id: "node"}
      {:ok, _} = SessionStore.transition(ctx.store, intent["session_id"], :rejoin_ready, :session_rejoined, updates, updates)
    end
    assert Registry.lookup(ctx.registry, intent["session_id"]) == []
    GenServer.stop(ctx.mgr)
    GenServer.stop(ctx.store)
    send(worker, {:finish, {:ok, %PrimeResponse{vm_id: "known-vm"}}})
    {:ok, store} = SessionStore.start_link(ctx.store_opts)
    report(ctx, ["known-vm"])
    {:ok, mgr} = SessionManager.start_link(Keyword.put(ctx.mgr_opts, :session_store, store))
    ctx = %{ctx | mgr: mgr, store: store}
    tick(ctx)
    await(fn -> Enum.any?(intents(ctx), &(&1["state"] == "ready")) end)
    assert {:ok, %{vm_id: "known-vm", state: :running, turn_seq: 0}} = SessionStore.get(store, intent["session_id"])
    assert Registry.lookup(ctx.registry, intent["session_id"]) != []
    refute_receive {:prime, _, _}
    tick(ctx)
    assert Inflight.pending_mib("node/pod", table: ctx.inflight) == 0
    refute_receive :unexpected_invoke
  end
  end

  test "stale capacity and no fitting class stay queued; expiry never renews the deadline" do
    ctx = stack()
    intent = enqueue(ctx, "no-fit")
    [fact] = NodeCapacity.all(ctx.cap)
    NodeCapacity.put(ctx.cap, "node/pod", %{fact | mem_budget_mib: 500})
    tick(ctx)
    assert {:ok, %{"state" => "queued", "reason" => "no_fitting_class"}} = SessionStore.residency_intent(ctx.store, {:get, intent["session_id"], intent["id"]})
    report(ctx, [])
    Agent.update(ctx.clock, &(&1 + 240_000))
    tick(ctx)
    assert {:ok, %{"state" => "queued", "reason" => "safe_memory_unavailable"}} = SessionStore.residency_intent(ctx.store, {:get, intent["session_id"], intent["id"]})
    Agent.update(ctx.clock, fn _ -> 3_000_000 end)
    report(ctx, [])
    tick(ctx)
    assert {:ok, %{"state" => "expired", "deadline" => 3_000_000}} = SessionStore.residency_intent(ctx.store, {:get, intent["session_id"], intent["id"]})
    assert Inflight.pending_mib("node/pod", table: ctx.inflight) == 0
    refute_receive {:prime, _, _}
  end


  test "known unissued dial failure releases capacity and can retry after repair" do
    ctx = stack(channel_fun: fn _ -> {:error, :unreachable} end)
    intent = enqueue(ctx, "dial-failure")
    tick(ctx)
    await(fn -> Enum.any?(intents(ctx), &(&1["reason"] == "restore_not_started")) end)
    assert Inflight.pending_mib("node/pod", table: ctx.inflight) == 0
    assert {:ok, %{"state" => "queued", "restore" => nil}} = SessionStore.residency_intent(ctx.store, {:get, intent["session_id"], intent["id"]})
    refute_receive {:prime, _, _}
    :sys.replace_state(ctx.mgr, &%{&1 | channel_fun: fn _ -> {:ok, :channel} end})
    tick(ctx)
    assert_receive {:prime, worker, _}
    send(worker, {:finish, {:ok, %PrimeResponse{vm_id: "after-repair"}}})
    await(fn -> Enum.any?(intents(ctx), &(&1["state"] == "ready")) end)
  end

  test "workspace authorization denial is permanent and never reserves a nonexistent VM" do
    ctx = stack(restore_artifact_fun: fn _, _ -> {:error, %GRPC.RPCError{status: 7, message: "denied"}} end)
    intent = enqueue(ctx, "denied-workspace")
    tick(ctx)
    await(fn -> Enum.any?(intents(ctx), &(&1["state"] == "failed")) end)
    assert {:ok, %{"reason" => "workspace_authorization_or_lineage", "restore" => nil}} = SessionStore.residency_intent(ctx.store, {:get, intent["session_id"], intent["id"]})
    assert Inflight.pending_mib("node/pod", table: ctx.inflight) == 0
    refute_receive {:prime, _, _}
    tick(ctx)
    refute_receive {:prime, _, _}
  end

  for confirmed_at_expiry <- [true, false] do
  @confirmed_at_expiry confirmed_at_expiry
  test "ordinary reconciliation and expiry retain physical claims until confirmed teardown: #{@confirmed_at_expiry}" do
    parent = self()
    {:ok, teardown} = Agent.start_link(fn -> @confirmed_at_expiry end)
    ctx = stack(channel_fun: fn dial -> {:ok, {:channel, dial}} end,
      retire_volume_fun: fn _, _ -> :ok end,
      session_opts: [channel_fun: fn _ -> {:ok, :channel} end,
        assign_fun: fn _, _ -> send(parent, :unexpected_invoke); {:error, :unexpected} end,
        destroy_fun: fn channel, vm_id ->
          send(parent, {:destroyed, channel, vm_id})
          {:ok, %{teardown_confirmed: Agent.get(teardown, & &1)}}
        end], relight_fun: fn _, _ ->
      send(parent, {:relight, self()})
      receive do {:finish, result} -> result end
    end)
    intent = enqueue(ctx, "banked-reconcile")
    :ok = SessionStore.adopt_state(ctx.store, intent["session_id"], :running)
    {:ok, _} = SessionStore.mark(ctx.store, intent["session_id"], :bank)
    {:ok, _} = SessionStore.transition(ctx.store, intent["session_id"], :bank_ready, :session_banked,
      %{snapshot_ref: "banked-snapshot", generation: 0}, %{snapshot_ref: "banked-snapshot"})
    [fact] = NodeCapacity.all(ctx.cap)
    snapshot = %{session_id: intent["session_id"], workload: "w", snapshot_ref: "banked-snapshot", size_bytes: 512, created_at_unix_ms: 0}
    NodeCapacity.put(ctx.cap, "node/pod", %{fact | session_snapshots: [snapshot]})
    tick(ctx)
    assert_receive {:relight, worker}
    SessionManager.reconcile(ctx.mgr)
    assert {:ok, %{state: :relighting}} = SessionStore.get(ctx.store, intent["session_id"])
    # Expiry removes demand while its exact physical outcome is unresolved.
    Agent.update(ctx.clock, fn _ -> 4_000_001 end)
    tick(ctx)
    SessionManager.reconcile(ctx.mgr)
    SessionManager.sweep(ctx.mgr)
    assert {:ok, %{state: :relighting}} = SessionStore.get(ctx.store, intent["session_id"])
    assert Inflight.pending_mib("node/pod", table: ctx.inflight) == 576
    send(worker, {:finish, {:ok, %Embervm.Node.V1.RelightResponse{vm_id: "late-vm"}}})
    await(fn -> Enum.any?(intents(ctx), &(&1["state"] == "expired")) end)
    assert {:ok, %{state: :running, vm_id: "late-vm", turn_seq: 0}} = SessionStore.get(ctx.store, intent["session_id"])
    # The late VM never appears in inventory before expiry destroys it.
    report(ctx, [])
    SessionManager.sweep(ctx.mgr)
    assert_receive {:destroyed, {:channel, "node/pod"}, "late-vm"}
    assert {:ok, %{state: :expired}} = SessionStore.get(ctx.store, intent["session_id"])
    expected = if @confirmed_at_expiry, do: 0, else: 576
    assert Inflight.pending_mib("node/pod", table: ctx.inflight) == expected
    GenServer.stop(ctx.mgr)
    GenServer.stop(ctx.store)
    :ets.delete_all_objects(ctx.inflight)
    Agent.update(teardown, fn _ -> true end)
    {:ok, store} = SessionStore.start_link(ctx.store_opts)
    {:ok, mgr} = SessionManager.start_link(Keyword.put(ctx.mgr_opts, :session_store, store))
    ctx = %{ctx | mgr: mgr, store: store}
    tick(ctx)
    await(fn -> Enum.any?(intents(ctx), &(&1["id"] == intent["id"] and &1["claim_reconciled"] == true)) end)
    assert Inflight.pending_mib("node/pod", table: ctx.inflight) == 0
    assert {:ok, %{"claim_reconciled" => true}} = SessionStore.residency_intent(store, {:get, intent["session_id"], intent["id"]})
    if not @confirmed_at_expiry, do: assert_receive({:destroyed, {:channel, "node/pod"}, "late-vm"})
    refute_receive {:prime, _, _}
    refute_receive :unexpected_invoke
  end
  end

  test "qualified cross-node recovery preserves verified workspace fingerprint" do
    parent = self()
    ctx = stack(cross_node_rejoin: true, restore_artifact_fun: fn _, req ->
      send(parent, {:workspace_request, req})
      {:ok, %RestoreArtifactResponse{content_fingerprint: req.expected_fingerprint}}
    end)
    intent = enqueue(ctx, "cross-node")
    {:ok, session} = SessionStore.get(ctx.store, intent["session_id"])
    captured = %{epoch: 0, volume_node_id: "node", lineage_id: session.lineage_id}
    send(ctx.mgr, {:workspace_exported, session.session_id, captured, {:ok, %{content_fingerprint: "verified-export"}}})
    :sys.get_state(ctx.mgr)
    [fact] = NodeCapacity.all(ctx.cap)
    :ets.delete_all_objects(ctx.cap)
    NodeCapacity.put(ctx.cap, "target/pod", Map.merge(fact, %{node_id: "target", configured_id: "target", instance_id: "target/pod", store_reachable: true}))
    tick(ctx)
    assert_receive {:workspace_request, %{expected_fingerprint: "verified-export"}}
    assert_receive {:prime, worker, _}
    assert Inflight.pending_mib("target/pod", table: ctx.inflight) == 576
    send(worker, {:finish, {:ok, %PrimeResponse{vm_id: "cross-node-vm"}}})
    await(fn -> Enum.any?(intents(ctx), &(&1["state"] == "ready")) end)
    assert {:ok, %{volume_node_id: "target", node_id: "target", turn_seq: 0}} = SessionStore.get(ctx.store, session.session_id)
    refute_receive :unexpected_invoke
  end
end
