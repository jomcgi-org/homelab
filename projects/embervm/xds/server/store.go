// Package server wires the localhost snapshot API and the ADS gRPC server around
// a go-control-plane SnapshotCache. The cache is the single source of truth the
// ADS server streams from; the HTTP API is the only writer.
package server

import (
	"context"
	"fmt"
	"sync"

	cachev3 "github.com/envoyproxy/go-control-plane/pkg/cache/v3"

	"github.com/jomcgi/homelab/projects/embervm/xds/snapshot"
)

// Store owns the SnapshotCache and enforces per-node version monotonicity. It is
// the only writer to the cache; the ADS server holds the same cache read-side.
//
// The cache is empty at boot and serves nothing until the first successful PUT,
// which is deliberate: the sidecar holds no durable state and makes no decisions,
// so a fresh (or restarted) sidecar advertises no config until the control plane
// re-pushes. If the control-plane container is down, node Envoys keep their
// last-ACKed config (xDS is eventually consistent), so an empty cache never tears
// down live routing.
type Store struct {
	cache cachev3.SnapshotCache

	mu sync.Mutex
	// lastAccepted records the version string last successfully applied per Envoy
	// node id, so a PUT carrying a lower-or-equal version is rejected. Versions are
	// caller-supplied and monotonic (the control plane's own counter); rejecting a
	// non-increasing version stops a stale re-push (e.g. a retried older request)
	// from clobbering a newer snapshot.
	lastAccepted map[string]string
}

// NewStore builds a Store over an ADS-mode SnapshotCache. ads=true means all
// resource types are served over one aggregated stream in a single, ordered
// snapshot, which is what the node Envoy's ADS bootstrap requires.
func NewStore() *Store {
	return &Store{
		cache:        cachev3.NewSnapshotCache(true, cachev3.IDHash{}, nil),
		lastAccepted: make(map[string]string),
	}
}

// Cache exposes the underlying SnapshotCache for the ADS server to stream from.
func (s *Store) Cache() cachev3.SnapshotCache {
	return s.cache
}

// versionError is returned when a PUT carries a version that is not strictly
// greater than the last accepted version for that node. The HTTP layer maps it to
// a 409 Conflict (the push is well-formed but out of order), distinct from a 400
// for a malformed document.
type versionError struct {
	node    string
	got     string
	current string
}

func (e *versionError) Error() string {
	return fmt.Sprintf("version %q for node %q is not greater than current %q", e.got, e.node, e.current)
}

// IsVersionConflict reports whether err is a monotonicity rejection.
func IsVersionConflict(err error) bool {
	_, ok := err.(*versionError)
	return ok
}

// Apply validates and installs a desired-state document for one Envoy node,
// enforcing strict version monotonicity and snapshot consistency before the swap.
// On success the new snapshot is served atomically to that node's ADS stream; on
// any error the previously served snapshot is left untouched.
func (s *Store) Apply(ctx context.Context, node string, d *snapshot.Desired) error {
	snap, err := snapshot.Build(d)
	if err != nil {
		return err
	}
	// NOTE: go-control-plane's Snapshot.Consistent() is deliberately NOT called
	// here. It requires the set of served RouteConfigurations to exactly equal the
	// set referenced by listeners' RDS. The static HTTP listener that references
	// this sidecar's RouteConfig lives in the node Envoy bootstrap, NOT in the
	// snapshot, and the R4 LDS listeners we DO serve are tcp_proxy (a direct
	// cluster reference, no RDS), so nothing in the snapshot references the
	// RouteConfig and Consistent() would always fail with a spurious RDS mismatch.
	// The route->cluster, cluster->EDS, and listener->cluster references this
	// sidecar owns are validated in snapshot.Build (snapshot.validate), which is
	// the correct check for this CDS/RDS/EDS + tcp-proxy-LDS surface.

	s.mu.Lock()
	defer s.mu.Unlock()

	current := s.lastAccepted[node]
	if current != "" && d.Version <= current {
		return &versionError{node: node, got: d.Version, current: current}
	}

	if err := s.cache.SetSnapshot(ctx, node, snap); err != nil {
		return fmt.Errorf("set snapshot for node %q: %w", node, err)
	}
	s.lastAccepted[node] = d.Version
	return nil
}

// CurrentVersion returns the version string currently served for a node, and
// whether any snapshot has been applied. Used by the debug GET handler.
func (s *Store) CurrentVersion(node string) (string, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	v, ok := s.lastAccepted[node]
	return v, ok
}
