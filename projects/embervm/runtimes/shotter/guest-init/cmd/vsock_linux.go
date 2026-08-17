//go:build linux

package main

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"os"
	"time"

	"golang.org/x/sys/unix"
)

func dialVsock(ctx context.Context, cid, port uint32) (net.Conn, error) {
	fd, err := unix.Socket(unix.AF_VSOCK, unix.SOCK_STREAM|unix.SOCK_CLOEXEC|unix.SOCK_NONBLOCK, 0)
	if err != nil {
		return nil, fmt.Errorf("ember-shotter-init: vsock socket: %w", err)
	}
	closeFD := true
	defer func() {
		if closeFD {
			_ = unix.Close(fd)
		}
	}()

	err = unix.Connect(fd, &unix.SockaddrVM{CID: cid, Port: port})
	if err != nil && !errors.Is(err, unix.EINPROGRESS) {
		return nil, fmt.Errorf("ember-shotter-init: vsock connect cid=%d port=%d: %w", cid, port, err)
	}
	for err != nil {
		pollTimeout := 100 * time.Millisecond
		if deadline, ok := ctx.Deadline(); ok && time.Until(deadline) < pollTimeout {
			pollTimeout = time.Until(deadline)
		}
		if pollTimeout <= 0 {
			return nil, fmt.Errorf("ember-shotter-init: vsock connect cid=%d port=%d: %w", cid, port, context.DeadlineExceeded)
		}
		ready, pollErr := unix.Poll([]unix.PollFd{{Fd: int32(fd), Events: unix.POLLOUT}}, int(pollTimeout.Milliseconds()))
		if pollErr != nil && !errors.Is(pollErr, unix.EINTR) {
			return nil, fmt.Errorf("ember-shotter-init: vsock poll cid=%d port=%d: %w", cid, port, pollErr)
		}
		if ctxErr := ctx.Err(); ctxErr != nil {
			return nil, fmt.Errorf("ember-shotter-init: vsock connect cid=%d port=%d: %w", cid, port, ctxErr)
		}
		if ready == 0 || errors.Is(pollErr, unix.EINTR) {
			continue
		}
		socketErr, getsockoptErr := unix.GetsockoptInt(fd, unix.SOL_SOCKET, unix.SO_ERROR)
		if getsockoptErr != nil {
			return nil, fmt.Errorf("ember-shotter-init: vsock connect status: %w", getsockoptErr)
		}
		if socketErr != 0 {
			return nil, fmt.Errorf("ember-shotter-init: vsock connect cid=%d port=%d: %w", cid, port, unix.Errno(socketErr))
		}
		err = nil
	}
	if err := unix.SetNonblock(fd, false); err != nil {
		return nil, fmt.Errorf("ember-shotter-init: set vsock blocking mode: %w", err)
	}
	file := os.NewFile(uintptr(fd), fmt.Sprintf("vsock-egress:%d:%d", cid, port))
	if file == nil {
		return nil, errors.New("ember-shotter-init: wrap vsock connection")
	}
	closeFD = false
	return &vsockConn{File: file}, nil
}

func startVsockServer(ctx context.Context, logger *slog.Logger, ready func() bool, config ProxyConfig) <-chan error {
	serveErr := make(chan error, 1)
	ln, err := listenVsock(guestHTTPPort)
	if err != nil {
		logger.Warn("ember-shotter-init: vsock listen unavailable; no server", "err", err)
		serveErr <- err
		close(serveErr)
		return serveErr
	}
	logger.Info("ember-shotter-init: vsock HTTP server listening", "port", guestHTTPPort)
	srv := &http.Server{Handler: newMux(ready, logger, config)}
	go func() { serveErr <- srv.Serve(ln) }()
	go func() {
		<-ctx.Done()
		_ = srv.Close()
	}()
	return serveErr
}

func listenVsock(port uint32) (net.Listener, error) {
	fd, err := unix.Socket(unix.AF_VSOCK, unix.SOCK_STREAM, 0)
	if err != nil {
		return nil, fmt.Errorf("ember-shotter-init: vsock socket: %w", err)
	}
	if err := unix.Bind(fd, &unix.SockaddrVM{CID: unix.VMADDR_CID_ANY, Port: port}); err != nil {
		_ = unix.Close(fd)
		return nil, fmt.Errorf("ember-shotter-init: vsock bind port=%d: %w", port, err)
	}
	if err := unix.Listen(fd, 16); err != nil {
		_ = unix.Close(fd)
		return nil, fmt.Errorf("ember-shotter-init: vsock listen port=%d: %w", port, err)
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
		return nil, fmt.Errorf("ember-shotter-init: vsock accept: %w", err)
	}
	file := os.NewFile(uintptr(nfd), fmt.Sprintf("vsock-http:%d", l.port))
	return &vsockConn{File: file}, nil
}

func (l *vsockListener) Close() error   { return unix.Close(l.fd) } // nosemgrep: no-bare-error-return
func (l *vsockListener) Addr() net.Addr { return vsockAddr{port: l.port} }

type vsockConn struct {
	*os.File
}

func (c *vsockConn) LocalAddr() net.Addr  { return vsockAddr{} }
func (c *vsockConn) RemoteAddr() net.Addr { return vsockAddr{} }

type vsockAddr struct{ port uint32 }

func (a vsockAddr) Network() string { return "vsock" }
func (a vsockAddr) String() string  { return fmt.Sprintf("vsock:%d", a.port) }
