package server

import (
	"context"
	"errors"
	"fmt"
	"net"
	"os"
	"sort"
	"syscall"
	"time"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	nodev1 "github.com/jomcgi/homelab/projects/embervm/proto/embervm/node/v1"

	"github.com/jomcgi/homelab/projects/embervm/noded/serving"
	"github.com/jomcgi/homelab/projects/embervm/noded/substrate"
)

// mmdsEnvKeyNamesSorted returns the sorted key names of an mmds_env map, for
// logging ONLY. It never returns values (see the D-R4.PR-7.1 redaction note at
// this function's call site in coldBootStateful): mmds_env may carry a
// first-boot secret (e.g. a Postgres password), so a log line built from this
// helper's output is safe to emit while the raw map is not.
func mmdsEnvKeyNamesSorted(mmdsEnv map[string]string) []string {
	keys := make([]string, 0, len(mmdsEnv))
	for k := range mmdsEnv {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}

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
// stateful contract is opaque L4 (decision 4). Every mode advances the volume's
// generation ledger BEFORE the VM boots (via attachGeneration: RecordBlessed
// when the request carries a nonzero blessed_generation, R7 ADR embervm/011,
// or the trusted node-local self-bump otherwise), so any writable attach the daemon
// witnesses is unconditionally reflected in the pair key even if the boot that
// follows fails; there is no un-bump. A readiness failure DESTROYS the VM,
// releases the tap, and DETACHES the volume (but never rolls the generation
// back) and returns FAILED_PRECONDITION: there is no half-alive endpoint a
// caller could observe or publish.
func (s *Server) StartStateful(ctx context.Context, req *nodev1.StartStatefulRequest) (*nodev1.StartStatefulResponse, error) {
	return s.startStateful(ctx, req, nodev1.InstanceOrigin_INSTANCE_ORIGIN_CONTROL_PLANE)
}

func (s *Server) startStateful(ctx context.Context, req *nodev1.StartStatefulRequest, origin nodev1.InstanceOrigin) (*nodev1.StartStatefulResponse, error) {
	if s.servingNet == nil || s.servingDriver == nil || s.statefulDriver == nil || s.volumes == nil {
		return nil, status.Error(codes.Unimplemented, "noded: stateful not configured")
	}
	if s.isDraining() {
		return nil, status.Error(codes.Unavailable, "noded: draining")
	}
	if s.slotsExhausted() {
		return nil, status.Errorf(codes.ResourceExhausted, "noded: node live-VM cap %d reached", s.SlotCeiling())
	}
	// Cheap rejection under real memory/tap pressure (ADR embervm/014 decision 3),
	// BEFORE the tap allocation and boot below. Stateful is tap-bearing; the
	// workload's mem need comes from the request's ResourceSpec.
	if err := s.admitOrReject(uint64(req.GetResources().GetMemMib()), classTapBearing); err != nil {
		return nil, err
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
		return s.startStatefulFresh(ctx, req, workload, port, origin)
	case nodev1.StartStatefulMode_START_STATEFUL_MODE_RELIGHT:
		return s.startStatefulRelight(ctx, req, workload, port, origin)
	case nodev1.StartStatefulMode_START_STATEFUL_MODE_COLD:
		return s.startStatefulCold(ctx, req, workload, port, origin)
	default:
		return nil, status.Error(codes.InvalidArgument, "noded: StartStateful mode must be FRESH, RELIGHT, or COLD")
	}
}

// attachGeneration resolves the generation a writable attach records. A nonzero
// blessedGeneration on the request is recorded via volume.Manager.RecordBlessed
// (the control-plane-issued value, never invented here). Zero means the lease
// lane for a trusted node-local activator; any other unblessed writable attach
// is refused FAILED_PRECONDITION (R7, ADR embervm/011, standing decision 4:
// the control plane is the sole issuer of volume generations). The activator
// follows the same self-bump discipline as checkpoint auto-abort when its lease
// is absent or exhausted and relies on the physical volume attach fence.
func (s *Server) attachGeneration(workload string, blessedGeneration uint64, activatorOrigin bool) (uint64, error) {
	if blessedGeneration > 0 {
		gen, err := s.volumes.RecordBlessed(workload, blessedGeneration)
		if err != nil {
			return 0, status.Errorf(codes.Internal, "noded: record blessed generation for %q: %v", workload, err)
		}
		return gen, nil
	}
	if activatorOrigin {
		// ADR embervm/037: node-local self-advancement of a generation is new
		// work and stops while the brick is silenced. The control-plane-issued
		// blessed_generation path above is deliberately NOT gated.
		if err := s.refuseIfSilenced(fmt.Sprintf("blessing-lease self-advance for %q", workload)); err != nil {
			return 0, err
		}
		generations, err := s.volumes.ConsumeGenerationFromLease(workload, 1)
		if err != nil {
			s.logger.Error("noded: blessing lease read or persist failed; falling back to unblessable self-bump", "workload", workload, "err", err)
		} else if len(generations) > 0 {
			gen, recordErr := s.volumes.RecordBlessed(workload, generations[0])
			if recordErr == nil {
				return gen, nil
			}
			s.logger.Warn("noded: blessing lease generation was not ahead of ledger; falling back to unblessable self-bump", "workload", workload, "generation", generations[0], "err", recordErr)
		} else {
			s.logger.Warn("blessing lease exhausted for workload, falling back to unblessable self-bump", "workload", workload)
		}
	}
	if !activatorOrigin {
		return 0, status.Errorf(codes.FailedPrecondition, "noded: writable attach for %q requires a blessed_generation", workload)
	}
	// Only the activator's lease-exhaustion fallback reaches this legacy
	// self-bump; every RPC caller must carry a blessed_generation.
	gen, err := s.volumes.BumpGeneration(workload)
	if err != nil {
		return 0, status.Errorf(codes.Internal, "noded: bump generation for %q: %v", workload, err)
	}
	return gen, nil
}

// startStatefulFresh creates the workload's volume if absent (refusing
// FAILED_PRECONDITION when absent and create_if_missing is false), then cold
// boots exactly like startStatefulCold. It is the only mode that may create the
// volume; RELIGHT's fallback and COLD both require it to already exist.
func (s *Server) startStatefulFresh(ctx context.Context, req *nodev1.StartStatefulRequest, workload string, port uint32, origin nodev1.InstanceOrigin) (*nodev1.StartStatefulResponse, error) {
	// A FRESH stateful boot is new-work placement (it may create the volume). A
	// stale registry (boot cache, no live sync) must refuse it; RELIGHT and COLD
	// operate on EXISTING state and stay allowed so existing warmth is served.
	if err := s.refuseIfStale("StartStateful FRESH"); err != nil {
		return nil, err
	}
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
	return s.coldBootStateful(ctx, req, workload, port, "", origin)
}

// startStatefulCold is an EXPLICIT cold boot from the existing volume (no
// bundle resume attempted). It requires the volume to already exist: unlike
// FRESH it never creates one, matching the proto doc's "explicit cold boot from
// the existing volume". cold_boot_reason is left empty: an explicit COLD boot
// is not a discarded relight, so it carries no reason.
func (s *Server) startStatefulCold(ctx context.Context, req *nodev1.StartStatefulRequest, workload string, port uint32, origin nodev1.InstanceOrigin) (*nodev1.StartStatefulResponse, error) {
	if !s.volumes.Exists(workload) {
		return nil, status.Errorf(codes.FailedPrecondition, "noded: stateful volume for workload %q does not exist (COLD requires an existing volume)", workload)
	}
	return s.coldBootStateful(ctx, req, workload, port, "", origin)
}

// startStatefulRelight resumes the banked bundle (relight_snapshot_ref) iff its
// stamped generation equals the volume's CURRENT generation. On a generation
// mismatch or a missing bundle, it EVICTS the bundle (warmth fails open) and
// falls back to a cold boot from boot_image_ref against the EXISTING volume
// (data fails closed: RELIGHT never creates a volume), setting cold_boot_reason
// to name why the warmth was discarded. The one exception is an UNREADABLE
// ledger: the fallback cold boot's own required generation bump re-reads the
// same corrupt ledger and fails, so an unreadable ledger fails CLOSED (returns
// an error) rather than fabricating a generation for a volume whose ledger it
// cannot trust (which could later false-match a stale bundle). The stale bundle
// is still evicted first, and the volume bytes are never touched, so this is
// fail-closed-but-data-safe, surfaced for an operator to repair.
func (s *Server) startStatefulRelight(ctx context.Context, req *nodev1.StartStatefulRequest, workload string, port uint32, origin nodev1.InstanceOrigin) (*nodev1.StartStatefulResponse, error) {
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
		return s.coldBootStateful(ctx, req, workload, port, reason, origin)
	}

	// Matched pair: resume the bundle. Attach lock first, then bump (the same
	// attach-before-bump ordering the cold-boot path uses, so a concurrent start
	// cannot strand the generation). The generation still bumps BEFORE boot
	// (every mode does), because resuming the memory snapshot re-exposes the
	// SAME writable device to a guest that is about to run again, and any
	// further write must be witnessed by a strictly newer generation than the
	// one the bundle was stamped with, or a future relight of a LATER bank
	// could falsely re-match this now-stale bundle.
	s.releaseOrphanedAttach(workload)
	if err := s.volumes.Attach(workload); err != nil {
		return nil, status.Errorf(codes.FailedPrecondition, "noded: %v", err)
	}
	attached := true
	defer func() {
		if attached {
			s.volumes.Detach(workload)
		}
	}()
	gen, err := s.attachGeneration(workload, req.GetBlessedGeneration(), origin == nodev1.InstanceOrigin_INSTANCE_ORIGIN_ACTIVATOR)
	if err != nil {
		return nil, err
	}

	// Re-acquire the SAME tap IP the bundle was banked with, exactly as serving
	// relight does. A relight resumes the guest from its memory snapshot, which has
	// this IP configured on eth0; a fresh tap with a DIFFERENT IP leaves the resumed
	// guest answering on an address the host tap does not have, so the readiness
	// probe times out ("guest not ready over tap") and the relight fails, which then
	// burns two generations (relight bump + cold fallback bump) and strands the
	// bundle. An absent pin (a bundle banked before this fix) falls back to a fresh
	// tap, so older bundles still relight, just without the IP-match guarantee.
	pinned := s.statefulDriver.StatefulPinnedIP(ref)
	var ip net.IP
	if pinned != "" {
		ip = net.ParseIP(pinned)
		if ip == nil {
			return nil, status.Errorf(codes.Internal, "noded: stateful snapshot %q has a malformed pinned ip %q", ref, pinned)
		}
		if _, aerr := s.servingNet.AllocateTapForIP(ctx, ip); aerr != nil {
			return nil, status.Errorf(codes.FailedPrecondition, "noded: re-acquire pinned stateful ip %s: %v", pinned, aerr)
		}
	} else {
		var aerr error
		if _, ip, aerr = s.servingNet.AllocateTap(ctx); aerr != nil {
			return nil, status.Errorf(codes.ResourceExhausted, "noded: allocate stateful tap: %v", aerr)
		}
	}
	h, err := s.statefulDriver.RestoreStateful(ctx, ref, s.volumes.VolumePath(workload))
	if err != nil {
		s.servingNet.ReleaseTap(ctx, ip)
		return nil, status.Errorf(codes.FailedPrecondition, "noded: relight stateful snapshot %q: %v", ref, err)
	}
	attached = false // ownership passes to finishStatefulStart's detach-on-failure path
	return s.finishStatefulStart(ctx, h, workload, ref, ip, port, gen, true, "", s.cfg.RestoreReadyTimeout, origin)
}

// coldBootStateful resolves boot_image_ref against the serving-images
// inventory (a stateful cold boot reuses the EXACT serving cold-boot mechanic:
// rootfs is drive 1, the handler artifact is drive 2, D-R3.11.2), bumps the
// volume's generation, attaches the volume, allocates a tap, and cold-boots
// with the volume as a third writable drive. Shared by FRESH, COLD, and the
// RELIGHT fallback so all three paths boot identically once the volume exists.
func (s *Server) coldBootStateful(ctx context.Context, req *nodev1.StartStatefulRequest, workload string, port uint32, coldBootReason string, origin nodev1.InstanceOrigin) (*nodev1.StartStatefulResponse, error) {
	bootImageRef := req.GetBootImageRef()
	if bootImageRef == "" {
		return nil, status.Error(codes.InvalidArgument, "noded: boot_image_ref required")
	}
	// boot_image_ref names a built base SNAPSHOT key. A stateful workload is an
	// image-lane, opaque-L4 guest (e.g. Postgres): its base is NOT a serving
	// handler-artifact base (that is a zip-serving-only mechanism, D-R3.11.2), so
	// it lives in the base registry, not the serving-images inventory. Resolve it
	// there and cold-boot the runtime rootfs behind it with NO drive-2 handler.
	base, ok := s.bases.get(bootImageRef)
	if !ok || base.state != nodev1.BaseBuildState_BASE_BUILD_STATE_READY {
		return nil, status.Errorf(codes.FailedPrecondition, "noded: boot image %q is not a ready base on this node", bootImageRef)
	}
	img, ok := s.resolveImageByRef(base.imageDigest)
	if !ok {
		return nil, status.Errorf(codes.FailedPrecondition, "noded: runtime image %q for boot image %q not provisioned on this node", base.imageDigest, bootImageRef)
	}
	harnessInit := img.HarnessInit
	if harnessInit == "" {
		harnessInit = s.cfg.HarnessInit
	}
	res := req.GetResources()

	// Acquire the attach lock BEFORE bumping the generation: the bump is part of
	// the attach, so a concurrent second start for the same workload is refused
	// here and never bumps the ledger. Bumping first (outside the lock) would let
	// a losing racer strand the winner's generation (ledger ahead of the booted
	// VM's stamp), making every later relight cold-boot. Singleton by construction
	// upstream, but the daemon enforces it where the data physically lives.
	s.releaseOrphanedAttach(workload)
	if err := s.volumes.Attach(workload); err != nil {
		return nil, status.Errorf(codes.FailedPrecondition, "noded: %v", err)
	}
	attached := true
	defer func() {
		if attached {
			s.volumes.Detach(workload)
		}
	}()
	gen, err := s.attachGeneration(workload, req.GetBlessedGeneration(), origin == nodev1.InstanceOrigin_INSTANCE_ORIGIN_ACTIVATOR)
	if err != nil {
		return nil, err
	}

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
	// MMDS-lite over boot-args (R4, D-R4.PR-7.1): mmds_env rides ONLY a
	// FRESH/COLD boot (this function), never a RELIGHT (startStatefulRelight's
	// resume path never calls ClaimStateful at all, so mmds_env cannot leak
	// onto a relight by construction). SECURITY: log only the key NAMES, never
	// the request's mmds_env values -- they may be a first-boot secret (e.g. a
	// Postgres password) and this log line must not persist it in plaintext.
	mmdsEnv := req.GetMmdsEnv()
	if len(mmdsEnv) > 0 {
		s.logger.Info("noded: stateful cold boot carrying mmds_env", "workload", workload, "keys", mmdsEnvKeyNamesSorted(mmdsEnv))
	}
	// No handler artifact for an image-lane stateful base (drive 2 is omitted; the
	// writable volume attaches as drive 2 -> /dev/vdb, which the driver signals to
	// the guest dynamically). Pass an empty handler path / zero size.
	h, err := s.statefulDriver.ClaimStateful(ctx, img.RootfsPath, harnessInit, int(res.GetVcpus()), int(res.GetMemMib()), nic, "", 0, s.volumes.VolumePath(workload), req.GetVolumeMount(), mmdsEnv)
	if err != nil {
		s.servingNet.ReleaseTap(ctx, ip)
		return nil, status.Errorf(codes.FailedPrecondition, "noded: cold-boot stateful vm: %v", err)
	}
	attached = false // ownership passes to finishStatefulStart's detach-on-failure path
	return s.finishStatefulStart(ctx, h, workload, "", ip, port, gen, false, coldBootReason, s.cfg.BootReadyTimeout, origin)
}

// finishStatefulStart is the shared tail of every StartStateful path: health-
// gate the guest over the tap by TCP CONNECT, and on success register the live
// stateful VM and start its TCP probe. On a readiness failure it reaps the VM,
// releases the tap, and DETACHES the volume (never rolling the generation
// back), returning FAILED_PRECONDITION.
func (s *Server) finishStatefulStart(ctx context.Context, h substrate.Handle, workload, sourceRef string, ip net.IP, port uint32, generation uint64, wasRelight bool, coldBootReason string, readyBudget time.Duration, origin nodev1.InstanceOrigin) (*nodev1.StartStatefulResponse, error) {
	if err := s.waitStatefulReady(ctx, ip, port, readyBudget); err != nil {
		s.reapStateful(h, ip, workload)
		return nil, status.Errorf(codes.FailedPrecondition, "noded: stateful guest not ready over tap: %v", err)
	}
	if err := s.servingNet.EnsureDNAT(ctx, ip, port); err != nil {
		s.reapStateful(h, ip, workload)
		return nil, status.Errorf(codes.FailedPrecondition, "noded: install stateful DNAT for %s: %v", ip, err)
	}
	probe := serving.StartTCPProbe(s.newTCPProber(), ip, port)
	s.volumes.Bind(workload, h.ID)
	s.statefulVMs.add(&statefulEntry{
		vmID:        h.ID,
		workload:    workload,
		handle:      h,
		ip:          ip,
		port:        port,
		tap:         serving.TapNameForIP(ip),
		generation:  generation,
		origin:      origin,
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

// defaultStatefulResolveTimeout is noded's deadline T for an unresolved
// interruptible-bank checkpoint (ADR embervm/008 open question 2): long enough
// that a healthy control plane always resolves well within it, short enough that a
// DEAD control plane cannot pin a paused VM's live-VM cap slot and memory for more
// than this. A tuning knob to refine alongside the flap guard.
const defaultStatefulResolveTimeout = 30 * time.Second

// statefulPendingAttachGrace bounds how long an unbound writable attach may
// sit before a new start may reclaim it. It is deliberately several times
// BootReadyTimeout (60s), the longest legitimate in-flight start, so a slow
// cold boot can never have its attach stolen out from under it.
const statefulPendingAttachGrace = 5 * time.Minute

const statefulAPISocketProbeTimeout = 100 * time.Millisecond

// releaseOrphanedAttach clears a writable-attach lock whose owning VM the
// registry no longer knows about, so a workload wedged by a lost Detach
// self-heals on its next wake instead of needing a noded restart (#3648).
func (s *Server) releaseOrphanedAttach(workload string) {
	live := ""
	if e, ok := s.statefulVMs.byWorkload(workload); ok {
		socketPath := s.statefulDriver.StatefulAPISocketPath(e.handle)
		if socketPath == "" || statefulAPISocketDead(socketPath) {
			if removed := s.statefulVMs.remove(e.vmID); removed != nil {
				removed.probe.Stop()
				if err := s.driver.Release(context.Background(), removed.handle); err != nil {
					s.logger.Warn("noded: release dead stateful vm", "vm_id", removed.vmID, "err", err)
				}
				if err := s.driver.RemoveBundle(removed.handle.ThreadID); err != nil {
					s.logger.Warn("noded: remove dead stateful vm bundle", "vm_id", removed.vmID, "err", err)
				}
				s.servingNet.ReleaseTap(context.Background(), removed.ip)
				s.signalChange()
			}
		} else {
			live = e.vmID
		}
	}
	if reason, released := s.volumes.ReleaseOrphaned(workload, live, statefulPendingAttachGrace); released {
		s.logger.Warn("noded: reclaimed orphaned writable attach", "workload", workload, "reason", reason)
	}
}

// statefulAPISocketDead recognizes only the definitive local death verdicts
// produced by a missing socket or a socket with no Firecracker listener. Other
// dial errors fail closed and preserve the healthy-attach fast path.
func statefulAPISocketDead(path string) bool {
	conn, err := net.DialTimeout("unix", path, statefulAPISocketProbeTimeout)
	if err == nil {
		_ = conn.Close()
		return false
	}
	return errors.Is(err, os.ErrNotExist) || errors.Is(err, syscall.ENOENT) || errors.Is(err, syscall.ECONNREFUSED)
}

// StopStateful tears down a live stateful VM. BANK pauses it, writes a
// stateful snapshot stamped with the CURRENT volume generation, evicts any
// PRIOR bundle for the workload (at most one banked bundle per workload),
// detaches the volume, destroys the VM, and returns {snapshot_ref, generation,
// size_bytes}. DESTROY tears the VM down with no snapshot and detaches the
// volume. CHECKPOINT is phase one of the interruptible bank (ADR embervm/008): it
// PAUSES the VM and snapshots to a temp, leaving it paused and resumable, and
// returns a checkpoint_token for a later ResolveStateful. Either terminal mode
// releases the tap.
func (s *Server) StopStateful(ctx context.Context, req *nodev1.StopStatefulRequest) (*nodev1.StopStatefulResponse, error) {
	if s.servingNet == nil || s.statefulDriver == nil || s.volumes == nil {
		return nil, status.Error(codes.Unimplemented, "noded: stateful not configured")
	}
	vmID := req.GetVmId()
	switch req.GetMode() {
	case nodev1.StopStatefulMode_STOP_STATEFUL_MODE_BANK:
		return s.stopStatefulBank(ctx, vmID)
	case nodev1.StopStatefulMode_STOP_STATEFUL_MODE_DESTROY:
		return s.stopStatefulDestroy(vmID)
	case nodev1.StopStatefulMode_STOP_STATEFUL_MODE_CHECKPOINT:
		return s.stopStatefulCheckpoint(ctx, vmID)
	default:
		return nil, status.Error(codes.InvalidArgument, "noded: StopStateful mode must be BANK, DESTROY, or CHECKPOINT")
	}
}

// stopStatefulCheckpoint is phase one of the interruptible bank (ADR embervm/008):
// it CHECKPOINTs a live stateful VM (pause + snapshot to a rescan-invisible temp,
// VM left paused) and arms noded's resolve-timeout auto-abort, returning the
// checkpoint token. It reuses beginStop's stop-serialization guard, so a second
// CHECKPOINT (or a BANK) of the same VM is FAILED_PRECONDITION. On a checkpoint
// failure the driver leaves the VM live (resumed), so the stop guard is cleared
// and the VM returns to serving rather than being pinned.
func (s *Server) stopStatefulCheckpoint(ctx context.Context, vmID string) (*nodev1.StopStatefulResponse, error) {
	e, ok := s.statefulVMs.beginStop(vmID)
	if !ok {
		return nil, status.Errorf(codes.FailedPrecondition, "noded: stateful vm %q not checkpointable (unknown or a stop is already in flight)", vmID)
	}
	snapshotRef := newID("state")
	token, err := s.statefulDriver.CheckpointStateful(ctx, e.handle, snapshotRef, e.generation, e.ip.String())
	if err != nil {
		s.statefulVMs.clearInFlight(vmID)
		return nil, status.Errorf(codes.FailedPrecondition, "noded: checkpoint stateful vm %q: %v", vmID, err)
	}
	// Arm the resolve-timeout auto-abort: a dead control plane must not leave the
	// VM paused (burning a cap slot and its memory) with every connection parked.
	// Store the token FIRST, then arm the auto-abort timer: a timer that fires
	// immediately (a very short resolve timeout) then always finds the token set,
	// so its auto-abort works instead of no-opping and leaving the checkpoint
	// without a backstop.
	s.statefulVMs.markCheckpointed(vmID, token)
	timer := time.AfterFunc(s.statefulResolveTimeout, func() { s.autoAbortCheckpoint(vmID, token) })
	s.statefulVMs.setCheckpointTimer(vmID, timer)
	s.signalChange()
	return &nodev1.StopStatefulResponse{CheckpointToken: token, Generation: e.generation}, nil
}

// ResolveStateful is phase two of the interruptible bank (ADR embervm/008): it
// resolves a VM left paused by StopStateful(CHECKPOINT). COMMIT publishes the
// checkpoint's temp as the workload's bundle and destroys the VM; ABORT bumps the
// volume generation, deletes the temp, and resumes the VM (hot). claimResolve is
// the single-resolve gate: exactly one of {this call, the resolve-timeout
// auto-abort} wins, so a COMMIT arriving after the auto-abort is FAILED_PRECONDITION.
func (s *Server) ResolveStateful(ctx context.Context, req *nodev1.ResolveStatefulRequest) (*nodev1.ResolveStatefulResponse, error) {
	if s.servingNet == nil || s.statefulDriver == nil || s.volumes == nil {
		return nil, status.Error(codes.Unimplemented, "noded: stateful not configured")
	}
	// Validate the mode BEFORE claiming the resolve, so an invalid mode never
	// consumes the single-resolve token or strands the paused VM.
	switch req.GetMode() {
	case nodev1.ResolveMode_RESOLVE_MODE_COMMIT, nodev1.ResolveMode_RESOLVE_MODE_ABORT:
	default:
		return nil, status.Error(codes.InvalidArgument, "noded: ResolveStateful mode must be COMMIT or ABORT")
	}
	vmID := req.GetVmId()
	token := req.GetCheckpointToken()
	e, ok := s.statefulVMs.claimResolve(vmID, token)
	if !ok {
		return nil, status.Errorf(codes.FailedPrecondition, "noded: no in-flight checkpoint for vm %q with the given token (unknown or already resolved)", vmID)
	}
	if req.GetMode() == nodev1.ResolveMode_RESOLVE_MODE_COMMIT {
		return s.commitCheckpoint(ctx, e, token)
	}
	return s.abortCheckpoint(ctx, e, token, req.GetBlessedGeneration())
}

// commitCheckpoint publishes the checkpoint's temp as the workload's bundle and
// tears the (already driver-destroyed) VM's tap + volume down, then records the
// bundle (evicting any prior, D-R4 one-bundle-per-workload). The tail mirrors
// stopStatefulBank's bundle bookkeeping exactly. The caller has already claimed
// the resolve (token cleared, timer stopped).
func (s *Server) commitCheckpoint(ctx context.Context, e *statefulEntry, token string) (*nodev1.ResolveStatefulResponse, error) {
	ref, err := s.statefulDriver.ResolveStatefulCommit(ctx, token)
	if err != nil {
		// The commit tore the paused VM down (a commit is destructive); drop the
		// entry and release its tap + volume so the workload cold-boots next.
		s.statefulVMs.remove(e.vmID)
		e.probe.Stop()
		s.servingNet.ReleaseTap(ctx, e.ip)
		s.volumes.Detach(e.workload)
		s.signalChange()
		return nil, status.Errorf(codes.FailedPrecondition, "noded: commit checkpoint for vm %q: %v", e.vmID, err)
	}
	// The driver destroyed the VM at commit; stop the probe, drop the entry, and
	// release the tap + detach the volume (the FC handle is already released).
	e.probe.Stop()
	s.statefulVMs.remove(e.vmID)
	s.servingNet.ReleaseTap(ctx, e.ip)
	s.volumes.Detach(e.workload)
	if prior, ok := s.statefulBundles.byWorkload(e.workload); ok && prior.snapshotRef != ref.ID {
		_ = s.statefulDriver.RemoveStatefulBundle(prior.snapshotRef)
		s.statefulBundles.remove(prior.snapshotRef)
	}
	s.statefulBundles.add(statefulBundleEntry{
		snapshotRef:     ref.ID,
		workload:        e.workload,
		generation:      e.generation,
		sizeBytes:       ref.SizeBytes,
		createdAtUnixMs: time.Now().UnixMilli(),
	})
	// Persist the workload binding beside the committed bundle (#38 F1), same as
	// the direct-bank path.
	s.writeStatefulWorkloadSidecar(ref.ID, e.workload)
	// Async off-node write-back (R6): the committed bundle and its paired volume,
	// fire-and-forget (identical to stopStatefulBank's tail).
	s.enqueueExport(&nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_STATEFUL, Workload: e.workload, Ref: ref.ID})
	s.enqueueExport(&nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_VOLUME, Workload: e.workload})
	s.signalChange()
	return &nodev1.ResolveStatefulResponse{SnapshotRef: ref.ID, Generation: e.generation, SizeBytes: uint64(ref.SizeBytes)}, nil
}

// abortCheckpoint returns a checkpointed VM to serving on the same process image.
// Order (ADR embervm/008): advance the volume generation, THEN delete the temp +
// resume (the driver does the latter two). The advance witnesses the resumed
// guest's future writes in the pair key even if resume then fails; there is no
// un-bump. A generation-advance failure is not fatal: delete-before-resume alone
// closes the crash leak (guarantee 3), so it logs and proceeds on the current
// generation rather than wedging. If the resume itself fails the driver destroyed
// the VM, so this degrades to a next-wake cold boot. The caller has already
// claimed the resolve.
//
// blessedGeneration (R7, ADR embervm/011, standing decision 4): when the control
// plane issued a generation for this abort (the normal CP-driven resolve path), we
// RecordBlessed it so genFile == blessedFile and the node reports
// generation_blessed:true. A legitimate abort must never look like an unblessed
// self-bump, or StatefulStore.update_quarantine quarantines the volume and every
// wake fails closed. Zero means the legacy self-bump lane: only the node's own
// resolve-timeout auto-abort (autoAbortCheckpoint), where no control plane is
// reachable to issue a generation, so the resulting unblessed state is a correct
// fail-closed signal rather than a bug.
func (s *Server) abortCheckpoint(ctx context.Context, e *statefulEntry, token string, blessedGeneration uint64) (*nodev1.ResolveStatefulResponse, error) {
	gen, genErr := s.recordAbortGeneration(e.workload, blessedGeneration)
	if genErr != nil {
		s.logger.Warn("noded: advance generation on checkpoint abort failed; proceeding (delete-before-resume still protects)", "workload", e.workload, "err", genErr)
		gen = e.generation
	}
	if err := s.statefulDriver.ResolveStatefulAbort(ctx, token); err != nil {
		// Resume failed; the driver tore the VM down (dead-handle discipline).
		// Degrade to a next-wake cold boot: drop the entry, release tap + volume.
		s.statefulVMs.remove(e.vmID)
		e.probe.Stop()
		s.servingNet.ReleaseTap(ctx, e.ip)
		s.volumes.Detach(e.workload)
		s.signalChange()
		return nil, status.Errorf(codes.FailedPrecondition, "noded: abort checkpoint for vm %q (resume failed, VM destroyed): %v", e.vmID, err)
	}
	// Resumed hot: record the advanced generation, clear the checkpoint + stop guard.
	s.statefulVMs.resumeFromCheckpoint(e.vmID, gen)
	s.signalChange()
	// Report the generation so the control plane's blessing ledger can confirm the
	// value it issued was recorded (it blessed the same number pre-dispatch). Zero
	// for the legacy self-bump lane, where the CP has no matching blessing.
	return &nodev1.ResolveStatefulResponse{Generation: gen}, nil
}

// recordAbortGeneration advances the volume generation for a checkpoint abort. A
// nonzero blessedGeneration (a CP-driven resolve) is recorded via RecordBlessed so
// genFile == blessedFile and the node self-certifies generation_blessed:true;
// RecordBlessed's gen > current guard keeps it strictly monotonic. Zero is the
// legacy self-bump lane (autoAbortCheckpoint only): BumpGeneration advances genFile
// without the blessed marker, which correctly reads unblessed since no control
// plane witnessed it.
func (s *Server) recordAbortGeneration(workload string, blessedGeneration uint64) (uint64, error) {
	if blessedGeneration > 0 {
		return s.volumes.RecordBlessed(workload, blessedGeneration)
	}
	return s.volumes.BumpGeneration(workload)
}

// autoAbortCheckpoint is the resolve-timeout backstop (ADR embervm/008): if a
// CHECKPOINT is left unresolved for statefulResolveTimeout, noded aborts it
// itself (resume, discard temp) so a dead control plane cannot pin a paused VM.
// It claims the resolve first, so it no-ops if the control plane already resolved.
// Deliberately NOT gated by the ADR embervm/037 silence timeout: resuming an
// already-live paused VM is recovery of live work, the same posture as the
// activators' splice-through-while-silenced, not new work.
func (s *Server) autoAbortCheckpoint(vmID, token string) {
	e, ok := s.statefulVMs.claimResolve(vmID, token)
	if !ok {
		return // already resolved by the control plane
	}
	s.logger.Warn("noded: interruptible-bank checkpoint resolve-timeout, auto-aborting", "vm_id", vmID, "workload", e.workload)
	// blessedGeneration 0: no control plane is reachable to issue a generation for
	// this self-driven abort, so the legacy self-bump lane applies. The resulting
	// unblessed volume may quarantine on the next wake, which is correct
	// fail-closed behaviour for a resume the CP ledger never witnessed (see the
	// runbook: recover by blessing the reported generation forward).
	if _, err := s.abortCheckpoint(context.Background(), e, token, 0); err != nil {
		s.logger.Warn("noded: checkpoint auto-abort failed", "vm_id", vmID, "err", err)
	}
}

// stopStatefulBank snapshots a live stateful VM to a banked bundle stamped with
// its CURRENT volume generation, evicts any prior bundle for the same
// workload (D-R4: at most one banked bundle per workload), destroys the VM,
// detaches the volume, and records the new banked bundle.
func (s *Server) stopStatefulBank(ctx context.Context, vmID string) (*nodev1.StopStatefulResponse, error) {
	e, ok := s.statefulVMs.beginStop(vmID)
	if !ok {
		return nil, status.Errorf(codes.FailedPrecondition, "noded: stateful vm %q not bankable (unknown or a stop is already in flight)", vmID)
	}
	e.probe.Stop()
	snapshotRef := newID("state")
	ref, err := s.statefulDriver.SnapshotStateful(ctx, e.handle, snapshotRef, e.generation, e.ip.String())
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
	// not block recording the new (successfully banked) one. Keyed off the live
	// entry's workload (e.workload), the authoritative fact, not the request
	// trace, so the one-bundle-per-workload eviction cannot misfire on a trace
	// that disagrees with the VM's actual workload.
	if prior, ok := s.statefulBundles.byWorkload(e.workload); ok && prior.snapshotRef != ref.ID {
		_ = s.statefulDriver.RemoveStatefulBundle(prior.snapshotRef)
		s.statefulBundles.remove(prior.snapshotRef)
	}
	s.statefulBundles.add(statefulBundleEntry{
		snapshotRef:     ref.ID,
		workload:        e.workload,
		generation:      e.generation,
		sizeBytes:       ref.SizeBytes,
		createdAtUnixMs: time.Now().UnixMilli(),
	})
	// Persist the workload binding beside the bundle so a boot-scan reconciliation
	// (which knows only the opaque ref) can recover it and compose the remote (S3)
	// prefix at eviction time (#38 F1). Written after the driver published the
	// complete bundle, so a crash before this leaves a usable bundle that seeds
	// empty and is safely SKIPPED by the reaper.
	s.writeStatefulWorkloadSidecar(ref.ID, e.workload)
	// Async off-node write-back (R6): a stateful bank ships BOTH the bundle and its
	// paired volume (vol.img + gen); the volume export skips when its generation is
	// unchanged since the last export. Both are fire-and-forget (never blocking).
	s.enqueueExport(&nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_STATEFUL, Workload: e.workload, Ref: ref.ID})
	s.enqueueExport(&nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_VOLUME, Workload: e.workload})
	s.signalChange()
	return &nodev1.StopStatefulResponse{SnapshotRef: ref.ID, Generation: e.generation, SizeBytes: uint64(ref.SizeBytes)}, nil
}

// stopStatefulDestroy tears a stateful VM down with no snapshot, releases its
// tap, and detaches its volume. Idempotent: an unknown vm_id returns confirmed.
// teardown_confirmed is true only when the reap fully completed; a reap failure
// returns an error, not a false confirm (ADR embervm/014 decision 5). The volume
// FILE survives (destroy tears down the VM, not its durable volume); DeleteVolume
// is the separate explicit data verb.
func (s *Server) stopStatefulDestroy(vmID string) (*nodev1.StopStatefulResponse, error) {
	if removed := s.statefulVMs.remove(vmID); removed != nil {
		removed.probe.Stop()
		if err := s.reapStateful(removed.handle, removed.ip, removed.workload); err != nil {
			return nil, status.Errorf(codes.Internal, "noded: reap stateful vm %q: %v", vmID, err)
		}
		s.signalChange()
	}
	return &nodev1.StopStatefulResponse{TeardownConfirmed: true}, nil
}

// reapStateful tears a stateful VM down (release the FC process + bundle),
// releases its tap + IP, and detaches its volume so a subsequent StartStateful
// for the same workload is not refused by a stale attach lock. Best-effort,
// mirroring reapServing plus the volume detach.
func (s *Server) reapStateful(h substrate.Handle, ip net.IP, workload string) error {
	err := s.reap(h, func() {})
	if s.servingNet != nil {
		s.servingNet.ReleaseTap(context.Background(), ip)
	}
	if s.volumes != nil {
		s.volumes.Detach(workload)
	}
	return err
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
	var err error
	if req.GetLineageId() != "" {
		// Deliberately prune stale attachment records before DeleteSession. Removing
		// this call strands crashed lineages forever.
		s.PruneStaleAttach(workload, req.GetLineageId())
		err = s.volumes.DeleteSession(workload, req.GetLineageId())
	} else {
		err = s.volumes.Delete(workload)
	}
	if err != nil {
		return nil, status.Errorf(codes.FailedPrecondition, "noded: delete volume for %q: %v", workload, err)
	}
	s.signalChange()
	return &nodev1.DeleteVolumeResponse{}, nil
}

// ArchiveVolume schedules a session workspace export and fast-ACKs. Workspaces
// have no generation ledger, so the lineage's single-writer invariant makes the
// store's checksum comparison the durability gate in the worker.
func (s *Server) ArchiveVolume(ctx context.Context, req *nodev1.ArchiveVolumeRequest) (*nodev1.ArchiveVolumeResponse, error) {
	if req.GetWorkload() == "" || req.GetLineageId() == "" {
		return nil, status.Error(codes.InvalidArgument, "noded: workload and lineage_id required")
	}
	if s.store == nil {
		return nil, status.Error(codes.FailedPrecondition, "noded: object store not configured; archive unavailable")
	}
	if s.volumes == nil {
		return nil, status.Error(codes.FailedPrecondition, "noded: volume manager not configured")
	}
	if s.lineageAttached(req.GetWorkload(), req.GetLineageId()) {
		s.logger.Warn("noded: skip archive of attached session workspace", "workload", req.GetWorkload(), "lineage_id", req.GetLineageId())
		return &nodev1.ArchiveVolumeResponse{Skipped: true}, nil
	}
	path := s.volumes.SessionVolumePath(req.GetWorkload(), req.GetLineageId())
	if _, err := os.Stat(path); err != nil {
		if os.IsNotExist(err) {
			return nil, status.Error(codes.NotFound, "noded: session workspace not found")
		}
		return nil, status.Errorf(codes.FailedPrecondition, "noded: session workspace unavailable: %v", err)
	}
	ref := &nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_SESSION_WORKSPACE, Workload: req.GetWorkload(), Ref: req.GetLineageId()}
	// Always enqueue: the workspace key is stable but its content mutates, so a
	// presence probe cannot prove the store copy is current. Export's checksum
	// compare in the worker skips the upload when nothing changed, which keeps
	// repeat drains cheap without freezing the durable copy at its first archive.
	s.enqueueExport(ref)
	return &nodev1.ArchiveVolumeResponse{}, nil
}
