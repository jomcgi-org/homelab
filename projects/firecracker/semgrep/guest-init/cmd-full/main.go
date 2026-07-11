// Command semgrep-full-guest-init is the PID 1 of the semgrep FULL-scan microVM
// (the fc-invoke "semgrep-full" workload). Unlike the warm scan-server init, it
// runs NO resident child: each request runs `semgrep scan --pro` as a subprocess
// over a materialized tree, so cross-file (interfile) dataflow fires. There is
// nothing to warm, so readiness is immediate and warmBase is off for this
// workload.
//
// It shares the PID-1 boot steps (loopback, tmpfs, env, offline Pro settings)
// with the warm init via internal/guestboot, differing only in a larger tmpfs
// (it materializes the whole repo tree) and in serving handler.NewFull.
package main

import (
	"context"
	"log/slog"
	"os"
	"os/signal"
	"syscall"

	"github.com/jomcgi/homelab/projects/firecracker/semgrep/guest-init/internal/fullscan"
	"github.com/jomcgi/homelab/projects/firecracker/semgrep/guest-init/internal/guestboot"
	"github.com/jomcgi/homelab/projects/firecracker/semgrep/guest-init/internal/handler"
	"github.com/jomcgi/homelab/projects/firecracker/semgrep/guest-init/internal/scanserver"
	"github.com/jomcgi/homelab/projects/firecracker/substrate/shim"
	"github.com/jomcgi/homelab/projects/firecracker/substrate/vsockproto"
)

func main() {
	logger := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: slog.LevelInfo}))
	slog.SetDefault(logger)
	if err := run(logger); err != nil {
		logger.Error("semgrep-full-guest-init exited with error", "err", err)
		os.Exit(1)
	}
}

func run(logger *slog.Logger) error {
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	guestboot.BringUpLoopback(logger)

	// A full scan materializes the whole repo tree (~750 files) into the tmpdir
	// plus whatever scratch semgrep writes under HOME, so this init asks for a
	// much larger tmpfs than the single-file warm path's 256m. tmpfs allocates on
	// write, so this is a ceiling within the guest's memMib, not a reservation.
	guestboot.MountTmpfsTmp(logger, "2g")

	guestboot.SetProcessEnv()

	// SetupEnv writes the offline Pro settings file and exports SEMGREP_SETTINGS_FILE
	// / HOME; `osemgrep-pro scan --pro` needs these for the offline Pro unlock
	// exactly as the warm scan-server does.
	if _, err := guestboot.SetupEnv(logger); err != nil {
		return err
	}

	// Reconstruct the pro-engine "install" layout (binary + version stamp) the
	// scan CLI insists on, in a tmpfs dir on PATH. Without this the CLI reports
	// "Semgrep Pro is either uninstalled or out of date" and exits 2. The returned
	// path is the pro binary to invoke the scan from (inside the shim dir).
	proBin, err := guestboot.SetupProEngine(logger)
	if err != nil {
		return err
	}

	rulesDir := guestboot.EnvOr("SEMGREP_SCAN_RULES", guestboot.DefaultRulesDir)
	logger.Info("starting full-scan (osemgrep-pro scan --pro) init", "rules", rulesDir, "proBin", proBin)

	h := handler.NewFull(func(req vsockproto.ScanRequest) (vsockproto.ScanResult, error) {
		return fullscan.Scan(ctx, req, fullscan.SemgrepRunner(rulesDir, proBin))
	})

	ln, err := scanserver.ListenVsock(vsockproto.GuestHTTPPort)
	if err != nil {
		return err
	}
	logger.Info("full-scan shim HTTP server listening", "port", vsockproto.GuestHTTPPort)

	// No warm child to wait on: ready immediately. The host does not snapshot this
	// workload (warmBase:false), so there is no warm-gate to satisfy.
	srv := shim.NewServer(h, shim.WithReady(func() bool { return true }))

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
