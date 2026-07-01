//go:build !linux

package main

import "log/slog"

// bringUpLoopback is a no-op off Linux; the guest is always Linux, this stub
// only keeps the package building for host tests.
func bringUpLoopback(*slog.Logger) {}
