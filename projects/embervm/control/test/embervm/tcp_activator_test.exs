defmodule Embervm.TcpActivatorTest do
  @moduledoc """
  Exercises Embervm.TcpActivator against REAL `:gen_tcp` sockets on an
  ephemeral test port range (15400-15401), with an injected fake
  StatefulManager (proving the activator resolves the workload by ACCEPT PORT
  and calls wake/3, never parsing any payload) and a fake "VM" echo server (a
  plain `:gen_tcp` listener) proving the bidirectional byte splice + half-close.
  """
  use ExUnit.Case, async: false

  alias Embervm.{NodeCapacity, TcpActivator, WorkloadCatalog}

  @port_a 15_400
  @port_b 15_401

  # A trivial echo server standing in for the woken VM: accepts one connection,
  # echoes every chunk it reads back verbatim, and half-closes its write side
  # the instant its read side sees EOF (mirroring how a real guest would react
  # to the client shutting down its write half).
  defp start_echo_server do
    {:ok, lsock} = :gen_tcp.listen(0, [:binary, packet: :raw, active: false, reuseaddr: true])
    {:ok, port} = :inet.port(lsock)

    pid =
      spawn_link(fn ->
        {:ok, csock} = :gen_tcp.accept(lsock)
        echo_loop(csock)
      end)

    {pid, port}
  end

  defp echo_loop(sock) do
    case :gen_tcp.recv(sock, 0, :infinity) do
      {:ok, data} ->
        :gen_tcp.send(sock, data)
        echo_loop(sock)

      {:error, _} ->
        :gen_tcp.shutdown(sock, :write)
        :gen_tcp.close(sock)
    end
  end

  # A fake StatefulManager: wake/3 records the call and returns a scripted
  # result per workload, so tests assert exactly which workload the activator
  # resolved from the accept port without any payload inspection.
  defmodule FakeManager do
    def start_link(reply_table), do: Agent.start_link(fn -> reply_table end, name: __MODULE__)

    def wake(_server, workload, principal) do
      Agent.get(__MODULE__, fn table ->
        send(Process.whereis(:tcp_activator_test_listener) || self(), {:woke, workload, principal})
        Map.get(table, workload, {:error, {:unknown_workload}})
      end)
    end
  end

  setup do
    cap_table = :"tcpact_cap_#{System.unique_integer([:positive])}"
    cat_table = :"tcpact_cat_#{System.unique_integer([:positive])}"
    NodeCapacity.create(cap_table)
    WorkloadCatalog.create(cat_table)

    WorkloadCatalog.upsert(cat_table, "wl-a", %{class: "stateful", stateful: %{listen_port: @port_a, port: 5432}})
    WorkloadCatalog.upsert(cat_table, "wl-b", %{class: "stateful", stateful: %{listen_port: @port_b, port: 5432}})

    Process.register(self(), :tcp_activator_test_listener)
    # on_exit runs in a separate process AFTER the test process has exited, by
    # which point the registered name is already auto-freed (a name is released
    # when its process dies), so an unconditional unregister raises "not a pid".
    # Guard it: only unregister if the name is somehow still registered.
    on_exit(fn ->
      if Process.whereis(:tcp_activator_test_listener),
        do: Process.unregister(:tcp_activator_test_listener)
    end)

    %{cat_table: cat_table}
  end

  defp start_activator(reply_table, opts \\ []) do
    # FakeManager registers under its fixed __MODULE__ name. A prior test's
    # FakeManager (linked to the now-dead test process) may not have fully released
    # that name yet when the next test starts, so start_link would return
    # {:error, {:already_started, _}}. Synchronously stop any lingering one first so
    # the name is free (Agent.stop waits for termination). The whereis lookup and
    # the stop race: that lingering FakeManager is linked to a dead test process and
    # can exit between the two, so Agent.stop then raises exit(:noproc) — swallow it,
    # since a gone process is exactly the state we wanted.
    case Process.whereis(FakeManager) do
      nil ->
        :ok

      pid ->
        try do
          Agent.stop(pid, :normal, 1_000)
        catch
          :exit, _ -> :ok
        end
    end

    {:ok, _} = FakeManager.start_link(reply_table)
    cat_table = Keyword.fetch!(opts, :catalog_table)

    {:ok, pid} =
      TcpActivator.start_link(
        name: nil,
        port_range: Keyword.get(opts, :port_range, [@port_a, @port_b]),
        manager: :fake,
        manager_mod: FakeManager,
        catalog_table: cat_table,
        store: Embervm.StatefulStore,
        store_mod: Keyword.fetch!(opts, :store_mod)
      )

    # Stop tolerantly: a still-open spliced connection can make the activator exit
    # with a non-:normal reason as its listeners tear down, which GenServer.stop
    # would re-raise. The teardown only needs the process gone, not a clean reason.
    on_exit(fn ->
      if Process.alive?(pid) do
        try do
          GenServer.stop(pid, :normal, 1_000)
        catch
          :exit, _ -> :ok
        end
      end
    end)

    pid
  end

  # A fake StatefulStore-shaped module: published_endpoint/2 always nil (so
  # every connection is a genuine miss) unless a test overrides it.
  defmodule NoEndpointStore do
    def published_endpoint(_srv, _workload), do: nil
  end

  defp connect(port) do
    {:ok, sock} = :gen_tcp.connect(~c"127.0.0.1", port, [:binary, packet: :raw, active: false], 2_000)
    sock
  end

  # -- workload resolution by accept port ---------------------------------------

  test "the workload is resolved by the LOCAL ACCEPT PORT, and wake/3 is called for it", ctx do
    {_echo_pid, vm_port} = start_echo_server()

    start_activator(%{"wl-a" => {:ok, %{ip: "127.0.0.1", port: vm_port}}}, catalog_table: ctx.cat_table, store_mod: NoEndpointStore)

    sock = connect(@port_a)
    assert_receive {:woke, "wl-a", _principal}, 1_000

    :ok = :gen_tcp.send(sock, "ping")
    assert {:ok, "ping"} = :gen_tcp.recv(sock, 4, 1_000)
    :gen_tcp.close(sock)
  end

  test "a different port resolves a DIFFERENT workload", ctx do
    {_echo_pid, vm_port} = start_echo_server()

    start_activator(
      %{"wl-a" => {:ok, %{ip: "127.0.0.1", port: vm_port}}, "wl-b" => {:ok, %{ip: "127.0.0.1", port: vm_port}}},
      catalog_table: ctx.cat_table,
      store_mod: NoEndpointStore
    )

    sock = connect(@port_b)
    assert_receive {:woke, "wl-b", _principal}, 1_000
    :gen_tcp.close(sock)
  end

  # -- bidirectional splice + half-close ----------------------------------------

  test "bytes splice bidirectionally through the woken VM (echo) in both directions", ctx do
    {_echo_pid, vm_port} = start_echo_server()

    start_activator(%{"wl-a" => {:ok, %{ip: "127.0.0.1", port: vm_port}}}, catalog_table: ctx.cat_table, store_mod: NoEndpointStore)

    sock = connect(@port_a)

    :ok = :gen_tcp.send(sock, "hello-splice")
    assert {:ok, "hello-splice"} = :gen_tcp.recv(sock, 12, 1_000)

    # A second write on the SAME connection proves the splice is not one-shot.
    :ok = :gen_tcp.send(sock, "again")
    assert {:ok, "again"} = :gen_tcp.recv(sock, 5, 1_000)

    # Half-close: shut down our write side. The echo server sees EOF, shuts its
    # own write side down, and we should observe a clean EOF (not a reset) on
    # our read side.
    :ok = :gen_tcp.shutdown(sock, :write)
    assert {:error, :closed} = :gen_tcp.recv(sock, 0, 2_000)

    :gen_tcp.close(sock)
  end

  # -- wake failure closes the connection ---------------------------------------

  test "a wake failure closes the parked connection (client observes EOF/reset)" do
    cat_table = :"tcpact_cat_fail_#{System.unique_integer([:positive])}"
    WorkloadCatalog.create(cat_table)
    WorkloadCatalog.upsert(cat_table, "wl-a", %{class: "stateful", stateful: %{listen_port: @port_a, port: 5432}})

    start_activator(%{"wl-a" => {:error, {:wake_failed, :boom}}}, catalog_table: cat_table, store_mod: NoEndpointStore)

    sock = connect(@port_a)
    assert_receive {:woke, "wl-a", _}, 1_000
    # The activator closed its side without dialing an upstream: our read sees EOF.
    assert {:error, :closed} = :gen_tcp.recv(sock, 0, 1_000)
  end

  # -- no workload owns the accept port -----------------------------------------

  test "a connection on a port with no owning stateful workload is closed" do
    empty_cat = :"tcpact_cat_empty_#{System.unique_integer([:positive])}"
    WorkloadCatalog.create(empty_cat)
    # No workload registered for @port_a in this catalog.

    {:ok, _} = FakeManager.start_link(%{})

    {:ok, pid} =
      TcpActivator.start_link(
        name: nil,
        port_range: [@port_a],
        manager: :fake,
        manager_mod: FakeManager,
        catalog_table: empty_cat,
        store: Embervm.StatefulStore,
        store_mod: NoEndpointStore
      )

    on_exit(fn -> Embervm.TestProcess.stop_safely(pid) end)

    sock = connect(@port_a)
    assert {:error, :closed} = :gen_tcp.recv(sock, 0, 1_000)
    refute_receive {:woke, _, _}, 200
  end

  # -- the straggler race: a live endpoint already exists -----------------------

  defmodule LiveEndpointStore do
    def published_endpoint(_srv, "wl-a"), do: %{ip: "127.0.0.1", port: Application.get_env(:embervm, :tcp_activator_test_vm_port)}
    def published_endpoint(_srv, _wl), do: nil
  end

  test "a connection resolved to an ALREADY-live endpoint splices directly, no wake", ctx do
    {_echo_pid, vm_port} = start_echo_server()
    Application.put_env(:embervm, :tcp_activator_test_vm_port, vm_port)
    on_exit(fn -> Application.delete_env(:embervm, :tcp_activator_test_vm_port) end)

    start_activator(%{}, catalog_table: ctx.cat_table, store_mod: LiveEndpointStore)

    sock = connect(@port_a)
    :ok = :gen_tcp.send(sock, "straggler")
    assert {:ok, "straggler"} = :gen_tcp.recv(sock, 9, 1_000)
    refute_receive {:woke, _, _}, 200
    :gen_tcp.close(sock)
  end
end
