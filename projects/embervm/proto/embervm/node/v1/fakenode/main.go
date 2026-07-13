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

func (fakeServer) GetNodeStatus(_ context.Context, req *nodev1.GetNodeStatusRequest) (*nodev1.NodeStatus, error) {
	return &nodev1.NodeStatus{
		NodeId:     req.GetNodeId(),
		LiveVms:    1,
		MaxLiveVms: 10,
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
