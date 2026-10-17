//go:build linux

package egress

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/embervm/noded/vsockproto"
)

// The first uid a brick's jail pool hands out (driver.vmUIDBase).
const testJailUID, testJailGID = 20000, 20000

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

// TestServeEgressSocketConnectableByJailUID is the regression proof for guest
// egress under the jailer: Firecracker, chrooted and running as the jail uid,
// must be able to connect(2) to the socket ServeEgress binds through the
// symlink bindJailedVsock lays out. A root-owned 0755 socket refuses it with
// EACCES and the guest sees every outbound request reset.
func TestServeEgressSocketConnectableByJailUID(t *testing.T) {
	if os.Geteuid() != 0 {
		if os.Getenv("EMBERVM_REQUIRE_ROOT_TESTS") == "1" {
			t.Fatal("EMBERVM_REQUIRE_ROOT_TESTS=1 but the test is not running as root")
		}
		t.Skip("needs root to confine a thread as a jail uid")
	}
	sidecarAddr, stopSidecar := startEchoSidecar(t)
	defer stopSidecar()

	tmp := t.TempDir()
	// A long jail root takes listenUnix's /proc/self/fd branch, as real jail
	// roots under a thread bundle do.
	jailRoot := filepath.Join(tmp, strings.Repeat("j", 100), "root")
	if err := os.MkdirAll(jailRoot, 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(jailRoot, 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.Chown(jailRoot, testJailUID, testJailGID); err != nil {
		t.Fatal(err)
	}
	jailName := "/vsock.sock_" + itoa(int(vsockproto.EgressPort))
	jailedPath := filepath.Join(jailRoot, jailName)
	udsPath := filepath.Join(tmp, "host", "vsock.sock")
	if err := os.MkdirAll(filepath.Dir(udsPath), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(jailedPath, egressListenPath(udsPath)); err != nil {
		t.Fatal(err)
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	serveErr := make(chan error, 1)
	go func() { serveErr <- ServeEgress(ctx, slog.Default(), udsPath, sidecarAddr) }()

	err := asJailedVMM(jailRoot, testJailUID, testJailGID, func() error {
		// Retry while the listener comes up; a socket that stays root-owned
		// keeps failing with EACCES until the deadline.
		deadline := time.Now().Add(2 * time.Second)
		for {
			conn, err := net.Dial("unix", jailName)
			if err == nil {
				defer conn.Close()
				want := []byte("jailed guest to sidecar")
				if _, err := conn.Write(want); err != nil {
					return err
				}
				_ = conn.SetReadDeadline(time.Now().Add(2 * time.Second))
				got := make([]byte, len(want))
				if _, err := io.ReadFull(conn, got); err != nil {
					return err
				}
				if string(got) != string(want) {
					return fmt.Errorf("echo = %q, want %q", got, want)
				}
				return nil
			}
			if time.Now().After(deadline) {
				return fmt.Errorf("connect %s as uid %d: %w", jailName, testJailUID, err)
			}
			time.Sleep(10 * time.Millisecond)
		}
	})
	if err != nil {
		t.Fatal(err)
	}
	info, err := os.Lstat(jailedPath)
	if err != nil {
		t.Fatal(err)
	}
	if st := info.Sys().(*syscall.Stat_t); st.Uid != testJailUID || st.Gid != testJailGID {
		t.Fatalf("egress socket owner = %d:%d, want the jail's %d:%d", st.Uid, st.Gid, testJailUID, testJailGID)
	}

	cancel()
	if err := <-serveErr; err != nil && !errors.Is(err, context.Canceled) {
		t.Fatalf("ServeEgress: %v", err)
	}
}
