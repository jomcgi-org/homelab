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
end
