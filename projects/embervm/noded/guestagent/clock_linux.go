//go:build linux

package guestagent

import "golang.org/x/sys/unix"

// realClock is the production Clock: it drives CLOCK_REALTIME through the
// clock_settime / clock_gettime syscalls. Setting the clock requires
// CAP_SYS_TIME, which the k3s guest has (it runs as root inside the microVM, see
// the image README); a lower-privilege guest would get EPERM here, surfaced to
// the node as a sync_clock err.
type realClock struct{}

// RealClock is the syscall-backed Clock the guest-init wires into the agent.
func RealClock() Clock { return realClock{} }

// SetRealtime writes epochNs to CLOCK_REALTIME via clock_settime. unix.Timespec
// splits the nanosecond epoch into whole seconds plus a nanosecond remainder.
func (realClock) SetRealtime(epochNs int64) error {
	ts := unix.NsecToTimespec(epochNs)
	return unix.ClockSettime(unix.CLOCK_REALTIME, &ts)
}

// GetRealtime reads CLOCK_REALTIME back via clock_gettime and recomposes the
// nanosecond epoch, so the node verifies the applied clock rather than trusting
// the value it sent.
func (realClock) GetRealtime() (int64, error) {
	var ts unix.Timespec
	if err := unix.ClockGettime(unix.CLOCK_REALTIME, &ts); err != nil {
		return 0, err
	}
	return ts.Nano(), nil
}
