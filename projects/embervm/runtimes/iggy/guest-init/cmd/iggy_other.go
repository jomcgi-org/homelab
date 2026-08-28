//go:build !linux

package main

import (
	"context"
	"log/slog"
	"sync/atomic"
)

// launchIggy is a no-op stub off Linux (the guest is always Linux); it only keeps
// the package building for host (e.g. darwin) unit tests and local builds. It
// flips ready so a host smoke run does not hang. The platform-independent halves
// (stateBootstrapped, requireRootPassword, iggyChildEnv, the boot-arg readers)
// live in iggy.go and cmdline.go and ARE exercised by the host-build tests.
func launchIggy(_ context.Context, logger *slog.Logger, mountPath string, ready *atomic.Bool) error {
	logger.Info("launchIggy is a no-op off Linux", "mount", mountPath)
	ready.Store(true)
	return nil
}

// mountStatefulVolume is a no-op stub off Linux (see the real mount in
// mount_linux.go); kept here so the !linux build has the symbol.
func mountStatefulVolume(logger *slog.Logger, dev, mountPath string) error {
	logger.Info("mountStatefulVolume is a no-op off Linux", "device", dev, "mount", mountPath)
	return nil
}
