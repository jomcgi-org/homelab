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

// mountProc mounts a procfs on /proc. A raw Firecracker boot hands PID 1 no
// mounted /proc (there is no initramfs or init system to do it), so without
// this /proc/cmdline is absent and the serving-port / handler-disk boot-arg
// translation (setServingPortEnv / setHandlerDiskEnv) silently no-ops, leaving
// a serving cold boot stuck on the vsock path with no handler. Task/session and
// build boots never read the cmdline (they use vsock defaults + a /shim/hydrate
// POST), which is why this was latent until serving needed it. Best-effort: an
// already-mounted or failed /proc is logged, not fatal.
func mountProc(logger *slog.Logger) {
	if err := unix.Mount("proc", "/proc", "proc", 0, ""); err != nil {
		logger.Warn("proc mount on /proc failed", "err", err)
		return
	}
	logger.Info("mounted proc on /proc")
}
