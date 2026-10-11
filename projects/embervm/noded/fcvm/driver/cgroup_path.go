package driver

import "path"

// DaemonCgroupName is the daemon-only child cgroup noded moves itself into when
// it delegates the memory controller to jailed VM leaves. It is exported so the
// server's budget reader resolves the same delegation root the launcher builds,
// instead of keeping a second copy of the name that could silently drift.
const DaemonCgroupName = "embervm-noded-daemon"

// BrickCgroupPath maps noded's own cgroup v2 path (the "0::<path>" entry of
// /proc/self/cgroup) to the brick's container cgroup: noded's own cgroup before
// jailer delegation, and its parent once noded has moved into DaemonCgroupName.
// The container cgroup is the delegation root: its memory.max is the brick's
// ceiling, and its memory.current and memory.stat are hierarchical over the
// daemon child and every VM leaf. An empty path stays empty so callers keep
// their own fallback.
func BrickCgroupPath(rel string) string {
	if rel == "" {
		return ""
	}
	clean := path.Clean(rel)
	if path.Base(clean) != DaemonCgroupName {
		return rel
	}
	parent := path.Dir(clean)
	if parent == "." {
		// A relative self path names the namespace root's child.
		return "/"
	}
	return parent
}
