// Command fakenode is a minimal in-memory NodeService gRPC server used only by
// the cross-language round-trip test (//projects/embervm/control:node_roundtrip_test).
// It proves the Go-generated server stubs and the Elixir-generated client stubs
// interoperate on the wire, including the server-streaming WatchNode RPC. No
// Firecracker, no real VM lifecycle: every handler returns deterministic,
// request-derived data so the Elixir client can assert genuine round-trip
// fidelity (echoed fields prove the request crossed the wire intact).
//
// It listens on 127.0.0.1 on an OS-chosen ephemeral port and prints "PORT=<n>"
// to stdout so the test harness can read the port and connect. It serves until
// the process is killed (the test owns its lifetime via an OS port/pipe).
package main

import (
	"context"
	"fmt"
	"net"
	"os"
	"strings"

	nodev1 "github.com/jomcgi/homelab/projects/embervm/proto/embervm/node/v1"
	"google.golang.org/grpc"
)

// watchNodeHeartbeats is how many NodeStatus messages WatchNode streams before
// completing. The test asserts it receives exactly this many, in order.
const watchNodeHeartbeats = 3

type fakeServer struct {
	nodev1.UnimplementedNodeServiceServer
}

func (fakeServer) BuildBase(_ context.Context, req *nodev1.BuildBaseRequest) (*nodev1.BuildBaseResponse, error) {
	// Echo image_ref into snapshot_ref so the client can prove the request field
	// arrived, and reflect the requested memory shape back.
	return &nodev1.BuildBaseResponse{
		SnapshotRef:   "snap:" + req.GetImageRef(),
		ImageDigest:   "sha256:" + req.GetWorkloadRevision(),
		BaseSizeBytes: uint64(req.GetResources().GetMemMib()) * 1024 * 1024,
		Arch:          "amd64",
		AlreadyBuilt:  false,
	}, nil
}

func (fakeServer) Prime(_ context.Context, req *nodev1.PrimeRequest) (*nodev1.PrimeResponse, error) {
	return &nodev1.PrimeResponse{VmId: "vm:" + req.GetSnapshotRef()}, nil
}

func (fakeServer) Assign(_ context.Context, req *nodev1.AssignRequest) (*nodev1.AssignResponse, error) {
	// Echo the guest request body and path back so the client can assert the full
	// HTTP-semantics payload survived the round trip.
	return &nodev1.AssignResponse{
		Response: &nodev1.GuestResponse{
			StatusCode: 200,
			Headers:    map[string]string{"x-echo-path": req.GetRequest().GetPath()},
			Body:       req.GetRequest().GetBody(),
		},
		Usage: &nodev1.UsageStats{CpuMs: 1, PeakRssMib: 2, WallMs: 3},
	}, nil
}

func (fakeServer) Destroy(_ context.Context, _ *nodev1.DestroyRequest) (*nodev1.DestroyResponse, error) {
	return &nodev1.DestroyResponse{}, nil
}

// SessionAssign echoes the request body and path back like Assign, but proves
// deliver-without-destroy by echoing the session_id it was handed into a
// response header the client asserts on (the VM "survives", so the session id
// is still meaningful). suspect is left false (a clean round trip).
func (fakeServer) SessionAssign(_ context.Context, req *nodev1.SessionAssignRequest) (*nodev1.SessionAssignResponse, error) {
	return &nodev1.SessionAssignResponse{
		Response: &nodev1.GuestResponse{
			StatusCode: 200,
			Headers: map[string]string{
				"x-echo-path":  req.GetRequest().GetPath(),
				"x-session-id": req.GetSessionId(),
			},
			Body: req.GetRequest().GetBody(),
		},
		Usage:   &nodev1.UsageStats{CpuMs: 4, PeakRssMib: 5, WallMs: 6},
		Suspect: false,
	}, nil
}

// Bank derives a snapshot_ref from the session_id so the client can prove the
// request field crossed the wire, and returns a fixed size.
func (fakeServer) Bank(_ context.Context, req *nodev1.BankRequest) (*nodev1.BankResponse, error) {
	return &nodev1.BankResponse{
		SnapshotRef: "sessions/" + req.GetSessionId(),
		SizeBytes:   2048,
	}, nil
}

// Relight derives a vm_id from the snapshot_ref so the client can prove the
// restore request crossed the wire intact.
func (fakeServer) Relight(_ context.Context, req *nodev1.RelightRequest) (*nodev1.RelightResponse, error) {
	return &nodev1.RelightResponse{VmId: "vm:" + req.GetSnapshotRef()}, nil
}

// EvictSnapshot is idempotent and returns an empty response for any ref.
func (fakeServer) EvictSnapshot(_ context.Context, _ *nodev1.EvictSnapshotRequest) (*nodev1.EvictSnapshotResponse, error) {
	return &nodev1.EvictSnapshotResponse{}, nil
}

// StartServing derives vm_id and ip from whichever source ref was set (fresh or
// relight) so the client can prove both oneof branches cross the wire, and
// echoes the requested port back.
func (fakeServer) StartServing(_ context.Context, req *nodev1.StartServingRequest) (*nodev1.StartServingResponse, error) {
	ref := req.GetFresh().GetServingImageRef()
	if ref == "" {
		ref = req.GetRelight().GetSnapshotRef()
	}
	return &nodev1.StartServingResponse{
		VmId: "vm:" + ref,
		Ip:   "10.99.0.1",
		Port: req.GetPort(),
	}, nil
}

// StopServing returns a snapshot_ref/size_bytes for BANK, and zero values for
// DESTROY, so the client can assert both mode branches.
func (fakeServer) StopServing(_ context.Context, req *nodev1.StopServingRequest) (*nodev1.StopServingResponse, error) {
	if req.GetMode() == nodev1.StopServingMode_STOP_SERVING_MODE_BANK {
		return &nodev1.StopServingResponse{
			SnapshotRef: "serving/" + req.GetVmId(),
			SizeBytes:   3072,
		}, nil
	}
	return &nodev1.StopServingResponse{}, nil
}

// StartStateful derives its response from mode so the client can assert every
// stateful boot path crosses the wire. RELIGHT scripts the pairing outcome off
// the relight_snapshot_ref content ("mismatch" -> generation_mismatch cold-boot
// fallback, "noledger" -> ledger_unreadable, otherwise a warm relight), which is
// how the round-trip test exercises each pairing branch against a stateless fake.
// FRESH/COLD cold-boot from boot_image_ref and report a bumped generation.
func (fakeServer) StartStateful(_ context.Context, req *nodev1.StartStatefulRequest) (*nodev1.StartStatefulResponse, error) {
	if req.GetMode() == nodev1.StartStatefulMode_START_STATEFUL_MODE_RELIGHT {
		ref := req.GetRelightSnapshotRef()
		switch {
		case strings.Contains(ref, "mismatch"):
			return &nodev1.StartStatefulResponse{
				VmId: "vm:" + ref, Ip: "10.99.0.3", Port: req.GetPort(),
				Generation: 8, WasRelight: false, ColdBootReason: "generation_mismatch",
			}, nil
		case strings.Contains(ref, "noledger"):
			return &nodev1.StartStatefulResponse{
				VmId: "vm:" + ref, Ip: "10.99.0.3", Port: req.GetPort(),
				Generation: 8, WasRelight: false, ColdBootReason: "ledger_unreadable",
			}, nil
		default:
			return &nodev1.StartStatefulResponse{
				VmId: "vm:" + ref, Ip: "10.99.0.3", Port: req.GetPort(),
				Generation: 7, WasRelight: true, ColdBootReason: "",
			}, nil
		}
	}
	// FRESH or COLD: cold boot from the boot image ref, generation bumped.
	return &nodev1.StartStatefulResponse{
		VmId: "vm:" + req.GetBootImageRef(), Ip: "10.99.0.3", Port: req.GetPort(),
		Generation: 1, WasRelight: false, ColdBootReason: "",
	}, nil
}

// StopStateful returns a bundle ref, stamped generation, and size for BANK, and
// zero values for DESTROY, so the client can assert both mode branches.
func (fakeServer) StopStateful(_ context.Context, req *nodev1.StopStatefulRequest) (*nodev1.StopStatefulResponse, error) {
	if req.GetMode() == nodev1.StopStatefulMode_STOP_STATEFUL_MODE_BANK {
		return &nodev1.StopStatefulResponse{
			SnapshotRef: "stateful/" + req.GetVmId(),
			Generation:  7,
			SizeBytes:   16384,
		}, nil
	}
	return &nodev1.StopStatefulResponse{}, nil
}

// DeleteVolume is idempotent and returns an empty response for any workload.
func (fakeServer) DeleteVolume(_ context.Context, _ *nodev1.DeleteVolumeRequest) (*nodev1.DeleteVolumeResponse, error) {
	return &nodev1.DeleteVolumeResponse{}, nil
}

func (fakeServer) GetNodeStatus(_ context.Context, req *nodev1.GetNodeStatusRequest) (*nodev1.NodeStatus, error) {
	return &nodev1.NodeStatus{
		NodeId:     req.GetNodeId(),
		LiveVms:    1,
		MaxLiveVms: 10,
		// Session facts (R2): deterministic, so the client can assert the new
		// repeated/scalar status fields round-trip.
		SessionVms: []*nodev1.SessionVm{
			{VmId: "vm-s1", SessionId: "s-sess1", Workload: "sandbox-session"},
		},
		SessionSnapshots: []*nodev1.SessionSnapshot{
			{
				SnapshotRef:     "sessions/s-sess2",
				SessionId:       "s-sess2",
				Workload:        "sandbox-session",
				SizeBytes:       4096,
				CreatedAtUnixMs: 1_700_000_000_000,
			},
		},
		SnapshotDiskFreeBytes: 9_000_000_000,
		SnapshotDiskUsedBytes: 1_000_000_000,
		// Serving facts (R3): deterministic, so the client can assert the new
		// repeated/scalar status fields round-trip.
		ServingVms: []*nodev1.ServingVm{
			{
				VmId:            "vm-srv1",
				Workload:        "sandbox-serving",
				Ip:              "10.99.0.2",
				Port:            8080,
				Healthy:         true,
				LastProbeUnixMs: 1_700_000_001_000,
			},
		},
		ServingSnapshots: []*nodev1.ServingSnapshot{
			{
				SnapshotRef:     "serving/s-srv2",
				Workload:        "sandbox-serving",
				SizeBytes:       8192,
				CreatedAtUnixMs: 1_700_000_002_000,
			},
		},
		ServingSubnetCidr: "10.99.0.0/24",
		// Stateful facts (R4): deterministic, so the client can assert the new
		// repeated stateful status fields (VMs with generations, the one banked
		// bundle, and the durable volume) round-trip.
		StatefulVms: []*nodev1.StatefulVm{
			{
				VmId:            "vm-st1",
				Workload:        "scratch-postgres",
				Ip:              "10.99.0.3",
				Port:            5432,
				Healthy:         true,
				Generation:      5,
				LastProbeUnixMs: 1_700_000_003_000,
			},
		},
		StatefulBundles: []*nodev1.StatefulBundle{
			{
				SnapshotRef:     "stateful/scratch-postgres",
				Workload:        "scratch-postgres",
				Generation:      5,
				SizeBytes:       16384,
				CreatedAtUnixMs: 1_700_000_004_000,
			},
		},
		Volumes: []*nodev1.Volume{
			{
				Workload:       "scratch-postgres",
				Generation:     5,
				SizeBytes:      10_737_418_240,
				AllocatedBytes: 536_870_912,
				Attached:       true,
			},
		},
	}, nil
}

func (fakeServer) WatchNode(req *nodev1.WatchNodeRequest, stream grpc.ServerStreamingServer[nodev1.NodeStatus]) error {
	// Stream a fixed number of heartbeats with an incrementing live_vms counter so
	// the client can assert both the count and the ordering, then complete.
	for i := uint32(0); i < watchNodeHeartbeats; i++ {
		if err := stream.Send(&nodev1.NodeStatus{NodeId: req.GetNodeId(), LiveVms: i}); err != nil {
			return err
		}
	}
	return nil
}

func main() {
	lis, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		fmt.Fprintf(os.Stderr, "fakenode: listen: %v\n", err)
		os.Exit(1)
	}
	srv := grpc.NewServer()
	nodev1.RegisterNodeServiceServer(srv, fakeServer{})

	// Announce the chosen port on stdout (unbuffered) so the harness can connect.
	fmt.Printf("PORT=%d\n", lis.Addr().(*net.TCPAddr).Port)

	if err := srv.Serve(lis); err != nil {
		fmt.Fprintf(os.Stderr, "fakenode: serve: %v\n", err)
		os.Exit(1)
	}
}
