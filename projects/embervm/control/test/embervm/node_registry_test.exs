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
      cpu_headroom_millicores: 0,
      live_vms: Keyword.get(opts, :live_vms, 0),
      max_live_vms: 8,
      draining: Keyword.get(opts, :draining, false),
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
end
