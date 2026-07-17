//go:build linux

package main

import (
	"context"
	"fmt"
	"log/slog"
	"net"
	"os"

	"github.com/jomcgi/homelab/projects/embervm/noded/guestagent"
	"golang.org/x/sys/unix"
)

// startGuestAgent binds the guest control agent's vsock listener on the frozen
// port 1024 and serves it in a supervised goroutine for the guest's lifetime, so
// a post-resume clock resync (standing decision 7) works. A bind/listen failure
// is logged, not fatal: the clock agent is only exercised on a RELIGHT resume,
// and a fresh boot (the spike) must still bring k3s up. The context cancels the
// listener on shutdown.
func startGuestAgent(ctx context.Context, logger *slog.Logger) {
	ln, err := listenVsock(guestagent.GroupGuestAgentVsockPort)
	if err != nil {
		logger.Warn("guest control agent: vsock listen unavailable; no clock resync channel", "err", err)
		return
	}
	logger.Info("guest control agent: listening", "port", guestagent.GroupGuestAgentVsockPort)
	agent := guestagent.New(guestagent.RealClock(), logger)
	go func() {
		if err := agent.Serve(ln); err != nil {
			logger.Warn("guest control agent: serve stopped", "err", err)
		}
	}()
	go func() {
		<-ctx.Done()
		_ = ln.Close()
	}()
}

// listenVsock binds an AF_VSOCK stream socket to (VMADDR_CID_ANY, port) and
// returns a net.Listener. Mirrors the postgres runtime guest-init's listenVsock
// (the same idiom, kept a private helper here since it is this small).
func listenVsock(port uint32) (net.Listener, error) {
	fd, err := unix.Socket(unix.AF_VSOCK, unix.SOCK_STREAM, 0)
	if err != nil {
		return nil, fmt.Errorf("ember-k3s-init: vsock socket: %w", err)
	}
	if err := unix.Bind(fd, &unix.SockaddrVM{CID: unix.VMADDR_CID_ANY, Port: port}); err != nil {
		_ = unix.Close(fd)
		return nil, fmt.Errorf("ember-k3s-init: vsock bind port=%d: %w", port, err)
	}
	if err := unix.Listen(fd, 16); err != nil {
		_ = unix.Close(fd)
		return nil, fmt.Errorf("ember-k3s-init: vsock listen port=%d: %w", port, err)
	}
	return &vsockListener{fd: fd, port: port}, nil
}

// vsockListener wraps a raw AF_VSOCK listening fd as a net.Listener.
type vsockListener struct {
	fd   int
	port uint32
}

func (l *vsockListener) Accept() (net.Conn, error) {
	nfd, _, err := unix.Accept(l.fd)
	if err != nil {
		return nil, fmt.Errorf("ember-k3s-init: vsock accept: %w", err)
	}
	f := os.NewFile(uintptr(nfd), fmt.Sprintf("vsock:%d", l.port))
	return &vsockConn{File: f}, nil
}

func (l *vsockListener) Close() error { return unix.Close(l.fd) } // nosemgrep: no-bare-error-return

func (l *vsockListener) Addr() net.Addr { return vsockAddr{port: l.port} }

// vsockConn wraps an *os.File-backed vsock connection as a net.Conn.
type vsockConn struct {
	*os.File
}

func (c *vsockConn) LocalAddr() net.Addr  { return vsockAddr{} }
func (c *vsockConn) RemoteAddr() net.Addr { return vsockAddr{} }

// vsockAddr is a minimal net.Addr for AF_VSOCK endpoints (logging only).
type vsockAddr struct{ port uint32 }

func (a vsockAddr) Network() string { return "vsock" }
func (a vsockAddr) String() string  { return fmt.Sprintf("vsock:%d", a.port) }
