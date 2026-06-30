// Command fc-agentd is the Firecracker snapshot/restore controller from ADR 022.
// It is a node-4 daemon that runs a Postgres-reconcile loop: read desired
// AgentThread state, drive Firecracker (boot/pause/snapshot/restore), write
// actual state back. Phase 0 is the skeleton: config, Postgres connection, a
// no-op reconcile loop, and SigNoz instrumentation. It must start and idle
// cleanly.
package main

import (
	"context"
	"log/slog"
	"os"
	"os/signal"
	"syscall"

	"github.com/jomcgi/homelab/projects/agent_platform/fc-agentd/internal/config"
	"github.com/jomcgi/homelab/projects/agent_platform/fc-agentd/internal/reconcile"
	"github.com/jomcgi/homelab/projects/agent_platform/fc-agentd/internal/store"
	"github.com/jomcgi/homelab/projects/agent_platform/fc-agentd/internal/telemetry"
	"github.com/jomcgi/homelab/projects/firecracker/substrate/fcvm/driver"
)

func main() {
	logger := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: slog.LevelInfo}))
	slog.SetDefault(logger)

	if err := run(logger); err != nil {
		logger.Error("fc-agentd exited with error", "err", err)
		os.Exit(1)
	}
}

func run(logger *slog.Logger) error {
	// Cancel on SIGINT/SIGTERM so the reconcile loop shuts down gracefully.
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	cfg, err := config.Load()
	if err != nil {
		return err
	}
	logger.Info(
		"fc-agentd starting",
		"node", cfg.Node,
		"arch", cfg.Arch,
		"snapshot_root", cfg.SnapshotRoot,
		"postgres", cfg.DatabaseURL != "",
	)

	tp, err := telemetry.Init(ctx)
	if err != nil {
		// Telemetry is best-effort; log and continue rather than fail to start.
		logger.Warn("telemetry init failed; continuing without tracing", "err", err)
	}
	defer func() {
		if shutdownErr := telemetry.Shutdown(context.Background(), tp); shutdownErr != nil {
			logger.Warn("telemetry shutdown failed", "err", shutdownErr)
		}
	}()

	fcDriver := driver.New(driver.Config{
		KernelImagePath: cfg.KernelImagePath,
		RootfsPath:      cfg.RootfsPath,
		BaseRootfsPath:  cfg.BaseRootfsPath,
		Provisioner:     cfg.RootfsProvisioner,
		ThinPool:        cfg.ThinPool,
		HarnessInit:     cfg.HarnessInit,
		VCPUs:           cfg.GuestVCPUs,
		MemMib:          cfg.GuestMemMib,
		SnapshotRoot:    cfg.SnapshotRoot,
		Node:            cfg.Node,
		Arch:            cfg.Arch,
	}, &driver.ExecLauncher{Bin: cfg.FirecrackerBin, OOMScoreAdj: cfg.GuestOOMScoreAdj}, nil)

	loop := &reconcile.Loop{
		Executor:         fcDriver,
		Reclaimer:        fcDriver,
		Node:             cfg.Node,
		Interval:         cfg.ReconcileInterval,
		Logger:           logger,
		ControlUDS:       fcDriver.VsockUDSPath,
		GooseEnv:         cfg.InjectedEnv,
		TierEnv:          cfg.TierEnv,
		EgressSidecar:    cfg.EgressSidecar,
		MaxConcurrent:    cfg.MaxConcurrent,
		MaxClaimAttempts: cfg.MaxClaimAttempts,
	}

	if cfg.DatabaseURL != "" {
		st, err := store.Open(ctx, cfg.DatabaseURL)
		if err != nil {
			return err
		}
		defer st.Close()
		loop.Registry = st
		logger.Info("connected to agent_threads registry")

		// Event-driven dispatch (ADR 026 Task 1.2): wake the reconcile loop the
		// instant a PENDING thread is submitted (Postgres LISTEN/NOTIFY), instead of
		// waiting out the 5s poll. The poll stays as the safety net, so this only
		// accelerates claims. Buffered depth 1 + non-blocking send coalesces a burst
		// of notifications into a single pending wake.
		wake := make(chan struct{}, 1)
		loop.Wake = wake
		go func() {
			notify := func() {
				select {
				case wake <- struct{}{}:
				default:
				}
			}
			if err := st.ListenPending(ctx, notify); err != nil && ctx.Err() == nil {
				logger.Warn("pending-thread listener exited; falling back to poll only", "err", err)
			}
		}()
		logger.Info("event-driven dispatch enabled (LISTEN agent_threads_pending)")
	} else {
		logger.Warn("DATABASE_URL not set; running reconcile loop in dry-run mode")
	}

	return loop.Run(ctx)
}
