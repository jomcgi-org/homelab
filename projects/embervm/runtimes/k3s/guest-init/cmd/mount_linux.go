//go:build linux

package main

import (
	"log/slog"
	"os"

	"golang.org/x/sys/unix"
)

// mountGuestFilesystems mounts the writable and pseudo filesystems k3s needs on
// the read-only, snapshot-shared rootfs. Every mount is best-effort (logged, not
// fatal): a genuinely missing mount surfaces as a k3s startup failure downstream,
// which is the honest place for that failure, and a base build (no k3s) still
// boots. Mirrors the postgres/python runtime guest-init mount helpers.
//
// The read-only rootfs bakes the airgap tarball at
// /var/lib/rancher/k3s/agent/images (a read-only baked file k3s imports); k3s's
// WRITABLE state (server db, kubelet, containerd) lives under other subtrees, so
// a tmpfs is layered over /var/lib/rancher (large: containerd image unpack +
// etcd/sqlite) with the baked images bind-restored on top. To keep this init
// simple and the airgap import working, /var/lib/rancher is NOT tmpfs-covered;
// instead the writable children k3s creates are already writable because the
// whole tree is placed writable in the image (the apko path declaration), and
// only the volatile pseudo-fs and /run/tmp/log get tmpfs here.
func mountGuestFilesystems(logger *slog.Logger) {
	// proc and sysfs: required by k3s (cgroup/kubelet) and by the boot-arg readers.
	mount(logger, "proc", "/proc", "proc", 0, "")
	mount(logger, "sysfs", "/sys", "sysfs", 0, "")

	// tmpfs over the volatile writable dirs. /run holds the containerd socket, CNI
	// state, and the token-auth CSV (/run/ember); /var/log holds k3s/containerd
	// logs; /tmp is generic scratch. Sizes are generous: containerd + k3s are not
	// memory-frugal, and this is RAM-backed so it counts against the memMib floor
	// the spike measures (fc-base sizing coupling).
	mount(logger, "tmpfs", "/run", "tmpfs", 0, "size=512m,mode=0755")
	mount(logger, "tmpfs", "/var/log", "tmpfs", 0, "size=128m,mode=0755")
	mount(logger, "tmpfs", "/tmp", "tmpfs", 0, "size=256m,mode=1777")

	// cgroup2 for kubelet/containerd. The kata guest kernel is cgroup2-shaped;
	// mounting it here rather than relying on k3s to do it keeps the ordering
	// explicit. Best-effort: if the kernel provides a pre-mounted cgroup2 (some
	// initramfs do) this is a harmless no-op error.
	_ = os.MkdirAll("/sys/fs/cgroup", 0o755)
	mount(logger, "cgroup2", "/sys/fs/cgroup", "cgroup2", 0, "")

	// The token-auth CSV directory on the /run tmpfs (server writes into it).
	if err := os.MkdirAll("/run/ember", 0o700); err != nil {
		logger.Warn("mkdir /run/ember failed", "err", err)
	}
}

// mount is a thin logged wrapper over unix.Mount so each call site reads as one
// line and a failure is logged with its target rather than crashing PID 1 (which
// would panic the guest kernel).
func mount(logger *slog.Logger, source, target, fstype string, flags uintptr, data string) {
	if err := unix.Mount(source, target, fstype, flags, data); err != nil {
		logger.Warn("mount failed", "target", target, "fstype", fstype, "err", err)
		return
	}
	logger.Info("mounted", "target", target, "fstype", fstype)
}
