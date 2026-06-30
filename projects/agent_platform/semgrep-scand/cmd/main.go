// Command semgrep-scand is the host daemon that scans batches of in-memory files
// by booting a fresh, offline semgrep-guest microVM per request. It exposes an
// HTTP API (POST /scan, GET /healthz); each scan claims a guest via the shared
// Firecracker driver, waits for the guest to warm and announce readiness over the
// control vsock, runs the scan over the guest's scan channel, and discards the
// guest. A weighted semaphore caps how many guests are live at once.
package main

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/jomcgi/homelab/projects/agent_platform/fcvm/driver"
	"github.com/jomcgi/homelab/projects/agent_platform/semgrep-scand/internal/config"
	"github.com/jomcgi/homelab/projects/agent_platform/semgrep-scand/internal/scanner"
	"github.com/jomcgi/homelab/projects/agent_platform/semgrep-scand/internal/server"
)

func main() {
	logger := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: slog.LevelInfo}))
	slog.SetDefault(logger)

	if err := run(logger); err != nil {
		logger.Error("semgrep-scand exited with error", "err", err)
		os.Exit(1)
	}
}

func run(logger *slog.Logger) error {
	// Cancel on SIGINT/SIGTERM so the HTTP server drains gracefully.
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	cfg, err := config.Load()
	if err != nil {
		return err
	}
	logger.Info(
		"semgrep-scand starting",
		"listen", cfg.ListenAddr,
		"node", cfg.Node,
		"arch", cfg.Arch,
		"max_concurrent", cfg.MaxConcurrent,
		"base_rootfs", cfg.BaseRootfsPath != "",
	)

	// The Firecracker driver: cold-boot the semgrep-guest base rootfs per scan.
	// OOMScoreAdj on the launcher makes the guest, never this daemon, the kernel's
	// first OOM victim under memory pressure.
	fcDriver := driver.New(driver.Config{
		KernelImagePath: cfg.KernelImagePath,
		KernelBootArgs:  cfg.KernelBootArgs,
		BaseRootfsPath:  cfg.BaseRootfsPath,
		Provisioner:     cfg.Provisioner,
		ThinPool:        cfg.ThinPool,
		HarnessInit:     cfg.HarnessInit,
		VCPUs:           cfg.GuestVCPUs,
		MemMib:          cfg.GuestMemMib,
		SnapshotRoot:    cfg.NvmeRoot,
		Node:            cfg.Node,
		Arch:            cfg.Arch,
	}, &driver.ExecLauncher{Bin: cfg.BinPath, OOMScoreAdj: cfg.GuestOomScoreAdj}, nil)

	scn := scanner.New(fcDriver, scanner.NewVsockTransport(), scanner.Config{
		MaxConcurrent:    cfg.MaxConcurrent,
		Arch:             cfg.Arch,
		BootReadyTimeout: cfg.BootReadyTimeout,
		ScanTimeout:      cfg.ScanTimeout,
	}, logger)

	srv := &http.Server{
		Addr:    cfg.ListenAddr,
		Handler: server.New(scn, logger),
		// Bound by the scan + boot budget plus slack, so a stuck client cannot pin
		// a guest slot forever.
		ReadHeaderTimeout: 10 * time.Second,
	}

	// Serve until a signal cancels ctx, then drain in-flight scans gracefully.
	errCh := make(chan error, 1)
	go func() {
		logger.Info("http server listening", "addr", cfg.ListenAddr)
		errCh <- srv.ListenAndServe()
	}()

	select {
	case err := <-errCh:
		if errors.Is(err, http.ErrServerClosed) {
			return nil
		}
		return err
	case <-ctx.Done():
		logger.Info("shutdown signal received; draining")
		shutdownCtx, cancel := context.WithTimeout(context.Background(), cfg.ScanTimeout+cfg.BootReadyTimeout)
		defer cancel()
		return srv.Shutdown(shutdownCtx)
	}
}
