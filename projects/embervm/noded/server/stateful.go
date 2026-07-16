package server

import (
	"context"
	"fmt"
	"net"
	"time"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	nodev1 "github.com/jomcgi/homelab/projects/embervm/proto/embervm/node/v1"

	"github.com/jomcgi/homelab/projects/embervm/noded/serving"
	"github.com/jomcgi/homelab/projects/embervm/noded/substrate"
)

// Cold-boot fallback reasons StartStateful(RELIGHT) reports when it discards a
// banked bundle rather than resuming it. Exported as constants (not inlined
// string literals at each call site) so the three outcomes are named once and
// the proto doc's exact vocabulary ("generation_mismatch", "no_bundle",
// "ledger_unreadable") cannot drift from the code that emits it.
const (
	coldBootReasonGenerationMismatch = "generation_mismatch"
	coldBootReasonNoBundle           = "no_bundle"
	coldBootReasonLedgerUnreadable   = "ledger_unreadable"
)

// StartStateful brings up the stateful VM with the workload's volume attached
// writable, attaching a tap NIC exactly as StartServing does, and health-gates
// by TCP CONNECT to {ip, port} (serving/probe_tcp.go's TCPProber), not HTTP: the
// stateful contract is opaque L4 (decision 4). Every mode bumps the volume's
// generation ledger BEFORE the VM boots (volume.Manager.BumpGeneration), so any
// writable attach the daemon witnesses is unconditionally reflected in the pair
// key even if the boot that follows fails; there is no un-bump. A readiness
// failure DESTROYS the VM, releases the tap, and DETACHES the volume (but never
// rolls the generation back) and returns FAILED_PRECONDITION: there is no
// half-alive endpoint a caller could observe or publish.
func (s *Server) StartStateful(ctx context.Context, req *nodev1.StartStatefulRequest) (*nodev1.StartStatefulResponse, error) {
	if s.servingNet == nil || s.servingDriver == nil || s.statefulDriver == nil || s.volumes == nil {
		return nil, status.Error(codes.Unimplemented, "noded: stateful not configured")
	}
	if s.isDraining() {
		return nil, status.Error(codes.Unavailable, "noded: draining")
	}
	if s.cfg.MaxLiveVMs > 0 && s.liveVMCount() >= s.cfg.MaxLiveVMs {
		return nil, status.Errorf(codes.ResourceExhausted, "noded: node live-VM cap %d reached", s.cfg.MaxLiveVMs)
	}
	workload := req.GetTrace().GetWorkload()
	if workload == "" {
		return nil, status.Error(codes.InvalidArgument, "noded: trace.workload required")
	}
	port := req.GetPort()
	if port == 0 {
		return nil, status.Error(codes.InvalidArgument, "noded: port required")
	}

	switch req.GetMode() {
	case nodev1.StartStatefulMode_START_STATEFUL_MODE_FRESH:
		return s.startStatefulFresh(ctx, req, workload, port)
	case nodev1.StartStatefulMode_START_STATEFUL_MODE_RELIGHT:
		return s.startStatefulRelight(ctx, req, workload, port)
	case nodev1.StartStatefulMode_START_STATEFUL_MODE_COLD:
		return s.startStatefulCold(ctx, req, workload, port)
	default:
		return nil, status.Error(codes.InvalidArgument, "noded: StartStateful mode must be FRESH, RELIGHT, or COLD")
	}
}

// startStatefulFresh creates the workload's volume if absent (refusing
// FAILED_PRECONDITION when absent and create_if_missing is false), then cold
// boots exactly like startStatefulCold. It is the only mode that may create the
// volume; RELIGHT's fallback and COLD both require it to already exist.
func (s *Server) startStatefulFresh(ctx context.Context, req *nodev1.StartStatefulRequest, workload string, port uint32) (*nodev1.StartStatefulResponse, error) {
	if !s.volumes.Exists(workload) {
		if !req.GetCreateIfMissing() {
			return nil, status.Errorf(codes.FailedPrecondition, "noded: stateful volume for workload %q does not exist and create_if_missing is false", workload)
		}
		if req.GetVolumeSizeBytes() == 0 {
			return nil, status.Error(codes.InvalidArgument, "noded: volume_size_bytes required to create a new volume")
		}
		if err := s.volumes.Create(workload, req.GetVolumeSizeBytes()); err != nil {
			return nil, status.Errorf(codes.Internal, "noded: create stateful volume for %q: %v", workload, err)
		}
	}
	return s.coldBootStateful(ctx, req, workload, port, "")
}

// startStatefulCold is an EXPLICIT cold boot from the existing volume (no
// bundle resume attempted). It requires the volume to already exist: unlike
// FRESH it never creates one, matching the proto doc's "explicit cold boot from
// the existing volume". cold_boot_reason is left empty: an explicit COLD boot
// is not a discarded relight, so it carries no reason.
func (s *Server) startStatefulCold(ctx context.Context, req *nodev1.StartStatefulRequest, workload string, port uint32) (*nodev1.StartStatefulResponse, error) {
	if !s.volumes.Exists(workload) {
		return nil, status.Errorf(codes.FailedPrecondition, "noded: stateful volume for workload %q does not exist (COLD requires an existing volume)", workload)
	}
	return s.coldBootStateful(ctx, req, workload, port, "")
}

// startStatefulRelight resumes the banked bundle (relight_snapshot_ref) iff its
// stamped generation equals the volume's CURRENT generation. On any mismatch,
// missing bundle, or unreadable ledger, it EVICTS the bundle (warmth fails
// open) and falls back to a cold boot from boot_image_ref against the EXISTING
// volume (data fails closed: RELIGHT never creates a volume), setting
// cold_boot_reason to name why the warmth was discarded.
func (s *Server) startStatefulRelight(ctx context.Context, req *nodev1.StartStatefulRequest, workload string, port uint32) (*nodev1.StartStatefulResponse, error) {
	ref := req.GetRelightSnapshotRef()
	if ref == "" {
		return nil, status.Error(codes.InvalidArgument, "noded: relight_snapshot_ref required for RELIGHT")
	}
	if !s.volumes.Exists(workload) {
		return nil, status.Errorf(codes.FailedPrecondition, "noded: stateful volume for workload %q does not exist (RELIGHT never creates one)", workload)
	}

	bundle, haveBundle := s.statefulBundles.get(ref)
	// A ledger read failure ("ledger_unreadable") outranks a bundle lookup miss
	// in the sense that both fall back to cold boot, but they are reported with
	// distinct reasons per the proto contract, so read the volume generation
	// first and remember the failure rather than short-circuiting.
	volGen, genErr := s.volumes.Generation(workload)

	var reason string
	switch {
	case genErr != nil:
		reason = coldBootReasonLedgerUnreadable
	case !haveBundle:
		reason = coldBootReasonNoBundle
	case bundle.generation != volGen:
		reason = coldBootReasonGenerationMismatch
	}

	if reason != "" {
		// Warmth fails open: evict the stale/unusable bundle (best-effort; a
		// disk error here must not block the cold-boot fallback that follows)
		// and cold-boot from boot_image_ref against the existing volume.
		if haveBundle {
			_ = s.statefulDriver.RemoveStatefulBundle(ref)
			s.statefulBundles.remove(ref)
		}
		return s.coldBootStateful(ctx, req, workload, port, reason)
	}

	// Matched pair: resume the bundle. The generation still bumps BEFORE boot
	// (every mode does), because resuming the memory snapshot re-exposes the
	// SAME writable device to a guest that is about to run again, and any
	// further write must be witnessed by a strictly newer generation than the
	// one the bundle was stamped with, or a future relight of a LATER bank
	// could falsely re-match this now-stale bundle.
	gen, err := s.volumes.BumpGeneration(workload)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "noded: bump generation for %q: %v", workload, err)
	}
	if err := s.volumes.Attach(workload); err != nil {
		return nil, status.Errorf(codes.FailedPrecondition, "noded: %v", err)
	}
	attached := true
	defer func() {
		if attached {
			s.volumes.Detach(workload)
		}
	}()

	// A stateful relight does not pin a specific IP the way a serving relight
	// does (D-R3.4.1): the stateful endpoint is control-plane-addressed (the
	// control plane reads {ip, port} fresh off every StartStateful response),
	// not guest-baked-critical, so a fresh tap allocation is sufficient.
	_, ip, err := s.servingNet.AllocateTap(ctx)
	if err != nil {
		return nil, status.Errorf(codes.ResourceExhausted, "noded: allocate stateful tap: %v", err)
	}
	h, err := s.statefulDriver.RestoreStateful(ctx, ref, s.volumes.VolumePath(workload))
	if err != nil {
		s.servingNet.ReleaseTap(ctx, ip)
		return nil, status.Errorf(codes.FailedPrecondition, "noded: relight stateful snapshot %q: %v", ref, err)
	}
	attached = false // ownership passes to finishStatefulStart's detach-on-failure path
	return s.finishStatefulStart(ctx, h, workload, ref, ip, port, gen, true, "", s.cfg.RestoreReadyTimeout)
}

// coldBootStateful resolves boot_image_ref against the serving-images
// inventory (a stateful cold boot reuses the EXACT serving cold-boot mechanic:
// rootfs is drive 1, the handler artifact is drive 2, D-R3.11.2), bumps the
// volume's generation, attaches the volume, allocates a tap, and cold-boots
// with the volume as a third writable drive. Shared by FRESH, COLD, and the
// RELIGHT fallback so all three paths boot identically once the volume exists.
func (s *Server) coldBootStateful(ctx context.Context, req *nodev1.StartStatefulRequest, workload string, port uint32, coldBootReason string) (*nodev1.StartStatefulResponse, error) {
	bootImageRef := req.GetBootImageRef()
	if bootImageRef == "" {
		return nil, status.Error(codes.InvalidArgument, "noded: boot_image_ref required")
	}
	// Resolve against the SAME serving-images inventory a serving cold boot
	// uses (D-R3.11.2): boot_image_ref names a built base key, not a static
	// runtime image ref.
	simg, ok := s.servingImage.get(bootImageRef)
	if !ok {
		return nil, status.Errorf(codes.FailedPrecondition, "noded: boot image %q not provisioned on this node (no cold-boot handler artifact built)", bootImageRef)
	}
	img, ok := s.cfg.Images[simg.runtimeImageRef]
	if !ok {
		return nil, status.Errorf(codes.FailedPrecondition, "noded: runtime image %q for boot image %q not provisioned on this node", simg.runtimeImageRef, bootImageRef)
	}
	harnessInit := img.HarnessInit
	if harnessInit == "" {
		harnessInit = s.cfg.HarnessInit
	}
	res := req.GetResources()

	gen, err := s.volumes.BumpGeneration(workload)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "noded: bump generation for %q: %v", workload, err)
	}
	if err := s.volumes.Attach(workload); err != nil {
		return nil, status.Errorf(codes.FailedPrecondition, "noded: %v", err)
	}
	attached := true
	defer func() {
		if attached {
			s.volumes.Detach(workload)
		}
	}()

	tap, ip, err := s.servingNet.AllocateTap(ctx)
	if err != nil {
		return nil, status.Errorf(codes.ResourceExhausted, "noded: allocate stateful tap: %v", err)
	}
	nic := substrate.NICSpec{
		HostDevName: tap,
		IP:          ip.String(),
		GatewayIP:   s.servingNet.GatewayIP().String(),
		PrefixLen:   s.servingNet.PrefixLen(),
		IfaceName:   "eth0",
		ServingPort: port,
	}
	h, err := s.statefulDriver.ClaimStateful(ctx, img.RootfsPath, harnessInit, int(res.GetVcpus()), int(res.GetMemMib()), nic, simg.handlerPath, simg.sizeBytes, s.volumes.VolumePath(workload), req.GetVolumeMount())
	if err != nil {
		s.servingNet.ReleaseTap(ctx, ip)
		return nil, status.Errorf(codes.FailedPrecondition, "noded: cold-boot stateful vm: %v", err)
	}
	attached = false // ownership passes to finishStatefulStart's detach-on-failure path
	return s.finishStatefulStart(ctx, h, workload, "", ip, port, gen, false, coldBootReason, s.cfg.BootReadyTimeout)
}

// finishStatefulStart is the shared tail of every StartStateful path: health-
// gate the guest over the tap by TCP CONNECT, and on success register the live
// stateful VM and start its TCP probe. On a readiness failure it reaps the VM,
// releases the tap, and DETACHES the volume (never rolling the generation
// back), returning FAILED_PRECONDITION.
func (s *Server) finishStatefulStart(ctx context.Context, h substrate.Handle, workload, sourceRef string, ip net.IP, port uint32, generation uint64, wasRelight bool, coldBootReason string, readyBudget time.Duration) (*nodev1.StartStatefulResponse, error) {
	if err := s.waitStatefulReady(ctx, ip, port, readyBudget); err != nil {
		s.reapStateful(h, ip, workload)
		return nil, status.Errorf(codes.FailedPrecondition, "noded: stateful guest not ready over tap: %v", err)
	}
	if err := s.servingNet.EnsureDNAT(ctx, ip, port); err != nil {
		s.reapStateful(h, ip, workload)
		return nil, status.Errorf(codes.FailedPrecondition, "noded: install stateful DNAT for %s: %v", ip, err)
	}
	probe := serving.StartTCPProbe(s.newTCPProber(), ip, port)
	s.statefulVMs.add(&statefulEntry{
		vmID:        h.ID,
		workload:    workload,
		handle:      h,
		ip:          ip,
		port:        port,
		tap:         serving.TapNameForIP(ip),
		generation:  generation,
		snapshotRef: sourceRef,
		probe:       probe,
	})
	s.signalChange()
	endpointIP, endpointPort := s.servingNet.Endpoint(ip, port)
	return &nodev1.StartStatefulResponse{
		VmId:           h.ID,
		Ip:             endpointIP,
		Port:           endpointPort,
		Generation:     generation,
		WasRelight:     wasRelight,
		ColdBootReason: coldBootReason,
	}, nil
}

// waitStatefulReady polls TCP CONNECT to ip:port over the tap until it
// succeeds or the budget expires, mirroring waitServingReady's retry-on-
// connection-refused shape but with no HTTP semantics: the stateful contract
// is opaque L4 (decision 4), so success is a completed handshake, not a status
// code.
func (s *Server) waitStatefulReady(ctx context.Context, ip net.IP, port uint32, budget time.Duration) error {
	deadline := time.Now().Add(budget)
	addr := net.JoinHostPort(ip.String(), fmt.Sprintf("%d", port))
	var lastErr error
	for time.Now().Before(deadline) {
		select {
		case <-ctx.Done():
			return ctx.Err() // nosemgrep: no-bare-error-return
		default:
		}
		d := net.Dialer{Timeout: 2 * time.Second}
		conn, err := d.DialContext(ctx, "tcp", addr)
		if err != nil {
			lastErr = err
			time.Sleep(150 * time.Millisecond)
			continue
		}
		_ = conn.Close()
		return nil
	}
	if lastErr == nil {
		lastErr = fmt.Errorf("timed out after %s", budget)
	}
	return lastErr // nosemgrep: no-bare-error-return
}

// StopStateful tears down a live stateful VM. BANK pauses it, writes a
// stateful snapshot stamped with the CURRENT volume generation, evicts any
// PRIOR bundle for the workload (at most one banked bundle per workload),
// detaches the volume, destroys the VM, and returns {snapshot_ref, generation,
// size_bytes}. DESTROY tears the VM down with no snapshot and detaches the
// volume. Either mode releases the tap.
func (s *Server) StopStateful(ctx context.Context, req *nodev1.StopStatefulRequest) (*nodev1.StopStatefulResponse, error) {
	if s.servingNet == nil || s.statefulDriver == nil || s.volumes == nil {
		return nil, status.Error(codes.Unimplemented, "noded: stateful not configured")
	}
	vmID := req.GetVmId()
	switch req.GetMode() {
	case nodev1.StopStatefulMode_STOP_STATEFUL_MODE_BANK:
		return s.stopStatefulBank(ctx, req, vmID)
	case nodev1.StopStatefulMode_STOP_STATEFUL_MODE_DESTROY:
		return s.stopStatefulDestroy(vmID)
	default:
		return nil, status.Error(codes.InvalidArgument, "noded: StopStateful mode must be BANK or DESTROY")
	}
}

// stopStatefulBank snapshots a live stateful VM to a banked bundle stamped with
// its CURRENT volume generation, evicts any prior bundle for the same
// workload (D-R4: at most one banked bundle per workload), destroys the VM,
// detaches the volume, and records the new banked bundle.
func (s *Server) stopStatefulBank(ctx context.Context, req *nodev1.StopStatefulRequest, vmID string) (*nodev1.StopStatefulResponse, error) {
	e, ok := s.statefulVMs.beginStop(vmID)
	if !ok {
		return nil, status.Errorf(codes.FailedPrecondition, "noded: stateful vm %q not bankable (unknown or a stop is already in flight)", vmID)
	}
	e.probe.Stop()
	snapshotRef := newID("state")
	ref, err := s.statefulDriver.SnapshotStateful(ctx, e.handle, snapshotRef, e.generation)
	if err != nil {
		// A bank is destructive: SnapshotStateful tore the VM down on failure, so
		// drop the now-dead registry entry, release its tap, and detach the
		// volume rather than misreport capacity or leave the volume falsely held.
		s.statefulVMs.remove(vmID)
		s.servingNet.ReleaseTap(ctx, e.ip)
		s.volumes.Detach(e.workload)
		return nil, status.Errorf(codes.FailedPrecondition, "noded: bank stateful vm %q: %v", vmID, err)
	}
	if removed := s.statefulVMs.remove(vmID); removed != nil {
		s.reapStateful(removed.handle, removed.ip, removed.workload)
	}
	// Evict any PRIOR bundle for this workload BEFORE recording the new one: at
	// most one banked bundle per workload, so the old bundle would otherwise be
	// an orphan a relight could never reach (byWorkload lookups always resolve
	// the newest add, but the stale bundle's disk bytes would leak with no
	// eviction path). Best-effort: a disk error removing the old bundle must
	// not block recording the new (successfully banked) one.
	if prior, ok := s.statefulBundles.byWorkload(req.GetTrace().GetWorkload()); ok && prior.snapshotRef != ref.ID {
		_ = s.statefulDriver.RemoveStatefulBundle(prior.snapshotRef)
		s.statefulBundles.remove(prior.snapshotRef)
	}
	s.statefulBundles.add(statefulBundleEntry{
		snapshotRef:     ref.ID,
		workload:        req.GetTrace().GetWorkload(),
		generation:      e.generation,
		sizeBytes:       ref.SizeBytes,
		createdAtUnixMs: time.Now().UnixMilli(),
	})
	s.signalChange()
	return &nodev1.StopStatefulResponse{SnapshotRef: ref.ID, Generation: e.generation, SizeBytes: uint64(ref.SizeBytes)}, nil
}

// stopStatefulDestroy tears a stateful VM down with no snapshot, releases its
// tap, and detaches its volume. Idempotent: an unknown vm_id returns OK.
func (s *Server) stopStatefulDestroy(vmID string) (*nodev1.StopStatefulResponse, error) {
	if removed := s.statefulVMs.remove(vmID); removed != nil {
		removed.probe.Stop()
		s.reapStateful(removed.handle, removed.ip, removed.workload)
		s.signalChange()
	}
	return &nodev1.StopStatefulResponse{}, nil
}

// reapStateful tears a stateful VM down (release the FC process + bundle),
// releases its tap + IP, and detaches its volume so a subsequent StartStateful
// for the same workload is not refused by a stale attach lock. Best-effort,
// mirroring reapServing plus the volume detach.
func (s *Server) reapStateful(h substrate.Handle, ip net.IP, workload string) {
	s.reap(h, func() {})
	if s.servingNet != nil {
		s.servingNet.ReleaseTap(context.Background(), ip)
	}
	if s.volumes != nil {
		s.volumes.Detach(workload)
	}
}

// DeleteVolume removes a workload's volume file and its generation ledger. It
// is the ONLY destructive data verb: FAILED_PRECONDITION while the volume is
// attached to a live VM, idempotent on an already-absent volume.
func (s *Server) DeleteVolume(_ context.Context, req *nodev1.DeleteVolumeRequest) (*nodev1.DeleteVolumeResponse, error) {
	if s.volumes == nil {
		return nil, status.Error(codes.Unimplemented, "noded: stateful not configured")
	}
	workload := req.GetWorkload()
	if workload == "" {
		return nil, status.Error(codes.InvalidArgument, "noded: workload required")
	}
	if err := s.volumes.Delete(workload); err != nil {
		return nil, status.Errorf(codes.FailedPrecondition, "noded: delete volume for %q: %v", workload, err)
	}
	s.signalChange()
	return &nodev1.DeleteVolumeResponse{}, nil
}
