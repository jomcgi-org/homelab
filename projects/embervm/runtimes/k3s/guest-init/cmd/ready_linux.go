//go:build linux

package main

import (
	"context"
	"log/slog"

	"github.com/jomcgi/homelab/projects/firecracker/substrate/shim"
	"github.com/jomcgi/homelab/projects/firecracker/substrate/vsockproto"
)

// startVsockReadyServer binds the frozen guest-contract readiness listener on
// GuestHTTPPort (1027) and serves the shim ready contract in a goroutine, so
// noded's BuildBase WaitReady can health-gate the base build (GET /shim/ready ->
// 200 once ready). Off Linux (or where vsock is unavailable) it degrades to a
// closed channel so the host build still runs. Mirrors the scratch-postgres
// guest-init's startVsockReadyServer exactly (same frozen contract, same port).
//
// This is DISTINCT from the guest control agent (guestagent, port 1024, the
// clock-resync lane): this is the readiness seam on the frozen guest-contract
// HTTP-over-vsock port that BuildBase gates on, the clock agent is a separate
// raw-frame lane.
func startVsockReadyServer(ctx context.Context, logger *slog.Logger, ready func() bool) <-chan error {
	serveErr := make(chan error, 1)
	ln, err := listenVsock(vsockproto.GuestHTTPPort)
	if err != nil {
		logger.Warn("ember-k3s-init: vsock listen unavailable; no ready server", "err", err)
		close(serveErr)
		return serveErr
	}
	logger.Info("ember-k3s-init: vsock ready server listening", "port", vsockproto.GuestHTTPPort)
	// The k3s guest has no /invoke surface (it is opaque L4 k8s over the tap NIC):
	// the handler 404s any invoke. Only /shim/healthz and /shim/ready are
	// load-bearing (the base-build gate).
	srv := shim.NewServer(
		func(_ context.Context, _ *shim.Request) (*shim.Response, error) {
			return &shim.Response{Status: 404, Body: []byte("k3s guest has no invoke surface")}, nil
		},
		shim.WithReady(ready),
	)
	go func() { serveErr <- srv.Serve(ln) }()
	go func() {
		<-ctx.Done()
		_ = srv.Close()
	}()
	return serveErr
}

// waitForShutdown blocks until the context is cancelled (SIGTERM) or the vsock
// server errors, returning nil on a clean shutdown so run's error contract
// holds. Used by the base-build path, which holds PID 1 after reporting ready.
func waitForShutdown(ctx context.Context, serveErr <-chan error, logger *slog.Logger) error {
	select {
	case <-ctx.Done():
		logger.Info("ember-k3s-init: shutdown signal")
		return nil
	case err := <-serveErr:
		if err != nil {
			logger.Warn("ember-k3s-init: vsock ready server stopped", "err", err)
		}
		return nil
	}
}
