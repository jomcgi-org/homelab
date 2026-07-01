//go:build linux

package main

import (
	"log/slog"

	"golang.org/x/sys/unix"
)

// mountTmpfsTmp mounts a tmpfs over /tmp so the guest's mutable scratch state
// lives in RAM rather than on the rootfs drive. This keeps the rootfs read-only
// and shareable across microVMs (the disposable-VM model), and gives goose a
// fast, writable /tmp regardless of how the rootfs is provisioned. Best-effort:
// a failure is logged rather than fatal, since tmpfs mounts effectively never
// fail in practice and a missing tmpfs only degrades to writing the rootfs.
func mountTmpfsTmp(logger *slog.Logger) {
	if err := unix.Mount("tmpfs", "/tmp", "tmpfs", 0, "size=256m,mode=1777"); err != nil {
		logger.Warn("tmpfs mount on /tmp failed", "err", err)
		return
	}
	logger.Info("mounted tmpfs on /tmp")
}
