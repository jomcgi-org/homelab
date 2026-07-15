//go:build linux

package main

import (
	"log/slog"
	"os"

	"golang.org/x/sys/unix"
)

// execShim replaces this process image with the python bootstrap shim, so
// python becomes PID 1. It only returns on failure (a successful exec never
// returns). The shim inherits the tmpfs-mounted /tmp and the default env set
// above.
func execShim(logger *slog.Logger) error {
	logger.Info("exec python bootstrap shim", "cmd", shimCmd)
	if err := unix.Exec(shimCmd[0], shimCmd, os.Environ()); err != nil {
		return err // nosemgrep: no-bare-error-return
	}
	return nil
}
