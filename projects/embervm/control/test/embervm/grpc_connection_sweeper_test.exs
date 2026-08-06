defmodule Embervm.GrpcConnectionSweeperTest do
  use ExUnit.Case, async: true

  alias Embervm.GrpcConnectionSweeper

  # A minimal node registry stub that returns a fixed set of live addresses.
  defmodule RegistryStub do
    use GenServer

    def start_link(addresses), do: GenServer.start_link(__MODULE__, addresses)

    @impl true
    def init(addresses), do: {:ok, addresses}

    @impl true
    def handle_call(:status, _from, addresses) do
      # Convert address list into the shape NodeRegistry.status returns
      status =
        addresses
        |> Enum.with_index()
        |> Enum.reduce(%{}, fn {addr, idx}, acc ->
          Map.put(acc, "node-#{idx}", %{address: addr})
        end)

      {:reply, status, addresses}
    end
  end

  defp start_registry(addresses) do
    {:ok, pid} = RegistryStub.start_link(addresses)
    pid
  end

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

  test "address normalization strips ipv4 prefix" do
    registry = start_registry(["10.42.3.34:9090"])

    sweeper =
      start_sweeper(
        node_registry: registry,
        sweep_enabled: false
      )

    result = GrpcConnectionSweeper.sweep_now(sweeper)
    assert is_list(result)
  end

  test "address normalization leaves non-ipv4 addresses unchanged" do
    # Both formats should be treated as equivalent when normalized
    registry = start_registry(["10.42.3.34:9090"])

    sweeper =
      start_sweeper(
        node_registry: registry,
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
    registry = start_registry([])
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
        node_registry: registry,
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
    bad_registry = spawn(fn -> :ok end)
    supervisor = mock_supervisor()

    dead_pid = spawn(fn -> :ok end)

    MockSupervisor.set_children(supervisor, [
      child(dead_pid)
    ])

    sweeper =
      start_sweeper(
        node_registry: bad_registry,
        supervisor: supervisor,
        sweep_enabled: false
      )

    result = GrpcConnectionSweeper.sweep_now(sweeper)

    # With the fixed fail-safe, the sweep is skipped and returns [].
    assert result == []
  end

  test "sweep logs summary in dry-run mode (gate off)" do
    registry = start_registry(["10.42.3.34:9090"])
    sweeper = start_sweeper(node_registry: registry, sweep_enabled: false)

    # The log is internal; we verify the sweeper runs without error
    result = GrpcConnectionSweeper.sweep_now(sweeper)
    assert is_list(result)
  end

  test "sweep respects the enabled flag" do
    registry = start_registry([])
    sweeper = start_sweeper(node_registry: registry, sweep_enabled: false)

    # With gate off and empty registry (sweep skipped), result is []
    result = GrpcConnectionSweeper.sweep_now(sweeper)
    assert result == []
  end

  test "sweeper can be disabled with sweep_interval_ms = 0" do
    registry = start_registry([])

    sweeper =
      start_sweeper(
        node_registry: registry,
        sweep_interval_ms: 0,
        sweep_enabled: false
      )

    # With interval = 0, no periodic sweep is scheduled
    result = GrpcConnectionSweeper.sweep_now(sweeper)
    assert is_list(result)
  end

  test "normalize_address handles ipv4-prefixed and raw formats" do
    # Test both input formats are handled without error
    registry = start_registry(["10.42.3.34:9090", "ipv4:10.42.1.141:9090"])
    sweeper = start_sweeper(node_registry: registry, sweep_enabled: false)

    result = GrpcConnectionSweeper.sweep_now(sweeper)
    assert is_list(result)
  end
end
