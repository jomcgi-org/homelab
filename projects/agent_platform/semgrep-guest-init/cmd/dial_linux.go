//go:build linux

package main

import (
	"fmt"
	"io"
	"os"

	"golang.org/x/sys/unix"
)

// dialVsock opens a stream AF_VSOCK connection to (cid, port) from inside the
// guest, used to reach the host controller's ControlPort and announce readiness.
// Go's net package does not recognise AF_VSOCK, so the connected fd is wrapped in
// an *os.File (driven by the runtime poller), as on the fc-agent-init dial side.
func dialVsock(cid, port uint32) (io.ReadWriteCloser, error) {
	fd, err := unix.Socket(unix.AF_VSOCK, unix.SOCK_STREAM, 0)
	if err != nil {
		return nil, fmt.Errorf("dialVsock: socket: %w", err)
	}
	if err := unix.Connect(fd, &unix.SockaddrVM{CID: cid, Port: port}); err != nil {
		_ = unix.Close(fd)
		return nil, fmt.Errorf("dialVsock: connect cid=%d port=%d: %w", cid, port, err)
	}
	return os.NewFile(uintptr(fd), fmt.Sprintf("vsock:%d:%d", cid, port)), nil
}
