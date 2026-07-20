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

  alias Embervm.NodeRegistry
  alias Embervm.Node.V1.{NodeStatus, WorkloadCapacity}

  # -- helpers ---------------------------------------------------------------

  # A controllable monotonic clock backed by an Agent: the registry reads it for
  # every timestamp, and the test advances it to drive age-out deterministically.
  defp new_clock do
    {:ok, pid} = Agent.start_link(fn -> 0 end)
    on_exit(fn -> if Process.alive?(pid), do: Agent.stop(pid) end)
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
      watch_startup: false
    ]

    {:ok, pid} = NodeRegistry.start_link(Keyword.merge(defaults, opts))
    on_exit(fn -> if Process.alive?(pid), do: GenServer.stop(pid) end)
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
    on_exit(fn -> if Process.alive?(connects), do: Agent.stop(connects) end)

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
        sync_registry_fun: fn :fake_channel, _id -> :ok end,
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
    on_exit(fn -> if Process.alive?(connects), do: Agent.stop(connects) end)
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
        sync_registry_fun: fn :fake_channel, _id -> :ok end,
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
    on_exit(fn -> if Process.alive?(syncs), do: Agent.stop(syncs) end)
    test_pid = self()

    # Record each replay (channel, node_id) and let the streamer proceed. The
    # sync_registry_fun is the seam the production default reads the catalog and
    # calls SyncRegistry through; here we just assert it fires on each connect.
    sync_registry_fun = fn :fake_channel, node_id ->
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
        sync_registry_fun: fn _ch, _id -> :ok end,
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
    on_exit(fn -> if Process.alive?(dialed), do: Agent.stop(dialed) end)
    {:ok, chan} = Agent.start_link(fn -> [] end)
    on_exit(fn -> if Process.alive?(chan), do: Agent.stop(chan) end)

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
    # NodeChannel is re-pointed under the NODE NAME key (what every dispatch consumer
    # looks up), not the instance_id, so a node-name lookup after a re-registration
    # resolves the new address rather than dialing the dead old endpoint.
    eventually(fn -> {"node-4", "new-ip:9090"} in Agent.get(chan, & &1) end, 200)
    assert NodeRegistry.status(reg)["node-4/uid-1"].address == "new-ip:9090"
  end

  test "dial-home registration DUAL-KEYS NodeChannel: instance_id (dispatcher) AND node-name alias (legacy wakes)" do
    # Bug A fix (brick co-location foundation Step 1). Two consumer classes address
    # NodeChannel differently: the dispatcher (PR-2) resolves pick_node to an
    # instance_id ("node-4/uid-1") and calls get("node-4/uid-1"); the legacy wakes
    # (PoolManager.refill_node/2 on facts.configured_id "node-4", plus
    # stateful/session/serving/group placement) call get("node-4"). A single-key
    # registration starved one class or the other. So registration points BOTH keys
    # at the instance's address; both lookups resolve.
    {:ok, chan} = Agent.start_link(fn -> [] end)
    on_exit(fn -> if Process.alive?(chan), do: Agent.stop(chan) end)

    {reg, _table} =
      start_registry(
        register_seams(
          channel_updater_fun: fn id, addr -> Agent.update(chan, &[{id, addr} | &1]) end
        )
      )

    :ok = NodeRegistry.register(reg, %{"node" => "node-4", "pod_uid" => "uid-1", "address" => "10.42.1.24:9090"})

    # Both keys resolve to the instance's address: the node-name alias (legacy wakes)
    # AND the instance_id (dispatcher). Neither class is starved.
    eventually(fn -> {"node-4", "10.42.1.24:9090"} in Agent.get(chan, & &1) end, 200)
    eventually(fn -> {"node-4/uid-1", "10.42.1.24:9090"} in Agent.get(chan, & &1) end, 200)
  end

  test "a node-scoped instance (empty pod_uid) keys NodeChannel exactly once (keys collapse)" do
    # When pod_uid is empty the instance_id IS the node name, so channel_keys/1
    # de-dups to a single key: the static/pinned single-daemon override registers
    # NodeChannel once, not twice under the same key.
    {:ok, chan} = Agent.start_link(fn -> [] end)
    on_exit(fn -> if Process.alive?(chan), do: Agent.stop(chan) end)

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

  test "instance expiry REMOVES both NodeChannel keys (no stale alias after an instance dies)" do
    # The dual-key add must be matched by a dual-key remove: expire_instance drops
    # both the instance_id and the node-name alias from NodeChannel, so no stale key
    # keeps a legacy wake dialing a torn-down pod's IP (and, under co-location, so a
    # same-node replacement's alias is not shadowed by a dead one).
    {clock, advance} = new_clock()
    {:ok, removed} = Agent.start_link(fn -> [] end)
    on_exit(fn -> if Process.alive?(removed), do: Agent.stop(removed) end)

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
    NodeRegistry.tick(reg)
    eventually(fn -> not Map.has_key?(NodeRegistry.status(reg), "node-4/uid-1") end, 50)

    # Both keys were removed from NodeChannel.
    keys = Agent.get(removed, & &1)
    assert "node-4/uid-1" in keys
    assert "node-4" in keys
  end

  test "registration feeds instance add to the BaseBuilder (so BuildBase can place)" do
    {:ok, bb} = Agent.start_link(fn -> [] end)
    on_exit(fn -> if Process.alive?(bb), do: Agent.stop(bb) end)

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
    NodeRegistry.tick(reg)
    eventually(fn -> not Map.has_key?(NodeRegistry.status(reg), "node-4/uid-1") end, 50)
    assert NodeRegistry.capacity(table) == []
  end
end
