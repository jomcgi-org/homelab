package server

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	nodev1 "github.com/jomcgi/homelab/projects/embervm/proto/embervm/node/v1"
)

// writeRootfs creates a rootfs file with a given age, mirroring what the
// rootfs-builder bakes.
func writeRootfs(t *testing.T, dir, name string, age time.Duration) string {
	t.Helper()
	if err := os.MkdirAll(dir, 0o700); err != nil {
		t.Fatalf("mkdir %s: %v", dir, err)
	}
	path := filepath.Join(dir, name)
	if err := os.WriteFile(path, []byte("rootfs"), 0o600); err != nil {
		t.Fatalf("write %s: %v", path, err)
	}
	when := time.Now().Add(-age)
	if err := os.Chtimes(path, when, when); err != nil {
		t.Fatalf("chtimes %s: %v", path, err)
	}
	return path
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
		if freed != int64(len("rootfs")) {
			t.Fatalf("freed = %d, want %d", freed, len("rootfs"))
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
