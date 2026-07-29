package server

import (
	"context"
	"fmt"
	"net"
	"net/http"
	"time"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	nodev1 "github.com/jomcgi/homelab/projects/embervm/proto/embervm/node/v1"

	"github.com/jomcgi/homelab/projects/embervm/noded/serving"
	"github.com/jomcgi/homelab/projects/embervm/noded/substrate"
)

// StartServing brings up a serving VM (fresh cold boot with a NIC, or relight from a
// banked serving snapshot), allocates a tap IP, health-gates the guest HTTP server
// over the tap, starts the per-VM health probe, and returns {vm_id, ip, port}. A
// readiness failure DESTROYS the VM and its tap and returns the error: there is no
// half-alive endpoint a caller could publish. FAILED_PRECONDITION on an unknown or
// unrestorable ref; RESOURCE_EXHAUSTED at the node live-VM cap. The daemon is off the
// request hit path after this: requests reach the guest directly over ip:port.
func (s *Server) StartServing(ctx context.Context, req *nodev1.StartServingRequest) (*nodev1.StartServingResponse, error) {
	return s.startServing(ctx, req, nodev1.InstanceOrigin_INSTANCE_ORIGIN_CONTROL_PLANE)
}

func (s *Server) startServing(ctx context.Context, req *nodev1.StartServingRequest, origin nodev1.InstanceOrigin) (*nodev1.StartServingResponse, error) {
	if s.servingNet == nil || s.servingDriver == nil {
		return nil, status.Error(codes.Unimplemented, "noded: serving not configured")
	}
	if s.isDraining() {
		return nil, status.Error(codes.Unavailable, "noded: draining")
	}
	if s.slotsExhausted() {
		return nil, status.Errorf(codes.ResourceExhausted, "noded: node live-VM cap %d reached", s.SlotCeiling())
	}
	// Cheap rejection under real memory/tap pressure (ADR embervm/014 decision 3),
	// BEFORE the tap allocation and cold boot below. Serving is tap-bearing, so
	// both mem headroom and the tap freelist are checked; the workload's mem need
	// comes from the request's ResourceSpec.
	if err := s.admitOrReject(uint64(req.GetResources().GetMemMib()), classTapBearing); err != nil {
		return nil, err
	}
	port := req.GetPort()
	if port == 0 {
		return nil, status.Error(codes.InvalidArgument, "noded: port required")
	}
	healthPath := req.GetHealthPath()
	if healthPath == "" {
		healthPath = defaultReadyPath
	}
	workload := req.GetTrace().GetWorkload()

	// Exactly one of fresh|relight is set (a oneof). Dispatch on which is present;
	// GetFresh()/GetRelight() return nil for the unset arm.
	switch {
	case req.GetFresh() != nil:
		return s.startServingFresh(ctx, req, req.GetFresh().GetServingImageRef(), workload, port, healthPath, origin)
	case req.GetRelight() != nil:
		return s.startServingRelight(ctx, req, req.GetRelight().GetSnapshotRef(), workload, port, healthPath, origin)
	default:
		return nil, status.Error(codes.InvalidArgument, "noded: exactly one of fresh|relight source required")
	}
}

// startServingFresh cold-boots a serving VM from the workload's cold-boot handler
// artifact WITH a freshly allocated tap NIC and its static IP baked into boot-args,
// then health-gates over the tap. servingImageRef names a SERVING IMAGE (a built
// cold-boot handler artifact in the serving-images inventory), NOT a base snapshot to
// resume (D-R3.4.2, D-R3.11.2): the runtime rootfs is drive 1 and the handler artifact
// is drive 2, from which the guest imports the handler before serving.
func (s *Server) startServingFresh(ctx context.Context, req *nodev1.StartServingRequest, servingImageRef, workload string, port uint32, healthPath string, origin nodev1.InstanceOrigin) (*nodev1.StartServingResponse, error) {
	// A FRESH serving cold boot is new-work placement. A stale registry (boot
	// cache, no live sync) refuses it; RELIGHT of an existing serving snapshot
	// stays allowed so existing warmth is served.
	if err := s.refuseIfStale("StartServing FRESH"); err != nil {
		return nil, err
	}
	if servingImageRef == "" {
		return nil, status.Error(codes.InvalidArgument, "noded: fresh.serving_image_ref required")
	}
	// Resolve the serving image against the serving-images inventory (NOT the static
	// runtime-rootfs image table): the ref is a built base key, and the inventory maps
	// it to its handler artifact + the runtime image whose rootfs cold-boots. Looking a
	// base key up in s.cfg.Images was the original "not provisioned" bug.
	simg, ok := s.servingImage.get(servingImageRef)
	if !ok {
		return nil, status.Errorf(codes.FailedPrecondition, "noded: serving image %q not provisioned on this node (no cold-boot handler artifact built)", servingImageRef)
	}
	img, ok := s.resolveImageByRef(simg.runtimeImageRef)
	if !ok {
		return nil, status.Errorf(codes.FailedPrecondition, "noded: runtime image %q for serving image %q not provisioned on this node", simg.runtimeImageRef, servingImageRef)
	}
	harnessInit := img.HarnessInit
	if harnessInit == "" {
		harnessInit = s.cfg.HarnessInit
	}
	res := req.GetResources()

	tap, ip, err := s.servingNet.AllocateTap(ctx)
	if err != nil {
		return nil, status.Errorf(codes.ResourceExhausted, "noded: allocate serving tap: %v", err)
	}
	nic := substrate.NICSpec{
		HostDevName: tap,
		IP:          ip.String(),
		GatewayIP:   s.servingNet.GatewayIP().String(),
		PrefixLen:   s.servingNet.PrefixLen(),
		IfaceName:   "eth0",
		// Single-source the guest TCP serving port from the same `port` the health
		// probe uses (finishServingStart below), so the shim binds exactly what the
		// daemon probes and publishes: GET http://ip:port{healthPath} (D-R3.11.1).
		ServingPort: port,
	}
	// Attach the handler artifact as the second read-only drive (D-R3.11.2); the guest
	// imports the handler off it before serving. The exact byte length lets the guest
	// read only the payload, not the block device's sector padding.
	h, err := s.servingDriver.ClaimServing(ctx, img.RootfsPath, harnessInit, int(res.GetVcpus()), int(res.GetMemMib()), nic, simg.handlerPath, simg.sizeBytes)
	if err != nil {
		s.servingNet.ReleaseTap(ctx, ip)
		return nil, status.Errorf(codes.FailedPrecondition, "noded: cold-boot serving vm: %v", err)
	}
	// A fresh cold boot has NO source snapshot ref (empty), so it never guards a bundle.
	return s.finishServingStart(ctx, h, workload, "", ip, port, healthPath, s.cfg.BootReadyTimeout, origin)
}

// startServingRelight resumes a banked serving snapshot (which already carries its NIC
// because the fresh path cold-booted one before the bank) and RE-PINS the same host IP
// the snapshot recorded (D-R3.4.1), so the resumed guest's baked eth0 IP still routes.
func (s *Server) startServingRelight(ctx context.Context, req *nodev1.StartServingRequest, ref, workload string, port uint32, healthPath string, origin nodev1.InstanceOrigin) (*nodev1.StartServingResponse, error) {
	if ref == "" {
		return nil, status.Error(codes.InvalidArgument, "noded: relight.snapshot_ref required")
	}
	if _, ok := s.servingSnap.get(ref); !ok {
		return nil, status.Errorf(codes.FailedPrecondition, "noded: unknown serving snapshot_ref %q", ref)
	}
	// Re-acquire the pinned IP the snapshot was banked with (D-R3.4.1). An absent pin
	// (a snapshot banked before pinning, or a cold node) falls back to a fresh IP.
	pinned := s.servingDriver.ServingPinnedIP(ref)
	var ip net.IP
	var err error
	if pinned != "" {
		ip = net.ParseIP(pinned)
		if ip == nil {
			return nil, status.Errorf(codes.Internal, "noded: serving snapshot %q has a malformed pinned ip %q", ref, pinned)
		}
		if _, aerr := s.servingNet.AllocateTapForIP(ctx, ip); aerr != nil {
			return nil, status.Errorf(codes.FailedPrecondition, "noded: re-acquire pinned serving ip %s: %v", pinned, aerr)
		}
	} else {
		_, ip, err = s.servingNet.AllocateTap(ctx)
		if err != nil {
			return nil, status.Errorf(codes.ResourceExhausted, "noded: allocate serving tap: %v", err)
		}
	}

	h, err := s.servingDriver.RestoreServing(ctx, ref)
	if err != nil {
		s.servingNet.ReleaseTap(ctx, ip)
		// The snapshot is left on disk (never deleted on a failed restore).
		return nil, status.Errorf(codes.FailedPrecondition, "noded: relight serving snapshot %q: %v", ref, err)
	}
	// The relit VM depends on ref: record it so EvictSnapshot refuses to delete the
	// bundle out from under this live VM (D-R3.4.1 relight-from state).
	return s.finishServingStart(ctx, h, workload, ref, ip, port, healthPath, s.cfg.RestoreReadyTimeout, origin)
}

// finishServingStart is the shared tail of both source modes: health-gate the guest
// over the tap, and on success register the live serving VM and start its health
// probe. On a readiness failure it reaps the VM and releases the tap (no half-alive
// endpoint), returning FAILED_PRECONDITION.
func (s *Server) finishServingStart(ctx context.Context, h substrate.Handle, workload, sourceRef string, ip net.IP, port uint32, healthPath string, readyBudget time.Duration, origin nodev1.InstanceOrigin) (*nodev1.StartServingResponse, error) {
	if err := s.waitServingReady(ctx, ip, port, healthPath, readyBudget); err != nil {
		s.reapServing(h, ip)
		return nil, status.Errorf(codes.FailedPrecondition, "noded: serving guest not ready over tap: %v", err)
	}
	// The guest is ready on its tap; expose it as a routable endpoint via noded's pod IP
	// + a per-VM prerouting DNAT rule (D-R3.11.4). Readiness stayed on the tap IP, so a
	// broken DNAT does not fail readiness; installing it here (before publish) means a
	// DNAT failure reaps rather than publishing an unreachable endpoint. Reap's
	// ReleaseTap folds in RemoveDNAT, cleaning any partial rule.
	if err := s.servingNet.EnsureDNAT(ctx, ip, port); err != nil {
		s.reapServing(h, ip)
		return nil, status.Errorf(codes.FailedPrecondition, "noded: install serving DNAT for %s: %v", ip, err)
	}
	probe := serving.StartProbe(s.newProber(), ip, port, healthPath)
	s.servingVMs.add(&servingEntry{
		vmID:        h.ID,
		workload:    workload,
		handle:      h,
		ip:          ip,
		port:        port,
		tap:         serving.TapNameForIP(ip),
		probe:       probe,
		snapshotRef: sourceRef,
		origin:      origin,
	})
	s.signalChange()
	// Report the projected endpoint (pod IP + DNAT port), NOT the node-internal tap IP.
	endpointIP, endpointPort := s.servingNet.Endpoint(ip, port)
	return &nodev1.StartServingResponse{VmId: h.ID, Ip: endpointIP, Port: endpointPort}, nil
}

// waitServingReady polls GET http://ip:port{healthPath} over the tap until it returns
// 2xx or the budget expires. Unlike Prime/Relight (which health-gate over vsock), a
// serving VM is reached at its real L3 endpoint, so readiness is probed the same way
// the health loop will probe it. It retries on connection refused (the guest HTTP
// server races kernel init) with a short backoff.
func (s *Server) waitServingReady(ctx context.Context, ip net.IP, port uint32, healthPath string, budget time.Duration) error {
	deadline := time.Now().Add(budget)
	url := fmt.Sprintf("http://%s:%d%s", ip.String(), port, normalizePath(healthPath))
	client := &http.Client{Timeout: 2 * time.Second}
	var lastErr error
	for time.Now().Before(deadline) {
		select {
		case <-ctx.Done():
			return ctx.Err() // nosemgrep: no-bare-error-return
		default:
		}
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
		if err != nil {
			return err
		}
		resp, err := client.Do(req)
		if err != nil {
			lastErr = err
			time.Sleep(150 * time.Millisecond)
			continue
		}
		_ = resp.Body.Close()
		if resp.StatusCode >= 200 && resp.StatusCode < 300 {
			return nil
		}
		lastErr = fmt.Errorf("status %d", resp.StatusCode)
		time.Sleep(150 * time.Millisecond)
	}
	if lastErr == nil {
		lastErr = fmt.Errorf("timed out after %s", budget)
	}
	// lastErr is always a freshly-constructed fmt.Errorf (a status code, a dial
	// error, or the timeout above), not a bare pass-through; the caller wraps it
	// with the serving-guest context.
	return lastErr // nosemgrep: no-bare-error-return
}

// StopServing tears down a live serving VM. BANK pauses it, writes a serving snapshot
// under serving/ (with the pinned IP), destroys the VM, and returns {snapshot_ref,
// size_bytes}; it refuses FAILED_PRECONDITION while a bank of the same vm_id is already
// in flight. DESTROY tears the VM down with no snapshot. Either mode releases the tap.
func (s *Server) StopServing(ctx context.Context, req *nodev1.StopServingRequest) (*nodev1.StopServingResponse, error) {
	if s.servingNet == nil || s.servingDriver == nil {
		return nil, status.Error(codes.Unimplemented, "noded: serving not configured")
	}
	vmID := req.GetVmId()
	switch req.GetMode() {
	case nodev1.StopServingMode_STOP_SERVING_MODE_BANK:
		return s.stopServingBank(ctx, req, vmID)
	case nodev1.StopServingMode_STOP_SERVING_MODE_DESTROY:
		return s.stopServingDestroy(vmID)
	default:
		return nil, status.Error(codes.InvalidArgument, "noded: StopServing mode must be BANK or DESTROY")
	}
}

// stopServingBank snapshots a live serving VM to a banked bundle (pinning its IP),
// destroys it, releases its tap, and records the banked snapshot.
func (s *Server) stopServingBank(ctx context.Context, req *nodev1.StopServingRequest, vmID string) (*nodev1.StopServingResponse, error) {
	e, ok := s.servingVMs.beginBank(vmID)
	if !ok {
		return nil, status.Errorf(codes.FailedPrecondition, "noded: serving vm %q not bankable (unknown or a bank is already in flight)", vmID)
	}
	// Stop the health probe before banking: the VM is about to pause then die.
	e.probe.Stop()
	snapshotRef := newID("serv")
	ref, err := s.servingDriver.SnapshotServing(ctx, e.handle, snapshotRef, e.ip.String())
	if err != nil {
		// A bank is destructive: SnapshotServing tore the VM down on failure, so drop
		// the now-dead registry entry and release its tap rather than misreport capacity.
		s.servingVMs.remove(vmID)
		s.servingNet.ReleaseTap(ctx, e.ip)
		return nil, status.Errorf(codes.FailedPrecondition, "noded: bank serving vm %q: %v", vmID, err)
	}
	if removed := s.servingVMs.remove(vmID); removed != nil {
		s.reapServing(removed.handle, removed.ip)
	}
	s.servingSnap.add(servingSnapshotEntry{
		snapshotRef:     ref.ID,
		workload:        req.GetTrace().GetWorkload(),
		ip:              e.ip.String(),
		sizeBytes:       ref.SizeBytes,
		createdAtUnixMs: time.Now().UnixMilli(),
	})
	// Async off-node write-back (R6): enqueue the banked serving bundle's export
	// fire-and-forget (never blocking this bank path).
	s.enqueueExport(&nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_SERVING, Workload: req.GetTrace().GetWorkload(), Ref: ref.ID})
	s.signalChange()
	return &nodev1.StopServingResponse{SnapshotRef: ref.ID, SizeBytes: uint64(ref.SizeBytes)}, nil
}

// stopServingDestroy tears a serving VM down with no snapshot and releases its tap.
// Idempotent: an unknown vm_id returns confirmed (the desired end-state already
// holds). teardown_confirmed is true only when the reap fully completed; a reap
// failure returns an error, not a false confirm (ADR embervm/014 decision 5).
func (s *Server) stopServingDestroy(vmID string) (*nodev1.StopServingResponse, error) {
	if removed := s.servingVMs.remove(vmID); removed != nil {
		removed.probe.Stop()
		if err := s.reapServing(removed.handle, removed.ip); err != nil {
			return nil, status.Errorf(codes.Internal, "noded: reap serving vm %q: %v", vmID, err)
		}
		s.signalChange()
	}
	return &nodev1.StopServingResponse{TeardownConfirmed: true}, nil
}

// reapServing tears a serving VM down (release the FC process + bundle) and releases
// its tap + IP. Best-effort, mirroring reap for task/session VMs plus the tap teardown.
func (s *Server) reapServing(h substrate.Handle, ip net.IP) error {
	err := s.reap(h, func() {})
	if s.servingNet != nil {
		s.servingNet.ReleaseTap(context.Background(), ip)
	}
	return err
}

// normalizePath ensures a health path has a leading slash.
func normalizePath(p string) string {
	if p == "" {
		return "/"
	}
	if p[0] != '/' {
		return "/" + p
	}
	return p
}
