package server

import (
	"sort"

	nodev1 "github.com/jomcgi/homelab/projects/embervm/proto/embervm/node/v1"
)

// ReadyBaseRootfsPaths returns the distinct rootfs images behind this daemon's
// READY bases, sorted. The startup jail preflight opens each through a jailed
// Firecracker before the daemon registers, so a base whose rootfs the jail
// cannot read keeps the brick from ever advertising it. The path is the one a
// restore uses: the base's rootfspath sidecar, else the registered path.
func (s *Server) ReadyBaseRootfsPaths() []string {
	seen := make(map[string]struct{})
	for _, base := range s.bases.snapshot() {
		if base.state != nodev1.BaseBuildState_BASE_BUILD_STATE_READY {
			continue
		}
		path := s.readBaseRootfsPath(base.snapshotRef)
		if path == "" {
			path = base.rootfsPath
		}
		if path != "" {
			seen[path] = struct{}{}
		}
	}
	paths := make([]string, 0, len(seen))
	for path := range seen {
		paths = append(paths, path)
	}
	sort.Strings(paths)
	return paths
}

// BrickMemBudgetMib is the memory budget this daemon reads from its brick's
// cgroup, the value NodeStatus reports. Startup refuses to register a classed
// brick that reads zero: it would be placed as an unbounded wildcard.
func (s *Server) BrickMemBudgetMib() uint64 {
	return s.memBudget()
}
