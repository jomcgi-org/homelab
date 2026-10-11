//go:build linux

package driver

import (
	"os"
	"path/filepath"
	"slices"
	"syscall"
	"testing"
)

func linkCount(t *testing.T, path string) uint64 {
	t.Helper()
	info, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	return uint64(info.Sys().(*syscall.Stat_t).Nlink)
}

// TestSweepOrphanJailsRemovesOnlyJailTrees pins J9: a crashed daemon's jail
// trees under its own warmth root are removed, releasing the inodes they pinned,
// while thread dirs, class dirs, deeper trees and symlinked entries survive.
func TestSweepOrphanJailsRemovesOnlyJailTrees(t *testing.T) {
	root := t.TempDir()
	base := filepath.Join(root, "bases", "k", "memfile")
	if err := os.MkdirAll(filepath.Dir(base), 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(base, []byte("memory"), 0o600); err != nil {
		t.Fatal(err)
	}
	thread := filepath.Join(root, "sess-aaaa")
	jail, err := prepareJail(thread, "/opt/fc/firecracker", "vm-old", os.Getuid(), os.Getgid(), filepath.Join(thread, "api.sock"))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := jail.stageInput("snapshot-memory", base, "/snapshot/memfile", false); err != nil {
		t.Fatal(err)
	}
	if got := linkCount(t, base); got != 2 {
		t.Fatalf("staged base link count = %d, want 2", got)
	}
	// A durable snapshot in the thread dir itself is not part of the jail.
	if err := os.WriteFile(filepath.Join(thread, "snapfile"), []byte("snap"), 0o600); err != nil {
		t.Fatal(err)
	}
	sentinel := filepath.Join(root, "sessions", "sess-x", "memfile")
	deep := filepath.Join(root, "i", "seg", "thread-y", "jailer", "firecracker", "vm", "root", "f")
	linkTarget := filepath.Join(t.TempDir(), "elsewhere", "f")
	for _, path := range []string{sentinel, deep, linkTarget} {
		if err := os.MkdirAll(filepath.Dir(path), 0o750); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, []byte("keep"), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.MkdirAll(filepath.Join(root, "thread-z"), 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(filepath.Dir(linkTarget), filepath.Join(root, "thread-z", "jailer")); err != nil {
		t.Fatal(err)
	}

	var failures []string
	removed := SweepOrphanJails(root, func(path string, err error) { failures = append(failures, path+": "+err.Error()) })
	if len(failures) != 0 {
		t.Fatalf("sweep failures: %q", failures)
	}
	if want := []string{filepath.Join(thread, "jailer")}; !slices.Equal(removed, want) {
		t.Fatalf("removed = %q, want %q", removed, want)
	}
	if got := linkCount(t, base); got != 1 {
		t.Fatalf("base link count after sweep = %d, want 1", got)
	}
	for _, kept := range []string{filepath.Join(thread, "snapfile"), sentinel, deep, linkTarget} {
		if _, err := os.Stat(kept); err != nil {
			t.Fatalf("sweep touched %s: %v", kept, err)
		}
	}
	if got := SweepOrphanJails(filepath.Join(root, "missing"), nil); got != nil {
		t.Fatalf("missing root swept %q", got)
	}
	if got := SweepOrphanJails("", nil); got != nil {
		t.Fatalf("empty root swept %q", got)
	}
}

// TestUIDLeaseLocksAreIgnoredBySweep confirms the uid lease lock files, plain
// files in the node-shared snapshot root, are inert to the startup jail sweep.
func TestUIDLeaseLocksAreIgnoredBySweep(t *testing.T) {
	dir := t.TempDir()
	lease, err := leaseUIDBlock(dir)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = lease.release() })
	if removed := SweepOrphanJails(dir, func(path string, err error) { t.Errorf("sweep error at %s: %v", path, err) }); len(removed) != 0 {
		t.Fatalf("sweep removed %q from a root holding only lease files", removed)
	}
	if _, err := os.Stat(filepath.Join(dir, uidLeaseLockName(0))); err != nil {
		t.Fatalf("lease lock file disappeared: %v", err)
	}
}

// TestRemoveJailTreeDetachesMountsFirst checks the bind-mount fallback: every
// mount at or below the tree is detached, deepest first, before removal.
func TestRemoveJailTreeDetachesMountsFirst(t *testing.T) {
	root := t.TempDir()
	tree := filepath.Join(root, "thread a", "jailer")
	if err := os.MkdirAll(filepath.Join(tree, "firecracker", "vm", "root"), 0o750); err != nil {
		t.Fatal(err)
	}
	mountinfo := filepath.Join(t.TempDir(), "mountinfo")
	escaped := filepath.Join(root, `thread\040a`, "jailer")
	lines := "" +
		"22 1 0:21 / /proc rw - proc proc rw\n" +
		"90 22 0:50 /x " + escaped + "/firecracker/vm/root/rootfs rw - ext4 /dev/sda rw\n" +
		"91 22 0:50 /y " + escaped + "/firecracker/vm/root rw - ext4 /dev/sda rw\n" +
		"92 22 0:50 /z " + root + `/thread\040b/jailer rw - ext4 /dev/sda rw` + "\n"
	if err := os.WriteFile(mountinfo, []byte(lines), 0o644); err != nil {
		t.Fatal(err)
	}
	origInfo, origUnmount := procSelfMountinfoPath, unmountJailPath
	var detached []string
	procSelfMountinfoPath = mountinfo
	unmountJailPath = func(target string) error { detached = append(detached, target); return nil }
	t.Cleanup(func() { procSelfMountinfoPath, unmountJailPath = origInfo, origUnmount })

	if err := removeJailTree(tree); err != nil {
		t.Fatalf("removeJailTree: %v", err)
	}
	want := []string{
		filepath.Join(tree, "firecracker", "vm", "root", "rootfs"),
		filepath.Join(tree, "firecracker", "vm", "root"),
	}
	if !slices.Equal(detached, want) {
		t.Fatalf("detached = %q, want %q", detached, want)
	}
	if _, err := os.Stat(tree); !os.IsNotExist(err) {
		t.Fatalf("tree survived: %v", err)
	}
}

// TestRemoveJailTreeUnmountsRealBindMount exercises a real bind-mount fallback.
func TestRemoveJailTreeUnmountsRealBindMount(t *testing.T) {
	if os.Geteuid() != 0 {
		t.Skip("bind mounts require CAP_SYS_ADMIN")
	}
	root := t.TempDir()
	host := filepath.Join(root, "host-file")
	if err := os.WriteFile(host, []byte("host"), 0o600); err != nil {
		t.Fatal(err)
	}
	tree := filepath.Join(root, "thread", "jailer")
	target := filepath.Join(tree, "firecracker", "vm", "root", "file")
	if err := os.MkdirAll(filepath.Dir(target), 0o750); err != nil {
		t.Fatal(err)
	}
	if err := bindMountFile(host, target, true); err != nil {
		t.Skipf("bind mount unavailable here: %v", err)
	}
	if err := removeJailTree(tree); err != nil {
		_ = unmountFile(target)
		t.Fatalf("removeJailTree: %v", err)
	}
	if _, err := os.Stat(tree); !os.IsNotExist(err) {
		t.Fatalf("tree survived: %v", err)
	}
	if got, err := os.ReadFile(host); err != nil || string(got) != "host" {
		t.Fatalf("host file after sweep = %q, %v", got, err)
	}
}
