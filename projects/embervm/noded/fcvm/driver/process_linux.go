//go:build linux

package driver

import (
	"errors"

	"golang.org/x/sys/unix"
)

// processRunning checks an owned child without consuming its exit status.
// WNOWAIT keeps cmd.Wait the sole reaper even when its watcher runs concurrently.
// Unlike signal 0 or Kill, this distinguishes a zombie from a running VMM.
func processRunning(pid int) (bool, error) {
	var info unix.Siginfo
	err := unix.Waitid(unix.P_PID, pid, &info, unix.WEXITED|unix.WNOWAIT|unix.WNOHANG, nil)
	if errors.Is(err, unix.ECHILD) {
		return false, nil // The cached Wait already reaped this owned child.
	}
	if err != nil {
		return false, err
	}
	return info.Signo == 0, nil
}
