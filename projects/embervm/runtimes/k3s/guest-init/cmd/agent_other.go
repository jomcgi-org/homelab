//go:build !linux

package main

import (
	"context"
	"log/slog"
)

// startGuestAgent is a no-op off Linux: AF_VSOCK is Linux-only and the guest
// runs inside a Linux microVM. This stub keeps the package building under the
// host toolchain for unit tests.
func startGuestAgent(_ context.Context, logger *slog.Logger) {
	logger.Info("guest control agent: skipped (non-linux host build)")
}
