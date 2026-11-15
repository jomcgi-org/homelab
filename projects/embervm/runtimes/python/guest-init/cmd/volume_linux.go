//go:build linux

package main

import (
	"log/slog"
	"os"
	"os/exec"

	"github.com/jomcgi/homelab/projects/firecracker/substrate/guestvolume"
	"golang.org/x/sys/unix"
)

// mountVolumeDevice formats an explicitly selected blank device and mounts it.
// Probe or mount errors retain mountStatefulVolume's fatal boot contract.
func mountVolumeDevice(logger *slog.Logger, dev, mountPath string) error {
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
