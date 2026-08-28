//go:build linux

package main

import (
	"fmt"
	"log/slog"
	"os"
	"os/exec"

	"golang.org/x/sys/unix"
)

// mountTmpfsTmp mounts a tmpfs over /tmp so transient guest state (any tooling
// scratch) is writable on the read-only rootfs. The server's data root is on the
// VOLUME, not here. Best-effort: a failure is logged, not fatal. Mirrors the
// postgres runtime guest-init's mountTmpfsTmp.
func mountTmpfsTmp(logger *slog.Logger) {
	if err := unix.Mount("tmpfs", "/tmp", "tmpfs", 0, "size=64m,mode=1777"); err != nil {
		logger.Warn("tmpfs mount on /tmp failed", "err", err)
		return
	}
	logger.Info("mounted tmpfs on /tmp")
}

// mountProc mounts a procfs on /proc so the boot-arg readers (statefulVolumeFrom
// Cmdline / setMmdsEnv) can read /proc/cmdline. A raw Firecracker boot leaves
// /proc unmounted. Best-effort.
func mountProc(logger *slog.Logger) {
	if err := unix.Mount("proc", "/proc", "proc", 0, ""); err != nil {
		logger.Warn("proc mount on /proc failed", "err", err)
		return
	}
	logger.Info("mounted proc on /proc")
}

// mountStatefulVolume formats dev with ext4 if it has no existing filesystem
// signature (a freshly created sparse volume on first boot), creates mountPath,
// and mounts it. This is the one place in the whole system that formats-if-blank
// and mounts the stateful volume (the host never mounts it, decision 9). A
// mkfs/mount failure is FATAL: an Iggy guest that cannot reach its volume must
// fail the boot loudly rather than run against a missing data directory.
// Mirrors the postgres runtime guest-init's mountStatefulVolume.
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
	if err := os.MkdirAll(mountPath, 0o755); err != nil {
		return fmt.Errorf("mkdir volume mount path %s: %w", mountPath, err)
	}
	if err := unix.Mount(dev, mountPath, "ext4", 0, ""); err != nil {
		return fmt.Errorf("mount %s at %s: %w", dev, mountPath, err)
	}
	logger.Info("stateful volume: mounted", "device", dev, "mount", mountPath)
	return nil
}

// deviceIsBlank reports whether dev has NO existing filesystem signature (blkid
// finds nothing), meaning this is a freshly created sparse volume that needs
// formatting on first boot. blkid exits 2 with empty output for an unrecognised
// signature (the blank case); any OTHER error is propagated so a real problem is
// not misread as "blank, go ahead and mkfs" (which would destroy data blkid
// merely failed to identify). Mirrors the postgres runtime guest-init.
func deviceIsBlank(dev string) (bool, error) {
	out, err := exec.Command("blkid", "-o", "value", "-s", "TYPE", dev).CombinedOutput()
	if err == nil {
		return len(trimTrailingNewline(out)) == 0, nil
	}
	if exitErr, ok := err.(*exec.ExitError); ok {
		if exitErr.ExitCode() == 2 {
			return true, nil
		}
	}
	return false, fmt.Errorf("blkid %s: %w: %s", dev, err, string(out))
}

// trimTrailingNewline strips a single trailing newline blkid's output carries.
func trimTrailingNewline(b []byte) []byte {
	if n := len(b); n > 0 && b[n-1] == '\n' {
		return b[:n-1]
	}
	return b
}
