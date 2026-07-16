//go:build !linux

package main

import "log/slog"

// mountVolumeDevice is a no-op stub off Linux (the guest is always Linux); it
// only keeps the package building for host (e.g. darwin) builds.
func mountVolumeDevice(logger *slog.Logger, dev, mountPath string) error {
	logger.Info("mountVolumeDevice is a no-op off Linux", "device", dev, "mount", mountPath)
	return nil
}
