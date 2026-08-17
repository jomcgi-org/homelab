//go:build !linux

package main

import (
	"context"
	"fmt"
	"log/slog"
	"net"
	"runtime"
)

func dialVsock(_ context.Context, _, _ uint32) (net.Conn, error) {
	return nil, fmt.Errorf("ember-shotter-init: AF_VSOCK unsupported on %s", runtime.GOOS)
}

func startVsockServer(_ context.Context, logger *slog.Logger, _ func() bool, _ ProxyConfig) <-chan error {
	logger.Warn("ember-shotter-init: AF_VSOCK unsupported on this GOOS")
	ch := make(chan error, 1)
	close(ch)
	return ch
}
