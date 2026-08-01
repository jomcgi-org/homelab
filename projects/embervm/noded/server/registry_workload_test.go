package server

import (
	"context"
	"os"
	"path/filepath"
	"reflect"
	"testing"

	"github.com/jomcgi/homelab/projects/embervm/noded/volume"
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
			MemberName:         "server",
			StartOrder:         0,
			HealthPort:         6443,
			EntryGuestPort:     6443,
			ReadyBudgetSeconds: 180,
			Sizing:             &nodev1.ResourceSpec{Vcpus: 2, MemMib: 1024},
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
	if got.GroupMemberPlan[0].ReadyBudgetSeconds != 180 {
		t.Errorf("group ready budget = %d, want 180", got.GroupMemberPlan[0].ReadyBudgetSeconds)
	}

	group.NodeLocalWake = false
	r.sync([]workloadEntry{group})
	if _, ok := r.groupByListenPort(5410); ok {
		t.Error("groupByListenPort accepted a workload without node_local_wake")
	}
}

func TestSyncRegistryStoresControlPlaneActivator(t *testing.T) {
	_, s := newTestServer(t, &fakeDriver{}, &fakeTransport{}, 8)
	if _, err := s.SyncRegistry(context.Background(), &nodev1.SyncRegistryRequest{
		ControlPlaneActivatorIp: "10.42.0.19",
	}); err != nil {
		t.Fatalf("SyncRegistry: %v", err)
	}
	if got := s.registry.controlPlaneActivator(); got != "10.42.0.19" {
		t.Errorf("control-plane activator = %q, want %q", got, "10.42.0.19")
	}
	if _, err := s.SyncRegistry(context.Background(), &nodev1.SyncRegistryRequest{}); err != nil {
		t.Fatalf("SyncRegistry(empty activator): %v", err)
	}
	if got := s.registry.controlPlaneActivator(); got != "" {
		t.Errorf("control-plane activator after empty sync = %q, want empty", got)
	}
}

// newLeaseTestServer is newTestServer plus a real volume manager: New only
// wires s.volumes when a VolumeRoot is configured, and the plain harness
// configures none, so lease delivery (SyncRegistry -> ApplyBlessingLease)
// would dereference a nil manager.
func newLeaseTestServer(t *testing.T) (nodev1.NodeServiceClient, *Server) {
	t.Helper()
	c, s := newTestServer(t, &fakeDriver{}, &fakeTransport{}, 8)
	s.volumes = volume.NewManager(filepath.Join(t.TempDir(), "volumes"))
	if err := s.volumes.Create("demo-pg", 1<<20); err != nil {
		t.Fatalf("Create volume: %v", err)
	}
	return c, s
}

// syncBlessingLease mirrors the wire shape the control plane emits: it sets
// WorkloadName and NodeLocalWake because the CP does, but neither is
// load-bearing for delivery (SyncRegistry keys on entry.Workload and applies
// leases regardless of the wake mode).
func syncBlessingLease(t *testing.T, c nodev1.NodeServiceClient, nextGeneration, leaseEnd uint64) {
	t.Helper()
	_, err := c.SyncRegistry(context.Background(), &nodev1.SyncRegistryRequest{
		Entries: []*nodev1.RegistryEntry{{
			Workload:      "demo-pg",
			NodeLocalWake: true,
			BlessingLeases: []*nodev1.BlessingLease{{
				WorkloadName:   "demo-pg",
				NextGeneration: nextGeneration,
				LeaseEnd:       leaseEnd,
			}},
		}},
	})
	if err != nil {
		t.Fatalf("SyncRegistry lease [%d, %d): %v", nextGeneration, leaseEnd, err)
	}
}

// TestSyncRegistryDeliversBlessingLeaseToVolumeManager pins the delivery hop
// itself: a lease pushed through the real proto path (bufconn client ->
// entryFromProto -> SyncRegistry -> ApplyBlessingLease) lands on disk, and an
// activator attach consumes it as blessed with a cursor that persists across
// attaches. If any link in that chain is cut, the first attach self-bumps from
// a fresh ledger and every assertion here fails.
func TestSyncRegistryDeliversBlessingLeaseToVolumeManager(t *testing.T) {
	c, s := newLeaseTestServer(t)

	syncBlessingLease(t, c, 7, 12)
	gen, err := s.attachGeneration("demo-pg", 0, true)
	if err != nil {
		t.Fatalf("first activator attach: %v", err)
	}
	if gen != 7 {
		t.Fatalf("first activator generation = %d, want 7", gen)
	}
	if !s.volumes.GenerationBlessed("demo-pg") {
		t.Fatal("first activator attach should be blessed")
	}

	gen, err = s.attachGeneration("demo-pg", 0, true)
	if err != nil {
		t.Fatalf("second activator attach: %v", err)
	}
	if gen != 8 {
		t.Errorf("second activator generation = %d, want 8", gen)
	}
}

// TestSyncRegistryLeaseReplayNeverRewindsIssuedGeneration pins the no-rewind
// property END TO END, across the three lease shapes a brick can receive:
// an identical replay and a stale narrower range leave the persisted cursor
// alone (the ApplyBlessingLease guard), while the control plane's actual
// re-grant shape (start BEHIND the brick's cursor with a larger lease_end,
// since append_blessing_lease derives start from the lag-prone reported
// generation) is accepted and DOES rewind the persisted cursor. What can
// never rewind is the issued generation: the ledger clamp in
// ConsumeGenerationFromLease is the only thing standing between a rewound
// cursor and double-issuance, so the final leg here is the end-to-end pin of
// that clamp.
func TestSyncRegistryLeaseReplayNeverRewindsIssuedGeneration(t *testing.T) {
	c, s := newLeaseTestServer(t)

	syncBlessingLease(t, c, 7, 12)
	for want := uint64(7); want <= 8; want++ {
		gen, err := s.attachGeneration("demo-pg", 0, true)
		if err != nil {
			t.Fatalf("activator attach %d: %v", want, err)
		}
		if gen != want {
			t.Fatalf("activator attach %d = %d, want %d", want, gen, want)
		}
	}

	syncBlessingLease(t, c, 7, 12)
	gen, err := s.attachGeneration("demo-pg", 0, true)
	if err != nil {
		t.Fatalf("replayed lease attach: %v", err)
	}
	if gen != 9 {
		t.Fatalf("replayed lease attach = %d, want 9", gen)
	}

	syncBlessingLease(t, c, 5, 10)
	next, err := s.attachGeneration("demo-pg", 0, true)
	if err != nil {
		t.Fatalf("stale lease attach: %v", err)
	}
	if next <= gen {
		t.Fatalf("stale lease rewound generation from %d to %d", gen, next)
	}
	if next != 10 {
		t.Errorf("stale lease attach = %d, want 10: the persisted range keeps the original end (the cursor sits at [10, 12) here), never adopting the stale one", next)
	}
	// The discriminating assertion: with the ApplyBlessingLease guard removed
	// the stale leg still yields 10 via an unblessable self-bump, so only this
	// blessed check separates a leased issuance from the fallback.
	if !s.volumes.GenerationBlessed("demo-pg") {
		t.Fatal("stale lease attach should remain blessed")
	}

	// The CP re-grant shape: start behind the brick's cursor, larger end. The
	// guard accepts it (the LeaseEnd half fails on an extension) and the
	// persisted cursor rewinds to 7, but the ledger clamp must advance past
	// the local generation (10), so the attach issues 11 and stays blessed.
	// Without the clamp, RecordBlessed(7) fails against the ledger and this
	// exact leg degrades to an unblessable self-bump.
	syncBlessingLease(t, c, 7, 62)
	regrant, err := s.attachGeneration("demo-pg", 0, true)
	if err != nil {
		t.Fatalf("re-grant lease attach: %v", err)
	}
	if regrant != 11 {
		t.Fatalf("re-grant lease attach = %d, want the clamp to advance past the ledger to 11", regrant)
	}
	if !s.volumes.GenerationBlessed("demo-pg") {
		t.Fatal("re-grant lease attach should be blessed, not a clamp-bypassing self-bump")
	}
}

// TestSyncRegistryLeaseRenewalExtendsRange pins renewal through the same
// delivery hop: a fresh range past the consumed one applies (only the
// NextGeneration half of the ApplyBlessingLease guard passes) and the next
// activator attach issues from it, blessed.
func TestSyncRegistryLeaseRenewalExtendsRange(t *testing.T) {
	c, s := newLeaseTestServer(t)

	syncBlessingLease(t, c, 7, 12)
	for want := uint64(7); want < 12; want++ {
		gen, err := s.attachGeneration("demo-pg", 0, true)
		if err != nil {
			t.Fatalf("initial lease attach %d: %v", want, err)
		}
		if gen != want {
			t.Fatalf("initial lease attach %d = %d, want %d", want, gen, want)
		}
	}

	syncBlessingLease(t, c, 12, 62)
	gen, err := s.attachGeneration("demo-pg", 0, true)
	if err != nil {
		t.Fatalf("renewed lease attach: %v", err)
	}
	if gen != 12 {
		t.Fatalf("renewed lease generation = %d, want 12", gen)
	}
	if !s.volumes.GenerationBlessed("demo-pg") {
		t.Fatal("renewed lease attach should be blessed")
	}
}

// TestActivatorAttachFallsBackToSelfBumpOnExhaustedLease pins the noded half
// of fencing by non-renewal: past lease_end the activator lane degrades to an
// UNBLESSABLE self-bump, so an unrenewed lease stops blessing.
func TestActivatorAttachFallsBackToSelfBumpOnExhaustedLease(t *testing.T) {
	c, s := newLeaseTestServer(t)

	syncBlessingLease(t, c, 7, 9)
	for want := uint64(7); want <= 8; want++ {
		gen, err := s.attachGeneration("demo-pg", 0, true)
		if err != nil {
			t.Fatalf("leased activator attach %d: %v", want, err)
		}
		if gen != want {
			t.Fatalf("leased activator attach %d = %d, want %d", want, gen, want)
		}
		if !s.volumes.GenerationBlessed("demo-pg") {
			t.Fatalf("leased activator attach %d should be blessed", want)
		}
	}

	gen, err := s.attachGeneration("demo-pg", 0, true)
	if err != nil {
		t.Fatalf("exhausted lease attach: %v", err)
	}
	if gen != 9 {
		t.Fatalf("exhausted lease generation = %d, want self-bump 9", gen)
	}
	// The discriminating assertion: the self-bump also lands on 9 (the lease
	// end), so the generation check alone cannot separate a leased issuance
	// from the fallback; only this blessed check can.
	if s.volumes.GenerationBlessed("demo-pg") {
		t.Fatal("self-bump after lease exhaustion must be unblessable")
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
