//go:build linux

package driver

import (
	"errors"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"testing"
)

// withCgroupProcFixtures points initialize's /proc reads at a fake cgroup2 mount
// rooted at mount and a fake /proc/self/cgroup naming self.
func withCgroupProcFixtures(t *testing.T, mount, self string) {
	t.Helper()
	dir := t.TempDir()
	mounts := filepath.Join(dir, "mounts")
	if err := os.WriteFile(mounts, []byte("proc /proc proc rw 0 0\ncgroup2 "+mount+" cgroup2 rw 0 0\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	setSelfCgroupFixture(t, dir, self)
	origMounts, origSelf := procMountsPath, procSelfCgroupPath
	procMountsPath = mounts
	procSelfCgroupPath = filepath.Join(dir, "self-cgroup")
	t.Cleanup(func() { procMountsPath, procSelfCgroupPath = origMounts, origSelf })
}

func setSelfCgroupFixture(t *testing.T, dir, self string) {
	t.Helper()
	if err := os.WriteFile(filepath.Join(dir, "self-cgroup"), []byte("0::"+self+"\n"), 0o644); err != nil {
		t.Fatal(err)
	}
}

func readFixture(t *testing.T, path string) string {
	t.Helper()
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return strings.TrimSpace(string(b))
}

// TestCgroupManagerInitializeIsIdempotentAcrossDelegation pins J3's driver half:
// a retry after a partial delegation, with noded already moved into the daemon
// child, must resolve the same brick cgroup instead of nesting a second
// daemon/VM pair below the daemon child.
func TestCgroupManagerInitializeIsIdempotentAcrossDelegation(t *testing.T) {
	mount := t.TempDir()
	ctr := filepath.Join(mount, "pod", "ctr")
	if err := os.MkdirAll(ctr, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(ctr, "cgroup.procs"), []byte("42\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	withCgroupProcFixtures(t, mount, "/pod/ctr")

	first := newCgroupManager()
	if err := first.initialize(); err != nil {
		t.Fatalf("initialize: %v", err)
	}
	wantParent := filepath.Join(ctr, cgroupVMParent)
	if first.parentDir != wantParent || first.parentArg != "pod/ctr/embervm-vms" || first.brickDir != ctr {
		t.Fatalf("parentDir=%q parentArg=%q brickDir=%q", first.parentDir, first.parentArg, first.brickDir)
	}
	if got := readFixture(t, filepath.Join(ctr, DaemonCgroupName, "cgroup.procs")); got != "42" {
		t.Fatalf("daemon cgroup.procs = %q, want the moved pid 42", got)
	}
	if got := readFixture(t, filepath.Join(ctr, "cgroup.subtree_control")); got != "+memory" {
		t.Fatalf("brick subtree_control = %q, want +memory", got)
	}
	if got := readFixture(t, filepath.Join(wantParent, "memory.oom.group")); got != "0" {
		t.Fatalf("VM parent memory.oom.group = %q, want 0 so an OOM kills one leaf", got)
	}

	// noded now reports the daemon child; a fresh manager must resolve the brick.
	setSelfCgroupFixture(t, filepath.Dir(procSelfCgroupPath), "/pod/ctr/"+DaemonCgroupName)
	retry := newCgroupManager()
	if err := retry.initialize(); err != nil {
		t.Fatalf("initialize after delegation: %v", err)
	}
	if retry.parentDir != wantParent || retry.parentArg != "pod/ctr/embervm-vms" || retry.brickDir != ctr {
		t.Fatalf("retry parentDir=%q parentArg=%q brickDir=%q", retry.parentDir, retry.parentArg, retry.brickDir)
	}
	for _, nested := range []string{DaemonCgroupName, cgroupVMParent} {
		if _, err := os.Stat(filepath.Join(ctr, DaemonCgroupName, nested)); !os.IsNotExist(err) {
			t.Fatalf("retry nested %s below the daemon cgroup: %v", nested, err)
		}
	}
}

// TestCgroupManagerCapsVMParentBelowBrick pins S17 part B: embervm-vms is capped
// at the brick's memory.max minus the daemon reserve on every launch, so an
// aggregate guest overrun OOMs inside the VM parent, never the container.
func TestCgroupManagerCapsVMParentBelowBrick(t *testing.T) {
	for _, tc := range []struct {
		name     string
		brickMax string
		want     string
	}{
		{name: "limited", brickMax: "4294967296\n", want: strconv.FormatInt(4294967296-512<<20, 10)},
		{name: "unlimited", brickMax: "max\n", want: "max"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			brick := t.TempDir()
			parent := filepath.Join(brick, cgroupVMParent)
			leaf := filepath.Join(parent, "vm-cap")
			if err := os.MkdirAll(leaf, 0o755); err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(filepath.Join(brick, "memory.max"), []byte(tc.brickMax), 0o644); err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(filepath.Join(leaf, "memory.events"), []byte("oom_kill 0\n"), 0o644); err != nil {
				t.Fatal(err)
			}
			manager := &cgroupManager{brickDir: brick, parentDir: parent, parentArg: "ctr/embervm-vms"}
			cg, err := manager.Create("vm-cap", memoryLimitBytes(256), 512)
			if err != nil {
				t.Fatalf("Create: %v", err)
			}
			if got := readFixture(t, filepath.Join(parent, "memory.max")); got != tc.want {
				t.Fatalf("VM parent memory.max = %q, want %q", got, tc.want)
			}
			// The per-VM ceiling is unchanged: guest plus the VMM overhead.
			if got, want := readFixture(t, filepath.Join(leaf, "memory.max")), strconv.FormatInt(memoryLimitBytes(256), 10); got != want {
				t.Fatalf("leaf memory.max = %q, want %q", got, want)
			}
			if cg.ParentArg() != "ctr/embervm-vms/vm-cap" {
				t.Fatalf("leaf parent arg = %q", cg.ParentArg())
			}
		})
	}
}

func TestCgroupManagerRefusesLaunchWhenReserveConsumesBrick(t *testing.T) {
	brick := t.TempDir()
	parent := filepath.Join(brick, cgroupVMParent)
	if err := os.MkdirAll(parent, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(brick, "memory.max"), []byte("536870912\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	manager := &cgroupManager{brickDir: brick, parentDir: parent, parentArg: "ctr/embervm-vms"}
	if _, err := manager.Create("vm-none", memoryLimitBytes(128), 512); err == nil {
		t.Fatal("Create succeeded although the daemon reserve consumes the whole brick")
	}
	if _, err := os.Stat(filepath.Join(parent, "vm-none")); !os.IsNotExist(err) {
		t.Fatalf("refused launch left a leaf behind: %v", err)
	}
}

func TestVMParentMemoryMax(t *testing.T) {
	for _, tc := range []struct {
		brickMax string
		reserve  int
		want     string
		wantErr  bool
	}{
		{brickMax: "8589934592", reserve: 256, want: strconv.FormatInt(8589934592-256<<20, 10)},
		{brickMax: " 8589934592\n", reserve: 0, want: "8589934592"},
		{brickMax: "8589934592", reserve: -5, want: "8589934592"},
		{brickMax: "max", reserve: 512, want: "max"},
		{brickMax: "268435456", reserve: 256, wantErr: true},
		{brickMax: "", reserve: 256, wantErr: true},
		{brickMax: "garbage", reserve: 256, wantErr: true},
	} {
		got, err := vmParentMemoryMax(tc.brickMax, tc.reserve)
		if (err != nil) != tc.wantErr || got != tc.want {
			t.Errorf("vmParentMemoryMax(%q, %d) = %q, %v; want %q, err=%v", tc.brickMax, tc.reserve, got, err, tc.want, tc.wantErr)
		}
	}
}

// TestCgroupManagerPrepareInitializesOnce pins the eager startup delegation: a
// failed prepare is not memoized, and once it succeeds a later Create reuses it.
func TestCgroupManagerPrepareInitializesOnce(t *testing.T) {
	parent := t.TempDir()
	leaf := filepath.Join(parent, "vm-after-prepare")
	if err := os.Mkdir(leaf, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(leaf, "memory.events"), []byte("oom_kill 0\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	// A populated leaf, so the startup sweep leaves it alone.
	if err := os.WriteFile(filepath.Join(leaf, "cgroup.procs"), []byte("1\n"), 0o600); err != nil {
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
	if err := manager.prepare(); err == nil {
		t.Fatal("first prepare unexpectedly succeeded")
	}
	if err := manager.prepare(); err != nil {
		t.Fatalf("second prepare did not recover: %v", err)
	}
	if _, err := manager.Create("vm-after-prepare", 1024, 0); err != nil {
		t.Fatalf("Create after prepare: %v", err)
	}
	if attempts != 2 {
		t.Fatalf("initialize attempts = %d, want 2", attempts)
	}
}

// TestSweepEmptyLeafCgroupsRemovesOnlyProcesslessLeaves pins J9's cgroup half:
// only a leaf whose whole subtree has no member process is removed.
func TestSweepEmptyLeafCgroupsRemovesOnlyProcesslessLeaves(t *testing.T) {
	parent := t.TempDir()
	leaf := func(rel, procs string) {
		dir := filepath.Join(parent, rel)
		if err := os.MkdirAll(dir, 0o755); err != nil {
			t.Fatal(err)
		}
		if procs != "-" {
			if err := os.WriteFile(filepath.Join(dir, "cgroup.procs"), []byte(procs), 0o644); err != nil {
				t.Fatal(err)
			}
		}
	}
	// Every cgroup directory carries cgroup.procs, as on cgroupfs.
	leaf("vm-empty", "")
	leaf("vm-nested-empty", "\n")
	leaf("vm-nested-empty/firecracker", "")
	leaf("vm-nested-empty/firecracker/vm-nested-empty", "")
	leaf("vm-live", "1234\n")
	leaf("vm-live-child", "")
	leaf("vm-live-child/firecracker", "")
	leaf("vm-live-child/firecracker/vm-live-child", "99\n")
	leaf("vm-unreadable", "-")
	if err := os.WriteFile(filepath.Join(parent, "memory.max"), []byte("max"), 0o644); err != nil {
		t.Fatal(err)
	}
	var calls []string
	orig := removeLeafCgroup
	removeLeafCgroup = func(dir string) error {
		calls = append(calls, filepath.Base(dir))
		return os.RemoveAll(dir) // a plain filesystem has real control files
	}
	t.Cleanup(func() { removeLeafCgroup = orig })

	removed, err := sweepEmptyLeafCgroups(parent)
	if err == nil {
		t.Fatal("a leaf with unreadable cgroup.procs was not reported")
	}
	slices.Sort(calls)
	if want := []string{"vm-empty", "vm-nested-empty"}; !slices.Equal(calls, want) {
		t.Fatalf("removed leaves = %q, want %q", calls, want)
	}
	if len(removed) != 2 {
		t.Fatalf("returned %q, want two removed leaves", removed)
	}
	for _, kept := range []string{"vm-live", "vm-live-child", "vm-unreadable"} {
		if _, err := os.Stat(filepath.Join(parent, kept)); err != nil {
			t.Fatalf("sweep touched %s: %v", kept, err)
		}
	}
}

func TestVMCgroupContainsPid(t *testing.T) {
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "cgroup.procs"), []byte("12\n345\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	cg := &vmCgroup{dir: dir}
	for pid, want := range map[int]bool{12: true, 345: true, 34: false, 1: false} {
		got, err := cg.contains(pid)
		if err != nil || got != want {
			t.Errorf("contains(%d) = %v, %v; want %v", pid, got, err, want)
		}
	}
	if _, err := (&vmCgroup{dir: filepath.Join(dir, "missing")}).contains(12); err == nil {
		t.Fatal("contains on a missing leaf reported no error")
	}
}
