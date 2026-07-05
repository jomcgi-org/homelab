//go:build !linux

package main

import (
	"fmt"
	"net"
	"runtime"
)

// listenVsock always fails off Linux: AF_VSOCK is Linux-only. The sandbox
// guest runs inside a Linux microVM; this stub only keeps the package
// building under the host (e.g. darwin) toolchain for unit tests and local
// builds.
func listenVsock(port uint32) (net.Listener, error) {
	return nil, fmt.Errorf("sandbox-guest-init: AF_VSOCK unsupported on %s", runtime.GOOS)
}
