package server

import (
	"encoding/json"
	"os"
	"path/filepath"
	"sort"
	"sync"

	nodev1 "github.com/jomcgi/homelab/projects/embervm/proto/embervm/node/v1"
)

// workloadEntry is the daemon's in-memory copy of one pushed RegistryEntry: the
// node-side identity of a workload (image digest, rootfs ref, harness init,
// sizing) the control plane owns and pushes over SyncRegistry/RegisterWorkload.
// It is the artifact-decoupling replacement for the retired EMBERVM_NODED_IMAGES
// config table: the daemon boots with NO entries and admits no work until the
// control plane replays the authoritative set on (re)connect.
type workloadEntry struct {
	Workload    string `json:"workload"`
	ImageDigest string `json:"imageDigest"`
	// ImageRef is the OCI ref a BuildBase resolves against to find this entry's
	// node-side rootfs/harness (the join key the retired EMBERVM_NODED_IMAGES
	// table keyed on). getByImageRef indexes on it.
	ImageRef             string `json:"imageRef"`
	RootfsRef            string `json:"rootfsRef"`
	HarnessInit          string `json:"harnessInit"`
	VCPUs                uint32 `json:"vcpus"`
	MemMib               uint32 `json:"memMib"`
	NodeLocalWake        bool   `json:"nodeLocalWake"`
	ServingPort          uint32 `json:"servingPort"`
	ServingHealthPath    string `json:"servingHealthPath"`
	StatefulListenPort   uint32 `json:"statefulListenPort"`
	StatefulPort         uint32 `json:"statefulPort"`
	StatefulVolumeMount  string `json:"statefulVolumeMount"`
	StatefulBootImageRef string `json:"statefulBootImageRef"`
	StatefulVolumeDevice string `json:"statefulVolumeDevice"`
}

// entryFromProto lifts a wire RegistryEntry into the daemon's internal shape.
func entryFromProto(e *nodev1.RegistryEntry) workloadEntry {
	return workloadEntry{
		Workload:             e.GetWorkload(),
		ImageDigest:          e.GetImageDigest(),
		ImageRef:             e.GetImageRef(),
		RootfsRef:            e.GetRootfsRef(),
		HarnessInit:          e.GetHarnessInit(),
		VCPUs:                e.GetSizing().GetVcpus(),
		MemMib:               e.GetSizing().GetMemMib(),
		NodeLocalWake:        e.GetNodeLocalWake(),
		ServingPort:          e.GetServingPort(),
		ServingHealthPath:    e.GetServingHealthPath(),
		StatefulListenPort:   e.GetStatefulListenPort(),
		StatefulPort:         e.GetStatefulPort(),
		StatefulVolumeMount:  e.GetStatefulVolumeMount(),
		StatefulBootImageRef: e.GetStatefulBootImageRef(),
		StatefulVolumeDevice: e.GetStatefulVolumeDevice(),
	}
}

// workloadRegistry is the daemon's in-memory, control-plane-pushed workload
// table. It is fed exclusively by the registry verbs (SyncRegistry converges to
// exactly the pushed set; Register/Deregister mutate one entry) and is the
// authority for whether the daemon has received its registry replay yet
// (readiness gates on synced).
//
// ## the stale-cache seam (ADR embervm/012: never warm-to-dead)
//
// On boot the daemon LOADS the last-synced table from NVMe (loadCache) and marks
// it STALE. A stale table serves EXISTING warmth (workloads it already knew) so a
// noded restart does not evict the warm pool, but it must NEVER admit NEW work:
// readiness stays gated on the FIRST live SyncRegistry of the connection, which
// clears the stale mark. So a stale registry keeps the lights on for what was
// already primed while the control plane reconnects, and only a live sync from
// the current control plane re-opens the daemon for new placement.
type workloadRegistry struct {
	mu      sync.Mutex
	entries map[string]workloadEntry
	// synced is true once the daemon has applied at least one live SyncRegistry
	// this process lifetime (readiness gate). A cache load does NOT set it.
	synced bool
	// stale is true when the current entries came from a boot cache load and no
	// live SyncRegistry has arrived yet. A stale table serves warmth but admits no
	// new work; the first live sync clears it.
	stale bool
	// cachePath is where applied syncs are persisted (tmp+rename). Empty disables
	// persistence entirely (tests that do not exercise the cache).
	cachePath string
}

func newWorkloadRegistry(cachePath string) *workloadRegistry {
	return &workloadRegistry{
		entries:   make(map[string]workloadEntry),
		cachePath: cachePath,
	}
}

// sync converges the table to EXACTLY the pushed set: entries absent from the
// push are dropped, entries present are added-or-updated. It marks the registry
// synced and clears any stale mark (a live sync is the authority a stale cache
// was standing in for), then persists the new table. Idempotent under replay:
// applying the same set twice yields the same table and the same on-disk cache.
// Returns the resulting entry count.
func (r *workloadRegistry) sync(entries []workloadEntry) int {
	r.mu.Lock()
	next := make(map[string]workloadEntry, len(entries))
	for _, e := range entries {
		if e.Workload == "" {
			continue
		}
		next[e.Workload] = e
	}
	r.entries = next
	r.synced = true
	r.stale = false
	snapshot := r.snapshotLocked()
	path := r.cachePath
	r.mu.Unlock()

	persistCache(path, snapshot)
	return len(next)
}

// register adds-or-updates one entry incrementally. It does NOT clear the stale
// mark or set synced: an incremental register is not the authoritative full
// replay the readiness gate waits for (only SyncRegistry converges the whole
// set). It persists the updated table. Returns the resulting entry count. An
// empty workload name is ignored.
func (r *workloadRegistry) register(e workloadEntry) int {
	r.mu.Lock()
	if e.Workload != "" {
		r.entries[e.Workload] = e
	}
	n := len(r.entries)
	snapshot := r.snapshotLocked()
	path := r.cachePath
	r.mu.Unlock()

	persistCache(path, snapshot)
	return n
}

// deregister removes one entry by workload name. Idempotent on an absent name.
// It persists the updated table. Returns the resulting entry count.
func (r *workloadRegistry) deregister(workload string) int {
	r.mu.Lock()
	delete(r.entries, workload)
	n := len(r.entries)
	snapshot := r.snapshotLocked()
	path := r.cachePath
	r.mu.Unlock()

	persistCache(path, snapshot)
	return n
}

// get returns the entry for a workload and whether it is present.
func (r *workloadRegistry) get(workload string) (workloadEntry, bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	e, ok := r.entries[workload]
	return e, ok
}

// statefulByListenPort resolves the opaque-L4 activator identity. The local
// accept port is the only workload discriminator, so node_local_wake and a
// nonzero stateful listen port are both required.
func (r *workloadRegistry) statefulByListenPort(port uint32) (workloadEntry, bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	for _, e := range r.entries {
		if e.NodeLocalWake && e.StatefulListenPort == port {
			return e, true
		}
	}
	return workloadEntry{}, false
}

// getByImageRef returns the entry whose ImageRef matches imageRef (the BuildBase
// join). An empty imageRef never matches, so a caller that has no image_ref
// (e.g. the zip lane's runtime is resolved separately) falls through cleanly.
//
// An entry whose RootfsRef is EMPTY is not a valid resolution: it names no
// node-side rootfs, so returning it hands Firecracker an empty PUT /drives/rootfs
// path ("No such file or directory") at cold boot. This happens under tag skew:
// when a workload's base was cut under a guest-image tag that has since churned,
// the control plane pushes a per-CR entry that matches by image_ref but whose
// rootfs_ref the identity map could not resolve (empty). We therefore skip
// empty-RootfsRef matches and prefer the first entry that carries a real path
// (e.g. the synthetic "image:"-keyed identity entry for the same ref), so a
// churned tag still resolves to the base present on disk instead of failing.
func (r *workloadRegistry) getByImageRef(imageRef string) (workloadEntry, bool) {
	if imageRef == "" {
		return workloadEntry{}, false
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	for _, e := range r.entries {
		if e.ImageRef == imageRef && e.RootfsRef != "" {
			return e, true
		}
	}
	return workloadEntry{}, false
}

// isSynced reports whether a live SyncRegistry has been applied this process
// lifetime. The readiness gate reads it: the daemon is not ready (and traffic
// never reaches it) until the control plane has replayed the registry.
func (r *workloadRegistry) isSynced() bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.synced
}

// isStale reports whether the current table is a boot-cache load with no live
// sync yet. A stale table serves warmth but admits no new work.
func (r *workloadRegistry) isStale() bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.stale
}

// count is the current number of registry entries.
func (r *workloadRegistry) count() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return len(r.entries)
}

// snapshotLocked returns a deterministic (workload-sorted) copy of the table for
// persistence. Caller must hold r.mu.
func (r *workloadRegistry) snapshotLocked() []workloadEntry {
	out := make([]workloadEntry, 0, len(r.entries))
	for _, e := range r.entries {
		out = append(out, e)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Workload < out[j].Workload })
	return out
}

// loadCache reads the last-synced table from cachePath, populates the table, and
// marks it STALE (a boot load is warmth-only until the first live sync). It never
// crash-loops on a bad cache file: a missing file loads an empty registry, and a
// corrupt/undecodable file is treated exactly like a missing one (empty, not
// stale, logged by the caller). Called once at construction, before the daemon
// serves. Returns whether a usable cache was loaded (for the caller's log).
func (r *workloadRegistry) loadCache() bool {
	if r.cachePath == "" {
		return false
	}
	data, err := os.ReadFile(r.cachePath)
	if err != nil {
		// Missing (or unreadable) cache: boot with an empty, non-stale registry.
		return false
	}
	var entries []workloadEntry
	if err := json.Unmarshal(data, &entries); err != nil {
		// Corrupt cache: NEVER crash-loop. Boot empty and non-stale, exactly like
		// a missing file. The next live SyncRegistry rewrites a clean cache.
		return false
	}
	loaded := make(map[string]workloadEntry, len(entries))
	for _, e := range entries {
		if e.Workload == "" {
			continue
		}
		loaded[e.Workload] = e
	}
	r.mu.Lock()
	r.entries = loaded
	r.stale = len(loaded) > 0
	r.synced = false
	r.mu.Unlock()
	return len(loaded) > 0
}

// persistCache atomically writes the table to path (write tmp + rename), so a
// crash mid-write never leaves a torn cache a later boot would fail to parse.
// Best-effort: a write failure is swallowed (the cache is an optimization, never
// the source of truth; a lost cache just boots empty and waits for a live sync).
// An empty path disables persistence.
func persistCache(path string, entries []workloadEntry) {
	if path == "" {
		return
	}
	data, err := json.Marshal(entries)
	if err != nil {
		return
	}
	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return
	}
	tmp, err := os.CreateTemp(dir, ".registry-*.json.tmp")
	if err != nil {
		return
	}
	tmpName := tmp.Name()
	if _, err := tmp.Write(data); err != nil {
		_ = tmp.Close()
		_ = os.Remove(tmpName)
		return
	}
	if err := tmp.Close(); err != nil {
		_ = os.Remove(tmpName)
		return
	}
	if err := os.Rename(tmpName, path); err != nil {
		_ = os.Remove(tmpName)
	}
}
