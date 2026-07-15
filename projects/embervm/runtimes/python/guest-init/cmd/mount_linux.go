//go:build linux

package main

import (
	"log/slog"

	"golang.org/x/sys/unix"
)

// mountTmpfsTmp mounts a tmpfs over /tmp so the python shim's unpack dir
// (/tmp/ember-app) is writable on a read-only rootfs. This is what lets one
// read-only rootfs file back every microVM restored from the warm-base
// snapshot: the mutable unpack state lives in the snapshot memfile (RAM),
// loaded copy-on-write per restore. Best-effort: a failure is logged rather
// than fatal (tmpfs mounts effectively never fail; a missing tmpfs only
// degrades to a failed write the shim's unpack surfaces). Mirrors the sandbox
// guest-init's mountTmpfsTmp.
func mountTmpfsTmp(logger *slog.Logger) {
	if err := unix.Mount("tmpfs", "/tmp", "tmpfs", 0, "size=256m,mode=1777"); err != nil {
		logger.Warn("tmpfs mount on /tmp failed", "err", err)
		return
	}
	logger.Info("mounted tmpfs on /tmp")
}
