package driver

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"time"

	"github.com/jomcgi/homelab/projects/embervm/noded/fcvm/fcclient"
)

const (
	// jailPreflightWorkload labels the preflight VMM in logs and exit evidence.
	jailPreflightWorkload = "jail-preflight"
	// jailPreflightDirName is the preflight's scratch under the warmth root. It
	// is this daemon's alone, so each preflight starts by removing any tree an
	// interrupted earlier run left there.
	jailPreflightDirName = "jail-preflight"
	// jailPreflightMemMib sizes the preflight VM's cgroup leaf. No guest boots,
	// so it only has to hold an idle Firecracker process.
	jailPreflightMemMib = 128
	// jailPreflightProbeBytes is the sparse probe drive's size.
	jailPreflightProbeBytes = 1 << 20
	// jailPreflightTimeout bounds the whole preflight, launch included.
	jailPreflightTimeout = 60 * time.Second
)

// JailPreflight proves, before the daemon registers, that a jailed Firecracker
// can open the inputs a guest will need. It drives the real launch path without
// booting a guest: it launches the VMM through the jailer, requires the process
// to be jailed, then through the jailed client PUTs the boot source and a
// read-only drive for a 0600 probe file (the provenance of a freshly baked
// rootfs) and for each distinct READY base rootfs. Firecracker opens each file
// as the jail uid at PUT time, so an input the jail cannot read fails here, at
// startup, instead of on every later restore. Kill then releases the jail, the
// cgroup and the uid.
func (d *Driver) JailPreflight(ctx context.Context, rootfsPaths []string) (err error) {
	ctx, cancel := context.WithTimeout(ctx, jailPreflightTimeout)
	defer cancel()
	if d.cfg.KernelImagePath == "" {
		return errors.New("driver: jail preflight: kernel image path is unset")
	}
	root := filepath.Join(d.warmthRoot(), jailPreflightDirName)
	if err := removeJailTree(root); err != nil {
		return fmt.Errorf("driver: jail preflight: clear stale preflight tree: %w", err)
	}
	dir := filepath.Join(root, newID("run"))
	if err := os.MkdirAll(dir, 0o750); err != nil {
		return fmt.Errorf("driver: jail preflight: %w", err)
	}
	defer func() {
		if rmErr := removeJailTree(root); rmErr != nil {
			err = errors.Join(err, fmt.Errorf("driver: jail preflight: remove preflight tree: %w", rmErr))
		}
	}()
	probe := filepath.Join(dir, "probe.img")
	if err := writeJailProbe(probe); err != nil {
		return fmt.Errorf("driver: jail preflight: create probe drive: %w", err)
	}
	sock := filepath.Join(dir, "api.sock")
	proc, err := d.launcher.Launch(ctx, LaunchSpec{
		VMID: newID("vm"), SocketPath: sock, Workload: jailPreflightWorkload,
		Phase: guestPhaseInit, MemMib: jailPreflightMemMib,
	})
	if err != nil {
		return fmt.Errorf("driver: jail preflight: launch jailed firecracker: %w", err)
	}
	defer func() {
		if killErr := proc.Kill(); killErr != nil {
			err = errors.Join(err, fmt.Errorf("driver: jail preflight: kill firecracker: %w", killErr))
		}
	}()
	if provider, ok := proc.(jailProvider); !ok || provider.Jail() == nil {
		return errors.New("driver: jail preflight: firecracker launched without a jail")
	}
	client := d.clientForProcess(sock, proc)
	if err := client.PutBootSource(ctx, fcclient.BootSource{KernelImagePath: d.cfg.KernelImagePath, BootArgs: d.cfg.KernelBootArgs}); err != nil {
		return fmt.Errorf("driver: jail preflight: kernel %q is not usable inside the jail: %w", d.cfg.KernelImagePath, err)
	}
	for _, path := range jailPreflightDrives(probe, rootfsPaths) {
		// The rootfs drive id gives every probe the staging a restored rootfs
		// gets. Re-PUTting one id before boot replaces the drive, and Firecracker
		// opens the new backing file each time.
		if err := client.PutDrive(ctx, fcclient.Drive{DriveID: "rootfs", PathOnHost: path, IsRootDevice: true, IsReadOnly: true}); err != nil {
			return fmt.Errorf("driver: jail preflight: drive %q is not readable inside the jail: %w", path, err)
		}
	}
	return nil
}

// jailPreflightDrives is the probe followed by each distinct non-empty rootfs.
func jailPreflightDrives(probe string, rootfsPaths []string) []string {
	drives := []string{probe}
	for _, path := range rootfsPaths {
		if path != "" && !slices.Contains(drives, path) {
			drives = append(drives, path)
		}
	}
	return drives
}

// writeJailProbe creates a sparse 0600 file, mirroring the mode a freshly baked
// rootfs has before the jail stages it.
func writeJailProbe(path string) error {
	f, err := os.OpenFile(path, os.O_CREATE|os.O_EXCL|os.O_RDWR, 0o600)
	if err != nil {
		return err
	}
	if err := f.Truncate(jailPreflightProbeBytes); err != nil {
		_ = f.Close()
		return err
	}
	if err := f.Close(); err != nil {
		return err
	}
	// The umask can only narrow the mode; pin it so the probe never passes on a
	// looser default than a real rootfs has.
	return os.Chmod(path, 0o600)
}
