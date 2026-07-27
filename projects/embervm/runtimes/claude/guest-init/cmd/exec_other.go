//go:build !linux

package main

import "log/slog"

func execShim(logger *slog.Logger) error {
	logger.Info("execShim is a no-op off Linux")
	return nil
}
