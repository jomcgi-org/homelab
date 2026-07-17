//go:build !linux

package main

import (
	"context"
	"log/slog"
)

// startVsockReadyServer off Linux degrades to a closed channel: the vsock
// readiness server is Linux-only (AF_VSOCK) and only runs inside the microVM.
// The host build still compiles and the base-build path treats the closed
// channel as "no ready server," which is fine off Linux.
func startVsockReadyServer(_ context.Context, logger *slog.Logger, _ func() bool) <-chan error {
	logger.Info("ember-k3s-init: vsock ready server skipped (non-linux host build)")
	ch := make(chan error, 1)
	close(ch)
	return ch
}

// waitForShutdown off Linux blocks on the context only (there is no vsock server
// to error), returning nil on cancellation.
func waitForShutdown(ctx context.Context, _ <-chan error, logger *slog.Logger) error {
	<-ctx.Done()
	logger.Info("ember-k3s-init: shutdown signal")
	return nil
}
