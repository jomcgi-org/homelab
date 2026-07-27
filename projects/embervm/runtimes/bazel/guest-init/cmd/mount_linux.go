//go:build linux

package main

import (
	"log/slog"

	"golang.org/x/sys/unix"
)

// mountProc mounts a procfs on /proc. A raw Firecracker boot leaves /proc
// unmounted; bazel and its cc toolchain probe read /proc, so mount it. Best
// -effort: a failure is logged, not fatal.
func mountProc(logger *slog.Logger) {
	if err := unix.Mount("proc", "/proc", "proc", 0, ""); err != nil {
		logger.Warn("proc mount on /proc failed", "err", err)
		return
	}
	logger.Info("mounted proc on /proc")
}

// mountTmpfsTmp mounts a LARGE tmpfs over /tmp so all of bazel's mutable state
// (install base + output base under /tmp/bazel, HOME cache under /tmp/home) lives
// in RAM. This is load-bearing, not a convenience: the base-snapshot rootfs is
// read-only and shared by every restored clone (noded RootfsReadOnly), so bazel
// state MUST be captured in the memfile, i.e. it must be on tmpfs. Phase 1
// instrumentation measured 203 MiB at the base snapshot cutoff, so 512 MiB
// provides 2.5x headroom. Too small an fs makes warming fail with ENOSPC, which
// fails the base build loudly (correct and intentional).
func mountTmpfsTmp(logger *slog.Logger) {
	if err := unix.Mount("tmpfs", "/tmp", "tmpfs", 0, "size=512m,mode=1777"); err != nil {
		logger.Warn("tmpfs mount on /tmp failed", "err", err)
		return
	}
	logger.Info("mounted tmpfs on /tmp", "size", "512m")
}
