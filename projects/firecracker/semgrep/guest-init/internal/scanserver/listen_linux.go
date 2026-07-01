//go:build linux

package scanserver

import (
	"fmt"
	"io"
	"os"

	"golang.org/x/sys/unix"
)

// Listen binds an AF_VSOCK stream socket to (VMADDR_CID_ANY, port) and returns a
// Listener. The guest accepts on its own CID, so the bind CID is ANY; the host
// dials the guest on this port (vsockproto.ScanPort). This mirrors the dial-side
// AF_VSOCK code in fc-agent-init/internal/vsockdial, for LISTEN/accept.
func Listen(port uint32) (Listener, error) {
	fd, err := unix.Socket(unix.AF_VSOCK, unix.SOCK_STREAM, 0)
	if err != nil {
		return nil, fmt.Errorf("scanserver: socket: %w", err)
	}
	if err := unix.Bind(fd, &unix.SockaddrVM{CID: unix.VMADDR_CID_ANY, Port: port}); err != nil {
		_ = unix.Close(fd)
		return nil, fmt.Errorf("scanserver: bind port=%d: %w", port, err)
	}
	if err := unix.Listen(fd, 16); err != nil {
		_ = unix.Close(fd)
		return nil, fmt.Errorf("scanserver: listen port=%d: %w", port, err)
	}
	return &vsockListener{fd: fd, port: port}, nil
}

type vsockListener struct {
	fd   int
	port uint32
}

// Accept blocks for the next connection and returns it as an os.File-backed
// io.ReadWriteCloser (Go's net package does not recognise AF_VSOCK, so the
// connected fd is driven by the runtime poller via os.NewFile, as on the dial
// side).
func (l *vsockListener) Accept() (io.ReadWriteCloser, error) {
	nfd, _, err := unix.Accept(l.fd)
	if err != nil {
		return nil, fmt.Errorf("scanserver: accept: %w", err)
	}
	return os.NewFile(uintptr(nfd), fmt.Sprintf("vsock-scan:%d", l.port)), nil
}

func (l *vsockListener) Close() error { return unix.Close(l.fd) }
