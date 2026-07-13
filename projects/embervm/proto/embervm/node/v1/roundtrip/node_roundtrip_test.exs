defmodule Embervm.NodeRoundtripTest do
  # Cross-language gRPC round trip for the node.proto contract: this Elixir client
  # (the generated Embervm.Node.V1.* stubs from node.pb.ex, over the grpc Mint
  # adapter) drives the Go fake NodeService server
  # (//projects/embervm/proto/embervm/node/v1/fakenode), exercising every RPC,
  # including the server-streaming WatchNode. It proves the Go server stubs and
  # the Elixir client stubs interoperate on the wire. No Firecracker, no real VM
  # lifecycle: the fake server returns request-derived data so each assertion
  # proves the request crossed the wire intact.
  #
  # Run only by the dedicated roundtrip genrule, which builds the Go binary and
  # passes its path in EMBERVM_FAKE_NODE_BIN. This file lives outside
  # control/test/ on purpose, so the general `mix test` never compiles it (that
  # build has neither node.pb.ex nor the Go binary staged).
  use ExUnit.Case, async: false

  alias Embervm.Node.V1.{
    AssignRequest,
    BuildBaseRequest,
    DestroyRequest,
    GetNodeStatusRequest,
    GuestRequest,
    NodeService,
    NodeStatus,
    PrimeRequest,
    ResourceSpec,
    Trace,
    WatchNodeRequest
  }

  setup do
    bin = System.get_env("EMBERVM_FAKE_NODE_BIN") || flunk("EMBERVM_FAKE_NODE_BIN not set")
    proc = Port.open({:spawn_executable, bin}, [:binary, :exit_status, {:line, 1024}])
    node_port = read_port(proc, 5_000)
    channel = connect(node_port, 50)

    on_exit(fn ->
      _ = GRPC.Stub.disconnect(channel)
      if Port.info(proc), do: Port.close(proc)
    end)

    {:ok, channel: channel}
  end

  test "unary RPCs echo request fields across the wire", %{channel: ch} do
    {:ok, bb} =
      NodeService.Stub.build_base(ch, %BuildBaseRequest{
        trace: %Trace{workload: "echo"},
        image_ref: "img@sha256:abc",
        workload_revision: "rev7",
        resources: %ResourceSpec{vcpus: 2, mem_mib: 512}
      })

    assert bb.snapshot_ref == "snap:img@sha256:abc"
    assert bb.image_digest == "sha256:rev7"
    assert bb.base_size_bytes == 512 * 1024 * 1024
    assert bb.arch == "amd64"

    {:ok, pr} = NodeService.Stub.prime(ch, %PrimeRequest{snapshot_ref: "snapX"})
    assert pr.vm_id == "vm:snapX"

    {:ok, asg} =
      NodeService.Stub.assign(ch, %AssignRequest{
        vm_id: "vm:snapX",
        request: %GuestRequest{method: "POST", path: "/invoke", body: "hello"},
        timeout_ms: 1_000
      })

    assert asg.response.status_code == 200
    assert asg.response.body == "hello"
    assert asg.response.headers["x-echo-path"] == "/invoke"
    assert asg.usage.wall_ms == 3

    assert {:ok, _} = NodeService.Stub.destroy(ch, %DestroyRequest{vm_id: "vm:snapX"})

    {:ok, ns} = NodeService.Stub.get_node_status(ch, %GetNodeStatusRequest{node_id: "node-4"})
    assert ns.node_id == "node-4"
    assert ns.max_live_vms == 10
  end

  test "WatchNode server-streams heartbeats in order", %{channel: ch} do
    {:ok, stream} = NodeService.Stub.watch_node(ch, %WatchNodeRequest{node_id: "node-4"})

    statuses =
      Enum.flat_map(stream, fn
        {:ok, %NodeStatus{} = s} -> [s]
        _ -> []
      end)

    assert length(statuses) == 3
    assert Enum.map(statuses, & &1.live_vms) == [0, 1, 2]
    assert Enum.all?(statuses, &(&1.node_id == "node-4"))
  end

  # Read the "PORT=<n>" line the fake server prints once it is listening.
  defp read_port(proc, timeout) do
    receive do
      {^proc, {:data, {:eol, "PORT=" <> rest}}} -> String.to_integer(String.trim(rest))
      {^proc, {:data, {:eol, _other}}} -> read_port(proc, timeout)
      {^proc, {:exit_status, code}} -> flunk("fake node exited early with status #{code}")
    after
      timeout -> flunk("timed out waiting for fake node PORT line")
    end
  end

  # The port is bound before the PORT line prints, but the HTTP/2 handshake races
  # the server's Accept loop, so retry the connect briefly. Plaintext h2c (no
  # cred) matches the fake server's insecure listener.
  defp connect(port, 0), do: flunk("could not connect to fake node on port #{port}")

  defp connect(port, tries) do
    case GRPC.Stub.connect("127.0.0.1:#{port}", adapter: GRPC.Client.Adapters.Mint) do
      {:ok, channel} ->
        channel

      {:error, _reason} ->
        Process.sleep(50)
        connect(port, tries - 1)
    end
  end
end
