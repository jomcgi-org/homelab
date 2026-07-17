// Command ember-postgres-init is the PID 1 of the R4 scratch-postgres stateful
// microVM (ADR embervm/001, D-R4.PR-11.1). A raw Firecracker boot ignores the
// OCI image config entirely and boots init=<HarnessInit> (see the noded
// driver's bootArgs), so the image entrypoint is never honoured. This init is
// that missing PID 1. It has to satisfy TWO distinct boot classes off ONE
// rootfs:
//
//   - BASE BUILD (a plain cold boot with NO volume/mmds boot-args): the noded
//     BuildBase RPC cold-boots the guest, health-gates it over vsock at
//     readyPath (/shim/ready), snapshots the warm base, and discards the VM.
//     Here there is no volume and no Postgres to run: this init just answers the
//     vsock ready contract (200 immediately) so the base snapshot is taken. The
//     warm base is only the OS; Postgres first runs on the stateful cold boot.
//
//   - STATEFUL FRESH/COLD (a cold boot carrying ember.volume_dev / ember.env.*):
//     guest-init mounts the writable volume at ember.volume_mount, decodes the
//     mmds_env secrets (POSTGRES_PASSWORD) into the process env, then runs the
//     Postgres bootstrap: initdb-if-PGDATA-empty (scram auth, listen on *, the
//     `scratch` database) then exec `postgres -D $PGDATA` on TCP 5432. On a
//     non-empty PGDATA (a later cold boot against an already-initialized volume)
//     it SKIPS initdb and just launches Postgres; WAL recovery runs
//     automatically. Runtime health is a TCP connect to 5432 over the tap NIC
//     (noded's finishStatefulStart), NOT the vsock ready path; the vsock server
//     stays up harmlessly and reports ready once Postgres accepts locally.
//
// A RELIGHT never re-runs kernel init (it resumes the running snapshot), so this
// init and its Postgres launch happen exactly once per volume generation; a
// relight resumes an already-live Postgres.
package main

import (
	"context"
	"log/slog"
	"os"
	"os/signal"
	"sync/atomic"
	"syscall"

	"github.com/jomcgi/homelab/projects/firecracker/substrate/shim"
	"github.com/jomcgi/homelab/projects/firecracker/substrate/vsockproto"
)

func main() {
	logger := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: slog.LevelInfo}))
	slog.SetDefault(logger)
	if err := run(logger); err != nil {
		logger.Error("ember-postgres-init exited with error", "err", err)
		os.Exit(1)
	}
}

func run(logger *slog.Logger) error {
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	// Writable /tmp on the read-only rootfs (initdb + Postgres write lock/socket
	// files under $PGDATA on the VOLUME, but any transient tooling under /tmp
	// needs a writable dir), plus /proc so the boot-arg readers below can read
	// /proc/cmdline.
	mountTmpfsTmp(logger)
	mountProc(logger)

	// A raw FC boot hands PID 1 no environment. Set PATH + Postgres defaults so
	// initdb/postgres and psql resolve, matching the apko image `environment`
	// block (which a Firecracker boot never consumes).
	setDefaultEnv(logger)

	// Stateful first-boot secrets (R4, D-R4.PR-7.1: MMDS-lite over boot-args):
	// decode every ember.env.<KEY>=<base64url> token into the process env BEFORE
	// the Postgres bootstrap reads $POSTGRES_PASSWORD. A base build carries none,
	// so this is a no-op there.
	setMmdsEnv(logger)

	// Detect the boot class from the presence of the volume boot-arg. A base
	// build has none (plain cold boot) and only needs the vsock ready answer; a
	// stateful cold boot mounts the volume and launches Postgres.
	dev, mountPath := statefulVolumeFromCmdline(logger)

	// ready gates the vsock /shim/ready probe. For a base build it flips true
	// immediately (the warm OS is the base). For a stateful boot it flips true
	// once Postgres accepts a local TCP connection, so a base build never
	// snapshots a half-initialized datastore (there is no datastore in a base
	// build) and a stray vsock probe on a stateful boot reflects real readiness.
	var ready atomic.Bool

	// Bring up the vsock ready server first (non-fatal off Linux / no vsock), so
	// the base build's WaitReady can always reach it. It shares the frozen shim
	// contract: GET /shim/healthz 200 always, GET /shim/ready 200 once ready.
	serveErr := startVsockReadyServer(ctx, logger, ready.Load)

	if dev == "" {
		// Base build: no volume, no Postgres. Answer ready and hold PID 1 until
		// the host snapshots and reaps the VM. Do NOT exit: exiting PID 1 panics
		// the guest kernel before noded can snapshot.
		ready.Store(true)
		logger.Info("ember-postgres-init: base build boot, ready (no volume, no postgres)")
		return waitForShutdown(ctx, serveErr, logger)
	}

	// Stateful cold boot: mount the volume, then bootstrap + launch Postgres.
	if err := mountStatefulVolume(logger, dev, mountPath); err != nil {
		return err
	}

	if err := bootstrapAndLaunchPostgres(ctx, logger, mountPath, &ready); err != nil {
		return err
	}

	logger.Info("ember-postgres-init: postgres exited")
	return waitForShutdown(ctx, serveErr, logger)
}

// waitForShutdown blocks until the context is cancelled (SIGTERM) or the vsock
// server errors, returning nil on a clean shutdown so run's error contract holds.
func waitForShutdown(ctx context.Context, serveErr <-chan error, logger *slog.Logger) error {
	select {
	case <-ctx.Done():
		logger.Info("ember-postgres-init: shutdown signal")
		return nil
	case err := <-serveErr:
		// http.ErrServerClosed is a clean stop; anything else is worth logging but
		// not worth crashing PID 1 (which would panic the kernel).
		if err != nil {
			logger.Warn("ember-postgres-init: vsock ready server stopped", "err", err)
		}
		return nil
	}
}

// startVsockReadyServer binds the vsock shim listener on GuestHTTPPort and serves
// the frozen ready contract in a goroutine. Off Linux (or where vsock is
// unavailable) it degrades to a closed channel, so the host build still runs.
func startVsockReadyServer(ctx context.Context, logger *slog.Logger, ready func() bool) <-chan error {
	serveErr := make(chan error, 1)
	ln, err := listenVsock(vsockproto.GuestHTTPPort)
	if err != nil {
		logger.Warn("ember-postgres-init: vsock listen unavailable; no ready server", "err", err)
		close(serveErr)
		return serveErr
	}
	logger.Info("ember-postgres-init: vsock ready server listening", "port", vsockproto.GuestHTTPPort)
	// The stateful guest has no /invoke surface (it is opaque L4 Postgres): the
	// handler 404s any invoke. Only /shim/healthz and /shim/ready are load-bearing.
	srv := shim.NewServer(
		func(_ context.Context, _ *shim.Request) (*shim.Response, error) {
			return &shim.Response{Status: 404, Body: []byte("stateful postgres guest has no invoke surface")}, nil
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
