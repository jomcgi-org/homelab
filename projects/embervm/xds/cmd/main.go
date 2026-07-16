// Command embervm-xds is the EmberVM xDS sidecar: a small go-control-plane ADS
// server that translates the control plane's desired-state pushes into the
// CDS/RDS/EDS resources the per-node serving Envoys consume (R3, PR-3). It runs as
// a SECOND container in the control-plane pod and holds NO durable state and makes
// NO decisions: the Elixir control plane (PR-4) decides endpoint facts and PUTs a
// full desired-state document over localhost; this process renders and serves it.
//
// Two listeners:
//   - a gRPC ADS server on EMBERVM_XDS_GRPC_PORT (pod network, reached by node
//     Envoys via the embervm Service): serves the SnapshotCache.
//   - an HTTP snapshot API bound to 127.0.0.1:EMBERVM_XDS_HTTP_PORT: the control-
//     plane container's private write channel. Never exposed on the pod network.
//
// Empty cache at boot: nothing is served until the first PUT. If this container is
// down, node Envoys keep their last-ACKed config (xDS eventually consistent).
package main

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"syscall"
	"time"

	"google.golang.org/grpc"

	"github.com/jomcgi/homelab/projects/embervm/xds/server"
)

func main() {
	logger := slog.New(slog.NewJSONHandler(os.Stdout, nil))

	grpcPort := envInt("EMBERVM_XDS_GRPC_PORT", 18000)
	httpPort := envInt("EMBERVM_XDS_HTTP_PORT", 18001)

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	store := server.NewStore()

	// ADS gRPC server on the pod network. Node Envoys dial this via the embervm
	// Service's xDS port.
	grpcListener, err := net.Listen("tcp", fmt.Sprintf(":%d", grpcPort))
	if err != nil {
		logger.Error("listen ads grpc", "port", grpcPort, "error", err)
		os.Exit(1)
	}
	grpcServer := grpc.NewServer()
	server.RegisterADS(ctx, grpcServer, store)

	// Snapshot API bound to loopback ONLY: the control-plane container writes here
	// over localhost; it must never be reachable from the pod network or Service.
	httpServer := &http.Server{
		Addr:              fmt.Sprintf("127.0.0.1:%d", httpPort),
		Handler:           server.NewHTTPHandler(store),
		ReadHeaderTimeout: 5 * time.Second,
	}

	errCh := make(chan error, 2)
	go func() {
		logger.Info("ads grpc server listening", "port", grpcPort)
		if serveErr := grpcServer.Serve(grpcListener); serveErr != nil {
			errCh <- fmt.Errorf("ads grpc serve: %w", serveErr)
		}
	}()
	go func() {
		logger.Info("snapshot api listening", "addr", httpServer.Addr)
		if serveErr := httpServer.ListenAndServe(); serveErr != nil && !errors.Is(serveErr, http.ErrServerClosed) {
			errCh <- fmt.Errorf("snapshot api serve: %w", serveErr)
		}
	}()

	select {
	case <-ctx.Done():
		logger.Info("shutdown signal received")
	case serveErr := <-errCh:
		logger.Error("server error", "error", serveErr)
	}

	shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if shutdownErr := httpServer.Shutdown(shutdownCtx); shutdownErr != nil {
		logger.Warn("snapshot api shutdown", "error", shutdownErr)
	}
	grpcServer.GracefulStop()
}

// envInt reads a positive integer env var, falling back to def when unset or
// unparseable. Ports are the only config the sidecar takes; everything else
// arrives as a runtime PUT.
func envInt(key string, def int) int {
	v := os.Getenv(key)
	if v == "" {
		return def
	}
	n, err := strconv.Atoi(v)
	if err != nil || n <= 0 {
		return def
	}
	return n
}
