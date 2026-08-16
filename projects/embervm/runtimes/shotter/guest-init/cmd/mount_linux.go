//go:build linux

package main

import (
	"log/slog"

	"golang.org/x/sys/unix"
)

func mountProc(logger *slog.Logger) {
	if err := unix.Mount("proc", "/proc", "proc", 0, ""); err != nil {
		logger.Warn("ember-shotter-init: proc mount on /proc failed", "err", err)
		return
	}
	logger.Info("ember-shotter-init: mounted proc on /proc")
}

// mountTmpfsTmp puts Chromium's profile, cache, and shared-memory fallback on
// RAM captured by the base memfile. The rootfs is read-only and shared by every
// clone, so browser writes cannot live there. 512 MiB is deliberate headroom
// for a warm browser plus one capture. If measurements require raising it, the
// shotter workload's memMib must be raised at the same time: tmpfs pages consume
// that guest memory budget rather than adding capacity outside it.
func mountTmpfsTmp(logger *slog.Logger) {
	if err := unix.Mount("tmpfs", "/tmp", "tmpfs", 0, "size=512m,mode=1777"); err != nil {
		logger.Warn("ember-shotter-init: tmpfs mount on /tmp failed", "err", err)
		return
	}
	logger.Info("ember-shotter-init: mounted tmpfs on /tmp", "size", "512m")
}
