package driver

import (
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

func TestMemoryLimitBytesIncludesVMMMargin(t *testing.T) {
	const guestMib = 512
	want := int64(guestMib+VMMMemoryOverheadMib) * 1024 * 1024
	if got := memoryLimitBytes(guestMib); got != want {
		t.Fatalf("memoryLimitBytes(%d) = %d, want %d", guestMib, got, want)
	}
}

func TestLaunchArgvDirectAndJailer(t *testing.T) {
	t.Run("direct", func(t *testing.T) {
		got := buildDirectArgs("vm-1", "/bundle/api.sock")
		want := []string{"--api-sock", "/bundle/api.sock", "--id", "vm-1"}
		if !reflect.DeepEqual(got, want) {
			t.Fatalf("direct argv = %#v, want %#v", got, want)
		}
	})

	t.Run("jailer", func(t *testing.T) {
		got := buildJailerArgs("/opt/fc/firecracker", "vm-1", 20000, 20000, "/bundle/jailer", "embervm-vms/vm-1", "/api.sock")
		want := []string{
			"--id", "vm-1",
			"--exec-file", "/opt/fc/firecracker",
			"--uid", "20000",
			"--gid", "20000",
			"--cgroup-version", "2",
			"--parent-cgroup", "embervm-vms/vm-1",
			"--chroot-base-dir", "/bundle/jailer",
			"--",
			"--api-sock", "/api.sock",
		}
		if !reflect.DeepEqual(got, want) {
			t.Fatalf("jailer argv = %#v, want %#v", got, want)
		}
	})
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
