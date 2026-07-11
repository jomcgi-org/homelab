// Command semgrep-guest-init is the PID 1 of the semgrep scanner microVM. It
// keeps a warm offline-Pro semgrep scan-server (osemgrep-pro, parsers warmed and
// rules compiled once) and serves scan requests over the fc-invoke shim
// protocol: an HTTP server over AF_VSOCK on vsockproto.GuestHTTPPort. Readiness
// is signalled via GET /shim/ready (200 once the scan-server has printed
// {"ready":true}, 503 before) so fc-invoke can poll and the host snapshots the
// VM only after warmup.
//
// The engine runs fully offline. A placeholder-token settings file (written by
// guestboot.SetupEnv) satisfies the Pro entitlement check without any network
// call. No SEMGREP_APP_TOKEN is set. The shared PID-1 boot steps (loopback,
// tmpfs, env, settings) live in internal/guestboot so the full-scan init reuses
// them; this init differs only in serving the warm scan-server.
package main

import (
	"context"
	"log/slog"
	"os"
	"os/signal"
	"sync/atomic"
	"syscall"

	"github.com/jomcgi/homelab/projects/firecracker/semgrep/guest-init/internal/guestboot"
	"github.com/jomcgi/homelab/projects/firecracker/semgrep/guest-init/internal/handler"
	"github.com/jomcgi/homelab/projects/firecracker/semgrep/guest-init/internal/scandriver"
	"github.com/jomcgi/homelab/projects/firecracker/semgrep/guest-init/internal/scanserver"
	"github.com/jomcgi/homelab/projects/firecracker/substrate/shim"
	"github.com/jomcgi/homelab/projects/firecracker/substrate/vsockproto"
)

func main() {
	logger := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: slog.LevelInfo}))
	slog.SetDefault(logger)
	if err := run(logger); err != nil {
		logger.Error("semgrep-guest-init exited with error", "err", err)
		os.Exit(1)
	}
}

func run(logger *slog.Logger) error {
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	// Raw FC boot leaves loopback DOWN; bring it up before anything binds 127.0.0.1.
	guestboot.BringUpLoopback(logger)

	// tmpfs over /tmp so all mutable guest state (the scan-server HOME and its
	// settings file) lives in RAM, keeping the rootfs read-only and shareable by
	// every microVM restored from one warm-base snapshot. 256m suffices for the
	// single-file scan-server (it holds no repo tree). Must precede SetupEnv so
	// the HOME lands on the tmpfs.
	guestboot.MountTmpfsTmp(logger, "256m")

	guestboot.SetProcessEnv()

	settingsFile, err := guestboot.SetupEnv(logger)
	if err != nil {
		return err
	}

	bin := guestboot.EnvOr("OSEMGREP_PRO_BIN", guestboot.DefaultOsemgrepPro)
	rulesDir := guestboot.EnvOr("SEMGREP_SCAN_RULES", guestboot.DefaultRulesDir)
	logger.Info("starting offline-Pro scan-server", "bin", bin, "rules", rulesDir)

	// Start blocks until the scan-server prints {"ready":true} (parsers warmed,
	// rules compiled) — the point at which the host may snapshot the VM. The
	// child's lifetime is bound to ctx: a SIGTERM cancels it and Close reaps it.
	driver, err := scandriver.Start(ctx, bin, rulesDir, settingsFile)
	if err != nil {
		return err
	}
	defer driver.Close()
	logger.Info("scan-server warm; ready to serve")

	// Flip readiness only after the scan-server is warm: /shim/ready returns 503
	// until this Store, 200 after, so fc-invoke will not route scan requests
	// before warmup and the host snapshots a warm VM.
	var ready atomic.Bool
	ready.Store(true)

	ln, err := scanserver.ListenVsock(vsockproto.GuestHTTPPort)
	if err != nil {
		return err
	}
	logger.Info("shim HTTP server listening", "port", vsockproto.GuestHTTPPort)

	h := handler.New(driver)
	srv := shim.NewServer(h, shim.WithReady(ready.Load))

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
		return err
	}
}
