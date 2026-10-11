package driver

import (
	"context"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"testing"

	"github.com/jomcgi/homelab/projects/embervm/noded/fcvm/fcclient"
	"github.com/jomcgi/homelab/projects/embervm/noded/substrate"
)

// ownershipCall is one recorded chown or chmod through the jail seams.
type ownershipCall struct {
	path     string
	uid, gid int
	mode     fs.FileMode
}

// ownershipRecorder replaces chownJailFile and chmodJailFile for one test.
// chown is recorded and, by default, not applied, so an unprivileged test can
// stage for a foreign jail uid. chmod is recorded and applied unless the test
// overrides it.
type ownershipRecorder struct {
	mu     sync.Mutex
	chowns []ownershipCall
	chmods []ownershipCall
	chown  func(path string, uid, gid int) error
	chmod  func(path string, mode fs.FileMode) error
}

func recordJailOwnership(t *testing.T) *ownershipRecorder {
	t.Helper()
	rec := &ownershipRecorder{
		chown: func(string, int, int) error { return nil },
		chmod: os.Chmod,
	}
	prevChown, prevChmod := chownJailFile, chmodJailFile
	chownJailFile = func(path string, uid, gid int) error {
		rec.mu.Lock()
		rec.chowns = append(rec.chowns, ownershipCall{path: path, uid: uid, gid: gid})
		chown := rec.chown
		rec.mu.Unlock()
		return chown(path, uid, gid)
	}
	chmodJailFile = func(path string, mode fs.FileMode) error {
		rec.mu.Lock()
		rec.chmods = append(rec.chmods, ownershipCall{path: path, mode: mode})
		chmod := rec.chmod
		rec.mu.Unlock()
		return chmod(path, mode)
	}
	t.Cleanup(func() { chownJailFile, chmodJailFile = prevChown, prevChmod })
	return rec
}

func (r *ownershipRecorder) chownsOf(path string) []ownershipCall {
	r.mu.Lock()
	defer r.mu.Unlock()
	var out []ownershipCall
	for _, call := range r.chowns {
		if call.path == path {
			out = append(out, call)
		}
	}
	return out
}

func (r *ownershipRecorder) chmodsOf(path string) []ownershipCall {
	r.mu.Lock()
	defer r.mu.Unlock()
	var out []ownershipCall
	for _, call := range r.chmods {
		if call.path == path {
			out = append(out, call)
		}
	}
	return out
}

func writeJailFixture(t *testing.T, path string, mode fs.FileMode) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(filepath.Base(path)), mode); err != nil {
		t.Fatal(err)
	}
	// WriteFile honours the umask; pin the exact fixture mode.
	if err := os.Chmod(path, mode); err != nil {
		t.Fatal(err)
	}
}

func statOwner(t *testing.T, path string) (fs.FileMode, int, int, uint64) {
	t.Helper()
	info, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	stat := info.Sys().(*syscall.Stat_t)
	return info.Mode().Perm(), int(stat.Uid), int(stat.Gid), uint64(stat.Nlink)
}

func TestJailCanReadFollowsDACClasses(t *testing.T) {
	const jailUID, jailGID = 20001, 20001
	for _, tc := range []struct {
		name             string
		mode             fs.FileMode
		fileUID, fileGID int
		want             bool
	}{
		{name: "root 0600 published rootfs", mode: 0o600, want: false},
		{name: "root 0640", mode: 0o640, want: false},
		{name: "root 0644 kernel", mode: 0o644, want: true},
		{name: "root 0444", mode: 0o444, want: true},
		{name: "root 0604 other read only", mode: 0o604, want: true},
		{name: "group class grants", mode: 0o640, fileGID: jailGID, want: true},
		{name: "group class is final", mode: 0o604, fileGID: jailGID, want: false},
		{name: "owner class grants", mode: 0o400, fileUID: jailUID, want: true},
		{name: "owner class is final", mode: 0o044, fileUID: jailUID, fileGID: jailGID, want: false},
		{name: "type bits ignored", mode: fs.ModeDir | 0o755, want: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if got := jailCanRead(tc.mode, tc.fileUID, tc.fileGID, jailUID, jailGID); got != tc.want {
				t.Fatalf("jailCanRead(%04o, uid %d, gid %d) = %v, want %v", tc.mode, tc.fileUID, tc.fileGID, got, tc.want)
			}
		})
	}
}

// TestStageReadOnlyInputsReadableByForeignJailUID is the unprivileged half of
// the 2026-09-06 EACCES regression: every read-only input, the rootfs and the
// kernel included, must end up readable through the other class for a jail uid
// that does not own it, without a chown and without touching a file that is
// already readable.
func TestStageReadOnlyInputsReadableByForeignJailUID(t *testing.T) {
	rec := recordJailOwnership(t)
	bundle := t.TempDir()
	jail, err := prepareJail(bundle, "/opt/fc/firecracker", "vm-ro", 20001, 20001, filepath.Join(bundle, "api.sock"))
	if err != nil {
		t.Fatalf("prepareJail: %v", err)
	}
	t.Cleanup(func() { _ = jail.Cleanup() })
	inputs := filepath.Join(bundle, "inputs")
	for _, role := range []string{"rootfs", "kernel", "initrd", "handler", "snapshot-state", "snapshot-memory"} {
		host := filepath.Join(inputs, role)
		writeJailFixture(t, host, 0o600)
		_, beforeUID, beforeGID, _ := statOwner(t, host)
		jailPath, err := jail.stageInput(role, host, "/in/"+role, false)
		if err != nil {
			t.Fatalf("stage %s: %v", role, err)
		}
		mode, uid, gid, _ := statOwner(t, host)
		if mode != 0o644 {
			t.Errorf("%s mode = %04o, want 0644 (a+r, no write bits)", role, mode)
		}
		if uid != beforeUID || gid != beforeGID {
			t.Errorf("%s owner changed to %d:%d", role, uid, gid)
		}
		if calls := rec.chownsOf(host); len(calls) != 0 {
			t.Errorf("%s was chowned for a read-only stage: %+v", role, calls)
		}
		staged, err := jail.hostPath(jailPath)
		if err != nil {
			t.Fatal(err)
		}
		hostInfo, _ := os.Stat(host)
		stagedInfo, err := os.Stat(staged)
		if err != nil || !os.SameFile(hostInfo, stagedInfo) {
			t.Errorf("%s is not staged as the host inode: %v", role, err)
		}
	}

	readable := filepath.Join(inputs, "vmlinux")
	writeJailFixture(t, readable, 0o644)
	if _, err := jail.stageInput("kernel-readable", readable, "/kernel", false); err != nil {
		t.Fatalf("stage readable kernel: %v", err)
	}
	if calls := rec.chmodsOf(readable); len(calls) != 0 {
		t.Fatalf("already readable kernel was chmodded: %+v", calls)
	}
}

// TestStageReadOnlyInputReownsForeignOwner covers a bundle captured while the
// jailer was briefly armed: its files are owned by a pool uid, which a later
// VMM reusing that uid could rewrite. Staging hands it back to the daemon.
func TestStageReadOnlyInputReownsForeignOwner(t *testing.T) {
	rec := recordJailOwnership(t)
	bundle := t.TempDir()
	jail, err := prepareJail(bundle, "/opt/fc/firecracker", "vm-reown", os.Getuid(), os.Getgid(), filepath.Join(bundle, "api.sock"))
	if err != nil {
		t.Fatalf("prepareJail: %v", err)
	}
	t.Cleanup(func() { _ = jail.Cleanup() })
	// Simulate a daemon whose identity differs from the file owner, which is
	// what a root daemon sees for a file a pool uid created.
	jail.ownerUID, jail.ownerGID = os.Getuid()+1, os.Getgid()+1
	memfile := filepath.Join(bundle, "bases", "k", "memfile")
	writeJailFixture(t, memfile, 0o600)
	if _, err := jail.stageInput("snapshot-memory", memfile, "/snapshot/memfile", false); err != nil {
		t.Fatalf("stage foreign-owned memfile: %v", err)
	}
	calls := rec.chownsOf(memfile)
	if len(calls) != 1 || calls[0].uid != jail.ownerUID || calls[0].gid != jail.ownerGID {
		t.Fatalf("memfile chowns = %+v, want one handoff to %d:%d", calls, jail.ownerUID, jail.ownerGID)
	}

	t.Run("refuses when the handoff fails", func(t *testing.T) {
		rec.chown = func(string, int, int) error { return fs.ErrPermission }
		snapfile := filepath.Join(bundle, "bases", "k", "snapfile")
		writeJailFixture(t, snapfile, 0o600)
		_, err := jail.stageInput("snapshot-state", snapfile, "/snapshot/snapfile", false)
		if err == nil || !strings.Contains(err.Error(), "re-own") {
			t.Fatalf("stage error = %v, want re-own failure", err)
		}
		if _, statErr := os.Lstat(filepath.Join(jail.RootDir, "snapshot", "snapfile")); !os.IsNotExist(statErr) {
			t.Fatalf("refused input was still staged in the jail: %v", statErr)
		}
	})
}

func TestStageReadOnlyInputRefusesUnreadableInput(t *testing.T) {
	for _, tc := range []struct {
		name    string
		chmod   func(string, fs.FileMode) error
		wantErr error
	}{
		{name: "chmod fails", chmod: func(string, fs.FileMode) error { return syscall.EROFS }, wantErr: syscall.EROFS},
		{name: "chmod has no effect", chmod: func(string, fs.FileMode) error { return nil }},
	} {
		t.Run(tc.name, func(t *testing.T) {
			rec := recordJailOwnership(t)
			rec.chmod = tc.chmod
			bundle := t.TempDir()
			jail, err := prepareJail(bundle, "/opt/fc/firecracker", "vm-refuse", 20001, 20001, filepath.Join(bundle, "api.sock"))
			if err != nil {
				t.Fatalf("prepareJail: %v", err)
			}
			t.Cleanup(func() { _ = jail.Cleanup() })
			rootfs := filepath.Join(bundle, "rootfs.ext4")
			writeJailFixture(t, rootfs, 0o600)
			_, err = jail.stageInput("rootfs", rootfs, rootfs, false)
			var unreadable *JailResourceUnreadableError
			if !errors.As(err, &unreadable) {
				t.Fatalf("stage error = %v, want *JailResourceUnreadableError", err)
			}
			if unreadable.Role != "rootfs" || unreadable.Path != rootfs || unreadable.Mode != 0o600 || unreadable.UID != 20001 {
				t.Fatalf("unreadable error = %+v", unreadable)
			}
			if tc.wantErr == nil && unreadable.Err != nil || tc.wantErr != nil && !errors.Is(err, tc.wantErr) {
				t.Fatalf("stage error = %v, want wrapped %v", err, tc.wantErr)
			}
			for _, want := range []string{"rootfs", rootfs, "0600", "20001"} {
				if !strings.Contains(err.Error(), want) {
					t.Errorf("error %q does not name %q", err, want)
				}
			}
			staged, _ := jail.hostPath(rootfs)
			if _, statErr := os.Lstat(staged); !os.IsNotExist(statErr) {
				t.Fatalf("unreadable rootfs was still staged: %v", statErr)
			}
		})
	}
}

// snapshotCreateAPI stands in for Firecracker writing both snapshot outputs
// through their jail paths.
type snapshotCreateAPI struct {
	fcAPI
	jail  *Jail
	err   error
	write func(jailPath string, data []byte) error
	calls []fcclient.SnapshotCreate
}

func (a *snapshotCreateAPI) CreateSnapshot(_ context.Context, s fcclient.SnapshotCreate) error {
	a.calls = append(a.calls, s)
	write := a.write
	if write == nil {
		write = func(jailPath string, data []byte) error {
			host, err := a.jail.hostPath(jailPath)
			if err != nil {
				return err
			}
			return os.WriteFile(host, data, 0o600)
		}
	}
	if err := write(s.SnapshotPath, []byte("state")); err != nil {
		return err
	}
	if err := write(s.MemFilePath, []byte("memory")); err != nil {
		return err
	}
	return a.err
}

func assertSnapshotOutputsReleased(t *testing.T, jail *Jail, paths ...string) {
	t.Helper()
	entries, err := os.ReadDir(filepath.Join(jail.RootDir, "snapshots"))
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 0 {
		t.Fatalf("jail /snapshots still holds %v", entries)
	}
	for _, path := range paths {
		if mode, _, _, nlink := statOwner(t, path); mode != 0o600 || nlink != 1 {
			t.Fatalf("%s mode %04o nlink %d, want 0600 with no jail alias", path, mode, nlink)
		}
	}
}

func TestJailedCreateSnapshotReleasesOutputs(t *testing.T) {
	const jailUID, jailGID = 20001, 20002
	newClient := func(t *testing.T) (*jailedClient, *snapshotCreateAPI, *ownershipRecorder, string, string) {
		rec := recordJailOwnership(t)
		bundle := t.TempDir()
		jail, err := prepareJail(bundle, "/opt/fc/firecracker", "vm-snap", jailUID, jailGID, filepath.Join(bundle, "api.sock"))
		if err != nil {
			t.Fatalf("prepareJail: %v", err)
		}
		t.Cleanup(func() { _ = jail.Cleanup() })
		api := &snapshotCreateAPI{jail: jail}
		return &jailedClient{fcAPI: api, jail: jail}, api, rec, filepath.Join(bundle, "snapfile.tmp"), filepath.Join(bundle, "memfile.tmp")
	}
	wantHandoffs := func(t *testing.T, rec *ownershipRecorder, path string) {
		t.Helper()
		calls := rec.chownsOf(path)
		want := []ownershipCall{{path: path, uid: jailUID, gid: jailGID}, {path: path, uid: os.Geteuid(), gid: os.Getegid()}}
		if fmt.Sprint(calls) != fmt.Sprint(want) {
			t.Fatalf("%s chowns = %+v, want jail handoff then daemon handoff %+v", path, calls, want)
		}
	}

	t.Run("success", func(t *testing.T) {
		client, api, rec, snap, mem := newClient(t)
		if err := client.CreateSnapshot(context.Background(), fcclient.SnapshotCreate{SnapshotPath: snap, MemFilePath: mem}); err != nil {
			t.Fatalf("CreateSnapshot: %v", err)
		}
		if len(api.calls) != 1 || !strings.HasPrefix(api.calls[0].SnapshotPath, "/snapshots/") || !strings.HasPrefix(api.calls[0].MemFilePath, "/snapshots/") {
			t.Fatalf("Firecracker saw %+v, want jail paths", api.calls)
		}
		wantHandoffs(t, rec, snap)
		wantHandoffs(t, rec, mem)
		assertSnapshotOutputsReleased(t, client.jail, snap, mem)
		for path, want := range map[string]string{snap: "state", mem: "memory"} {
			if got, err := os.ReadFile(path); err != nil || string(got) != want {
				t.Fatalf("%s = %q, %v, want %q", path, got, err, want)
			}
		}
		// A retry on the same host paths (diff then full bank) must restage cleanly.
		if err := client.CreateSnapshot(context.Background(), fcclient.SnapshotCreate{SnapshotPath: snap, MemFilePath: mem}); err != nil {
			t.Fatalf("second CreateSnapshot: %v", err)
		}
		assertSnapshotOutputsReleased(t, client.jail, snap, mem)
	})

	t.Run("api error", func(t *testing.T) {
		client, api, rec, snap, mem := newClient(t)
		api.err = errors.New("injected snapshot failure")
		err := client.CreateSnapshot(context.Background(), fcclient.SnapshotCreate{SnapshotPath: snap, MemFilePath: mem})
		if err != api.err {
			t.Fatalf("CreateSnapshot error = %v, want the API error unchanged", err)
		}
		wantHandoffs(t, rec, snap)
		wantHandoffs(t, rec, mem)
		assertSnapshotOutputsReleased(t, client.jail, snap, mem)
	})

	t.Run("second output fails to stage", func(t *testing.T) {
		client, api, rec, snap, _ := newClient(t)
		missing := filepath.Join(filepath.Dir(snap), "missing", "memfile.tmp")
		if err := client.CreateSnapshot(context.Background(), fcclient.SnapshotCreate{SnapshotPath: snap, MemFilePath: missing}); err == nil {
			t.Fatal("CreateSnapshot succeeded with an uncreatable memory output")
		}
		if len(api.calls) != 0 {
			t.Fatalf("Firecracker was called after a stage failure: %+v", api.calls)
		}
		wantHandoffs(t, rec, snap)
		assertSnapshotOutputsReleased(t, client.jail, snap)
	})

	t.Run("release failure fails the snapshot", func(t *testing.T) {
		client, _, rec, snap, mem := newClient(t)
		rec.chown = func(path string, uid, _ int) error {
			if uid == os.Geteuid() && path == mem {
				return fs.ErrPermission
			}
			return nil
		}
		err := client.CreateSnapshot(context.Background(), fcclient.SnapshotCreate{SnapshotPath: snap, MemFilePath: mem})
		if err == nil || !errors.Is(err, fs.ErrPermission) || !strings.Contains(err.Error(), "release jailed snapshot outputs") {
			t.Fatalf("CreateSnapshot error = %v, want a release failure", err)
		}
		entries, readErr := os.ReadDir(filepath.Join(client.jail.RootDir, "snapshots"))
		if readErr != nil || len(entries) != 0 {
			t.Fatalf("jail aliases after a release failure = %v, %v", entries, readErr)
		}
	})
}

// TestJailLegacyPlaceholderBackingIsPrivatePerJail pins the legacy restore
// path to a private copy: chowning the node-shared placeholder to each jail
// uid would let two concurrent legacy restores steal it from each other.
func TestJailLegacyPlaceholderBackingIsPrivatePerJail(t *testing.T) {
	launcher := &fakeLauncher{}
	root := shortTempDir(t)
	d := New(Config{SnapshotRoot: root, Arch: "amd64"}, launcher, nil)
	dir := d.baseDir("legacy")
	if err := os.MkdirAll(dir, 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "snapfile"), []byte("snap"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "rootfspath"), []byte("/rootfs"), 0o600); err != nil {
		t.Fatal(err)
	}
	h, err := d.Claim(context.Background(), substrate.ClaimSpec{
		ThreadID: "legacy-private", BaseSnapshotRef: substrate.SnapshotRef{ID: "legacy"},
	})
	if err != nil {
		t.Fatalf("Claim: %v", err)
	}
	t.Cleanup(func() { _ = d.Release(context.Background(), h) })
	specs := launcher.specs()
	if len(specs) != 1 {
		t.Fatalf("launch specs = %d, want 1", len(specs))
	}
	var backing *JailResource
	for i := range specs[0].Resources {
		if specs[0].Resources[i].Role == "legacy-placeholder-backing" {
			backing = &specs[0].Resources[i]
		}
	}
	if backing == nil || !backing.Writable || !backing.PrivateCopy {
		t.Fatalf("legacy placeholder backing = %+v, want writable private copy", backing)
	}

	sharedMode, sharedUID, sharedGID, _ := statOwner(t, backing.HostPath)
	shared, err := os.Stat(backing.HostPath)
	if err != nil {
		t.Fatal(err)
	}
	var staged []os.FileInfo
	for _, vm := range []string{"vm-a", "vm-b"} {
		bundle := filepath.Join(shortTempDir(t), "thread")
		jail, err := prepareJail(bundle, "/opt/fc/firecracker", vm, os.Getuid(), os.Getgid(), filepath.Join(bundle, "api.sock"))
		if err != nil {
			t.Fatalf("prepareJail %s: %v", vm, err)
		}
		t.Cleanup(func() { _ = jail.Cleanup() })
		jailPath, err := jail.stageResource(*backing)
		if err != nil {
			t.Fatalf("stage %s backing: %v", vm, err)
		}
		host, _ := jail.hostPath(jailPath)
		info, err := os.Stat(host)
		if err != nil {
			t.Fatal(err)
		}
		if os.SameFile(info, shared) {
			t.Fatalf("%s staged the shared placeholder inode", vm)
		}
		staged = append(staged, info)
	}
	if os.SameFile(staged[0], staged[1]) {
		t.Fatal("two jails share one placeholder copy")
	}
	if mode, uid, gid, _ := statOwner(t, backing.HostPath); mode != sharedMode || uid != sharedUID || gid != sharedGID {
		t.Fatalf("shared placeholder changed to %04o %d:%d", mode, uid, gid)
	}
}

// TestPrepareJailFollowsJailerExecFileResolution pins prepareJail to the
// chroot the real jailer derives: <chroot-base>/<basename of the canonical
// --exec-file>/<id>/root. Staging anywhere else leaves the API socket alias
// pointing at a directory the jailed Firecracker never sees.
func TestPrepareJailFollowsJailerExecFileResolution(t *testing.T) {
	bin := filepath.Join(t.TempDir(), "bin")
	if err := os.MkdirAll(bin, 0o755); err != nil {
		t.Fatal(err)
	}
	target := filepath.Join(bin, "firecracker-v1.16.1-x86_64")
	if err := os.WriteFile(target, []byte("vmm"), 0o755); err != nil {
		t.Fatal(err)
	}
	link := filepath.Join(bin, "firecracker")
	if err := os.Symlink(filepath.Base(target), link); err != nil {
		t.Fatal(err)
	}
	chained := filepath.Join(bin, "firecracker-current")
	if err := os.Symlink(link, chained); err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct {
		name, execFile, wantName string
	}{
		{name: "regular file", execFile: target, wantName: "firecracker-v1.16.1-x86_64"},
		{name: "relative symlink", execFile: link, wantName: "firecracker-v1.16.1-x86_64"},
		{name: "symlink chain", execFile: chained, wantName: "firecracker-v1.16.1-x86_64"},
		{name: "unresolvable keeps literal name", execFile: "/opt/fc/firecracker", wantName: "firecracker"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			bundle := t.TempDir()
			socket := filepath.Join(bundle, "api.sock")
			jail, err := prepareJail(bundle, tc.execFile, "vm-exec", os.Getuid(), os.Getgid(), socket)
			if err != nil {
				t.Fatalf("prepareJail: %v", err)
			}
			t.Cleanup(func() { _ = jail.Cleanup() })
			want := filepath.Join(bundle, "jailer", tc.wantName, "vm-exec", "root")
			if jail.RootDir != want || jail.BaseDir != filepath.Join(bundle, "jailer") {
				t.Fatalf("jail root = %q base %q, want root %q", jail.RootDir, jail.BaseDir, want)
			}
			if got, err := os.Readlink(socket); err != nil || got != filepath.Join(want, "api.sock") {
				t.Fatalf("API socket alias = %q, %v, want inside %q", got, err, want)
			}
		})
	}
}
