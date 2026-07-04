// Command fc-invoke is the host daemon that dispatches HTTP invocations to a
// pool of single-use Firecracker microVMs, multiplexed across named workloads.
// It exposes POST /invoke/{workload}[/{session}] and GET /healthz: each request
// claims (or restores from a warm base) a guest for the named workload, POSTs
// the body to the guest shim over vsock, proxies the guest response back, and
// discards the guest. One Invoker is built per configured workload, each with
// its own Firecracker driver (its own base rootfs and sizing) but sharing the
// HTTP ingress and vsock transport.
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

	"github.com/jomcgi/homelab/projects/firecracker/substrate/cluster/auth"
	"github.com/jomcgi/homelab/projects/firecracker/substrate/cluster/catalog"
	"github.com/jomcgi/homelab/projects/firecracker/substrate/cluster/ingress"
	"github.com/jomcgi/homelab/projects/firecracker/substrate/node/fcvm/driver"
	"github.com/jomcgi/homelab/projects/firecracker/substrate/node/invoker"
	"github.com/jomcgi/homelab/projects/firecracker/substrate/node/vsockhttp"
	"github.com/jomcgi/homelab/projects/firecracker/substrate/substrate"
)

func main() {
	// Handle the "__fcmount" re-exec FIRST: when launching a microVM with
	// per-instance vsock isolation the daemon re-execs itself in a fresh mount
	// namespace, and this call bind-mounts the bundle dir then execs firecracker
	// (never returning). It is a no-op for a normal daemon start, so it must run
	// before any other startup work.
	driver.ExecMountTrampoline()

	logger := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: slog.LevelInfo}))
	slog.SetDefault(logger)

	if err := run(logger); err != nil {
		logger.Error("fc-invoke exited with error", "err", err)
		os.Exit(1)
	}
}

func run(logger *slog.Logger) error {
	// Cancel on SIGINT/SIGTERM so the HTTP server drains gracefully.
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	cfg, err := catalog.Load()
	if err != nil {
		return err
	}
	logger.Info(
		"fc-invoke starting",
		"listen", cfg.ListenAddr,
		"node", cfg.Node,
		"arch", cfg.Arch,
		"workloads", len(cfg.Workloads),
	)

	self, err := os.Executable()
	if err != nil {
		return fmt.Errorf("resolve executable: %w", err)
	}

	// One transport is shared across every workload: it is stateless and opens a
	// fresh vsock connection per RoundTrip (keep-alives disabled), so no two
	// microVMs ever share a connection.
	transport := vsockhttp.NewTransport()

	// Build one Invoker per workload. Each gets its own Firecracker driver
	// because workloads differ in base rootfs, vCPUs, and memory; the drivers
	// share the daemon's kernel, firecracker binary, snapshot root, and vsock
	// isolation directory.
	invokers := make(map[string]substrate.NodeExecutor, len(cfg.Workloads))
	for name, wl := range cfg.Workloads {
		// Per-workload PID-1 path: guest images install their init at different
		// paths (e.g. semgrep-guest-init vs the agent's fc-agent-init), so the
		// kernel init= boot arg must be per-workload; fall back to the global.
		harnessInit := wl.HarnessInit
		if harnessInit == "" {
			harnessInit = cfg.HarnessInit
		}
		// The driver boots the workload's base rootfs READ-ONLY: every mutable
		// guest path is a tmpfs (RAM, captured in the snapshot memfile), so one
		// read-only rootfs file backs every microVM with no per-request copy.
		// CanonicalVsockDir plus the launcher's VsockBindTarget give each microVM
		// its own vsock socket at the snapshot's single embedded path (per-instance
		// mount namespace). OOMScoreAdj makes a guest, never the daemon, the first
		// OOM victim under memory pressure.
		d := driver.New(driver.Config{
			KernelImagePath:   cfg.KernelImagePath,
			KernelBootArgs:    cfg.KernelBootArgs,
			RootfsPath:        wl.RootfsPath,
			RootfsReadOnly:    true,
			CanonicalVsockDir: cfg.CanonicalVsockDir,
			HarnessInit:       harnessInit,
			VCPUs:             wl.VCPUs,
			MemMib:            wl.MemMib,
			SnapshotRoot:      cfg.SnapshotRoot,
			Node:              cfg.Node,
			Arch:              cfg.Arch,
		}, &driver.ExecLauncher{
			Bin:             cfg.BinPath,
			OOMScoreAdj:     cfg.GuestOomScoreAdj,
			VsockBindTarget: cfg.CanonicalVsockDir,
			Self:            self,
		}, nil)

		inv := invoker.New(d, transport, invoker.Config{
			Workload:         wl,
			BaseKey:          name,
			Arch:             cfg.Arch,
			BootReadyTimeout: cfg.BootReadyTimeout,
			SidecarAddr:      cfg.EgressSidecarAddr,
		}, logger)
		invokers[name] = inv

		// Build the warm base in the background so the daemon serves immediately;
		// invocations cold-boot (the fallback) until the base is ready, then
		// restore. A build failure is non-fatal: the invoker simply keeps
		// cold-booting until a later request-driven rebuild succeeds.
		if wl.WarmBase {
			// Pass name/inv/timeout as args so the goroutine does not capture the
			// range variables (nogo loopclosure).
			go func(name string, inv *invoker.Invoker, budget time.Duration) {
				bctx, cancel := context.WithTimeout(ctx, budget)
				defer cancel()
				if err := inv.BuildBase(bctx); err != nil {
					logger.Warn("initial warm base build failed; invocations will cold-boot until it succeeds",
						"workload", name, "err", err)
				}
			}(name, inv, cfg.BootReadyTimeout+wl.RequestTimeout)
		}
	}

	// Caller authentication (STPA: unauthenticated /invoke). When
	// FC_INVOKE_ALLOWED_CALLERS is set, every /invoke request must present a
	// ServiceAccount bearer token that the Kubernetes TokenReview API resolves to
	// an allow-listed identity; /healthz stays open for kubelet probes. When it is
	// unset the daemon runs open and says so loudly, so a missing config is never
	// a silent hole. This is the portable, substrate-independent control; the
	// homelab layers a Linkerd AuthorizationPolicy on top as defence-in-depth.
	var handler http.Handler = ingress.New(invokers, logger)
	if len(cfg.AllowedCallers) > 0 {
		reviewer, err := auth.NewClusterReviewer()
		if err != nil {
			return fmt.Errorf("build token reviewer: %w", err)
		}
		handler = auth.Middleware(handler, reviewer, cfg.AllowedCallers, logger)
		logger.Info("caller authentication enabled", "allowedCallers", cfg.AllowedCallers)
	} else {
		logger.Warn("caller authentication DISABLED: FC_INVOKE_ALLOWED_CALLERS is unset, so /invoke is open to any in-cluster client")
	}

	srv := &http.Server{
		Addr:    cfg.ListenAddr,
		Handler: handler,
		// Bound so a slow client sending headers cannot pin the ingress.
		ReadHeaderTimeout: 10 * time.Second,
	}

	// Serve until a signal cancels ctx, then drain in-flight invocations.
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
		// Stop accepting new invocations and wait for in-flight ones to finish.
		// Each /invoke is a synchronous request that holds its guest until the
		// response is proxied back, so Shutdown draining handlers == draining
		// running tasks. The budget covers the longest workload so a rollout
		// never drops a task; the pod's grace period is set above it.
		logger.Info("shutdown signal received; draining in-flight invocations", "budget", cfg.DrainTimeout)
		shutdownCtx, cancel := context.WithTimeout(context.Background(), cfg.DrainTimeout)
		defer cancel()
		return srv.Shutdown(shutdownCtx)
	}
}
