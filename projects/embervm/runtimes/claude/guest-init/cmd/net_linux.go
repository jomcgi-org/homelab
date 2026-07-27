//go:build linux

package main

import (
	"fmt"
	"log/slog"

	"golang.org/x/sys/unix"
)

// loopbackUpFlags returns the current interface flags with IFF_UP set while
// preserving all other flags returned by the kernel.
func loopbackUpFlags(cur uint16) uint16 {
	return cur | unix.IFF_UP
}

// bringUpLoopback makes 127.0.0.1 available for the shim's local egress
// listener. Raw Firecracker boots leave lo down. This is best-effort so a
// failure is visible in the guest logs without preventing the runtime boot.
func bringUpLoopback(logger *slog.Logger) {
	if err := bringUpLoopbackIoctl(); err != nil {
		logger.Warn("ember-claude-init: could not bring up loopback", "err", err)
		return
	}
	logger.Info("ember-claude-init: loopback up")
}

// bringUpLoopbackIoctl performs the read-modify-write of lo's flags using the
// conventional AF_INET datagram ioctl socket.
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
