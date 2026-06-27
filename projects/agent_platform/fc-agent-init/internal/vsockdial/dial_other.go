//go:build !linux

// Package vsockdial's non-Linux stub. fc-agent-init only runs as PID 1 inside a
// Linux microVM, but the package must still compile under the host toolchain
// (e.g. a darwin developer build), where AF_VSOCK does not exist.
package vsockdial

import (
	"fmt"
	"io"
	"runtime"
)

// Dial always fails off Linux: AF_VSOCK is Linux-only.
func Dial(cid, port uint32) (io.ReadWriteCloser, error) {
	return nil, fmt.Errorf("vsockdial: AF_VSOCK unsupported on %s", runtime.GOOS)
}
