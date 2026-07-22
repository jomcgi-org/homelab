package server

import (
	"context"
	"net"
	"time"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	nodev1 "github.com/jomcgi/homelab/projects/embervm/proto/embervm/node/v1"

	"github.com/jomcgi/homelab/projects/embervm/noded/groupclock"
	"github.com/jomcgi/homelab/projects/embervm/noded/serving"
	"github.com/jomcgi/homelab/projects/embervm/noded/substrate"
)

// realGroupClock is the production groupClock: it resyncs a relit member guest's
// wall clock over the port-1024 length-prefixed JSON agent channel (the frozen R5
// contract) using the host wall clock, and verifies the read-back within one second.
// It wraps groupclock.Resync with a real VsockDialer; tests inject a fake groupClock
// instead so the resync path is exercised without a guest.
type realGroupClock struct{}

func (realGroupClock) Resync(ctx context.Context, udsPath string) error {
	return groupclock.Resync(ctx, groupclock.VsockDialer{}, udsPath, time.Now)
}

// StartGroupMember boots (FRESH) or resumes (RELIGHT) exactly one composite-group
// member on its group bridge. FRESH cold-boots the member's image rootfs on the
// pinned tap with its first-boot env delivered via the MMDS-lite boot-args seam
// (env is FRESH-only) and TCP-health-gates {ip, health_port}. RELIGHT recreates the
// member's pinned tap world (same tap name + MAC + IP the member had at bank time,
// the D-R3.4.1 pin on the group bridge), resumes the banked bundle, runs the
// clock-resync handshake against the guest control agent over vsock port 1024, fails
// the call (FAILED_PRECONDITION, with the delta in the error) when the read-back
// clock is more than one second off, and only then TCP-health-gates. A member VM
// counts against max_live_vms and is EXCLUDED from primed_vm_ids.
func (s *Server) StartGroupMember(ctx context.Context, req *nodev1.StartGroupMemberRequest) (*nodev1.StartGroupMemberResponse, error) {
	if s.groupNet == nil || s.groupDriver == nil {
		return nil, status.Error(codes.Unimplemented, "noded: group member lifecycle not configured")
	}
	if s.isDraining() {
		return nil, status.Error(codes.Unavailable, "noded: draining")
	}
	if s.cfg.MaxLiveVMs > 0 && s.liveVMCount() >= s.cfg.MaxLiveVMs {
		return nil, status.Errorf(codes.ResourceExhausted, "noded: node live-VM cap %d reached", s.cfg.MaxLiveVMs)
	}
	groupInstanceID := req.GetGroupInstanceId()
	if groupInstanceID == "" {
		return nil, status.Error(codes.InvalidArgument, "noded: group_instance_id required")
	}
	if !s.groupNet.Has(groupInstanceID) {
		return nil, status.Errorf(codes.FailedPrecondition, "noded: group network %q does not exist on this node", groupInstanceID)
	}
	memberName := req.GetMemberName()
	if memberName == "" {
		return nil, status.Error(codes.InvalidArgument, "noded: member_name required")
	}
	healthPort := req.GetHealthPort()
	if healthPort == 0 {
		return nil, status.Error(codes.InvalidArgument, "noded: health_port required")
	}
	pinnedIP := net.ParseIP(req.GetIp())
	if pinnedIP == nil {
		return nil, status.Errorf(codes.InvalidArgument, "noded: ip %q is not a valid address", req.GetIp())
	}

	switch req.GetMode() {
	case nodev1.StartGroupMemberMode_START_GROUP_MEMBER_MODE_FRESH:
		return s.startGroupMemberFresh(ctx, req, groupInstanceID, memberName, pinnedIP, healthPort)
	case nodev1.StartGroupMemberMode_START_GROUP_MEMBER_MODE_RELIGHT:
		return s.startGroupMemberRelight(ctx, req, groupInstanceID, memberName, pinnedIP, healthPort)
	default:
		return nil, status.Error(codes.InvalidArgument, "noded: StartGroupMember mode must be FRESH or RELIGHT")
	}
}

// startGroupMemberFresh cold-boots a member from its image source on the pinned tap.
// It resolves the FRESH source (a built base + its runtime image, exactly like a
// stateful boot_image_ref: a NIC cold boot cannot resume a vsock-only base memory
// snapshot, D-R3.4.2), pins the member tap on the group bridge, cold-boots with the
// first-boot env carried, and health-gates {ip, health_port}. A readiness failure
// reaps the VM and removes the tap.
func (s *Server) startGroupMemberFresh(ctx context.Context, req *nodev1.StartGroupMemberRequest, groupInstanceID, memberName string, pinnedIP net.IP, healthPort uint32) (*nodev1.StartGroupMemberResponse, error) {
	// A FRESH member cold boot is new-work placement. A stale registry (boot
	// cache, no live sync) refuses it; RELIGHT of an existing member bundle stays
	// allowed so existing warmth is served.
	if err := s.refuseIfStale("StartGroupMember FRESH"); err != nil {
		return nil, err
	}
	source := req.GetSource()
	if source == "" {
		return nil, status.Error(codes.InvalidArgument, "noded: source required for FRESH")
	}
	rootfsPath, harnessInit, err := s.resolveGroupMemberBoot(source)
	if err != nil {
		return nil, err
	}

	tap, ip, err := s.pinGroupMemberTap(ctx, groupInstanceID, memberName, req.GetMemberIndex(), pinnedIP)
	if err != nil {
		return nil, err
	}

	res := req.GetResources()
	nic := substrate.NICSpec{
		HostDevName: tap,
		GuestMAC:    s.groupMemberMAC(groupInstanceID, memberName, req.GetMemberIndex()),
		IP:          ip.String(),
		GatewayIP:   s.groupNet.GatewayIP(groupInstanceID).String(),
		PrefixLen:   s.groupNet.PrefixLen(groupInstanceID),
		IfaceName:   "eth0",
		ServingPort: healthPort,
	}
	// MMDS-lite (D-R4.PR-7.1): env rides a FRESH cold boot ONLY; a RELIGHT never
	// reaches this path (it resumes a memory snapshot), so env cannot leak onto a
	// relight by construction. SECURITY: log only key names, never values (env may
	// carry a first-boot secret), mirroring the stateful path.
	env := req.GetEnv()
	if len(env) > 0 {
		s.logger.Info("noded: group member fresh boot carrying env", "group", groupInstanceID, "member", memberName, "keys", mmdsEnvKeyNamesSorted(env))
	}
	h, err := s.groupDriver.ClaimGroupMember(ctx, rootfsPath, harnessInit, int(res.GetVcpus()), int(res.GetMemMib()), nic, env)
	if err != nil {
		s.groupNet.RemoveMemberTap(ctx, groupInstanceID, tap, ip)
		return nil, status.Errorf(codes.FailedPrecondition, "noded: cold-boot group member: %v", err)
	}
	return s.finishGroupMemberStart(ctx, h, groupInstanceID, memberName, req.GetMemberIndex(), tap, ip, healthPort, req.GetEntryGuestPort(), false, "", groupMemberReadyBudget(req, s.cfg.BootReadyTimeout))
}

// groupMemberReadyBudget resolves the readiness budget for one member start: the
// request's ready_budget_seconds when set (the control plane forwards the
// workload's wakeTimeoutSeconds so daemon gate and CP wake bound share one
// policy), else the daemon default for the start mode. Without the override a
// member that needs longer than the daemon default (a k3s agent's kubelet opens
// :10250 only after the full join) is reaped here while the CP is still waiting
// on its own, longer bound.
func groupMemberReadyBudget(req *nodev1.StartGroupMemberRequest, fallback time.Duration) time.Duration {
	if secs := req.GetReadyBudgetSeconds(); secs > 0 {
		return time.Duration(secs) * time.Second
	}
	return fallback
}

// startGroupMemberRelight recreates the member's pinned tap world, resumes the banked
// bundle, runs the clock-resync handshake (failing the call if the read-back clock is
// off by more than one second), and health-gates. The bundle is resolved from the
// snapshot_ref (group/<set_id>/<member_name>): the ref names the set and member, so
// the daemon re-derives the bundle path from it. On a failed restore the bundle is
// NEVER deleted (a lost member surfaces loudly).
func (s *Server) startGroupMemberRelight(ctx context.Context, req *nodev1.StartGroupMemberRequest, groupInstanceID, memberName string, pinnedIP net.IP, healthPort uint32) (*nodev1.StartGroupMemberResponse, error) {
	ref := req.GetSnapshotRef()
	if ref == "" {
		return nil, status.Error(codes.InvalidArgument, "noded: snapshot_ref required for RELIGHT")
	}
	setID, refMember, ok := parseGroupMemberRef(ref)
	if !ok {
		return nil, status.Errorf(codes.InvalidArgument, "noded: snapshot_ref %q is not a group/<set_id>/<member> ref", ref)
	}
	if refMember != memberName {
		return nil, status.Errorf(codes.InvalidArgument, "noded: snapshot_ref %q member %q does not match request member %q", ref, refMember, memberName)
	}

	// Recreate the pinned world FIRST (same tap name + MAC + IP), because the resumed
	// guest keeps its baked eth0 and a snapshot restore never re-runs kernel init:
	// the tap MUST exist before the resume or the guest's NIC black-holes.
	tap, ip, err := s.pinGroupMemberTap(ctx, groupInstanceID, memberName, req.GetMemberIndex(), pinnedIP)
	if err != nil {
		return nil, err
	}
	h, err := s.groupDriver.RestoreGroupMember(ctx, setID, memberName)
	if err != nil {
		s.groupNet.RemoveMemberTap(ctx, groupInstanceID, tap, ip)
		return nil, status.Errorf(codes.FailedPrecondition, "noded: relight group member %q: %v", ref, err)
	}

	// Clock resync BEFORE the health gate: a bad clock must fail the relight early,
	// over the port-1024 length-prefixed JSON agent channel (NOT the old HTTP
	// /shim/clock path). A read-back more than one second off fails the call.
	uds := s.driver.VsockUDSPath(h.ThreadID)
	clockCtx, cancelClock := context.WithTimeout(ctx, s.cfg.RestoreReadyTimeout)
	clockErr := s.groupClock.Resync(clockCtx, uds)
	cancelClock()
	if clockErr != nil {
		s.reapGroupMember(h, groupInstanceID, tap, ip)
		return nil, status.Errorf(codes.FailedPrecondition, "noded: group member %q clock resync failed: %v", ref, clockErr)
	}

	return s.finishGroupMemberStart(ctx, h, groupInstanceID, memberName, req.GetMemberIndex(), tap, ip, healthPort, req.GetEntryGuestPort(), true, ref, groupMemberReadyBudget(req, s.cfg.RestoreReadyTimeout))
}

// finishGroupMemberStart is the shared tail of both member start paths: TCP-health-
// gate {ip, health_port}, register the live member, and start its probe. On a
// readiness failure it reaps the VM and removes the tap, returning
// FAILED_PRECONDITION (no half-alive member is ever published).
func (s *Server) finishGroupMemberStart(ctx context.Context, h substrate.Handle, groupInstanceID, memberName string, memberIndex uint32, tap string, ip net.IP, healthPort, entryGuestPort uint32, wasRelight bool, snapshotRef string, readyBudget time.Duration) (*nodev1.StartGroupMemberResponse, error) {
	if err := s.waitStatefulReady(ctx, ip, healthPort, readyBudget); err != nil {
		// Loud on purpose: this reap is the daemon KILLING a member that missed its
		// readiness budget. Silent, it reads as a mystery VM disappearance (the R6
		// Gate-1 drill lost both k3s agents here with no trace).
		s.logger.Warn("noded: reaping group member (readiness gate missed)",
			"group", groupInstanceID, "member", memberName, "vm", h.ID,
			"ip", ip.String(), "health_port", healthPort, "budget", readyBudget.String(), "err", err.Error())
		s.reapGroupMember(h, groupInstanceID, tap, ip)
		return nil, status.Errorf(codes.FailedPrecondition, "noded: group member %q not ready over tap within %s: %v", memberName, readyBudget, err)
	}
	// Install the entry DNAT for the ENTRY member (entry_guest_port > 0) so the entry
	// endpoint the control plane publishes, {pod_ip, vmPort}, actually routes to this
	// member's tap:entry_guest_port. Mirrors serving/stateful, which call EnsureDNAT
	// inline in their start handlers. Refreshed on every entry-member start (fresh or
	// relight), so it always points at the live entry tap; DeleteGroupNetwork drops it
	// with the group record on teardown. A non-entry member (0) installs nothing.
	if entryGuestPort > 0 {
		if err := s.groupNet.EnsureEntryDNAT(ctx, groupInstanceID, ip, entryGuestPort); err != nil {
			s.reapGroupMember(h, groupInstanceID, tap, ip)
			return nil, status.Errorf(codes.Internal, "noded: install group entry DNAT for member %q: %v", memberName, err)
		}
	}
	probe := serving.StartTCPProbe(s.newGroupTCPProber(), ip, healthPort)
	s.groupMembers.add(&groupMemberEntry{
		vmID:            h.ID,
		groupInstanceID: groupInstanceID,
		memberName:      memberName,
		memberIndex:     memberIndex,
		ip:              ip,
		tap:             tap,
		port:            healthPort,
		handle:          h,
		snapshotRef:     snapshotRef,
		probe:           probe,
	})
	s.signalChange()
	resp := &nodev1.StartGroupMemberResponse{
		VmId:       h.ID,
		Ip:         ip.String(),
		WasRelight: wasRelight,
	}
	// The ENTRY member's response carries the daemon's own projection of the entry
	// endpoint, {noded pod IP, vmPort}: the DNAT installed above lives in THIS
	// pod's netns, so this address (not the control plane's own pod IP) is the one
	// the CP must publish. Empty when DNAT is disabled (no pod IP configured).
	if entryGuestPort > 0 {
		epIP, epPort := s.groupNet.EntryEndpoint(ip, entryGuestPort)
		if epIP != "" && epIP != ip.String() {
			resp.EndpointIp = epIP
			resp.EndpointPort = epPort
		}
	}
	return resp, nil
}

// StopGroupMember tears down one live member VM. BANK pauses it, snapshots it under
// group/<set_id>/<member_name>/, records the banked bundle, destroys the VM, and
// returns {snapshot_ref, size_bytes}. DESTROY tears the VM down with no snapshot.
// Either mode removes the member tap and releases its pinned IP. A concurrent stop
// on the same vm_id is refused FAILED_PRECONDITION.
func (s *Server) StopGroupMember(ctx context.Context, req *nodev1.StopGroupMemberRequest) (*nodev1.StopGroupMemberResponse, error) {
	if s.groupNet == nil || s.groupDriver == nil {
		return nil, status.Error(codes.Unimplemented, "noded: group member lifecycle not configured")
	}
	vmID := req.GetVmId()
	switch req.GetMode() {
	case nodev1.StopGroupMemberMode_STOP_GROUP_MEMBER_MODE_BANK:
		return s.stopGroupMemberBank(ctx, req, vmID)
	case nodev1.StopGroupMemberMode_STOP_GROUP_MEMBER_MODE_DESTROY:
		return s.stopGroupMemberDestroy(ctx, vmID)
	default:
		return nil, status.Error(codes.InvalidArgument, "noded: StopGroupMember mode must be BANK or DESTROY")
	}
}

// stopGroupMemberBank pauses a live member and snapshots it into the caller-supplied
// set directory (group/<set_id>/<member_name>/), records the banked bundle, destroys
// the VM, and removes its tap. The per-member pause->snapshot timing is measured and
// logged for the closure gate (noded owns its OWN per-member timing; the pause-spread
// across a SET is the control plane's, which orchestrates the per-member bank calls).
func (s *Server) stopGroupMemberBank(ctx context.Context, req *nodev1.StopGroupMemberRequest, vmID string) (*nodev1.StopGroupMemberResponse, error) {
	setID := req.GetSetId()
	memberName := req.GetMemberName()
	if setID == "" || memberName == "" {
		return nil, status.Error(codes.InvalidArgument, "noded: set_id and member_name required for BANK")
	}
	e, ok := s.groupMembers.beginStop(vmID)
	if !ok {
		return nil, status.Errorf(codes.FailedPrecondition, "noded: group member %q not bankable (unknown or a stop is already in flight)", vmID)
	}
	if memberName != e.memberName {
		// The bank writes under the caller-supplied member_name; guard against a
		// request that names a different member than the live VM (a set-dir mixup).
		e.mu.Lock()
		e.inFlight = false
		e.mu.Unlock()
		return nil, status.Errorf(codes.InvalidArgument, "noded: member_name %q does not match live member %q for vm %q", memberName, e.memberName, vmID)
	}
	e.probe.Stop()
	// SnapshotGroupMember pauses (no cross-VM barrier: standing decision 10) then
	// writes the snapshot; the daemon issues NO guest-agent command between the pause
	// decision and the snapshot, so the bank never races an in-flight agent command.
	pauseStart := time.Now()
	ref, err := s.groupDriver.SnapshotGroupMember(ctx, e.handle, setID, memberName)
	bankDuration := time.Since(pauseStart)
	if err != nil {
		// A bank is destructive: SnapshotGroupMember tore the VM down on failure, so
		// drop the dead entry and remove its tap rather than misreport capacity.
		s.groupMembers.remove(vmID)
		s.groupNet.RemoveMemberTap(ctx, e.groupInstanceID, e.tap, e.ip)
		return nil, status.Errorf(codes.FailedPrecondition, "noded: bank group member %q: %v", vmID, err)
	}
	if removed := s.groupMembers.remove(vmID); removed != nil {
		s.reapGroupMember(removed.handle, removed.groupInstanceID, removed.tap, removed.ip)
	}
	s.logger.Info("noded: banked group member", "group", e.groupInstanceID, "set", setID, "member", memberName, "ref", ref.ID, "pause_to_snapshot_ms", bankDuration.Milliseconds())
	s.groupBundles.add(groupBundleEntry{
		setID:           setID,
		memberName:      memberName,
		groupInstanceID: e.groupInstanceID,
		snapshotRef:     ref.ID,
		sizeBytes:       ref.SizeBytes,
		createdAtUnixMs: time.Now().UnixMilli(),
	})
	// Persist the group_instance_id binding beside the member bundle so a boot-scan
	// reconciliation (which reads only set_id + member_name off the dir) can recover
	// it and compose the remote (S3) GROUP_SET prefix at eviction time (#38 F2).
	s.writeGroupInstanceSidecar(setID, memberName, e.groupInstanceID)
	// Async off-node write-back (R6): a group set is the export unit, so enqueue
	// the SET (keyed by group instance + set_id) fire-and-forget after each member
	// bank. The dedupe coalesces overlapping member banks of the same set; the
	// export walks whatever is on disk at run time and the checksum-compare re-uploads
	// on the next member bank until the set converges complete. Never blocks the bank.
	s.enqueueExport(&nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_GROUP_SET, Workload: e.groupInstanceID, Ref: setID})
	s.signalChange()
	return &nodev1.StopGroupMemberResponse{SnapshotRef: ref.ID, SizeBytes: uint64(ref.SizeBytes)}, nil
}

// stopGroupMemberDestroy tears a member VM down with no snapshot, removes its tap,
// and releases its pinned IP. Idempotent: an unknown vm_id returns OK.
func (s *Server) stopGroupMemberDestroy(ctx context.Context, vmID string) (*nodev1.StopGroupMemberResponse, error) {
	// Serialize against a concurrent stop the same way BANK does, so a DESTROY racing
	// a BANK (or another DESTROY) on one vm_id cannot double-reap.
	e, ok := s.groupMembers.beginStop(vmID)
	if !ok {
		// Unknown id is an idempotent no-op; a stop already in flight is refused.
		if s.groupMembers.get(vmID) == nil {
			return &nodev1.StopGroupMemberResponse{}, nil
		}
		return nil, status.Errorf(codes.FailedPrecondition, "noded: group member %q stop already in flight", vmID)
	}
	// beginStop returned the entry marked in-flight; drop it from the registry and
	// reap it (the entry is the authoritative handle/tap/ip to tear down).
	s.groupMembers.remove(vmID)
	e.probe.Stop()
	s.reapGroupMember(e.handle, e.groupInstanceID, e.tap, e.ip)
	s.signalChange()
	return &nodev1.StopGroupMemberResponse{}, nil
}

// reapGroupMember tears a member VM down (release the FC process + bundle) and
// removes its tap + pinned IP from the group bridge. Best-effort, mirroring
// reapStateful minus the volume detach (a member has no writable volume).
func (s *Server) reapGroupMember(h substrate.Handle, groupInstanceID, tap string, ip net.IP) {
	s.reap(h, func() {})
	if s.groupNet != nil {
		s.groupNet.RemoveMemberTap(context.Background(), groupInstanceID, tap, ip)
	}
}

// pinGroupMemberTap pins a member's NIC world on the group bridge (derive + verify
// against the request IP, reserve the IP, create the tap), mapping the manager's
// errors to the right gRPC codes. Shared by FRESH and RELIGHT.
func (s *Server) pinGroupMemberTap(ctx context.Context, groupInstanceID, memberName string, memberIndex uint32, pinnedIP net.IP) (tap string, ip net.IP, err error) {
	tap, _, terr := s.groupNet.EnsureMemberTap(ctx, groupInstanceID, memberName, memberIndex, pinnedIP)
	if terr != nil {
		return "", nil, status.Errorf(codes.FailedPrecondition, "noded: pin group member tap: %v", terr)
	}
	return tap, pinnedIP, nil
}

// groupMemberMAC derives a member's deterministic MAC via the manager's read-only
// addressing (no reservation), for the NIC spec. A derivation error leaves the MAC
// empty (FC then assigns one), which never happens for a group already validated by
// EnsureMemberTap above, so this is a safe convenience.
func (s *Server) groupMemberMAC(groupInstanceID, memberName string, memberIndex uint32) string {
	_, mac, _, err := s.groupNet.MemberAddressingFor(groupInstanceID, memberName, memberIndex)
	if err != nil {
		return ""
	}
	return mac
}

// resolveGroupMemberBoot resolves a FRESH member source to its bootable rootfs +
// harness init. Unlike the stateful cold-boot path (which resumes a built base
// snapshot and so keys the base registry), a composite member FRESH is a plain
// rootfs cold boot (plan Task 6: "FRESH cold-boots the image rootfs"; a NIC cold
// boot never resumes a vsock-only base memory snapshot, D-R3.4.2). So the source
// names a provisioned runtime image directly, not a built base: the control plane
// sends member.image_ref as the source, the per-member rootfs is staged on the
// node by the noded init-container base builder, and s.cfg.Images is the node-side
// image identity table (image_ref -> {rootfsPath, harnessInit}). Resolving through
// s.bases here (keyed by baseKeyFor(workload, image_ref, revision, vendor),
// never the raw image_ref) could never match the source the control plane sends,
// so every
// composite member start failed with "not a ready base"; a composite cluster could
// not boot at all until this resolved the image directly.
func (s *Server) resolveGroupMemberBoot(source string) (rootfsPath, harnessInit string, err error) {
	img, ok := s.resolveImageByRef(source)
	if !ok {
		return "", "", status.Errorf(codes.FailedPrecondition, "noded: group member source %q is not a provisioned image on this node", source)
	}
	harnessInit = img.HarnessInit
	if harnessInit == "" {
		harnessInit = s.cfg.HarnessInit
	}
	return img.RootfsPath, harnessInit, nil
}

// parseGroupMemberRef splits a group member snapshot_ref (group/<set_id>/<member>)
// into (set_id, member_name). It returns ok=false for any ref that is not exactly
// three path segments led by "group".
func parseGroupMemberRef(ref string) (setID, memberName string, ok bool) {
	parts := splitPath(ref)
	if len(parts) != 3 || parts[0] != "group" || parts[1] == "" || parts[2] == "" {
		return "", "", false
	}
	return parts[1], parts[2], true
}

// splitPath splits a slash-separated ref into its non-empty-leading segments. It is a
// tiny local helper (not filepath.Split, which peels only the last element) so the
// three-segment group ref parses without pulling path semantics that would treat a
// trailing slash specially.
func splitPath(ref string) []string {
	var out []string
	start := 0
	for i := 0; i < len(ref); i++ {
		if ref[i] == '/' {
			out = append(out, ref[start:i])
			start = i + 1
		}
	}
	out = append(out, ref[start:])
	return out
}
