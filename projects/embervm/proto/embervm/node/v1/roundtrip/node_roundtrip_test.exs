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
    BankRequest,
    BuildBaseRequest,
    DestroyRequest,
    EvictSnapshotRequest,
    FreshSource,
    GetNodeStatusRequest,
    GuestRequest,
    NodeService,
    NodeStatus,
    PrimeRequest,
    RelightRequest,
    RelightSource,
    ResourceSpec,
    SessionAssignRequest,
    StartServingRequest,
    StopServingRequest,
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

  test "session verbs round-trip across the wire (R2 additive contract)", %{channel: ch} do
    # SessionAssign: deliver-without-destroy. The fake echoes the body/path AND
    # the session_id (into a header) so we prove the new session_id field crossed.
    {:ok, sa} =
      NodeService.Stub.session_assign(ch, %SessionAssignRequest{
        vm_id: "vm-s1",
        request: %GuestRequest{method: "POST", path: "/invoke", body: "state"},
        timeout_ms: 1_000,
        session_id: "s-abc"
      })

    assert sa.response.status_code == 200
    assert sa.response.body == "state"
    assert sa.response.headers["x-echo-path"] == "/invoke"
    assert sa.response.headers["x-session-id"] == "s-abc"
    assert sa.usage.wall_ms == 6
    assert sa.suspect == false

    # Bank: derives the snapshot_ref from the session_id.
    {:ok, bank} = NodeService.Stub.bank(ch, %BankRequest{vm_id: "vm-s1", session_id: "s-abc"})
    assert bank.snapshot_ref == "sessions/s-abc"
    assert bank.size_bytes == 2048

    # Relight: derives the vm_id from the snapshot_ref.
    {:ok, relit} =
      NodeService.Stub.relight(ch, %RelightRequest{
        snapshot_ref: "sessions/s-abc",
        session_id: "s-abc"
      })

    assert relit.vm_id == "vm:sessions/s-abc"

    # EvictSnapshot: idempotent, returns an empty response.
    assert {:ok, _} =
             NodeService.Stub.evict_snapshot(ch, %EvictSnapshotRequest{
               snapshot_ref: "sessions/s-abc"
             })
  end

  test "serving verbs round-trip across the wire (R3 additive contract)", %{channel: ch} do
    # StartServing, fresh source: the fake derives vm_id from the fresh ref and
    # echoes the requested port, proving the oneof fresh branch crossed the wire.
    {:ok, fresh} =
      NodeService.Stub.start_serving(ch, %StartServingRequest{
        source: {:fresh, %FreshSource{snapshot_ref: "base-snap"}},
        port: 8080,
        health_path: "/healthz",
        resources: %ResourceSpec{vcpus: 1, mem_mib: 256}
      })

    assert fresh.vm_id == "vm:base-snap"
    assert fresh.ip == "10.99.0.1"
    assert fresh.port == 8080

    # StartServing, relight source: proves the oneof relight branch crosses the
    # wire distinctly from fresh.
    {:ok, relit} =
      NodeService.Stub.start_serving(ch, %StartServingRequest{
        source: {:relight, %RelightSource{snapshot_ref: "serving/s-abc"}},
        port: 9090,
        health_path: "/healthz"
      })

    assert relit.vm_id == "vm:serving/s-abc"
    assert relit.port == 9090

    # StopServing, BANK mode: derives snapshot_ref from vm_id.
    {:ok, banked} =
      NodeService.Stub.stop_serving(ch, %StopServingRequest{
        vm_id: "vm:base-snap",
        mode: :STOP_SERVING_MODE_BANK
      })

    assert banked.snapshot_ref == "serving/vm:base-snap"
    assert banked.size_bytes == 3072

    # StopServing, DESTROY mode: no snapshot produced.
    {:ok, destroyed} =
      NodeService.Stub.stop_serving(ch, %StopServingRequest{
        vm_id: "vm:serving/s-abc",
        mode: :STOP_SERVING_MODE_DESTROY
      })

    assert destroyed.snapshot_ref == ""
    assert destroyed.size_bytes == 0
  end

  test "NodeStatus reports serving facts (R3 additive fields)", %{channel: ch} do
    {:ok, ns} = NodeService.Stub.get_node_status(ch, %GetNodeStatusRequest{node_id: "node-4"})

    assert [vm] = ns.serving_vms
    assert vm.vm_id == "vm-srv1"
    assert vm.workload == "sandbox-serving"
    assert vm.ip == "10.99.0.2"
    assert vm.port == 8080
    assert vm.healthy == true
    assert vm.last_probe_unix_ms == 1_700_000_001_000

    assert [snap] = ns.serving_snapshots
    assert snap.snapshot_ref == "serving/s-srv2"
    assert snap.workload == "sandbox-serving"
    assert snap.size_bytes == 8192
    assert snap.created_at_unix_ms == 1_700_000_002_000

    assert ns.serving_subnet_cidr == "10.99.0.0/24"
  end

  test "NodeStatus reports session facts (R2 additive fields)", %{channel: ch} do
    {:ok, ns} = NodeService.Stub.get_node_status(ch, %GetNodeStatusRequest{node_id: "node-4"})

    assert [vm] = ns.session_vms
    assert vm.vm_id == "vm-s1"
    assert vm.session_id == "s-sess1"
    assert vm.workload == "sandbox-session"

    assert [snap] = ns.session_snapshots
    assert snap.snapshot_ref == "sessions/s-sess2"
    assert snap.session_id == "s-sess2"
    assert snap.size_bytes == 4096
    assert snap.created_at_unix_ms == 1_700_000_000_000

    assert ns.snapshot_disk_free_bytes == 9_000_000_000
    assert ns.snapshot_disk_used_bytes == 1_000_000_000
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
