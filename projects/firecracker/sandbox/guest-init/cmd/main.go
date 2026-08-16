// Command sandbox-guest-init is the PID 1 of a zero-egress language sandbox
// microVM (ADR agents/044). It mounts a tmpfs over /tmp (the rootfs is
// read-only and shared across every microVM restored from the warm-base
// snapshot), warms the selected language runtime before any real request
// lands, then serves the fc-invoke shim protocol: an HTTP server over AF_VSOCK
// on vsockproto.GuestHTTPPort. Each /invoke request runs one snippet and
// returns its stdout, stderr, exit code, and any files it created.
package main

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"os/exec"
	"os/signal"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/jomcgi/homelab/projects/firecracker/sandbox/guest-init/internal/handler"
	"github.com/jomcgi/homelab/projects/firecracker/substrate/shim"
	"github.com/jomcgi/homelab/projects/firecracker/substrate/vsockproto"
)

func main() {
	logger := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: slog.LevelInfo}))
	slog.SetDefault(logger)
	if err := run(logger); err != nil {
		logger.Error("sandbox-guest-init exited with error", "err", err)
		os.Exit(1)
	}
}

func run(logger *slog.Logger) error {
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	// tmpfs over /tmp keeps every per-invoke workdir (handler.Handle's
	// os.MkdirTemp) in RAM, so the rootfs stays read-only and shareable
	// across disposable microVMs restored from the one warm-base snapshot.
	mountTmpfsTmp(logger)

	// Before ANY exec, including the warm-up below. Firecracker hands PID 1 no
	// environment, and exec.Command resolves argv[0] against this process's
	// PATH rather than the one in cmd.Env, so without this every language
	// reports "executable file not found in $PATH" and the warm-up fails
	// silently because a failed warm-up is deliberately non-fatal.
	if err := handler.EnsureSearchPath(); err != nil {
		return fmt.Errorf("ensure sandbox search path: %w", err)
	}

	spec, err := handler.SelectSpec()
	if err != nil {
		return fmt.Errorf("select sandbox language: %w", err)
	}
	logger.Info("sandbox language selected", "language", spec.Name)

	prepareWarmCacheDirs(logger, spec)
	warmup(logger, spec)

	// The warm-base snapshot (ADR 022) is taken once /shim/ready first
	// returns 200, so flipping this only after warmup captures the populated
	// runtime cache and resident pages in every restored guest.
	var ready atomic.Bool
	ready.Store(true)

	ln, err := listenVsock(vsockproto.GuestHTTPPort)
	if err != nil {
		return err // nosemgrep: no-bare-error-return
	}
	logger.Info("shim HTTP server listening", "port", vsockproto.GuestHTTPPort)

	// WithClock installs POST /shim/clock so the node can resync this guest's
	// wall clock after restoring it. Best-effort: a
	// set failure is a 500 the node logs and moves past, never a relight
	// failure.
	srv := shim.NewServer(
		handler.New(spec),
		shim.WithReady(ready.Load),
		shim.WithClock(func(epochMs int64) error {
			if err := setWallClock(epochMs); err != nil {
				logger.Warn("guest clock resync failed", "epoch_ms", epochMs, "err", err)
				return err // nosemgrep: no-bare-error-return
			}
			logger.Info("guest clock resynced", "epoch_ms", epochMs)
			return nil
		}),
	)

	// Serve in a goroutine so ctx cancellation (SIGTERM) can close the server
	// gracefully rather than blocking indefinitely on Accept.
	serveErr := make(chan error, 1)
	go func() { serveErr <- srv.Serve(ln) }()

	select {
	case <-ctx.Done():
		_ = srv.Close()
		<-serveErr
		return nil
	case err := <-serveErr:
		return err // nosemgrep: no-bare-error-return
	}
}

// prepareWarmCacheDirs creates shared caches on the writable /tmp tmpfs. The
// warm-up runs as root while request processes run as uid 65532, so every
// cache must remain world-writable.
func prepareWarmCacheDirs(logger *slog.Logger, spec handler.Spec) {
	for _, dir := range spec.CacheDirs {
		if err := os.MkdirAll(dir, 0o777); err != nil {
			logger.Warn("could not create warm cache directory", "path", dir, "err", err)
			continue
		}
		if err := os.Chmod(dir, 0o777); err != nil {
			logger.Warn("could not make warm cache directory world-writable", "path", dir, "err", err)
		}
	}
}

// warmup runs the selected language's best-effort warm command before
// readiness flips. A failed warm-up never prevents the guest from serving.
func warmup(logger *slog.Logger, spec handler.Spec) {
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	if len(spec.Warm) == 0 {
		return
	}
	cmd := exec.CommandContext(ctx, spec.Warm[0], spec.Warm[1:]...)
	cmd.Dir = "/tmp"
	cmd.Env = spec.Environment("/tmp")
	if out, err := cmd.CombinedOutput(); err != nil {
		// Error, not Warn: readiness still flips and the warm base still
		// snapshots, so this log line is the ONLY signal that a guest cannot
		// run its own toolchain. It was a Warn while all six languages failed.
		logger.Error("language warm-up failed; guest still serves cold", "language", spec.Name, "err", err, "out", string(out))
		return
	}
	logger.Info("language warm-up done", "language", spec.Name)
}
