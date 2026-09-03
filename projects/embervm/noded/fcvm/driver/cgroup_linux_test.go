//go:build linux

package driver

import (
	"errors"
	"os"
	"path/filepath"
	"testing"
)

func TestReadOOMKillCount(t *testing.T) {
	path := filepath.Join(t.TempDir(), "memory.events")
	data := []byte("low 0\nhigh 0\nmax 3\noom 2\noom_kill 1\noom_group_kill 1\n")
	if err := os.WriteFile(path, data, 0o600); err != nil {
		t.Fatal(err)
	}
	got, err := readOOMKillCount(path)
	if err != nil {
		t.Fatalf("readOOMKillCount: %v", err)
	}
	if got != 1 {
		t.Fatalf("oom_kill = %d, want 1", got)
	}
}

func TestCgroupManagerRetriesInitializationAfterFailure(t *testing.T) {
	parent := t.TempDir()
	vmDir := filepath.Join(parent, "vm-retry")
	if err := os.Mkdir(vmDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(vmDir, "memory.events"), []byte("oom_kill 0\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	manager := newCgroupManager()
	attempts := 0
	manager.initializeFn = func() error {
		attempts++
		if attempts == 1 {
			return errors.New("temporary delegation failure")
		}
		manager.parentDir = parent
		manager.parentArg = "test-parent"
		return nil
	}
	if _, err := manager.Create("vm-retry", 1024); err == nil {
		t.Fatal("first cgroup creation unexpectedly succeeded")
	}
	if _, err := manager.Create("vm-retry", 1024); err != nil {
		t.Fatalf("second cgroup creation did not recover: %v", err)
	}
	if attempts != 2 {
		t.Fatalf("initialize attempts = %d, want 2", attempts)
	}
}

func TestVMCgroupRemoveWalksJailerChildren(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "vm", "firecracker", "vm-id")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	top := filepath.Dir(filepath.Dir(dir))
	if err := (&vmCgroup{dir: top}).Remove(); err != nil {
		t.Fatalf("Remove: %v", err)
	}
	if _, err := os.Stat(top); !os.IsNotExist(err) {
		t.Fatalf("cgroup tree remains after Remove: %v", err)
	}
}

func TestCgroupFailureLoggingDeduplicatesPathSpecificErrors(t *testing.T) {
	manager := newCgroupManager()
	first := &os.PathError{Op: "open", Path: "/sys/fs/cgroup/vm-a/memory.max", Err: os.ErrPermission}
	second := &os.PathError{Op: "open", Path: "/sys/fs/cgroup/vm-b/memory.max", Err: os.ErrPermission}
	if !manager.shouldLogFailure(first) {
		t.Fatal("first permission failure was suppressed")
	}
	if manager.shouldLogFailure(second) {
		t.Fatal("same failure cause at a different VM path was logged twice")
	}
	if !manager.shouldLogFailure(os.ErrNotExist) {
		t.Fatal("distinct failure cause was suppressed")
	}
}
