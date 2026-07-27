//go:build linux

package main

import (
	"log/slog"
	"os"

	"golang.org/x/sys/unix"
)

func execShim(logger *slog.Logger) error {
	logger.Info("exec Claude shim", "cmd", shimCmd)
	if err := unix.Exec(shimCmd[0], shimCmd, os.Environ()); err != nil {
		return err // nosemgrep: no-bare-error-return
	}
	return nil
}
