// Command embervm-noded is the EmberVM node daemon: the gRPC NodeService server
// that owns Firecracker microVM lifecycle on one node (node-4). It is the fork of
// fc-invoke's node layer (ADR embervm/001, fork-not-extend), reshaped behind the
// embervm.node.v1 contract. The control plane (Elixir) drives it: BuildBase turns
// a guest image into a base snapshot, Prime restores a pristine VM and parks it,
// Assign delivers one vsock HTTP task then destroys the VM, and WatchNode streams
// capacity facts. The daemon owns NO concurrency policy and NO durable state
// beyond node-local snapshot files; on start it reports existing bases so the
// control plane reconciles instead of rebuilding.
package main

import (
	"context"
	"errors"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"google.golang.org/grpc"

	nodev1 "github.com/jomcgi/homelab/projects/embervm/proto/embervm/node/v1"

	"github.com/jomcgi/homelab/projects/embervm/noded/config"
	"github.com/jomcgi/homelab/projects/embervm/noded/fcvm/driver"
	"github.com/jomcgi/homelab/projects/embervm/noded/server"
	"github.com/jomcgi/homelab/projects/embervm/noded/vsockhttp"
)

func main() {
	// Handle the "__fcmount" re-exec FIRST: launching a microVM with per-instance
	// vsock isolation re-execs this binary in a fresh mount namespace, bind-mounts
	// the bundle dir, then execs firecracker (never returning). It is a no-op for a
	// normal daemon start, so it must run before any other startup work.
	driver.ExecMountTrampoline()

	logger := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: slog.LevelInfo}))
	slog.SetDefault(logger)

	if err := run(logger); err != nil {
		logger.Error("embervm-noded exited with error", "err", err)
		os.Exit(1)
	}
}

func run(logger *slog.Logger) error {
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	cfg, err := config.Load()
	if err != nil {
		return err
	}
	logger.Info("embervm-noded starting",
		"listen", cfg.ListenAddr, "health", cfg.HealthAddr,
		"node", cfg.Node, "arch", cfg.Arch,
		"maxLiveVMs", cfg.MaxLiveVMs, "images", len(cfg.Images))

	self, err := os.Executable()
	if err != nil {
		return err
	}

	// One transport shared across every VM: stateless, a fresh vsock connection
	// per RoundTrip (keep-alives disabled), so no two microVMs share a connection.
	transport := vsockhttp.NewTransport()

	// The shared restore driver serves Prime/Assign/Destroy: it only ever restores
	// from a base snapshot, so its rootfs/sizing are irrelevant (the snapshot has
	// them baked). Its in-memory live map is the authority for LiveCount.
	restoreDriver := newDriver(cfg, self, driverExtras{})

	// Per-build drivers cold-boot one image's rootfs at its sizing, then snapshot;
	// they are discarded after the base is written (the base lives on disk).
	newBuild := func(spec server.BuildDriverSpec) server.BuildDriver {
		return newDriver(cfg, self, driverExtras{
			rootfsPath:  spec.RootfsPath,
			harnessInit: spec.HarnessInit,
			vcpus:       spec.VCPUs,
			memMib:      spec.MemMib,
		})
	}

	srv := server.New(server.Options{
		Config:         cfg,
		Driver:         restoreDriver,
		Transport:      transport,
		NewBuildDriver: newBuild,
		Logger:         logger,
	})
	// Report node-local base snapshots left by a prior incarnation so the control
	// plane reconciles rather than rebuilding.
	srv.ReconcileBasesFromDisk()

	var serverOpts []grpc.ServerOption
	if cfg.BearerToken != "" {
		serverOpts = append(serverOpts,
			grpc.UnaryInterceptor(unaryAuthInterceptor(cfg.BearerToken)),
			grpc.StreamInterceptor(streamAuthInterceptor(cfg.BearerToken)),
		)
		logger.Info("bearer-token auth enabled")
	} else {
		logger.Warn("bearer-token auth DISABLED: EMBERVM_NODED_BEARER_TOKEN is unset, so the gRPC surface is open to any in-cluster client (rely on Cilium/Linkerd policy)")
	}
	gs := grpc.NewServer(serverOpts...)
	nodev1.RegisterNodeServiceServer(gs, srv)

	lis, err := net.Listen("tcp", cfg.ListenAddr)
	if err != nil {
		return err
	}

	// Plain-HTTP /healthz for kubelet probes (a privileged single-replica pod does
	// not warrant gRPC health-checking machinery).
	health := &http.Server{
		Addr:              cfg.HealthAddr,
		Handler:           healthHandler(),
		ReadHeaderTimeout: 5 * time.Second,
	}
	go func() {
		logger.Info("health endpoint listening", "addr", cfg.HealthAddr)
		if err := health.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			logger.Error("health server failed", "err", err)
		}
	}()

	errCh := make(chan error, 1)
	go func() {
		logger.Info("gRPC NodeService listening", "addr", cfg.ListenAddr)
		errCh <- gs.Serve(lis)
	}()

	select {
	case err := <-errCh:
		return err
	case <-ctx.Done():
		// Drain: stop advertising capacity, stop accepting new RPCs, and let
		// in-flight Assigns finish. Each Assign holds its microVM until it returns,
		// so GracefulStop draining handlers == draining running tasks (the fc-invoke
		// HTTP-Shutdown lesson). The pod's terminationGracePeriodSeconds is set
		// above DrainTimeout so Kubernetes never SIGKILLs mid-drain.
		logger.Info("shutdown signal received; draining", "budget", cfg.DrainTimeout)
		srv.SetDraining()
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = health.Shutdown(shutdownCtx)

		done := make(chan struct{})
		go func() { gs.GracefulStop(); close(done) }()
		select {
		case <-done:
		case <-time.After(cfg.DrainTimeout):
			logger.Warn("drain budget exceeded; forcing stop", "budget", cfg.DrainTimeout)
			gs.Stop()
		}
		return nil
	}
}

// driverExtras carries the per-driver cold-boot fields (empty for the shared
// restore driver, populated for a build driver).
type driverExtras struct {
	rootfsPath  string
	harnessInit string
	vcpus       int
	memMib      int
}

// newDriver builds an fcvm driver bound to the node's substrate paths. Cold-boot
// fields (rootfs, sizing, harness init) are set only for build drivers; the
// restore driver leaves them zero because restore ignores them.
func newDriver(cfg config.Config, self string, x driverExtras) *driver.Driver {
	return driver.New(driver.Config{
		KernelImagePath:   cfg.KernelImagePath,
		KernelBootArgs:    cfg.KernelBootArgs,
		RootfsPath:        x.rootfsPath,
		RootfsReadOnly:    true,
		CanonicalVsockDir: cfg.CanonicalVsockDir,
		HarnessInit:       x.harnessInit,
		VCPUs:             x.vcpus,
		MemMib:            x.memMib,
		SnapshotRoot:      cfg.SnapshotRoot,
		Node:              cfg.Node,
		Arch:              cfg.Arch,
	}, &driver.ExecLauncher{
		Bin:             cfg.BinPath,
		OOMScoreAdj:     cfg.GuestOomScoreAdj,
		VsockBindTarget: cfg.CanonicalVsockDir,
		Self:            self,
	}, nil)
}

// healthHandler answers 200 on /healthz for kubelet probes.
func healthHandler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok"))
	})
	return mux
}
