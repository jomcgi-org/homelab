//go:build linux

package vsockdial

import (
	"fmt"
	"net"
	"os"

	"golang.org/x/sys/unix"
)

// Listen binds an AF_VSOCK stream socket to (VMADDR_CID_ANY, port) and returns a
// net.Listener the shim HTTP server serves on. The guest accepts on its own CID,
// so the bind CID is ANY; the fc-invoke daemon connects to this port over vsock.
// This is the inbound counterpart to Dial (the outbound egress hop); both live in
// this package so all AF_VSOCK code stays in one place.
func Listen(port uint32) (net.Listener, error) {
	fd, err := unix.Socket(unix.AF_VSOCK, unix.SOCK_STREAM, 0)
	if err != nil {
		return nil, fmt.Errorf("vsockdial: socket: %w", err)
	}
	if err := unix.Bind(fd, &unix.SockaddrVM{CID: unix.VMADDR_CID_ANY, Port: port}); err != nil {
		_ = unix.Close(fd)
		return nil, fmt.Errorf("vsockdial: bind port=%d: %w", port, err)
	}
	if err := unix.Listen(fd, 16); err != nil {
		_ = unix.Close(fd)
		return nil, fmt.Errorf("vsockdial: listen port=%d: %w", port, err)
	}
	return &vsockNetListener{fd: fd, port: port}, nil
}

// vsockNetListener wraps a raw AF_VSOCK listening fd as a net.Listener so the
// shim HTTP server can accept connections without knowing about vsock.
type vsockNetListener struct {
	fd   int
	port uint32
}

func (l *vsockNetListener) Accept() (net.Conn, error) {
	nfd, _, err := unix.Accept(l.fd)
	if err != nil {
		return nil, fmt.Errorf("vsockdial: accept: %w", err)
	}
	f := os.NewFile(uintptr(nfd), fmt.Sprintf("vsock-http:%d", l.port))
	return &vsockNetConn{File: f}, nil
}

func (l *vsockNetListener) Close() error { return unix.Close(l.fd) } // nosemgrep: no-bare-error-return

func (l *vsockNetListener) Addr() net.Addr { return vsockAddr{port: l.port} }

// vsockNetConn wraps an *os.File-backed vsock connection as a net.Conn.
// net/http requires net.Conn, not just io.ReadWriteCloser. os.File provides
// Read/Write/Close and the three SetDeadline methods via promotion; we add
// LocalAddr/RemoteAddr which os.File lacks.
type vsockNetConn struct {
	*os.File
}

func (c *vsockNetConn) LocalAddr() net.Addr  { return vsockAddr{} }
func (c *vsockNetConn) RemoteAddr() net.Addr { return vsockAddr{} }

// vsockAddr is a minimal net.Addr for AF_VSOCK endpoints. The HTTP server uses
// it only for logging; actual addressing is handled by the vsock port.
type vsockAddr struct{ port uint32 }

func (a vsockAddr) Network() string { return "vsock" }
func (a vsockAddr) String() string  { return fmt.Sprintf("vsock:%d", a.port) }
