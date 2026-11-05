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

    @impl true
    def handle_call({:set_children, children}, _from, {_old, terminated}) do
      {:reply, :ok, {children, terminated}}
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

  defmodule MetadataSupervisor do
    use GenServer

    def start_link(_), do: GenServer.start_link(__MODULE__, nil)

    @impl true
    def init(_), do: {:ok, %{children: %{}, listed: []}}

    def set_children(supervisor, children, metadata) do
      GenServer.call(supervisor, {:set_children, children, metadata})
    end

    @impl true
    def handle_call({:set_children, children, metadata}, _from, _state) do
      {:reply, :ok, %{children: metadata, listed: children}}
    end

    @impl true
    def handle_call(:which_children, _from, state), do: {:reply, state.listed, state}
  end

  # These fixtures are local to the metadata tests. Monitor cleanup even when
  # an assertion fails, including the child whose init never acknowledges.
  defp stop_fixture_processes(pids) do
    monitors = Enum.map(pids, &{&1, Process.monitor(&1)})
    Enum.each(pids, &Process.exit(&1, :kill))

    Enum.each(monitors, fn {pid, monitor} ->
      assert_receive {:DOWN, ^monitor, :process, ^pid, _}, 1_000
      refute Process.alive?(pid)
    end)
  end

  defp track_fixture(pid) do
    Process.unlink(pid)
    on_exit(fn -> stop_fixture_processes([pid]) end)
    pid
  end

  def metadata_init_never_acks(owner, ready, starter) do
    send(owner, {ready, self(), starter})

    receive do
      :never -> :ok
    end
  end

  defp metadata_wedged_child do
    owner = self()
    ready = make_ref()

    starter =
      spawn(fn ->
        receive do
          :start ->
            :proc_lib.start_link(
              __MODULE__,
              :metadata_init_never_acks,
              [owner, ready, self()],
              :infinity,
              []
            )
        end
      end)

    # Registered before starting init: even a failed readiness assertion must
    # stop the starter and any unacknowledged child linked to it.
    on_exit(fn ->
      children =
        case Process.info(starter, :links) do
          {:links, children} -> children
          nil -> []
        end

      stop_fixture_processes([starter | children])
    end)

    send(starter, :start)
    assert_receive {^ready, init_child, ^starter}, 1_000
    on_exit(fn -> stop_fixture_processes([init_child]) end)
    deadline = System.monotonic_time(:millisecond) + 1_000
    assert_sync_start(starter, deadline)
    assert {:links, links} = Process.info(starter, :links)
    assert init_child in links

    assert {:timeout, {:sys, :get_state, [^starter, 25]}} =
             catch_exit(:sys.get_state(starter, 25))

    starter
  end

  defp assert_sync_start(pid, deadline) do
    case Process.info(pid, [:current_function, :status]) do
      [current_function: {:proc_lib, :sync_start, 2}, status: :waiting] ->
        :ok

      state ->
        assert System.monotonic_time(:millisecond) < deadline,
               "fixture did not enter sync_start: #{inspect(state)}"

        :erlang.yield()
        assert_sync_start(pid, deadline)
    end
  end

  defp start_metadata_sweeper do
    {:ok, supervisor} = MetadataSupervisor.start_link(nil)
    track_fixture(supervisor)
    {terminate_fun, calls} = terminate_child_collector()
    track_fixture(calls)

    sweeper =
      start_sweeper(
        status_fun: fn -> %{"node-0" => %{address: "10.42.3.34:9090"}} end,
        supervisor: supervisor,
        terminate_child_fun: terminate_fun,
        strikes_required: 3
      )
      |> track_fixture()

    {supervisor, sweeper, calls}
  end

  defp connection_metadata(args) do
    {{GRPC.Client.Connection, :start_link, args}, :transient, 5_000, :worker,
     [GRPC.Client.Connection]}
  end

  defp metadata_entries(_pids, nil), do: %{}
  defp metadata_entries(pids, metadata), do: Map.new(pids, &{&1, metadata})

  for {shape, prefixed} <- [{:target_and_opts, false}, {:keyword_opts, true}] do
    test "default supervisor metadata protects a live target via #{shape}" do
      {supervisor, sweeper, calls} = start_metadata_sweeper()
      pid = metadata_wedged_child()
      target = if unquote(prefixed), do: "ipv4:10.42.3.34:9090", else: "10.42.3.34:9090"

      args =
        case unquote(shape) do
          :target_and_opts -> [target, []]
          :keyword_opts -> [[target: target]]
        end

      metadata = connection_metadata(args)
      MetadataSupervisor.set_children(supervisor, [child(pid)], %{pid => metadata})
      assert :sys.get_state(supervisor).children == %{pid => metadata}

      for _ <- 1..3 do
        assert GrpcConnectionSweeper.sweep_now(sweeper) == []
        assert :sys.get_state(sweeper).strikes == %{}
        assert Agent.get(calls, & &1) == []
        assert_sync_start(pid, System.monotonic_time(:millisecond) + 1_000)
      end
    end
  end

  test "default supervisor metadata retains normalized strikes across PID replacement" do
    {supervisor, sweeper, calls} = start_metadata_sweeper()
    target = "10.42.9.99:9090"
    first = metadata_wedged_child()

    MetadataSupervisor.set_children(supervisor, [child(first)], %{
      first => connection_metadata(["ipv4:#{target}", []])
    })

    assert GrpcConnectionSweeper.sweep_now(sweeper) == []
    assert :sys.get_state(sweeper).strikes == %{{:target, target} => 1}
    assert Agent.get(calls, & &1) == []

    replacement = metadata_wedged_child()
    refute replacement == first

    MetadataSupervisor.set_children(supervisor, [child(replacement)], %{
      replacement => connection_metadata([[target: target]])
    })

    assert GrpcConnectionSweeper.sweep_now(sweeper) == []
    assert :sys.get_state(sweeper).strikes == %{{:target, target} => 2}
    assert Agent.get(calls, & &1) == []
    assert [{^replacement, reason}] = GrpcConnectionSweeper.sweep_now(sweeper)
    assert reason =~ "reapable for 3 consecutive sweeps"
    assert :sys.get_state(sweeper).strikes == %{{:target, target} => 3}
    assert Agent.get(calls, & &1) == [replacement]
  end

  test "default supervisor metadata increments same-target siblings once per sweep" do
    {supervisor, sweeper, calls} = start_metadata_sweeper()
    target = "10.42.9.99:9090"
    first = metadata_wedged_child()
    second = metadata_wedged_child()

    MetadataSupervisor.set_children(supervisor, [child(first), child(second)], %{
      first => connection_metadata([target, []]),
      second => connection_metadata([[target: "ipv4:#{target}"]])
    })

    for strike <- 1..2 do
      assert GrpcConnectionSweeper.sweep_now(sweeper) == []
      assert :sys.get_state(sweeper).strikes == %{{:target, target} => strike}
      assert Agent.get(calls, & &1) == []
    end

    result = GrpcConnectionSweeper.sweep_now(sweeper)
    assert Enum.sort(Enum.map(result, &elem(&1, 0))) == Enum.sort([first, second])
    assert Enum.all?(result, fn {_, reason} -> reason =~ "reapable for 3 consecutive sweeps" end)
    assert :sys.get_state(sweeper).strikes == %{{:target, target} => 3}
    assert Enum.sort(Agent.get(calls, & &1)) == Enum.sort([first, second])
  end

  for {label, metadata} <- [
        missing: nil,
        malformed: {:not, :child_metadata},
        invalid_direct:
          {{GRPC.Client.Connection, :start_link, [42, []]}, :transient, 5_000, :worker, []},
        empty_direct:
          {{GRPC.Client.Connection, :start_link, ["", []]}, :transient, 5_000, :worker, []},
        absent_keyword:
          {{GRPC.Client.Connection, :start_link, [[]]}, :transient, 5_000, :worker, []},
        invalid_keyword:
          {{GRPC.Client.Connection, :start_link, [[target: nil]]}, :transient, 5_000, :worker, []},
        empty_keyword:
          {{GRPC.Client.Connection, :start_link, [[target: "ipv4:"]]}, :transient, 5_000, :worker,
           []}
      ] do
    test "default supervisor metadata falls back safely for #{label}" do
      {supervisor, sweeper, calls} = start_metadata_sweeper()
      wedged = metadata_wedged_child()
      non_wedged = unreadable_non_wedged_child()
      metadata = unquote(Macro.escape(metadata))
      children = [child(wedged), child(non_wedged)]

      entries = metadata_entries([wedged, non_wedged], metadata)

      MetadataSupervisor.set_children(supervisor, children, entries)

      for strike <- 1..2 do
        assert GrpcConnectionSweeper.sweep_now(sweeper) == []
        assert :sys.get_state(sweeper).strikes == %{{:unreadable_pid, wedged} => strike}
        assert Agent.get(calls, & &1) == []
        assert_non_wedged(non_wedged)
      end

      assert [{^wedged, reason}] = GrpcConnectionSweeper.sweep_now(sweeper)
      assert reason =~ "reapable for 3 consecutive sweeps"
      assert :sys.get_state(sweeper).strikes == %{{:unreadable_pid, wedged} => 3}
      assert Agent.get(calls, & &1) == [wedged]
      assert_non_wedged(non_wedged)
    end
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

  defp unreadable_non_wedged_child do
    owner = self()
    ready = make_ref()

    pid =
      spawn(fn ->
        Enum.each(1..1_000, fn _ -> :erlang.yield() end)
        send(owner, {ready, self()})

        # Ignore system messages so the sweeper must time out reading state.
        receive do
          :stop -> :ok
        end
      end)

    on_exit(fn ->
      monitor = Process.monitor(pid)
      Process.exit(pid, :kill)
      assert_receive {:DOWN, ^monitor, :process, ^pid, _}, 1_000
      refute Process.alive?(pid)
    end)

    assert_receive {^ready, ^pid}, 1_000
    assert {:timeout, {:sys, :get_state, [^pid, 25]}} = catch_exit(:sys.get_state(pid, 25))
    assert_non_wedged(pid)
    pid
  end

  defp assert_non_wedged(pid) do
    assert [current_function: current_function, reductions: reductions, status: :waiting] =
             Process.info(pid, [:current_function, :reductions, :status])

    refute current_function == {:proc_lib, :sync_start, 2}
    assert reductions > 100
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
        supervisor: mock_supervisor(),
        status_fun: fn -> %{"node-0" => %{address: "10.42.3.34:9090"}} end
      )

    result = GrpcConnectionSweeper.sweep_now(sweeper)
    assert is_list(result)
  end

  test "address normalization leaves non-ipv4 addresses unchanged" do
    # Both formats should be treated as equivalent when normalized
    sweeper =
      start_sweeper(
        supervisor: mock_supervisor(),
        status_fun: fn -> %{"node-0" => %{address: "10.42.3.34:9090"}} end
      )

    # Verify the sweeper handles both formats without error
    result = GrpcConnectionSweeper.sweep_now(sweeper)
    assert is_list(result)
  end

  test "with the SPIFFE dial on, a channel to the TLS port of a live node is kept (#5758)" do
    previous = Application.get_env(:embervm, :noded_tls, :not_set)

    on_exit(fn ->
      case previous do
        :not_set -> Application.delete_env(:embervm, :noded_tls)
        value -> Application.put_env(:embervm, :noded_tls, value)
      end
    end)

    Application.put_env(:embervm, :noded_tls, %{
      spiffe_id: "spiffe://embervm.jomcgi.dev/ns/embervm/sa/embervm-embervm-noded",
      tls_port: 9443,
      cert_file: "/run/embervm-svid/svid.pem",
      key_file: "/run/embervm-svid/svid_key.pem",
      bundle_file: "/run/embervm-svid/svid_bundle.pem"
    })

    supervisor = mock_supervisor()
    tls_child = mock_child("ipv4:10.42.3.34:9443")
    plaintext_child = mock_child("ipv4:10.42.3.34:9090")
    stray_child = mock_child("ipv4:10.42.9.99:9443")

    MockSupervisor.set_children(supervisor, [
      child(tls_child),
      child(plaintext_child),
      child(stray_child)
    ])

    {terminate_fun, calls} = terminate_child_collector()

    sweeper =
      start_sweeper(
        status_fun: fn -> %{"node-0" => %{address: "10.42.3.34:9090"}} end,
        supervisor: supervisor,
        terminate_child_fun: terminate_fun,
        strikes_required: 1
      )

    assert [{^stray_child, _reason}] = GrpcConnectionSweeper.sweep_now(sweeper)
    assert Agent.get(calls, & &1) == [stray_child]
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
        supervisor: supervisor
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
        supervisor: supervisor
      )

    result = GrpcConnectionSweeper.sweep_now(sweeper)

    # With the fixed fail-safe, the sweep is skipped and returns [].
    assert result == []
  end

  test "sweeper can be disabled with sweep_interval_ms = 0" do
    sweeper =
      start_sweeper(
        status_fun: fn -> %{} end,
        sweep_interval_ms: 0
      )

    # With interval = 0, no periodic sweep is scheduled
    result = GrpcConnectionSweeper.sweep_now(sweeper)
    assert is_list(result)
  end

  test "normalize_address handles ipv4-prefixed and raw formats" do
    # Test both input formats are handled without error
    sweeper =
      start_sweeper(
        supervisor: mock_supervisor(),
        status_fun: fn ->
          %{
            "node-0" => %{address: "10.42.3.34:9090"},
            "node-1" => %{address: "ipv4:10.42.1.141:9090"}
          }
        end
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
        terminate_child_fun: terminate_fun
      )

    assert GrpcConnectionSweeper.sweep_now(sweeper) == []
    assert :sys.get_state(sweeper).strikes == %{{:target, target} => 1}
    assert Agent.get(calls, & &1) == []

    assert GrpcConnectionSweeper.sweep_now(sweeper) == []
    assert :sys.get_state(sweeper).strikes == %{{:target, target} => 2}
    assert Agent.get(calls, & &1) == []

    assert [{^child_pid, reason}] = GrpcConnectionSweeper.sweep_now(sweeper)
    assert reason =~ "reapable for 3 consecutive sweeps"
    assert Agent.get(calls, & &1) == [child_pid]
  end

  test "child with the same target across restart is reaped on the third strike" do
    supervisor = mock_supervisor()
    target = "10.42.9.99:9090"
    first_pid = mock_child(target)
    MockSupervisor.set_children(supervisor, [child(first_pid)])
    {terminate_fun, calls} = terminate_child_collector()

    sweeper =
      start_sweeper(
        status_fun: fn -> %{"node-0" => %{address: "10.42.3.34:9090"}} end,
        supervisor: supervisor,
        terminate_child_fun: terminate_fun
      )

    assert GrpcConnectionSweeper.sweep_now(sweeper) == []
    assert :sys.get_state(sweeper).strikes == %{{:target, target} => 1}

    second_pid = mock_child(target)
    refute second_pid == first_pid
    MockSupervisor.set_children(supervisor, [child(second_pid)])

    assert GrpcConnectionSweeper.sweep_now(sweeper) == []
    assert :sys.get_state(sweeper).strikes == %{{:target, target} => 2}
    assert [{^second_pid, reason}] = GrpcConnectionSweeper.sweep_now(sweeper)
    assert reason =~ "reapable for 3 consecutive sweeps"
    assert Agent.get(calls, & &1) == [second_pid]
  end

  for readable_prefix <- ["", "ipv4:"],
      unreadable_prefix <- ["", "ipv4:"],
      order <- [:readable_first, :unreadable_first],
      prior_strikes <- [0, 1, 2] do
    @tag :mixed_target
    test "target-level keep wins with #{inspect({readable_prefix, unreadable_prefix, order, prior_strikes})}" do
      supervisor = mock_supervisor()
      target = "10.42.9.99:9090"
      stale_target = "10.42.8.88:9090"
      readable = mock_child(unquote(readable_prefix) <> target)
      unreadable = unreadable_non_wedged_child()
      stale = mock_child(stale_target)
      owner = self()
      {terminate_fun, calls} = terminate_child_collector()

      sweeper =
        start_sweeper(
          status_fun: fn -> %{"node-0" => %{address: "10.42.3.34:9090"}} end,
          supervisor: supervisor,
          terminate_child_fun: terminate_fun,
          start_args_target_fun: fn ^unreadable ->
            send(owner, {:start_args_target, unreadable})
            {:ok, unquote(unreadable_prefix) <> target}
          end,
          strikes_required: 3
        )

      # Accumulate strikes through normal sweeps, before adding the keeper.
      MockSupervisor.set_children(supervisor, [child(readable)])

      for count <- Enum.take([1, 2], unquote(prior_strikes)) do
        assert GrpcConnectionSweeper.sweep_now(sweeper) == []
        assert :sys.get_state(sweeper).strikes == %{{:target, target} => count}
        assert Agent.get(calls, & &1) == []
      end

      siblings =
        case unquote(order) do
          :readable_first -> [child(readable), child(unreadable)]
          :unreadable_first -> [child(unreadable), child(readable)]
        end

      MockSupervisor.set_children(supervisor, siblings ++ [child(stale)])

      for count <- 1..3 do
        result = GrpcConnectionSweeper.sweep_now(sweeper)
        assert Process.alive?(sweeper)
        assert_receive {:start_args_target, ^unreadable}
        assert_non_wedged(unreadable)
        assert :sys.get_state(sweeper).strikes == %{{:target, stale_target} => count}

        if count < 3 do
          assert result == []
          assert Agent.get(calls, & &1) == []
        else
          assert [{^stale, reason}] = result
          assert reason =~ "reapable for 3 consecutive sweeps"
          assert Agent.get(calls, & &1) == [stale]
        end

        refute readable in Agent.get(calls, & &1)
        refute unreadable in Agent.get(calls, & &1)
      end
    end
  end

  test "two reapable siblings share one strike per sweep and both reap on strike three" do
    supervisor = mock_supervisor()
    target = "10.42.9.99:9090"
    first = mock_child(target)
    second = mock_child("ipv4:" <> target)
    MockSupervisor.set_children(supervisor, [child(first), child(second)])
    {terminate_fun, calls} = terminate_child_collector()

    sweeper =
      start_sweeper(
        status_fun: fn -> %{"node-0" => %{address: "10.42.3.34:9090"}} end,
        supervisor: supervisor,
        terminate_child_fun: terminate_fun,
        strikes_required: 3
      )

    for count <- 1..3 do
      result = GrpcConnectionSweeper.sweep_now(sweeper)
      assert Process.alive?(sweeper)
      assert :sys.get_state(sweeper).strikes == %{{:target, target} => count}

      if count < 3 do
        assert result == []
        assert Agent.get(calls, & &1) == []
      else
        assert [{^first, first_reason}, {^second, second_reason}] = result
        assert first_reason =~ "reapable for 3 consecutive sweeps"
        assert second_reason =~ "reapable for 3 consecutive sweeps"
        assert Agent.get(calls, & &1) == [second, first]
      end
    end
  end

  test "sweeper logs accumulating strikes before it reaps (#4419)" do
    # The armed sweeper used to log NOTHING until it killed
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
        terminate_child_fun: terminate_fun
      )

    log = ExUnit.CaptureLog.capture_log(fn -> GrpcConnectionSweeper.sweep_now(sweeper) end)

    assert log =~
             "pass complete (children=1, keep_set=1, reaped=0, pending=1, under_threshold=1)"

    assert log =~ "1 orchestrator(s) accumulating strikes"

    assert log =~
             "#{inspect(child_pid)}: target #{inspect(target)} not in live set (strike 1 of 3)"
  end

  test "sweeper logs a heartbeat on a clean pass" do
    supervisor = mock_supervisor()
    live_pid = mock_child("10.42.3.34:9090")
    MockSupervisor.set_children(supervisor, [child(live_pid)])

    sweeper =
      start_sweeper(
        status_fun: fn -> %{"node-0" => %{address: "10.42.3.34:9090"}} end,
        supervisor: supervisor
      )

    log = ExUnit.CaptureLog.capture_log(fn -> GrpcConnectionSweeper.sweep_now(sweeper) end)

    assert log =~
             "embervm grpc connection sweeper: pass complete (children=1, keep_set=1, reaped=0, pending=0, under_threshold=0)"
  end

  test "timeout/wedged branch is protected by strikes" do
    supervisor = mock_supervisor()

    # Start a process that enters proc_lib.start_link but never calls init_ack,
    # leaving it stuck in {proc_lib, :sync_start, 2}. This is stable across
    # repeated :sys.get_state probes because current_function is determined by
    # call stack, not reductions. Verify the child stays wedged across sweeps
    # with process_info assertions.
    init_task =
      Task.async(fn ->
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
        terminate_child_fun: terminate_fun
      )

    assert GrpcConnectionSweeper.sweep_now(sweeper) == []
    assert :sys.get_state(sweeper).strikes[{:unreadable_pid, wedged_pid}] == 1
    assert Agent.get(calls, & &1) == []

    assert elem(:erlang.process_info(wedged_pid, :current_function), 1) ==
             {:proc_lib, :sync_start, 2},
           "wedged classification must be stable across sweep 1"

    # Sweep 2: strike becomes 2; verify still wedged
    assert GrpcConnectionSweeper.sweep_now(sweeper) == []
    assert :sys.get_state(sweeper).strikes[{:unreadable_pid, wedged_pid}] == 2
    assert Agent.get(calls, & &1) == []

    assert elem(:erlang.process_info(wedged_pid, :current_function), 1) ==
             {:proc_lib, :sync_start, 2},
           "wedged classification must be stable across sweep 2"

    # Sweep 3: strike reaches threshold (3), child terminated
    assert [{^wedged_pid, reason}] = GrpcConnectionSweeper.sweep_now(sweeper)
    assert reason =~ "reapable for 3 consecutive sweeps"
    assert Agent.get(calls, & &1) == [wedged_pid]
  end

  test "timeout/wedged branch uses the target from start args when available" do
    supervisor = mock_supervisor()

    init_task =
      Task.async(fn ->
        :proc_lib.start_link(
          __MODULE__,
          :wedged_init_never_acks,
          [],
          :infinity,
          []
        )
      end)

    Process.sleep(100)
    wedged_pid = init_task.pid
    target = "10.42.9.99:9090"
    MockSupervisor.set_children(supervisor, [child(wedged_pid)])
    {terminate_fun, calls} = terminate_child_collector()

    sweeper =
      start_sweeper(
        status_fun: fn -> %{"node-0" => %{address: "10.42.3.34:9090"}} end,
        supervisor: supervisor,
        terminate_child_fun: terminate_fun,
        start_args_target_fun: fn ^wedged_pid -> {:ok, "ipv4:#{target}"} end,
        strikes_required: 1
      )

    assert [{^wedged_pid, reason}] = GrpcConnectionSweeper.sweep_now(sweeper)
    assert reason =~ "reapable for 1 consecutive sweeps"
    assert :sys.get_state(sweeper).strikes == %{{:target, target} => 1}
    assert Agent.get(calls, & &1) == [wedged_pid]
  end

  test "catch :exit aborts sweep, grants no strikes" do
    supervisor = mock_supervisor()
    child_pid = mock_child("10.42.9.99:9090")
    MockSupervisor.set_children(supervisor, [child(child_pid)])

    sweeper =
      start_sweeper(
        status_fun: fn -> exit(:noproc) end,
        supervisor: supervisor
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
        terminate_child_fun: terminate_fun
      )

    assert GrpcConnectionSweeper.sweep_now(sweeper) == []
    assert :sys.get_state(sweeper).strikes[{:target, "10.42.9.99:9090"}] == 1
    assert :sys.get_state(sweeper).strikes[{:target, "10.42.3.34:9090"}] == nil

    MockChild.set_target(child_a, "10.42.3.34:9090")
    assert GrpcConnectionSweeper.sweep_now(sweeper) == []
    assert :sys.get_state(sweeper).strikes == %{}

    MockChild.set_target(child_a, "10.42.9.99:9090")
    assert GrpcConnectionSweeper.sweep_now(sweeper) == []
    assert :sys.get_state(sweeper).strikes[{:target, "10.42.9.99:9090"}] == 1
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
        terminate_child_fun: terminate_fun
      )

    assert GrpcConnectionSweeper.sweep_now(sweeper) == []
    assert GrpcConnectionSweeper.sweep_now(sweeper) == []
    assert :sys.get_state(sweeper).strikes[{:target, "10.42.9.99:9090"}] == 2
    assert Agent.get(calls, & &1) == []
  end

  test "keep-set target never accumulates strikes across pid changes" do
    supervisor = mock_supervisor()
    target = "10.42.3.34:9090"
    first_pid = mock_child(target)
    MockSupervisor.set_children(supervisor, [child(first_pid)])

    sweeper =
      start_sweeper(
        status_fun: fn -> %{"node-0" => %{address: target}} end,
        supervisor: supervisor
      )

    assert GrpcConnectionSweeper.sweep_now(sweeper) == []
    assert :sys.get_state(sweeper).strikes == %{}

    second_pid = mock_child(target)
    refute second_pid == first_pid
    MockSupervisor.set_children(supervisor, [child(second_pid)])

    assert GrpcConnectionSweeper.sweep_now(sweeper) == []
    assert :sys.get_state(sweeper).strikes == %{}
  end

  test "strike entries are pruned for targets that disappear from supervisor's child list" do
    supervisor = mock_supervisor()
    child_a = mock_child("10.42.9.99:9090")
    child_b = mock_child("10.42.9.98:9090")
    MockSupervisor.set_children(supervisor, [child(child_a), child(child_b)])
    {terminate_fun, calls} = terminate_child_collector()

    sweeper =
      start_sweeper(
        status_fun: fn -> %{"node-0" => %{address: "10.42.3.34:9090"}} end,
        supervisor: supervisor,
        terminate_child_fun: terminate_fun
      )

    assert GrpcConnectionSweeper.sweep_now(sweeper) == []

    assert :sys.get_state(sweeper).strikes == %{
             {:target, "10.42.9.99:9090"} => 1,
             {:target, "10.42.9.98:9090"} => 1
           }

    MockSupervisor.set_children(supervisor, [child(child_b)])
    assert GrpcConnectionSweeper.sweep_now(sweeper) == []
    assert :sys.get_state(sweeper).strikes == %{{:target, "10.42.9.98:9090"} => 2}

    assert [{^child_b, reason}] = GrpcConnectionSweeper.sweep_now(sweeper)
    assert reason =~ "reapable for 3 consecutive sweeps"
    assert :sys.get_state(sweeper).strikes == %{{:target, "10.42.9.98:9090"} => 3}
    assert Agent.get(calls, & &1) == [child_b]
  end
end
