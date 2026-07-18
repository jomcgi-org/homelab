package server

import (
	"context"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"strconv"
	"sync"
	"testing"
	"time"

	nodev1 "github.com/jomcgi/homelab/projects/embervm/proto/embervm/node/v1"

	"github.com/jomcgi/homelab/projects/embervm/noded/config"
)

// fakeStore is an in-memory artifactStore for the R6 server tests: it records
// every artifact export (prefix -> the exported meta) so tests assert an export
// fired, and models restore/evict/present without touching the network. It
// satisfies the server package's artifactStore seam, so the Server holds it
// exactly as it would a real *store.Store.
type fakeStore struct {
	mu sync.Mutex
	// arts maps a store prefix to its exported files (name -> content) and gen.
	arts map[string]fakeArtifact
	// exportCalls counts Export invocations per prefix (skipped or not).
	exportCalls map[string]int
	// reachable is what Reachable reports.
	reachable bool
	// order records prefixes in export order (for asserting meta-last is N/A here;
	// we assert on the fact of export, not object ordering, since the fake stores
	// whole artifacts atomically).
	order []string
}

type fakeArtifact struct {
	files map[string]string
	gen   uint64
}

func newFakeStore() *fakeStore {
	return &fakeStore{
		arts:        make(map[string]fakeArtifact),
		exportCalls: make(map[string]int),
		reachable:   true,
	}
}

func (f *fakeStore) Export(_ context.Context, prefix, localDir string, files []string, generation uint64, _ int64) (int64, bool, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.exportCalls[prefix]++
	// Read the local files into memory, mirroring the real client's read-then-put.
	got := make(map[string]string, len(files))
	var total int64
	for _, name := range files {
		b, err := os.ReadFile(filepath.Join(localDir, name))
		if err != nil {
			return 0, false, err
		}
		got[name] = string(b)
		total += int64(len(b))
	}
	// Idempotency: if the stored content is identical, skip.
	if existing, ok := f.arts[prefix]; ok && sameStringMap(existing.files, got) {
		return 0, true, nil
	}
	f.arts[prefix] = fakeArtifact{files: got, gen: generation}
	f.order = append(f.order, prefix)
	return total, false, nil
}

func (f *fakeStore) Restore(_ context.Context, prefix, localDir string) (int64, uint64, error) {
	f.mu.Lock()
	art, ok := f.arts[prefix]
	f.mu.Unlock()
	if !ok {
		return 0, 0, errFakeNotPresent
	}
	if err := os.MkdirAll(localDir, 0o700); err != nil {
		return 0, 0, err
	}
	var total int64
	for name, content := range art.files {
		dst := filepath.Join(localDir, name)
		if err := os.MkdirAll(filepath.Dir(dst), 0o700); err != nil {
			return 0, 0, err
		}
		if err := os.WriteFile(dst, []byte(content), 0o600); err != nil {
			return 0, 0, err
		}
		total += int64(len(content))
	}
	return total, art.gen, nil
}

func (f *fakeStore) DeleteArtifact(_ context.Context, prefix string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	delete(f.arts, prefix)
	return nil
}

func (f *fakeStore) Present(_ context.Context, prefix string) (bool, uint64, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	art, ok := f.arts[prefix]
	if !ok {
		return false, 0, nil
	}
	return true, art.gen, nil
}

func (f *fakeStore) Reachable(_ context.Context) bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.reachable
}

func (f *fakeStore) calls(prefix string) int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.exportCalls[prefix]
}

func (f *fakeStore) has(prefix string) bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	_, ok := f.arts[prefix]
	return ok
}

// errFakeNotPresent stands in for store.ErrNotPresent in these in-package tests
// (the server maps any Restore error to FAILED_PRECONDITION regardless of which
// sentinel it is).
var errFakeNotPresent = errFake("not present")

type errFake string

func (e errFake) Error() string { return string(e) }

func sameStringMap(a, b map[string]string) bool {
	if len(a) != len(b) {
		return false
	}
	for k, v := range a {
		if b[k] != v {
			return false
		}
	}
	return true
}

// newStoreTestServer builds a Server with the fake store wired and a real
// on-disk SnapshotRoot/VolumeRoot temp layout, so artifact enumeration reads
// genuine files. No driver networking is needed: the R6 handlers and export
// queue read the disk directly.
func newStoreTestServer(t *testing.T, fs *fakeStore) *Server {
	t.Helper()
	root := t.TempDir()
	volRoot := t.TempDir()
	s := New(Options{
		Config:    config.Config{Arch: "amd64", Node: "node-4", SnapshotRoot: root, VolumeRoot: volRoot},
		Driver:    &fakeDriver{},
		Transport: &fakeTransport{},
		Store:     fs,
		Logger:    slog.New(slog.NewTextHandler(io.Discard, nil)),
	})
	s.memHeadroom = func() uint64 { return 0 }
	return s
}

// writeBundle writes a fake bundle dir (snapfile + memfile + optional sidecars)
// under the given kind's local dir, so enumeration finds a complete artifact.
func writeBundleFiles(t *testing.T, dir string, files map[string]string) {
	t.Helper()
	if err := os.MkdirAll(dir, 0o700); err != nil {
		t.Fatalf("mkdir %s: %v", dir, err)
	}
	for name, content := range files {
		if err := os.WriteFile(filepath.Join(dir, name), []byte(content), 0o600); err != nil {
			t.Fatalf("write %s: %v", name, err)
		}
	}
}

// waitForExport polls until the fake store holds the prefix or the deadline
// passes (the export queue is async).
func waitForExport(t *testing.T, fs *fakeStore, prefix string) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if fs.has(prefix) {
			return
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatalf("export of %q did not land within the deadline", prefix)
}

// TestExportArtifactStateful proves a direct ExportArtifact of a banked stateful
// bundle uploads its files and reports it exported in NodeStatus.
func TestExportArtifactStateful(t *testing.T) {
	fs := newFakeStore()
	s := newStoreTestServer(t, fs)
	ctx := context.Background()

	ref := "state-abc"
	dir := filepath.Join(s.cfg.SnapshotRoot, "stateful", ref)
	writeBundleFiles(t, dir, map[string]string{"snapfile": "snap", "memfile": "mem", "gen": "3"})
	s.statefulBundles.add(statefulBundleEntry{snapshotRef: ref, workload: "scratch-postgres", generation: 3})

	resp, err := s.ExportArtifact(ctx, &nodev1.ExportArtifactRequest{
		Artifact: &nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_STATEFUL, Workload: "scratch-postgres", Ref: ref},
	})
	if err != nil {
		t.Fatalf("ExportArtifact: %v", err)
	}
	if resp.GetSkipped() {
		t.Fatal("first export should not be skipped")
	}
	if resp.GetBytesMoved() == 0 {
		t.Fatal("export moved 0 bytes")
	}
	prefix := "stateful/scratch-postgres/state-abc"
	if !fs.has(prefix) {
		t.Fatalf("store missing %q after export", prefix)
	}
	// NodeStatus reports the bundle exported.
	for _, b := range s.nodeStatus().GetStatefulBundles() {
		if b.GetSnapshotRef() == ref && !b.GetExported() {
			t.Fatal("stateful bundle should report exported=true")
		}
	}
}

// TestExportArtifactMissingLocalFails proves ExportArtifact refuses
// FAILED_PRECONDITION when the local artifact is absent.
func TestExportArtifactMissingLocalFails(t *testing.T) {
	fs := newFakeStore()
	s := newStoreTestServer(t, fs)
	_, err := s.ExportArtifact(context.Background(), &nodev1.ExportArtifactRequest{
		Artifact: &nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_STATEFUL, Workload: "nope", Ref: "gone"},
	})
	if err == nil {
		t.Fatal("export of an absent artifact should fail")
	}
}

// TestExportArtifactStoreDisabled proves the verb refuses when no store is
// configured (nil).
func TestExportArtifactStoreDisabled(t *testing.T) {
	root := t.TempDir()
	s := New(Options{
		Config:    config.Config{Arch: "amd64", Node: "node-4", SnapshotRoot: root},
		Driver:    &fakeDriver{},
		Transport: &fakeTransport{},
		Logger:    slog.New(slog.NewTextHandler(io.Discard, nil)),
	})
	_, err := s.ExportArtifact(context.Background(), &nodev1.ExportArtifactRequest{
		Artifact: &nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_STATEFUL, Workload: "w", Ref: "r"},
	})
	if err == nil {
		t.Fatal("export with no store should fail FAILED_PRECONDITION")
	}
}

// TestBankCommitTriggersExport proves a session Bank enqueues an async export
// that lands in the store (the export-after-commit path), via the internal
// enqueue plus the worker pool.
func TestBankCommitTriggersExport(t *testing.T) {
	fs := newFakeStore()
	s := newStoreTestServer(t, fs)
	ctx := context.Background()
	s.startExportQueue(ctx)

	ref := "sess-1"
	dir := filepath.Join(s.cfg.SnapshotRoot, "sessions", ref)
	writeBundleFiles(t, dir, map[string]string{"snapfile": "s", "memfile": "m"})

	// Simulate the tail of Bank: enqueue the export for the banked bundle.
	s.enqueueExport(&nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_SESSION, Workload: "sandbox-session", Ref: ref})
	waitForExport(t, fs, "session/sandbox-session/sess-1")
}

// TestVolumeExportSkipsUnchangedGeneration proves a second volume export at the
// same generation is skipped (the gen-unchanged short-circuit).
func TestVolumeExportSkipsUnchangedGeneration(t *testing.T) {
	fs := newFakeStore()
	s := newStoreTestServer(t, fs)
	ctx := context.Background()

	// Create a volume at generation 5.
	if err := s.volumes.Create("scratch-postgres", 1<<20); err != nil {
		t.Fatalf("create volume: %v", err)
	}
	for i := 0; i < 5; i++ {
		if _, err := s.volumes.BumpGeneration("scratch-postgres"); err != nil {
			t.Fatalf("bump: %v", err)
		}
	}
	volRef := &nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_VOLUME, Workload: "scratch-postgres"}

	// First export lands.
	if _, err := s.ExportArtifact(ctx, &nodev1.ExportArtifactRequest{Artifact: volRef}); err != nil {
		t.Fatalf("first volume export: %v", err)
	}
	prefix := "volume/scratch-postgres"
	firstCalls := fs.calls(prefix)
	if firstCalls != 1 {
		t.Fatalf("first export call count = %d, want 1", firstCalls)
	}

	// Enqueue an async re-export at the SAME generation: the local short-circuit
	// (exported cache generation == current) drops it without an Export call.
	s.startExportQueue(ctx)
	s.enqueueExport(volRef)
	// Give the worker a moment; the call count must NOT increase.
	time.Sleep(100 * time.Millisecond)
	if got := fs.calls(prefix); got != firstCalls {
		t.Fatalf("gen-unchanged re-export issued %d extra Export calls, want 0", got-firstCalls)
	}
}

// TestRestoreArtifactRoundTrip proves RestoreArtifact fetches a stateful bundle
// AND a volume pair back onto disk and re-registers the bundle.
func TestRestoreArtifactRoundTrip(t *testing.T) {
	fs := newFakeStore()
	s := newStoreTestServer(t, fs)
	ctx := context.Background()

	// Seed the store with a stateful bundle and a volume by exporting them from a
	// throwaway source layout.
	bundleRef := "state-xyz"
	srcBundle := filepath.Join(s.cfg.SnapshotRoot, "stateful", bundleRef)
	writeBundleFiles(t, srcBundle, map[string]string{"snapfile": "snapdata", "memfile": "memdata", "gen": "4"})
	s.statefulBundles.add(statefulBundleEntry{snapshotRef: bundleRef, workload: "scratch-postgres", generation: 4})
	if _, err := s.ExportArtifact(ctx, &nodev1.ExportArtifactRequest{
		Artifact: &nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_STATEFUL, Workload: "scratch-postgres", Ref: bundleRef},
	}); err != nil {
		t.Fatalf("seed bundle export: %v", err)
	}
	if err := s.volumes.Create("scratch-postgres", 1<<20); err != nil {
		t.Fatalf("create volume: %v", err)
	}
	if _, err := s.volumes.BumpGeneration("scratch-postgres"); err != nil {
		t.Fatalf("bump: %v", err)
	}
	if _, err := s.ExportArtifact(ctx, &nodev1.ExportArtifactRequest{
		Artifact: &nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_VOLUME, Workload: "scratch-postgres"},
	}); err != nil {
		t.Fatalf("seed volume export: %v", err)
	}

	// Wipe local disk copies to force a real restore.
	if err := os.RemoveAll(srcBundle); err != nil {
		t.Fatalf("rm bundle: %v", err)
	}
	s.statefulBundles.remove(bundleRef)
	if err := os.RemoveAll(filepath.Dir(s.volumes.VolumePath("scratch-postgres"))); err != nil {
		t.Fatalf("rm volume: %v", err)
	}

	// Restore the bundle.
	resp, err := s.RestoreArtifact(ctx, &nodev1.RestoreArtifactRequest{
		Artifact: &nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_STATEFUL, Workload: "scratch-postgres", Ref: bundleRef},
	})
	if err != nil {
		t.Fatalf("restore bundle: %v", err)
	}
	if resp.GetGeneration() != 4 {
		t.Fatalf("restored bundle gen = %d, want 4", resp.GetGeneration())
	}
	if got, _ := os.ReadFile(filepath.Join(srcBundle, "snapfile")); string(got) != "snapdata" {
		t.Fatalf("restored snapfile = %q, want snapdata", got)
	}
	// The reconcile re-registered the bundle so a rescan sees it.
	if _, ok := s.statefulBundles.get(bundleRef); !ok {
		t.Fatal("restored stateful bundle not re-registered")
	}

	// Restore the volume pair.
	if _, err := s.RestoreArtifact(ctx, &nodev1.RestoreArtifactRequest{
		Artifact: &nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_VOLUME, Workload: "scratch-postgres"},
	}); err != nil {
		t.Fatalf("restore volume: %v", err)
	}
	if !s.volumes.Exists("scratch-postgres") {
		t.Fatal("restored volume file missing")
	}
	gen, err := s.volumes.Generation("scratch-postgres")
	if err != nil || gen != 1 {
		t.Fatalf("restored volume generation = (%d, %v), want (1, nil)", gen, err)
	}
}

// TestRestoreArtifactAbsentFails proves RestoreArtifact refuses
// FAILED_PRECONDITION when the store copy is absent.
func TestRestoreArtifactAbsentFails(t *testing.T) {
	fs := newFakeStore()
	s := newStoreTestServer(t, fs)
	_, err := s.RestoreArtifact(context.Background(), &nodev1.RestoreArtifactRequest{
		Artifact: &nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_STATEFUL, Workload: "w", Ref: "missing"},
	})
	if err == nil {
		t.Fatal("restore of an absent store copy should fail")
	}
}

// TestEvictArtifactRemote proves EvictArtifact(remote=true) removes the store
// copy and is idempotent.
func TestEvictArtifactRemote(t *testing.T) {
	fs := newFakeStore()
	s := newStoreTestServer(t, fs)
	ctx := context.Background()

	ref := "serv-1"
	dir := filepath.Join(s.cfg.SnapshotRoot, "serving", ref)
	writeBundleFiles(t, dir, map[string]string{"snapfile": "s", "memfile": "m"})
	s.servingSnap.add(servingSnapshotEntry{snapshotRef: ref, workload: "hot-image-demo"})
	if _, err := s.ExportArtifact(ctx, &nodev1.ExportArtifactRequest{
		Artifact: &nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_SERVING, Workload: "hot-image-demo", Ref: ref},
	}); err != nil {
		t.Fatalf("export: %v", err)
	}
	prefix := "serving/hot-image-demo/serv-1"
	if !fs.has(prefix) {
		t.Fatal("store missing artifact before evict")
	}
	if _, err := s.EvictArtifact(ctx, &nodev1.EvictArtifactRequest{
		Artifact: &nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_SERVING, Workload: "hot-image-demo", Ref: ref},
		Remote:   true,
	}); err != nil {
		t.Fatalf("evict remote: %v", err)
	}
	if fs.has(prefix) {
		t.Fatal("store still holds artifact after remote evict")
	}
	// Idempotent.
	if _, err := s.EvictArtifact(ctx, &nodev1.EvictArtifactRequest{
		Artifact: &nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_SERVING, Workload: "hot-image-demo", Ref: ref},
		Remote:   true,
	}); err != nil {
		t.Fatalf("second evict should be idempotent: %v", err)
	}
}

// TestEvictArtifactVolumePairingGuard proves a volume evict is refused while its
// current generation still pairs with a banked local bundle (standing decision 8).
func TestEvictArtifactVolumePairingGuard(t *testing.T) {
	fs := newFakeStore()
	s := newStoreTestServer(t, fs)
	ctx := context.Background()

	if err := s.volumes.Create("scratch-postgres", 1<<20); err != nil {
		t.Fatalf("create volume: %v", err)
	}
	gen, err := s.volumes.BumpGeneration("scratch-postgres")
	if err != nil {
		t.Fatalf("bump: %v", err)
	}
	// A banked bundle stamped with the CURRENT generation: the pair is live.
	s.statefulBundles.add(statefulBundleEntry{snapshotRef: "b1", workload: "scratch-postgres", generation: gen})

	_, err = s.EvictArtifact(ctx, &nodev1.EvictArtifactRequest{
		Artifact: &nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_VOLUME, Workload: "scratch-postgres"},
		Remote:   true,
	})
	if err == nil {
		t.Fatal("evicting a volume still paired with a banked bundle should be refused")
	}

	// After the bundle is gone, the evict is allowed.
	s.statefulBundles.remove("b1")
	if _, err := s.EvictArtifact(ctx, &nodev1.EvictArtifactRequest{
		Artifact: &nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_VOLUME, Workload: "scratch-postgres"},
		Remote:   true,
	}); err != nil {
		t.Fatalf("evict after unpairing should succeed: %v", err)
	}
}

// TestDrainingDoesNotBlockOnExportQueue proves a full export queue never blocks
// the enqueue path (fire-and-forget): even with no workers draining it, many
// enqueues return promptly rather than deadlocking a drain.
func TestDrainingDoesNotBlockOnExportQueue(t *testing.T) {
	fs := newFakeStore()
	s := newStoreTestServer(t, fs)
	// Start the queue with workers, then flood far past its depth. The enqueue
	// must never block even when the queue is saturated (it drops instead).
	s.startExportQueue(context.Background())

	done := make(chan struct{})
	go func() {
		for i := 0; i < exportQueueDepth*4; i++ {
			s.enqueueExport(&nodev1.ArtifactRef{
				Kind:     nodev1.ArtifactKind_ARTIFACT_KIND_SESSION,
				Workload: "w",
				Ref:      "flood-" + strconv.Itoa(i),
			})
		}
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("enqueue flood blocked; the export queue must never stall the caller")
	}
}
