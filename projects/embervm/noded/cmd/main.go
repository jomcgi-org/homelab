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
	"fmt"
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
	"github.com/jomcgi/homelab/projects/embervm/noded/serving"
	"github.com/jomcgi/homelab/projects/embervm/noded/store"
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
		"maxLiveVMs", cfg.MaxLiveVMs, "registryCache", cfg.RegistryCachePath)

	// Startup GC of orphan per-instance (brick) warmth: reap SnapshotRoot/i/<seg>
	// dirs left by evicted/rolled-out co-located bricks whose pod UID is not ours.
	// Fail-soft (warmth is regenerable) and narrow: bases/ and the VolumeRoot are
	// never touched, and this is a no-op on the legacy DaemonSet (flat warmth).
	if removed := config.PruneStaleInstanceWarmth(cfg, func(segment string, err error) {
		logger.Warn("startup GC: could not remove stale instance warmth", "segment", segment, "err", err)
	}); len(removed) > 0 {
		logger.Info("startup GC: reaped stale instance warmth", "count", len(removed), "segments", removed)
	}

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

	// The serving network Manager owns the host bridge, taps, nftables, and IP
	// allocation for serving-class VMs (R3). A malformed serving CIDR fails startup
	// loudly. The same restore driver serves the serving driver mechanics: it gains a
	// cold-boot-with-NIC ClaimServing plus SnapshotServing/RestoreServing under the
	// serving/ prefix, and its live map counts serving VMs against the node cap.
	// cfg.TapPrealloc (ADR embervm/014 decision 4) is clamped to the brick's slot
	// ceiling below, a configured runaway backstop no longer derived from the memory budget.
	servingNet, err := serving.NewManager(serving.ExecRunner{}, cfg.ServingBridge, cfg.ServingSubnetCIDR, cfg.PodIP, cfg.ServingPortBase, cfg.TapPrealloc)
	if err != nil {
		return err
	}
	if cfg.PodIP == "" {
		// Fail-loud-not-silent (mirrors the empty-bearer-token posture): without a pod IP
		// the daemon reports node-internal tap IPs and installs no DNAT, so serving
		// endpoints are unreachable from off-node Envoys. Fine for tests/local; a warning
		// in prod flags a missing Downward-API env.
		logger.Warn("serving: EMBERVM_NODED_POD_IP unset; serving endpoints report node-internal tap IPs and DNAT is disabled")
	}

	// The composite-group network Manager (R5) owns per-group bridges, deterministic
	// member addressing, the inter-group isolation (a dedicated embervm_group nft
	// table, so it never collides with the serving forward/serving_dnat chains), and
	// the entry-member pod-IP DNAT (the same D-R3.11.4 lane). It carves a /24 per
	// group out of the composite supernet and denies composite->serving toward the
	// serving bridge. A malformed supernet fails startup loudly.
	groupNet, err := serving.NewGroupManager(serving.ExecRunner{}, cfg.CompositeSupernet, cfg.ServingBridge, cfg.PodIP, cfg.ServingPortBase)
	if err != nil {
		return err
	}

	// The off-node object store (R6): the continuity verbs and the async export
	// queue move banked artifacts to and from this S3-API endpoint. store.New
	// returns nil for an empty endpoint (the store is disabled), so the Options
	// field is left unset in that case to keep the server's typed-nil guards clean
	// (a nil interface, not an interface holding a nil pointer).
	artStore := store.New(cfg.StoreEndpoint, cfg.StoreBucket)
	if cfg.StoreEndpoint == "" {
		logger.Warn("object store DISABLED: EMBERVM_NODED_STORE_ENDPOINT unset; banked state stays local-only (no off-node durability, no restore-on-miss)")
	} else {
		logger.Info("object store configured", "endpoint", cfg.StoreEndpoint, "bucket", cfg.StoreBucket)
	}

	opts := server.Options{
		Config: cfg,
		Driver: restoreDriver,
		// The same restore driver serves the R2 session verbs: SnapshotSession /
		// RestoreSession / RemoveSessionBundle reuse its base-bundle snapshot/restore
		// mechanics under a sessions/ prefix, and its live map already counts session
		// VMs against the node cap.
		SessionDriver: restoreDriver,
		// The same restore driver also serves the R3 serving verbs (ClaimServing cold
		// boot + SnapshotServing/RestoreServing under serving/).
		ServingNet:                servingNet,
		ServingDriver:             restoreDriver,
		Transport:                 transport,
		NewBuildDriver:            newBuild,
		Logger:                    logger,
		ServingProbeInterval:      cfg.ServingProbeInterval,
		ServingUnhealthyThreshold: cfg.ServingUnhealthyThreshold,
		// The same restore driver also serves the R4 stateful verbs (ClaimStateful
		// cold boot with a writable volume + SnapshotStateful/RestoreStateful under
		// stateful/); the volume manager itself is a plain directory (VolumeRoot),
		// not a driver capability.
		StatefulDriver:             restoreDriver,
		VolumeRoot:                 cfg.VolumeRoot,
		StatefulProbeInterval:      cfg.StatefulProbeInterval,
		StatefulUnhealthyThreshold: cfg.StatefulUnhealthyThreshold,
		// The composite-group network Manager (R5) and the same restore driver for
		// the durable on-disk group-network records (group_networks/<gid>/config.json,
		// a sibling of the bases/sessions/serving/stateful bundle dirs).
		GroupNet:     groupNet,
		GroupRecords: restoreDriver,
		// The same restore driver serves the R5 member lifecycle (ClaimGroupMember
		// cold boot on the group bridge + SnapshotGroupMember/RestoreGroupMember under
		// group/<set_id>/<member>/). The member RELIGHT clock resync defaults to the
		// real vsock-backed groupclock (port-1024 length-prefixed JSON); no explicit
		// GroupClock is set here.
		GroupDriver:             restoreDriver,
		GroupProbeInterval:      cfg.StatefulProbeInterval,
		GroupUnhealthyThreshold: cfg.StatefulUnhealthyThreshold,
	}
	if artStore != nil {
		opts.Store = artStore
	}
	srv := server.New(opts)
	// Cap the tap-prealloc pool (ADR embervm/014 decision 4) at the brick's
	// configured live-VM backstop: pre-creating more taps than the daemon's runaway
	// limit wastes boot-time netlink work. Must run before EnsureNetwork below.
	servingNet.ClampTapPrealloc(srv.SlotCeiling())
	// Report node-local base snapshots left by a prior incarnation so the control
	// plane reconciles rather than rebuilding.
	srv.ReconcileBasesFromDisk()
	// Called only at daemon startup, before any work begins.
	srv.CleanupStagingDirs()
	// Report node-local BANKED session snapshots left by a prior incarnation so the
	// control plane adopts surviving banked sessions (live session VMs died with the
	// prior daemon; their last banked snapshot, if any, stays restorable).
	srv.ReconcileSessionsFromDisk()
	// Report node-local BANKED serving snapshots (with their pinned IPs) the same way,
	// so a relight after restart re-acquires the same tap IP (D-R3.4.1).
	srv.ReconcileServingFromDisk()
	// Report node-local BANKED stateful bundles (with their stamped generations) and
	// ensure VolumeRoot exists; the durable volumes themselves need no in-memory
	// seeding (volume.Manager reads VolumeRoot fresh off disk on every NodeStatus).
	srv.ReconcileStatefulFromDisk()
	// Report node-local group-network records left by a prior incarnation so the
	// control plane reconciles group networks from node truth (the bridges died with
	// the prior pod; the durable records survive and the control plane re-issues
	// CreateGroupNetwork to rebuild each bridge, Task 7).
	srv.ReconcileGroupNetworksFromDisk()
	// Report node-local BANKED group member bundles left by a prior incarnation,
	// grouped by set dir, so the control plane reconciles banked-group inventory from
	// node truth (live members died with the prior pod; the on-disk bundle sets
	// survive and the control plane resolves each group to relightable-or-fresh).
	srv.ReconcileGroupBundlesFromDisk()
	// R6 off-node durability: start the bounded async export-worker pool, the
	// store-reachability probe (feeds NodeStatus.store_reachable), and a reconcile
	// sweep that enqueues an export for any local artifact whose store copy is
	// missing or stale (covers "a roll exited before exports finished"). All three
	// no-op when the store is disabled. They run for the daemon's lifetime; ctx
	// cancels them on shutdown, and the export queue is fire-and-forget so it never
	// holds the drain deadline.
	srv.StartStoreLoops(ctx)
	// Sample the cgroup CPU usage rate on the liveness cadence so
	// NodeStatus.cpu_headroom_millicores always has a live two-sample delta
	// rather than a stale or empty one (ADR embervm/005 item 4: the daemon
	// reads its own cgroup budget, not static config). Mem budget/headroom
	// are cheap best-effort reads with no caching and need no loop.
	srv.StartBudgetLoop(ctx)
	// Create the serving bridge and install the ingress-only nftables posture before
	// serving any StartServing. Idempotent across restarts (existing bridge tolerated).
	if err := servingNet.EnsureNetwork(ctx); err != nil {
		return fmt.Errorf("serving network setup: %w", err)
	}
	// Enable forwarding and install the composite-group isolation nftables posture
	// (initially the empty-group / post-adoption table) before any CreateGroupNetwork.
	if err := groupNet.EnsureNetwork(ctx); err != nil {
		return fmt.Errorf("group network setup: %w", err)
	}

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
	// The HTTP activator is serving-class only. Bind before marking it advertised so
	// NodeStatus never sends an Envoy request to a listener that is not present.
	activatorLis, err := net.Listen("tcp", cfg.ActivatorAddr)
	if err != nil {
		return err
	}
	activatorHTTP := &http.Server{
		Handler:           srv.ActivatorHandler(),
		ReadHeaderTimeout: 5 * time.Second,
	}
	srv.EnableActivator()
	var statefulActivatorListeners []net.Listener
	if lo, hi := cfg.StatefulActivatorPortRange[0], cfg.StatefulActivatorPortRange[1]; lo != 0 && hi >= lo && cfg.VolumeRoot != "" {
		for port := lo; port <= hi; port++ {
			statefulLis, err := net.Listen("tcp", fmt.Sprintf(":%d", port))
			if err != nil {
				for _, bound := range statefulActivatorListeners {
					_ = bound.Close()
				}
				return fmt.Errorf("stateful activator listen on port %d: %w", port, err)
			}
			statefulActivatorListeners = append(statefulActivatorListeners, statefulLis)
		}
		srv.StartStatefulActivator(ctx, statefulActivatorListeners)
	}
	var groupActivatorListeners []net.Listener
	if lo, hi := cfg.GroupActivatorPortRange[0], cfg.GroupActivatorPortRange[1]; lo != 0 && hi >= lo && cfg.CompositeSupernet != "" {
		for port := lo; port <= hi; port++ {
			groupLis, err := net.Listen("tcp", fmt.Sprintf(":%d", port))
			if err != nil {
				for _, bound := range groupActivatorListeners {
					_ = bound.Close()
				}
				return fmt.Errorf("group activator listen on port %d: %w", port, err)
			}
			groupActivatorListeners = append(groupActivatorListeners, groupLis)
		}
		srv.StartGroupActivator(ctx, groupActivatorListeners)
	}

	// Plain-HTTP /healthz for kubelet probes (a privileged single-replica pod does
	// not warrant gRPC health-checking machinery).
	health := &http.Server{
		Addr:              cfg.HealthAddr,
		Handler:           healthHandler(srv),
		ReadHeaderTimeout: 5 * time.Second,
	}
	go func() {
		logger.Info("health endpoint listening", "addr", cfg.HealthAddr)
		if err := health.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			logger.Error("health server failed", "err", err)
		}
	}()
	go func() {
		logger.Info("activator endpoint listening", "addr", cfg.ActivatorAddr)
		if err := activatorHTTP.Serve(activatorLis); err != nil && !errors.Is(err, http.ErrServerClosed) {
			logger.Error("activator server failed", "err", err)
		}
	}()

	errCh := make(chan error, 1)
	go func() {
		logger.Info("gRPC NodeService listening", "addr", cfg.ListenAddr)
		errCh <- gs.Serve(lis)
	}()

	// Dial-home registration (R0 PR-2): now that the gRPC surface is up, advertise
	// this instance's identity to the control plane so it adopts us without ever
	// listing pods. Started AFTER Serve so the control plane never dials an address
	// that is not yet listening; the loop stops re-advertising once draining and
	// exits on ctx cancellation. A no-op when no control-plane URL is configured.
	srv.RunRegisterLoop(ctx)

	select {
	case err := <-errCh:
		return err
	case <-ctx.Done():
		// Drain (R6 bounded preemption): publish a deadline and HOLD the door.
		// noded has no lifecycle authority, so it does not bank anything itself; it
		// sets draining + a deadline on NodeStatus, and the control plane, watching
		// that stream, force-banks every managed session/serving/stateful/group VM
		// before the deadline (ADR embervm/009). The gRPC surface stays UP the whole
		// time so those Bank/Stop/Resolve rpcs are served; only new BuildBase/Prime/
		// Assign are refused (the draining flag). We hold until the managed live-VM
		// registry empties or the deadline passes, then drain in-flight task Assigns
		// via GracefulStop. The pod's terminationGracePeriodSeconds is drain + 30s so
		// Kubernetes never SIGKILLs mid-bank.
		deadline := time.Now().Add(cfg.DrainTimeout)
		logger.Info("shutdown signal received; draining", "budget", cfg.DrainTimeout, "deadline", deadline.UTC())
		srv.SetDraining(deadline)

		shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		_ = health.Shutdown(shutdownCtx)
		_ = activatorHTTP.Shutdown(shutdownCtx)
		cancel()

		// Hold the door: keep serving lifecycle rpcs until the control plane has
		// banked every managed VM, or the deadline elapses. remaining>0 means the
		// bank pass could not finish in the window; those VMs die with the pod (spot
		// semantics: their durable state is the volume or a prior bundle).
		if remaining := srv.WaitForManagedDrain(context.Background(), deadline); remaining > 0 {
			logger.Warn("drain deadline reached with managed VMs still live; they will be reaped with the pod", "remaining", remaining)
		} else {
			logger.Info("all managed VMs drained; stopping")
		}

		// Builds are the last drain priority (durable banks first, serving banks
		// second, builds finish-or-abort last; ADR embervm/009 resolved-question 5):
		// let any in-flight BuildBase work finish within whatever budget the bank pass
		// left, else abort it cleanly. Builds are reconstructible, so an abort only
		// re-queues; the build VM is torn down and no half-written snapshot survives.
		if aborted := srv.WaitForBuildsOrAbort(deadline); aborted > 0 {
			logger.Warn("drain deadline reached with builds in flight; aborted and left re-queueable", "aborted", aborted)
		}

		// Now stop the server: in-flight task Assigns get a short grace, then hard stop.
		done := make(chan struct{})
		go func() { gs.GracefulStop(); close(done) }()
		select {
		case <-done:
		case <-time.After(10 * time.Second):
			logger.Warn("graceful stop budget exceeded; forcing stop")
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
		WarmthRoot:        cfg.WarmthRoot,
		Node:              cfg.Node,
		Arch:              cfg.Arch,
		Vendor:            cfg.CpuVendor,
		Template:          cfg.CpuTemplate,
	}, &driver.ExecLauncher{
		Bin:             cfg.BinPath,
		OOMScoreAdj:     cfg.GuestOomScoreAdj,
		VsockBindTarget: cfg.CanonicalVsockDir,
		Self:            self,
	}, nil)
}

// healthHandler answers the kubelet probes. /healthz is LIVENESS: it is 200 as
// soon as the process is up (the daemon is alive and should not be restarted).
// /readyz is READINESS: it is 200 only AFTER the control plane has replayed the
// workload registry over SyncRegistry (artifact-decoupling Phase 2). Gating
// readiness on the registry replay means Service traffic never reaches a pod with
// an empty registry: a freshly (re)started noded is live but not ready until it
// has been told what it can serve. A daemon serving a STALE boot-cache registry
// is deliberately NOT ready (it serves existing warmth but admits no new work, ADR
// embervm/012); only a live sync flips it ready.
func healthHandler(srv *server.Server) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok"))
	})
	mux.HandleFunc("/readyz", func(w http.ResponseWriter, _ *http.Request) {
		if srv.RegistrySynced() {
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte("ready"))
			return
		}
		w.WriteHeader(http.StatusServiceUnavailable)
		_, _ = w.Write([]byte("registry not synced"))
	})
	return mux
}
