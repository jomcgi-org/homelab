package server

import (
	"os"
	"path/filepath"
	"reflect"
	"testing"
)

func TestReadyBaseRootfsPathsReturnsDistinctReadyRootfs(t *testing.T) {
	_, s := newTestServer(t, &fakeDriver{}, &fakeTransport{}, 8)
	s.bases.readyBuild("echo__ready000001", "echo", "img:1", "/rootfs/echo/a.ext4", "/shim/ready", 1)
	// A second READY base on the same image is probed once.
	s.bases.readyBuild("echo__ready000002", "echo", "img:1", "/rootfs/echo/a.ext4", "/shim/ready", 1)
	s.bases.readyBuild("other__ready00001", "other", "img:2", "/rootfs/other/b.ext4", "/shim/ready", 1)
	s.bases.beginBuild("echo__building001", "echo", "/rootfs/echo/building.ext4", "/shim/ready")
	s.bases.failBuild("echo__failed00001", "echo", "/rootfs/echo/failed.ext4", "/shim/ready", "boom")
	// The rootfspath sidecar a restore reads wins over the registered path.
	sidecarDir := filepath.Join(s.cfg.SnapshotRoot, "bases", "sidecar__ready001")
	if err := os.MkdirAll(sidecarDir, 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(sidecarDir, "rootfspath"), []byte("/rootfs/side/c.ext4\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	s.bases.readyBuild("sidecar__ready001", "side", "img:3", "/rootfs/side/registered.ext4", "/shim/ready", 1)

	got := s.ReadyBaseRootfsPaths()
	want := []string{"/rootfs/echo/a.ext4", "/rootfs/other/b.ext4", "/rootfs/side/c.ext4"}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("ReadyBaseRootfsPaths() = %q, want %q", got, want)
	}
}

func TestBrickMemBudgetMibReportsTheBudgetReader(t *testing.T) {
	_, s := newTestServer(t, &fakeDriver{}, &fakeTransport{}, 8)
	s.memBudget = func() (uint64, bool) { v := uint64(3840); return v, v > 0 }
	if got := s.BrickMemBudgetMib(); got != 3840 {
		t.Fatalf("BrickMemBudgetMib() = %d, want 3840", got)
	}
}
