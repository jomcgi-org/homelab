package driver

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"testing"
)

// jailingLauncher wraps fakeLauncher so each process carries a real prepared
// jail, the way a jailed ExecLauncher launch does. The jail's API socket alias
// is a separate name so the fake's listening socket stays intact.
type jailingLauncher struct {
	*fakeLauncher
	mu sync.Mutex
	// staged is every file the jail held when its VMM was killed, as the
	// absolute path Firecracker saw inside the chroot.
	staged  []string
	killed  int
	unjail  bool
	failErr error
}

type jailingProcess struct {
	*fakeProcess
	owner *jailingLauncher
}

func (p *jailingProcess) Kill() error {
	if p.jail != nil {
		var staged []string
		_ = filepath.WalkDir(p.jail.RootDir, func(path string, entry os.DirEntry, err error) error {
			if err == nil && entry.Type().IsRegular() {
				rel, _ := filepath.Rel(p.jail.RootDir, path)
				staged = append(staged, "/"+rel)
			}
			return nil
		})
		p.owner.mu.Lock()
		p.owner.staged = append(p.owner.staged, staged...)
		p.owner.mu.Unlock()
		_ = p.jail.Cleanup()
	}
	p.owner.mu.Lock()
	p.owner.killed++
	p.owner.mu.Unlock()
	return p.fakeProcess.Kill()
}

func (l *jailingLauncher) Launch(ctx context.Context, spec LaunchSpec) (Process, error) {
	if l.failErr != nil {
		return nil, l.failErr
	}
	proc, err := l.fakeLauncher.Launch(ctx, spec)
	if err != nil {
		return nil, err
	}
	fake := proc.(*fakeProcess)
	if !l.unjail {
		dir := filepath.Dir(spec.SocketPath)
		jail, err := prepareJail(dir, "/opt/fc/firecracker", spec.VMID, os.Getuid(), os.Getgid(), filepath.Join(dir, "jail-api.sock"))
		if err != nil {
			_ = fake.Kill()
			return nil, err
		}
		fake.jail = jail
	}
	return &jailingProcess{fakeProcess: fake, owner: l}, nil
}

// preflightDriver builds a driver whose kernel and rootfs inputs live beside
// the warmth root, so staging hard-links them like production scratch does.
func preflightDriver(t *testing.T, launcher Launcher) (*Driver, string) {
	t.Helper()
	root := shortTempDir(t)
	kernel := filepath.Join(root, "vmlinux")
	if err := os.WriteFile(kernel, []byte("kernel"), 0o644); err != nil {
		t.Fatal(err)
	}
	return New(Config{KernelImagePath: kernel, SnapshotRoot: root, Node: "node-4", Arch: "amd64"}, launcher, nil), root
}

func writeRootfs(t *testing.T, root, name string) string {
	t.Helper()
	path := filepath.Join(root, "rootfs", name)
	if err := os.MkdirAll(filepath.Dir(path), 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte("ext4"), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestJailPreflightOpensProbeAndEachReadyRootfsThroughTheJail(t *testing.T) {
	launcher := &jailingLauncher{fakeLauncher: &fakeLauncher{}}
	d, root := preflightDriver(t, launcher)
	a := writeRootfs(t, root, "a.ext4")
	b := writeRootfs(t, root, "b.ext4")
	stale := filepath.Join(d.warmthRoot(), jailPreflightDirName, "run-old", "jailer", "firecracker", "vm", "root", "f")
	if err := os.MkdirAll(filepath.Dir(stale), 0o750); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(stale, nil, 0o600); err != nil {
		t.Fatal(err)
	}

	if err := d.JailPreflight(context.Background(), []string{a, b, a, ""}); err != nil {
		t.Fatalf("JailPreflight: %v", err)
	}
	paths := launcher.requestPaths()
	if !slices.Contains(paths, "PUT /boot-source") {
		t.Fatalf("requests %q never PUT the boot source", paths)
	}
	drives := 0
	for _, p := range paths {
		if p == "PUT /drives/rootfs" {
			drives++
		}
		if strings.Contains(p, "/actions") || strings.Contains(p, "/snapshot") {
			t.Fatalf("preflight booted or snapshotted a guest: %q", paths)
		}
	}
	if drives != 3 {
		t.Fatalf("drive PUTs = %d, want the probe plus two distinct rootfs: %q", drives, paths)
	}
	if launcher.killed != 1 {
		t.Fatalf("preflight VMM kills = %d, want 1", launcher.killed)
	}
	staged := launcher.staged
	for _, want := range []string{"/kernel", a, b} {
		if !slices.Contains(staged, want) {
			t.Fatalf("staged inputs %q lack %q", staged, want)
		}
	}
	probes := 0
	for _, p := range staged {
		if filepath.Base(p) == "probe.img" {
			probes++
		}
	}
	if probes != 1 {
		t.Fatalf("staged inputs %q hold %d probe drives, want 1", staged, probes)
	}
	if _, err := os.Stat(filepath.Join(d.warmthRoot(), jailPreflightDirName)); !os.IsNotExist(err) {
		t.Fatalf("preflight scratch survived: %v", err)
	}
	specs := launcher.specs()
	if len(specs) != 1 || specs[0].Workload != jailPreflightWorkload || specs[0].DirectExec {
		t.Fatalf("launch specs = %#v, want one jailed preflight launch", specs)
	}
}

func TestJailPreflightFailsWhenTheJailCannotOpenADrive(t *testing.T) {
	launcher := &jailingLauncher{fakeLauncher: &fakeLauncher{failPath: "/drives/rootfs"}}
	d, _ := preflightDriver(t, launcher)
	err := d.JailPreflight(context.Background(), nil)
	if err == nil || !strings.Contains(err.Error(), "probe.img") {
		t.Fatalf("JailPreflight error = %v, want the unreadable probe named", err)
	}
	if launcher.killed != 1 {
		t.Fatalf("preflight VMM kills = %d, want 1 after a failure", launcher.killed)
	}
}

func TestJailPreflightRefusesAnUnjailedLaunch(t *testing.T) {
	launcher := &jailingLauncher{fakeLauncher: &fakeLauncher{}, unjail: true}
	d, _ := preflightDriver(t, launcher)
	err := d.JailPreflight(context.Background(), nil)
	if err == nil || !strings.Contains(err.Error(), "without a jail") {
		t.Fatalf("JailPreflight error = %v, want an unjailed launch refused", err)
	}
	if launcher.killed != 1 {
		t.Fatalf("unjailed preflight VMM kills = %d, want 1", launcher.killed)
	}
	if paths := launcher.requestPaths(); len(paths) != 0 {
		t.Fatalf("an unjailed VMM was configured: %q", paths)
	}
}

func TestJailPreflightReportsLaunchFailure(t *testing.T) {
	refused := errors.New("refused")
	launcher := &jailingLauncher{fakeLauncher: &fakeLauncher{}, failErr: refused}
	d, _ := preflightDriver(t, launcher)
	if err := d.JailPreflight(context.Background(), nil); !errors.Is(err, refused) {
		t.Fatalf("JailPreflight error = %v, want the launch failure", err)
	}
	unset := New(Config{SnapshotRoot: shortTempDir(t)}, launcher, nil)
	if err := unset.JailPreflight(context.Background(), nil); err == nil {
		t.Fatal("JailPreflight ran without a kernel image path")
	}
}

func TestJailPreflightProbeMirrorsFreshRootfsMode(t *testing.T) {
	probe := filepath.Join(t.TempDir(), "probe.img")
	if err := writeJailProbe(probe); err != nil {
		t.Fatal(err)
	}
	info, err := os.Stat(probe)
	if err != nil {
		t.Fatal(err)
	}
	if info.Mode().Perm() != 0o600 || info.Size() != jailPreflightProbeBytes {
		t.Fatalf("probe mode=%o size=%d, want 0600 and %d", info.Mode().Perm(), info.Size(), jailPreflightProbeBytes)
	}
}
