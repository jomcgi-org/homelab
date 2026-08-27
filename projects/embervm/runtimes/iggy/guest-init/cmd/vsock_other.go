//go:build !linux

package main

import (
	"fmt"
	"net"
	"runtime"
)

// listenVsock always fails off Linux: AF_VSOCK is Linux-only. The iggy guest
// runs inside a Linux microVM; this stub keeps the package building under the
// host (e.g. darwin) toolchain for unit tests and local builds.
func listenVsock(_ uint32) (net.Listener, error) {
	return nil, fmt.Errorf("ember-iggy-init: AF_VSOCK unsupported on %s", runtime.GOOS)
}
