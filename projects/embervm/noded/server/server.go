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
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	nodev1 "github.com/jomcgi/homelab/projects/embervm/proto/embervm/node/v1"

	"github.com/jomcgi/homelab/projects/embervm/noded/config"
	"github.com/jomcgi/homelab/projects/embervm/noded/substrate"
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
	RoundTrip(ctx context.Context, udsPath string, req *http.Request) (*http.Response, error)
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

	cfg       config.Config
	driver    vmDriver
	transport transport
	// newBuildDriver builds a cold-boot driver for one image's rootfs+sizing.
	// nil disables BuildBase (used by tests that only exercise Prime/Assign).
	newBuildDriver func(BuildDriverSpec) BuildDriver
	logger         *slog.Logger

	// memHeadroom reads free guest memory in MiB (cgroup v2, best-effort).
	// Overridable in tests.
	memHeadroom func() uint64

	vms   *vmRegistry
	bases *baseRegistry

	drainingMu sync.RWMutex
	draining   bool

	subMu sync.Mutex
	subs  map[chan struct{}]struct{}
}

// Options configures a Server.
type Options struct {
	Config         config.Config
	Driver         vmDriver
	Transport      transport
	NewBuildDriver func(BuildDriverSpec) BuildDriver
	Logger         *slog.Logger
}

// New builds a Server. Driver and Transport must not be nil.
func New(opts Options) *Server {
	logger := opts.Logger
	if logger == nil {
		logger = slog.Default()
	}
	s := &Server{
		cfg:            opts.Config,
		driver:         opts.Driver,
		transport:      opts.Transport,
		newBuildDriver: opts.NewBuildDriver,
		logger:         logger,
		vms:            newVMRegistry(),
		bases:          newBaseRegistry(),
		subs:           make(map[chan struct{}]struct{}),
	}
	s.memHeadroom = readMemHeadroomMib
	return s
}

var _ nodev1.NodeServiceServer = (*Server)(nil)

// ---- BuildBase -------------------------------------------------------------

// BuildBase resolves the image to its node-side rootfs, cold-boots a guest,
// health-gates it on the ready path, and snapshots it into a base bundle. It is
// idempotent per (image_ref, workload_revision): a repeat call for an already
// READY base returns the existing snapshot_ref without rebuilding.
func (s *Server) BuildBase(ctx context.Context, req *nodev1.BuildBaseRequest) (*nodev1.BuildBaseResponse, error) {
	if s.isDraining() {
		return nil, status.Error(codes.Unavailable, "noded: draining")
	}
	if s.newBuildDriver == nil {
		return nil, status.Error(codes.Unimplemented, "noded: base building not configured")
	}
	imageRef := req.GetImageRef()
	if imageRef == "" {
		return nil, status.Error(codes.InvalidArgument, "noded: image_ref required")
	}
	workload := req.GetTrace().GetWorkload()
	img, ok := s.cfg.Images[imageRef]
	if !ok {
		return nil, status.Errorf(codes.FailedPrecondition, "noded: image %q not provisioned on this node", imageRef)
	}
	baseKey := baseKeyFor(workload, imageRef, req.GetWorkloadRevision())
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

	sizeBytes, err := s.runBuild(ctx, bd, baseKey, readyPath)
	if err != nil {
		s.bases.failBuild(baseKey, err.Error())
		s.signalChange()
		return nil, status.Errorf(codes.FailedPrecondition, "noded: build base %q: %v", baseKey, err)
	}
	// The control plane records the resolved image identity; without an OCI pull
	// the ref IS the identity for R0 (deploys are digest-pinned upstream).
	imageDigest := imageRef
	s.bases.readyBuild(baseKey, workload, imageDigest, readyPath, sizeBytes)
	s.signalChange()
	return &nodev1.BuildBaseResponse{
		SnapshotRef:   baseKey,
		ImageDigest:   imageDigest,
		BaseSizeBytes: uint64(sizeBytes),
		Arch:          s.cfg.Arch,
		AlreadyBuilt:  false,
	}, nil
}

// runBuild cold-boots a build guest, waits for readiness, snapshots it into the
// base bundle, and always discards the build VM (the base lives in the bundle).
func (s *Server) runBuild(ctx context.Context, bd BuildDriver, baseKey, readyPath string) (int64, error) {
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

	readyCtx, cancel := context.WithTimeout(ctx, s.cfg.BootReadyTimeout)
	defer cancel()
	if err := s.transport.WaitReady(readyCtx, bd.VsockUDSPath(h.ThreadID), readyPath); err != nil {
		return 0, fmt.Errorf("guest readiness: %w", err)
	}
	ref, err := bd.SnapshotBase(ctx, h, baseKey)
	if err != nil {
		return 0, fmt.Errorf("snapshot: %w", err)
	}
	return ref.SizeBytes, nil
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
	if s.cfg.MaxLiveVMs > 0 && s.driver.LiveCount() >= s.cfg.MaxLiveVMs {
		return nil, status.Errorf(codes.ResourceExhausted, "noded: node live-VM cap %d reached", s.cfg.MaxLiveVMs)
	}
	base, ok := s.bases.get(ref)
	if !ok {
		return nil, status.Errorf(codes.FailedPrecondition, "noded: unknown snapshot_ref %q", ref)
	}
	if base.state != nodev1.BaseBuildState_BASE_BUILD_STATE_READY {
		return nil, status.Errorf(codes.FailedPrecondition, "noded: base %q not ready (state %s)", ref, base.state)
	}
	readyPath := base.readyPath
	if readyPath == "" {
		readyPath = defaultReadyPath
	}

	spec := substrate.ClaimSpec{
		Arch:     s.cfg.Arch,
		ThreadID: newID("vm"),
		BaseSnapshotRef: substrate.SnapshotRef{
			ID:   ref,
			Node: s.cfg.Node,
			Arch: s.cfg.Arch,
			Base: true,
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
	}
	return &nodev1.DestroyResponse{}, nil
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
	primed, live := s.vms.capacity()
	caps := s.workloadCapacities(primed)
	maxLive := s.cfg.MaxLiveVMs
	if maxLive < 0 {
		maxLive = 0
	}
	return &nodev1.NodeStatus{
		NodeId:                s.cfg.Node,
		Workloads:             caps,
		MemHeadroomMib:        s.memHeadroom(),
		CpuHeadroomMillicores: 0, // TODO(task11): report cgroup cpu headroom
		LiveVms:               uint32(live),
		MaxLiveVms:            uint32(maxLive),
		Draining:              s.isDraining(),
		BuildError:            s.bases.firstBuildError(),
	}
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
		c := get(b.workload)
		c.SnapshotRef = b.snapshotRef
		c.BaseState = b.state
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

// SetDraining marks the daemon draining so WatchNode reports draining=true and
// new BuildBase/Prime calls are rejected. Called on SIGTERM before GracefulStop.
func (s *Server) SetDraining() {
	s.drainingMu.Lock()
	s.draining = true
	s.drainingMu.Unlock()
	s.signalChange()
}

func (s *Server) isDraining() bool {
	s.drainingMu.RLock()
	defer s.drainingMu.RUnlock()
	return s.draining
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
	n := 0
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
		memfile := filepath.Join(root, baseKey, "memfile")
		var size int64 = fi.Size()
		if mfi, err := os.Stat(memfile); err == nil {
			size += mfi.Size()
		}
		s.bases.register(baseEntry{
			snapshotRef: baseKey,
			workload:    workloadFromBaseKey(baseKey),
			readyPath:   defaultReadyPath,
			sizeBytes:   size,
			state:       nodev1.BaseBuildState_BASE_BUILD_STATE_READY,
		})
		n++
	}
	if n > 0 {
		s.logger.Info("noded: reconciled existing base snapshots", "count", n)
		s.signalChange()
	}
}

// ---- helpers ---------------------------------------------------------------

// defaultReadyPath is the frozen guest-contract readiness path.
const defaultReadyPath = "/shim/ready"

// dmNameRE-style sanitiser for the workload component of a base key.
var baseKeyUnsafe = regexp.MustCompile(`[^A-Za-z0-9_-]`)

// baseKeyFor derives the deterministic, filesystem-safe base key (== the opaque
// snapshot_ref) from the workload and the (image_ref, workload_revision)
// idempotency inputs. The workload prefix is recoverable on startup for the
// capacity report; the hash suffix keys the bundle per image+revision.
func baseKeyFor(workload, imageRef, revision string) string {
	sum := sha256.Sum256([]byte(imageRef + "\x00" + revision))
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

// readMemHeadroomMib reports free guest memory in MiB from the cgroup v2 memory
// controller (the pod cgroup that bounds the daemon and its child microVMs).
// Best-effort: any error or an unlimited cgroup yields 0 (the max-live-VM cap is
// the real backstop; headroom is an advisory hint until Task 11).
func readMemHeadroomMib() uint64 {
	maxRaw, err := os.ReadFile("/sys/fs/cgroup/memory.max")
	if err != nil {
		return 0
	}
	curRaw, err := os.ReadFile("/sys/fs/cgroup/memory.current")
	if err != nil {
		return 0
	}
	return parseMemHeadroomMib(string(maxRaw), string(curRaw))
}

// parseMemHeadroomMib computes (max-current) in MiB from cgroup v2 memory.max
// and memory.current contents. "max" (unlimited) or a parse error yields 0.
func parseMemHeadroomMib(maxRaw, curRaw string) uint64 {
	maxStr := strings.TrimSpace(maxRaw)
	if maxStr == "max" || maxStr == "" {
		return 0
	}
	maxB, err := strconv.ParseInt(maxStr, 10, 64)
	if err != nil {
		return 0
	}
	curB, err := strconv.ParseInt(strings.TrimSpace(curRaw), 10, 64)
	if err != nil {
		return 0
	}
	if curB >= maxB {
		return 0
	}
	return uint64(maxB-curB) / (1 << 20)
}
