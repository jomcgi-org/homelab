//go:build linux

package main

import (
	"log/slog"

	"golang.org/x/sys/unix"
)

// bringUpLoopback sets the loopback interface UP. semgrep-guest-init is the guest's
// PID 1 and there is no init system, so nothing else brings lo up. Until it is up,
// a bind to a loopback address succeeds but no traffic flows over 127.0.0.1. The
// kernel already assigns 127.0.0.1/8 to lo, so only the IFF_UP flag is missing; we
// flip it via SIOCSIFFLAGS. Best-effort: a failure is logged, not fatal.
func bringUpLoopback(logger *slog.Logger) {
	fd, err := unix.Socket(unix.AF_INET, unix.SOCK_DGRAM|unix.SOCK_CLOEXEC, 0)
	if err != nil {
		logger.Warn("loopback: open socket failed", "err", err)
		return
	}
	defer unix.Close(fd)

	ifr, err := unix.NewIfreq("lo")
	if err != nil {
		logger.Warn("loopback: new ifreq failed", "err", err)
		return
	}
	if err := unix.IoctlIfreq(fd, unix.SIOCGIFFLAGS, ifr); err != nil {
		logger.Warn("loopback: get flags failed", "err", err)
		return
	}
	flags := ifr.Uint16()
	if flags&unix.IFF_UP != 0 {
		logger.Info("loopback already up")
		return
	}
	ifr.SetUint16(flags | uint16(unix.IFF_UP) | uint16(unix.IFF_RUNNING))
	if err := unix.IoctlIfreq(fd, unix.SIOCSIFFLAGS, ifr); err != nil {
		logger.Warn("loopback: set up failed", "err", err)
		return
	}
	logger.Info("loopback interface up")
}
