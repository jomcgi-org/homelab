//go:build !linux

package main

import "log/slog"

// bringUpLoopback / setHostname are no-ops off Linux (the interface ioctl and
// sethostname syscall are Linux-only). The guest runs inside a Linux microVM;
// these stubs keep the package building under the host toolchain for unit tests
// and local builds.
func bringUpLoopback(_ *slog.Logger) {}
func setHostname(_ *slog.Logger)     {}
