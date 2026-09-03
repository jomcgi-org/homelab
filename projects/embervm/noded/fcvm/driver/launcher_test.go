package driver

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"github.com/jomcgi/homelab/projects/embervm/noded/fcvm/fcclient"
	"github.com/jomcgi/homelab/projects/embervm/noded/vsockproto"
)

type driveRecordingAPI struct {
	fcAPI
	drive fcclient.Drive
}

func (a *driveRecordingAPI) PutDrive(_ context.Context, drive fcclient.Drive) error {
	a.drive = drive
	return nil
}

func TestPrepareJailStagesOnlyVMResources(t *testing.T) {
	bundle := t.TempDir()
	socket := filepath.Join(bundle, "api.sock")
	jail, err := prepareJail(bundle, "/opt/fc/firecracker", "vm-1", os.Getuid(), os.Getgid(), socket)
	if err != nil {
		t.Fatalf("prepareJail: %v", err)
	}
	wantRoot := filepath.Join(bundle, "jailer", "firecracker", "vm-1", "root")
	if jail.RootDir != wantRoot {
		t.Fatalf("root = %q, want %q", jail.RootDir, wantRoot)
	}
	target, err := os.Readlink(socket)
	if err != nil {
		t.Fatalf("API socket alias: %v", err)
	}
	if want := filepath.Join(wantRoot, "api.sock"); target != want {
		t.Fatalf("API socket alias target = %q, want %q", target, want)
	}

	rootfs := filepath.Join(bundle, "rootfs.ext4")
	if err := os.WriteFile(rootfs, []byte("rootfs"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := jail.stageInput("rootfs", rootfs, rootfs, false); err != nil {
		t.Fatalf("stage rootfs: %v", err)
	}
	stagedRootfs := filepath.Join(wantRoot, strings.TrimPrefix(rootfs, string(filepath.Separator)))
	hostInfo, err := os.Stat(rootfs)
	if err != nil {
		t.Fatal(err)
	}
	stagedInfo, err := os.Stat(stagedRootfs)
	if err != nil {
		t.Fatal(err)
	}
	if !os.SameFile(hostInfo, stagedInfo) {
		t.Fatal("staged rootfs is not a hard link to the host backing file")
	}

	serial := filepath.Join(bundle, "serial.log")
	if _, err := jail.stageOutput(serial, "/serial.log"); err != nil {
		t.Fatalf("stage serial: %v", err)
	}
	for _, path := range []string{
		stagedRootfs,
		filepath.Join(wantRoot, "serial.log"),
	} {
		if _, err := os.Stat(path); err != nil {
			t.Errorf("expected staged path %q: %v", path, err)
		}
	}
	entries, err := os.ReadDir(filepath.Dir(stagedRootfs))
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 1 || entries[0].Name() != "rootfs.ext4" {
		t.Fatalf("resource staging set = %v, want only rootfs.ext4", entries)
	}
	if got := len(jail.Resources()); got != 1 {
		t.Fatalf("resource count = %d, want 1", got)
	}
}

func TestClientForJailedProcessStagesDrive(t *testing.T) {
	bundle := t.TempDir()
	jail, err := prepareJail(bundle, "/opt/fc/firecracker", "vm-client", os.Getuid(), os.Getgid(), filepath.Join(bundle, "api.sock"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = jail.Cleanup() })
	rootfs := filepath.Join(bundle, "rootfs")
	if err := os.WriteFile(rootfs, []byte("rootfs"), 0o600); err != nil {
		t.Fatal(err)
	}
	recorder := &driveRecordingAPI{}
	d := New(Config{}, &fakeLauncher{}, func(string) fcAPI { return recorder })
	process := &fakeProcess{jail: jail}
	client := d.clientForProcess(filepath.Join(bundle, "api.sock"), process)
	if err := client.PutDrive(context.Background(), fcclient.Drive{
		DriveID:    "rootfs",
		PathOnHost: rootfs,
		IsReadOnly: true,
	}); err != nil {
		t.Fatalf("PutDrive: %v", err)
	}
	if recorder.drive.PathOnHost != rootfs {
		t.Fatalf("jailed drive path = %q, want embedded path %q", recorder.drive.PathOnHost, rootfs)
	}
	staged, err := jail.hostPath(rootfs)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(staged); err != nil {
		t.Fatalf("staged drive missing: %v", err)
	}
}

func TestBindJailedVsockStagesGuestEgressTarget(t *testing.T) {
	root := t.TempDir()
	d := New(Config{SnapshotRoot: root}, &fakeLauncher{}, nil)
	threadID := "egress-thread"
	if err := os.MkdirAll(d.threadDir(threadID), 0o750); err != nil {
		t.Fatal(err)
	}
	jail, err := prepareJail(d.threadDir(threadID), "/opt/fc/firecracker", "vm-egress", os.Getuid(), os.Getgid(), filepath.Join(d.threadDir(threadID), "api.sock"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = jail.Cleanup() })
	if err := d.bindJailedVsock(&fakeProcess{jail: jail}, threadID); err != nil {
		t.Fatalf("bindJailedVsock: %v", err)
	}
	hostTarget := d.VsockUDSPath(threadID) + "_" + fmt.Sprint(vsockproto.EgressPort)
	got, err := os.Readlink(hostTarget)
	if err != nil {
		t.Fatalf("guest egress alias: %v", err)
	}
	want, err := jail.hostPath(d.bootVsockPath(threadID) + "_" + fmt.Sprint(vsockproto.EgressPort))
	if err != nil {
		t.Fatal(err)
	}
	if got != want {
		t.Fatalf("guest egress alias target = %q, want %q", got, want)
	}
}

func TestJailPrivateCopyDoesNotMutateSharedPlaceholder(t *testing.T) {
	bundle := t.TempDir()
	shared := filepath.Join(bundle, "placeholder-volume.img")
	if err := os.WriteFile(shared, []byte("placeholder"), 0o600); err != nil {
		t.Fatal(err)
	}
	before, err := os.Stat(shared)
	if err != nil {
		t.Fatal(err)
	}
	jail, err := prepareJail(bundle, "/opt/fc/firecracker", "vm-copy", os.Getuid(), os.Getgid(), filepath.Join(bundle, "api.sock"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = jail.Cleanup() })
	resource := JailResource{Role: "volume", HostPath: shared, JailPath: shared, Writable: true, PrivateCopy: true}
	if _, err := jail.stageResource(resource); err != nil {
		t.Fatalf("stage private placeholder: %v", err)
	}
	// PutDrive stages the same role again. It must retain the launch-time private
	// copy instead of replacing it with a hard link to the shared inode.
	if _, err := jail.stageInput("volume", shared, shared, true); err != nil {
		t.Fatalf("restage volume: %v", err)
	}
	after, err := os.Stat(shared)
	if err != nil {
		t.Fatal(err)
	}
	stagedPath, err := jail.hostPath(shared)
	if err != nil {
		t.Fatal(err)
	}
	staged, err := os.Stat(stagedPath)
	if err != nil {
		t.Fatal(err)
	}
	if os.SameFile(after, staged) {
		t.Fatal("private placeholder staging reused the shared inode")
	}
	if before.Mode().Perm() != after.Mode().Perm() {
		t.Fatalf("shared placeholder mode changed from %o to %o", before.Mode().Perm(), after.Mode().Perm())
	}
}

func TestExecProcessCleanupRemovesJailTreeAndReleasesUID(t *testing.T) {
	bundle := t.TempDir()
	jail, err := prepareJail(bundle, "/opt/fc/firecracker", "vm-clean", os.Getuid(), os.Getgid(), filepath.Join(bundle, "api.sock"))
	if err != nil {
		t.Fatal(err)
	}
	released := 0
	proc := &execProcess{jail: jail, releaseID: func() { released++ }}
	proc.cleanup()
	proc.cleanup()
	if _, err := os.Stat(jail.Dir); !os.IsNotExist(err) {
		t.Fatalf("jail tree still exists after cleanup: %v", err)
	}
	if released != 1 {
		t.Fatalf("uid release count = %d, want 1", released)
	}
}

func TestAllocateUIDReturnsErrorWhenPoolExhausted(t *testing.T) {
	productionLaunchState.Lock()
	savedNext := productionLaunchState.nextUID
	savedUsed := productionLaunchState.usedUIDs
	productionLaunchState.nextUID = 0
	productionLaunchState.usedUIDs = make(map[int]bool, vmUIDCount)
	for uid := vmUIDBase; uid < vmUIDBase+vmUIDCount; uid++ {
		productionLaunchState.usedUIDs[uid] = true
	}
	productionLaunchState.Unlock()
	t.Cleanup(func() {
		productionLaunchState.Lock()
		productionLaunchState.nextUID = savedNext
		productionLaunchState.usedUIDs = savedUsed
		productionLaunchState.Unlock()
	})

	if _, _, err := (&ExecLauncher{}).allocateUID(); err == nil {
		t.Fatal("allocateUID succeeded with every uid in use")
	}
}

func TestSnapshotDriveResourcesUsesPatchedVolume(t *testing.T) {
	got := snapshotDriveResources([]JailResource{
		{Role: "rootfs", HostPath: "/old/rootfs", JailPath: "/old/rootfs"},
		{Role: "volume", HostPath: "/old/volume", JailPath: "/old/volume", Writable: true},
		{Role: "snapshot-state", HostPath: "/bundle/snapfile", JailPath: "/snapshot/snapfile"},
		{Role: "volume-patch", HostPath: "/new/volume", JailPath: "/new/volume", Writable: true},
	})
	want := []JailResource{
		{Role: "rootfs", HostPath: "/old/rootfs", JailPath: "/old/rootfs"},
		{Role: "volume", HostPath: "/new/volume", JailPath: "/new/volume", Writable: true},
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("snapshot resources = %#v, want %#v", got, want)
	}
}
