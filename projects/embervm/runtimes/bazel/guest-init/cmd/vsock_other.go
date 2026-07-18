//go:build !linux

package main

import (
	"context"
	"log/slog"
)

// startVsockServer is a no-op off Linux: AF_VSOCK is Linux-only. The bazel-query
// guest runs inside a Linux microVM; this stub keeps the package building under
// the host (e.g. darwin) toolchain for unit tests and local builds. It returns a
// closed channel so run's waitForShutdown falls through to the signal path.
func startVsockServer(_ context.Context, logger *slog.Logger, _ func() bool) <-chan error {
	logger.Warn("ember-bazel-init: AF_VSOCK unsupported on this GOOS; no server (host build)")
	ch := make(chan error, 1)
	close(ch)
	return ch
}
