//go:build !linux

package main

import (
	"context"
	"errors"
	"log/slog"
	"net"
	"net/netip"
)

// setupCapture is a no-op off Linux; the guest is always Linux. This stub keeps
// the package building for host (darwin) tests.
func setupCapture(context.Context, *slog.Logger, *synthResolver) {}

// originalDst is unavailable off Linux (SO_ORIGINAL_DST is a netfilter feature).
func originalDst(*net.TCPConn) (netip.AddrPort, error) {
	return netip.AddrPort{}, errors.New("SO_ORIGINAL_DST unavailable off linux")
}
