//go:build !linux

package main

import "fmt"

// setWallClock is unsupported off Linux; the guest is always Linux, this stub
// only keeps the package building for host (e.g. darwin) builds. Returning an
// error keeps the /shim/clock endpoint honest on a non-Linux host.
func setWallClock(int64) error {
	return fmt.Errorf("setWallClock is only supported on linux")
}
