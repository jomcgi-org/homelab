//go:build linux

// Package vsockdial opens an AF_VSOCK stream connection from inside the guest to
// the host controller. It is the guest half of the ADR 022 control channel; the
// host half is a plain unix-domain listener (Firecracker bridges guest vsock
// connections to a per-thread host socket), so only the guest needs real vsock.
package vsockdial

import (
	"fmt"
	"io"
	"os"

	"golang.org/x/sys/unix"
)

// Dial opens a stream AF_VSOCK connection to (cid, port) and returns it as an
// io.ReadWriteCloser. Go's net package does not recognise AF_VSOCK, so the
// connected fd is wrapped in an *os.File (which the runtime poller drives) rather
// than net.FileConn. The caller layers vsockproto.NewConn on top.
func Dial(cid, port uint32) (io.ReadWriteCloser, error) {
	fd, err := unix.Socket(unix.AF_VSOCK, unix.SOCK_STREAM, 0)
	if err != nil {
		return nil, fmt.Errorf("vsockdial: socket: %w", err)
	}
	if err := unix.Connect(fd, &unix.SockaddrVM{CID: cid, Port: port}); err != nil {
		_ = unix.Close(fd)
		return nil, fmt.Errorf("vsockdial: connect cid=%d port=%d: %w", cid, port, err)
	}
	// os.NewFile takes ownership of fd and registers it with the runtime netpoller,
	// so Read/Write are non-blocking and goroutine-friendly. Close closes the fd.
	return os.NewFile(uintptr(fd), fmt.Sprintf("vsock:%d:%d", cid, port)), nil
}
