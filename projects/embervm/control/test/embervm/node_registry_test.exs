defmodule Embervm.NodeRegistryTest do
  # Task 9 acceptance: the node registry consumes a daemon's WatchNode NodeStatus
  # stream into fail-closed capacity ETS, ages a silent node out (unknown@5s,
  # down@15s under an injected clock), reassigns a downed node's in-flight tasks,
  # and drops all facts the instant a daemon reports draining.
  #
  # Most cases drive the projection + age-out state machine synchronously through
  # the inject_status/1 + tick/1 seams with an injected clock, so there is no
  # real time or streamer process to race. One case exercises the real
  # spawn_monitor streamer path with an injected connect_fun/watch_fun (no real
  # gRPC; the wire-correctness of the stubs is covered by the Task 3 round-trip
  # test), proving connect -> emit -> ETS publish -> reconnect-on-drop.
  use ExUnit.Case, async: true

  alias Embervm.{NodeRegistry, StatefulStore, WorkloadCatalog}
  alias Embervm.OpLog.SQLite
  alias Embervm.Node.V1.{GroupMemberVm, NodeStatus, WorkloadCapacity}

  # -- helpers ---------------------------------------------------------------

  # A controllable monotonic clock backed by an Agent: the registry reads it for
  # every timestamp, and the test advances it to drive age-out deterministically.
  defp new_clock do
    {:ok, pid} = Agent.start_link(fn -> 0 end)
    on_exit(fn -> Embervm.TestProcess.stop_safely(pid) end)
    clock = fn -> Agent.get(pid, & &1) end
    advance = fn ms -> Agent.update(pid, &(&1 + ms)) end
    {clock, advance}
  end

  defp unique_table, do: :"nc_test_#{System.unique_integer([:positive])}"

  # Start a registry with test-friendly defaults; opts override any of them.
  defp start_registry(opts) do
    table = Keyword.get(opts, :table, unique_table())

    defaults = [
      name: nil,
      table: table,
      nodes: [%{id: "node-4", address: "node-4.test:9090"}],
      watch_startup: false,
      # No background registry re-push unless a test opts in (mirrors the manual
      # informer control the harness relies on).
      registry_resync_ms: 0
    ]

    {:ok, pid} = NodeRegistry.start_link(Keyword.merge(defaults, opts))
    on_exit(fn -> Embervm.TestProcess.stop_safely(pid) end)
    {pid, table}
  end

  defp node_status(opts \\ []) do
    %NodeStatus{
      node_id: Keyword.get(opts, :node_id, "node-4"),
      workloads:
        Keyword.get(opts, :workloads, [
          %WorkloadCapacity{
            workload: "echo",
            free_primed_slots: Keyword.get(opts, :free_primed_slots, 3),
            snapshot_ref: "snap-echo",
            base_state: :BASE_BUILD_STATE_READY
          }
        ]),
      mem_headroom_mib: 2048,
      cpu_headroom_millicores: Keyword.get(opts, :cpu_headroom_millicores, 0),
      live_vms: Keyword.get(opts, :live_vms, 0),
      max_live_vms: 8,
      mem_budget_mib: Keyword.get(opts, :mem_budget_mib, 3584),
      cpu_budget_millicores: Keyword.get(opts, :cpu_budget_millicores, 2000),
      draining: Keyword.get(opts, :draining, false),
      cpu_vendor: Keyword.get(opts, :cpu_vendor, ""),
      build_error: ""
    }
  end

  # `tries` is a poll count at 10ms intervals, so 200 is a 2s budget, not 200ms.
  # Waits here are bets on scheduler latency rather than properties of the code,
  # and a budget that is generous costs nothing on the passing path: the poll
  # returns as soon as the condition holds. Keep new waits at 200 rather than
  # picking a tighter number that only ever loses on a loaded executor.
  defp eventually(_fun, 0), do: flunk("condition never became true")

  defp eventually(fun, tries) do
    if fun.() do
      :ok
    else
      Process.sleep(10)
      eventually(fun, tries - 1)
    end
  end

  # -- capacity projection ---------------------------------------------------

  test "a healthy NodeStatus publishes dispatchable capacity facts" do
    {clock, _advance} = new_clock()
    {reg, table} = start_registry(clock: clock)

    :ok = NodeRegistry.inject_status(reg, "node-4", node_status(free_primed_slots: 3))

    assert [facts] = NodeRegistry.capacity(table)
    assert facts.node_id == "node-4"
    assert facts.workloads["echo"].free_primed_slots == 3
    assert facts.workloads["echo"].base_state == :BASE_BUILD_STATE_READY
    assert facts.max_live_vms == 8

    snapshot = NodeRegistry.status(reg)
    assert snapshot["node-4"].health == :healthy
    assert snapshot["node-4"].dispatchable
  end

  test "a group member's ACTIVATOR origin is retained in node facts for adoption" do
    {clock, _advance} = new_clock()
    {reg, table} = start_registry(clock: clock)

    status = %NodeStatus{
      node_status()
      | group_member_vms: [
          %GroupMemberVm{
            vm_id: "vm-relit",
            group_instance_id: "group-relit",
            member_name: "leader",
            ip: "10.101.0.10",
            healthy: true,
            origin: :INSTANCE_ORIGIN_ACTIVATOR
          }
        ]
    }

    :ok = NodeRegistry.inject_status(reg, "node-4", status)

    assert [%{group_member_vms: [%{origin: :INSTANCE_ORIGIN_ACTIVATOR}]}] = NodeRegistry.capacity(table)
  end

  test "registry projects the CPU-vendor fact (Bug B: restore-on-miss vendor keying)" do
    {clock, _advance} = new_clock()
    {reg, table} = start_registry(clock: clock)

    :ok = NodeRegistry.inject_status(reg, "node-4", node_status(cpu_vendor: "amd"))

    assert [facts] = NodeRegistry.capacity(table)
    assert facts.cpu_vendor == "amd"
  end

  test "registry projects budget facts (R0 PR-1: mem/cpu budget from the daemon's own cgroup)" do
    {clock, _advance} = new_clock()
    {reg, table} = start_registry(clock: clock)

    :ok =
      NodeRegistry.inject_status(
        reg,
        "node-4",
        node_status(mem_budget_mib: 3584, cpu_budget_millicores: 2000, cpu_headroom_millicores: 1500)
      )

    assert [facts] = NodeRegistry.capacity(table)
    assert facts.mem_budget_mib == 3584
    assert facts.cpu_budget_millicores == 2000
    assert facts.cpu_headroom_millicores == 1500
  end

  test "a draining NodeStatus stops new assignments immediately (fail-closed, zero facts)" do
    {clock, _advance} = new_clock()
    {reg, table} = start_registry(clock: clock)

    :ok = NodeRegistry.inject_status(reg, "node-4", node_status())
    assert [_facts] = NodeRegistry.capacity(table)

    # Draining must retract capacity the instant it is observed, even though the
    # stream is still healthy.
    :ok = NodeRegistry.inject_status(reg, "node-4", node_status(draining: true))
    assert NodeRegistry.capacity(table) == []

    snapshot = NodeRegistry.status(reg)
    assert snapshot["node-4"].health == :healthy
    assert snapshot["node-4"].draining
    refute snapshot["node-4"].dispatchable
  end

  # -- age-out + reassignment ------------------------------------------------

  test "capacity ages out: unknown at 5s (facts retracted), down at 15s (reassign fires once)" do
    {clock, advance} = new_clock()
    test_pid = self()
    reassign_fun = fn node_id -> send(test_pid, {:reassigned, node_id}) end
    {reg, table} = start_registry(clock: clock, reassign_fun: reassign_fun)

    :ok = NodeRegistry.inject_status(reg, "node-4", node_status())
    assert [_facts] = NodeRegistry.capacity(table)

    # 5s of silence -> unknown: facts retracted, but NOT yet a reassignment.
    advance.(5_000)
    :ok = NodeRegistry.tick(reg)
    assert NodeRegistry.capacity(table) == []
    assert NodeRegistry.status(reg)["node-4"].health == :unknown
    refute_receive {:reassigned, _}, 50

    # 15s of silence -> down: the reassignment path fires exactly once.
    advance.(10_000)
    :ok = NodeRegistry.tick(reg)
    assert NodeRegistry.status(reg)["node-4"].health == :down
    assert_receive {:reassigned, "node-4"}

    # A further tick while still down must not re-fire reassignment.
    advance.(5_000)
    :ok = NodeRegistry.tick(reg)
    refute_receive {:reassigned, _}, 50
  end

  test "a fresh NodeStatus after down recovers the node to healthy and republishes facts" do
    {clock, advance} = new_clock()
    test_pid = self()
    {reg, table} = start_registry(clock: clock, reassign_fun: fn id -> send(test_pid, {:reassigned, id}) end)

    :ok = NodeRegistry.inject_status(reg, "node-4", node_status())
    advance.(15_000)
    :ok = NodeRegistry.tick(reg)
    assert NodeRegistry.status(reg)["node-4"].health == :down
    assert_receive {:reassigned, "node-4"}

    # Daemon comes back: a new status re-marks the node healthy and republishes.
    :ok = NodeRegistry.inject_status(reg, "node-4", node_status(free_primed_slots: 5))
    assert [facts] = NodeRegistry.capacity(table)
    assert facts.workloads["echo"].free_primed_slots == 5
    assert NodeRegistry.status(reg)["node-4"].health == :healthy
  end

  test "a never-answering daemon ages starting -> unknown -> down from registry start" do
    {clock, advance} = new_clock()
    test_pid = self()
    {reg, table} = start_registry(clock: clock, reassign_fun: fn id -> send(test_pid, {:reassigned, id}) end)

    # No status ever injected. Starts :starting with no facts.
    assert NodeRegistry.capacity(table) == []
    assert NodeRegistry.status(reg)["node-4"].health == :starting

    advance.(5_000)
    :ok = NodeRegistry.tick(reg)
    assert NodeRegistry.status(reg)["node-4"].health == :unknown

    advance.(10_000)
    :ok = NodeRegistry.tick(reg)
    assert NodeRegistry.status(reg)["node-4"].health == :down
    assert_receive {:reassigned, "node-4"}
  end

  # -- real streamer wiring --------------------------------------------------

  test "a real spawn_monitor streamer connects, publishes facts, and reconnects on clean drop" do
    {:ok, connects} = Agent.start_link(fn -> 0 end)
    on_exit(fn -> Embervm.TestProcess.stop_safely(connects) end)

    connect_fun = fn _address ->
      Agent.update(connects, &(&1 + 1))
      {:ok, :fake_channel}
    end

    # Emit one status, then close cleanly so the registry reconnects (a second
    # connect). Real (wall) clock here so watch/backoff timers behave normally.
    watch_fun = fn :fake_channel, node_id, emit ->
      emit.(node_status(node_id: node_id))
      {:ok, :closed}
    end

    {_reg, table} =
      start_registry(
        watch_startup: true,
        connect_fun: connect_fun,
        watch_fun: watch_fun,
        # Stub the registry replay: the real default dials NodeService.Stub over
        # the fake channel, which is not a real gRPC channel. The replay is not
        # under test here (see the dedicated reconnect-replays-registry test).
        sync_registry_fun: fn :fake_channel, _id, _request -> :ok end,
        disconnect_fun: fn :fake_channel -> :ok end,
        # Small backoff so the reconnect fires quickly; large age-out so the
        # real-time ticks do not race the assertions.
        base_backoff_ms: 10,
        max_backoff_ms: 10,
        age_check_ms: 60_000,
        unknown_after_ms: 60_000,
        down_after_ms: 60_000
      )

    # The streamer connected and its emitted status reached the capacity table.
    eventually(fn -> NodeRegistry.capacity(table) != [] end, 100)
    assert [facts] = NodeRegistry.capacity(table)
    assert facts.node_id == "node-4"

    # The clean close drove a reconnect: connect_fun was called at least twice.
    eventually(fn -> Agent.get(connects, & &1) >= 2 end, 100)
  end

  test "a wedged real streamer (emit then silent) ages to down, reassigns, kills, and reconnects" do
    {:ok, connects} = Agent.start_link(fn -> 0 end)
    on_exit(fn -> Embervm.TestProcess.stop_safely(connects) end)
    test_pid = self()

    connect_fun = fn _address ->
      Agent.update(connects, &(&1 + 1))
      {:ok, :fake_channel}
    end

    # Emit one status, then block forever: the stream stays OPEN with no further
    # data (a silent wedge), which the blocking-consumer cannot observe. Only the
    # owner's age-out timer can catch it.
    watch_fun = fn :fake_channel, node_id, emit ->
      emit.(node_status(node_id: node_id))

      receive do
        :never -> {:ok, :closed}
      end
    end

    {_reg, table} =
      start_registry(
        watch_startup: true,
        connect_fun: connect_fun,
        watch_fun: watch_fun,
        # Stub the registry replay: the real default dials NodeService.Stub over
        # the fake channel (not a real gRPC channel); the replay is not under test.
        sync_registry_fun: fn :fake_channel, _id, _request -> :ok end,
        disconnect_fun: fn :fake_channel -> :ok end,
        reassign_fun: fn id -> send(test_pid, {:reassigned, id}) end,
        # Small age-out windows and backoff so the wedge is detected and the kill
        # + reconnect happen quickly, with plenty of polling margin below.
        unknown_after_ms: 30,
        down_after_ms: 60,
        age_check_ms: 15,
        base_backoff_ms: 10,
        max_backoff_ms: 10
      )

    # The first (soon-wedged) streamer connected and published its status.
    eventually(fn -> NodeRegistry.capacity(table) != [] end, 200)

    # The age-out timer catches the silent wedge and fires the reassignment path,
    # even though the streamer process is alive and blocked (not crashed).
    assert_receive {:reassigned, "node-4"}, 2_000

    # The wedged streamer was killed and a fresh connection opened (recovery).
    eventually(fn -> Agent.get(connects, & &1) >= 2 end, 200)
  end

  # -- registry replay on (re)connect (artifact-decoupling Phase 2) ----------

  test "pushes SyncRegistry on every (re)connect, before consuming the watch stream" do
    {:ok, syncs} = Agent.start_link(fn -> [] end)
    on_exit(fn -> Embervm.TestProcess.stop_safely(syncs) end)
    test_pid = self()

    # Record each replay (channel, node_id) and let the streamer proceed. The
    # sync_registry_fun is the seam the production default reads the catalog and
    # calls SyncRegistry through; here we just assert it fires on each connect.
    sync_registry_fun = fn :fake_channel, node_id, _request ->
      Agent.update(syncs, &[node_id | &1])
      send(test_pid, {:replayed, node_id})
      :ok
    end

    connect_fun = fn _address -> {:ok, :fake_channel} end

    # Emit one status then close cleanly so the registry reconnects: a second
    # connect must trigger a SECOND replay (the re-converge on reconnect).
    watch_fun = fn :fake_channel, node_id, emit ->
      emit.(node_status(node_id: node_id))
      {:ok, :closed}
    end

    {_reg, _table} =
      start_registry(
        watch_startup: true,
        connect_fun: connect_fun,
        watch_fun: watch_fun,
        sync_registry_fun: sync_registry_fun,
        disconnect_fun: fn :fake_channel -> :ok end,
        base_backoff_ms: 10,
        max_backoff_ms: 10,
        age_check_ms: 60_000,
        unknown_after_ms: 60_000,
        down_after_ms: 60_000
      )

    # The first connect replayed the registry.
    assert_receive {:replayed, "node-4"}, 1_000
    # The clean-close reconnect replayed it AGAIN (re-converge on every connect).
    assert_receive {:replayed, "node-4"}, 1_000
    eventually(fn -> length(Agent.get(syncs, & &1)) >= 2 end, 200)
  end

  test "periodically re-pushes the registry to a still-connected daemon (ADR embervm/018 catalog-change convergence)" do
    {:ok, syncs} = Agent.start_link(fn -> 0 end)
    on_exit(fn -> Embervm.TestProcess.stop_safely(syncs) end)
    test_pid = self()

    sync_registry_fun = fn :fake_channel, node_id, _request ->
      n = Agent.get_and_update(syncs, fn c -> {c + 1, c + 1} end)
      send(test_pid, {:synced, node_id, n})
      :ok
    end

    # A watch that STAYS open (blocks), so the streamer never reconnects: any
    # re-push beyond the first connect-time one is the PERIODIC re-sync, which is
    # what propagates a catalog change (e.g. nodeLocalWake) to a connected daemon.
    watch_fun = fn :fake_channel, node_id, emit ->
      emit.(node_status(node_id: node_id))
      receive do: (:never -> {:ok, :closed})
    end

    {_reg, _table} =
      start_registry(
        watch_startup: true,
        connect_fun: fn _address -> {:ok, :fake_channel} end,
        watch_fun: watch_fun,
        sync_registry_fun: sync_registry_fun,
        disconnect_fun: fn :fake_channel -> :ok end,
        registry_resync_ms: 30,
        base_backoff_ms: 60_000,
        max_backoff_ms: 60_000,
        age_check_ms: 60_000,
        unknown_after_ms: 60_000,
        down_after_ms: 60_000
      )

    # The connect-time push fired once, then the periodic re-sync fires AGAIN and
    # AGAIN with no reconnect (the streamer is still blocked in watch_fun).
    assert_receive {:synced, "node-4", 1}, 1_000
    assert_receive {:synced, "node-4", 2}, 1_000
    assert_receive {:synced, "node-4", 3}, 1_000
  end

  test "a node-local-wake composite registry plan carries the group's ready budget" do
    workload = "registry-group-#{System.unique_integer([:positive])}"

    WorkloadCatalog.upsert(WorkloadCatalog.table(), workload, %{
      image_ref: "registry-group-image",
      vcpus: 2,
      mem_mib: 1024,
      group: %{
        node_local_wake: true,
        wake_timeout_seconds: 47,
        entry: %{member: "api", port: 8080, listen_port: 5410},
        members: [
          %{name: "api", start_order: 10, health_port: 8080, vcpus: 2, mem_mib: 1024},
          %{name: "worker", replicas: 2, start_order: 20, health_port: 9090, vcpus: 1, mem_mib: 512}
        ]
      }
    })

    on_exit(fn -> WorkloadCatalog.drop(WorkloadCatalog.table(), workload) end)
    test_pid = self()

    {_reg, _table} =
      start_registry(
        watch_startup: true,
        connect_fun: fn _address -> {:ok, :fake_channel} end,
        watch_fun: blocking_watch(),
        sync_registry_fun: fn :fake_channel, "node-4", request ->
          send(test_pid, {:registry_sync, request})
          :ok
        end,
        disconnect_fun: fn :fake_channel -> :ok end,
        age_check_ms: 60_000,
        unknown_after_ms: 60_000,
        down_after_ms: 60_000
      )

    assert_receive {:registry_sync, request}, 1_000
    entry = Enum.find(request.entries, &(&1.workload == workload))

    assert Enum.map(entry.group_member_plan, & &1.member_name) == ["api", "worker-0", "worker-1"]
    assert Enum.all?(entry.group_member_plan, &(&1.ready_budget_seconds == 47))
  end

  test "registry resync grants a lease for a stateful node-local-wake workload" do
    workload = "registry-stateful-lease-#{System.unique_integer([:positive])}"

    WorkloadCatalog.upsert(WorkloadCatalog.table(), workload, %{
      image_ref: "registry-stateful-lease-image",
      stateful: %{node_local_wake: true, listen_port: 5411, port: 8080, volume_mount_path: "/data"},
      serving: %{port: 8080, health_path: "/health"}
    })

    on_exit(fn -> WorkloadCatalog.drop(WorkloadCatalog.table(), workload) end)
    test_pid = self()

    {_reg, _table} =
      start_registry(
        watch_startup: true,
        connect_fun: fn _address -> {:ok, :fake_channel} end,
        watch_fun: blocking_watch(),
        sync_registry_fun: fn :fake_channel, "node-4", request ->
          send(test_pid, {:registry_sync, request})
          :ok
        end,
        disconnect_fun: fn :fake_channel -> :ok end,
        age_check_ms: 60_000,
        unknown_after_ms: 60_000,
        down_after_ms: 60_000
      )

    assert_receive {:registry_sync, request}, 1_000
    entry = Enum.find(request.entries, &(&1.workload == workload))
    assert entry.blessing_leases != []

    leases =
      StatefulStore.blessing_leases_for_node("node-4")
      |> Enum.filter(&(&1.workload_name == workload))

    assert [%{workload_name: ^workload, next_generation: next_generation, lease_end: lease_end}] = leases

    assert lease_end > next_generation
    {:ok, ops} = SQLite.read_from(Embervm.OpLog.SQLite, 0)
    assert Enum.any?(ops, &(&1.kind == :blessing_lease_granted and &1.workload == workload))
  end

  test "registry resync does not grant a lease for a serving-only node-local-wake workload" do
    workload = "registry-serving-lease-#{System.unique_integer([:positive])}"

    WorkloadCatalog.upsert(WorkloadCatalog.table(), workload, %{
      image_ref: "registry-serving-lease-image",
      serving: %{node_local_wake: true, port: 8080, health_path: "/health"}
    })

    on_exit(fn -> WorkloadCatalog.drop(WorkloadCatalog.table(), workload) end)
    test_pid = self()

    {_reg, _table} =
      start_registry(
        watch_startup: true,
        connect_fun: fn _address -> {:ok, :fake_channel} end,
        watch_fun: blocking_watch(),
        sync_registry_fun: fn :fake_channel, "node-4", request ->
          send(test_pid, {:registry_sync, request})
          :ok
        end,
        disconnect_fun: fn :fake_channel -> :ok end,
        age_check_ms: 60_000,
        unknown_after_ms: 60_000,
        down_after_ms: 60_000
      )

    assert_receive {:registry_sync, request}, 1_000
    entry = Enum.find(request.entries, &(&1.workload == workload))
    assert entry.blessing_leases == []
    refute Enum.any?(StatefulStore.blessing_leases_for_node("node-4"), &(&1.workload_name == workload))

    {:ok, ops} = SQLite.read_from(Embervm.OpLog.SQLite, 0)
    refute Enum.any?(ops, &(&1.kind == :blessing_lease_granted and &1.workload == workload))
  end

  test "SyncRegistry carries the configured control-plane activator IP, or empty when unset" do
    test_pid = self()

    sync_registry_fun = fn :fake_channel, node_id, request ->
      send(test_pid, {:registry_sync, node_id, request})
      :ok
    end

    registry_opts = [
      watch_startup: true,
      connect_fun: fn _address -> {:ok, :fake_channel} end,
      watch_fun: blocking_watch(),
      sync_registry_fun: sync_registry_fun,
      disconnect_fun: fn :fake_channel -> :ok end,
      age_check_ms: 60_000,
      unknown_after_ms: 60_000,
      down_after_ms: 60_000
    ]

    {_configured, _table} = start_registry(Keyword.put(registry_opts, :control_plane_activator_ip, "10.0.0.12"))
    assert_receive {:registry_sync, "node-4", %{control_plane_activator_ip: "10.0.0.12"}}, 1_000

    {_unset, _table} = start_registry(registry_opts)
    assert_receive {:registry_sync, "node-4", %{control_plane_activator_ip: ""}}, 1_000
  end

  # -- dial-home registration (R0 PR-2) --------------------------------------

  # A watch that stays open (blocks) so a per-instance streamer persists and the
  # node set is driven purely by registration, not reconnects.
  defp blocking_watch do
    fn _ch, node_id, emit ->
      emit.(node_status(node_id: node_id))
      receive do: (:never -> {:ok, :closed})
    end
  end

  defp register_seams(opts) do
    Keyword.merge(
      [
        watch_startup: true,
        nodes: [],
        connect_fun: fn _addr -> {:ok, :fake_channel} end,
        watch_fun: blocking_watch(),
        sync_registry_fun: fn _ch, _id, _request -> :ok end,
        disconnect_fun: fn :fake_channel -> :ok end,
        channel_updater_fun: fn _id, _addr -> :ok end,
        age_check_ms: 60_000,
        unknown_after_ms: 60_000,
        down_after_ms: 60_000
      ],
      opts
    )
  end

  test "register/2 upserts an instance keyed by (node, pod_uid) and dials it" do
    {reg, table} = start_registry(register_seams([]))

    :ok =
      NodeRegistry.register(reg, %{
        "node" => "node-4",
        "pod_uid" => "uid-1",
        "address" => "10.0.0.1:9090",
        "boot_id" => "boot-1"
      })

    eventually(fn -> Map.has_key?(NodeRegistry.status(reg), "node-4/uid-1") end, 200)
    s = NodeRegistry.status(reg)["node-4/uid-1"]
    assert s.configured_id == "node-4"
    assert s.pod_uid == "uid-1"

    # Its WatchNode status published a dispatchable capacity row keyed by instance.
    eventually(fn -> NodeRegistry.capacity(table) != [] end, 200)
    [facts] = NodeRegistry.capacity(table)
    assert facts.node_id == "node-4"
    assert facts.pod_uid == "uid-1"
    assert facts.instance_id == "node-4/uid-1"
  end

  test "two instances on ONE node coexist (surge roll)" do
    {reg, table} = start_registry(register_seams([]))

    for uid <- ["uid-old", "uid-new"] do
      :ok =
        NodeRegistry.register(reg, %{
          "node" => "node-4",
          "pod_uid" => uid,
          "address" => "10.0.0.#{uid}:9090"
        })
    end

    eventually(fn -> map_size(NodeRegistry.status(reg)) == 2 end, 200)
    eventually(fn -> length(NodeRegistry.capacity(table)) == 2 end, 200)

    uids = table |> NodeRegistry.capacity() |> Enum.map(& &1.pod_uid) |> Enum.sort()
    assert uids == ["uid-new", "uid-old"]
    # Both rows share the node name but are distinct instances.
    nodes = table |> NodeRegistry.capacity() |> Enum.map(& &1.node_id) |> Enum.uniq()
    assert nodes == ["node-4"]
  end

  test "re-registration at a NEW address re-points the streamer and channel" do
    {:ok, dialed} = Agent.start_link(fn -> [] end)
    on_exit(fn -> Embervm.TestProcess.stop_safely(dialed) end)
    {:ok, chan} = Agent.start_link(fn -> [] end)
    on_exit(fn -> Embervm.TestProcess.stop_safely(chan) end)

    connect_fun = fn address ->
      Agent.update(dialed, &[address | &1])
      {:ok, :fake_channel}
    end

    {reg, _table} =
      start_registry(
        register_seams(
          connect_fun: connect_fun,
          channel_updater_fun: fn id, addr -> Agent.update(chan, &[{id, addr} | &1]) end
        )
      )

    :ok = NodeRegistry.register(reg, %{"node" => "node-4", "pod_uid" => "uid-1", "address" => "old-ip:9090"})
    eventually(fn -> "old-ip:9090" in Agent.get(dialed, & &1) end, 200)

    # Same instance (node+pod_uid), NEW address.
    :ok = NodeRegistry.register(reg, %{"node" => "node-4", "pod_uid" => "uid-1", "address" => "new-ip:9090"})
    eventually(fn -> "new-ip:9090" in Agent.get(dialed, & &1) end, 200)
    # NodeChannel is re-pointed under the instance_id key (post-B0c the only key it is
    # registered under, and what every consumer resolves before dialing), so a lookup
    # after a re-registration resolves the new address rather than the dead old endpoint.
    eventually(fn -> {"node-4/uid-1", "new-ip:9090"} in Agent.get(chan, & &1) end, 200)
    assert NodeRegistry.status(reg)["node-4/uid-1"].address == "new-ip:9090"
  end

  test "dial-home registration keys NodeChannel ONLY by instance_id (node-name alias removed, PR-B0c)" do
    # Post the instance-key migration (B0a/B0b) every NodeChannel consumer resolves the
    # owning instance_id before dialing: the dispatcher (PR-2) via pick_node, and the
    # stateful/session/serving/group wakes plus PoolManager via their dial resolvers.
    # PR-B0c dropped the node-name alias: a shared last-writer-wins node-name key across
    # a node's co-located bricks could only misroute a wake to the wrong sibling. So
    # registration now points a SINGLE key, the instance_id, at the instance's address.
    {:ok, chan} = Agent.start_link(fn -> [] end)
    on_exit(fn -> Embervm.TestProcess.stop_safely(chan) end)

    {reg, _table} =
      start_registry(
        register_seams(
          channel_updater_fun: fn id, addr -> Agent.update(chan, &[{id, addr} | &1]) end
        )
      )

    :ok = NodeRegistry.register(reg, %{"node" => "node-4", "pod_uid" => "uid-1", "address" => "10.42.1.24:9090"})

    # The instance_id key resolves; the bare node-name alias is NOT registered.
    eventually(fn -> {"node-4/uid-1", "10.42.1.24:9090"} in Agent.get(chan, & &1) end, 200)
    refute {"node-4", "10.42.1.24:9090"} in Agent.get(chan, & &1)
  end

  test "a node-scoped instance (empty pod_uid) keys NodeChannel exactly once under its node name" do
    # When pod_uid is empty the instance_id IS the node name, so channel_keys/1 (just
    # [instance_id] post-B0c) registers the static/pinned single-daemon override once,
    # under its node name, which is where a node-scoped fact's dial resolves anyway.
    {:ok, chan} = Agent.start_link(fn -> [] end)
    on_exit(fn -> Embervm.TestProcess.stop_safely(chan) end)

    {reg, _table} =
      start_registry(
        register_seams(
          channel_updater_fun: fn id, addr -> Agent.update(chan, &[{id, addr} | &1]) end
        )
      )

    :ok = NodeRegistry.register(reg, %{"node" => "node-9", "address" => "10.42.9.1:9090"})

    eventually(fn -> {"node-9", "10.42.9.1:9090"} in Agent.get(chan, & &1) end, 200)
    updates = Enum.filter(Agent.get(chan, & &1), &match?({"node-9", "10.42.9.1:9090"}, &1))
    assert length(updates) == 1
  end

  test "instance expiry removes its instance_id from NodeChannel (its only key, no node-name alias post-B0c)" do
    # Expiry drops the expiring instance's UNIQUE instance_id key, which post-B0c is the
    # only key it was registered under. There is no shared node-name alias to touch, so
    # removing the instance_id can never affect a co-located sibling (covered end-to-end
    # in the next test).
    {clock, advance} = new_clock()
    {:ok, removed} = Agent.start_link(fn -> [] end)
    on_exit(fn -> Embervm.TestProcess.stop_safely(removed) end)

    {reg, _table} =
      start_registry(
        register_seams(
          clock: clock,
          channel_remover_fun: fn key -> Agent.update(removed, &[key | &1]) end,
          age_check_ms: 60_000,
          unknown_after_ms: 5_000,
          down_after_ms: 15_000,
          expire_after_ms: 90_000
        )
      )

    :ok = NodeRegistry.register(reg, %{"node" => "node-4", "pod_uid" => "uid-1", "address" => "10.0.0.1:9090"})
    eventually(fn -> Map.has_key?(NodeRegistry.status(reg), "node-4/uid-1") end, 200)

    # Drive both expiry signals: registration lapses (advance past expire_after) AND
    # the stream goes dead (advance past down_after with no fresh status).
    advance.(100_000)
    :ok = NodeRegistry.tick(reg)

    # Asserted, not polled. tick/1 is a GenServer.call whose handler runs evaluate_ages/1
    # inline before replying, and status/1 is a call from this same process, so it is
    # strictly ordered after that reply. No later expiry pass can land inside a poll
    # window either (age_check_ms is 60_000 here). Waiting could therefore never turn a
    # missed expiry into a pass; it only delayed the failure by the poll budget and
    # reported "condition never became true" instead of what was actually in the map.
    # This has failed intermittently (#4078): when it does, that is a real expiry bug,
    # and refute prints the offending status map at the moment it happens.
    refute Map.has_key?(NodeRegistry.status(reg), "node-4/uid-1")

    # The instance_id was removed from NodeChannel; the bare node name was never a key,
    # so it never appears in the removal list.
    keys = Agent.get(removed, & &1)
    assert "node-4/uid-1" in keys
    refute "node-4" in keys
  end

  test "expiring one co-located instance leaves its sibling's instance_id intact (PR-B0c: no shared alias)" do
    # The real NodeChannel is the source of truth: register two co-located instances A
    # and B on the same node. Post-B0c there is no shared node-name alias: A and B are
    # keyed purely by their unique instance_ids, so expiring A cannot affect B. This is
    # the structural fix for the co-location misroute: with no shared, last-writer-wins
    # node-name key, one sibling's expiry has nothing to clobber, and the bare node name
    # resolves to nothing (every consumer dials the owning instance_id).
    nid_suffix = System.unique_integer([:positive])
    a_id = "node-#{nid_suffix}/uid-A"
    b_id = "node-#{nid_suffix}/uid-B"
    node = "node-#{nid_suffix}"
    a_addr = "10.0.0.1:9090"
    b_addr = "10.0.0.2:9090"

    # A real NodeChannel (fake connect returns a per-address sentinel) so we exercise
    # the actual dual-key add + instance-only remove, not an Agent stub.
    {:ok, nc} =
      Embervm.NodeChannel.start_link(
        name: nil,
        nodes: [],
        connect_fun: fn addr -> {:ok, {:chan, addr}} end,
        disconnect_fun: fn _ -> :ok end
      )

    on_exit(fn -> Embervm.TestProcess.stop_safely(nc) end)

    {clock, advance} = new_clock()

    {reg, _table} =
      start_registry(
        register_seams(
          clock: clock,
          channel_updater_fun: fn key, addr -> Embervm.NodeChannel.update_address(nc, key, addr) end,
          channel_remover_fun: fn key -> Embervm.NodeChannel.remove_address(nc, key) end,
          age_check_ms: 60_000,
          unknown_after_ms: 5_000,
          down_after_ms: 15_000,
          expire_after_ms: 90_000
        )
      )

    :ok = NodeRegistry.register(reg, %{"node" => node, "pod_uid" => "uid-A", "address" => a_addr})
    eventually(fn -> Map.has_key?(NodeRegistry.status(reg), a_id) end, 200)
    :ok = NodeRegistry.register(reg, %{"node" => node, "pod_uid" => "uid-B", "address" => b_addr})
    eventually(fn -> Map.has_key?(NodeRegistry.status(reg), b_id) end, 200)

    # Sanity: before expiry, both instance_id keys resolve to their own address, and
    # the bare node name resolves to nothing (no alias post-B0c).
    assert {:ok, {:chan, ^a_addr}} = Embervm.NodeChannel.get(nc, a_id)
    assert {:ok, {:chan, ^b_addr}} = Embervm.NodeChannel.get(nc, b_id)
    assert {:error, :unknown_node} = Embervm.NodeChannel.get(nc, node)

    # Expire A only: lapse its registration + kill its stream while B keeps re-registering.
    # (Advancing the clock lapses BOTH; we refresh B's liveness by re-registering it so
    # only A meets the two-signal expiry.)
    advance.(100_000)
    :ok = NodeRegistry.register(reg, %{"node" => node, "pod_uid" => "uid-B", "address" => b_addr})
    NodeRegistry.tick(reg)
    eventually(fn -> not Map.has_key?(NodeRegistry.status(reg), a_id) end, 200)

    # A's own key is gone; B's instance_id is untouched; the bare node name still
    # resolves to nothing. A's expiry could not affect B because they never shared a key.
    assert {:error, :unknown_node} = Embervm.NodeChannel.get(nc, a_id)
    assert {:ok, {:chan, ^b_addr}} = Embervm.NodeChannel.get(nc, b_id)
    assert {:error, :unknown_node} = Embervm.NodeChannel.get(nc, node)
  end

  test "registration feeds instance add to the BaseBuilder (so BuildBase can place)" do
    {:ok, bb} = Agent.start_link(fn -> [] end)
    on_exit(fn -> Embervm.TestProcess.stop_safely(bb) end)

    {reg, _table} =
      start_registry(register_seams(base_builder_updater_fun: fn msg -> Agent.update(bb, &[msg | &1]) end))

    :ok = NodeRegistry.register(reg, %{"node" => "node-a", "pod_uid" => "uid-a", "address" => "a.test:9090"})
    eventually(fn -> {:add, "node-a/uid-a", "a.test:9090"} in Agent.get(bb, & &1) end, 200)
  end

  test "register/2 rejects a body with no node" do
    {reg, _table} = start_registry(register_seams([]))
    assert {:error, :invalid} = NodeRegistry.register(reg, %{"pod_uid" => "uid", "address" => "x:9090"})
    assert {:error, :invalid} = NodeRegistry.register(reg, %{"node" => "node-4", "pod_uid" => "uid"})
  end

  test "expiry requires BOTH a lapsed registration AND a dead stream" do
    {clock, advance} = new_clock()

    {reg, table} =
      start_registry(
        register_seams(
          clock: clock,
          # Real age-out cadence driven by tick/1 under the injected clock.
          age_check_ms: 60_000,
          unknown_after_ms: 5_000,
          down_after_ms: 15_000,
          expire_after_ms: 90_000
        )
      )

    :ok = NodeRegistry.register(reg, %{"node" => "node-4", "pod_uid" => "uid-1", "address" => "10.0.0.1:9090"})
    eventually(fn -> Map.has_key?(NodeRegistry.status(reg), "node-4/uid-1") end, 200)

    # Registration lapses but the stream is still HEALTHY (the blocking watch keeps
    # emitting on connect; here no fresh status arrives, but we only advance a bit):
    # advance past the registration lapse yet keep health above :down. The instance
    # must NOT be expired on one signal alone.
    advance.(100_000)
    # Drive an age evaluation. The stream has gone silent (no new status), so the
    # instance will also be :down after 15s; to isolate the "one signal" case we
    # re-register to refresh liveness first is not possible without a fresh status.
    # Instead assert the two-signal semantics directly: an instance whose stream is
    # healthy (fresh status) but registration lapsed survives.
    :ok = NodeRegistry.inject_status(reg, "node-4/uid-1", node_status())
    NodeRegistry.tick(reg)
    assert Map.has_key?(NodeRegistry.status(reg), "node-4/uid-1")

    # Now let the stream go dead too (advance past down_after with no fresh status).
    advance.(100_000)
    :ok = NodeRegistry.tick(reg)

    # Asserted rather than polled, for the same reason as the expiry test above: tick/1
    # applies the age-out inline before it replies, so there is nothing to wait for.
    refute Map.has_key?(NodeRegistry.status(reg), "node-4/uid-1")
    assert NodeRegistry.capacity(table) == []
  end
end
