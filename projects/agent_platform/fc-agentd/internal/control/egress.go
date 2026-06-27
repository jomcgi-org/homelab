package control

import (
	"context"
	"fmt"
	"io"
	"log/slog"
	"net"
	"os"

	"github.com/jomcgi/homelab/projects/agent_platform/vsockproto"
)

// egressListenPath is the host unix socket Firecracker bridges the guest's
// egress-port connections to.
func egressListenPath(udsPath string) string {
	return fmt.Sprintf("%s_%d", udsPath, vsockproto.EgressPort)
}

// ServeEgress forwards a guest's vsock egress connections to the co-located
// egress-proxy sidecar (ADR 023). fc-agentd holds no secrets and never parses
// the bytes (a raw tunnel): the secret-holding proxy is a separate process
// reached only over localhost, preserving the blast-radius split even though
// fc-agentd parses guest control frames. Each guest connection (one per outbound
// request the guest's HTTP client opens) gets its own tunnel to sidecarAddr.
// Returns nil on ctx cancellation.
func ServeEgress(ctx context.Context, logger *slog.Logger, udsPath, sidecarAddr string) error {
	path := egressListenPath(udsPath)
	_ = os.Remove(path)
	ln, err := net.Listen("unix", path)
	if err != nil {
		return fmt.Errorf("control: egress listen %s: %w", path, err)
	}
	defer func() {
		_ = ln.Close()
		_ = os.Remove(path)
	}()

	go func() {
		<-ctx.Done()
		_ = ln.Close()
	}()

	for {
		guestConn, err := ln.Accept()
		if err != nil {
			if ctx.Err() != nil {
				return nil
			}
			return fmt.Errorf("control: egress accept: %w", err)
		}
		go tunnelToSidecar(logger, guestConn, sidecarAddr)
	}
}

// tunnelToSidecar dials the sidecar and copies bytes both ways until either side
// closes; closing both conns then unblocks the other copy.
func tunnelToSidecar(logger *slog.Logger, guestConn net.Conn, sidecarAddr string) {
	defer guestConn.Close()
	up, err := net.Dial("tcp", sidecarAddr)
	if err != nil {
		logger.Warn("control: egress dial sidecar", "addr", sidecarAddr, "err", err)
		return
	}
	defer up.Close()

	done := make(chan struct{}, 2)
	go func() { _, _ = io.Copy(up, guestConn); done <- struct{}{} }()
	go func() { _, _ = io.Copy(guestConn, up); done <- struct{}{} }()
	<-done
}
