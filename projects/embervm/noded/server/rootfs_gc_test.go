package server

import (
	"os"
	"path/filepath"
	"testing"
	"time"
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
