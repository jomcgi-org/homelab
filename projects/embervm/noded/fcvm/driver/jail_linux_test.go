//go:build linux

package driver

import (
	"context"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"runtime"
	"syscall"
	"testing"

	"github.com/jomcgi/homelab/projects/embervm/noded/fcvm/fcclient"
)

// The first uid and gid a brick's pool hands out, and so the one most likely to
// be reused across bricks and restarts.
const testJailUID, testJailGID = vmUIDBase, vmUIDBase

// requireRootJailTest skips a test that needs a real uid drop, chroot or mount
// unless it runs as root. EMBERVM_REQUIRE_ROOT_TESTS=1 turns the skip into a
// failure, so a run meant to prove the jailer cannot pass vacuously.
func requireRootJailTest(t *testing.T) {
	t.Helper()
	if os.Geteuid() == 0 {
		return
	}
	if os.Getenv("EMBERVM_REQUIRE_ROOT_TESTS") == "1" {
		t.Fatal("EMBERVM_REQUIRE_ROOT_TESTS=1 but the test is not running as root")
	}
	t.Skip("needs root to confine a thread as a jail uid")
}

// asJailedVMM runs fn on a dedicated OS thread confined the way the jailer
// confines Firecracker: chrooted into root, every uid and gid set to the jail
// identity, no supplementary groups and, having left uid 0, no capabilities.
// CLONE_FS keeps the chroot on this thread. The thread is never unlocked, so
// the runtime discards it when fn returns instead of reusing it.
func asJailedVMM(root string, uid, gid int, fn func() error) error {
	done := make(chan error, 1)
	go func() {
		runtime.LockOSThread()
		done <- func() error {
			if err := syscall.Unshare(syscall.CLONE_FS); err != nil {
				return fmt.Errorf("unshare CLONE_FS: %w", err)
			}
			if err := syscall.Chroot(root); err != nil {
				return fmt.Errorf("chroot %s: %w", root, err)
			}
			if err := syscall.Chdir("/"); err != nil {
				return fmt.Errorf("chdir /: %w", err)
			}
			// Raw syscalls drop only this thread; syscall.Setuid would apply
			// the drop to every thread of the test process.
			if _, _, errno := syscall.RawSyscall(syscall.SYS_SETGROUPS, 0, 0, 0); errno != 0 {
				return fmt.Errorf("setgroups: %w", errno)
			}
			if _, _, errno := syscall.RawSyscall(syscall.SYS_SETRESGID, uintptr(gid), uintptr(gid), uintptr(gid)); errno != 0 {
				return fmt.Errorf("setresgid: %w", errno)
			}
			if _, _, errno := syscall.RawSyscall(syscall.SYS_SETRESUID, uintptr(uid), uintptr(uid), uintptr(uid)); errno != 0 {
				return fmt.Errorf("setresuid: %w", errno)
			}
			return fn()
		}()
	}()
	return <-done
}

func openInJail(path string, flag int) error {
	f, err := os.OpenFile(path, flag, 0)
	if err != nil {
		return err
	}
	return f.Close()
}

// readOnlyAsJail checks every jail path the way a jailed VMM sees it: readable,
// and neither writable nor chmod-able, because the file is not its own.
func readOnlyAsJail(paths ...string) error {
	var errs error
	for _, path := range paths {
		if err := openInJail(path, os.O_RDONLY); err != nil {
			errs = errors.Join(errs, fmt.Errorf("open %s read-only: %w", path, err))
		}
		if err := openInJail(path, os.O_RDWR); !errors.Is(err, fs.ErrPermission) {
			errs = errors.Join(errs, fmt.Errorf("open %s read-write = %v, want EACCES", path, err))
		}
		if err := os.Chmod(path, 0o666); !errors.Is(err, fs.ErrPermission) {
			errs = errors.Join(errs, fmt.Errorf("chmod %s = %v, want EPERM", path, err))
		}
	}
	return errs
}

// jailInputAPI accepts the configuration calls that stage read-only inputs.
type jailInputAPI struct{ fcAPI }

func (jailInputAPI) PutBootSource(context.Context, fcclient.BootSource) error      { return nil }
func (jailInputAPI) PutDrive(context.Context, fcclient.Drive) error                { return nil }
func (jailInputAPI) LoadSnapshot(context.Context, fcclient.SnapshotLoad) error     { return nil }
func (jailInputAPI) CreateSnapshot(context.Context, fcclient.SnapshotCreate) error { return nil }

func prepareTestJail(t *testing.T, bundle, vmID string) *Jail {
	t.Helper()
	jail, err := prepareJail(bundle, "/opt/fc/firecracker", vmID, testJailUID, testJailGID, filepath.Join(bundle, "api.sock"))
	if err != nil {
		t.Fatalf("prepareJail %s: %v", vmID, err)
	}
	t.Cleanup(func() { _ = jail.Cleanup() })
	return jail
}

// TestJailedInputsOpenableAsJailUID is the regression proof for the
// 2026-09-06 arming: the base rootfs was published root-owned 0600 and staged
// without a read fixup, so the jailed Firecracker failed every boot and
// restore with "Permission denied (os error 13)".
func TestJailedInputsOpenableAsJailUID(t *testing.T) {
	requireRootJailTest(t)
	scratch := t.TempDir()
	// Publish the rootfs as the builder and rootfs-store did: a 0600
	// temporary renamed into the cache name, then hard-linked to the base path.
	tmp, err := os.CreateTemp(scratch, "rootfs.ext4.tmp.*")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := tmp.WriteString("ext4"); err != nil {
		t.Fatal(err)
	}
	if err := tmp.Close(); err != nil {
		t.Fatal(err)
	}
	cache := filepath.Join(scratch, "rootfs-cache.ext4")
	if err := os.Rename(tmp.Name(), cache); err != nil {
		t.Fatal(err)
	}
	baseRootfs := filepath.Join(scratch, "base-rootfs.ext4")
	if err := os.Link(cache, baseRootfs); err != nil {
		t.Fatal(err)
	}
	kernel := filepath.Join(scratch, "vmlinux")
	writeJailFixture(t, kernel, 0o644)
	snap := filepath.Join(scratch, "bases", "k", "snapfile")
	mem := filepath.Join(scratch, "bases", "k", "memfile")
	writeJailFixture(t, snap, 0o600)
	writeJailFixture(t, mem, 0o600)

	jail := prepareTestJail(t, filepath.Join(scratch, "thread"), "vm-open")
	client := &jailedClient{fcAPI: jailInputAPI{}, jail: jail}
	ctx := context.Background()
	if err := client.PutBootSource(ctx, fcclient.BootSource{KernelImagePath: kernel}); err != nil {
		t.Fatalf("PutBootSource: %v", err)
	}
	if err := client.PutDrive(ctx, fcclient.Drive{DriveID: "rootfs", PathOnHost: baseRootfs, IsRootDevice: true, IsReadOnly: true}); err != nil {
		t.Fatalf("PutDrive: %v", err)
	}
	if err := client.LoadSnapshot(ctx, fcclient.SnapshotLoad{
		SnapshotPath: snap,
		MemBackend:   &fcclient.MemBackend{BackendType: "File", BackendPath: mem},
	}); err != nil {
		t.Fatalf("LoadSnapshot: %v", err)
	}
	if err := asJailedVMM(jail.RootDir, testJailUID, testJailGID, func() error {
		return readOnlyAsJail("/kernel", baseRootfs, "/snapshot/snapfile", "/snapshot/memfile")
	}); err != nil {
		t.Fatal(err)
	}
	for _, path := range []string{cache, kernel, snap, mem} {
		if _, uid, gid, _ := statOwner(t, path); uid != 0 || gid != 0 {
			t.Errorf("%s owner = %d:%d, want root: shared inputs are never handed to a jail uid", path, uid, gid)
		}
	}
}

// TestJailedSnapshotOutputsReturnToDaemon proves the jail uid can write its
// snapshot outputs while Firecracker creates them, and that the finished files
// belong to the daemon, so a later VMM given the same recycled uid can read the
// published base but not rewrite it.
func TestJailedSnapshotOutputsReturnToDaemon(t *testing.T) {
	requireRootJailTest(t)
	scratch := t.TempDir()
	build := prepareTestJail(t, filepath.Join(scratch, "build"), "vm-build")
	api := &snapshotCreateAPI{jail: build, write: func(jailPath string, data []byte) error {
		return asJailedVMM(build.RootDir, testJailUID, testJailGID, func() error {
			return os.WriteFile(jailPath, data, 0)
		})
	}}
	client := &jailedClient{fcAPI: api, jail: build}
	dir := filepath.Join(scratch, "bases", "k")
	if err := os.MkdirAll(dir, 0o750); err != nil {
		t.Fatal(err)
	}
	snap, mem := filepath.Join(dir, "snapfile"), filepath.Join(dir, "memfile")
	if err := client.CreateSnapshot(context.Background(), fcclient.SnapshotCreate{SnapshotPath: snap, MemFilePath: mem}); err != nil {
		t.Fatalf("CreateSnapshot: %v", err)
	}
	assertSnapshotOutputsReleased(t, build, snap, mem)
	for _, path := range []string{snap, mem} {
		if _, uid, gid, _ := statOwner(t, path); uid != 0 || gid != 0 {
			t.Fatalf("%s owner = %d:%d after the snapshot, want root", path, uid, gid)
		}
	}

	restore := prepareTestJail(t, filepath.Join(scratch, "restore"), "vm-restore")
	restoreClient := &jailedClient{fcAPI: jailInputAPI{}, jail: restore}
	if err := restoreClient.LoadSnapshot(context.Background(), fcclient.SnapshotLoad{
		SnapshotPath: snap,
		MemBackend:   &fcclient.MemBackend{BackendType: "File", BackendPath: mem},
	}); err != nil {
		t.Fatalf("LoadSnapshot: %v", err)
	}
	if err := asJailedVMM(restore.RootDir, testJailUID, testJailGID, func() error {
		return readOnlyAsJail("/snapshot/snapfile", "/snapshot/memfile")
	}); err != nil {
		t.Fatal(err)
	}
}

// TestStageReadOnlyInputReownsJailOwnedBundle heals a bundle captured while
// the jailer was briefly armed, whose memfile a pool uid still owns.
func TestStageReadOnlyInputReownsJailOwnedBundle(t *testing.T) {
	requireRootJailTest(t)
	scratch := t.TempDir()
	mem := filepath.Join(scratch, "sessions", "s", "memfile")
	writeJailFixture(t, mem, 0o600)
	if err := os.Chown(mem, testJailUID, testJailGID); err != nil {
		t.Fatal(err)
	}
	jail := prepareTestJail(t, filepath.Join(scratch, "thread"), "vm-heal")
	if _, err := jail.stageInput("snapshot-memory", mem, "/snapshot/memfile", false); err != nil {
		t.Fatalf("stage jail-owned memfile: %v", err)
	}
	if mode, uid, gid, _ := statOwner(t, mem); mode != 0o644 || uid != 0 || gid != 0 {
		t.Fatalf("memfile = %04o %d:%d, want 0644 root:root", mode, uid, gid)
	}
	if err := asJailedVMM(jail.RootDir, testJailUID, testJailGID, func() error {
		return readOnlyAsJail("/snapshot/memfile")
	}); err != nil {
		t.Fatal(err)
	}
}

func bindMountForTest(t *testing.T, source, target string, readOnly bool) {
	t.Helper()
	if err := syscall.Mount(source, target, "", syscall.MS_BIND, ""); err != nil {
		t.Skipf("bind mount unavailable: %v", err)
	}
	t.Cleanup(func() { _ = syscall.Unmount(target, syscall.MNT_DETACH) })
	if readOnly {
		if err := syscall.Mount("", target, "", syscall.MS_BIND|syscall.MS_REMOUNT|syscall.MS_RDONLY, ""); err != nil {
			t.Fatalf("remount %s read-only: %v", target, err)
		}
	}
}

// TestStageReadOnlyInputOnReadOnlyMount pins the two halves of the readability
// rule on a filesystem that refuses chmod: an input that is already readable
// is staged untouched, and one that is not fails with a typed error before
// Firecracker starts.
func TestStageReadOnlyInputOnReadOnlyMount(t *testing.T) {
	requireRootJailTest(t)
	scratch := t.TempDir()
	src, ro := filepath.Join(scratch, "src"), filepath.Join(scratch, "ro")
	writeJailFixture(t, filepath.Join(src, "vmlinux"), 0o644)
	writeJailFixture(t, filepath.Join(src, "rootfs.ext4"), 0o600)
	if err := os.MkdirAll(ro, 0o755); err != nil {
		t.Fatal(err)
	}
	bindMountForTest(t, src, ro, true)
	jail := prepareTestJail(t, filepath.Join(scratch, "thread"), "vm-ro-mount")

	if _, err := jail.stageInput("kernel", filepath.Join(ro, "vmlinux"), "/kernel", false); err != nil {
		t.Fatalf("stage readable kernel from a read-only mount: %v", err)
	}
	if err := asJailedVMM(jail.RootDir, testJailUID, testJailGID, func() error {
		return openInJail("/kernel", os.O_RDONLY)
	}); err != nil {
		t.Fatal(err)
	}
	rootfs := filepath.Join(ro, "rootfs.ext4")
	_, err := jail.stageInput("rootfs", rootfs, rootfs, false)
	var unreadable *JailResourceUnreadableError
	if !errors.As(err, &unreadable) || !errors.Is(err, syscall.EROFS) || unreadable.Role != "rootfs" || unreadable.Mode != 0o600 {
		t.Fatalf("stage unreadable rootfs on a read-only mount = %v, want typed EROFS refusal", err)
	}
}

// TestJailedCreateSnapshotReleasesBindMountedOutputs covers outputs on another
// mount, where the hard link fails with EXDEV and staging bind-mounts instead.
// The release must unmount and remove the alias, or the next snapshot to the
// same host path fails with EBUSY.
func TestJailedCreateSnapshotReleasesBindMountedOutputs(t *testing.T) {
	requireRootJailTest(t)
	scratch := t.TempDir()
	src, out := filepath.Join(scratch, "out-src"), filepath.Join(scratch, "out")
	for _, dir := range []string{src, out} {
		if err := os.MkdirAll(dir, 0o750); err != nil {
			t.Fatal(err)
		}
	}
	bindMountForTest(t, src, out, false)
	jail := prepareTestJail(t, filepath.Join(scratch, "thread"), "vm-bind")
	client := &jailedClient{fcAPI: &snapshotCreateAPI{jail: jail}, jail: jail}
	snap, mem := filepath.Join(out, "snapfile.tmp"), filepath.Join(out, "memfile.tmp")
	for attempt := range 2 {
		if err := client.CreateSnapshot(context.Background(), fcclient.SnapshotCreate{SnapshotPath: snap, MemFilePath: mem}); err != nil {
			t.Fatalf("CreateSnapshot attempt %d: %v", attempt, err)
		}
		jail.mu.Lock()
		mounts := append([]string(nil), jail.mounts...)
		jail.mu.Unlock()
		if len(mounts) != 0 {
			t.Fatalf("attempt %d left bind mounts %v", attempt, mounts)
		}
		assertSnapshotOutputsReleased(t, jail, snap, mem)
		for _, path := range []string{snap, mem} {
			if _, uid, _, _ := statOwner(t, path); uid != 0 {
				t.Fatalf("%s owner = %d, want root", path, uid)
			}
		}
	}
}
