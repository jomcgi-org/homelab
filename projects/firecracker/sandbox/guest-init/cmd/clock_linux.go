//go:build linux

package main

import (
	"golang.org/x/sys/unix"
)

// setWallClock sets the guest's wall clock (CLOCK_REALTIME) to epochMs. It is
// the /shim/clock target the node calls after restoring a guest: a snapshot's
// monotonic-frozen clock lags real time by however long it was suspended, and
// time-dependent snippet code would see the stale value without this resync.
// Best-effort by contract: the caller (the shim
// handler) turns a failure into a 500 that the node logs and moves past,
// rather than failing the relight. Requires CAP_SYS_TIME, which the guest-init
// PID 1 (root) holds; it runs BEFORE privileges are dropped per snippet.
func setWallClock(epochMs int64) error {
	ts := unix.NsecToTimespec(epochMs * int64(1_000_000))
	return unix.ClockSettime(unix.CLOCK_REALTIME, &ts)
}
