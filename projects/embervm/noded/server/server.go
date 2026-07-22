// Package server implements the embervm.node.v1 NodeService: the gRPC face of
// embervm-noded. It is the reshaped fork of fc-invoke's node/invoker - the same
// single-use Firecracker lifecycle (cold boot -> health-gate -> snapshot; restore
// -> park; deliver one vsock HTTP task -> destroy), re-cut behind the Task 3 gRPC
// contract instead of an HTTP reverse proxy.
//
// What the fork drops from the invoker: the per-workload semaphore (concurrency
// is the control plane's; the daemon keeps only a node-level max-live-VM
// backstop), the warm-base auto-build-at-startup (base builds are BuildBase RPCs
// now), path-based sessions, and the lazy HTTP response-streaming ownership
// (Assign reads the guest body whole because the VM is destroyed immediately
// after, so there is nothing to stream over).
//
// What the fork keeps: the fcvm driver's Claim/SnapshotBase/Release lifecycle,
// the vsockhttp transport with its per-attempt WaitReady cap and short restore
// budget, the read-only-rootfs + tmpfs guest conventions, and the egress
// forwarder (present but unused: task VMs get no NIC, egress disabled).
package server

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"

	"golang.org/x/sys/unix"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	nodev1 "github.com/jomcgi/homelab/projects/embervm/proto/embervm/node/v1"

	"github.com/jomcgi/homelab/projects/embervm/noded/config"
	"github.com/jomcgi/homelab/projects/embervm/noded/serving"
	"github.com/jomcgi/homelab/projects/embervm/noded/substrate"
	"github.com/jomcgi/homelab/projects/embervm/noded/volume"
)

const (
	// maxGuestResponseBytes caps how much of a guest response body the daemon
	// buffers before destroying the VM. The submit API caps the request body at
	// 8 MiB; responses are bounded generously here so a runaway guest cannot pin
	// daemon memory.
	maxGuestResponseBytes = 32 << 20
	// defaultInvokePath is the guest path an Assign POSTs to when the request
	// carries none. The contract defaults path to the workload invokePath; the
	// daemon does not hold the workload table, so it uses the frozen guest
	// convention and the control plane overrides per task.
	defaultInvokePath = "/invoke"
	// defaultAssignTimeout bounds a guest round-trip when the AssignRequest sets
	// timeout_ms to zero.
	defaultAssignTimeout = 90 * time.Second
	// livenessInterval is how often WatchNode re-sends NodeStatus absent any
	// material change. It is below the control plane's 5s "unknown" ageing window
	// so a healthy node never looks stale.
	livenessInterval = 2 * time.Second
	// defaultArchiveMaxBytes bounds a zip-lane archive fetch when the config leaves
	// ArchiveMaxBytes unset (0). 512 MiB matches config.Load's default and is a
	// daemon-memory backstop (the archive is read into memory for vsock hydration,
	// never written to disk); the bytes are opaque to noded.
	defaultArchiveMaxBytes = 512 << 20
)

// vmDriver is the subset of the fcvm driver the restore/assign/destroy paths
// need. The real *driver.Driver satisfies it; tests inject a fake.
type vmDriver interface {
	// Claim restores a microVM from spec.BaseSnapshotRef (Prime always restores).
	Claim(ctx context.Context, spec substrate.ClaimSpec) (substrate.Handle, error)
	// Release kills the microVM process, reclaiming its memory.
	Release(ctx context.Context, h substrate.Handle) error
	// RemoveBundle deletes the microVM's on-disk bundle dir.
	RemoveBundle(threadID string) error
	// VsockUDSPath is the host UDS backing a thread's vsock device.
	VsockUDSPath(threadID string) string
	// Stats reads the guest's /proc counters; must be called while it is alive.
	Stats(h substrate.Handle) (substrate.GuestStats, error)
	// LiveCount is how many microVMs the driver is supervising (backstop cap).
	LiveCount() int
}

// BuildDriver is the subset a BuildBase cold boot + snapshot needs. It is a
// separate seam because each base build runs on a driver configured for that
// image's rootfs and sizing (cold boot uses them; restore ignores them), whereas
// the shared vmDriver only ever restores. Exported so the daemon entrypoint can
// name it as the return type of the build-driver factory.
type BuildDriver interface {
	Claim(ctx context.Context, spec substrate.ClaimSpec) (substrate.Handle, error)
	SnapshotBase(ctx context.Context, h substrate.Handle, baseKey string) (substrate.SnapshotRef, error)
	Release(ctx context.Context, h substrate.Handle) error
	RemoveBundle(threadID string) error
	VsockUDSPath(threadID string) string
}

// transport is the host-side HTTP-over-vsock client (the forked vsockhttp
// Transport). Tests inject a fake.
type transport interface {
	WaitReady(ctx context.Context, udsPath, readyPath string) error
	Prime(ctx context.Context, udsPath string) error
	Hydrate(ctx context.Context, udsPath string, archive []byte) error
	RoundTrip(ctx context.Context, udsPath string, req *http.Request) (*http.Response, error)
}

// sessionDriver is the subset of the fcvm driver the R2 session verbs need on top
// of vmDriver: bank a live session VM to a self-contained bundle, relight (restore)
// a VM from a banked bundle, and remove a banked bundle from disk. The real
// *driver.Driver satisfies it; tests inject a fake. It is a separate seam from
// vmDriver so a Server built without session support (older tests) still compiles,
// and so a reviewer sees exactly which driver mechanics the session path reuses
// (the base-bundle snapshot/restore path, under a sessions/ prefix).
type sessionDriver interface {
	// SnapshotSession pauses a live session VM and writes a self-contained session
	// bundle (memfile + snapfile, the base-bundle format) under sessions/<ref>. It
	// does NOT resume: the caller Releases the VM immediately after (Bank destroys).
	SnapshotSession(ctx context.Context, h substrate.Handle, snapshotRef string) (substrate.SnapshotRef, error)
	// RestoreSession launches a fresh VM from a banked session bundle and resumes it.
	RestoreSession(ctx context.Context, snapshotRef string) (substrate.Handle, error)
	// RemoveSessionBundle deletes a banked session bundle from disk (idempotent).
	RemoveSessionBundle(snapshotRef string) error
	// SessionsDir is the directory holding banked session bundles, rescanned on start.
	SessionsDir() string
}

// BuildDriverSpec parameterises a per-image cold-boot driver.
type BuildDriverSpec struct {
	RootfsPath  string
	HarnessInit string
	VCPUs       int
	MemMib      int
}

// Server implements nodev1.NodeServiceServer.
type Server struct {
	nodev1.UnimplementedNodeServiceServer

	cfg           config.Config
	driver        vmDriver
	sessionDriver sessionDriver
	transport     transport
	// newBuildDriver builds a cold-boot driver for one image's rootfs+sizing.
	// nil disables BuildBase (used by tests that only exercise Prime/Assign).
	newBuildDriver func(BuildDriverSpec) BuildDriver
	logger         *slog.Logger

	// budget is the cgroup v2 reader backing the four hooks below (ADR
	// embervm/005 item 4): a brick's slot count and headroom are read from
	// its own cgroup, never configured. Refresh() is called by
	// StartBudgetLoop on the liveness cadence so CpuHeadroom has a live
	// usage-rate delta; the mem hooks are cheap best-effort reads with no
	// caching.
	budget *budget

	// memHeadroom reads free guest memory in MiB (cgroup v2, best-effort).
	// Overridable in tests.
	memHeadroom func() uint64
	// memBudget reads the cgroup memory ceiling in MiB (memory.max minus the
	// daemon RSS reserve). 0 means unknown (unlimited cgroup or read error),
	// never a guess. Overridable in tests.
	memBudget func() uint64
	// cpuBudget reads the cgroup CPU ceiling in millicores from cpu.max. 0
	// means unknown. Overridable in tests.
	cpuBudget func() uint64
	// cpuHeadroom reads the last-sampled CPU headroom in millicores (budget
	// minus the observed usage rate across the two most recent Refresh
	// calls). 0 until a second sample exists. Overridable in tests.
	cpuHeadroom func() uint64
	// slotCeiling maps the configured MaxLiveVMs backstop to the brick's
	// cgroup-derived live-VM slot ceiling (floor(MemBudgetMib/minWorkload),
	// clamped to the configured value). The reported MaxLiveVms is this
	// ceiling, so a 2gi brick advertises a handful of slots rather than the
	// configured default. Overridable in tests.
	slotCeiling func(configured uint64) uint64

	// httpClient fetches zip-lane archives from the in-cluster SeaweedFS read path
	// over the pod network. Overridable in tests (a fake archive server).
	httpClient *http.Client

	vms   *vmRegistry
	bases *baseRegistry
	// registry is the control-plane-pushed workload table (artifact-decoupling
	// Phase 2). The daemon boots with it empty (or a STALE boot-cache load) and
	// admits no new work until the control plane replays it over SyncRegistry
	// (readiness gate). It replaces the retired EMBERVM_NODED_IMAGES config table.
	registry     *workloadRegistry
	sessionVMs   *sessionRegistry
	sessionSnap  *sessionSnapshotRegistry
	servingVMs   *servingRegistry
	servingSnap  *servingSnapshotRegistry
	servingImage *servingImageRegistry

	// servingNet owns the host serving network (bridge, taps, nftables, IP
	// allocation). nil disables the serving verbs (task/session-only tests and any
	// build without serving networking configured): StartServing/StopServing then
	// return Unimplemented and NodeStatus.serving_subnet_cidr is empty.
	servingNet servingNetwork
	// servingDriver serves the serving-class driver mechanics (cold boot with NIC,
	// bank/relight under serving/). nil (alongside a nil servingNet) disables serving.
	servingDriver servingDriver
	// newProber builds the per-VM health prober from config. Overridable in tests.
	newProber func() *serving.Prober

	statefulVMs     *statefulRegistry
	statefulBundles *statefulBundleRegistry
	// statefulDriver serves the stateful-class driver mechanics (cold boot with
	// NIC + writable volume, bank/relight under stateful/). nil disables the
	// stateful verbs (StartStateful/StopStateful/DeleteVolume then return
	// Unimplemented). It reuses servingNet for tap/DNAT (the stateful and
	// serving networking postures are identical: a tap NIC on the same bridge).
	statefulDriver statefulDriver
	// volumes owns the on-disk stateful volume layout (create/attach/generation
	// ledger/delete). nil disables the stateful verbs alongside statefulDriver.
	volumes *volume.Manager
	// newTCPProber builds the per-VM TCP-connect health prober from config
	// (R4). Overridable in tests, mirroring newProber.
	newTCPProber func() *serving.TCPProber
	// statefulResolveTimeout is noded's own deadline T for an interruptible-bank
	// CHECKPOINT left unresolved (ADR embervm/008): after it, noded auto-aborts
	// (resume, discard temp) so a dead control plane cannot pin a paused VM's cap
	// slot and memory forever. Defaulted in New from defaultStatefulResolveTimeout;
	// tests shrink it to exercise the auto-abort without waiting.
	statefulResolveTimeout time.Duration
	// newGroupTCPProber builds the per-member TCP-connect health prober (R5).
	// Overridable in tests, mirroring newTCPProber.
	newGroupTCPProber func() *serving.TCPProber

	// groupNet owns the host composite-group networking (per-group bridges, member
	// addressing, inter-group isolation, entry DNAT). nil disables the R5 group
	// verbs: CreateGroupNetwork/DeleteGroupNetwork then return Unimplemented and
	// NodeStatus.group_networks is empty. In production a *serving.GroupManager
	// satisfies it.
	groupNet groupNetwork
	// groupRecords owns the durable on-disk group-network records (the adoption
	// source of truth). nil (alongside a nil groupNet) disables the group verbs. In
	// production the same *driver.Driver satisfies it.
	groupRecords groupRecordStore
	// groupMembers is the live composite-group member registry, filled by
	// StartGroupMember and drained by StopGroupMember. It counts against the node
	// live-VM cap (via the shared driver's LiveCount) and is EXCLUDED from
	// primed_vm_ids and every other class registry.
	groupMembers *groupMemberRegistry
	// groupDriver serves the R5 member-class driver mechanics (cold boot with a NIC
	// on the group bridge, bank/relight under group/<set_id>/<member>/). nil
	// (alongside a nil groupNet) disables the member verbs, which then return
	// Unimplemented. In production the same *driver.Driver satisfies it.
	groupDriver groupMemberDriver
	// groupBundles is the banked-group-bundle inventory, seeded from disk on start
	// (ScanGroupBundleSets) and updated on a member bank; reported grouped by set_id.
	groupBundles *groupBundleRegistry
	// groupClock is the host-side clock-resync seam a member RELIGHT uses over the
	// port-1024 length-prefixed JSON agent channel. nil falls back to the real
	// groupclock.Resync; overridable in tests.
	groupClock groupClock

	drainingMu          sync.RWMutex
	draining            bool
	drainDeadlineUnixMs int64

	// activeBuilds tracks in-flight BuildBase work so a drain can finish-or-abort
	// it inside the budget (artifact-decoupling Phase 0). Each entry's cancel tears
	// its build context down: runBuild's deferred Release + RemoveBundle destroy the
	// build VM, driveBuild marks the base re-queueable (failBuild, never READY), and
	// no half-written snapshot survives (meta.json-last already covers the store
	// side). buildsWG counts the same builds so the drain waits for a clean finish
	// until the deadline before it cancels.
	buildsMu     sync.Mutex
	activeBuilds map[string]context.CancelFunc
	buildsWG     sync.WaitGroup

	// store is the off-node object-store client the R6 continuity verbs and the
	// async export queue use. nil disables the store (no endpoint configured):
	// ExportArtifact/RestoreArtifact refuse FAILED_PRECONDITION, exports are
	// no-ops, and NodeStatus.store_reachable stays false. In production a
	// *store.Store satisfies it; tests inject an in-memory fake.
	store artifactStore
	// exported caches which artifacts (by store prefix) have a current store copy
	// and at which generation, updated by the export queue and read by the
	// NodeStatus projection for the per-artifact `exported` bool and
	// Volume.exported_generation.
	exported *exportedCache
	// exportCh is the bounded async export queue; exportDedupe drops a re-enqueue
	// of an already-queued prefix. Both nil/empty until startExportQueue runs.
	exportCh       chan exportJob
	exportOnce     sync.Once
	exportDedupeMu sync.Mutex
	exportDedupe   map[string]struct{}
	// restoreCh is the bounded async BASE-restore queue; restoreDedupe drops a
	// re-enqueue of an already-in-flight prefix (held enqueue-through-completion,
	// so a re-triggered restore of a base still downloading is a no-op). Both
	// nil/empty until startExportQueue runs (the restore queue shares that
	// lifecycle). A BASE restore's multi-GB download runs here so the RPC never
	// holds an idle flow open long enough for the Cilium/eBPF datapath to reap its
	// conntrack entry, mirroring the export queue (base-durability PR-2).
	restoreCh       chan restoreJob
	restoreDedupeMu sync.Mutex
	restoreDedupe   map[string]struct{}
	// storeReachable is the latest object-store reachability verdict from the
	// probe loop, surfaced in NodeStatus.store_reachable.
	storeMu        sync.RWMutex
	storeReachable bool

	subMu sync.Mutex
	subs  map[chan struct{}]struct{}
}

// Options configures a Server.
type Options struct {
	Config config.Config
	Driver vmDriver
	// SessionDriver serves the R2 session verbs (bank/relight/evict). It may be nil
	// in tests that only exercise the task-class path; the session handlers then
	// return Unimplemented. In production the same *driver.Driver satisfies both
	// Driver and SessionDriver.
	SessionDriver sessionDriver
	// ServingNet and ServingDriver serve the R3 serving verbs. Both nil (the default)
	// leaves StartServing/StopServing returning Unimplemented, so a Server built
	// without serving support still compiles and task/session behavior is untouched.
	// In production the same *driver.Driver satisfies ServingDriver and a
	// *serving.Manager satisfies ServingNet.
	ServingNet     servingNetwork
	ServingDriver  servingDriver
	Transport      transport
	NewBuildDriver func(BuildDriverSpec) BuildDriver
	Logger         *slog.Logger
	// ServingProbeInterval / ServingUnhealthyThreshold configure the per-VM health
	// probe loop (defaults applied when zero).
	ServingProbeInterval      time.Duration
	ServingUnhealthyThreshold int
	// StatefulDriver serves the R4 stateful verbs (StartStateful/StopStateful/
	// DeleteVolume). nil (the default) leaves them returning Unimplemented, so a
	// Server built without stateful support still compiles and task/session/
	// serving behavior is untouched. In production the same *driver.Driver
	// satisfies ServingDriver and StatefulDriver.
	StatefulDriver statefulDriver
	// VolumeRoot is the directory the daemon creates per-workload stateful
	// volume files under (VolumeRoot/<workload>/vol.img + gen). Required
	// alongside StatefulDriver to enable the stateful verbs.
	VolumeRoot string
	// StatefulProbeInterval / StatefulUnhealthyThreshold configure the per-VM
	// TCP-connect health probe loop (defaults applied when zero), mirroring the
	// serving probe knobs but for opaque L4 TCP CONNECT instead of HTTP GET.
	StatefulProbeInterval      time.Duration
	StatefulUnhealthyThreshold int
	// GroupNet and GroupRecords serve the R5 composite-group verbs
	// (CreateGroupNetwork/DeleteGroupNetwork, and Task 5's member lifecycle). Both
	// nil (the default) leaves the group verbs returning Unimplemented, so a Server
	// built without group support still compiles and every other class is
	// untouched. In production a *serving.GroupManager satisfies GroupNet and the
	// same *driver.Driver satisfies GroupRecords.
	GroupNet     groupNetwork
	GroupRecords groupRecordStore
	// GroupDriver serves the R5 member lifecycle (StartGroupMember/StopGroupMember).
	// nil (the default) leaves the member verbs returning Unimplemented, so a Server
	// built without group support still compiles. In production the same
	// *driver.Driver satisfies GroupDriver.
	GroupDriver groupMemberDriver
	// GroupClock is the host-side member clock-resync seam. nil uses the real
	// groupclock.Resync over a VsockDialer; tests inject a fake.
	GroupClock groupClock
	// GroupProbeInterval / GroupUnhealthyThreshold configure the per-member TCP
	// health probe loop (defaults applied when zero), mirroring the stateful knobs.
	GroupProbeInterval      time.Duration
	GroupUnhealthyThreshold int
	// Store is the off-node object-store client the R6 continuity verbs and the
	// async export queue use. nil (the default) disables the store: the continuity
	// verbs refuse FAILED_PRECONDITION and exports are no-ops, so a Server built
	// without a store still compiles and every other class is untouched. In
	// production a *store.Store satisfies it; tests inject an in-memory fake.
	Store artifactStore
}

// New builds a Server. Driver and Transport must not be nil.
func New(opts Options) *Server {
	logger := opts.Logger
	if logger == nil {
		logger = slog.Default()
	}
	s := &Server{
		cfg:             opts.Config,
		driver:          opts.Driver,
		sessionDriver:   opts.SessionDriver,
		transport:       opts.Transport,
		newBuildDriver:  opts.NewBuildDriver,
		logger:          logger,
		vms:             newVMRegistry(),
		bases:           newBaseRegistry(),
		registry:        newWorkloadRegistry(opts.Config.RegistryCachePath),
		sessionVMs:      newSessionRegistry(),
		sessionSnap:     newSessionSnapshotRegistry(),
		servingVMs:      newServingRegistry(),
		servingSnap:     newServingSnapshotRegistry(),
		servingImage:    newServingImageRegistry(),
		servingNet:      opts.ServingNet,
		servingDriver:   opts.ServingDriver,
		statefulVMs:     newStatefulRegistry(),
		statefulBundles: newStatefulBundleRegistry(),
		statefulDriver:  opts.StatefulDriver,
		groupNet:        opts.GroupNet,
		groupRecords:    opts.GroupRecords,
		groupMembers:    newGroupMemberRegistry(),
		groupDriver:     opts.GroupDriver,
		groupBundles:    newGroupBundleRegistry(),
		groupClock:      opts.GroupClock,
		store:           opts.Store,
		exported:        newExportedCache(),
		exportDedupe:    make(map[string]struct{}),
		restoreDedupe:   make(map[string]struct{}),
		subs:            make(map[chan struct{}]struct{}),
		activeBuilds:    make(map[string]context.CancelFunc),
	}
	// VolumeRoot may be set directly on Options (mirroring how cmd/main.go wires
	// every other stateful/serving knob explicitly) or left to fall back to
	// Config.VolumeRoot, so a caller that only populates Config (as several
	// existing tests do for other fields) still gets a working volume manager.
	volumeRoot := opts.VolumeRoot
	if volumeRoot == "" {
		volumeRoot = opts.Config.VolumeRoot
	}
	if volumeRoot != "" {
		s.volumes = volume.NewManager(volumeRoot)
		s.cfg.VolumeRoot = volumeRoot
	}
	probeInterval := opts.ServingProbeInterval
	probeThreshold := opts.ServingUnhealthyThreshold
	s.newProber = func() *serving.Prober { return serving.NewProber(probeInterval, probeThreshold) }
	statefulProbeInterval := opts.StatefulProbeInterval
	statefulProbeThreshold := opts.StatefulUnhealthyThreshold
	s.newTCPProber = func() *serving.TCPProber { return serving.NewTCPProber(statefulProbeInterval, statefulProbeThreshold) }
	groupProbeInterval := opts.GroupProbeInterval
	groupProbeThreshold := opts.GroupUnhealthyThreshold
	s.newGroupTCPProber = func() *serving.TCPProber { return serving.NewTCPProber(groupProbeInterval, groupProbeThreshold) }
	// The R5 member RELIGHT clock resync defaults to the real vsock-backed groupclock
	// (port-1024 length-prefixed JSON, host-wall-clock source) unless a test injects a
	// fake. A member relight FAILS when the read-back is more than one second off.
	if s.groupClock == nil {
		s.groupClock = &realGroupClock{}
	}
	s.budget = newBudget(uint64(opts.Config.DaemonReserveMib))
	s.memHeadroom = s.budget.MemHeadroomMib
	s.memBudget = s.budget.MemBudgetMib
	s.cpuBudget = s.budget.CpuBudgetMillicores
	s.cpuHeadroom = s.budget.CpuHeadroomMillicores
	s.slotCeiling = s.budget.SlotCeiling
	s.statefulResolveTimeout = defaultStatefulResolveTimeout
	// Re-seed the serving-images inventory from disk so a daemon restart re-discovers
	// the cold-boot handler artifacts it built before (mirroring the banked-snapshot
	// rescan). Only when serving is configured; task/session-only builds skip it.
	if opts.ServingDriver != nil {
		for _, a := range opts.ServingDriver.ScanServingHandlerArtifacts() {
			s.servingImage.add(servingImageEntry{
				baseKey: a.BaseKey,
				// The disk rescan has no control-plane binding, so recover the workload
				// from the base-key prefix (same as ReconcileBasesFromDisk) so NodeStatus
				// keys serving_image_ref by workload immediately, not only after a rebuild.
				workload:        workloadFromBaseKey(a.BaseKey),
				handlerPath:     a.Path,
				runtimeImageRef: a.RuntimeImageRef,
				sizeBytes:       a.SizeBytes,
			})
		}
	}
	// Load the last-synced workload registry from NVMe and mark it STALE (ADR
	// embervm/012, never warm-to-dead): a restarted daemon serves the warm pool it
	// already knew from the cached table while it waits for the control plane to
	// reconnect and replay, but readiness stays gated on the FIRST live SyncRegistry
	// so no NEW work is admitted against a stale table. A missing or corrupt cache
	// boots an empty registry (persistCache/loadCache never crash-loop).
	if s.registry.loadCache() {
		logger.Info("workload registry loaded from cache (stale until first live sync)", "entries", s.registry.count())
	}
	// The fetch timeout is the per-attempt bound; a per-request context (below)
	// enforces the same budget so a slow filer cannot pin a build.
	s.httpClient = &http.Client{Timeout: opts.Config.ArchiveFetchTimeout}
	return s
}

var _ nodev1.NodeServiceServer = (*Server)(nil)

// resolveImage resolves a workload's node-side image identity (rootfs path,
// harness init) for a BuildBase. It consults the CONTROL-PLANE-PUSHED workload
// registry first (keyed by workload, carrying rootfs_ref + harness_init from the
// SyncRegistry entry), which is the artifact-decoupling authority now that
// EMBERVM_NODED_IMAGES is retired. It falls back to the legacy image_ref-keyed
// cfg.Images table (always empty after Phase 2, but kept so the resolution is a
// superset and a test that still seeds cfg.Images keeps working). Returns
// (identity, true) when either source knows the workload/image, (zero, false)
// otherwise, which BuildBase maps to FAILED_PRECONDITION.
func (s *Server) resolveImage(workload, imageRef string) (config.Image, bool) {
	// Prefer the workload-keyed entry when it carries a rootfs (the authoritative
	// per-workload identity). Fall back to the image_ref join (the entry whose
	// ImageRef matches this BuildBase's ref), which is the stable bridge for a
	// workload whose CR name the entry is keyed by but whose BuildBase names the
	// image by ref. Finally fall back to the legacy cfg.Images table (empty after
	// Phase 2, kept so a test that seeds it still resolves).
	if e, ok := s.registry.get(workload); ok && e.RootfsRef != "" {
		return config.Image{RootfsPath: e.RootfsRef, HarnessInit: e.HarnessInit}, true
	}
	if e, ok := s.registry.getByImageRef(imageRef); ok {
		return config.Image{RootfsPath: e.RootfsRef, HarnessInit: e.HarnessInit}, true
	}
	img, ok := s.cfg.Images[imageRef]
	return img, ok
}

// resolveImageByRef resolves a RUNTIME image identity strictly by its image_ref
// (rootfs path + harness init), for the cold-boot paths that name a runtime image
// directly rather than a workload: serving-fresh (the serving-image inventory's
// runtimeImageRef), stateful cold-boot (the base's imageDigest, which IS the
// runtime ref), and a composite group member FRESH (member.image_ref). These are
// NOT keyed by the serving/stateful/group WORKLOAD (a zip-lane serving workload's
// own registry entry carries no image_ref, and a composite CR carries multiple
// member images under one workload), so they must resolve by the runtime ref
// through the pushed registry's image_ref index, falling back to the legacy
// cfg.Images table (empty after Phase 2, kept for tests that seed it).
func (s *Server) resolveImageByRef(imageRef string) (config.Image, bool) {
	// getByImageRef already skips empty-RootfsRef entries (tag-skew guard), but
	// assert the invariant here too so this resolution boundary never yields an
	// empty rootfs path to a cold boot, mirroring the by-workload guard above.
	if e, ok := s.registry.getByImageRef(imageRef); ok && e.RootfsRef != "" {
		return config.Image{RootfsPath: e.RootfsRef, HarnessInit: e.HarnessInit}, true
	}
	img, ok := s.cfg.Images[imageRef]
	return img, ok
}

// imageProvisioned reports whether a runtime image_ref is still provisioned on
// this node (the pushed registry knows it by image_ref, or the legacy cfg.Images
// table does). It gates the NodeStatus capacity filters: a READY base or a
// serving image whose runtime rootfs is no longer provisioned is NOT advertised,
// so the control plane never places a wake that would FAILED_PRECONDITION at
// cold-boot resolution. This mirrors resolveImageByRef's resolution exactly, so
// "advertised" and "cold-bootable" stay in lockstep. Crucially, with the pushed
// registry as the source, an empty cfg.Images (the Phase 2 prod condition) no
// longer wrongly hides every base: a base whose runtime the control plane pushed
// is provisioned and reported.
func (s *Server) imageProvisioned(imageRef string) bool {
	_, ok := s.resolveImageByRef(imageRef)
	return ok
}

// refuseIfStale rejects an admission RPC that would place NEW work while the
// workload registry is STALE (a boot-cache load with no live SyncRegistry yet,
// ADR embervm/012 never-warm-to-dead). A stale daemon serves EXISTING warmth but
// must admit NO new work: the control plane dials pods directly with
// publishNotReadyAddresses, so the /readyz gate does NOT protect this path, and a
// stale daemon otherwise advertises full placeable capacity (its warm bases were
// adopted from disk and imageProvisioned is true from the stale cache). This is
// the daemon-side backstop that makes "serve existing warmth, never admit new
// work" airtight. WARM-serving paths (Assign to an already-primed VM, relight/
// restore of an existing base, a Prime refill of an already-warm workload) do NOT
// call this, so existing warmth keeps flowing. Returns nil when not stale.
func (s *Server) refuseIfStale(what string) error {
	if s.registry.isStale() {
		return status.Errorf(codes.FailedPrecondition, "noded: registry stale, awaiting live sync (refusing %s)", what)
	}
	return nil
}

// hasPrimedForWorkload reports whether the task pool already holds at least one
// primed VM for a workload. A Prime for a workload with existing warmth is a
// refill (allowed while stale); a Prime for a workload with none is cold
// placement (refused while stale).
func (s *Server) hasPrimedForWorkload(workload string) bool {
	primed, _ := s.vms.capacity()
	return len(primed[workload]) > 0
}

// ---- BuildBase -------------------------------------------------------------

// BuildBase resolves the image to its node-side rootfs, cold-boots a guest,
// health-gates it on the ready path, and snapshots it into a base bundle. It is
// idempotent per (image_ref, workload_revision): a repeat call for an already
// READY base returns the existing snapshot_ref without rebuilding.
func (s *Server) BuildBase(ctx context.Context, req *nodev1.BuildBaseRequest) (*nodev1.BuildBaseResponse, error) {
	if s.isDraining() {
		return nil, status.Error(codes.Unavailable, "noded: draining")
	}
	// A stale registry (boot cache, no live sync yet) admits no new work: a base
	// build is the coldest possible placement.
	if err := s.refuseIfStale("BuildBase"); err != nil {
		return nil, err
	}
	if s.newBuildDriver == nil {
		return nil, status.Error(codes.Unimplemented, "noded: base building not configured")
	}
	if req.GetZip() != nil {
		return s.buildBaseZip(ctx, req)
	}
	return s.buildBaseImage(ctx, req)
}

// buildBaseImage is the original IMAGE-lane BuildBase: resolve image_ref to a
// node-side rootfs, cold-boot, health-gate, snapshot. Idempotent per (image_ref,
// workload_revision).
func (s *Server) buildBaseImage(ctx context.Context, req *nodev1.BuildBaseRequest) (*nodev1.BuildBaseResponse, error) {
	imageRef := req.GetImageRef()
	if imageRef == "" {
		return nil, status.Error(codes.InvalidArgument, "noded: image_ref required")
	}
	workload := req.GetTrace().GetWorkload()
	img, ok := s.resolveImage(workload, imageRef)
	if !ok {
		return nil, status.Errorf(codes.FailedPrecondition, "noded: image %q (workload %q) not provisioned on this node", imageRef, workload)
	}
	baseKey := baseKeyFor(workload, imageRef, req.GetWorkloadRevision(), s.cfg.CpuVendor)
	// The control plane records the resolved image identity; without an OCI pull
	// the ref IS the identity for R0 (deploys are digest-pinned upstream). The image
	// lane carries no archive and never hydrates (nil archive).
	return s.driveBuild(ctx, req, baseKey, imageRef, img, nil)
}

// buildBaseZip is the R1 ZIP-lane BuildBase: fetch the adopter's archive from the
// SeaweedFS read path, verify its sha256 into memory, cold-boot the runtime image
// with NO archive drive, then hydrate the guest shim over vsock (POST
// /shim/hydrate) with the clean bytes so it unpacks and imports the handler before
// the snapshot. The snapshot is memory + rootfs only, with no archive backing
// file, so it is self-contained and portable (shippable, restorable on any node).
// Idempotent per (runtime image digest, archive sha256).
func (s *Server) buildBaseZip(ctx context.Context, req *nodev1.BuildBaseRequest) (*nodev1.BuildBaseResponse, error) {
	zip := req.GetZip()
	runtimeRef := zip.GetRuntimeImageRef()
	if runtimeRef == "" {
		return nil, status.Error(codes.InvalidArgument, "noded: zip.runtime_image_ref required")
	}
	if zip.GetArchiveUrl() == "" {
		return nil, status.Error(codes.InvalidArgument, "noded: zip.archive_url required")
	}
	if zip.GetArchiveSha256() == "" {
		return nil, status.Error(codes.InvalidArgument, "noded: zip.archive_sha256 required")
	}
	workload := req.GetTrace().GetWorkload()
	img, ok := s.resolveImage(workload, runtimeRef)
	if !ok {
		return nil, status.Errorf(codes.FailedPrecondition, "noded: runtime image %q (workload %q) not provisioned on this node", runtimeRef, workload)
	}
	// Idempotency key is (runtime image digest, archive sha256): a re-registration
	// of the SAME archive on the SAME runtime is a no-op hit. The runtime ref is
	// the digest for R0 (deploys are digest-pinned upstream), mirroring the image
	// lane where image_ref IS the identity.
	imageDigest := runtimeRef
	baseKey := baseKeyForZip(workload, imageDigest, zip.GetArchiveSha256(), s.cfg.CpuVendor)

	// Short-circuit on an already-built base BEFORE fetching the archive: an
	// idempotent repeat must not re-download or re-attach.
	if existing, ok := s.bases.get(baseKey); ok && existing.state == nodev1.BaseBuildState_BASE_BUILD_STATE_READY {
		return &nodev1.BuildBaseResponse{
			SnapshotRef:   existing.snapshotRef,
			ImageDigest:   existing.imageDigest,
			BaseSizeBytes: uint64(existing.sizeBytes),
			Arch:          s.cfg.Arch,
			AlreadyBuilt:  true,
		}, nil
	}

	// Fetch + verify into memory BEFORE claiming the build guest so a bad archive
	// fails cheaply (no cold boot wasted). noded handles opaque bytes only; the
	// guest shim owns unpack and zip-slip defence. The bytes never touch disk: they
	// are hydrated over vsock after boot, so there is no block file to leak or to
	// pin the snapshot to.
	archive, err := s.fetchAndVerifyArchive(ctx, zip.GetArchiveUrl(), zip.GetArchiveSha256())
	if err != nil {
		return nil, err // already a *status.Error with the right code
	}

	return s.driveBuild(ctx, req, baseKey, imageDigest, img, archive)
}

// driveBuild is the shared tail of both lanes: the idempotency short-circuit,
// per-key build serialization, cold boot + health-gate + snapshot, and the
// ready/fail bookkeeping. archive, when non-nil (zip lane), is hydrated into the
// build guest over vsock (POST /shim/hydrate) after boot and before the readiness
// wait; the image lane passes nil and never hydrates.
func (s *Server) driveBuild(ctx context.Context, req *nodev1.BuildBaseRequest, baseKey, imageDigest string, img config.Image, archive []byte) (*nodev1.BuildBaseResponse, error) {
	workload := req.GetTrace().GetWorkload()
	readyPath := req.GetReadyPath()
	if readyPath == "" {
		readyPath = defaultReadyPath
	}

	// Idempotency: an already-built base is a no-op hit.
	if existing, ok := s.bases.get(baseKey); ok && existing.state == nodev1.BaseBuildState_BASE_BUILD_STATE_READY {
		return &nodev1.BuildBaseResponse{
			SnapshotRef:   existing.snapshotRef,
			ImageDigest:   existing.imageDigest,
			BaseSizeBytes: uint64(existing.sizeBytes),
			Arch:          s.cfg.Arch,
			AlreadyBuilt:  true,
		}, nil
	}
	// Serialize builds per key. A concurrent duplicate is rejected rather than
	// double-booting a build guest.
	if !s.bases.beginBuild(baseKey, workload, readyPath) {
		return nil, status.Errorf(codes.FailedPrecondition, "noded: base build already in progress for %q", baseKey)
	}
	s.signalChange()

	// Register the build so a drain can finish-or-abort it inside the budget. The
	// build runs under buildCtx (a cancelable child of the RPC ctx): a drain past
	// the deadline cancels it, runBuild's deferred teardown destroys the build VM,
	// and failBuild below leaves the base re-queueable.
	buildCtx, cancelBuild := context.WithCancel(ctx)
	defer cancelBuild()
	s.registerBuild(baseKey, cancelBuild)
	defer s.finishBuild(baseKey)

	harnessInit := img.HarnessInit
	if harnessInit == "" {
		harnessInit = s.cfg.HarnessInit
	}
	res := req.GetResources()
	bd := s.newBuildDriver(BuildDriverSpec{
		RootfsPath:  img.RootfsPath,
		HarnessInit: harnessInit,
		VCPUs:       int(res.GetVcpus()),
		MemMib:      int(res.GetMemMib()),
	})

	sizeBytes, err := s.runBuild(buildCtx, bd, baseKey, readyPath, archive)
	if err != nil {
		s.bases.failBuild(baseKey, err.Error())
		s.signalChange()
		return nil, status.Errorf(codes.FailedPrecondition, "noded: build base %q: %v", baseKey, err)
	}
	s.bases.readyBuild(baseKey, workload, imageDigest, readyPath, sizeBytes)
	// Persist the runtime image ref alongside the snapshot so a daemon restart can
	// restore it (ReconcileBasesFromDisk). The base dir name is <workload>__<sig>
	// and does NOT encode the runtime image, but a stateful cold boot resolves the
	// rootfs via base.imageDigest -> cfg.Images, so a base adopted from disk without
	// it is unbootable. Best-effort: a missing ref file just forces a rebuild.
	s.writeBaseImageRef(baseKey, imageDigest)

	// Serving base (R3, D-R3.11.2): additionally persist a cold-boot-readable handler
	// artifact from the SAME verified archive bytes noded already holds, so a serving
	// cold boot (which cannot resume the vsock-only memory snapshot to get the handler)
	// can attach it as a read-only drive and import the handler off disk. This is
	// strictly ADDITIVE: the base memory snapshot above is still produced (the task
	// lane needs it if the workload is also task-class), and a non-serving build never
	// enters this branch. Only the zip lane carries an archive; an image-lane serving
	// base has no handler zip and is left for a later rung.
	servingImageRef := ""
	if req.GetServing() && archive != nil && s.servingDriver != nil {
		path, artifactBytes, werr := s.servingDriver.WriteServingHandlerArtifact(baseKey, imageDigest, archive)
		if werr != nil {
			// A handler-artifact write failure fails the build: a serving base that
			// reports READY without a usable cold-boot artifact would place and then
			// FAILED_PRECONDITION at StartServing, which is worse than failing here.
			s.bases.failBuild(baseKey, werr.Error())
			s.signalChange()
			return nil, status.Errorf(codes.FailedPrecondition, "noded: write serving handler artifact for %q: %v", baseKey, werr)
		}
		s.servingImage.add(servingImageEntry{
			baseKey:         baseKey,
			workload:        workload,
			handlerPath:     path,
			runtimeImageRef: imageDigest, // the zip lane's imageDigest IS the runtime ref
			sizeBytes:       artifactBytes,
		})
		servingImageRef = baseKey
	}

	s.signalChange()
	return &nodev1.BuildBaseResponse{
		SnapshotRef:     baseKey,
		ImageDigest:     imageDigest,
		BaseSizeBytes:   uint64(sizeBytes),
		Arch:            s.cfg.Arch,
		AlreadyBuilt:    false,
		ServingImageRef: servingImageRef,
	}, nil
}

// runBuild cold-boots a build guest, (for the zip lane) hydrates it with the
// archive over vsock, waits for readiness, snapshots it into the base bundle, and
// always discards the build VM (the base lives in the bundle). The build guest is
// claimed with only its rootfs drive; the archive, when non-nil, crosses over
// vsock as a clean HTTP body, so the snapshot carries no archive backing file.
//
// Sequence: Claim (cold boot) -> Prime (drain the vsock path via /shim/healthz)
// -> Hydrate (POST the archive; zip lane only) -> WaitReady (/shim/ready flips 200
// only after the shim unpacks + imports the handler) -> SnapshotBase.
func (s *Server) runBuild(ctx context.Context, bd BuildDriver, baseKey, readyPath string, archive []byte) (int64, error) {
	spec := substrate.ClaimSpec{Arch: s.cfg.Arch, ThreadID: newID("build")}
	h, err := bd.Claim(ctx, spec)
	if err != nil {
		return 0, fmt.Errorf("cold boot: %w", err)
	}
	defer func() {
		if rerr := bd.Release(context.Background(), h); rerr != nil {
			s.logger.Warn("noded: release build guest", "base", baseKey, "err", rerr)
		}
		if rerr := bd.RemoveBundle(h.ThreadID); rerr != nil {
			s.logger.Warn("noded: remove build bundle", "base", baseKey, "err", rerr)
		}
	}()

	uds := bd.VsockUDSPath(h.ThreadID)

	// Zip lane: prime the vsock path open, then hydrate the shim with the archive
	// BEFORE the readiness wait. The shim serves /shim/healthz immediately but stays
	// not-ready until this hydrate unpacks the archive and imports the handler, so
	// WaitReady below is what health-gates the imported handler. A hydrate failure
	// (bad zip, zip-slip, import error) fails the build: no snapshot.
	if archive != nil {
		primeCtx, cancelPrime := context.WithTimeout(ctx, s.cfg.BootReadyTimeout)
		if perr := s.transport.Prime(primeCtx, uds); perr != nil {
			s.logger.Warn("noded: build vsock prime did not complete; hydrate will retry the dial", "base", baseKey, "err", perr)
		}
		cancelPrime()

		hydrateCtx, cancelHydrate := context.WithTimeout(ctx, s.cfg.BootReadyTimeout)
		herr := s.transport.Hydrate(hydrateCtx, uds, archive)
		cancelHydrate()
		if herr != nil {
			return 0, fmt.Errorf("hydrate archive: %w", herr)
		}
	}

	readyCtx, cancel := context.WithTimeout(ctx, s.cfg.BootReadyTimeout)
	defer cancel()
	if err := s.transport.WaitReady(readyCtx, uds, readyPath); err != nil {
		return 0, fmt.Errorf("guest readiness: %w", err)
	}
	ref, err := bd.SnapshotBase(ctx, h, baseKey)
	if err != nil {
		return 0, fmt.Errorf("snapshot: %w", err)
	}
	return ref.SizeBytes, nil
}

// fetchAndVerifyArchive GETs the zip archive from archiveURL over the pod network,
// reads it into memory bounded by the size cap, and verifies the bytes against
// wantSha256. The clean bytes are returned for vsock hydration; nothing is written
// to disk, so there is no block file to leak and the resulting snapshot has no
// archive backing dependency. On ANY error a *status.Error with the right code is
// returned:
//   - InvalidArgument for a malformed wantSha256,
//   - FailedPrecondition for a fetch failure, an over-size archive, or a sha256
//     mismatch (a corrupted or swapped archive must never reach a base).
//
// noded NEVER unpacks or inspects the bytes: zip-slip/bomb defence lives in the
// disposable guest's shim (Task 5).
func (s *Server) fetchAndVerifyArchive(ctx context.Context, archiveURL, wantSha256 string) ([]byte, error) {
	wantSha256 = strings.ToLower(strings.TrimSpace(wantSha256))
	if _, err := hex.DecodeString(wantSha256); err != nil || len(wantSha256) != 64 {
		return nil, status.Errorf(codes.InvalidArgument, "noded: zip.archive_sha256 must be 64 hex chars, got %q", wantSha256)
	}

	// Bound the fetch by the configured timeout even if the caller's context is
	// longer, so a hung filer cannot pin a build past the budget.
	fetchCtx := ctx
	if s.cfg.ArchiveFetchTimeout > 0 {
		var cancel context.CancelFunc
		fetchCtx, cancel = context.WithTimeout(ctx, s.cfg.ArchiveFetchTimeout)
		defer cancel()
	}
	req, err := http.NewRequestWithContext(fetchCtx, http.MethodGet, archiveURL, nil)
	if err != nil {
		return nil, status.Errorf(codes.FailedPrecondition, "noded: build zip archive request: %v", err)
	}
	resp, err := s.httpClient.Do(req)
	if err != nil {
		return nil, status.Errorf(codes.FailedPrecondition, "noded: fetch zip archive: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, status.Errorf(codes.FailedPrecondition, "noded: fetch zip archive: unexpected status %d", resp.StatusCode)
	}

	// Cap the read so a runaway or malicious URL cannot exhaust daemon memory;
	// exceeding the cap fails the build (an over-size archive is not honoured).
	limit := s.cfg.ArchiveMaxBytes
	if limit <= 0 {
		limit = defaultArchiveMaxBytes
	}
	archive, err := io.ReadAll(io.LimitReader(resp.Body, limit+1))
	if err != nil {
		return nil, status.Errorf(codes.FailedPrecondition, "noded: read zip archive: %v", err)
	}
	if int64(len(archive)) > limit {
		return nil, status.Errorf(codes.FailedPrecondition, "noded: zip archive exceeds %d-byte cap", limit)
	}
	sum := sha256.Sum256(archive)
	gotSha256 := hex.EncodeToString(sum[:])
	if gotSha256 != wantSha256 {
		return nil, status.Errorf(codes.FailedPrecondition, "noded: zip archive sha256 mismatch: got %s want %s", gotSha256, wantSha256)
	}
	return archive, nil
}

// ---- Prime -----------------------------------------------------------------

// Prime restores one pristine VM from snapshot_ref, waits for the guest ready
// path, and parks it idle. It enforces the node-level live-VM backstop cap.
func (s *Server) Prime(ctx context.Context, req *nodev1.PrimeRequest) (*nodev1.PrimeResponse, error) {
	if s.isDraining() {
		return nil, status.Error(codes.Unavailable, "noded: draining")
	}
	ref := req.GetSnapshotRef()
	if ref == "" {
		return nil, status.Error(codes.InvalidArgument, "noded: snapshot_ref required")
	}
	if s.cfg.MaxLiveVMs > 0 && s.liveVMCount() >= s.cfg.MaxLiveVMs {
		return nil, status.Errorf(codes.ResourceExhausted, "noded: node live-VM cap %d reached", s.cfg.MaxLiveVMs)
	}
	base, ok := s.bases.get(ref)
	if !ok {
		return nil, status.Errorf(codes.FailedPrecondition, "noded: unknown snapshot_ref %q", ref)
	}
	if base.state != nodev1.BaseBuildState_BASE_BUILD_STATE_READY {
		return nil, status.Errorf(codes.FailedPrecondition, "noded: base %q not ready (state %s)", ref, base.state)
	}
	// While the registry is stale, a Prime that would be the FIRST warm VM for a
	// workload is cold placement (refused); a Prime that refills an already-warm
	// workload keeps existing warmth topped up (allowed). This preserves "serve
	// existing warmth, never admit new work" without the daemon knowing CP intent.
	if !s.hasPrimedForWorkload(base.workload) {
		if err := s.refuseIfStale("Prime (no existing warm pool for workload)"); err != nil {
			return nil, err
		}
	}
	readyPath := base.readyPath
	if readyPath == "" {
		readyPath = defaultReadyPath
	}

	spec := substrate.ClaimSpec{
		Arch:     s.cfg.Arch,
		ThreadID: newID("vm"),
		BaseSnapshotRef: substrate.SnapshotRef{
			ID:     ref,
			Node:   s.cfg.Node,
			Arch:   s.cfg.Arch,
			Vendor: s.cfg.CpuVendor,
			Base:   true,
		},
	}
	h, err := s.driver.Claim(ctx, spec)
	if err != nil {
		return nil, status.Errorf(codes.FailedPrecondition, "noded: restore snapshot %q: %v", ref, err)
	}
	uds := s.driver.VsockUDSPath(h.ThreadID)

	// Shake out Firecracker's post-restore vsock RX-queue race off the readiness
	// path (best-effort), then health-gate on the short restore budget.
	primeCtx, cancelPrime := context.WithTimeout(ctx, s.cfg.RestoreReadyTimeout)
	if perr := s.transport.Prime(primeCtx, uds); perr != nil {
		s.logger.Warn("noded: vsock prime did not complete; readiness poll will retry past the race", "vm", h.ID, "err", perr)
	}
	cancelPrime()

	readyCtx, cancelReady := context.WithTimeout(ctx, s.cfg.RestoreReadyTimeout)
	readyErr := s.transport.WaitReady(readyCtx, uds, readyPath)
	cancelReady()
	if readyErr != nil {
		// A restore that never health-gates is discarded; the control plane
		// degrades to a slower boot, never to incorrect behavior.
		s.reap(h, func() {})
		return nil, status.Errorf(codes.FailedPrecondition, "noded: restored guest not ready: %v", readyErr)
	}

	s.vms.add(&vmEntry{
		id:           h.ID,
		workload:     base.workload,
		snapshotRef:  ref,
		handle:       h,
		egressCancel: func() {},
		state:        vmPrimed,
	})
	s.signalChange()
	return &nodev1.PrimeResponse{VmId: h.ID}, nil
}

// ---- Assign ----------------------------------------------------------------

// Assign delivers exactly one HTTP task to a primed vm_id over vsock, returns the
// guest response plus usage stats, and DESTROYS the VM regardless of outcome.
// Assign on an unknown/already-used vm_id fails FAILED_PRECONDITION with no side
// effects.
func (s *Server) Assign(ctx context.Context, req *nodev1.AssignRequest) (*nodev1.AssignResponse, error) {
	vmID := req.GetVmId()
	e, ok := s.vms.claimForAssign(vmID)
	if !ok {
		// Unknown or not primed: no VM is touched, no second task runs.
		return nil, status.Errorf(codes.FailedPrecondition, "noded: vm %q not assignable (unknown, already assigned, or destroyed)", vmID)
	}
	// Single-use: destroy the VM after this call regardless of outcome. Reap only
	// if THIS defer is the one that removes the entry from the registry: an out-of-
	// band Destroy racing this Assign may remove-and-reap first (killing the VM
	// mid-round-trip, which just fails this Assign), and map removal under the
	// registry lock guarantees exactly one caller gets the non-nil entry, so the VM
	// is never Released twice.
	defer func() {
		if removed := s.vms.remove(vmID); removed != nil {
			s.reap(removed.handle, removed.egressCancel)
		}
		s.signalChange()
	}()

	gr := req.GetRequest()
	method := gr.GetMethod()
	if method == "" {
		method = http.MethodPost
	}
	path := gr.GetPath()
	if path == "" {
		path = defaultInvokePath
	}
	timeout := time.Duration(req.GetTimeoutMs()) * time.Millisecond
	if timeout <= 0 {
		timeout = defaultAssignTimeout
	}
	rtCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	httpReq, err := http.NewRequestWithContext(rtCtx, method, "http://vsock"+path, bytes.NewReader(gr.GetBody()))
	if err != nil {
		return nil, status.Errorf(codes.InvalidArgument, "noded: build guest request: %v", err)
	}
	for k, v := range gr.GetHeaders() {
		httpReq.Header.Set(k, v)
	}

	uds := s.driver.VsockUDSPath(e.handle.ThreadID)
	t0 := time.Now()
	resp, err := s.transport.RoundTrip(rtCtx, uds, httpReq)
	if err != nil {
		if errors.Is(err, context.DeadlineExceeded) || rtCtx.Err() == context.DeadlineExceeded {
			return nil, status.Errorf(codes.DeadlineExceeded, "noded: guest did not respond within %s", timeout)
		}
		return nil, status.Errorf(codes.Unavailable, "noded: guest round-trip: %v", err)
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(io.LimitReader(resp.Body, maxGuestResponseBytes))
	if err != nil {
		return nil, status.Errorf(codes.Unavailable, "noded: read guest response: %v", err)
	}
	wallMs := time.Since(t0).Milliseconds()

	// Sample the guest's whole-invocation /proc counters while it is still alive
	// (the deferred reap kills it after this returns). Best-effort.
	usage := &nodev1.UsageStats{WallMs: wallMs}
	if stats, serr := s.driver.Stats(e.handle); serr == nil {
		usage.CpuMs = stats.CPUMillis
		usage.PeakRssMib = stats.PeakRSSMib
	} else {
		s.logger.Debug("noded: guest stats unavailable", "vm", vmID, "err", serr)
	}

	return &nodev1.AssignResponse{
		Response: &nodev1.GuestResponse{
			StatusCode: uint32(resp.StatusCode),
			Headers:    flattenHeaders(resp.Header),
			Body:       body,
		},
		Usage: usage,
	}, nil
}

// ---- Destroy ---------------------------------------------------------------

// Destroy reaps a primed or wedged VM out of band. Idempotent: an unknown or
// already-destroyed vm_id returns OK (the desired end-state already holds).
func (s *Server) Destroy(_ context.Context, req *nodev1.DestroyRequest) (*nodev1.DestroyResponse, error) {
	if e := s.vms.remove(req.GetVmId()); e != nil {
		s.reap(e.handle, e.egressCancel)
		s.signalChange()
		return &nodev1.DestroyResponse{}, nil
	}
	// A session VM destroyed out of band (e.g. the control plane tearing down a
	// suspect or terminal session) lives in the distinct session registry.
	if e := s.sessionVMs.remove(req.GetVmId()); e != nil {
		s.reap(e.handle, func() {})
		s.signalChange()
		return &nodev1.DestroyResponse{}, nil
	}
	// A serving VM destroyed out of band lives in the distinct serving registry; its
	// probe loop is stopped and its tap released alongside the reap.
	if e := s.servingVMs.remove(req.GetVmId()); e != nil {
		e.probe.Stop()
		s.reapServing(e.handle, e.ip)
		s.signalChange()
	}
	return &nodev1.DestroyResponse{}, nil
}

// liveVMCount is the node-wide count of live microVMs for the backstop cap:
// task-pool VMs plus live session VMs. The fcvm driver's own live map is the
// authority (both Prime and RestoreSession claim through the same driver), so its
// LiveCount already sums both; this helper names the invariant at the call site
// (session VMs count against max_live_vms exactly like task VMs).
func (s *Server) liveVMCount() int {
	return s.driver.LiveCount()
}

// ---- Session verbs (R2) ----------------------------------------------------

// adoptPrimedSession promotes a primed VM from the task registry into the session
// registry on a session's FIRST invoke. The session-create path primes/claims the
// VM through the shared warm pool (Prime always lands it in the task registry), so
// something has to move it into the session registry before SessionAssign can serve
// it; doing that lazily on first invoke (rather than via a separate create-time
// verb) keeps create a single round-trip and mirrors how Relight registers a relit
// VM. It returns the new session entry with the in-flight guard ALREADY held (so a
// racing second SessionAssign gets rejected until this one finishes), or (nil,false)
// when vmID is not a primed task VM (unknown, already assigned/adopted, destroyed),
// which SessionAssign maps to FAILED_PRECONDITION. The physical VM is already a
// session-base VM (restored from the session workload's base, running the persistent
// kernel); only its registry bookkeeping changes.
func (s *Server) adoptPrimedSession(vmID, sessionID, workload string) (*sessionEntry, bool) {
	ve, ok := s.vms.claimForSession(vmID, workload)
	if !ok {
		return nil, false
	}
	se := &sessionEntry{
		vmID:        ve.id,
		sessionID:   sessionID,
		workload:    workload,
		snapshotRef: ve.snapshotRef,
		handle:      ve.handle,
		inFlight:    true, // held for the SessionAssign that triggered this adoption
	}
	s.sessionVMs.add(se)
	s.signalChange() // the VM moved primed -> session-live; refresh NodeStatus
	return se, true
}

// SessionAssign delivers exactly one HTTP task to a LIVE session vm_id over vsock
// and returns the guest response plus usage WITHOUT destroying the VM (the
// opposite of Assign's single-use destroy tail: a session survives across
// invocations). A per-vm in-flight guard serializes calls: a concurrent
// SessionAssign or Bank on the same vm_id is rejected FAILED_PRECONDITION. An
// unknown, task-class, or mid-bank vm_id is likewise FAILED_PRECONDITION. On a
// guest timeout the VM is LEFT ALIVE and the response carries suspect=true so the
// control plane decides whether to destroy it.
func (s *Server) SessionAssign(ctx context.Context, req *nodev1.SessionAssignRequest) (*nodev1.SessionAssignResponse, error) {
	vmID := req.GetVmId()
	e, ok := s.sessionVMs.beginInFlight(vmID)
	if !ok {
		// Not (yet) in the session registry. A freshly CREATED session's VM was
		// primed/claimed through the shared warm pool, so it still lives in the task
		// registry on its FIRST invoke; adopt it into the session registry now
		// (mirroring how Relight registers a relit VM). A relit session's VM is
		// already here, so this branch only runs once per session's lifetime. A
		// genuinely unknown, already-adopted, mid-bank, or in-flight vm_id still
		// fails FAILED_PRECONDITION.
		adopted, ok2 := s.adoptPrimedSession(vmID, req.GetSessionId(), req.GetTrace().GetWorkload())
		if !ok2 {
			return nil, status.Errorf(codes.FailedPrecondition, "noded: session vm %q not assignable (unknown, task-class, mid-bank, or a call is already in flight)", vmID)
		}
		e = adopted
	}
	// The VM SURVIVES: clear the in-flight guard on return, never reap.
	defer e.endInFlight()

	gr := req.GetRequest()
	method := gr.GetMethod()
	if method == "" {
		method = http.MethodPost
	}
	path := gr.GetPath()
	if path == "" {
		path = defaultInvokePath
	}
	timeout := time.Duration(req.GetTimeoutMs()) * time.Millisecond
	if timeout <= 0 {
		timeout = defaultAssignTimeout
	}
	rtCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	httpReq, err := http.NewRequestWithContext(rtCtx, method, "http://vsock"+path, bytes.NewReader(gr.GetBody()))
	if err != nil {
		return nil, status.Errorf(codes.InvalidArgument, "noded: build guest request: %v", err)
	}
	for k, v := range gr.GetHeaders() {
		httpReq.Header.Set(k, v)
	}

	uds := s.driver.VsockUDSPath(e.handle.ThreadID)
	t0 := time.Now()
	resp, err := s.transport.RoundTrip(rtCtx, uds, httpReq)
	if err != nil {
		// Timeout or transport fault: the VM is LEFT ALIVE (unlike Assign). Flag it
		// suspect so the control plane can decide to destroy it, and map a deadline to
		// DEADLINE_EXCEEDED so the caller can distinguish it.
		if errors.Is(err, context.DeadlineExceeded) || rtCtx.Err() == context.DeadlineExceeded {
			return nil, status.Errorf(codes.DeadlineExceeded, "noded: session guest did not respond within %s (vm left alive, suspect)", timeout)
		}
		return &nodev1.SessionAssignResponse{
			Response: &nodev1.GuestResponse{StatusCode: uint32(http.StatusBadGateway)},
			Usage:    &nodev1.UsageStats{WallMs: time.Since(t0).Milliseconds()},
			Suspect:  true,
		}, nil
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(io.LimitReader(resp.Body, maxGuestResponseBytes))
	if err != nil {
		return nil, status.Errorf(codes.Unavailable, "noded: read session guest response: %v", err)
	}
	wallMs := time.Since(t0).Milliseconds()

	usage := &nodev1.UsageStats{WallMs: wallMs}
	if stats, serr := s.driver.Stats(e.handle); serr == nil {
		usage.CpuMs = stats.CPUMillis
		usage.PeakRssMib = stats.PeakRSSMib
	} else {
		s.logger.Debug("noded: session guest stats unavailable", "vm", vmID, "err", serr)
	}

	return &nodev1.SessionAssignResponse{
		Response: &nodev1.GuestResponse{
			StatusCode: uint32(resp.StatusCode),
			Headers:    flattenHeaders(resp.Header),
			Body:       body,
		},
		Usage: usage,
	}, nil
}

// Bank pauses a live session VM, writes a full self-contained snapshot bundle
// (memfile + rootfs state, the same format bases use) under the sessions/ prefix,
// destroys the VM, and returns the opaque {snapshot_ref, size_bytes}. It refuses
// FAILED_PRECONDITION while a SessionAssign is in flight on that vm_id (the
// control plane's session process guarantees ordering; this guard is the
// daemon-side backstop). The produced snapshot enters the in-memory banked
// inventory so it is reported in NodeStatus even before the next disk rescan.
func (s *Server) Bank(ctx context.Context, req *nodev1.BankRequest) (*nodev1.BankResponse, error) {
	if s.sessionDriver == nil {
		return nil, status.Error(codes.Unimplemented, "noded: session banking not configured")
	}
	vmID := req.GetVmId()
	// Take the in-flight guard: a Bank cannot proceed while a SessionAssign holds the
	// VM, and while the guard is held no new SessionAssign can start.
	e, ok := s.sessionVMs.beginInFlight(vmID)
	if !ok {
		return nil, status.Errorf(codes.FailedPrecondition, "noded: session vm %q not bankable (unknown, task-class, or a call is already in flight)", vmID)
	}
	// The Bank destroys the VM either way: on success the entry is removed and the
	// paused VM reaped below; on failure SnapshotSession has already torn the VM
	// down (a bank is destructive), so drop the now-dead registry entry rather than
	// leave it misreporting session capacity until the control plane reaps it.
	snapshotRef := newID("sess")
	ref, err := s.sessionDriver.SnapshotSession(ctx, e.handle, snapshotRef)
	if err != nil {
		s.sessionVMs.remove(vmID)
		return nil, status.Errorf(codes.FailedPrecondition, "noded: bank session vm %q: %v", vmID, err)
	}
	// Destroy the VM: the session releases its live capacity and holds only disk.
	if removed := s.sessionVMs.remove(vmID); removed != nil {
		s.reap(removed.handle, func() {})
	}
	s.sessionSnap.add(sessionSnapshotEntry{
		snapshotRef:     ref.ID,
		sessionID:       req.GetSessionId(),
		workload:        req.GetTrace().GetWorkload(),
		sizeBytes:       ref.SizeBytes,
		createdAtUnixMs: time.Now().UnixMilli(),
	})
	// Async off-node write-back (R6): the banked bundle is now crash-consistent on
	// disk, so enqueue its export fire-and-forget (never blocking this bank path).
	s.enqueueExport(&nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_SESSION, Workload: req.GetTrace().GetWorkload(), Ref: ref.ID})
	s.signalChange()
	return &nodev1.BankResponse{SnapshotRef: ref.ID, SizeBytes: uint64(ref.SizeBytes)}, nil
}

// Relight restores a VM from a banked session snapshot_ref (the deliver-without-
// destroy sibling of Prime's restore), waits for guest readiness with the same
// vsockhttp WaitReady mechanics Prime uses (150ms attempts, 2s RestoreReadyTimeout),
// and returns the fresh vm_id. After ready it best-effort POSTs the wall-clock
// epoch-ms to the guest /shim/clock so time-dependent code resumes with a correct
// clock; a 404 (a guest without the endpoint) is skipped and logged, never an
// error. FAILED_PRECONDITION if the ref is unknown or unrestorable; the snapshot is
// NEVER deleted on a failed restore (the control plane decides).
func (s *Server) Relight(ctx context.Context, req *nodev1.RelightRequest) (*nodev1.RelightResponse, error) {
	if s.sessionDriver == nil {
		return nil, status.Error(codes.Unimplemented, "noded: session relight not configured")
	}
	if s.isDraining() {
		return nil, status.Error(codes.Unavailable, "noded: draining")
	}
	ref := req.GetSnapshotRef()
	if ref == "" {
		return nil, status.Error(codes.InvalidArgument, "noded: snapshot_ref required")
	}
	if s.cfg.MaxLiveVMs > 0 && s.liveVMCount() >= s.cfg.MaxLiveVMs {
		return nil, status.Errorf(codes.ResourceExhausted, "noded: node live-VM cap %d reached", s.cfg.MaxLiveVMs)
	}
	if !s.sessionSnap.has(ref) {
		return nil, status.Errorf(codes.FailedPrecondition, "noded: unknown session snapshot_ref %q", ref)
	}
	h, err := s.sessionDriver.RestoreSession(ctx, ref)
	if err != nil {
		// The snapshot is left on disk (never deleted on a failed restore).
		return nil, status.Errorf(codes.FailedPrecondition, "noded: relight session snapshot %q: %v", ref, err)
	}
	uds := s.driver.VsockUDSPath(h.ThreadID)

	// Shake out the post-restore vsock RX-queue race, then health-gate on the short
	// restore budget (same mechanics as Prime).
	primeCtx, cancelPrime := context.WithTimeout(ctx, s.cfg.RestoreReadyTimeout)
	if perr := s.transport.Prime(primeCtx, uds); perr != nil {
		s.logger.Warn("noded: session vsock prime did not complete; readiness poll will retry past the race", "vm", h.ID, "err", perr)
	}
	cancelPrime()

	readyCtx, cancelReady := context.WithTimeout(ctx, s.cfg.RestoreReadyTimeout)
	readyErr := s.transport.WaitReady(readyCtx, uds, defaultReadyPath)
	cancelReady()
	if readyErr != nil {
		// A restore that never health-gates is discarded; the snapshot stays on disk
		// for the control plane to decide (never a silent blank VM).
		s.reap(h, func() {})
		return nil, status.Errorf(codes.FailedPrecondition, "noded: relit guest not ready: %v", readyErr)
	}

	// Best-effort guest clock resync: a restored guest's wall clock is frozen at the
	// bank instant, so POST the current epoch-ms to /shim/clock. A 404 (a guest
	// without the endpoint) is skipped and logged, NEVER an error.
	s.resyncGuestClock(ctx, uds, h.ID)

	s.sessionVMs.add(&sessionEntry{
		vmID:        h.ID,
		sessionID:   req.GetSessionId(),
		workload:    req.GetTrace().GetWorkload(),
		snapshotRef: ref,
		handle:      h,
	})
	s.signalChange()
	return &nodev1.RelightResponse{VmId: h.ID}, nil
}

// resyncGuestClock POSTs the current wall-clock epoch-ms to the guest /shim/clock
// endpoint over vsock so a relit guest resumes with a correct clock. It is
// strictly best-effort: any transport error, a non-2xx, or a 404 (a guest build
// without the endpoint) is logged and swallowed, never surfaced. The daemon does
// not fail a relight on a clock-resync miss.
func (s *Server) resyncGuestClock(ctx context.Context, uds, vmID string) {
	rtCtx, cancel := context.WithTimeout(ctx, s.cfg.RestoreReadyTimeout)
	defer cancel()
	body := []byte(strconv.FormatInt(time.Now().UnixMilli(), 10))
	httpReq, err := http.NewRequestWithContext(rtCtx, http.MethodPost, "http://vsock/shim/clock", bytes.NewReader(body))
	if err != nil {
		s.logger.Debug("noded: build clock resync request failed", "vm", vmID, "err", err)
		return
	}
	httpReq.Header.Set("Content-Type", "text/plain")
	resp, err := s.transport.RoundTrip(rtCtx, uds, httpReq)
	if err != nil {
		s.logger.Debug("noded: clock resync round-trip failed (best-effort)", "vm", vmID, "err", err)
		return
	}
	defer resp.Body.Close()
	_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 4<<10))
	if resp.StatusCode == http.StatusNotFound {
		s.logger.Info("noded: guest has no /shim/clock endpoint; skipping clock resync", "vm", vmID)
		return
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		s.logger.Warn("noded: clock resync rejected (best-effort, ignored)", "vm", vmID, "status", resp.StatusCode)
	}
}

// EvictSnapshot deletes a banked session snapshot bundle from node disk. It is
// idempotent (an unknown ref is OK: the desired end-state already holds) and
// refuses FAILED_PRECONDITION while a LIVE session VM relit from this ref is still
// running (evicting a bundle out from under a live relit VM would lose the state
// needed to re-bank it).
func (s *Server) EvictSnapshot(_ context.Context, req *nodev1.EvictSnapshotRequest) (*nodev1.EvictSnapshotResponse, error) {
	ref := req.GetSnapshotRef()
	if ref == "" {
		return nil, status.Error(codes.InvalidArgument, "noded: snapshot_ref required")
	}
	// A serving snapshot ref is evicted exactly like a session ref, just stored under
	// the serving/ prefix (per the PR-1 proto contract: serving reuses EvictSnapshot).
	// Dispatch on which inventory holds the ref BEFORE the session-driver guard so a
	// serving ref evicts even in a build wired only for serving.
	if _, ok := s.servingSnap.get(ref); ok {
		return s.evictServingSnapshot(ref)
	}
	if s.sessionDriver == nil {
		return nil, status.Error(codes.Unimplemented, "noded: session eviction not configured")
	}
	// In-use guard: refuse while a live session VM was relit from this ref.
	for _, e := range s.sessionVMs.snapshotWithRefs() {
		if e.snapshotRef == ref {
			return nil, status.Errorf(codes.FailedPrecondition, "noded: session snapshot %q is in use by live vm %q", ref, e.vmID)
		}
	}
	if err := s.sessionDriver.RemoveSessionBundle(ref); err != nil {
		return nil, status.Errorf(codes.Internal, "noded: evict session snapshot %q: %v", ref, err)
	}
	s.sessionSnap.remove(ref)
	s.signalChange()
	return &nodev1.EvictSnapshotResponse{}, nil
}

// evictServingSnapshot deletes a banked serving snapshot bundle from disk. It mirrors
// the session eviction path exactly: idempotent, and refusing FAILED_PRECONDITION when
// a LIVE serving VM was relit from this ref (evicting the bundle out from under a live
// relit VM would lose the relit-from state needed to re-bank it if the VM dies or the
// node restarts before the next StopServing(BANK)). The guard is ENFORCED by scanning
// the live serving registry for any VM whose source snapshotRef == ref.
func (s *Server) evictServingSnapshot(ref string) (*nodev1.EvictSnapshotResponse, error) {
	if s.servingDriver == nil {
		return nil, status.Error(codes.Unimplemented, "noded: serving eviction not configured")
	}
	// In-use guard: refuse while a live serving VM was relit from this ref.
	for _, e := range s.servingVMs.snapshotWithRefs() {
		if e.snapshotRef == ref {
			return nil, status.Errorf(codes.FailedPrecondition, "noded: serving snapshot %q is in use by live vm %q", ref, e.vmID)
		}
	}
	if err := s.servingDriver.RemoveServingBundle(ref); err != nil {
		return nil, status.Errorf(codes.Internal, "noded: evict serving snapshot %q: %v", ref, err)
	}
	s.servingSnap.remove(ref)
	s.signalChange()
	return &nodev1.EvictSnapshotResponse{}, nil
}

// ---- Workload registry (artifact-decoupling Phase 2) -----------------------

// SyncRegistry converges the daemon's in-memory workload registry to EXACTLY the
// pushed set (entries absent are dropped, entries present are added-or-updated),
// marks the registry synced (clearing the readiness gate), and clears any stale
// boot-cache mark. Idempotent under replay. The control plane calls this once per
// (re)connect after the adoption handshake so a daemon that missed incremental
// pushes while disconnected re-converges to truth.
func (s *Server) SyncRegistry(_ context.Context, req *nodev1.SyncRegistryRequest) (*nodev1.SyncRegistryResponse, error) {
	entries := make([]workloadEntry, 0, len(req.GetEntries()))
	for _, e := range req.GetEntries() {
		entries = append(entries, entryFromProto(e))
	}
	n := s.registry.sync(entries)
	s.logger.Info("workload registry synced", "entries", n)
	// A sync can flip the daemon ready; wake any WatchNode observers so the
	// control plane sees the new registry-derived facts promptly.
	s.signalChange()
	return &nodev1.SyncRegistryResponse{EntryCount: uint32(n)}, nil
}

// RegisterWorkload adds-or-updates one registry entry incrementally. It does not
// clear the readiness gate (only a full SyncRegistry is the authoritative replay
// the gate waits for). Idempotent.
func (s *Server) RegisterWorkload(_ context.Context, req *nodev1.RegisterWorkloadRequest) (*nodev1.RegisterWorkloadResponse, error) {
	entry := req.GetEntry()
	if entry == nil || entry.GetWorkload() == "" {
		return nil, status.Error(codes.InvalidArgument, "noded: entry.workload required")
	}
	n := s.registry.register(entryFromProto(entry))
	s.signalChange()
	return &nodev1.RegisterWorkloadResponse{EntryCount: uint32(n)}, nil
}

// DeregisterWorkload removes one registry entry by workload name. Idempotent on
// an absent workload.
func (s *Server) DeregisterWorkload(_ context.Context, req *nodev1.DeregisterWorkloadRequest) (*nodev1.DeregisterWorkloadResponse, error) {
	n := s.registry.deregister(req.GetWorkload())
	s.signalChange()
	return &nodev1.DeregisterWorkloadResponse{EntryCount: uint32(n)}, nil
}

// RegistrySynced reports whether the daemon has received its registry replay
// (the readiness gate: traffic never reaches a pod with an empty registry). The
// daemon entrypoint's /readyz probe reads it.
func (s *Server) RegistrySynced() bool {
	return s.registry.isSynced()
}

// ---- Node status -----------------------------------------------------------

// GetNodeStatus is the unary snapshot of the capacity facts WatchNode streams.
func (s *Server) GetNodeStatus(_ context.Context, req *nodev1.GetNodeStatusRequest) (*nodev1.NodeStatus, error) {
	ns := s.nodeStatus()
	// Echo the caller's node_id when it set one, for its own correlation.
	if id := req.GetNodeId(); id != "" {
		ns.NodeId = id
	}
	return ns, nil
}

// WatchNode streams NodeStatus: an initial snapshot immediately, then on every
// material change and at a liveness interval, until the client disconnects.
func (s *Server) WatchNode(req *nodev1.WatchNodeRequest, stream grpc.ServerStreamingServer[nodev1.NodeStatus]) error {
	send := func() error {
		ns := s.nodeStatus()
		if id := req.GetNodeId(); id != "" {
			ns.NodeId = id
		}
		return stream.Send(ns)
	}
	if err := send(); err != nil {
		return err
	}
	ch := s.subscribe()
	defer s.unsubscribe(ch)
	ticker := time.NewTicker(livenessInterval)
	defer ticker.Stop()
	ctx := stream.Context()
	for {
		select {
		case <-ctx.Done():
			return ctx.Err() // nosemgrep: no-bare-error-return
		case <-ticker.C:
			if err := send(); err != nil {
				return err
			}
		case <-ch:
			if err := send(); err != nil {
				return err
			}
		}
	}
}

// nodeStatus assembles the current capacity fact set.
func (s *Server) nodeStatus() *nodev1.NodeStatus {
	primed, taskLive := s.vms.capacity()
	caps := s.workloadCapacities(primed)
	maxLive := s.cfg.MaxLiveVMs
	if maxLive < 0 {
		maxLive = 0
	}
	// The reported MaxLiveVms is the brick's cgroup-derived slot ceiling (ADR
	// embervm/013 section 7), with the configured backstop as an upper clamp:
	// a size-class brick advertises a budget-honest slot count, never the raw
	// configured default. When the cgroup budget is unknown the ceiling equals
	// the configured backstop, preserving pre-budget behavior.
	maxLive = int(s.slotCeiling(uint64(maxLive)))
	// Session and serving VMs both count against the node live-VM total alongside
	// task-pool VMs (all three classes Claim through the same driver, but this sum
	// names the invariant at the call site).
	sessionVMs := s.sessionVMsStatus()
	servingVMs := s.servingVMsStatus()
	statefulVMs := s.statefulVMsStatus()
	// Stateful VMs count against the node live-VM total alongside task/session/
	// serving (all four classes Claim through the same driver, so its LiveCount
	// already sums them; naming the invariant here mirrors the session/serving
	// comment above).
	// Group member VMs count against the node live-VM total alongside every other
	// class (all Claim through the same driver). Empty in Task 4; Task 5 fills the
	// member registry.
	groupMemberVMs := s.groupMemberVmsStatus()
	live := taskLive + len(sessionVMs) + len(servingVMs) + len(statefulVMs) + len(groupMemberVMs)
	snaps := s.sessionSnapshotsStatus()
	freeBytes, usedBytes := s.snapshotDiskUsage()
	return &nodev1.NodeStatus{
		NodeId:                s.cfg.Node,
		PodUid:                s.cfg.PodUID,
		SizeClass:             s.cfg.SizeClass,
		CpuVendor:             s.cfg.CpuVendor,
		Workloads:             caps,
		MemHeadroomMib:        s.memHeadroom(),
		CpuHeadroomMillicores: uint32(s.cpuHeadroom()),
		LiveVms:               uint32(live),
		MaxLiveVms:            uint32(maxLive),
		Draining:              s.isDraining(),
		DrainDeadlineUnixMs:   s.drainDeadline(),
		BuildError:            s.bases.firstBuildError(),
		SessionVms:            sessionVMs,
		SessionSnapshots:      snaps,
		SnapshotDiskFreeBytes: freeBytes,
		SnapshotDiskUsedBytes: usedBytes,
		ServingVms:            servingVMs,
		ServingSnapshots:      s.servingSnapshotsStatus(),
		ServingSubnetCidr:     s.servingSubnetCIDR(),
		StatefulVms:           statefulVMs,
		StatefulBundles:       s.statefulBundlesStatus(),
		Volumes:               s.volumesStatus(),
		GroupNetworks:         s.groupNetworksStatus(),
		GroupMemberVms:        groupMemberVMs,
		GroupBundleSets:       s.groupBundleSetsStatus(),
		StoreReachable:        s.storeReachableNow(),
		MemBudgetMib:          s.memBudget(),
		CpuBudgetMillicores:   s.cpuBudget(),
		CpuSku:                s.cpuSku(),
		LocalBases:            s.localBasesStatus(),
	}
}

// localBasesStatus projects the daemon's FULL on-disk base inventory into the
// repeated NodeStatus.local_bases (base-durability PR-3). Unlike the
// WorkloadCapacity projection (ONE current base per workload) AND unlike the base
// registry alone, this SCANS the bases/ directory so it reports EVERY dir present:
//   - registered bases (READY/BUILDING/FAILED), carrying the registry's state +
//     size,
//   - superseded versions left by turnovers,
//   - and UNREGISTERED on-disk-only dirs (a build that died mid-write leaves a
//     memfile.tmp/snapfile.tmp orphan with no snapfile, so ReconcileBasesFromDisk
//     never registered it), reported with base_state UNSPECIFIED.
//
// Scanning disk (not just the registry) is what lets the control plane's retention
// sweep target the .tmp orphans too: a registry-only projection would hide them,
// stranding the exact bytes PR-3 exists to reclaim. A dir also present in the
// registry takes the registry's state/size (authoritative for a live BUILDING
// ref); a dir absent from the registry is sized from its files on disk. Bounded by
// the on-disk base-dir count.
func (s *Server) localBasesStatus() []*nodev1.BaseInventoryEntry {
	root := filepath.Join(s.cfg.SnapshotRoot, "bases")
	dirents, err := os.ReadDir(root)
	if err != nil {
		// Missing dir (fresh node) or unreadable: fall back to the registry view so
		// a scan error never blanks an inventory the registry could still report.
		return s.localBasesFromRegistry()
	}
	// Index the registry by ref so a scanned dir can adopt its authoritative state.
	reg := make(map[string]baseEntry)
	for _, b := range s.bases.snapshot() {
		reg[b.snapshotRef] = b
	}
	out := make([]*nodev1.BaseInventoryEntry, 0, len(dirents))
	seen := make(map[string]struct{}, len(dirents))
	for _, ent := range dirents {
		if !ent.IsDir() {
			continue
		}
		ref := ent.Name()
		seen[ref] = struct{}{}
		if b, ok := reg[ref]; ok {
			out = append(out, &nodev1.BaseInventoryEntry{
				Ref:       ref,
				Workload:  b.workload,
				SizeBytes: uint64(b.sizeBytes),
				BaseState: b.state,
			})
			continue
		}
		// Unregistered on-disk dir (a superseded dir not re-registered, or a .tmp
		// orphan): report it so the sweep can reclaim it. base_state UNSPECIFIED
		// marks "on disk, not a registry-known build".
		out = append(out, &nodev1.BaseInventoryEntry{
			Ref:       ref,
			Workload:  workloadFromBaseKey(ref),
			SizeBytes: dirSizeBytes(filepath.Join(root, ref)),
			BaseState: nodev1.BaseBuildState_BASE_BUILD_STATE_UNSPECIFIED,
		})
	}
	// A BUILDING ref whose dir has not yet materialized on disk (the build just
	// started) lives only in the registry; include it so the sweep sees it BUILDING
	// and never targets it.
	for ref, b := range reg {
		if _, ok := seen[ref]; ok {
			continue
		}
		out = append(out, &nodev1.BaseInventoryEntry{
			Ref:       ref,
			Workload:  b.workload,
			SizeBytes: uint64(b.sizeBytes),
			BaseState: b.state,
		})
	}
	if len(out) == 0 {
		return nil
	}
	return out
}

// localBasesFromRegistry is the registry-only fallback for localBasesStatus when
// the bases/ dir cannot be scanned (missing on a fresh node, or a read error).
func (s *Server) localBasesFromRegistry() []*nodev1.BaseInventoryEntry {
	entries := s.bases.snapshot()
	if len(entries) == 0 {
		return nil
	}
	out := make([]*nodev1.BaseInventoryEntry, 0, len(entries))
	for _, b := range entries {
		out = append(out, &nodev1.BaseInventoryEntry{
			Ref:       b.snapshotRef,
			Workload:  b.workload,
			SizeBytes: uint64(b.sizeBytes),
			BaseState: b.state,
		})
	}
	return out
}

// dirSizeBytes sums the regular-file sizes directly under a base dir (snapfile +
// memfile + any .tmp partials + sidecars), best-effort: an unreadable dir or entry
// contributes 0 rather than failing the whole NodeStatus projection.
func dirSizeBytes(dir string) uint64 {
	ents, err := os.ReadDir(dir)
	if err != nil {
		return 0
	}
	var total uint64
	for _, e := range ents {
		if e.IsDir() {
			continue
		}
		if fi, err := e.Info(); err == nil && fi.Size() > 0 {
			total += uint64(fi.Size())
		}
	}
	return total
}

// cpuSku builds this node's full CPU-restore identity (PR-E): vendor plus the
// CPU template in force. Nil when CpuVendor is unset (an undetected vendor
// reports no sku at all, matching how it already skips the vendor-only check;
// a wire-unset CpuSku is exactly the pre-PR-E behavior a daemon that never set
// this field produces).
func (s *Server) cpuSku() *nodev1.CpuSku {
	if s.cfg.CpuVendor == "" {
		return nil
	}
	return &nodev1.CpuSku{Vendor: s.cfg.CpuVendor, Template: s.cfg.CpuTemplate}
}

// servingVMsStatus projects the live serving-VM registry into the NodeStatus message
// shape, including each VM's current health-probe verdict. Reported ONLY here, never
// in WorkloadCapacity.primed_vm_ids or session_vms (a serving VM is disjoint from both
// the task pool and the session pool).
func (s *Server) servingVMsStatus() []*nodev1.ServingVm {
	live := s.servingVMs.snapshot()
	out := make([]*nodev1.ServingVm, 0, len(live))
	for _, e := range live {
		// Report the projected routable endpoint (pod IP + DNAT port), not the stored
		// node-internal tap IP; the registry keeps the tap IP for the probe and pin.
		ip, port := e.ip, e.port
		if s.servingNet != nil {
			ip, port = s.servingNet.Endpoint(net.ParseIP(e.ip), e.port)
		}
		out = append(out, &nodev1.ServingVm{
			VmId:            e.vmID,
			Workload:        e.workload,
			Ip:              ip,
			Port:            port,
			Healthy:         e.healthy,
			LastProbeUnixMs: e.lastProbeUnixMs,
		})
	}
	return out
}

// servingSnapshotsStatus projects the banked-serving-snapshot inventory into
// NodeStatus.
func (s *Server) servingSnapshotsStatus() []*nodev1.ServingSnapshot {
	snaps := s.servingSnap.snapshot()
	out := make([]*nodev1.ServingSnapshot, 0, len(snaps))
	for _, e := range snaps {
		out = append(out, &nodev1.ServingSnapshot{
			SnapshotRef:     e.snapshotRef,
			Workload:        e.workload,
			SizeBytes:       uint64(e.sizeBytes),
			CreatedAtUnixMs: e.createdAtUnixMs,
			Exported:        s.artifactExported(nodev1.ArtifactKind_ARTIFACT_KIND_SERVING, e.workload, e.snapshotRef),
		})
	}
	return out
}

// artifactExported reports whether an artifact's store copy is currently present
// (from the exported cache the export queue maintains). A workload-less entry (a
// disk-only reconcile whose control-plane binding is not yet known) has no
// composable prefix and reports false until the control plane rebinds it, which
// is safe: the CP treats false as "a roll would lose this off-node copy".
func (s *Server) artifactExported(kind nodev1.ArtifactKind, workload, ref string) bool {
	prefix := artifactPrefix(&nodev1.ArtifactRef{Kind: kind, Workload: workload, Ref: ref}, s.cfg.CpuVendor)
	if prefix == "" {
		return false
	}
	return s.exported.present(prefix)
}

// servingSubnetCIDR reports the serving subnet CIDR for NodeStatus, or "" when no
// serving network is configured (task/session-only builds and tests).
func (s *Server) servingSubnetCIDR() string {
	if s.servingNet == nil {
		return ""
	}
	return s.servingNet.CIDR()
}

// statefulVMsStatus projects the live stateful-VM registry into the NodeStatus
// message shape, including each VM's current TCP-probe health verdict and its
// volume generation (the pair key). Reported ONLY here, never in
// WorkloadCapacity.primed_vm_ids, session_vms, or serving_vms.
func (s *Server) statefulVMsStatus() []*nodev1.StatefulVm {
	if s.statefulVMs == nil {
		return nil
	}
	live := s.statefulVMs.snapshot()
	out := make([]*nodev1.StatefulVm, 0, len(live))
	for _, e := range live {
		ip, port := e.ip, e.port
		if s.servingNet != nil {
			ip, port = s.servingNet.Endpoint(net.ParseIP(e.ip), e.port)
		}
		out = append(out, &nodev1.StatefulVm{
			VmId:              e.vmID,
			Workload:          e.workload,
			Ip:                ip,
			Port:              port,
			Healthy:           e.healthy,
			Generation:        e.generation,
			LastProbeUnixMs:   e.lastProbeUnixMs,
			CheckpointPending: e.checkpointPending,
			CheckpointToken:   e.checkpointToken,
		})
	}
	return out
}

// statefulBundlesStatus projects the banked-stateful-bundle inventory into
// NodeStatus (at most one entry per workload by construction).
func (s *Server) statefulBundlesStatus() []*nodev1.StatefulBundle {
	if s.statefulBundles == nil {
		return nil
	}
	bundles := s.statefulBundles.snapshot()
	out := make([]*nodev1.StatefulBundle, 0, len(bundles))
	for _, e := range bundles {
		out = append(out, &nodev1.StatefulBundle{
			SnapshotRef:     e.snapshotRef,
			Workload:        e.workload,
			Generation:      e.generation,
			SizeBytes:       uint64(e.sizeBytes),
			CreatedAtUnixMs: e.createdAtUnixMs,
			Exported:        s.artifactExported(nodev1.ArtifactKind_ARTIFACT_KIND_STATEFUL, e.workload, e.snapshotRef),
		})
	}
	return out
}

// volumesStatus projects the durable volume inventory (scanned fresh off disk
// on every call via volume.Manager.Scan, never cached) into NodeStatus.
func (s *Server) volumesStatus() []*nodev1.Volume {
	if s.volumes == nil {
		return nil
	}
	inv, err := s.volumes.Scan()
	if err != nil {
		s.logger.Warn("noded: scan stateful volumes", "err", err)
		return nil
	}
	out := make([]*nodev1.Volume, 0, len(inv))
	for _, v := range inv {
		var exportedGen uint64
		prefix := artifactPrefix(&nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_VOLUME, Workload: v.Workload}, s.cfg.CpuVendor)
		if prefix != "" {
			if g, ok := s.exported.generation(prefix); ok {
				exportedGen = g
			}
		}
		out = append(out, &nodev1.Volume{
			Workload:           v.Workload,
			Generation:         v.Generation,
			SizeBytes:          v.SizeBytes,
			AllocatedBytes:     v.AllocatedBytes,
			Attached:           v.Attached,
			ExportedGeneration: exportedGen,
			GenerationBlessed:  v.GenerationBlessed,
		})
	}
	return out
}

// sessionVMsStatus projects the live session-VM registry into the NodeStatus
// message shape. These are reported ONLY here, never in
// WorkloadCapacity.primed_vm_ids, so a session VM is never adopted into the
// single-use task pool.
func (s *Server) sessionVMsStatus() []*nodev1.SessionVm {
	live := s.sessionVMs.snapshot()
	out := make([]*nodev1.SessionVm, 0, len(live))
	for _, e := range live {
		out = append(out, &nodev1.SessionVm{
			VmId:      e.vmID,
			SessionId: e.sessionID,
			Workload:  e.workload,
		})
	}
	return out
}

// sessionSnapshotsStatus projects the banked-snapshot inventory into NodeStatus.
func (s *Server) sessionSnapshotsStatus() []*nodev1.SessionSnapshot {
	snaps := s.sessionSnap.snapshot()
	out := make([]*nodev1.SessionSnapshot, 0, len(snaps))
	for _, e := range snaps {
		out = append(out, &nodev1.SessionSnapshot{
			SnapshotRef:     e.snapshotRef,
			SessionId:       e.sessionID,
			Workload:        e.workload,
			SizeBytes:       uint64(e.sizeBytes),
			CreatedAtUnixMs: e.createdAtUnixMs,
			Exported:        s.artifactExported(nodev1.ArtifactKind_ARTIFACT_KIND_SESSION, e.workload, e.snapshotRef),
		})
	}
	return out
}

// workloadCapacities merges the primed vm_ids with base build state per
// workload. Every workload that has a base OR primed VMs gets one entry.
// free_primed_slots is the count of the primed ids so the two never disagree.
func (s *Server) workloadCapacities(primed map[string][]string) []*nodev1.WorkloadCapacity {
	byWorkload := make(map[string]*nodev1.WorkloadCapacity)
	get := func(wl string) *nodev1.WorkloadCapacity {
		c, ok := byWorkload[wl]
		if !ok {
			c = &nodev1.WorkloadCapacity{
				Workload:  wl,
				BaseState: nodev1.BaseBuildState_BASE_BUILD_STATE_NONE,
			}
			byWorkload[wl] = c
		}
		return c
	}
	for _, b := range s.bases.snapshot() {
		// A READY base is only reportable if its runtime image is still provisioned:
		// the control plane places a stateful cold boot on snapshot_ref and the daemon
		// resolves it via imageDigest -> cfg.Images, so advertising a base built against
		// a superseded image (or an in-memory entry whose disk snapshot was GC'd) would
		// place a wake that fails FAILED_PRECONDITION. BUILDING/FAILED are reported as-is
		// so the control plane sees progress. Mirrors the serving-image filter below.
		if b.state == nodev1.BaseBuildState_BASE_BUILD_STATE_READY {
			if !s.imageProvisioned(b.imageDigest) {
				continue
			}
		}
		c := get(b.workload)
		c.SnapshotRef = b.snapshotRef
		c.BaseState = b.state
		// Base durability (base-durability PR-1, additive): report whether this
		// base's store copy is present and current, read from the same
		// exportedCache the session/serving/stateful projections read via
		// artifactExported. The control plane's BaseBuilder uses this to know the
		// durability floor has landed before it treats the local disk as a cache.
		// artifactExported is false for a base whose prefix cannot be composed
		// (no vendor detected), which the control plane safely reads as
		// not-yet-exported and re-exports (the export verb is idempotent).
		c.Exported = s.artifactExported(nodev1.ArtifactKind_ARTIFACT_KIND_BASE, b.workload, b.snapshotRef)
	}
	// Serving images (cold-boot handler artifacts) are reported in a DISTINCT field
	// from the base memory snapshot (D-R3.11.2): serving placement cold-boots this ref,
	// never snapshot_ref. A workload may have both (a base snapshot for the task lane
	// AND a serving image for the serving lane); they are independent facts.
	for _, si := range s.servingImage.snapshot() {
		// Only report a serving image whose runtime rootfs is STILL provisioned on
		// this node. A serving cold boot attaches that runtime rootfs as drive 1
		// (startServingFresh resolves si.runtimeImageRef against s.cfg.Images), so a
		// base built against a since-superseded runtime image, whose rootfs is gone
		// after the node rolled, is not cold-bootable. Old serving bases are not GC'd,
		// so after a runtime roll multiple serving bases coexist per workload, and the
		// snapshot() map order is nondeterministic; reporting a stale one makes the
		// control plane place a wake on it and get FAILED_PRECONDITION "runtime image
		// ... not provisioned" (a transient post-roll 503). Filtering to
		// provisioned-runtime bases keeps the reported serving_image_ref always
		// cold-bootable; if a workload has ONLY stale bases, it reports none and the
		// control plane rebuilds. Base GC of the stale bundles is a separate follow-up
		// (D-R3.11.3); this makes them harmless to placement. When more than one base
		// is provisioned (a brief runtime transition), any is cold-bootable, so the
		// residual nondeterminism cannot cause a 503.
		if !s.imageProvisioned(si.runtimeImageRef) {
			continue
		}
		c := get(si.workload)
		c.ServingImageRef = si.baseKey
	}
	for wl, ids := range primed {
		c := get(wl)
		c.FreePrimedSlots = uint32(len(ids))
		c.PrimedVmIds = ids
	}
	out := make([]*nodev1.WorkloadCapacity, 0, len(byWorkload))
	for _, c := range byWorkload {
		out = append(out, c)
	}
	return out
}

// ---- drain + change notification -------------------------------------------

// SetDraining marks the daemon draining and publishes the drain deadline so
// WatchNode reports draining=true plus drain_deadline_unix_ms, and new
// BuildBase/Prime/Assign calls are rejected. The control plane reads the deadline
// off the WatchNode stream and force-banks every managed VM before it (R6). All
// LIFECYCLE rpcs (Bank/Stop/Resolve/StopGroupMember) keep being served while
// draining so that force-bank can run; only new-work verbs are refused. Called on
// SIGTERM before the daemon holds shutdown for the bank pass.
func (s *Server) SetDraining(deadline time.Time) {
	s.drainingMu.Lock()
	s.draining = true
	s.drainDeadlineUnixMs = deadline.UnixMilli()
	s.drainingMu.Unlock()
	s.signalChange()
}

func (s *Server) isDraining() bool {
	s.drainingMu.RLock()
	defer s.drainingMu.RUnlock()
	return s.draining
}

// drainDeadline returns the published drain deadline in unix-ms, 0 when not
// draining. Surfaced in NodeStatus so the control plane bounds its force-bank
// pass to deadline - safety_margin.
func (s *Server) drainDeadline() int64 {
	s.drainingMu.RLock()
	defer s.drainingMu.RUnlock()
	return s.drainDeadlineUnixMs
}

// managedLiveVMCount is the number of live NON-task VMs the control plane must
// force-bank during a drain: session, serving, stateful, and group member VMs.
// Task-class VMs (in-flight Assigns) are excluded; they drain via GracefulStop.
// The shutdown path holds the gRPC surface up until this reaches zero (every
// managed VM banked or destroyed by the control plane) or the deadline passes.
func (s *Server) managedLiveVMCount() int {
	return len(s.sessionVMs.snapshot()) + s.servingVMs.count() + s.statefulVMs.count() + s.groupMembers.count()
}

// WaitForManagedDrain holds until every managed VM has left the registry (the
// control plane force-banked or destroyed them) or the deadline passes,
// whichever comes first, and returns the count still live at return (0 on a
// clean drain). It wakes on every NodeStatus change (each Bank/Stop signals) so a
// clean drain returns promptly, with a periodic re-check as a backstop against a
// missed signal. It does NOT stop the gRPC server: the caller does that after
// this returns, so lifecycle rpcs stay served for the whole window.
func (s *Server) WaitForManagedDrain(ctx context.Context, deadline time.Time) int {
	ch := s.subscribe()
	defer s.unsubscribe(ch)
	timer := time.NewTimer(time.Until(deadline))
	defer timer.Stop()
	ticker := time.NewTicker(500 * time.Millisecond)
	defer ticker.Stop()
	for {
		if n := s.managedLiveVMCount(); n == 0 {
			return 0
		}
		select {
		case <-ch:
		case <-ticker.C:
		case <-timer.C:
			return s.managedLiveVMCount()
		case <-ctx.Done():
			return s.managedLiveVMCount()
		}
	}
}

// registerBuild records an in-flight build's cancel func so a drain can abort it,
// and adds it to the wait group so a drain can wait for a clean finish. Paired
// with finishBuild in driveBuild's defer.
func (s *Server) registerBuild(baseKey string, cancel context.CancelFunc) {
	s.buildsWG.Add(1)
	s.buildsMu.Lock()
	s.activeBuilds[baseKey] = cancel
	s.buildsMu.Unlock()
}

// finishBuild deregisters a build once driveBuild returns (built, failed, or
// aborted). It cancels the build context (releasing its resources on the normal
// path too) and drops the wait-group count.
func (s *Server) finishBuild(baseKey string) {
	s.buildsMu.Lock()
	cancel, ok := s.activeBuilds[baseKey]
	delete(s.activeBuilds, baseKey)
	s.buildsMu.Unlock()
	if ok {
		cancel()
	}
	s.buildsWG.Done()
}

// WaitForBuildsOrAbort is the last drain step (after the control plane has
// force-banked durable and serving state via WaitForManagedDrain): in-flight
// BuildBase work is allowed to FINISH if it fits the remaining budget, else it is
// cleanly ABORTED at the deadline. Builds are reconstructible by definition (ADR
// embervm/009 resolved-question 5: durable banks first, serving banks second,
// abort builds on clock expiry), so aborting one only re-queues it: cancelling
// the build context makes runBuild tear the build VM down (deferred Release +
// RemoveBundle) and driveBuild mark the base re-queueable (failBuild, never
// READY), so no half-written snapshot survives. Returns the number of builds that
// had to be aborted (0 on a clean finish), for the shutdown log.
func (s *Server) WaitForBuildsOrAbort(deadline time.Time) int {
	done := make(chan struct{})
	go func() {
		s.buildsWG.Wait()
		close(done)
	}()
	select {
	case <-done:
		return 0
	case <-time.After(time.Until(deadline)):
	}
	// Deadline reached with builds still in flight: cancel them all, then wait for
	// their teardown (Release/RemoveBundle run on context.Background, so the abort
	// itself is never cut short) before returning.
	s.buildsMu.Lock()
	aborted := len(s.activeBuilds)
	for _, cancel := range s.activeBuilds {
		cancel()
	}
	s.buildsMu.Unlock()
	<-done
	return aborted
}

func (s *Server) subscribe() chan struct{} {
	ch := make(chan struct{}, 1)
	s.subMu.Lock()
	s.subs[ch] = struct{}{}
	s.subMu.Unlock()
	return ch
}

func (s *Server) unsubscribe(ch chan struct{}) {
	s.subMu.Lock()
	delete(s.subs, ch)
	s.subMu.Unlock()
}

// signalChange wakes every WatchNode subscriber to re-send NodeStatus. Non-
// blocking: a subscriber already holding a pending signal is skipped (its next
// send already reflects the latest state).
func (s *Server) signalChange() {
	s.subMu.Lock()
	defer s.subMu.Unlock()
	for ch := range s.subs {
		select {
		case ch <- struct{}{}:
		default:
		}
	}
}

// reap tears a VM down: stop egress, kill the process, remove the bundle. Callers
// that can race (Assign's single-use teardown vs an out-of-band Destroy) both
// remove the entry from the registry FIRST, and map removal under the registry
// lock hands the non-nil entry to exactly one caller, so reap runs once per VM.
// Best-effort: failures are logged, never returned.
func (s *Server) reap(h substrate.Handle, egressCancel func()) {
	if egressCancel != nil {
		egressCancel()
	}
	if err := s.driver.Release(context.Background(), h); err != nil {
		s.logger.Warn("noded: release vm", "vm", h.ID, "thread", h.ThreadID, "err", err)
	}
	if err := s.driver.RemoveBundle(h.ThreadID); err != nil {
		s.logger.Warn("noded: remove bundle", "vm", h.ID, "thread", h.ThreadID, "err", err)
	}
}

// snapshotDiskUsage reports the sessions snapshot dir filesystem's free and used
// bytes so the control plane's LRU eviction and the watermark alert can see disk
// pressure. It statfs()es the sessions dir (or the snapshot root if the sessions
// dir does not exist yet). Best-effort: any error yields (0, 0), which the control
// plane's fail-closed disk policy reads as "no facts" (it then initiates no new
// banks rather than banking onto a possibly-full disk).
func (s *Server) snapshotDiskUsage() (freeBytes, usedBytes uint64) {
	dir := s.snapshotSessionsDir()
	if dir == "" {
		return 0, 0
	}
	// statfs needs an existing path; fall back to the snapshot root, then give up.
	target := dir
	if _, err := os.Stat(target); err != nil {
		target = s.cfg.SnapshotRoot
		if target == "" {
			return 0, 0
		}
		if _, err := os.Stat(target); err != nil {
			return 0, 0
		}
	}
	var st unix.Statfs_t
	if err := unix.Statfs(target, &st); err != nil {
		s.logger.Debug("noded: statfs sessions dir", "dir", target, "err", err)
		return 0, 0
	}
	bsize := uint64(st.Bsize) //nolint:unconvert // Bsize is int64 on linux, uint32 on darwin
	freeBytes = st.Bavail * bsize
	usedBytes = (st.Blocks - st.Bfree) * bsize
	return freeBytes, usedBytes
}

// snapshotSessionsDir is the directory holding banked session bundles. It prefers
// the session driver's own SessionsDir (the single source of truth for the path,
// which resolves under the per-instance warmth root); when no session driver is
// wired (task-only tests) it derives it from the config warmth root (WarmthRoot
// when set, else SnapshotRoot, mirroring driver.warmthRoot), and returns "" if
// neither is available.
func (s *Server) snapshotSessionsDir() string {
	if s.sessionDriver != nil {
		return s.sessionDriver.SessionsDir()
	}
	warmth := s.cfg.WarmthRoot
	if warmth == "" {
		warmth = s.cfg.SnapshotRoot
	}
	if warmth == "" {
		return ""
	}
	return filepath.Join(warmth, "sessions")
}

// ---- startup base reconciliation -------------------------------------------

// ReconcileBasesFromDisk scans SnapshotRoot/bases for base bundles left by a
// prior daemon incarnation and registers them READY, so the control plane
// reconciles against existing snapshots instead of rebuilding. A base dir name is
// the baseKey "<workload>__<sig>"; the workload prefix is recovered for the
// capacity report. Missing dir or unreadable entries are ignored (fresh node).
func (s *Server) ReconcileBasesFromDisk() {
	root := filepath.Join(s.cfg.SnapshotRoot, "bases")
	entries, err := os.ReadDir(root)
	if err != nil {
		if !os.IsNotExist(err) {
			s.logger.Warn("noded: scan base bundles", "root", root, "err", err)
		}
		return
	}
	n, gc := 0, 0
	for _, ent := range entries {
		if !ent.IsDir() {
			continue
		}
		baseKey := ent.Name()
		snapfile := filepath.Join(root, baseKey, "snapfile")
		fi, err := os.Stat(snapfile)
		if err != nil {
			// A dir without a snapfile is a half-written or unrelated bundle; skip.
			continue
		}
		// Restore the runtime image ref persisted at build (writeBaseImageRef). A base
		// whose ref file is MISSING (built before this was persisted) is not
		// cold-bootable and is GC'd here. We do NOT gate GC on whether the runtime is
		// "provisioned": at boot the pushed workload registry is not yet synced (the
		// control plane replays it only after we connect), so treating an unprovisioned
		// runtime as "GC the base" would DESTROY every valid warm base on a daemon
		// restart (the artifact-decoupling Phase 2 prod condition, where cfg.Images is
		// empty and the registry arrives asynchronously). Instead we register the base
		// READY and let the NodeStatus capacity filter (imageProvisioned) decide whether
		// to ADVERTISE it once the registry has synced, so an unprovisioned base is
		// simply not placed, never deleted. Reclaiming truly-superseded bases is the
		// PR-G GC's job (D-R3.11.3), not a boot-time reconcile side effect.
		imageRef := s.readBaseImageRef(baseKey)
		if imageRef == "" {
			if err := os.RemoveAll(filepath.Join(root, baseKey)); err == nil {
				gc++
			}
			continue
		}
		memfile := filepath.Join(root, baseKey, "memfile")
		var size int64 = fi.Size()
		if mfi, err := os.Stat(memfile); err == nil {
			size += mfi.Size()
		}
		s.bases.register(baseEntry{
			snapshotRef: baseKey,
			workload:    workloadFromBaseKey(baseKey),
			imageDigest: imageRef,
			readyPath:   defaultReadyPath,
			sizeBytes:   size,
			state:       nodev1.BaseBuildState_BASE_BUILD_STATE_READY,
		})
		n++
	}
	if n > 0 || gc > 0 {
		s.logger.Info("noded: reconciled base snapshots", "adopted", n, "gc_superseded", gc)
		s.signalChange()
	}
}

// writeBaseImageRef persists a base's runtime image ref next to its snapshot so a
// daemon restart can restore base.imageDigest (the base dir name does not encode
// it). Best-effort: a write failure just means the base is GC'd and rebuilt on the
// next reconcile.
func (s *Server) writeBaseImageRef(baseKey, imageRef string) {
	path := filepath.Join(s.cfg.SnapshotRoot, "bases", baseKey, "imageref")
	if err := os.WriteFile(path, []byte(imageRef), 0o644); err != nil {
		s.logger.Warn("noded: persist base imageref", "base", baseKey, "err", err)
	}
}

// readBaseImageRef reads the persisted runtime image ref for a base, or "" if the
// file is absent (a base built before persistence, or a partial write).
func (s *Server) readBaseImageRef(baseKey string) string {
	b, err := os.ReadFile(filepath.Join(s.cfg.SnapshotRoot, "bases", baseKey, "imageref"))
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(b))
}

// ReconcileSessionsFromDisk scans the sessions snapshot dir for banked session
// bundles left by a prior daemon incarnation and seeds the in-memory banked
// inventory, so a restarted daemon reports what banked sessions survive and the
// control plane adopts them (a banked session survives a daemon restart; a live
// one does not, its Firecracker child having died with the daemon). A bundle dir
// name is the opaque snapshot_ref; session_id/workload are unknown for a disk-only
// entry (the control plane rebinds them by adoption from its own projection). A
// missing dir or unreadable entries are ignored (fresh node). The sessions dir is
// (re)created 0700 so a banked bundle (a principal's memory image) is never
// world-readable.
func (s *Server) ReconcileSessionsFromDisk() {
	root := s.snapshotSessionsDir()
	if root == "" {
		return
	}
	// Ensure the dir exists with tight perms even on a fresh node, so the first Bank
	// never races a 0755 MkdirAll and leaves a world-readable window.
	if err := os.MkdirAll(root, 0o700); err != nil {
		s.logger.Warn("noded: create sessions dir", "root", root, "err", err)
	} else {
		// MkdirAll respects umask; force 0700 so an existing looser dir is tightened.
		if err := os.Chmod(root, 0o700); err != nil {
			s.logger.Warn("noded: chmod sessions dir 0700", "root", root, "err", err)
		}
	}
	entries, err := os.ReadDir(root)
	if err != nil {
		if !os.IsNotExist(err) {
			s.logger.Warn("noded: scan session bundles", "root", root, "err", err)
		}
		return
	}
	n := 0
	for _, ent := range entries {
		if !ent.IsDir() {
			continue
		}
		ref := ent.Name()
		snapfile := filepath.Join(root, ref, "snapfile")
		fi, err := os.Stat(snapfile)
		if err != nil {
			// A dir without a snapfile is a half-written or evicted-in-progress bundle;
			// skip (a half-written bank never reports as restorable).
			continue
		}
		size := fi.Size()
		var createdMs int64 = fi.ModTime().UnixMilli()
		memfile := filepath.Join(root, ref, "memfile")
		if mfi, err := os.Stat(memfile); err == nil {
			size += mfi.Size()
		}
		s.sessionSnap.add(sessionSnapshotEntry{
			snapshotRef:     ref,
			sizeBytes:       size,
			createdAtUnixMs: createdMs,
		})
		n++
	}
	if n > 0 {
		s.logger.Info("noded: reconciled existing session snapshots", "count", n)
		s.signalChange()
	}
}

// ReconcileServingFromDisk scans the serving/ snapshot dir for banked serving bundles
// left by a prior daemon incarnation and seeds the in-memory banked-serving inventory,
// so a restarted daemon reports what banked serving snapshots survive and the control
// plane adopts them (a live serving VM died with the prior daemon; its last banked
// snapshot, if any, stays restorable). It recovers each bundle's PINNED IP (D-R3.4.1)
// from the ip sidecar so a relight after restart still re-acquires the same address. A
// bundle dir name is the opaque snapshot_ref; workload is unknown for a disk-only entry
// (the control plane rebinds by adoption). A missing dir or unreadable entries are
// ignored (fresh node). The dir is (re)created 0700 so a banked bundle is never
// world-readable.
func (s *Server) ReconcileServingFromDisk() {
	if s.servingDriver == nil {
		return
	}
	root := s.servingDriver.ServingDir()
	if root == "" {
		return
	}
	if err := os.MkdirAll(root, 0o700); err != nil {
		s.logger.Warn("noded: create serving dir", "root", root, "err", err)
	} else if err := os.Chmod(root, 0o700); err != nil {
		s.logger.Warn("noded: chmod serving dir 0700", "root", root, "err", err)
	}
	entries, err := os.ReadDir(root)
	if err != nil {
		if !os.IsNotExist(err) {
			s.logger.Warn("noded: scan serving bundles", "root", root, "err", err)
		}
		return
	}
	n := 0
	for _, ent := range entries {
		if !ent.IsDir() {
			continue
		}
		ref := ent.Name()
		snapfile := filepath.Join(root, ref, "snapfile")
		fi, err := os.Stat(snapfile)
		if err != nil {
			// A dir without a snapfile is half-written or evicting; skip.
			continue
		}
		size := fi.Size()
		createdMs := fi.ModTime().UnixMilli()
		memfile := filepath.Join(root, ref, "memfile")
		if mfi, err := os.Stat(memfile); err == nil {
			size += mfi.Size()
		}
		s.servingSnap.add(servingSnapshotEntry{
			snapshotRef:     ref,
			ip:              s.servingDriver.ServingPinnedIP(ref),
			sizeBytes:       size,
			createdAtUnixMs: createdMs,
		})
		n++
	}
	if n > 0 {
		s.logger.Info("noded: reconciled existing serving snapshots", "count", n)
		s.signalChange()
	}
}

// ReconcileStatefulFromDisk scans the stateful/ bundle dir (via the driver's
// ScanStatefulBundles) and VolumeRoot (via volumeManager.Inventory) for state a
// prior daemon incarnation left behind, and seeds both in-memory inventories,
// so a restarted daemon reports what banked stateful warmth and durable volumes
// survive. Unlike serving, a bundle's generation stamp is recovered from disk
// (not an IP), and VOLUMES themselves also survive a restart (they are the
// durable data, not disk-cache-of-a-live-VM), so this reconciles two disk
// sources into two registries: statefulBundles (ephemeral warmth) and the
// volume.Manager's own on-disk state (durable, needs no in-memory seeding
// beyond what volume.Manager.Scan already reads live off disk on every call).
// A live stateful VM does NOT survive a daemon restart (its Firecracker child
// died with the daemon), exactly like session and serving; the control plane
// resolves any orphaned live-instance record from its own projection.
func (s *Server) ReconcileStatefulFromDisk() {
	if s.statefulDriver == nil || s.volumes == nil {
		return
	}
	root := s.statefulDriver.StatefulDir()
	if root != "" {
		if err := os.MkdirAll(root, 0o700); err != nil {
			s.logger.Warn("noded: create stateful dir", "root", root, "err", err)
		} else if err := os.Chmod(root, 0o700); err != nil {
			s.logger.Warn("noded: chmod stateful dir 0700", "root", root, "err", err)
		}
	}
	// Sweep any orphaned interruptible-bank checkpoint temps (ADR embervm/008): a
	// restart killed every paused checkpoint VM, so its temp can never be resolved.
	// Done before the bundle rescan so a stale temp is gone before anything reads
	// the stateful tree.
	if gc := s.statefulDriver.GCStatefulCheckpoints(); gc > 0 {
		s.logger.Info("noded: swept orphaned stateful checkpoint temps", "count", gc)
	}
	bundles := s.statefulDriver.ScanStatefulBundles()
	for _, b := range bundles {
		s.statefulBundles.add(statefulBundleEntry{
			snapshotRef: b.SnapshotRef,
			// workload is unknown from disk alone (the bundle dir name is the
			// opaque snapshot_ref); the control plane rebinds by adoption, same
			// as a disk-only serving/session rescan entry.
			generation:      b.Generation,
			sizeBytes:       b.SizeBytes,
			createdAtUnixMs: b.CreatedAtUnixMs,
		})
	}
	// The volume inventory itself needs no separate seeding step: volume.Manager
	// scans VolumeRoot fresh on every Inventory() call (statefulVolumesStatus),
	// so durable volumes are always reported from disk truth with no in-memory
	// registry to reconcile. Only touch the dir here so a fresh node's VolumeRoot
	// exists before the first StartStateful(FRESH) needs it.
	if err := os.MkdirAll(s.cfg.VolumeRoot, 0o700); err != nil && s.cfg.VolumeRoot != "" {
		s.logger.Warn("noded: create volume root", "root", s.cfg.VolumeRoot, "err", err)
	}
	if n := len(bundles); n > 0 {
		s.logger.Info("noded: reconciled existing stateful bundles", "count", n)
		s.signalChange()
	}
}

// ---- helpers ---------------------------------------------------------------

// defaultReadyPath is the frozen guest-contract readiness path.
const defaultReadyPath = "/shim/ready"

// dmNameRE-style sanitiser for the workload component of a base key.
var baseKeyUnsafe = regexp.MustCompile(`[^A-Za-z0-9_-]`)

// baseKeyFor derives the deterministic, filesystem-safe base key (== the opaque
// snapshot_ref) from the workload and the (image_ref, workload_revision, vendor)
// idempotency inputs. vendor is hashed in (R7, standing decision 1) so the same
// image built on an Intel node and an AMD node gets DIFFERENT base keys: a
// Firecracker snapshot restore never crosses the vendor boundary, so a base key
// that ignored vendor would let one vendor's warm cache be handed to noded on
// the other, and BuildBase's idempotency check would wrongly report the
// mismatched-vendor base as AlreadyBuilt. The workload prefix is recoverable on
// startup for the capacity report; the hash suffix keys the bundle per
// image+revision+vendor.
func baseKeyFor(workload, imageRef, revision, vendor string) string {
	sum := sha256.Sum256([]byte(imageRef + "\x00" + revision + "\x00" + vendor))
	sig := hex.EncodeToString(sum[:])[:12]
	wl := baseKeyUnsafe.ReplaceAllString(workload, "_")
	if wl == "" {
		wl = "wl"
	}
	return wl + "__" + sig
}

// baseKeyForZip derives the base key (== the opaque snapshot_ref) for a ZIP-lane
// build. Its idempotency inputs are (runtime image digest, archive sha256,
// vendor): the same archive on the same runtime AND the same CPU vendor keys the
// same base, so a re-registration is a no-op hit; a different vendor keys a
// distinct base for the same reason baseKeyFor hashes vendor in (R7, standing
// decision 1). The workload prefix is recoverable on startup for the capacity
// report, mirroring baseKeyFor.
func baseKeyForZip(workload, imageDigest, archiveSha256, vendor string) string {
	sum := sha256.Sum256([]byte("zip\x00" + imageDigest + "\x00" + archiveSha256 + "\x00" + vendor))
	sig := hex.EncodeToString(sum[:])[:12]
	wl := baseKeyUnsafe.ReplaceAllString(workload, "_")
	if wl == "" {
		wl = "wl"
	}
	return wl + "__" + sig
}

// workloadFromBaseKey recovers the workload prefix from a "<workload>__<sig>"
// base key. A key without the separator yields "" (unknown workload).
func workloadFromBaseKey(baseKey string) string {
	if i := strings.LastIndex(baseKey, "__"); i > 0 {
		return baseKey[:i]
	}
	return ""
}

// newID returns a per-claim unique bundle id.
func newID(prefix string) string {
	var b [8]byte
	_, _ = rand.Read(b[:])
	return prefix + "-" + hex.EncodeToString(b[:])
}

// flattenHeaders collapses an http.Header to the proto's map<string,string>,
// joining multi-valued headers with commas.
func flattenHeaders(h http.Header) map[string]string {
	if len(h) == 0 {
		return nil
	}
	out := make(map[string]string, len(h))
	for k, v := range h {
		out[k] = strings.Join(v, ",")
	}
	return out
}
