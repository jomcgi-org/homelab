//go:build linux

package main

import (
	"fmt"
	"io"
	"log/slog"
	"os"
	"os/exec"
	"path/filepath"

	"golang.org/x/sys/unix"
)

// mountGuestFilesystems mounts the writable and pseudo filesystems k3s needs on
// the read-only, snapshot-shared rootfs. Every mount is best-effort (logged, not
// fatal): a genuinely missing mount surfaces as a k3s startup failure downstream,
// which is the honest place for that failure, and a base build (no k3s) still
// boots. Mirrors the postgres/python runtime guest-init mount helpers.
//
// The read-only rootfs bakes the airgap tarball at
// /var/lib/rancher/k3s/agent/images (a read-only baked file k3s imports); k3s's
// WRITABLE state (server db, kubelet, containerd) lives under other subtrees, so
// a tmpfs is layered over /var/lib/rancher (large: containerd image unpack +
// etcd/sqlite) with the baked images bind-restored on top. To keep this init
// simple and the airgap import working, /var/lib/rancher is NOT tmpfs-covered;
// instead the writable children k3s creates are already writable because the
// whole tree is placed writable in the image (the apko path declaration), and
// only the volatile pseudo-fs and /run/tmp/log get tmpfs here.
func mountGuestFilesystems(logger *slog.Logger) {
	// proc and sysfs: required by k3s (cgroup/kubelet) and by the boot-arg readers.
	mount(logger, "proc", "/proc", "proc", 0, "")
	mount(logger, "sysfs", "/sys", "sysfs", 0, "")

	// tmpfs over the volatile writable dirs. /run holds the containerd socket, CNI
	// state, and the token-auth CSV (/run/ember); /var/log holds k3s/containerd
	// logs; /tmp is generic scratch. Sizes are generous: containerd + k3s are not
	// memory-frugal, and this is RAM-backed so it counts against the memMib floor
	// the spike measures (fc-base sizing coupling).
	mount(logger, "tmpfs", "/run", "tmpfs", 0, "size=512m,mode=0755")
	mount(logger, "tmpfs", "/var/log", "tmpfs", 0, "size=128m,mode=0755")
	mount(logger, "tmpfs", "/tmp", "tmpfs", 0, "size=256m,mode=1777")

	// cgroup2 for kubelet/containerd. The kata guest kernel is cgroup2-shaped;
	// mounting it here rather than relying on k3s to do it keeps the ordering
	// explicit. Best-effort: if the kernel provides a pre-mounted cgroup2 (some
	// initramfs do) this is a harmless no-op error.
	_ = os.MkdirAll("/sys/fs/cgroup", 0o755)
	mount(logger, "cgroup2", "/sys/fs/cgroup", "cgroup2", 0, "")

	// The token-auth CSV directory on the /run tmpfs (server writes into it).
	if err := os.MkdirAll("/run/ember", 0o700); err != nil {
		logger.Warn("mkdir /run/ember failed", "err", err)
	}
}

// mountCompositeDataDir gives a WARMTH-ONLY composite member (R5, no volume) its
// writable k3s data dir. The stateful lane mounts a block-device volume at
// k3sDataDir; a composite member has none, and the rootfs is read-only, so
// k3s's self-extract + datastore + containerd unpack have nowhere to write (k3s
// dies "extracting data: no such file or directory"). This mounts a tmpfs there
// instead and stages the baked airgap tarball into it, exactly as
// mountStatefulVolume does after its block mount. The k3s datastore + unpacked
// images therefore live in RAM (counting against the member's mem_mib) and are
// lost on fresh/destroy, which IS the R5 warmth-only contract. No explicit size
// is set, so the tmpfs defaults to 50% of guest RAM and auto-scales with the
// member's mem_mib (server larger than agent). A tmpfs mount failure is FATAL: a
// member that cannot get a writable data dir must fail the boot loudly rather
// than crash obscurely inside k3s.
func mountCompositeDataDir(logger *slog.Logger) error {
	if err := unix.Mount("tmpfs", k3sDataDir, "tmpfs", 0, "mode=0700"); err != nil {
		return fmt.Errorf("mount tmpfs at %s: %w", k3sDataDir, err)
	}
	logger.Info("composite member: k3s data dir on tmpfs (warmth-only, no volume)", "mount", k3sDataDir)

	// Stage the baked airgap tarball from /opt/k3s-airgap (outside the tree, so the
	// tmpfs did not hide it) into <k3sDataDir>/agent/images so k3s auto-imports it.
	// Non-fatal, same posture as the stateful lane: a k3s that cannot find the
	// airgap set fails its own readiness loudly downstream (the honest place).
	if err := stageAirgapImages(logger, k3sDataDir); err != nil {
		logger.Warn("composite member: staging airgap images failed", "err", err)
	}
	return nil
}

// mount is a thin logged wrapper over unix.Mount so each call site reads as one
// line and a failure is logged with its target rather than crashing PID 1 (which
// would panic the guest kernel).
func mount(logger *slog.Logger, source, target, fstype string, flags uintptr, data string) {
	if err := unix.Mount(source, target, fstype, flags, data); err != nil {
		logger.Warn("mount failed", "target", target, "fstype", fstype, "err", err)
		return
	}
	logger.Info("mounted", "target", target, "fstype", fstype)
}

// mountStatefulVolume formats dev with ext4 if it has no existing filesystem
// signature (a freshly created sparse volume on first boot), mounts it at
// mountPath (k3s's data dir), and stages the baked airgap tarball into the
// mounted tree so k3s auto-imports it. This is the one place that formats-if-
// blank and mounts the volume (the host never mounts it). A mkfs/mount failure
// is FATAL: a k3s guest that cannot reach its data dir must fail the boot loudly
// rather than run against a missing/unmounted directory. Mirrors the postgres
// runtime guest-init's mountVolumeDevice, plus the airgap-staging copy.
func mountStatefulVolume(logger *slog.Logger, dev, mountPath string) error {
	blank, err := deviceIsBlank(dev)
	if err != nil {
		return fmt.Errorf("probe volume device %s: %w", dev, err)
	}
	if blank {
		logger.Info("stateful volume: no filesystem signature, formatting ext4", "device", dev)
		if out, err := exec.Command("mkfs.ext4", "-q", dev).CombinedOutput(); err != nil {
			return fmt.Errorf("mkfs.ext4 %s: %w: %s", dev, err, string(out))
		}
	}
	if err := os.MkdirAll(mountPath, 0o700); err != nil {
		return fmt.Errorf("mkdir volume mount path %s: %w", mountPath, err)
	}
	if err := unix.Mount(dev, mountPath, "ext4", 0, ""); err != nil {
		return fmt.Errorf("mount %s at %s: %w", dev, mountPath, err)
	}
	logger.Info("stateful volume: mounted", "device", dev, "mount", mountPath)

	// Stage the baked airgap tarball into the (now-mounted) data dir so k3s
	// auto-imports it from <mount>/agent/images at startup. The tarball is baked
	// at /opt/k3s-airgap (outside the mount, so the volume never hides it); copy
	// it in only when absent (a relight/second cold boot already has it).
	if err := stageAirgapImages(logger, mountPath); err != nil {
		// Non-fatal: a k3s that cannot find the airgap set will try to pull and
		// fail its own readiness loudly downstream (the honest place), but the
		// mount itself succeeded so the boot proceeds and the failure is visible.
		logger.Warn("stateful volume: staging airgap images failed", "err", err)
	}
	return nil
}

// stageAirgapImages copies the baked airgap tarball from the staging dir
// (/opt/k3s-airgap, in the read-only rootfs) into <mount>/agent/images, where
// k3s auto-imports it. It is idempotent: an already-present tarball (a later
// cold boot against a populated volume) is left untouched. Absent staging dir
// (an image built without the airgap layer) is a no-op.
func stageAirgapImages(logger *slog.Logger, mountPath string) error {
	const stageDir = "/opt/k3s-airgap"
	entries, err := os.ReadDir(stageDir)
	if err != nil {
		if os.IsNotExist(err) {
			return nil
		}
		return fmt.Errorf("read airgap staging dir: %w", err)
	}
	destDir := filepath.Join(mountPath, "agent", "images")
	if err := os.MkdirAll(destDir, 0o700); err != nil {
		return fmt.Errorf("mkdir %s: %w", destDir, err)
	}
	for _, e := range entries {
		if e.IsDir() {
			continue
		}
		dest := filepath.Join(destDir, e.Name())
		if _, err := os.Stat(dest); err == nil {
			continue // already staged (populated volume)
		}
		if err := copyFile(filepath.Join(stageDir, e.Name()), dest); err != nil {
			return fmt.Errorf("copy airgap %s: %w", e.Name(), err)
		}
		logger.Info("stateful volume: staged airgap tarball", "name", e.Name(), "dest", dest)
	}
	return nil
}

// copyFile copies src to dst (0644), creating dst. Used only for the airgap
// tarball staging; a plain byte copy is all that is needed.
func copyFile(src, dst string) error {
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close() //nolint:errcheck // read-only source
	out, err := os.OpenFile(dst, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, 0o644)
	if err != nil {
		return err
	}
	if _, err := io.Copy(out, in); err != nil {
		_ = out.Close()
		return err
	}
	return out.Close()
}

// deviceIsBlank reports whether dev has NO existing filesystem signature (blkid
// finds nothing), meaning a freshly created sparse volume that needs formatting.
// blkid exits 2 with empty output for an unrecognised signature (the blank
// case); any OTHER error is propagated so a real problem is not misread as
// "blank, go ahead and mkfs". Mirrors the postgres runtime guest-init.
func deviceIsBlank(dev string) (bool, error) {
	out, err := exec.Command("blkid", "-o", "value", "-s", "TYPE", dev).CombinedOutput()
	if err == nil {
		return len(trimTrailingNewline(out)) == 0, nil
	}
	if exitErr, ok := err.(*exec.ExitError); ok && exitErr.ExitCode() == 2 {
		return true, nil
	}
	return false, fmt.Errorf("blkid %s: %w: %s", dev, err, string(out))
}

// trimTrailingNewline strips a single trailing newline from blkid's output.
func trimTrailingNewline(b []byte) []byte {
	if n := len(b); n > 0 && b[n-1] == '\n' {
		return b[:n-1]
	}
	return b
}
