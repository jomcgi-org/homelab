//go:build linux

package main

import (
	"log/slog"
	"os"
	"os/exec"

	"github.com/jomcgi/homelab/projects/firecracker/substrate/guestvolume"
	"golang.org/x/sys/unix"
)

// mountTmpfsTmp mounts a tmpfs over /tmp so transient guest state (the initdb
// pwfile, any tooling scratch) is writable on the read-only rootfs. PGDATA is on
// the VOLUME, not here. Best-effort: a failure is logged, not fatal. Mirrors the
// python runtime guest-init's mountTmpfsTmp.
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

// mountStatefulVolume formats an explicitly selected blank device and mounts it.
// A failure is fatal: Postgres must not run against a missing data directory.
func mountStatefulVolume(logger *slog.Logger, dev, mountPath string) error {
	return mountVolumeWithOps(logger, dev, mountPath, guestvolume.Operations{
		CombinedOutput: func(name string, args ...string) ([]byte, error) {
			return exec.Command(name, args...).CombinedOutput()
		},
		MkdirAll: os.MkdirAll, Mount: unix.Mount,
	})
}

func mountVolumeWithOps(logger *slog.Logger, dev, mountPath string, ops guestvolume.Operations) error {
	return guestvolume.Mount(dev, mountPath, []string{"-q", dev}, ops, guestvolume.Hooks{
		BeforeFormat: func() {
			logger.Info("stateful volume: no filesystem signature, formatting ext4", "device", dev)
		},
		Mounted: func() {
			logger.Info("stateful volume: mounted", "device", dev, "mount", mountPath)
		},
	})
}
