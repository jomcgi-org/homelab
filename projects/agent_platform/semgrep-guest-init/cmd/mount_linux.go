//go:build linux

package main

import (
	"log/slog"

	"golang.org/x/sys/unix"
)

// mountTmpfsTmp mounts a tmpfs over /tmp so all of the guest's mutable state (the
// semgrep workspace, its isolated HOME, the git repo) lives in RAM rather than on
// the rootfs drive. This is what lets the rootfs stay read-only and be shared by
// every microVM restored from one warm-base snapshot: the mutable state is captured
// in the snapshot memfile (RAM), which Firecracker loads copy-on-write per restore,
// so concurrent restored guests never write the single shared rootfs file. Mounted
// before the workspace/HOME directories are created so they land on the tmpfs. The
// rootfs is read-only, so a tmpfs failure makes the subsequent workspace MkdirAll
// fail and surfaces there; logged here rather than aborting, since tmpfs mounts
// effectively never fail in practice.
func mountTmpfsTmp(logger *slog.Logger) {
	if err := unix.Mount("tmpfs", "/tmp", "tmpfs", 0, "size=256m,mode=1777"); err != nil {
		logger.Warn("tmpfs mount on /tmp failed", "err", err)
		return
	}
	logger.Info("mounted tmpfs on /tmp")
}
