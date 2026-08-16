//go:build linux

package main

import (
	"fmt"
	"log/slog"
	"os"
	"os/exec"
	"syscall"

	"golang.org/x/sys/unix"
)

const (
	guestHostname = "ember-shotter"
	browserUID    = 65532
	browserGID    = 65532
)

func bringUpLoopback(logger *slog.Logger) {
	if err := bringUpLoopbackIoctl(); err != nil {
		logger.Error("ember-shotter-init: could not bring up loopback; CDP cannot become ready", "err", err)
		return
	}
	logger.Info("ember-shotter-init: loopback up")
}

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
	ifr.SetUint16(ifr.Uint16() | unix.IFF_UP | unix.IFF_RUNNING)
	if err := unix.IoctlIfreq(fd, unix.SIOCSIFFLAGS, ifr); err != nil {
		return fmt.Errorf("set lo flags: %w", err)
	}
	return nil
}

func setHostname(logger *slog.Logger) {
	if current, err := os.Hostname(); err == nil && current != "" && current != "(none)" && current != "localhost" {
		logger.Info("ember-shotter-init: hostname already set", "hostname", current)
		return
	}
	if err := unix.Sethostname([]byte(guestHostname)); err != nil {
		logger.Warn("ember-shotter-init: sethostname failed", "hostname", guestHostname, "err", err)
		return
	}
	logger.Info("ember-shotter-init: hostname set", "hostname", guestHostname)
}

func setWallClock(epochMs int64) error {
	ts := unix.NsecToTimespec(epochMs * int64(1_000_000))
	return unix.ClockSettime(unix.CLOCK_REALTIME, &ts)
}

// setBrowserCredential drops only the Chromium process to the image's non-root
// uid. PID 1 remains root because mounting procfs and tmpfs requires it.
func setBrowserCredential(cmd *exec.Cmd) {
	if os.Geteuid() != 0 {
		return
	}
	cmd.SysProcAttr = &syscall.SysProcAttr{
		Credential: &syscall.Credential{
			Uid:    browserUID,
			Gid:    browserGID,
			Groups: []uint32{},
		},
	}
}
