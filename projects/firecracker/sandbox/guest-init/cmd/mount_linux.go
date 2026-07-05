//go:build linux

package main

import (
	"log/slog"

	"golang.org/x/sys/unix"
)

// mountTmpfsTmp mounts a tmpfs over /tmp so every per-invoke workdir
// (handler.Handle's os.MkdirTemp) lives in RAM rather than on the rootfs
// drive. This is what lets the rootfs stay read-only and be shared by every
// microVM restored from one warm-base snapshot: the mutable state is
// captured in the snapshot memfile (RAM), which Firecracker loads
// copy-on-write per restore, so concurrent restored guests never write the
// single shared rootfs file. Best-effort: a failure is logged rather than
// fatal, since tmpfs mounts effectively never fail in practice and a missing
// tmpfs only degrades to a failed write on the read-only rootfs, which the
// first request's MkdirTemp will then surface.
func mountTmpfsTmp(logger *slog.Logger) {
	if err := unix.Mount("tmpfs", "/tmp", "tmpfs", 0, "size=256m,mode=1777"); err != nil {
		logger.Warn("tmpfs mount on /tmp failed", "err", err)
		return
	}
	logger.Info("mounted tmpfs on /tmp")
}
