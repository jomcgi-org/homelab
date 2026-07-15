//go:build !linux

package main

import "log/slog"

// execShim is a no-op stub off Linux (the guest is always Linux); it only keeps
// the package building for host (e.g. darwin) builds. It returns nil so run's
// contract (returns only on failure) is preserved without a host exec.
func execShim(logger *slog.Logger) error {
	logger.Info("execShim is a no-op off Linux")
	return nil
}
