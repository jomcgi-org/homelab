defmodule Embervm.NodeChannelTest do
  @moduledoc """
  `Embervm.NodeChannel` caches one gRPC channel per node and reuses it across
  the Prime/Assign hot path. A fake `connect_fun` counts dials and returns a
  distinct sentinel channel per call, so a reuse (one dial for many gets) and an
  invalidate (a re-dial after a transport error) are both observable. Node ids
  are unique per test because the channel cache is process-global
  (persistent_term).
  """
  use ExUnit.Case, async: true

  alias Embervm.NodeChannel

  defp counting_connect do
    {:ok, agent} = Agent.start_link(fn -> 0 end)
    connect = fn _addr -> {:ok, {:chan, Agent.get_and_update(agent, fn n -> {n + 1, n + 1} end)}} end
    {agent, connect}
  end

  defp node_id, do: "node-test-#{System.unique_integer([:positive])}"

  defp start(node_id, connect) do
    {:ok, pid} =
      NodeChannel.start_link(
        name: nil,
        nodes: [%{id: node_id, address: "addr"}],
        connect_fun: connect,
        disconnect_fun: fn _ -> :ok end
      )

    # start_link/1 links the GenServer to this test process, so it may already
    # be torn down (by the link, concurrently with on_exit) by the time this
    # callback runs; tolerate an already-stopped process instead of racing an
    # alive?-then-stop check against it.
    on_exit(fn ->
      try do
        GenServer.stop(pid)
      catch
        :exit, _ -> :ok
      end
    end)

    pid
  end

  test "dials once and caches: repeated gets reuse the channel" do
    nid = node_id()
    {dials, connect} = counting_connect()
    pid = start(nid, connect)

    assert {:ok, chan} = NodeChannel.get(pid, nid)
    assert {:ok, ^chan} = NodeChannel.get(pid, nid)
    assert {:ok, ^chan} = NodeChannel.get(pid, nid)
    assert Agent.get(dials, & &1) == 1
  end

  test "invalidate drops the channel so the next get re-dials" do
    nid = node_id()
    {dials, connect} = counting_connect()
    pid = start(nid, connect)

    assert {:ok, chan1} = NodeChannel.get(pid, nid)
    :ok = NodeChannel.invalidate(pid, nid, chan1)
    # Cast is async; the re-dial happens on the next get, which re-checks the
    # cache. Poll until the invalidation has been processed.
    Process.sleep(20)

    assert {:ok, chan2} = NodeChannel.get(pid, nid)
    assert chan2 != chan1
    assert Agent.get(dials, & &1) == 2
  end

  test "a stale invalidate (channel already replaced) is ignored" do
    nid = node_id()
    {dials, connect} = counting_connect()
    pid = start(nid, connect)

    assert {:ok, chan1} = NodeChannel.get(pid, nid)
    :ok = NodeChannel.invalidate(pid, nid, chan1)
    Process.sleep(20)
    assert {:ok, chan2} = NodeChannel.get(pid, nid)

    # Invalidating the OLD channel again must not touch the current one.
    :ok = NodeChannel.invalidate(pid, nid, chan1)
    Process.sleep(20)
    assert {:ok, ^chan2} = NodeChannel.get(pid, nid)
    assert Agent.get(dials, & &1) == 2
  end

  test "an unconfigured node id yields :unknown_node" do
    nid = node_id()
    {_dials, connect} = counting_connect()
    pid = start(nid, connect)

    assert {:error, :unknown_node} = NodeChannel.get(pid, "not-configured-#{System.unique_integer([:positive])}")
  end

  test "update_address adds a previously-unknown key so get/1 resolves it" do
    # The registry registers an instance under its instance_id via update_address; a
    # key unknown at init must become dialable rather than staying :unknown_node. (This
    # exercises NodeChannel's key-add API, which is unchanged by B0c; only the set of
    # keys the registry adds narrowed to the instance_id alone.)
    nid = node_id()
    {_dials, connect} = counting_connect()
    pid = start(nid, connect)

    alias_key = "#{nid}/uid-1"
    assert {:error, :unknown_node} = NodeChannel.get(pid, alias_key)

    :ok = NodeChannel.update_address(pid, alias_key, "addr")
    assert {:ok, _chan} = NodeChannel.get(pid, alias_key)
  end

  test "remove_address drops the key and its cached channel so get/1 falls back to :unknown_node" do
    # Instance expiry removes the instance's key; a removed key must no longer resolve,
    # so no stale endpoint keeps dialing a torn-down pod's address.
    nid = node_id()
    {_dials, connect} = counting_connect()
    pid = start(nid, connect)

    assert {:ok, _chan} = NodeChannel.get(pid, nid)
    :ok = NodeChannel.remove_address(pid, nid)
    assert {:error, :unknown_node} = NodeChannel.get(pid, nid)
  end

  test "remove_address on an unknown key is a no-op" do
    nid = node_id()
    {_dials, connect} = counting_connect()
    pid = start(nid, connect)

    assert :ok = NodeChannel.remove_address(pid, "never-known-#{System.unique_integer([:positive])}")
  end

  describe "transport_dead?/1" do
    test "a transport death WRAPPED as an RPCError (status 2, connection closed) is dead" do
      # The exact shape the Mint gRPC adapter synthesises when a replaced noded pod's
      # connection breaks: an UNKNOWN(2) RPCError whose message is a transport failure.
      # Misclassifying this as a healthy-channel status is what wedged the node.
      err = %GRPC.RPCError{
        status: 2,
        message: "error occurred while receiving data: {:error, \"the connection is closed\"}"
      }

      assert NodeChannel.transport_dead?(err)
    end

    test "a real server-returned gRPC status rode a HEALTHY channel (not dead)" do
      # FAILED_PRECONDITION / RESOURCE_EXHAUSTED etc. are server verdicts: the channel
      # is fine and must NOT be torn down (D-R2.7.2).
      refute NodeChannel.transport_dead?(%GRPC.RPCError{status: 9, message: "snapshot lost"})
      refute NodeChannel.transport_dead?(%GRPC.RPCError{status: 8, message: "cap reached"})
      # An UNKNOWN(2) that is NOT a connection-closed message is also left as a status.
      refute NodeChannel.transport_dead?(%GRPC.RPCError{status: 2, message: "boom"})
    end

    test "raw transport errors are dead" do
      assert NodeChannel.transport_dead?(:closed)
      assert NodeChannel.transport_dead?({:error, :closed})
      # Mint's transport error is matched structurally (by __struct__), so the map form
      # exercises the same clause without a compile-time dependency on the Mint struct.
      assert NodeChannel.transport_dead?(%{__struct__: Mint.TransportError, reason: :closed})
    end

    test "unrelated reasons are not dead" do
      refute NodeChannel.transport_dead?(:timeout)
      refute NodeChannel.transport_dead?({:error, :whatever})
      refute NodeChannel.transport_dead?(:some_atom)
    end
  end
end
