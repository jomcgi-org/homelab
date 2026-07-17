//go:build !linux

package guestagent

import (
	"fmt"
	"runtime"
)

// realClock off Linux always fails: CLOCK_REALTIME set/get via the Linux
// clock_settime/clock_gettime syscalls is Linux-only. The guest agent runs
// inside a Linux microVM; this stub keeps the package building under the host
// (e.g. darwin) toolchain for unit tests, which use a fake Clock rather than
// this one.
type realClock struct{}

// RealClock returns the stub Clock so the package compiles off Linux.
func RealClock() Clock { return realClock{} }

func (realClock) SetRealtime(int64) error {
	return fmt.Errorf("guestagent: CLOCK_REALTIME set unsupported on %s", runtime.GOOS)
}

func (realClock) GetRealtime() (int64, error) {
	return 0, fmt.Errorf("guestagent: CLOCK_REALTIME get unsupported on %s", runtime.GOOS)
}
