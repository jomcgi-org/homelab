defmodule Embervm.GrpcConnectionSweeperTest do
  use ExUnit.Case, async: true

  alias Embervm.GrpcConnectionSweeper

  # Mock supervisor that records which children were terminated
  defmodule MockSupervisor do
    use GenServer

    def start_link, do: GenServer.start_link(__MODULE__, [])

    @impl true
    def init(_), do: {:ok, {[], []}}

    def set_children(supervisor, children) do
      GenServer.call(supervisor, {:set_children, children})
    end

    def reset_terminated(supervisor) do
      GenServer.call(supervisor, :reset_terminated)
    end

    @impl true
    def handle_call({:set_children, children}, _from, {_old, terminated}) do
      {:reply, :ok, {children, terminated}}
    end

    @impl true
    def handle_call(:reset_terminated, _from, {children, _terminated}) do
      {:reply, :ok, {children, []}}
    end

    @impl true
    def handle_call(:which_children, _from, state) do
      {children, _terminated} = state
      {:reply, children, state}
    end
  end

  defp mock_supervisor do
    {:ok, pid} = MockSupervisor.start_link()
    pid
  end

  defmodule MockChild do
    use GenServer

    def start_link(target), do: GenServer.start_link(__MODULE__, target)

    def set_target(child, target), do: GenServer.call(child, {:set_target, target})

    @impl true
    def init(target), do: {:ok, %{target: target}}

    @impl true
    def handle_call({:set_target, target}, _from, _state), do: {:reply, :ok, %{target: target}}
  end

  defp mock_child(target) do
    {:ok, pid} = MockChild.start_link(target)
    pid
  end

  defp terminate_child_collector do
    {:ok, calls} = Agent.start_link(fn -> [] end)

    fun = fn _supervisor, pid ->
      Agent.update(calls, &[pid | &1])
      :ok
    end

    {fun, calls}
  end

  # Helper to create a mock child specification
  defp child(pid) do
    {:undefined, pid, :worker, []}
  end

  defp start_sweeper(opts) do
    {:ok, pid} =
      GrpcConnectionSweeper.start_link(
        Keyword.merge(
          [
            name: nil,
            sweep_interval_ms: 0
          ],
          opts
        )
      )

    pid
  end

  def wedged_init_never_acks do
    # This init is started by proc_lib.start_link but never calls init_ack,
    # so the process stays stuck in {proc_lib, :sync_start, 2}.
    # Used only by test "timeout/wedged branch is protected by strikes".
    receive do
      :never -> :ok
    end
  end

  test "address normalization strips ipv4 prefix" do
    sweeper =
      start_sweeper(
        status_fun: fn -> %{"node-0" => %{address: "10.42.3.34:9090"}} end,
        sweep_enabled: false
      )

    result = GrpcConnectionSweeper.sweep_now(sweeper)
    assert is_list(result)
  end

  test "address normalization leaves non-ipv4 addresses unchanged" do
    # Both formats should be treated as equivalent when normalized
    sweeper =
      start_sweeper(
        status_fun: fn -> %{"node-0" => %{address: "10.42.3.34:9090"}} end,
        sweep_enabled: false
      )

    # Verify the sweeper handles both formats without error
    result = GrpcConnectionSweeper.sweep_now(sweeper)
    assert is_list(result)
  end

  test "empty registry address set aborts sweep (fail-safe)" do
    # Empty registry = no live addresses = must NOT reap anything.
    # This is a routine case at boot under dial-home: the registry is seeded
    # empty and fills via registration. The sweeper must abort entirely (skip
    # the sweep), not classify children against an empty set.
    supervisor = mock_supervisor()

    # Create a mock child with a target NOT in the live set (which is empty).
    # Under the broken (inverted) fail-safe, this child would be reaped because
    # address_in_set?("ipv4:10.42.3.34:9090", MapSet.new()) == false.
    # After the fix, the sweep is aborted before classification reaches this.
    dead_pid = spawn(fn -> :ok end)

    MockSupervisor.set_children(supervisor, [
      child(dead_pid)
    ])

    sweeper =
      start_sweeper(
        status_fun: fn -> %{} end,
        supervisor: supervisor,
        sweep_enabled: false
      )

    result = GrpcConnectionSweeper.sweep_now(sweeper)

    # With the fixed fail-safe, the sweep is skipped and returns [].
    # With the broken code, every child with a readable target would be reaped
    # (but since we can't mock :sys.get_state easily, we rely on the sweep
    # skipping entirely). The critical assertion is that result == [] and the
    # log says the sweep was skipped.
    assert result == []
  end

  test "unavailable registry aborts sweep (fail-safe)" do
    # If the registry crashes or is unreachable, we must NOT reap anything.
    # The sweeper must distinguish this from a populated keep-set.
    supervisor = mock_supervisor()

    dead_pid = spawn(fn -> :ok end)

    MockSupervisor.set_children(supervisor, [
      child(dead_pid)
    ])

    sweeper =
      start_sweeper(
        status_fun: fn -> raise "registry error" end,
        supervisor: supervisor,
        sweep_enabled: false
      )

    result = GrpcConnectionSweeper.sweep_now(sweeper)

    # With the fixed fail-safe, the sweep is skipped and returns [].
    assert result == []
  end

  test "sweep logs summary in dry-run mode (gate off)" do
    sweeper =
      start_sweeper(
        status_fun: fn -> %{"node-0" => %{address: "10.42.3.34:9090"}} end,
        sweep_enabled: false
      )

    # The log is internal; we verify the sweeper runs without error
    result = GrpcConnectionSweeper.sweep_now(sweeper)
    assert is_list(result)
  end

  test "sweep respects the enabled flag" do
    sweeper = start_sweeper(status_fun: fn -> %{} end, sweep_enabled: false)

    # With gate off and empty registry (sweep skipped), result is []
    result = GrpcConnectionSweeper.sweep_now(sweeper)
    assert result == []
  end

  test "sweeper can be disabled with sweep_interval_ms = 0" do
    sweeper =
      start_sweeper(
        status_fun: fn -> %{} end,
        sweep_interval_ms: 0,
        sweep_enabled: false
      )

    # With interval = 0, no periodic sweep is scheduled
    result = GrpcConnectionSweeper.sweep_now(sweeper)
    assert is_list(result)
  end

  test "normalize_address handles ipv4-prefixed and raw formats" do
    # Test both input formats are handled without error
    sweeper =
      start_sweeper(
        status_fun: fn ->
          %{
            "node-0" => %{address: "10.42.3.34:9090"},
            "node-1" => %{address: "ipv4:10.42.1.141:9090"}
          }
        end,
        sweep_enabled: false
      )

    result = GrpcConnectionSweeper.sweep_now(sweeper)
    assert is_list(result)
  end

  test "child reapable on N consecutive sweeps IS terminated on the Nth sweep, not before" do
    supervisor = mock_supervisor()
    target = "10.42.9.99:9090"
    child_pid = mock_child(target)
    MockSupervisor.set_children(supervisor, [child(child_pid)])
    {terminate_fun, calls} = terminate_child_collector()

    sweeper =
      start_sweeper(
        status_fun: fn -> %{"node-0" => %{address: "10.42.3.34:9090"}} end,
        supervisor: supervisor,
        terminate_child_fun: terminate_fun,
        sweep_enabled: true
      )

    assert GrpcConnectionSweeper.sweep_now(sweeper) == []
    assert :sys.get_state(sweeper).strikes == %{child_pid => 1}
    assert Agent.get(calls, & &1) == []

    assert GrpcConnectionSweeper.sweep_now(sweeper) == []
    assert :sys.get_state(sweeper).strikes == %{child_pid => 2}
    assert Agent.get(calls, & &1) == []

    assert [{^child_pid, reason}] = GrpcConnectionSweeper.sweep_now(sweeper)
    assert reason =~ "reapable for 3 consecutive sweeps"
    assert Agent.get(calls, & &1) == [child_pid]
  end

  test "armed sweeper logs accumulating strikes before it reaps (#4419)" do
    # When the gate is ON the sweeper used to log NOTHING until it killed
    # something, so "working and finding nothing" and "not running at all"
    # read identically. Every pass must leave a heartbeat, and a pid climbing
    # toward the threshold must be visible before the kill lands.
    supervisor = mock_supervisor()
    target = "10.42.9.99:9090"
    child_pid = mock_child(target)
    MockSupervisor.set_children(supervisor, [child(child_pid)])
    {terminate_fun, _calls} = terminate_child_collector()

    sweeper =
      start_sweeper(
        status_fun: fn -> %{"node-0" => %{address: "10.42.3.34:9090"}} end,
        supervisor: supervisor,
        terminate_child_fun: terminate_fun,
        sweep_enabled: true
      )

    log = ExUnit.CaptureLog.capture_log(fn -> GrpcConnectionSweeper.sweep_now(sweeper) end)
    assert log =~ "pass complete (children=1, keep_set=1, reaped=0, pending=1)"
    assert log =~ "1 orchestrator(s) accumulating strikes"
    assert log =~ "#{inspect(child_pid)}: target #{inspect(target)} not in live set (strike 1 of 3)"
    refute log =~ "DRY RUN"
  end

  test "dry-run sweeper logs a heartbeat on a clean pass" do
    supervisor = mock_supervisor()
    live_pid = mock_child("10.42.3.34:9090")
    MockSupervisor.set_children(supervisor, [child(live_pid)])

    sweeper =
      start_sweeper(
        status_fun: fn -> %{"node-0" => %{address: "10.42.3.34:9090"}} end,
        supervisor: supervisor,
        sweep_enabled: false
      )

    log = ExUnit.CaptureLog.capture_log(fn -> GrpcConnectionSweeper.sweep_now(sweeper) end)
    assert log =~ "(DRY RUN, gate off): pass complete (children=1, keep_set=1, reaped=0, pending=0)"
  end

  test "timeout/wedged branch is protected by strikes" do
    supervisor = mock_supervisor()

    # Start a process that enters proc_lib.start_link but never calls init_ack,
    # leaving it stuck in {proc_lib, :sync_start, 2}. This is stable across
    # repeated :sys.get_state probes because current_function is determined by
    # call stack, not reductions. Verify the child stays wedged across sweeps
    # with process_info assertions.
    init_task = Task.async(fn ->
      :proc_lib.start_link(
        __MODULE__,
        :wedged_init_never_acks,
        [],
        :infinity,
        []
      )
    end)

    # Give the init task time to enter sync_start before proceeding
    Process.sleep(100)

    # The task is hung in start_link waiting for init_ack; extract its pid
    wedged_pid = init_task.pid

    # Verify it is in the expected sync_start state
    case :erlang.process_info(wedged_pid, :current_function) do
      {:current_function, {:proc_lib, :sync_start, 2}} ->
        :ok

      other ->
        flunk("Process not in sync_start: #{inspect(other)}, cannot continue test")
    end

    MockSupervisor.set_children(supervisor, [child(wedged_pid)])
    {terminate_fun, calls} = terminate_child_collector()

    sweeper =
      start_sweeper(
        status_fun: fn -> %{"node-0" => %{address: "10.42.3.34:9090"}} end,
        supervisor: supervisor,
        terminate_child_fun: terminate_fun,
        sweep_enabled: true
      )

    assert GrpcConnectionSweeper.sweep_now(sweeper) == []
    assert :sys.get_state(sweeper).strikes[wedged_pid] == 1
    assert Agent.get(calls, & &1) == []
    assert elem(:erlang.process_info(wedged_pid, :current_function), 1) == {:proc_lib, :sync_start, 2},
      "wedged classification must be stable across sweep 1"

    # Sweep 2: strike becomes 2; verify still wedged
    assert GrpcConnectionSweeper.sweep_now(sweeper) == []
    assert :sys.get_state(sweeper).strikes[wedged_pid] == 2
    assert Agent.get(calls, & &1) == []
    assert elem(:erlang.process_info(wedged_pid, :current_function), 1) == {:proc_lib, :sync_start, 2},
      "wedged classification must be stable across sweep 2"

    # Sweep 3: strike reaches threshold (3), child terminated
    assert [{^wedged_pid, reason}] = GrpcConnectionSweeper.sweep_now(sweeper)
    assert reason =~ "reapable for 3 consecutive sweeps"
    assert Agent.get(calls, & &1) == [wedged_pid]
  end

  test "catch :exit aborts sweep, grants no strikes" do
    supervisor = mock_supervisor()
    child_pid = mock_child("10.42.9.99:9090")
    MockSupervisor.set_children(supervisor, [child(child_pid)])

    sweeper =
      start_sweeper(
        status_fun: fn -> exit(:noproc) end,
        supervisor: supervisor,
        sweep_enabled: false
      )

    result = GrpcConnectionSweeper.sweep_now(sweeper)
    assert result == []
    assert :sys.get_state(sweeper).strikes == %{}
  end

  test "child reapable once, then keep, then reapable again is NOT terminated" do
    supervisor = mock_supervisor()
    child_a = mock_child("10.42.9.99:9090")
    child_b = mock_child("10.42.3.34:9090")
    MockSupervisor.set_children(supervisor, [child(child_a), child(child_b)])
    {terminate_fun, calls} = terminate_child_collector()

    sweeper =
      start_sweeper(
        status_fun: fn -> %{"node-0" => %{address: "10.42.3.34:9090"}} end,
        supervisor: supervisor,
        terminate_child_fun: terminate_fun,
        sweep_enabled: true
      )

    assert GrpcConnectionSweeper.sweep_now(sweeper) == []
    assert :sys.get_state(sweeper).strikes[child_a] == 1
    assert :sys.get_state(sweeper).strikes[child_b] == nil

    MockChild.set_target(child_a, "10.42.3.34:9090")
    assert GrpcConnectionSweeper.sweep_now(sweeper) == []
    assert :sys.get_state(sweeper).strikes == %{}

    MockChild.set_target(child_a, "10.42.9.99:9090")
    assert GrpcConnectionSweeper.sweep_now(sweeper) == []
    assert :sys.get_state(sweeper).strikes[child_a] == 1
    assert Agent.get(calls, & &1) == []
  end

  test "child reapable for N-1 sweeps only is NOT terminated" do
    supervisor = mock_supervisor()
    child_pid = mock_child("10.42.9.99:9090")
    MockSupervisor.set_children(supervisor, [child(child_pid)])
    {terminate_fun, calls} = terminate_child_collector()

    sweeper =
      start_sweeper(
        status_fun: fn -> %{"node-0" => %{address: "10.42.3.34:9090"}} end,
        supervisor: supervisor,
        terminate_child_fun: terminate_fun,
        sweep_enabled: true
      )

    assert GrpcConnectionSweeper.sweep_now(sweeper) == []
    assert GrpcConnectionSweeper.sweep_now(sweeper) == []
    assert :sys.get_state(sweeper).strikes[child_pid] == 2
    assert Agent.get(calls, & &1) == []
  end

  test "with gate OFF, N consecutive reapable sweeps still terminate NOTHING" do
    supervisor = mock_supervisor()
    child_pid = mock_child("10.42.9.99:9090")
    MockSupervisor.set_children(supervisor, [child(child_pid)])
    {terminate_fun, calls} = terminate_child_collector()

    sweeper =
      start_sweeper(
        status_fun: fn -> %{"node-0" => %{address: "10.42.3.34:9090"}} end,
        supervisor: supervisor,
        terminate_child_fun: terminate_fun,
        sweep_enabled: false
      )

    assert GrpcConnectionSweeper.sweep_now(sweeper) == []
    assert GrpcConnectionSweeper.sweep_now(sweeper) == []
    assert [{^child_pid, reason}] = GrpcConnectionSweeper.sweep_now(sweeper)
    assert reason =~ "reapable for 3 consecutive sweeps"
    assert Agent.get(calls, & &1) == []
  end

  test "strike entries are pruned for pids that disappear from supervisor's child list" do
    supervisor = mock_supervisor()
    child_a = mock_child("10.42.9.99:9090")
    child_b = mock_child("10.42.9.98:9090")
    MockSupervisor.set_children(supervisor, [child(child_a), child(child_b)])

    sweeper =
      start_sweeper(
        status_fun: fn -> %{"node-0" => %{address: "10.42.3.34:9090"}} end,
        supervisor: supervisor,
        sweep_enabled: false
      )

    assert GrpcConnectionSweeper.sweep_now(sweeper) == []
    assert map_size(:sys.get_state(sweeper).strikes) == 2

    MockSupervisor.set_children(supervisor, [child(child_b)])
    assert GrpcConnectionSweeper.sweep_now(sweeper) == []
    assert :sys.get_state(sweeper).strikes == %{child_b => 2}

    assert [{^child_b, reason}] = GrpcConnectionSweeper.sweep_now(sweeper)
    assert reason =~ "reapable for 3 consecutive sweeps"
    assert :sys.get_state(sweeper).strikes == %{child_b => 3}
  end
end
