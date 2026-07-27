//go:build !linux

package main

import "log/slog"

func mountWorkspaceVolume(logger *slog.Logger) error {
	logger.Info("workspace volume is a no-op off Linux")
	return nil
}
