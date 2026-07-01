//go:build !linux

package vsockdial

import (
	"fmt"
	"net"
	"runtime"
)

// Listen always fails off Linux: AF_VSOCK is Linux-only. The guest runs inside a
// Linux microVM; this stub only keeps the package building under the host
// toolchain (e.g. darwin) for unit tests and local builds.
func Listen(port uint32) (net.Listener, error) {
	return nil, fmt.Errorf("vsockdial: AF_VSOCK unsupported on %s", runtime.GOOS)
}
