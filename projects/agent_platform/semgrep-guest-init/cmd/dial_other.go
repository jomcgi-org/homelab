//go:build !linux

package main

import (
	"fmt"
	"io"
	"runtime"
)

// dialVsock always fails off Linux: AF_VSOCK is Linux-only. Off-cluster there is no
// host controller anyway, so the caller treats the failure as "no controller" and
// proceeds without announcing readiness.
func dialVsock(cid, port uint32) (io.ReadWriteCloser, error) {
	return nil, fmt.Errorf("dialVsock: AF_VSOCK unsupported on %s", runtime.GOOS)
}
