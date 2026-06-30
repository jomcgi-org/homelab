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
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/jomcgi/homelab/projects/agent_platform/semgrep-scand/internal/config"
	"github.com/jomcgi/homelab/projects/agent_platform/semgrep-scand/internal/scanner"
	"github.com/jomcgi/homelab/projects/agent_platform/semgrep-scand/internal/server"
	"github.com/jomcgi/homelab/projects/firecracker/substrate/fcvm/driver"
)

func main() {
	// Handle the "__fcmount" re-exec FIRST: when launching a microVM with per-instance
	// vsock isolation the daemon re-execs itself in a fresh mount namespace, and this
	// call bind-mounts the bundle dir then execs firecracker (never returning). It is
	// a no-op for a normal daemon start.
	driver.ExecMountTrampoline()

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
		"warm_base", cfg.WarmBase,
		"base_rootfs", cfg.BaseRootfsPath != "",
	)

	self, err := os.Executable()
	if err != nil {
		return fmt.Errorf("resolve executable: %w", err)
	}

	// The Firecracker driver boots the shared semgrep-guest base rootfs READ-ONLY:
	// every mutable path in the guest is a tmpfs (RAM, captured in the snapshot
	// memfile), so one read-only rootfs file backs every microVM with no per-thread
	// copy. CanonicalVsockDir + the launcher's VsockBindTarget give each microVM its
	// own vsock socket at the snapshot's single embedded path (per-instance mount
	// namespace). OOMScoreAdj makes a guest, never the daemon, the first OOM victim.
	fcDriver := driver.New(driver.Config{
		KernelImagePath:   cfg.KernelImagePath,
		KernelBootArgs:    cfg.KernelBootArgs,
		RootfsPath:        cfg.BaseRootfsPath,
		RootfsReadOnly:    true,
		CanonicalVsockDir: cfg.CanonicalVsockDir,
		HarnessInit:       cfg.HarnessInit,
		VCPUs:             cfg.GuestVCPUs,
		MemMib:            cfg.GuestMemMib,
		SnapshotRoot:      cfg.NvmeRoot,
		Node:              cfg.Node,
		Arch:              cfg.Arch,
	}, &driver.ExecLauncher{
		Bin:             cfg.BinPath,
		OOMScoreAdj:     cfg.GuestOomScoreAdj,
		VsockBindTarget: cfg.CanonicalVsockDir,
		Self:            self,
	}, nil)

	scn := scanner.New(fcDriver, scanner.NewVsockTransport(), scanner.Config{
		MaxConcurrent:    cfg.MaxConcurrent,
		Arch:             cfg.Arch,
		BootReadyTimeout: cfg.BootReadyTimeout,
		ScanTimeout:      cfg.ScanTimeout,
		WarmBase:         cfg.WarmBase,
		BaseKey:          cfg.BaseKey,
		RestorePrime:     cfg.RestorePrime,
	}, logger)

	// Build the warm base in the background so the daemon serves immediately; scans
	// cold-boot (the fallback) until the base is ready (~one warm-up), then restore.
	if cfg.WarmBase {
		go func() {
			bctx, cancel := context.WithTimeout(ctx, cfg.BootReadyTimeout+cfg.ScanTimeout)
			defer cancel()
			if err := scn.BuildBase(bctx); err != nil {
				logger.Warn("initial warm base build failed; scans will cold-boot until it succeeds", "err", err)
			}
		}()
	}

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
