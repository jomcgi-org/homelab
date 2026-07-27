//go:build linux

package main

import (
	"log/slog"
)

func mountTmpfsTmp(logger *slog.Logger) {
	// Two GiB leaves room for Bash tool scratch files, package caches, and a
	// checkout-sized buffer during a normal agent turn.
	if err := mountFn("tmpfs", "/tmp", "tmpfs", 0, "size=2g,mode=1777"); err != nil {
		logger.Warn("tmpfs mount on /tmp failed", "err", err)
		return
	}
	logger.Info("mounted tmpfs on /tmp")
}

func mountProc(logger *slog.Logger) {
	if err := mountFn("proc", "/proc", "proc", 0, ""); err != nil {
		logger.Warn("proc mount on /proc failed", "err", err)
		return
	}
	logger.Info("mounted proc on /proc")
}
