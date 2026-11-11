//go:build linux

// Package guestvsock adapts guest AF_VSOCK listeners to net.Listener.
package guestvsock

import (
	"fmt"
	"net"
	"os"

	"golang.org/x/sys/unix"
)

// Options preserves each runtime's diagnostic context and accepted-file label.
// ErrorPrefix includes any trailing separator; FilePrefix excludes the port.
type Options struct {
	ErrorPrefix     string
	FilePrefix      string
	WrapAcceptError bool
}

// socketOps is private so callers cannot replace the socket lifecycle.
type socketOps struct {
	socket func(int, int, int) (int, error)
	bind   func(int, unix.Sockaddr) error
	listen func(int, int) error
	accept func(int) (int, unix.Sockaddr, error)
	close  func(int) error
}

// Listen binds CID_ANY at port with backlog 16. Closing the listener and file
// deadlines retain the underlying unix.Close and os.File behavior; this package
// adds no idempotent-close, accept-unblocking or deadline guarantees.
func Listen(port uint32, options Options) (net.Listener, error) {
	return listen(port, options, socketOps{
		socket: unix.Socket, bind: unix.Bind, listen: unix.Listen,
		accept: unix.Accept, close: unix.Close,
	})
}

func listen(port uint32, options Options, ops socketOps) (net.Listener, error) {
	fd, err := ops.socket(unix.AF_VSOCK, unix.SOCK_STREAM, 0)
	if err != nil {
		return nil, fmt.Errorf("%ssocket: %w", options.ErrorPrefix, err)
	}
	if err := ops.bind(fd, &unix.SockaddrVM{CID: unix.VMADDR_CID_ANY, Port: port}); err != nil {
		_ = ops.close(fd)
		return nil, fmt.Errorf("%sbind port=%d: %w", options.ErrorPrefix, port, err)
	}
	if err := ops.listen(fd, 16); err != nil {
		_ = ops.close(fd)
		return nil, fmt.Errorf("%slisten port=%d: %w", options.ErrorPrefix, port, err)
	}
	return &listener{fd: fd, port: port, options: options, ops: ops}, nil
}

type listener struct {
	fd      int
	port    uint32
	options Options
	ops     socketOps
}

func (l *listener) Accept() (net.Conn, error) {
	fd, _, err := l.ops.accept(l.fd)
	if err != nil {
		if l.options.WrapAcceptError {
			return nil, fmt.Errorf("%saccept: %w", l.options.ErrorPrefix, err)
		}
		return nil, err
	}
	// The accepted descriptor transfers to exactly one os.File owner.
	file := os.NewFile(uintptr(fd), fmt.Sprintf("%s:%d", l.options.FilePrefix, l.port))
	return &conn{File: file}, nil
}

func (l *listener) Close() error   { return l.ops.close(l.fd) }
func (l *listener) Addr() net.Addr { return addr{port: l.port} }

type conn struct{ *os.File }

func (c *conn) LocalAddr() net.Addr  { return addr{} }
func (c *conn) RemoteAddr() net.Addr { return addr{} }

type addr struct{ port uint32 }

func (a addr) Network() string { return "vsock" }
func (a addr) String() string  { return fmt.Sprintf("vsock:%d", a.port) }

var (
	_ net.Listener = (*listener)(nil)
	_ net.Conn     = (*conn)(nil)
	_ net.Addr     = addr{}
)
