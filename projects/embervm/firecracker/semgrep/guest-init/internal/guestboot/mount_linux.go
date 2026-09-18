//go:build linux

package guestboot

import (
	"log/slog"

	"golang.org/x/sys/unix"
)

// MountTmpfsTmp mounts a tmpfs over /tmp so all of the guest's mutable state (the
// engine HOME, its settings file, and for the full scanner the materialized repo
// tree) lives in RAM rather than on the read-only rootfs. size is the tmpfs size
// option (e.g. "256m" for the single-file warm path, larger for the full-scan
// path that writes the whole repo tree). tmpfs allocates pages on write, so size
// is a ceiling, not a reservation. Mounted before any HOME/tree dirs are created
// so they land on the tmpfs. Best-effort: a failure is logged, and the later
// MkdirAll on the read-only rootfs surfaces it.
func MountTmpfsTmp(logger *slog.Logger, size string) {
	if err := unix.Mount("tmpfs", "/tmp", "tmpfs", 0, "size="+size+",mode=1777"); err != nil {
		logger.Warn("tmpfs mount on /tmp failed", "err", err)
		return
	}
	logger.Info("mounted tmpfs on /tmp", "size", size)
}
