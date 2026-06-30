//go:build !linux

package scanserver

import (
	"fmt"
	"runtime"
)

// Listen always fails off Linux: AF_VSOCK is Linux-only. semgrep-guest-init runs
// as PID 1 inside a Linux microVM; this stub only keeps the package building under
// the host (e.g. darwin) toolchain for the unit tests, which inject a fake
// Listener and never call Listen.
func Listen(port uint32) (Listener, error) {
	return nil, fmt.Errorf("scanserver: AF_VSOCK unsupported on %s", runtime.GOOS)
}
