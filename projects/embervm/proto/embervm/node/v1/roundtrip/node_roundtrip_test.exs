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
    ArtifactRef,
    ArchiveVolumeRequest,
    AssignRequest,
    BankRequest,
    BuildBaseRequest,
    CreateGroupNetworkRequest,
    DeleteGroupNetworkRequest,
    DeleteVolumeRequest,
    DestroyRequest,
    EvictArtifactRequest,
    EvictSnapshotRequest,
    ExportArtifactRequest,
    FreshSource,
    GetNodeStatusRequest,
    GuestRequest,
    NodeService,
    NodeStatus,
    PrimeRequest,
    RelightRequest,
    RelightSource,
    RetireVolumeRequest,
    ResolveStatefulRequest,
    ResourceSpec,
    RestoreArtifactRequest,
    SessionAssignRequest,
    StartGroupMemberRequest,
    StartServingRequest,
    StartStatefulRequest,
    StopGroupMemberRequest,
    StopServingRequest,
    StopStatefulRequest,
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

    {:ok, pr} = NodeService.Stub.prime(ch, %PrimeRequest{
      snapshot_ref: "snapX",
      volume_disk_path: "/var/lib/ember/session/w/workspace.img",
      volume_mount: "/workspace",
      volume_size_bytes: 10_737_418_240,
      lineage_id: "lineage-1"
    })
    assert pr.vm_id == "vm:lineage-1:/workspace"

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
    assert [%{workload: "sandbox-session", lineage_id: "s-sess3", size_bytes: 1024, allocated_bytes: 512}] = ns.session_volumes
  end

  test "session verbs round-trip across the wire (R2 additive contract)", %{channel: ch} do
    # SessionAssign: deliver-without-destroy. The fake echoes the body/path AND
    # the session_id (into a header) so we prove the new session_id field crossed.
    {:ok, sa} =
      NodeService.Stub.session_assign(ch, %SessionAssignRequest{
        vm_id: "vm-s1",
        request: %GuestRequest{method: "POST", path: "/invoke", body: "state"},
        timeout_ms: 1_000,
        session_id: "s-abc",
        volume_disk_path: "/var/lib/ember/volumes/session/claude-runtime/s-abc/workspace.img",
        volume_mount: "/workspace-persistent",
        volume_size_bytes: 10_737_418_240,
        lineage_id: "s-abc"
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

    assert {:ok, %Embervm.Node.V1.ArchiveVolumeResponse{skipped: false}} =
             NodeService.Stub.archive_volume(ch, %ArchiveVolumeRequest{
               workload: "claude-runtime",
               lineage_id: "s-abc"
             })

    assert {:ok, %Embervm.Node.V1.RetireVolumeResponse{}} =
             NodeService.Stub.retire_volume(ch, %RetireVolumeRequest{
               workload: "claude-runtime",
               lineage_id: "s-abc"
             })
  end

  test "serving verbs round-trip across the wire (R3 additive contract)", %{channel: ch} do
    # StartServing, fresh source: the fake derives vm_id from the fresh ref and
    # echoes the requested port, proving the oneof fresh branch crossed the wire.
    {:ok, fresh} =
      NodeService.Stub.start_serving(ch, %StartServingRequest{
        source: {:fresh, %FreshSource{serving_image_ref: "base-snap"}},
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

  test "stateful verbs round-trip across the wire (R4 additive contract)", %{channel: ch} do
    # StartStateful FRESH: cold-boot the boot image ref, generation bumped to 1,
    # no relight, no cold-boot reason. Proves the mode enum, boot_image_ref, and
    # the volume/mmds fields cross the wire.
    {:ok, fresh} =
      NodeService.Stub.start_stateful(ch, %StartStatefulRequest{
        trace: %Trace{workload: "scratch-postgres"},
        mode: :START_STATEFUL_MODE_FRESH,
        boot_image_ref: "pg-base",
        port: 5432,
        volume_size_bytes: 10 * 1024 * 1024 * 1024,
        volume_mount: "/data",
        create_if_missing: true,
        resources: %ResourceSpec{vcpus: 1, mem_mib: 512},
        mmds_env: %{"POSTGRES_PASSWORD" => "seeded"}
      })

    assert fresh.vm_id == "vm:pg-base"
    assert fresh.ip == "10.99.0.3"
    assert fresh.port == 5432
    assert fresh.generation == 1
    assert fresh.was_relight == false
    assert fresh.cold_boot_reason == ""

    # StartStateful RELIGHT, matched pair: resumes warm (was_relight true), no
    # cold-boot reason. The fake scripts a warm relight for any ordinary ref.
    {:ok, warm} =
      NodeService.Stub.start_stateful(ch, %StartStatefulRequest{
        mode: :START_STATEFUL_MODE_RELIGHT,
        relight_snapshot_ref: "stateful/scratch-postgres",
        boot_image_ref: "pg-base",
        port: 5432
      })

    assert warm.vm_id == "vm:stateful/scratch-postgres"
    assert warm.generation == 7
    assert warm.was_relight == true
    assert warm.cold_boot_reason == ""

    # StartStateful RELIGHT, generation mismatch: the fake scripts a cold-boot
    # fallback off the ref content, proving cold_boot_reason crosses the wire.
    {:ok, mismatch} =
      NodeService.Stub.start_stateful(ch, %StartStatefulRequest{
        mode: :START_STATEFUL_MODE_RELIGHT,
        relight_snapshot_ref: "stateful/mismatch",
        boot_image_ref: "pg-base",
        port: 5432
      })

    assert mismatch.was_relight == false
    assert mismatch.cold_boot_reason == "generation_mismatch"

    # StartStateful RELIGHT, unreadable ledger: the other cold-boot fallback branch.
    {:ok, noledger} =
      NodeService.Stub.start_stateful(ch, %StartStatefulRequest{
        mode: :START_STATEFUL_MODE_RELIGHT,
        relight_snapshot_ref: "stateful/noledger",
        boot_image_ref: "pg-base",
        port: 5432
      })

    assert noledger.was_relight == false
    assert noledger.cold_boot_reason == "ledger_unreadable"

    # StopStateful BANK: derives the bundle ref from vm_id and stamps the generation.
    {:ok, banked} =
      NodeService.Stub.stop_stateful(ch, %StopStatefulRequest{
        vm_id: "vm-st1",
        mode: :STOP_STATEFUL_MODE_BANK
      })

    assert banked.snapshot_ref == "stateful/vm-st1"
    assert banked.generation == 7
    assert banked.size_bytes == 16384

    # StopStateful DESTROY: no snapshot produced.
    {:ok, destroyed} =
      NodeService.Stub.stop_stateful(ch, %StopStatefulRequest{
        vm_id: "vm-st1",
        mode: :STOP_STATEFUL_MODE_DESTROY
      })

    assert destroyed.snapshot_ref == ""
    assert destroyed.generation == 0
    assert destroyed.size_bytes == 0

    # StopStateful CHECKPOINT (ADR embervm/008 phase one): pauses and returns an
    # opaque token plus the paused generation, publishing NO bundle yet (empty
    # snapshot_ref / zero size_bytes).
    {:ok, ckpt} =
      NodeService.Stub.stop_stateful(ch, %StopStatefulRequest{
        vm_id: "vm-st1",
        mode: :STOP_STATEFUL_MODE_CHECKPOINT
      })

    assert ckpt.checkpoint_token == "ckpt:vm-st1"
    assert ckpt.generation == 7
    assert ckpt.snapshot_ref == ""
    assert ckpt.size_bytes == 0

    # ResolveStateful COMMIT (phase two): publishes the temp as the bundle and
    # returns the ref, deriving it from the token so the client proves it crossed.
    {:ok, committed} =
      NodeService.Stub.resolve_stateful(ch, %ResolveStatefulRequest{
        vm_id: "vm-st1",
        checkpoint_token: "ckpt:vm-st1",
        mode: :RESOLVE_MODE_COMMIT
      })

    assert committed.snapshot_ref == "stateful/ckpt:vm-st1"
    assert committed.generation == 8
    assert committed.size_bytes == 16384

    # ResolveStateful ABORT: resumes the VM and publishes no bundle (empty result).
    {:ok, aborted} =
      NodeService.Stub.resolve_stateful(ch, %ResolveStatefulRequest{
        vm_id: "vm-st1",
        checkpoint_token: "ckpt:vm-st1",
        mode: :RESOLVE_MODE_ABORT
      })

    assert aborted.snapshot_ref == ""
    assert aborted.generation == 0
    assert aborted.size_bytes == 0

    # DeleteVolume: idempotent, and echoes the lineage that was deleted.
    assert {:ok, %Embervm.Node.V1.DeleteVolumeResponse{lineage_id: "lineage-1"}} =
             NodeService.Stub.delete_volume(ch, %DeleteVolumeRequest{workload: "scratch-postgres", lineage_id: "lineage-1"})
  end

  test "StartStateful honors blessed_generation and volume_device (R7 additive fields)", %{
    channel: ch
  } do
    # FRESH with blessed_generation set: the control-plane-issued generation wins
    # over the fake's legacy self-bump value of 1.
    {:ok, fresh_blessed} =
      NodeService.Stub.start_stateful(ch, %StartStatefulRequest{
        mode: :START_STATEFUL_MODE_FRESH,
        boot_image_ref: "pg-base",
        port: 5432,
        blessed_generation: 42
      })

    assert fresh_blessed.generation == 42

    # FRESH with blessed_generation unset (0): falls back to the legacy scripted
    # value, proving a pre-blessing daemon/CP pair still round-trips.
    {:ok, fresh_legacy} =
      NodeService.Stub.start_stateful(ch, %StartStatefulRequest{
        mode: :START_STATEFUL_MODE_FRESH,
        boot_image_ref: "pg-base",
        port: 5432
      })

    assert fresh_legacy.generation == 1

    # RELIGHT with blessed_generation set: same override on the warm-relight path.
    {:ok, relight_blessed} =
      NodeService.Stub.start_stateful(ch, %StartStatefulRequest{
        mode: :START_STATEFUL_MODE_RELIGHT,
        relight_snapshot_ref: "stateful/scratch-postgres",
        boot_image_ref: "pg-base",
        port: 5432,
        blessed_generation: 43
      })

    assert relight_blessed.generation == 43
    assert relight_blessed.was_relight == true

    # RELIGHT with blessed_generation unset (0): falls back to the legacy scripted
    # value of 7 for the warm-relight branch.
    {:ok, relight_legacy} =
      NodeService.Stub.start_stateful(ch, %StartStatefulRequest{
        mode: :START_STATEFUL_MODE_RELIGHT,
        relight_snapshot_ref: "stateful/scratch-postgres",
        boot_image_ref: "pg-base",
        port: 5432
      })

    assert relight_legacy.generation == 7

    # volume_device set (R7, Longhorn-native lane): the fake echoes it into vm_id
    # so the client can assert the device path crossed the wire.
    {:ok, device_set} =
      NodeService.Stub.start_stateful(ch, %StartStatefulRequest{
        mode: :START_STATEFUL_MODE_FRESH,
        boot_image_ref: "pg-base",
        port: 5432,
        volume_device: "/dev/longhorn/scratch-postgres"
      })

    assert device_set.vm_id == "vm:pg-base@/dev/longhorn/scratch-postgres"

    # volume_device empty (legacy vol.img lane): no suffix on vm_id.
    {:ok, device_unset} =
      NodeService.Stub.start_stateful(ch, %StartStatefulRequest{
        mode: :START_STATEFUL_MODE_FRESH,
        boot_image_ref: "pg-base",
        port: 5432
      })

    assert device_unset.vm_id == "vm:pg-base"
  end

  test "ResolveStatefulRequest.blessed_generation survives protobuf encode/decode (R7 abort-blessing fix)" do
    # Unlike the fields exercised above, blessed_generation on a RESOLVE_MODE_ABORT
    # request has no fake-server echo to observe crossing the wire (the fake's
    # ResolveStateful ABORT branch returns an empty response regardless of the
    # request). Prove the field itself survives protobuf wire encoding directly
    # against the generated struct's own Protobuf.Encoder/Decoder (no server, no
    # channel needed): this is the field-level guarantee the gRPC round trip above
    # otherwise relies on transitively.
    req = %ResolveStatefulRequest{
      trace: %Trace{workload: "scratch-postgres"},
      vm_id: "vm-st1",
      checkpoint_token: "ckpt:vm-st1",
      mode: :RESOLVE_MODE_ABORT,
      blessed_generation: 9
    }

    encoded = ResolveStatefulRequest.encode(req)
    decoded = ResolveStatefulRequest.decode(encoded)

    assert decoded.blessed_generation == 9
    assert decoded.mode == :RESOLVE_MODE_ABORT
    assert decoded.vm_id == "vm-st1"
    assert decoded.checkpoint_token == "ckpt:vm-st1"

    # A COMMIT request (blessed_generation unused, left at its zero default)
    # round-trips identically, proving the new field does not disturb the
    # existing COMMIT wire shape.
    commit_req = %ResolveStatefulRequest{
      vm_id: "vm-st1",
      checkpoint_token: "ckpt:vm-st1",
      mode: :RESOLVE_MODE_COMMIT
    }

    decoded_commit =
      commit_req
      |> ResolveStatefulRequest.encode()
      |> ResolveStatefulRequest.decode()

    assert decoded_commit.blessed_generation == 0
    assert decoded_commit.mode == :RESOLVE_MODE_COMMIT
  end

  test "group verbs round-trip across the wire (R5 additive contract)", %{channel: ch} do
    # CreateGroupNetwork: derives bridge_name/gateway_ip from the request, proving
    # the group_instance_id and cidr fields crossed the wire.
    {:ok, net} =
      NodeService.Stub.create_group_network(ch, %CreateGroupNetworkRequest{
        trace: %Trace{workload: "composite"},
        group_instance_id: "grp-inst1",
        cidr: "10.100.0.0/24"
      })

    assert net.bridge_name == "br-grp-inst1"
    assert net.gateway_ip == "10.100.0.1"

    # CreateGroupNetwork refusal: the fake scripts a CIDR-overlap FAILED_PRECONDITION
    # off the cidr content, proving the refusal status crosses the wire.
    {:error, refused} =
      NodeService.Stub.create_group_network(ch, %CreateGroupNetworkRequest{
        group_instance_id: "grp-inst2",
        cidr: "10.100.0.0/24-overlap"
      })

    assert refused.status == GRPC.Status.failed_precondition()

    # StartGroupMember FRESH: cold-boots from source, echoes the pinned ip, no
    # relight. Proves the mode enum, source, member identity, and env map cross.
    {:ok, fresh} =
      NodeService.Stub.start_group_member(ch, %StartGroupMemberRequest{
        trace: %Trace{workload: "composite"},
        mode: :START_GROUP_MEMBER_MODE_FRESH,
        group_instance_id: "grp-inst1",
        member_name: "worker-0",
        member_index: 0,
        ip: "10.100.0.10",
        source: "worker-base",
        health_port: 8080,
        resources: %ResourceSpec{vcpus: 1, mem_mib: 256},
        env: %{"EMBER_GROUP_SIZE" => "3"}
      })

    assert fresh.vm_id == "vm:worker-base"
    assert fresh.ip == "10.100.0.10"
    assert fresh.was_relight == false
    # No entry port and no budget on this request -> the R6 fields stay zero-valued.
    assert fresh.endpoint_ip == ""
    assert fresh.endpoint_port == 0

    # StartGroupMember FRESH, ENTRY member with a readiness budget (R6): proves
    # ready_budget_seconds crosses the wire (the fake folds it into vm_id) and the
    # daemon-side endpoint projection fields cross back (the F-fix lane: the CP
    # publishes the DAEMON's reported endpoint, so these must survive the wire).
    {:ok, entry_fresh} =
      NodeService.Stub.start_group_member(ch, %StartGroupMemberRequest{
        trace: %Trace{workload: "composite"},
        mode: :START_GROUP_MEMBER_MODE_FRESH,
        group_instance_id: "grp-inst1",
        member_name: "server",
        member_index: 0,
        ip: "10.100.0.10",
        source: "server-base",
        health_port: 6443,
        resources: %ResourceSpec{vcpus: 1, mem_mib: 256},
        env: %{},
        entry_guest_port: 6443,
        ready_budget_seconds: 180
      })

    assert entry_fresh.vm_id == "vm:server-base@180s"
    assert entry_fresh.endpoint_ip == "fakenode-pod"
    assert entry_fresh.endpoint_port == 36_443

    # StartGroupMember RELIGHT, verified: resumes warm (was_relight true) once the
    # clock-resync handshake verified within one second. The fake scripts a warm
    # relight for any ordinary ref.
    {:ok, warm} =
      NodeService.Stub.start_group_member(ch, %StartGroupMemberRequest{
        mode: :START_GROUP_MEMBER_MODE_RELIGHT,
        group_instance_id: "grp-inst1",
        member_name: "worker-0",
        member_index: 0,
        ip: "10.100.0.10",
        snapshot_ref: "group/set-abc/worker-0"
      })

    assert warm.vm_id == "vm:group/set-abc/worker-0"
    assert warm.ip == "10.100.0.10"
    assert warm.was_relight == true

    # StartGroupMember RELIGHT, clock-resync failure: the fake scripts a
    # FAILED_PRECONDITION off the ref content, proving the resync-failure status
    # crosses the wire (standing decision 7: a >1s delta fails the resume).
    {:error, clockfail} =
      NodeService.Stub.start_group_member(ch, %StartGroupMemberRequest{
        mode: :START_GROUP_MEMBER_MODE_RELIGHT,
        group_instance_id: "grp-inst1",
        member_name: "worker-0",
        ip: "10.100.0.10",
        snapshot_ref: "group/set-abc/worker-0-clockfail"
      })

    assert clockfail.status == GRPC.Status.failed_precondition()

    # StopGroupMember BANK: derives the per-member bundle ref from set_id/member_name.
    {:ok, banked} =
      NodeService.Stub.stop_group_member(ch, %StopGroupMemberRequest{
        vm_id: "vm-g1",
        mode: :STOP_GROUP_MEMBER_MODE_BANK,
        set_id: "set-abc",
        member_name: "worker-0"
      })

    assert banked.snapshot_ref == "group/set-abc/worker-0"
    assert banked.size_bytes == 5120

    # StopGroupMember DESTROY: no snapshot produced.
    {:ok, destroyed} =
      NodeService.Stub.stop_group_member(ch, %StopGroupMemberRequest{
        vm_id: "vm-g1",
        mode: :STOP_GROUP_MEMBER_MODE_DESTROY,
        set_id: "set-abc",
        member_name: "worker-0"
      })

    assert destroyed.snapshot_ref == ""
    assert destroyed.size_bytes == 0

    # DeleteGroupNetwork: idempotent, returns an empty response.
    assert {:ok, _} =
             NodeService.Stub.delete_group_network(ch, %DeleteGroupNetworkRequest{
               group_instance_id: "grp-inst1"
             })
  end

  test "NodeStatus reports stateful facts (R4 additive fields)", %{channel: ch} do
    {:ok, ns} = NodeService.Stub.get_node_status(ch, %GetNodeStatusRequest{node_id: "node-4"})

    assert [vm, ckpt_vm] = ns.stateful_vms
    assert vm.vm_id == "vm-st1"
    assert vm.workload == "scratch-postgres"
    assert vm.ip == "10.99.0.3"
    assert vm.port == 5432
    assert vm.healthy == true
    assert vm.generation == 5
    assert vm.last_probe_unix_ms == 1_700_000_003_000
    # A live VM is never checkpoint-pending.
    assert vm.checkpoint_pending == false
    assert vm.checkpoint_token == ""
    # origin (ADR embervm/018 Phase 2): a node-woken stateful VM reports ACTIVATOR
    # so the control plane adopts it (fenced-writer adoption trusts its generation).
    assert vm.origin == :INSTANCE_ORIGIN_ACTIVATOR

    # The second VM is PAUSED awaiting a resolve (ADR embervm/008): adoption reads
    # checkpoint_pending + the token to resolve a stranded checkpoint.
    assert ckpt_vm.vm_id == "vm-st2"
    assert ckpt_vm.workload == "demo-postgres"
    assert ckpt_vm.checkpoint_pending == true
    assert ckpt_vm.checkpoint_token == "ckpt:vm-st2"

    assert [bundle] = ns.stateful_bundles
    assert bundle.snapshot_ref == "stateful/scratch-postgres"
    assert bundle.workload == "scratch-postgres"
    assert bundle.generation == 5
    assert bundle.size_bytes == 16384
    assert bundle.created_at_unix_ms == 1_700_000_004_000

    assert [vol] = ns.volumes
    assert vol.workload == "scratch-postgres"
    assert vol.generation == 5
    assert vol.size_bytes == 10_737_418_240
    assert vol.allocated_bytes == 536_870_912
    assert vol.attached == true
  end

  test "NodeStatus reports group facts (R5 additive fields)", %{channel: ch} do
    {:ok, ns} = NodeService.Stub.get_node_status(ch, %GetNodeStatusRequest{node_id: "node-4"})

    assert [net] = ns.group_networks
    assert net.group_instance_id == "grp-inst1"
    assert net.cidr == "10.100.0.0/24"
    assert net.bridge == "br-grp-inst1"
    assert net.member_count == 2

    assert [vm] = ns.group_member_vms
    assert vm.vm_id == "vm-g1"
    assert vm.group_instance_id == "grp-inst1"
    assert vm.member_name == "worker-0"
    assert vm.ip == "10.100.0.10"
    assert vm.healthy == true
    assert vm.last_probe_unix_ms == 1_700_000_005_000

    # The bundle set is deliberately PARTIAL (one member ref), proving the daemon
    # reports refs grouped by set dir and leaves the completeness judgment to the
    # control plane.
    assert [set] = ns.group_bundle_sets
    assert set.set_id == "set-abc"
    assert set.group_instance_id == "grp-inst1"
    assert set.created_at_unix_ms == 1_700_000_006_000
    assert [member] = set.members
    assert member.member_name == "worker-0"
    assert member.snapshot_ref == "group/set-abc/worker-0"
    assert member.size_bytes == 5120
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
    # origin (ADR embervm/018 Fork A): the activator-woken VM reports ACTIVATOR so
    # the control plane adopts rather than orphan-destroys it.
    assert vm.origin == :INSTANCE_ORIGIN_ACTIVATOR

    assert [snap] = ns.serving_snapshots
    assert snap.snapshot_ref == "serving/s-srv2"
    assert snap.workload == "sandbox-serving"
    assert snap.size_bytes == 8192
    assert snap.created_at_unix_ms == 1_700_000_002_000

    assert ns.serving_subnet_cidr == "10.99.0.0/24"
  end

  test "NodeStatus advertises the node-local activator (ADR embervm/018 Fork A)", %{
    channel: ch
  } do
    {:ok, ns} = NodeService.Stub.get_node_status(ch, %GetNodeStatusRequest{node_id: "node-4"})

    # The L7 fallback endpoint EndpointPublisher prefers over the CP pod activator.
    assert ns.activator_endpoint.ip == "10.99.0.1"
    assert ns.activator_endpoint.port == 8081
    # The L4 activator ip (forward-compat; consumed by the stateful lane in a
    # later phase) crosses the wire now.
    assert ns.activator_ip == "10.99.0.1"
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

  test "continuity verbs round-trip across the wire (R6 additive contract)", %{channel: ch} do
    # ExportArtifact: first export of a stateful bundle records the store key and
    # reports bytes moved (derived from workload+ref length), not skipped.
    stateful = %ArtifactRef{
      kind: :ARTIFACT_KIND_STATEFUL,
      workload: "scratch-postgres",
      ref: "stateful/scratch-postgres"
    }

    {:ok, exp} =
      NodeService.Stub.export_artifact(ch, %ExportArtifactRequest{
        artifact: stateful,
        trace: %Trace{workload: "scratch-postgres"}
      })

    assert exp.skipped == false
    assert exp.bytes_moved > 0
    assert exp.generation == 0

    # A repeat export of the same key short-circuits skipped=true, no bytes moved.
    {:ok, again} =
      NodeService.Stub.export_artifact(ch, %ExportArtifactRequest{artifact: stateful})

    assert again.skipped == true
    assert again.bytes_moved == 0

    # A VOLUME export echoes the volume generation so the pairing fact crosses.
    volume = %ArtifactRef{kind: :ARTIFACT_KIND_VOLUME, workload: "scratch-postgres", ref: ""}
    {:ok, vol_exp} = NodeService.Stub.export_artifact(ch, %ExportArtifactRequest{artifact: volume})
    assert vol_exp.generation == 5

    # RestoreArtifact: the bundle is present in the store, so restore succeeds.
    {:ok, res} =
      NodeService.Stub.restore_artifact(ch, %RestoreArtifactRequest{artifact: stateful})

    assert res.bytes_moved > 0

    # RestoreArtifact of an absent key surfaces FAILED_PRECONDITION (never silent).
    absent = %ArtifactRef{kind: :ARTIFACT_KIND_SESSION, workload: "nope", ref: "sessions/none"}

    {:error, missing} =
      NodeService.Stub.restore_artifact(ch, %RestoreArtifactRequest{artifact: absent})

    assert missing.status == GRPC.Status.failed_precondition()

    # RestoreArtifact with vendor set (R7): a normal ref restores fine, proving
    # vendor crosses the wire without being misread as a mismatch.
    {:ok, vendor_ok} =
      NodeService.Stub.restore_artifact(ch, %RestoreArtifactRequest{
        artifact: stateful,
        vendor: "amd"
      })

    assert vendor_ok.bytes_moved > 0

    # RestoreArtifact vendor mismatch (R7, standing decision 1): the fake scripts
    # a FAILED_PRECONDITION off a "vendor-mismatch" ref when vendor is set, proving
    # a cross-vendor restore fails closed rather than silently cold-booting.
    mismatched = %ArtifactRef{
      kind: :ARTIFACT_KIND_STATEFUL,
      workload: "scratch-postgres",
      ref: "stateful/scratch-postgres-vendor-mismatch"
    }

    {:ok, _} = NodeService.Stub.export_artifact(ch, %ExportArtifactRequest{artifact: mismatched})

    {:error, vendor_mismatch} =
      NodeService.Stub.restore_artifact(ch, %RestoreArtifactRequest{
        artifact: mismatched,
        vendor: "intel"
      })

    assert vendor_mismatch.status == GRPC.Status.failed_precondition()

    # EvictArtifact(remote=true): removes the store copy, reports bytes freed.
    {:ok, ev} =
      NodeService.Stub.evict_artifact(ch, %EvictArtifactRequest{artifact: stateful, remote: true})

    assert ev.bytes_freed > 0

    # After eviction, a restore of the same key fails (the copy is gone).
    {:error, gone} =
      NodeService.Stub.restore_artifact(ch, %RestoreArtifactRequest{artifact: stateful})

    assert gone.status == GRPC.Status.failed_precondition()

    # EvictArtifact is idempotent on an already-absent artifact.
    {:ok, noop} =
      NodeService.Stub.evict_artifact(ch, %EvictArtifactRequest{artifact: stateful, remote: true})

    assert noop.bytes_freed == 0
  end

  test "NodeStatus reports continuity facts (R6 additive fields)", %{channel: ch} do
    {:ok, ns} = NodeService.Stub.get_node_status(ch, %GetNodeStatusRequest{node_id: "node-4"})

    assert ns.drain_deadline_unix_ms == 1_700_000_009_000
    assert ns.store_reachable == true

    assert [bundle] = ns.stateful_bundles
    assert bundle.exported == true

    assert [vol] = ns.volumes
    assert vol.exported_generation == 5

    assert [set] = ns.group_bundle_sets
    assert set.exported == true
  end

  test "NodeStatus reports distribution facts (R7 additive fields)", %{channel: ch} do
    {:ok, ns} = NodeService.Stub.get_node_status(ch, %GetNodeStatusRequest{node_id: "node-4"})

    assert ns.cpu_vendor == "amd"
    assert ns.node_template_hash == "tmpl-abc123"

    assert [vol] = ns.volumes
    assert vol.generation_blessed == true
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
