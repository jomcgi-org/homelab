//go:build !linux

package main

import (
	"context"
	"log/slog"
)

// startVsockServer is a no-op off Linux: AF_VSOCK is Linux-only. The bazel-query
// guest runs inside a Linux microVM; this stub keeps the package building under
// the host (e.g. darwin) toolchain for unit tests and local builds. It returns an
// already-closed channel, so run's waitForShutdown reads the zero-value nil from
// it immediately and PID 1 returns (exits). That is fine here: this path is only
// reached on a non-Linux host build/test, never in the real guest.
func startVsockServer(_ context.Context, logger *slog.Logger, _ func() bool) <-chan error {
	logger.Warn("ember-bazel-init: AF_VSOCK unsupported on this GOOS; no server (host build)")
	ch := make(chan error, 1)
	close(ch)
	return ch
}
