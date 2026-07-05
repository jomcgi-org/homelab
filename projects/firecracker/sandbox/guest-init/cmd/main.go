// Command sandbox-guest-init is the PID 1 of the zero-egress python sandbox
// microVM (ADR agents/044). It mounts a tmpfs over /tmp (the rootfs is
// read-only and shared across every microVM restored from the warm-base
// snapshot), pre-imports the baked scientific python libraries once so their
// pages are resident in the snapshot memfile before any real request lands,
// then serves the fc-invoke shim protocol: an HTTP server over AF_VSOCK on
// vsockproto.GuestHTTPPort. Each /invoke request runs one Python snippet
// (internal/handler.Handle) and returns its stdout, stderr, exit code, and
// any files the snippet created.
package main

import (
	"context"
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

	// A raw Firecracker boot hands PID 1 no environment (the kernel ignores
	// the OCI image config), so PATH must be set explicitly before the
	// warmup exec below; this mirrors the PATH handler.Handle sets for every
	// request.
	_ = os.Setenv("PATH", "/usr/bin:/bin:/usr/local/bin")

	// matplotlib's config/cache dir, shared by the warm-import below and every
	// per-request exec (handler.MPLConfigDir). Created world-writable (0777)
	// because warm-import runs as root here but per-request python runs as the
	// dropped uid 65532; both must be able to (re)write the font cache. It
	// lives on the /tmp tmpfs so it is captured in the warm-base snapshot.
	_ = os.Setenv("MPLCONFIGDIR", handler.MPLConfigDir)
	if err := os.MkdirAll(handler.MPLConfigDir, 0o777); err != nil {
		logger.Warn("could not create MPLCONFIGDIR; matplotlib will fall back and may leak a font cache into workdirs", "err", err)
	} else {
		_ = os.Chmod(handler.MPLConfigDir, 0o777)
	}

	warmImports(logger)

	// The warm-base snapshot (ADR 022) is taken once /shim/ready first
	// returns 200, so flipping this only after warmImports is the mechanism
	// that captures the pre-imported library pages in every restored guest.
	var ready atomic.Bool
	ready.Store(true)

	ln, err := listenVsock(vsockproto.GuestHTTPPort)
	if err != nil {
		return err // nosemgrep: no-bare-error-return
	}
	logger.Info("shim HTTP server listening", "port", vsockproto.GuestHTTPPort)

	srv := shim.NewServer(handler.Handle, shim.WithReady(ready.Load))

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

// warmImports pre-imports the baked scientific python libraries once, before
// readiness flips, so the warm-base snapshot captures their page cache in
// RAM: every restored guest then starts a request already warm instead of
// paying a cold import on the first real invocation. numpy is not a pinned
// Wolfi package (see guest/apko.yaml) but IS importable: scipy and pandas
// pull in a compatible version transitively. Best-effort: a missing or
// broken import is logged, not fatal, so a library regression never bricks
// the guest, it only misses the warm-cache benefit.
func warmImports(logger *slog.Logger) {
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	// Beyond importing, render one throwaway figure so matplotlib builds its
	// font cache (fontlist.json) into MPLCONFIGDIR now, captured in the
	// warm-base snapshot. Without an actual draw the cache is not built at
	// import time, and the first real plot per guest would both scan fonts and
	// write the cache into the request workdir.
	cmd := exec.CommandContext(ctx, "python3", "-c",
		"import matplotlib; matplotlib.use('Agg'); "+
			"import numpy, pandas, scipy, PIL, yaml, dateutil; "+
			"import io, matplotlib.pyplot as plt; "+
			"plt.plot([0, 1], [0, 1]); plt.savefig(io.BytesIO(), format='png')")
	if out, err := cmd.CombinedOutput(); err != nil {
		logger.Warn("warm-import failed; guest still serves, just cold on the first request", "err", err, "out", string(out))
		return
	}
	logger.Info("warm-import done")
}
