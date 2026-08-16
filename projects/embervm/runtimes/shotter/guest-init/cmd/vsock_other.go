//go:build !linux

package main

import (
	"context"
	"log/slog"
)

func startVsockServer(_ context.Context, logger *slog.Logger, _ func() bool) <-chan error {
	logger.Warn("ember-shotter-init: AF_VSOCK unsupported on this GOOS")
	ch := make(chan error, 1)
	close(ch)
	return ch
}
