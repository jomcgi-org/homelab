package server

import (
	"context"

	discoveryv3 "github.com/envoyproxy/go-control-plane/envoy/service/discovery/v3"
	serverv3 "github.com/envoyproxy/go-control-plane/pkg/server/v3"
	"google.golang.org/grpc"
)

// RegisterADS attaches a SotW ADS server backed by the store's SnapshotCache to a
// gRPC server. Node Envoys connect here (over the pod-network xDS port exposed on
// the embervm Service) and stream CDS/RDS/EDS off the aggregated discovery
// service. The server holds no state of its own: it is a read view over the cache
// the HTTP API writes.
//
// Callbacks are the zero-value no-op (CallbackFuncs{}): the sidecar does not gate
// or mutate discovery requests, it only serves whatever snapshot the control
// plane last pushed. ADS mode (SnapshotCache built with ads=true) guarantees the
// three resource types are delivered on one ordered stream, so Envoy applies a
// consistent cluster+endpoint+route set together.
func RegisterADS(ctx context.Context, gs *grpc.Server, store *Store) {
	srv := serverv3.NewServer(ctx, store.Cache(), serverv3.CallbackFuncs{})
	discoveryv3.RegisterAggregatedDiscoveryServiceServer(gs, srv)
}
