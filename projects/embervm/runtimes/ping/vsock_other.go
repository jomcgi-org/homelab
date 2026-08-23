//go:build !linux

// Off-Linux stub: AF_VSOCK is Linux-only. The guest image builds under the
// linux/amd64 platform transition, so this file never compiles into it.
package main

import (
	"fmt"
	"net"
	"runtime"
)

func listenVsock(port uint32) (net.Listener, error) {
	return nil, fmt.Errorf("AF_VSOCK unsupported on %s", runtime.GOOS)
}
