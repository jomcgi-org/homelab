//go:build !linux

package scanserver

import (
	"fmt"
	"net"
	"runtime"
)

// ListenVsock always fails off Linux: AF_VSOCK is Linux-only. The semgrep
// guest runs inside a Linux microVM; this stub only keeps the package building
// under the host (e.g. darwin) toolchain for unit tests and local builds.
func ListenVsock(port uint32) (net.Listener, error) {
	return nil, fmt.Errorf("scanserver: AF_VSOCK unsupported on %s", runtime.GOOS)
}
