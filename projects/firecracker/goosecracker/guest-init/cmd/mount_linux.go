//go:build linux

package main

import (
	"log/slog"

	"golang.org/x/sys/unix"
)

// mountTmpfs mounts a tmpfs of the given size over target so guest state written
// there lives in RAM rather than on the read-only rootfs drive. This keeps the
// rootfs read-only and shareable across disposable microVMs while giving goose
// writable scratch (/tmp, HOME) and a writable workspace. Best-effort: a failure
// is logged rather than fatal, since tmpfs mounts effectively never fail in
// practice and a missing tmpfs only degrades to a failed write on the read-only
// rootfs.
func mountTmpfs(logger *slog.Logger, target, size string) {
	if err := unix.Mount("tmpfs", target, "tmpfs", 0, "size="+size+",mode=1777"); err != nil {
		logger.Warn("tmpfs mount failed", "target", target, "err", err)
		return
	}
	logger.Info("mounted tmpfs", "target", target, "size", size)
}
