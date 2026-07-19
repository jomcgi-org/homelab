//go:build linux

package main

import (
	"fmt"
	"log/slog"
	"os"

	"golang.org/x/sys/unix"
)

// guestHostname is the name set on the guest and matched in the baked /etc/hosts
// (see the BUILD's guest_init_tar). Any stable, resolvable name works; it exists
// only so InetAddress.getLocalHost() resolves without a DNS round trip.
const guestHostname = "ember-bazel"

// loopbackUpFlags returns the interface flags with the up + running bits set. It
// is the pure flag-math half of bringUpLoopback, split out so the OR is unit
// tested without a socket (bringing an interface up is read-modify-write:
// SIOCGIFFLAGS gives the current flags, we set IFF_UP|IFF_RUNNING, SIOCSIFFLAGS
// writes them back, preserving any other bits the kernel already had).
func loopbackUpFlags(cur uint16) uint16 {
	return cur | unix.IFF_UP | unix.IFF_RUNNING
}

// bringUpLoopback brings the loopback interface UP via the SIOCSIFFLAGS ioctl. A
// raw Firecracker boot leaves `lo` DOWN, and the bazel client connects to the
// bazel SERVER over a gRPC socket on 127.0.0.1; with lo down that connect blocks
// forever and the warming VM sits at ZERO CPU with no output. This is best-effort
// by design: on failure it logs LOUDLY to the console (so a still-hung warming is
// diagnosable) but does not abort the boot.
//
// It issues the ioctl directly rather than shelling out: Wolfi's busybox ships
// NEITHER an `ip` nor an `ifconfig` applet (confirmed on the live guest:
// `exec: "ip": executable file not found in $PATH`), so the exec path could never
// work in this image. The vendored x/sys/unix DOES expose a type-safe Ifreq
// wrapper (NewIfreq + IoctlIfreq, present since 2021), so no hand-laid ifreq union
// struct is needed and the code is arch-portable.
func bringUpLoopback(logger *slog.Logger) {
	if err := bringUpLoopbackIoctl(); err != nil {
		logger.Error("ember-bazel-init: could NOT bring up loopback; bazel client may block connecting to the server on 127.0.0.1 (warming will hang)", "err", err)
		return
	}
	logger.Info("ember-bazel-init: loopback up (ioctl)")
}

// bringUpLoopbackIoctl performs the read-modify-write on `lo`'s flags. A DGRAM
// socket on AF_INET is the conventional handle for interface-flag ioctls (the
// socket family is irrelevant; it is only a file descriptor to carry the ioctl).
// CLOEXEC so the transient fd never leaks into the bazel subprocess.
func bringUpLoopbackIoctl() error {
	fd, err := unix.Socket(unix.AF_INET, unix.SOCK_DGRAM|unix.SOCK_CLOEXEC, 0)
	if err != nil {
		return fmt.Errorf("open ioctl socket: %w", err)
	}
	defer unix.Close(fd)

	ifr, err := unix.NewIfreq("lo")
	if err != nil {
		return fmt.Errorf("new ifreq lo: %w", err)
	}
	if err := unix.IoctlIfreq(fd, unix.SIOCGIFFLAGS, ifr); err != nil {
		return fmt.Errorf("get lo flags: %w", err)
	}
	ifr.SetUint16(loopbackUpFlags(ifr.Uint16()))
	if err := unix.IoctlIfreq(fd, unix.SIOCSIFFLAGS, ifr); err != nil {
		return fmt.Errorf("set lo flags up: %w", err)
	}
	return nil
}

// setHostname sets the guest hostname when it is unset or the default. It pairs
// with the baked /etc/hosts (127.0.0.1 localhost + guestHostname), so a JVM
// InetAddress.getLocalHost() resolves locally instead of stalling on a lookup.
// Best-effort: a failure is logged, not fatal.
func setHostname(logger *slog.Logger) {
	if cur, err := os.Hostname(); err == nil && cur != "" && cur != "(none)" && cur != "localhost" {
		// A meaningful hostname is already set (e.g. noded injected one); leave it.
		logger.Info("ember-bazel-init: hostname already set", "hostname", cur)
		return
	}
	if err := unix.Sethostname([]byte(guestHostname)); err != nil {
		logger.Warn("ember-bazel-init: sethostname failed", "hostname", guestHostname, "err", err)
		return
	}
	logger.Info("ember-bazel-init: hostname set", "hostname", guestHostname)
}
