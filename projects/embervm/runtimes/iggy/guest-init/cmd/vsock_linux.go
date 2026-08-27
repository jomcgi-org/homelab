//go:build linux

package main

import (
	"fmt"
	"net"
	"os"

	"golang.org/x/sys/unix"
)

// listenVsock binds an AF_VSOCK stream socket to (VMADDR_CID_ANY, port) and
// returns a net.Listener for shim.Server.Serve. The base build's WaitReady
// dials this port to health-gate the warm-base snapshot. Mirrors the sandbox
// guest-init's listenVsock, kept as a private helper here since it is this small.
func listenVsock(port uint32) (net.Listener, error) {
	fd, err := unix.Socket(unix.AF_VSOCK, unix.SOCK_STREAM, 0)
	if err != nil {
		return nil, fmt.Errorf("ember-iggy-init: vsock socket: %w", err)
	}
	if err := unix.Bind(fd, &unix.SockaddrVM{CID: unix.VMADDR_CID_ANY, Port: port}); err != nil {
		_ = unix.Close(fd)
		return nil, fmt.Errorf("ember-iggy-init: vsock bind port=%d: %w", port, err)
	}
	if err := unix.Listen(fd, 16); err != nil {
		_ = unix.Close(fd)
		return nil, fmt.Errorf("ember-iggy-init: vsock listen port=%d: %w", port, err)
	}
	return &vsockListener{fd: fd, port: port}, nil
}

// vsockListener wraps a raw AF_VSOCK listening fd as a net.Listener so the shim
// HTTP server can accept connections without knowing about vsock.
type vsockListener struct {
	fd   int
	port uint32
}

func (l *vsockListener) Accept() (net.Conn, error) {
	nfd, _, err := unix.Accept(l.fd)
	if err != nil {
		return nil, fmt.Errorf("ember-iggy-init: vsock accept: %w", err)
	}
	f := os.NewFile(uintptr(nfd), fmt.Sprintf("vsock-http:%d", l.port))
	return &vsockConn{File: f}, nil
}

func (l *vsockListener) Close() error { return unix.Close(l.fd) } // nosemgrep: no-bare-error-return

func (l *vsockListener) Addr() net.Addr { return vsockAddr{port: l.port} }

// vsockConn wraps an *os.File-backed vsock connection as a net.Conn. net/http
// requires net.Conn, not just io.ReadWriteCloser.
type vsockConn struct {
	*os.File
}

func (c *vsockConn) LocalAddr() net.Addr  { return vsockAddr{} }
func (c *vsockConn) RemoteAddr() net.Addr { return vsockAddr{} }

// vsockAddr is a minimal net.Addr for AF_VSOCK endpoints (logging only).
type vsockAddr struct{ port uint32 }

func (a vsockAddr) Network() string { return "vsock" }
func (a vsockAddr) String() string  { return fmt.Sprintf("vsock:%d", a.port) }
