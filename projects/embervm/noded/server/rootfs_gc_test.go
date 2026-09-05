package server

import (
	"context"
	"encoding/hex"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	nodev1 "github.com/jomcgi/homelab/projects/embervm/proto/embervm/node/v1"

	"github.com/jomcgi/homelab/projects/embervm/noded/config"
)

const (
	testRootfsUUIDA = "550e8400-e29b-41d4-a716-446655440000"
	testRootfsUUIDB = "6ba7b810-9dad-11d1-80b4-00c04fd430c8"
)

func writeExt4Rootfs(t *testing.T, dir, name, uuid string) string {
	t.Helper()
	if err := os.MkdirAll(dir, 0o700); err != nil {
		t.Fatalf("mkdir %s: %v", dir, err)
	}
	rawUUID, err := hex.DecodeString(strings.ReplaceAll(uuid, "-", ""))
	if err != nil || len(rawUUID) != 16 {
		t.Fatalf("decode test UUID %q: %v", uuid, err)
	}
	data := make([]byte, 0x48c)
	data[ext4MagicOffset] = 0x53
	data[ext4MagicOffset+1] = 0xef
	copy(data[ext4UUIDOffset:], rawUUID)
	path := filepath.Join(dir, name)
	if err := os.WriteFile(path, data, 0o600); err != nil {
		t.Fatalf("write %s: %v", path, err)
	}
	return path
}

// writeRootfs creates a rootfs file with a given age, mirroring what the
// rootfs-builder bakes.
func writeRootfs(t *testing.T, dir, name string, age time.Duration) string {
	t.Helper()
	if err := os.MkdirAll(dir, 0o700); err != nil {
		t.Fatalf("mkdir %s: %v", dir, err)
	}
	path := writeExt4Rootfs(t, dir, name, testRootfsUUIDA)
	when := time.Now().Add(-age)
	if err := os.Chtimes(path, when, when); err != nil {
		t.Fatalf("chtimes %s: %v", path, err)
	}
	return path
}

func TestExt4UUID_Happy(t *testing.T) {
	path := writeExt4Rootfs(t, t.TempDir(), "rootfs.ext4", testRootfsUUIDA)
	got, err := ext4UUID(path)
	if err != nil {
		t.Fatalf("ext4UUID: %v", err)
	}
	if got != testRootfsUUIDA {
		t.Fatalf("ext4UUID = %q, want %q", got, testRootfsUUIDA)
	}
}

func TestExt4UUID_ShortFile(t *testing.T) {
	path := filepath.Join(t.TempDir(), "short.ext4")
	if err := os.WriteFile(path, make([]byte, ext4MagicOffset), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := ext4UUID(path); err == nil {
		t.Fatal("ext4UUID accepted a short file")
	}
}

func TestExt4UUID_BadMagic(t *testing.T) {
	path := filepath.Join(t.TempDir(), "not-ext4.img")
	if err := os.WriteFile(path, make([]byte, 0x48c), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := ext4UUID(path); err == nil || !strings.Contains(err.Error(), "magic") {
		t.Fatalf("ext4UUID bad magic error = %v", err)
	}
}

func TestWriteAndReadBaseRootfsID(t *testing.T) {
	s := newStoreTestServer(t, newFakeStore())
	baseKey := "echo__rootfsid"
	if err := os.MkdirAll(filepath.Join(s.cfg.SnapshotRoot, "bases", baseKey), 0o700); err != nil {
		t.Fatal(err)
	}
	rootfs := writeExt4Rootfs(t, t.TempDir(), "rootfs.ext4", testRootfsUUIDA)
	if err := s.writeBaseRootfsID(baseKey, rootfs); err != nil {
		t.Fatalf("writeBaseRootfsID: %v", err)
	}
	if got := s.readBaseRootfsID(baseKey); got != testRootfsUUIDA {
		t.Fatalf("readBaseRootfsID = %q, want %q", got, testRootfsUUIDA)
	}
}

func TestBuildBasesFailWhenRootfsNotExt4(t *testing.T) {
	snapshotRoot := t.TempDir()
	rootfs := filepath.Join(t.TempDir(), "rootfs.img")
	if err := os.WriteFile(rootfs, make([]byte, ext4HeaderSize), 0o600); err != nil {
		t.Fatal(err)
	}
	baseKey := baseKeyFor("echo", "img:1", "r1", "", "not-an-ext4-rootfs")
	writeBundleFiles(t, filepath.Join(snapshotRoot, "bases", baseKey), map[string]string{
		"memfile":  "mem",
		"snapfile": "snap",
	})
	s := New(Options{
		Config: config.Config{
			Arch: "amd64", Node: "node-4", SnapshotRoot: snapshotRoot,
			BootReadyTimeout: time.Second,
			Images:           map[string]config.Image{"img:1": {RootfsPath: rootfs}},
		},
		Driver:         &fakeDriver{},
		Transport:      &fakeTransport{},
		NewBuildDriver: func(BuildDriverSpec) BuildDriver { return &fakeDriver{} },
		Logger:         slog.New(slog.NewTextHandler(io.Discard, nil)),
	})

	_, err := s.BuildBase(context.Background(), &nodev1.BuildBaseRequest{
		Trace:            &nodev1.Trace{Workload: "echo"},
		ImageRef:         "img:1",
		WorkloadRevision: "r1",
		ReadyPath:        "/shim/ready",
		Resources:        &nodev1.ResourceSpec{Vcpus: 1, MemMib: 128},
	})
	if status.Code(err) != codes.FailedPrecondition || !strings.Contains(err.Error(), "read rootfs UUID") {
		t.Fatalf("BuildBase error = %v, want rootfs identity FailedPrecondition", err)
	}
	if base, ok := s.bases.get(baseKey); ok {
		t.Fatalf("base = %+v, want no registry entry when rootfs identity cannot key the build", base)
	}
}

func TestBaseRootfsMatches_NoRootfsID(t *testing.T) {
	dir := t.TempDir()
	rootfs := writeExt4Rootfs(t, t.TempDir(), "rootfs.ext4", testRootfsUUIDA)
	if ok, mismatch := baseRootfsMatches(dir, rootfs); ok || mismatch == nil || mismatch.Mismatch || !strings.Contains(mismatch.Reason, "rootfsid") {
		t.Fatalf("baseRootfsMatches = (%v, %+v), want read failure with rootfsid reason", ok, mismatch)
	}
}

func TestBaseRootfsMatches_MismatchedUUID(t *testing.T) {
	dir := t.TempDir()
	rootfs := writeExt4Rootfs(t, t.TempDir(), "rootfs.ext4", testRootfsUUIDB)
	if err := os.WriteFile(filepath.Join(dir, "rootfsid"), []byte(testRootfsUUIDA), 0o600); err != nil {
		t.Fatal(err)
	}
	if ok, mismatch := baseRootfsMatches(dir, rootfs); ok || mismatch == nil || !mismatch.Mismatch || mismatch.Actual != testRootfsUUIDB || mismatch.Reason != "" {
		t.Fatalf("baseRootfsMatches = (%v, %+v), want UUID mismatch with actual UUID", ok, mismatch)
	}
}

func TestBaseRootfsMatches_MatchedUUID(t *testing.T) {
	dir := t.TempDir()
	rootfs := writeExt4Rootfs(t, t.TempDir(), "rootfs.ext4", testRootfsUUIDA)
	if err := os.WriteFile(filepath.Join(dir, "rootfsid"), []byte(testRootfsUUIDA), 0o600); err != nil {
		t.Fatal(err)
	}
	if ok, mismatch := baseRootfsMatches(dir, rootfs); !ok || mismatch != nil {
		t.Fatalf("baseRootfsMatches = (%v, %+v), want true with nil mismatch", ok, mismatch)
	}
}

// TestSweepRootfsRemovesUnreferencedKeepsCurrent proves the core reclaim: with a
// registry naming one rootfs per workload, every OTHER rootfs in that directory
// is removed and the referenced one survives.
func TestSweepRootfsRemovesUnreferencedKeepsCurrent(t *testing.T) {
	s := newStoreTestServer(t, newFakeStore())
	root := t.TempDir()
	dir := filepath.Join(root, "semgrep")

	current := writeRootfs(t, dir, "rootfs-2026.07.28.00.15.51-53e998e.ext4", 2*time.Hour)
	old1 := writeRootfs(t, dir, "rootfs-2026.07.26.16.59.35-b7e6104.ext4", 48*time.Hour)
	old2 := writeRootfs(t, dir, "rootfs-2026.07.27.02.18.05-db42ab6.ext4", 24*time.Hour)
	// A non-rootfs file in the same dir must be left alone.
	other := filepath.Join(dir, "notes.txt")
	if err := os.WriteFile(other, []byte("keep me"), 0o600); err != nil {
		t.Fatalf("write other: %v", err)
	}

	s.registry.sync([]workloadEntry{{Workload: "semgrep", ImageRef: "img", RootfsRef: current}})

	removed, _ := s.sweepRootfs(time.Now())
	if removed != 2 {
		t.Fatalf("removed = %d, want 2", removed)
	}
	if _, err := os.Stat(current); err != nil {
		t.Fatalf("the referenced rootfs must survive: %v", err)
	}
	for _, p := range []string{old1, old2} {
		if _, err := os.Stat(p); !os.IsNotExist(err) {
			t.Fatalf("unreferenced rootfs %s should be gone", p)
		}
	}
	if _, err := os.Stat(other); err != nil {
		t.Fatalf("a non-rootfs file must be left alone: %v", err)
	}
}

// TestSweepRootfsSparesAFreshlyBakedFile proves the age guard covers the one real
// race: a bake has landed on disk but its SyncRegistry has not arrived, so it
// looks unreferenced while being about to become current.
func TestSweepRootfsSparesAFreshlyBakedFile(t *testing.T) {
	s := newStoreTestServer(t, newFakeStore())
	root := t.TempDir()
	dir := filepath.Join(root, "semgrep")

	current := writeRootfs(t, dir, "rootfs-old.ext4", 48*time.Hour)
	fresh := writeRootfs(t, dir, "rootfs-just-baked.ext4", 1*time.Minute)

	s.registry.sync([]workloadEntry{{Workload: "semgrep", ImageRef: "img", RootfsRef: current}})

	if removed, _ := s.sweepRootfs(time.Now()); removed != 0 {
		t.Fatalf("removed = %d, want 0 (the only candidate is younger than the min age)", removed)
	}
	if _, err := os.Stat(fresh); err != nil {
		t.Fatalf("a freshly baked rootfs must be spared: %v", err)
	}
}

// TestSweepRootfsNoOpOnAnEmptyRegistry proves the boot guard. An empty registry
// means "we do not yet know what is current", NOT "nothing is referenced", and
// sweeping then would delete every rootfs on the node.
func TestSweepRootfsNoOpOnAnEmptyRegistry(t *testing.T) {
	s := newStoreTestServer(t, newFakeStore())
	root := t.TempDir()
	dir := filepath.Join(root, "semgrep")
	kept := writeRootfs(t, dir, "rootfs-old.ext4", 48*time.Hour)

	if removed, _ := s.sweepRootfs(time.Now()); removed != 0 {
		t.Fatalf("removed = %d, want 0 with an empty registry", removed)
	}
	if _, err := os.Stat(kept); err != nil {
		t.Fatalf("nothing may be removed before the first registry sync: %v", err)
	}
}

// TestSweepRootfsOnlyTouchesRegisteredDirectories proves the sweep never wanders:
// a directory no registry entry points into is left entirely alone, even when it
// holds files matching the rootfs naming convention.
func TestSweepRootfsOnlyTouchesRegisteredDirectories(t *testing.T) {
	s := newStoreTestServer(t, newFakeStore())
	root := t.TempDir()

	registered := filepath.Join(root, "semgrep")
	current := writeRootfs(t, registered, "rootfs-current.ext4", 48*time.Hour)
	stale := writeRootfs(t, registered, "rootfs-stale.ext4", 48*time.Hour)

	// A dir the registry knows nothing about, with a rootfs-shaped file in it.
	unknown := filepath.Join(root, "some-other-thing")
	untouched := writeRootfs(t, unknown, "rootfs-unknown.ext4", 48*time.Hour)

	s.registry.sync([]workloadEntry{{Workload: "semgrep", ImageRef: "img", RootfsRef: current}})

	if removed, _ := s.sweepRootfs(time.Now()); removed != 1 {
		t.Fatalf("removed = %d, want 1 (only the stale file in the registered dir)", removed)
	}
	if _, err := os.Stat(stale); !os.IsNotExist(err) {
		t.Fatal("the stale rootfs in the registered dir should be gone")
	}
	if _, err := os.Stat(untouched); err != nil {
		t.Fatalf("a directory the registry does not reference must not be swept: %v", err)
	}
}

// TestSweepRootfsKeepsBaseRootfs proves a base's absolute rootfs reference
// survives registry turnover. Existing snapshots still need their original
// rootfs even after new workloads point at a different file.
func TestSweepRootfsKeepsBaseRootfs(t *testing.T) {
	s := newStoreTestServer(t, newFakeStore())
	dir := filepath.Join(t.TempDir(), "semgrep")
	baseRootfs := writeRootfs(t, dir, "rootfs-A.ext4", 48*time.Hour)
	currentRootfs := writeRootfs(t, dir, "rootfs-B.ext4", 48*time.Hour)

	s.registry.sync([]workloadEntry{{Workload: "semgrep", ImageRef: "img", RootfsRef: currentRootfs}})
	s.bases.readyBuild("semgrep__base", "semgrep", "img", baseRootfs, "", 0)

	if removed, _ := s.sweepRootfs(time.Now()); removed != 0 {
		t.Fatalf("removed = %d, want 0 when both rootfs files are referenced", removed)
	}
	for _, path := range []string{baseRootfs, currentRootfs} {
		if _, err := os.Stat(path); err != nil {
			t.Fatalf("referenced rootfs %s must survive: %v", path, err)
		}
	}
}

// TestSweepRootfsRemovesCompletelyOrphanedRootfs proves an old file is
// reclaimable once the registry has completed an authoritative empty sync.
// The second registry entry only establishes the directory to scan; it points
// at a different, absent rootfs and does not reference the orphan.
func TestSweepRootfsRemovesCompletelyOrphanedRootfs(t *testing.T) {
	s := newStoreTestServer(t, newFakeStore())
	dir := filepath.Join(t.TempDir(), "semgrep")
	orphaned := writeRootfs(t, dir, "rootfs-orphaned.ext4", 48*time.Hour)

	s.registry.sync([]workloadEntry{{Workload: "semgrep", ImageRef: "img", RootfsRef: filepath.Join(dir, "rootfs-current.ext4")}})

	if removed, _ := s.sweepRootfs(time.Now()); removed != 1 {
		t.Fatalf("removed = %d, want 1 for the orphaned rootfs", removed)
	}
	if _, err := os.Stat(orphaned); !os.IsNotExist(err) {
		t.Fatalf("orphaned rootfs should be gone, stat error = %v", err)
	}
}

// TestSweepRootfsEmptyRegistryGuard proves an unsynced empty registry is a
// boot-safety condition, not evidence that every rootfs is disposable.
func TestSweepRootfsEmptyRegistryGuard(t *testing.T) {
	s := newStoreTestServer(t, newFakeStore())
	rootfs := writeRootfs(t, filepath.Join(t.TempDir(), "semgrep"), "rootfs.ext4", 48*time.Hour)

	if removed, _ := s.sweepRootfs(time.Now()); removed != 0 {
		t.Fatalf("removed = %d, want 0 with an unsynced empty registry", removed)
	}
	if _, err := os.Stat(rootfs); err != nil {
		t.Fatalf("rootfs must survive the empty-registry guard: %v", err)
	}
}

// TestSweepRootfsMinAgeGuard proves a freshly baked file is spared while its
// registry update may still be in flight.
func TestSweepRootfsMinAgeGuard(t *testing.T) {
	s := newStoreTestServer(t, newFakeStore())
	rootfs := writeRootfs(t, filepath.Join(t.TempDir(), "semgrep"), "rootfs.ext4", time.Minute)

	if removed, _ := s.sweepRootfs(time.Now()); removed != 0 {
		t.Fatalf("removed = %d, want 0 for a young rootfs", removed)
	}
	if _, err := os.Stat(rootfs); err != nil {
		t.Fatalf("young rootfs must survive the min-age guard: %v", err)
	}
}

func TestSweepRootfsTemporaryFiles(t *testing.T) {
	t.Run("removes old orphaned temporary and counts bytes", func(t *testing.T) {
		// Protects against: temp files left by failed bakes not being reclaimed.
		s := newStoreTestServer(t, newFakeStore())
		dir := filepath.Join(t.TempDir(), "semgrep")
		temporary := writeRootfs(t, dir, "rootfs-60cd82d81a20.ext4.tmp.1", rootfsGCMinAge+time.Minute)
		current := filepath.Join(dir, "rootfs-current.ext4")

		s.registry.sync([]workloadEntry{{Workload: "semgrep", ImageRef: "img", RootfsRef: current}})

		removed, freed := s.sweepRootfs(time.Now())
		if removed != 1 {
			t.Fatalf("removed = %d, want 1", removed)
		}
		if freed != 0x48c {
			t.Fatalf("freed = %d, want %d", freed, 0x48c)
		}
		if _, err := os.Stat(temporary); !os.IsNotExist(err) {
			t.Fatalf("orphaned temporary should be gone, stat error = %v", err)
		}
	})

	t.Run("spares young temporary", func(t *testing.T) {
		// Protects against: deleting temporaries of in-progress bakes (the min-age guard for temporaries).
		s := newStoreTestServer(t, newFakeStore())
		dir := filepath.Join(t.TempDir(), "semgrep")
		temporary := writeRootfs(t, dir, "rootfs-60cd82d81a20.ext4.tmp.1", rootfsGCMinAge-time.Minute)
		current := filepath.Join(dir, "rootfs-current.ext4")

		s.registry.sync([]workloadEntry{{Workload: "semgrep", ImageRef: "img", RootfsRef: current}})

		if removed, _ := s.sweepRootfs(time.Now()); removed != 0 {
			t.Fatalf("removed = %d, want 0 for a young temporary", removed)
		}
		if _, err := os.Stat(temporary); err != nil {
			t.Fatalf("young temporary must survive the min-age guard: %v", err)
		}
	})

	t.Run("ignores unrelated temporary name", func(t *testing.T) {
		// Protects against: problem 1 - the loose contains predicate matching unrelated files.
		s := newStoreTestServer(t, newFakeStore())
		dir := filepath.Join(t.TempDir(), "semgrep")
		unrelated := writeRootfs(t, dir, "backup.tar.tmp.1", rootfsGCMinAge+time.Minute)
		current := filepath.Join(dir, "rootfs-current.ext4")

		s.registry.sync([]workloadEntry{{Workload: "semgrep", ImageRef: "img", RootfsRef: current}})

		if removed, _ := s.sweepRootfs(time.Now()); removed != 0 {
			t.Fatalf("removed = %d, want 0 for an unrelated temporary name", removed)
		}
		if _, err := os.Stat(unrelated); err != nil {
			t.Fatalf("unrelated temporary must survive: %v", err)
		}
	})

	t.Run("keeps referenced rootfs", func(t *testing.T) {
		// Protects against: the predicate edit accidentally widening the match to consume live images.
		s := newStoreTestServer(t, newFakeStore())
		dir := filepath.Join(t.TempDir(), "semgrep")
		current := writeRootfs(t, dir, "rootfs-current.ext4", rootfsGCMinAge+time.Minute)

		s.registry.sync([]workloadEntry{{Workload: "semgrep", ImageRef: "img", RootfsRef: current}})

		if removed, _ := s.sweepRootfs(time.Now()); removed != 0 {
			t.Fatalf("removed = %d, want 0 for a referenced rootfs", removed)
		}
		if _, err := os.Stat(current); err != nil {
			t.Fatalf("referenced rootfs must survive: %v", err)
		}
	})
}

// TestReconcileBasesCheckRootfsExists proves restart reconciliation reports a base
// with a lost backing file as NONE, which is the ONLY state the control plane acts
// on: Embervm.BaseBuilder.node_reports_base_absent?/3 matches NONE and nothing there
// matches FAILED, so reporting FAILED would leave the base unusable AND unrecovered.
func TestReconcileBasesCheckRootfsExists(t *testing.T) {
	s := newStoreTestServer(t, newFakeStore())
	baseKey := "semgrep__missing-rootfs"
	baseDir := filepath.Join(s.cfg.SnapshotRoot, "bases", baseKey)
	if err := os.MkdirAll(baseDir, 0o755); err != nil {
		t.Fatalf("mkdir base dir: %v", err)
	}
	for name, contents := range map[string]string{
		"snapfile":   "snap",
		"imageref":   "img",
		"rootfspath": filepath.Join(t.TempDir(), "does-not-exist.ext4"),
	} {
		if err := os.WriteFile(filepath.Join(baseDir, name), []byte(contents), 0o644); err != nil {
			t.Fatalf("write %s: %v", name, err)
		}
	}

	s.ReconcileBasesFromDisk()
	got, ok := s.bases.get(baseKey)
	if !ok {
		t.Fatal("missing-rootfs base was not registered")
	}
	if got.state != nodev1.BaseBuildState_BASE_BUILD_STATE_NONE {
		t.Fatalf("base state = %v, want NONE", got.state)
	}
	if !(strings.Contains(got.buildErr, "rootfs") &&
		(strings.Contains(got.buildErr, "missing") || strings.Contains(got.buildErr, "rebuild"))) {
		t.Fatalf("build error = %q, want rootfs missing/rebuild explanation", got.buildErr)
	}
}

// TestReconcileBasesAcceptsExistingRootfs proves a restart adopts a complete
// base as READY when its persisted backing rootfs is still present.
func TestReconcileBasesAcceptsExistingRootfs(t *testing.T) {
	s := newStoreTestServer(t, newFakeStore())
	baseKey := "semgrep__existing-rootfs"
	baseDir := filepath.Join(s.cfg.SnapshotRoot, "bases", baseKey)
	if err := os.MkdirAll(baseDir, 0o755); err != nil {
		t.Fatalf("mkdir base dir: %v", err)
	}
	rootfs := writeRootfs(t, t.TempDir(), "rootfs.ext4", 0)
	for name, contents := range map[string]string{
		"snapfile":   "snap",
		"imageref":   "img",
		"rootfspath": rootfs,
		"rootfsid":   testRootfsUUIDA,
	} {
		if err := os.WriteFile(filepath.Join(baseDir, name), []byte(contents), 0o644); err != nil {
			t.Fatalf("write %s: %v", name, err)
		}
	}

	s.ReconcileBasesFromDisk()
	got, ok := s.bases.get(baseKey)
	if !ok {
		t.Fatal("existing-rootfs base was not registered")
	}
	if got.state != nodev1.BaseBuildState_BASE_BUILD_STATE_READY {
		t.Fatalf("base state = %v, want READY", got.state)
	}
	if got.buildErr != "" {
		t.Fatalf("build error = %q, want empty", got.buildErr)
	}
}

func writeIdentityBaseBundle(t *testing.T, s *Server, baseKey, rootfsPath, recordedID string) string {
	t.Helper()
	dir := filepath.Join(s.cfg.SnapshotRoot, "bases", baseKey)
	files := map[string]string{
		"imageref":   "img",
		"memfile":    "mem",
		"rootfspath": rootfsPath,
		"snapfile":   "snap",
	}
	if recordedID != "" {
		files["rootfsid"] = recordedID
	}
	if err := os.MkdirAll(dir, 0o700); err != nil {
		t.Fatal(err)
	}
	for name, contents := range files {
		if err := os.WriteFile(filepath.Join(dir, name), []byte(contents), 0o600); err != nil {
			t.Fatalf("write %s: %v", name, err)
		}
	}
	return dir
}

func TestReconcileBasesRemovesMissingRootfsID(t *testing.T) {
	s := newStoreTestServer(t, newFakeStore())
	baseKey := "echo__missing-rootfsid"
	rootfs := writeExt4Rootfs(t, t.TempDir(), "rootfs.ext4", testRootfsUUIDA)
	dir := writeIdentityBaseBundle(t, s, baseKey, rootfs, "")

	if err := s.ReconcileBasesFromDisk(); err != nil {
		t.Fatalf("ReconcileBasesFromDisk: %v", err)
	}
	if _, ok := s.bases.get(baseKey); ok {
		t.Fatal("base without rootfsid was registered")
	}
	if _, err := os.Stat(dir); !os.IsNotExist(err) {
		t.Fatalf("base without rootfsid was not removed: %v", err)
	}
}

func TestReconcileBasesRemovesMismatched(t *testing.T) {
	s := newStoreTestServer(t, newFakeStore())
	baseKey := "echo__mismatched-rootfsid"
	rootfs := writeExt4Rootfs(t, t.TempDir(), "rootfs.ext4", testRootfsUUIDB)
	dir := writeIdentityBaseBundle(t, s, baseKey, rootfs, testRootfsUUIDA)

	if err := s.ReconcileBasesFromDisk(); err != nil {
		t.Fatalf("ReconcileBasesFromDisk: %v", err)
	}
	if _, ok := s.bases.get(baseKey); ok {
		t.Fatal("base with mismatched rootfsid was registered")
	}
	if _, err := os.Stat(dir); !os.IsNotExist(err) {
		t.Fatalf("mismatched base was not removed: %v", err)
	}
}

func TestReconcileBasesDoesNotRemoveIfVMInUse(t *testing.T) {
	s := newStoreTestServer(t, newFakeStore())
	baseKey := "echo__in-use-mismatch"
	rootfs := writeExt4Rootfs(t, t.TempDir(), "rootfs.ext4", testRootfsUUIDB)
	dir := writeIdentityBaseBundle(t, s, baseKey, rootfs, testRootfsUUIDA)
	s.vms.add(&vmEntry{id: "vm-live", workload: "echo", snapshotRef: baseKey, state: vmPrimed})

	if err := s.ReconcileBasesFromDisk(); err != nil {
		t.Fatalf("ReconcileBasesFromDisk: %v", err)
	}
	base, ok := s.bases.get(baseKey)
	if !ok || base.state != nodev1.BaseBuildState_BASE_BUILD_STATE_NONE {
		t.Fatalf("in-use mismatched base = %+v, ok=%v, want NONE", base, ok)
	}
	if !strings.Contains(base.buildErr, "UUID mismatch") {
		t.Fatalf("buildErr = %q, want mismatch reason", base.buildErr)
	}
	// The bundle is moved aside, not left at bases/<key>: a rebuild publishing
	// onto that path must find no complete bundle to adopt as a collision winner.
	if _, err := os.Stat(dir); !os.IsNotExist(err) {
		t.Fatalf("in-use mismatched base still at %s (err=%v), want moved aside", dir, err)
	}
	aside := staleBaseDirsFor(t, s, baseKey)
	if len(aside) != 1 {
		t.Fatalf("moved-aside dirs = %v, want exactly one", aside)
	}
	if _, err := os.Stat(filepath.Join(aside[0], "snapfile")); err != nil {
		t.Fatalf("moved-aside bundle lost its snapfile: %v", err)
	}

	// While the VM lives, a second reconcile keeps the moved-aside bundle and
	// does not treat it as a base.
	if err := s.ReconcileBasesFromDisk(); err != nil {
		t.Fatalf("second ReconcileBasesFromDisk: %v", err)
	}
	if got := staleBaseDirsFor(t, s, baseKey); len(got) != 1 {
		t.Fatalf("moved-aside dirs after second reconcile = %v, want the same one", got)
	}
	if _, ok := s.bases.get(aside[0]); ok {
		t.Fatalf("moved-aside dir %s was registered as a base", aside[0])
	}

	// Once nothing references the key, the moved-aside bundle is reclaimed.
	s.vms.remove("vm-live")
	if err := s.ReconcileBasesFromDisk(); err != nil {
		t.Fatalf("third ReconcileBasesFromDisk: %v", err)
	}
	if got := staleBaseDirsFor(t, s, baseKey); len(got) != 0 {
		t.Fatalf("moved-aside dirs after VM gone = %v, want none", got)
	}
}

// staleBaseDirsFor lists the moved-aside bundle dirs for baseKey.
func staleBaseDirsFor(t *testing.T, s *Server, baseKey string) []string {
	t.Helper()
	entries, err := os.ReadDir(filepath.Join(s.cfg.SnapshotRoot, "bases"))
	if err != nil {
		t.Fatalf("ReadDir bases: %v", err)
	}
	var out []string
	for _, ent := range entries {
		if orig, ok := staleBaseOriginalKey(ent.Name()); ok && orig == baseKey {
			out = append(out, filepath.Join(s.cfg.SnapshotRoot, "bases", ent.Name()))
		}
	}
	return out
}

func TestStaleBaseOriginalKey(t *testing.T) {
	if _, ok := staleBaseOriginalKey("echo__abc"); ok {
		t.Fatal("plain base key reported as stale")
	}
	if _, ok := staleBaseOriginalKey(".stale.123"); ok {
		t.Fatal("suffix with no key reported as stale")
	}
	orig, ok := staleBaseOriginalKey(filepath.Base(staleBaseDir("/x/bases/echo__abc")))
	if !ok || orig != "echo__abc" {
		t.Fatalf("staleBaseOriginalKey = (%q, %v), want (echo__abc, true)", orig, ok)
	}
}

func TestReconcileBasesSkipsGateIfBuilding(t *testing.T) {
	s := newStoreTestServer(t, newFakeStore())
	baseKey := "echo__building-mismatch"
	rootfs := writeExt4Rootfs(t, t.TempDir(), "rootfs.ext4", testRootfsUUIDB)
	dir := writeIdentityBaseBundle(t, s, baseKey, rootfs, testRootfsUUIDA)
	if !s.bases.beginBuild(baseKey, "echo", rootfs, "/shim/ready") {
		t.Fatal("beginBuild refused fresh key")
	}

	if err := s.ReconcileBasesFromDisk(); err != nil {
		t.Fatalf("ReconcileBasesFromDisk: %v", err)
	}
	base, ok := s.bases.get(baseKey)
	if !ok || base.state != nodev1.BaseBuildState_BASE_BUILD_STATE_BUILDING {
		t.Fatalf("building base = %+v, ok=%v, want BUILDING unchanged", base, ok)
	}
	if _, err := os.Stat(dir); err != nil {
		t.Fatalf("building base was removed: %v", err)
	}
}

func TestReconcileBasesRegisterMatched(t *testing.T) {
	s := newStoreTestServer(t, newFakeStore())
	baseKey := "echo__matched-rootfsid"
	rootfs := writeExt4Rootfs(t, t.TempDir(), "rootfs.ext4", testRootfsUUIDA)
	dir := writeIdentityBaseBundle(t, s, baseKey, rootfs, testRootfsUUIDA)

	if err := s.ReconcileBasesFromDisk(); err != nil {
		t.Fatalf("ReconcileBasesFromDisk: %v", err)
	}
	got, ok := s.bases.get(baseKey)
	if !ok || got.state != nodev1.BaseBuildState_BASE_BUILD_STATE_READY {
		t.Fatalf("matched base = %+v, ok=%v, want READY", got, ok)
	}
	if _, err := os.Stat(dir); err != nil {
		t.Fatalf("matched base was removed: %v", err)
	}
}

func TestAdoptSiblingBaseBundleRootfsIdentity(t *testing.T) {
	for _, tc := range []struct {
		name       string
		recordedID string
		want       bool
	}{
		{name: "missing", recordedID: "", want: false},
		{name: "mismatched", recordedID: testRootfsUUIDB, want: false},
		{name: "matched", recordedID: testRootfsUUIDA, want: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			s := newStoreTestServer(t, newFakeStore())
			baseKey := "echo__adopt-" + tc.name
			rootfs := writeExt4Rootfs(t, t.TempDir(), "rootfs.ext4", testRootfsUUIDA)
			writeIdentityBaseBundle(t, s, baseKey, rootfs, tc.recordedID)
			_, got := s.adoptSiblingBaseBundle(baseKey, "echo", "img", rootfs, "/shim/ready")
			if got != tc.want {
				t.Fatalf("adoptSiblingBaseBundle accepted = %v, want %v", got, tc.want)
			}
		})
	}
}

func TestAdoptSiblingRefuseMismatched(t *testing.T) {
	s := newStoreTestServer(t, newFakeStore())
	baseKey := "echo__adopt-mismatch"
	rootfs := writeExt4Rootfs(t, t.TempDir(), "rootfs.ext4", testRootfsUUIDA)
	writeIdentityBaseBundle(t, s, baseKey, rootfs, testRootfsUUIDB)
	if _, ok := s.adoptSiblingBaseBundle(baseKey, "echo", "img", rootfs, "/shim/ready"); ok {
		t.Fatal("adoptSiblingBaseBundle accepted a mismatched rootfs UUID")
	}
}

func TestBootRejectsBaseRootfsMismatch(t *testing.T) {
	drv := &fakeDriver{}
	s := New(Options{
		Config:    config.Config{Arch: "amd64", Node: "node-4", MaxLiveVMs: 4, SnapshotRoot: t.TempDir()},
		Driver:    drv,
		Transport: &fakeTransport{},
		Logger:    slog.New(slog.NewTextHandler(io.Discard, nil)),
	})
	baseKey := "echo__boot-mismatch"
	rootfs := writeExt4Rootfs(t, t.TempDir(), "rootfs.ext4", testRootfsUUIDB)
	writeIdentityBaseBundle(t, s, baseKey, rootfs, testRootfsUUIDA)
	s.bases.readyBuild(baseKey, "echo", "img", rootfs, "/shim/ready", 2048)

	_, err := s.Prime(context.Background(), &nodev1.PrimeRequest{SnapshotRef: baseKey})
	if status.Code(err) != codes.FailedPrecondition || !strings.Contains(err.Error(), "base rootfs identity mismatch") {
		t.Fatalf("Prime error = %v, want distinct rootfs identity mismatch", err)
	}
	if claims, _, _, _ := drv.counts(); claims != 0 {
		t.Fatalf("driver claims = %d, want 0 before identity gate", claims)
	}
}

func TestBaseStoreCopyIsStale_NoRemote(t *testing.T) {
	fs := newFakeStore()
	s := newStoreTestServer(t, fs)
	dir := t.TempDir()
	writeBundleFiles(t, dir, map[string]string{"rootfsid": testRootfsUUIDA})

	stale, err := s.baseStoreCopyIsStale(context.Background(), "base/amd/echo/missing", dir)
	if err != nil || stale {
		t.Fatalf("baseStoreCopyIsStale = (%v, %v), want (false, nil)", stale, err)
	}
}

func TestBaseStoreCopyIsStale_LocalRootfsidMissing(t *testing.T) {
	s := newStoreTestServer(t, newFakeStore())
	// An unverifiable local bundle must never force an overwrite of the store
	// copy; it is reported as not stale and left for the reconcile gate.
	stale, err := s.baseStoreCopyIsStale(context.Background(), "base/amd/echo/missing-local", t.TempDir())
	if err != nil || stale {
		t.Fatalf("baseStoreCopyIsStale = (%v, %v), want (false, nil)", stale, err)
	}
}

func TestBaseStoreCopyIsStale_StoreError(t *testing.T) {
	fs := newFakeStore()
	fs.artifactFileErr = context.DeadlineExceeded
	s := newStoreTestServer(t, fs)
	dir := t.TempDir()
	writeBundleFiles(t, dir, map[string]string{"rootfsid": testRootfsUUIDA})

	stale, err := s.baseStoreCopyIsStale(context.Background(), "base/amd/echo/store-error", dir)
	if err != context.DeadlineExceeded || stale {
		t.Fatalf("baseStoreCopyIsStale = (%v, %v), want (false, deadline exceeded)", stale, err)
	}
}

func TestBaseStoreCopyIsStale_RemoteLacksRootfsid(t *testing.T) {
	fs := newFakeStore()
	s := newStoreTestServer(t, fs)
	dir := t.TempDir()
	writeBundleFiles(t, dir, map[string]string{"rootfsid": testRootfsUUIDA})
	prefix := "base/amd/echo/no-rootfsid"
	fs.seedArtifact(prefix, map[string]string{"snapfile": "snap"}, 0, "amd", "")

	stale, err := s.baseStoreCopyIsStale(context.Background(), prefix, dir)
	if err != nil || !stale {
		t.Fatalf("baseStoreCopyIsStale = (%v, %v), want (true, nil)", stale, err)
	}
}

func TestBaseStoreCopyIsStale_DifferentRootfsid(t *testing.T) {
	fs := newFakeStore()
	s := newStoreTestServer(t, fs)
	dir := t.TempDir()
	writeBundleFiles(t, dir, map[string]string{"rootfsid": testRootfsUUIDA})
	prefix := "base/amd/echo/different-rootfsid"
	fs.seedArtifact(prefix, map[string]string{"rootfsid": testRootfsUUIDB}, 0, "amd", "")

	stale, err := s.baseStoreCopyIsStale(context.Background(), prefix, dir)
	if err != nil || !stale {
		t.Fatalf("baseStoreCopyIsStale = (%v, %v), want (true, nil)", stale, err)
	}
}

func TestBaseStoreCopyIsStale_MatchingRootfsid(t *testing.T) {
	fs := newFakeStore()
	s := newStoreTestServer(t, fs)
	dir := t.TempDir()
	writeBundleFiles(t, dir, map[string]string{"rootfsid": testRootfsUUIDA})
	prefix := "base/amd/echo/matching-rootfsid"
	fs.seedArtifact(prefix, map[string]string{"rootfsid": testRootfsUUIDA}, 0, "amd", "")

	stale, err := s.baseStoreCopyIsStale(context.Background(), prefix, dir)
	if err != nil || stale {
		t.Fatalf("baseStoreCopyIsStale = (%v, %v), want (false, nil)", stale, err)
	}
}

func TestExportJobSkipsAlreadyDurableForNonBase(t *testing.T) {
	fs := newFakeStore()
	s := newStoreTestServer(t, fs)
	ref := &nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_SESSION, Workload: "echo", Ref: "session-1"}
	key := artifactPrefix(ref, s.cfg.CpuVendor)
	writeBundleFiles(t, s.artifactLocalDir(ref), map[string]string{"snapfile": "local"})
	fs.seedArtifact(key, map[string]string{"snapfile": "remote"}, 0, "amd", "")

	s.runExportJob(context.Background(), exportJob{key: key, ref: ref})
	if got := fs.calls(key); got != 0 {
		t.Fatalf("store.Export calls = %d, want 0 for already-durable non-base", got)
	}
}

func TestExportJobDoesNotSkipWhenBaseIsStale(t *testing.T) {
	fs := newFakeStore()
	s := newStoreTestServer(t, fs)
	baseKey := "echo__stale-remote"
	ref := &nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_BASE, Workload: "echo", Ref: baseKey}
	prefix := artifactPrefix(ref, s.cfg.CpuVendor)
	writeBundleFiles(t, s.artifactLocalDir(ref), map[string]string{"rootfsid": testRootfsUUIDA, "snapfile": "local"})
	fs.seedArtifact(prefix, map[string]string{"rootfsid": testRootfsUUIDB, "snapfile": "remote"}, 0, "amd", "")

	s.runExportJob(context.Background(), exportJob{
		key: prefix,
		ref: ref,
	})
	if got := fs.calls(prefix); got != 1 {
		t.Fatalf("store.Export calls = %d, want 1 for stale base", got)
	}
	if got := fs.overwrites(prefix); got != 1 {
		t.Fatalf("overwrite exports = %d, want 1 for stale base", got)
	}
}

func TestExportJobDoesNotSkipWhenBaseStalenessCheckErrors(t *testing.T) {
	fs := newFakeStore()
	fs.artifactFileErr = context.DeadlineExceeded
	s := newStoreTestServer(t, fs)
	baseKey := "echo__staleness-error"
	ref := &nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_BASE, Workload: "echo", Ref: baseKey}
	prefix := artifactPrefix(ref, s.cfg.CpuVendor)
	writeBundleFiles(t, s.artifactLocalDir(ref), map[string]string{"rootfsid": testRootfsUUIDA, "snapfile": "local"})
	fs.seedArtifact(prefix, map[string]string{"rootfsid": testRootfsUUIDA, "snapfile": "remote"}, 0, "amd", "")

	s.runExportJob(context.Background(), exportJob{key: prefix, ref: ref})
	if got := fs.calls(prefix); got != 1 {
		t.Fatalf("store.Export calls = %d, want 1 after staleness check error", got)
	}
	if got := fs.overwrites(prefix); got != 1 {
		t.Fatalf("overwrite exports = %d, want 1 after staleness check error", got)
	}
}

func TestRunRestoreJobRejectsRefusedBase(t *testing.T) {
	fs := newFakeStore()
	s := newStoreTestServer(t, fs)
	baseKey := "echo__refused-restore"
	ref := &nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_BASE, Workload: "echo", Ref: baseKey}
	prefix := artifactPrefix(ref, s.cfg.CpuVendor)
	rootfs := writeExt4Rootfs(t, t.TempDir(), "rootfs.ext4", testRootfsUUIDB)
	fs.seedArtifact(prefix, map[string]string{
		"imageref":   "img",
		"memfile":    "mem",
		"rootfsid":   testRootfsUUIDA,
		"rootfspath": rootfs,
		"snapfile":   "snap",
	}, 0, "amd", "")

	s.runRestoreJob(context.Background(), restoreJob{
		ref:      ref,
		prefix:   prefix,
		localDir: s.artifactLocalDir(ref),
	})
	if s.exported.present(prefix) {
		t.Fatal("refused restored base was marked exported")
	}
	if _, ok := s.bases.get(baseKey); ok {
		t.Fatal("refused restored base remained registered")
	}
}
