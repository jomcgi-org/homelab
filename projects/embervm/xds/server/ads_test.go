package server

import (
	"context"
	"net"
	"testing"
	"time"

	corev3 "github.com/envoyproxy/go-control-plane/envoy/config/core/v3"
	discoveryv3 "github.com/envoyproxy/go-control-plane/envoy/service/discovery/v3"
	resourcev3 "github.com/envoyproxy/go-control-plane/pkg/resource/v3"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/test/bufconn"

	"github.com/jomcgi/homelab/projects/embervm/xds/snapshot"
)

// TestADS_smokeServesPushedCluster is an in-process, Envoy-less ADS smoke: it
// stands up the real ADS server over an in-memory bufconn, pushes a snapshot via
// the store (as the HTTP API would), opens an aggregated discovery stream, and
// confirms the server responds to a CDS subscription with the pushed cluster. It
// exercises the full server wiring (SnapshotCache -> go-control-plane server ->
// ADS stream) without a firecracker VM or an Envoy binary.
func TestADS_smokeServesPushedCluster(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	store := NewStore()
	if err := store.Apply(ctx, "test-node", &snapshot.Desired{
		Version:  "0000000001",
		Clusters: []snapshot.Cluster{{Name: "c1", Endpoints: []snapshot.Endpoint{{IP: "10.0.0.1", Port: 80}}}},
		Routes:   []snapshot.Route{{Host: "h", Cluster: "c1"}},
	}); err != nil {
		t.Fatalf("seed snapshot: %v", err)
	}

	lis := bufconn.Listen(1 << 20)
	gs := grpc.NewServer()
	RegisterADS(ctx, gs, store)
	go func() { _ = gs.Serve(lis) }()
	defer gs.Stop()

	conn, err := grpc.NewClient(
		"passthrough:///bufnet",
		grpc.WithContextDialer(func(ctx context.Context, _ string) (net.Conn, error) {
			return lis.DialContext(ctx)
		}),
		grpc.WithTransportCredentials(insecure.NewCredentials()),
	)
	if err != nil {
		t.Fatalf("dial bufconn: %v", err)
	}
	defer conn.Close()

	client := discoveryv3.NewAggregatedDiscoveryServiceClient(conn)
	stream, err := client.StreamAggregatedResources(ctx)
	if err != nil {
		t.Fatalf("open ADS stream: %v", err)
	}

	// Subscribe to CDS on the aggregated stream; the node id must match the
	// snapshot key (IDHash serves per node.id).
	if err := stream.Send(&discoveryv3.DiscoveryRequest{
		Node:    &corev3.Node{Id: "test-node"},
		TypeUrl: resourcev3.ClusterType,
	}); err != nil {
		t.Fatalf("send CDS request: %v", err)
	}

	resp, err := stream.Recv()
	if err != nil {
		t.Fatalf("recv CDS response: %v", err)
	}
	if resp.GetTypeUrl() != resourcev3.ClusterType {
		t.Fatalf("response type = %q, want CDS", resp.GetTypeUrl())
	}
	if len(resp.GetResources()) != 1 {
		t.Fatalf("want 1 CDS resource, got %d", len(resp.GetResources()))
	}
	if resp.GetVersionInfo() != "0000000001" {
		t.Errorf("version info = %q, want 0000000001", resp.GetVersionInfo())
	}
}
