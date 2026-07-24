package server

import (
	"os"
	"path/filepath"
	"reflect"
	"testing"

	nodev1 "github.com/jomcgi/homelab/projects/embervm/proto/embervm/node/v1"
)

func entry(workload, digest, rootfs string) workloadEntry {
	return workloadEntry{Workload: workload, ImageDigest: digest, RootfsRef: rootfs, HarnessInit: "/init"}
}

func TestGroupByListenPortRequiresNodeLocalWakeAndPlan(t *testing.T) {
	r := newWorkloadRegistry("")
	group := entryFromProto(&nodev1.RegistryEntry{
		Workload:        "scratch-k8s",
		NodeLocalWake:   true,
		GroupListenPort: 5410,
		GroupMemberPlan: []*nodev1.GroupMemberPlanEntry{{
			MemberName:     "server",
			StartOrder:     0,
			HealthPort:     6443,
			EntryGuestPort: 6443,
			Sizing:         &nodev1.ResourceSpec{Vcpus: 2, MemMib: 1024},
		}},
	})
	r.sync([]workloadEntry{group})
	got, ok := r.groupByListenPort(5410)
	if !ok || got.Workload != "scratch-k8s" || len(got.GroupMemberPlan) != 1 {
		t.Fatalf("groupByListenPort = %+v, %v", got, ok)
	}
	if got.GroupMemberPlan[0].MemMib != 1024 || got.GroupMemberPlan[0].EntryGuestPort != 6443 {
		t.Errorf("group plan = %+v", got.GroupMemberPlan[0])
	}

	group.NodeLocalWake = false
	r.sync([]workloadEntry{group})
	if _, ok := r.groupByListenPort(5410); ok {
		t.Error("groupByListenPort accepted a workload without node_local_wake")
	}
}

// TestSyncConvergesDropsStaleEntry is the C1 converge-drops-stale-entry case: a
// SyncRegistry carrying a set that OMITS a previously-known workload drops it, so
// the table converges to EXACTLY the pushed set.
func TestSyncConvergesDropsStaleEntry(t *testing.T) {
	r := newWorkloadRegistry("")

	r.sync([]workloadEntry{entry("a", "d1", "/a"), entry("b", "d2", "/b")})
	if r.count() != 2 {
		t.Fatalf("after first sync count = %d, want 2", r.count())
	}

	// Second sync omits "b": it must be dropped, and "a" updated in place.
	n := r.sync([]workloadEntry{entry("a", "d1-new", "/a2")})
	if n != 1 || r.count() != 1 {
		t.Fatalf("after converge count = %d (returned %d), want 1", r.count(), n)
	}
	if _, ok := r.get("b"); ok {
		t.Error("stale entry b should be dropped after converge")
	}
	a, ok := r.get("a")
	if !ok || a.ImageDigest != "d1-new" || a.RootfsRef != "/a2" {
		t.Errorf("a not updated in place: %+v (ok=%v)", a, ok)
	}
}

// TestSyncIdempotentUnderReplay proves applying the same set twice is a no-op.
func TestSyncIdempotentUnderReplay(t *testing.T) {
	r := newWorkloadRegistry("")
	set := []workloadEntry{entry("a", "d1", "/a"), entry("b", "d2", "/b")}
	r.sync(set)
	first, _ := r.get("a")
	r.sync(set)
	second, _ := r.get("a")
	if !reflect.DeepEqual(first, second) || r.count() != 2 {
		t.Errorf("replay changed state: first=%+v second=%+v count=%d", first, second, r.count())
	}
}

// TestSyncClearsReadinessGate proves the readiness gate: unsynced until the first
// SyncRegistry, then synced.
func TestSyncClearsReadinessGate(t *testing.T) {
	r := newWorkloadRegistry("")
	if r.isSynced() {
		t.Fatal("registry should start unsynced (readiness gate closed)")
	}
	r.sync([]workloadEntry{entry("a", "d1", "/a")})
	if !r.isSynced() {
		t.Error("registry should be synced after first SyncRegistry")
	}
}

// TestRegisterDeregisterIncremental proves the incremental verbs mutate one entry
// each and are idempotent.
func TestRegisterDeregisterIncremental(t *testing.T) {
	r := newWorkloadRegistry("")
	r.register(entry("a", "d1", "/a"))
	if r.count() != 1 {
		t.Fatalf("after register count = %d, want 1", r.count())
	}
	// Register does NOT clear the readiness gate (only a full sync is the replay).
	if r.isSynced() {
		t.Error("register must not set synced; only SyncRegistry is the authoritative replay")
	}
	r.deregister("a")
	if r.count() != 0 {
		t.Errorf("after deregister count = %d, want 0", r.count())
	}
	// Idempotent on an absent workload.
	r.deregister("a")
	if r.count() != 0 {
		t.Errorf("deregister of absent workload changed count to %d", r.count())
	}
}

// TestRestoreServesWarmFromStaleCache is the C3 restart-serves-warm-from-stale
// case: a boot cache load populates the table STALE (serves existing warmth) and
// leaves the readiness gate CLOSED (admits no new work until a live sync).
func TestRestoreServesWarmFromStaleCache(t *testing.T) {
	path := filepath.Join(t.TempDir(), "registry.json")

	// First daemon lifetime: sync writes the cache.
	r1 := newWorkloadRegistry(path)
	r1.sync([]workloadEntry{entry("a", "d1", "/a"), entry("b", "d2", "/b")})

	// Restart: a fresh registry loads the cache.
	r2 := newWorkloadRegistry(path)
	if !r2.loadCache() {
		t.Fatal("loadCache should report a usable cache")
	}
	if r2.count() != 2 {
		t.Errorf("stale cache count = %d, want 2 (serves warmth)", r2.count())
	}
	if _, ok := r2.get("a"); !ok {
		t.Error("warm-cache workload a should be servable from the stale cache")
	}
	if !r2.isStale() {
		t.Error("a boot-cache load must be marked stale")
	}
	if r2.isSynced() {
		t.Error("a stale cache must NOT flip the readiness gate: no new work until a live sync")
	}
}

// TestLiveSyncClearsStale is the C3 live-sync-clears-stale case: the first live
// SyncRegistry after a stale boot clears the stale mark and opens the gate.
func TestLiveSyncClearsStale(t *testing.T) {
	path := filepath.Join(t.TempDir(), "registry.json")
	r1 := newWorkloadRegistry(path)
	r1.sync([]workloadEntry{entry("a", "d1", "/a")})

	r2 := newWorkloadRegistry(path)
	r2.loadCache()
	if !r2.isStale() {
		t.Fatal("precondition: r2 should be stale after cache load")
	}

	r2.sync([]workloadEntry{entry("a", "d1", "/a"), entry("c", "d3", "/c")})
	if r2.isStale() {
		t.Error("live sync must clear the stale mark")
	}
	if !r2.isSynced() {
		t.Error("live sync must open the readiness gate")
	}
	if r2.count() != 2 {
		t.Errorf("count after live sync = %d, want 2", r2.count())
	}
}

// TestCorruptCacheBootsEmpty is the C3 corrupt-cache-file-boots-empty case: a bad
// cache file NEVER crash-loops; it boots an empty, non-stale registry.
func TestCorruptCacheBootsEmpty(t *testing.T) {
	path := filepath.Join(t.TempDir(), "registry.json")
	if err := os.WriteFile(path, []byte("{ this is not valid json"), 0o600); err != nil {
		t.Fatal(err)
	}
	r := newWorkloadRegistry(path)
	if r.loadCache() {
		t.Error("loadCache should report no usable cache for a corrupt file")
	}
	if r.count() != 0 {
		t.Errorf("corrupt cache should boot empty, count = %d", r.count())
	}
	if r.isStale() {
		t.Error("corrupt cache should NOT be marked stale (nothing to serve)")
	}
	// And a subsequent live sync still works (rewrites a clean cache).
	r.sync([]workloadEntry{entry("a", "d1", "/a")})
	if r.count() != 1 || !r.isSynced() {
		t.Errorf("recovery sync failed: count=%d synced=%v", r.count(), r.isSynced())
	}
}

// TestMissingCacheBootsEmpty proves an absent cache file boots empty and
// non-stale (a first-ever boot).
func TestMissingCacheBootsEmpty(t *testing.T) {
	path := filepath.Join(t.TempDir(), "does-not-exist.json")
	r := newWorkloadRegistry(path)
	if r.loadCache() {
		t.Error("loadCache should report no cache for a missing file")
	}
	if r.count() != 0 || r.isStale() {
		t.Errorf("missing cache should boot empty and non-stale: count=%d stale=%v", r.count(), r.isStale())
	}
}

// TestPersistAtomicRoundTrip proves a synced table survives a marshal/reload with
// its fields intact (sizing carried through).
func TestPersistAtomicRoundTrip(t *testing.T) {
	path := filepath.Join(t.TempDir(), "registry.json")
	r1 := newWorkloadRegistry(path)
	want := workloadEntry{Workload: "a", ImageDigest: "d1", RootfsRef: "/a", HarnessInit: "/init", VCPUs: 2, MemMib: 512}
	r1.sync([]workloadEntry{want})

	r2 := newWorkloadRegistry(path)
	r2.loadCache()
	got, ok := r2.get("a")
	if !ok || !reflect.DeepEqual(got, want) {
		t.Errorf("round-trip mismatch: got %+v (ok=%v), want %+v", got, ok, want)
	}
}

// TestGetByImageRefSkipsEmptyRootfsUnderTagSkew is the demo-postgres cold-boot
// regression: under guest-image tag churn the control plane pushes a per-CR entry
// that matches by image_ref but whose rootfs_ref the identity map could not
// resolve (empty), alongside the synthetic "image:"-keyed identity entry that
// carries the base's real on-disk path. getByImageRef must NOT return the
// empty-rootfs entry (which would hand Firecracker an empty PUT /drives/rootfs
// path, "No such file or directory"); it must fall through to the entry with a
// real path so the churned tag still resolves to the base present on disk.
func TestGetByImageRefSkipsEmptyRootfsUnderTagSkew(t *testing.T) {
	r := newWorkloadRegistry("")
	// Order-independent: register the empty per-CR entry first so a naive
	// first-match would return it. Both entries carry the SAME image_ref (the
	// churned tag the daemon resolves a stateful cold boot against).
	r.sync([]workloadEntry{
		{Workload: "demo-postgres", ImageRef: "img-pg", RootfsRef: ""},
		{Workload: "image:img-pg", ImageRef: "img-pg", RootfsRef: "/rootfs/pg", HarnessInit: "/init"},
	})

	got, ok := r.getByImageRef("img-pg")
	if !ok {
		t.Fatal("getByImageRef found no entry for img-pg; want the real-path identity entry")
	}
	if got.RootfsRef == "" {
		t.Fatalf("getByImageRef returned an empty-rootfs entry (%+v); want the entry with a real path", got)
	}
	if got.RootfsRef != "/rootfs/pg" {
		t.Errorf("RootfsRef = %q, want /rootfs/pg", got.RootfsRef)
	}
}

// TestGetByImageRefEmptyOnlyDoesNotResolve proves that when the ONLY entry for a
// ref carries an empty rootfs (no provisioned base anywhere), getByImageRef
// resolves nothing rather than yielding an empty path. This keeps the cold-boot
// caller's "not provisioned" FailedPrecondition correct instead of failing later
// inside Firecracker.
func TestGetByImageRefEmptyOnlyDoesNotResolve(t *testing.T) {
	r := newWorkloadRegistry("")
	r.sync([]workloadEntry{
		{Workload: "demo-postgres", ImageRef: "img-pg", RootfsRef: ""},
	})

	if got, ok := r.getByImageRef("img-pg"); ok {
		t.Fatalf("getByImageRef resolved an empty-rootfs-only ref to %+v; want no resolution", got)
	}
}
