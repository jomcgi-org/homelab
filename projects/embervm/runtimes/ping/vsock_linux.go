//go:build linux

// ListenVsock binds an AF_VSOCK stream socket to (VMADDR_CID_ANY, port) and
// returns it as a net.Listener, mirroring the semgrep guest-init's scanserver
// helper. Replicated rather than imported because that helper is internal to
// the semgrep guest-init package.
package main

import (
	"fmt"
	"net"
	"os"

	"golang.org/x/sys/unix"
)

func listenVsock(port uint32) (net.Listener, error) {
	fd, err := unix.Socket(unix.AF_VSOCK, unix.SOCK_STREAM, 0)
	if err != nil {
		return nil, fmt.Errorf("socket: %w", err)
	}
	if err := unix.Bind(fd, &unix.SockaddrVM{CID: unix.VMADDR_CID_ANY, Port: port}); err != nil {
		_ = unix.Close(fd)
		return nil, fmt.Errorf("bind port=%d: %w", port, err)
	}
	if err := unix.Listen(fd, 16); err != nil {
		_ = unix.Close(fd)
		return nil, fmt.Errorf("listen port=%d: %w", port, err)
	}
	return &vsockListener{fd: fd, port: port}, nil
}

type vsockListener struct {
	fd   int
	port uint32
}

func (l *vsockListener) Accept() (net.Conn, error) {
	nfd, _, err := unix.Accept(l.fd)
	if err != nil {
		return nil, err
	}
	f := os.NewFile(uintptr(nfd), fmt.Sprintf("vsock-http:%d", l.port))
	return &vsockConn{File: f}, nil
}

func (l *vsockListener) Close() error   { return unix.Close(l.fd) }
func (l *vsockListener) Addr() net.Addr { return vsockAddr{port: l.port} }

type vsockConn struct{ *os.File }

func (c *vsockConn) LocalAddr() net.Addr  { return vsockAddr{} }
func (c *vsockConn) RemoteAddr() net.Addr { return vsockAddr{} }

type vsockAddr struct{ port uint32 }

func (a vsockAddr) Network() string { return "vsock" }
func (a vsockAddr) String() string  { return fmt.Sprintf("vsock:%d", a.port) }
