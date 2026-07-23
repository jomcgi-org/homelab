package server

import (
	"context"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strconv"
	"sync"
	"testing"
	"time"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	nodev1 "github.com/jomcgi/homelab/projects/embervm/proto/embervm/node/v1"

	"github.com/jomcgi/homelab/projects/embervm/noded/config"
	"github.com/jomcgi/homelab/projects/embervm/noded/substrate"
)

// fakeServingNet is a servingNetwork that hands out the loopback address (127.0.0.1)
// so the real health-gate and probe HTTP paths reach the test's local httptest server,
// while recording every tap alloc / pin re-acquire / release so tests assert the IP
// lifecycle. Every allocation resolves to 127.0.0.1 (the address the health server
// listens on); the pin round-trip is still exercised because the server pins whatever
// ip the net returned and AllocateTapForIP records the re-acquire.
type fakeServingNet struct {
	mu             sync.Mutex
	allocs         int
	forIPCalls     []string // ips passed to AllocateTapForIP (pin re-acquire)
	released       []string // ips passed to ReleaseTap
	live           map[string]int
	failAllocForIP map[string]error
	// ensureDNATCalls records the tap IPs EnsureDNAT was asked to expose; failEnsureDNAT
	// (when true) makes EnsureDNAT fail so a test can drive the reap path.
	ensureDNATCalls []string
	failEnsureDNAT  bool
	// podIP/podPort, when podIP is set, make Endpoint project (podIP, podPort) instead
	// of the tap IP, so a test asserts the server publishes the projected endpoint while
	// the probe/pin still used the tap IP. Empty podIP keeps the tap-IP fallback (so the
	// existing tests that assert resp.ip == 127.0.0.1 are unchanged).
	podIP   string
	podPort uint32
	// availableTaps is what AvailableTaps reports (the tap-pressure freelist).
	// Defaults to a large value (never exhausted) so existing serving tests admit;
	// a pressure test sets it to 0 to exercise the `pressure:taps` rejection.
	availableTaps int
}

func newFakeServingNet() *fakeServingNet {
	return &fakeServingNet{live: map[string]int{}, availableTaps: 1 << 20}
}

func (f *fakeServingNet) AvailableTaps() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.availableTaps
}

func (f *fakeServingNet) EnsureNetwork(_ context.Context) error { return nil }

func (f *fakeServingNet) AllocateTap(_ context.Context) (string, net.IP, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.allocs++
	ip := net.ParseIP("127.0.0.1")
	f.live[ip.String()]++
	return "tap-serv", ip, nil
}

func (f *fakeServingNet) AllocateTapForIP(_ context.Context, ip net.IP) (string, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.forIPCalls = append(f.forIPCalls, ip.String())
	if err, ok := f.failAllocForIP[ip.String()]; ok {
		return "", err
	}
	f.live[ip.String()]++
	return "tap-serv", nil
}

func (f *fakeServingNet) ReleaseTap(_ context.Context, ip net.IP) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.released = append(f.released, ip.String())
	if f.live[ip.String()] > 0 {
		f.live[ip.String()]--
	}
}

func (f *fakeServingNet) EnsureDNAT(_ context.Context, ip net.IP, _ uint32) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.ensureDNATCalls = append(f.ensureDNATCalls, ip.String())
	if f.failEnsureDNAT {
		return status.Error(codes.Internal, "fake serving net: EnsureDNAT failed")
	}
	return nil
}

func (f *fakeServingNet) Endpoint(ip net.IP, guestPort uint32) (string, uint32) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.podIP == "" {
		return ip.String(), guestPort
	}
	return f.podIP, f.podPort
}

func (f *fakeServingNet) GatewayIP() net.IP { return net.ParseIP("172.31.0.1") }
func (f *fakeServingNet) PrefixLen() int    { return 24 }
func (f *fakeServingNet) CIDR() string      { return "172.31.0.0/24" }

func (f *fakeServingNet) dnatCalls() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]string(nil), f.ensureDNATCalls...)
}

func (f *fakeServingNet) releaseCount() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.released)
}

func (f *fakeServingNet) pinReacquires() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]string(nil), f.forIPCalls...)
}

// fakeServingDriver is a servingDriver that cold-boots (ClaimServing) and banks/
// relights serving VMs in memory, recording the pinned IP so a relight round-trips it.
type fakeServingDriver struct {
	mu                 sync.Mutex
	live               int
	claims             int
	banked             map[string]string // snapshotRef -> pinnedIP
	handlers           map[string]string // baseKey -> handler artifact path (written)
	handlerRuntimeRefs map[string]string // baseKey -> runtime image ref sidecar
	servingDir         string
	failClaim          error
	lastClaimNIC       substrate.NICSpec
	// lastHandlerDiskPath/lastHandlerZipBytes record the handler-disk args the last
	// ClaimServing carried, so a test can assert the serving cold boot attached the
	// artifact drive with the exact byte length (D-R3.11.2).
	lastHandlerDiskPath string
	lastHandlerZipBytes int64
}

func newFakeServingDriver(dir string) *fakeServingDriver {
	return &fakeServingDriver{
		banked:             map[string]string{},
		handlers:           map[string]string{},
		handlerRuntimeRefs: map[string]string{},
		servingDir:         filepath.Join(dir, "serving"),
	}
}

// writeFile writes s to path 0600, failing the test on error.
func writeFile(t *testing.T, path, s string) {
	t.Helper()
	if err := os.WriteFile(path, []byte(s), 0o600); err != nil {
		t.Fatalf("write %s: %v", path, err)
	}
}

func (f *fakeServingDriver) ClaimServing(_ context.Context, _ string, _ string, _ int, _ int, nic substrate.NICSpec, handlerDiskPath string, handlerZipBytes int64) (substrate.Handle, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.failClaim != nil {
		return substrate.Handle{}, f.failClaim
	}
	f.live++
	f.claims++
	f.lastClaimNIC = nic
	f.lastHandlerDiskPath = handlerDiskPath
	f.lastHandlerZipBytes = handlerZipBytes
	return substrate.Handle{ID: "serv-vm-" + strconv.Itoa(f.claims), ThreadID: "t-" + strconv.Itoa(f.claims), Node: "node-4"}, nil
}

// WriteServingHandlerArtifact records the handler artifact + runtime ref for a base key
// and returns a deterministic fake path + the byte length, mirroring the real driver.
func (f *fakeServingDriver) WriteServingHandlerArtifact(baseKey, runtimeImageRef string, zip []byte) (string, int64, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	path := filepath.Join(f.servingDir, "..", "bases", baseKey, "handler.zip")
	f.handlers[baseKey] = path
	f.handlerRuntimeRefs[baseKey] = runtimeImageRef
	return path, int64(len(zip)), nil
}

// ServingHandlerArtifactPath reports the recorded artifact path for a base key.
func (f *fakeServingDriver) ServingHandlerArtifactPath(baseKey string) (string, bool) {
	f.mu.Lock()
	defer f.mu.Unlock()
	p, ok := f.handlers[baseKey]
	return p, ok
}

// ScanServingHandlerArtifacts returns the recorded artifacts as a startup-rescan would.
func (f *fakeServingDriver) ScanServingHandlerArtifacts() []substrate.ServingHandlerArtifact {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := make([]substrate.ServingHandlerArtifact, 0, len(f.handlers))
	for k, p := range f.handlers {
		out = append(out, substrate.ServingHandlerArtifact{BaseKey: k, Path: p, RuntimeImageRef: f.handlerRuntimeRefs[k]})
	}
	return out
}

// SnapshotServing records the bank (with its pinned IP) but does NOT decrement live:
// like the real driver it leaves the VM paused for the caller (stopServingBank) to
// Release, and that Release (via reapServing) is what decrements live.
func (f *fakeServingDriver) SnapshotServing(_ context.Context, _ substrate.Handle, snapshotRef, pinnedIP string) (substrate.SnapshotRef, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.banked[snapshotRef] = pinnedIP
	return substrate.SnapshotRef{ID: snapshotRef, SizeBytes: 4096}, nil
}

func (f *fakeServingDriver) RestoreServing(_ context.Context, snapshotRef string) (substrate.Handle, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if _, ok := f.banked[snapshotRef]; !ok {
		return substrate.Handle{}, status.Errorf(codes.FailedPrecondition, "no such banked serving snapshot %q", snapshotRef)
	}
	f.live++
	return substrate.Handle{ID: "relit-" + snapshotRef, ThreadID: "t-relit", Node: "node-4"}, nil
}

func (f *fakeServingDriver) ServingPinnedIP(snapshotRef string) string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.banked[snapshotRef]
}

func (f *fakeServingDriver) RemoveServingBundle(snapshotRef string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	delete(f.banked, snapshotRef)
	return nil
}

func (f *fakeServingDriver) ServingDir() string { return f.servingDir }

func (f *fakeServingDriver) liveCount() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.live
}

// servingDriverLiveAdapter makes the fakeServingDriver ALSO satisfy the task vmDriver
// seam's LiveCount so the server's node cap sees serving VMs. Only the methods the
// server calls on s.driver in the serving path (LiveCount, Release, RemoveBundle) are
// meaningful; the rest are unused stubs.
type servingVMDriverAdapter struct {
	*fakeServingDriver
}

func (a servingVMDriverAdapter) Claim(_ context.Context, _ substrate.ClaimSpec) (substrate.Handle, error) {
	return substrate.Handle{}, status.Error(codes.Unimplemented, "unused")
}

// Release decrements the fake serving driver's live count, mirroring the real
// driver's Release (which removes the VM from its live map). reapServing -> s.reap ->
// s.driver.Release drives this, so a reaped serving VM stops counting against the cap.
func (a servingVMDriverAdapter) Release(_ context.Context, _ substrate.Handle) error {
	a.fakeServingDriver.mu.Lock()
	if a.fakeServingDriver.live > 0 {
		a.fakeServingDriver.live--
	}
	a.fakeServingDriver.mu.Unlock()
	return nil
}
func (a servingVMDriverAdapter) RemoveBundle(_ string) error         { return nil }
func (a servingVMDriverAdapter) VsockUDSPath(threadID string) string { return "/tmp/" + threadID }
func (a servingVMDriverAdapter) Stats(_ substrate.Handle) (substrate.GuestStats, error) {
	return substrate.GuestStats{}, nil
}
func (a servingVMDriverAdapter) LiveCount() int { return a.fakeServingDriver.liveCount() }

// healthServer stands up a loopback HTTP server that answers 200 on healthPath, so the
// serving health-gate and probe can reach a real endpoint. It returns the port.
func healthServer(t *testing.T, healthPath string) (*httptest.Server, uint32) {
	t.Helper()
	mux := http.NewServeMux()
	mux.HandleFunc(healthPath, func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(200) })
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	// httptest binds 127.0.0.1:PORT; extract PORT.
	_, portStr, err := net.SplitHostPort(srv.Listener.Addr().String())
	if err != nil {
		t.Fatalf("split health server addr: %v", err)
	}
	p, _ := strconv.Atoi(portStr)
	return srv, uint32(p)
}

// newServingTestServer wires a Server with serving support. The fake serving net hands
// out 127.0.0.1 so the server's real HTTP health-gate and probe reach the test's local
// health server. BootReadyTimeout/RestoreReadyTimeout are set short so a ready endpoint
// gates fast.
func newServingTestServer(t *testing.T) (*Server, *fakeServingNet, *fakeServingDriver) {
	t.Helper()
	dir := t.TempDir()
	fsd := newFakeServingDriver(dir)
	fsn := newFakeServingNet()
	s := New(Options{
		Config: config.Config{
			Arch: "amd64", Node: "node-4", MaxLiveVMs: 4, SnapshotRoot: dir,
			BootReadyTimeout:    2 * time.Second,
			RestoreReadyTimeout: 2 * time.Second,
			Images:              map[string]config.Image{"img-a": {RootfsPath: "/rootfs/a"}},
		},
		Driver:        servingVMDriverAdapter{fsd},
		ServingNet:    fsn,
		ServingDriver: fsd,
		Logger:        slog.New(slog.NewTextHandler(io.Discard, nil)),
	})
	s.memHeadroom = func() uint64 { return 0 }
	// Seed a built serving image "img-a" mapping to the runtime rootfs "img-a" and a
	// handler artifact, so a fresh cold boot resolves the serving-images inventory
	// (D-R3.11.2) exactly as a real BuildBase would have populated it.
	s.servingImage.add(servingImageEntry{
		baseKey:         "img-a",
		workload:        "wl-serve",
		handlerPath:     "/disks/bases/img-a/handler.zip",
		runtimeImageRef: "img-a",
		sizeBytes:       2048,
	})
	return s, fsn, fsd
}

const servingHealthPath = "/healthz"

// startFresh is a helper that cold-boots a fresh serving VM against a ready loopback
// health server on the given port.
func startFresh(t *testing.T, s *Server, port uint32) *nodev1.StartServingResponse {
	t.Helper()
	resp, err := s.StartServing(context.Background(), &nodev1.StartServingRequest{
		Source:     &nodev1.StartServingRequest_Fresh{Fresh: &nodev1.FreshSource{ServingImageRef: "img-a"}},
		Port:       port,
		HealthPath: servingHealthPath,
		Trace:      &nodev1.Trace{Workload: "wl-serve"},
	})
	if err != nil {
		t.Fatalf("StartServing(fresh): %v", err)
	}
	return resp
}

// TestStartServingFreshResolvesFromPushedRegistry proves the artifact-decoupling
// Phase 2 prod condition: with cfg.Images EMPTY (the env parse retired), a serving
// cold boot resolves its runtime rootfs from the control-plane-PUSHED registry
// (by the runtime image_ref) instead of the config table. Without the
// resolveImageByRef fix this fails "runtime image ... not provisioned".
func TestStartServingFreshResolvesFromPushedRegistry(t *testing.T) {
	_, port := healthServer(t, servingHealthPath)
	s, _, _ := newServingTestServer(t)

	// Prod condition: no config image table at all.
	s.cfg.Images = map[string]config.Image{}
	// The control plane pushed the runtime image identity keyed by image_ref.
	s.registry.sync([]workloadEntry{
		{Workload: "image:img-a", ImageRef: "img-a", RootfsRef: "/rootfs/a", HarnessInit: "/init"},
	})

	resp := startFresh(t, s, port)
	if resp.GetVmId() == "" {
		t.Fatal("StartServing(fresh) resolved no VM from the pushed registry")
	}
	if resp.GetIp() != "127.0.0.1" {
		t.Errorf("ip = %q want 127.0.0.1", resp.GetIp())
	}
}

// TestStartServingFreshRefusedWhileStale proves a stale registry refuses a FRESH
// serving cold boot (new-work placement) with FailedPrecondition/stale, while a
// live sync clears the refusal.
func TestStartServingFreshRefusedWhileStale(t *testing.T) {
	_, port := healthServer(t, servingHealthPath)
	s, _, _ := newServingTestServer(t)
	s.registry.mu.Lock()
	s.registry.stale = true
	s.registry.mu.Unlock()

	_, err := s.StartServing(context.Background(), &nodev1.StartServingRequest{
		Source:     &nodev1.StartServingRequest_Fresh{Fresh: &nodev1.FreshSource{ServingImageRef: "img-a"}},
		Port:       port,
		HealthPath: servingHealthPath,
		Trace:      &nodev1.Trace{Workload: "wl-serve"},
	})
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("FRESH StartServing while stale: err = %v, want FailedPrecondition", err)
	}
}

func TestStartServingFresh(t *testing.T) {
	_, port := healthServer(t, servingHealthPath)
	s, fsn, fsd := newServingTestServer(t)

	resp := startFresh(t, s, port)
	if resp.GetVmId() == "" {
		t.Error("StartServing returned no vm_id")
	}
	if resp.GetIp() != "127.0.0.1" {
		t.Errorf("ip = %q want 127.0.0.1", resp.GetIp())
	}
	if resp.GetPort() != port {
		t.Errorf("port = %d want %d", resp.GetPort(), port)
	}
	if fsd.claims != 1 {
		t.Errorf("ClaimServing calls = %d want 1", fsd.claims)
	}
	// The cold boot carried a NIC with the allocated IP and the gateway.
	if fsd.lastClaimNIC.IP != "127.0.0.1" || fsd.lastClaimNIC.GatewayIP != "172.31.0.1" {
		t.Errorf("NIC not configured correctly: %+v", fsd.lastClaimNIC)
	}
	// The cold boot also carried the handler-disk artifact (path + EXACT byte length),
	// resolved from the seeded serving-images inventory (D-R3.11.2), so the guest can
	// import the handler off the second drive and read only the payload.
	if fsd.lastHandlerDiskPath != "/disks/bases/img-a/handler.zip" {
		t.Errorf("handler disk path = %q want the seeded artifact path", fsd.lastHandlerDiskPath)
	}
	if fsd.lastHandlerZipBytes != 2048 {
		t.Errorf("handler zip bytes = %d want 2048 (exact length for the EOCD-padding defence)", fsd.lastHandlerZipBytes)
	}
	// The live serving VM is reported in NodeStatus.serving_vms and counted in live_vms,
	// but NOT in any primed pool.
	ns := s.nodeStatus()
	if len(ns.GetServingVms()) != 1 {
		t.Fatalf("serving_vms = %d want 1", len(ns.GetServingVms()))
	}
	if !ns.GetServingVms()[0].GetHealthy() {
		t.Error("a freshly started serving VM should report healthy")
	}
	if ns.GetServingSubnetCidr() != "172.31.0.0/24" {
		t.Errorf("serving_subnet_cidr = %q", ns.GetServingSubnetCidr())
	}
	if ns.GetLiveVms() != 1 {
		t.Errorf("live_vms = %d want 1", ns.GetLiveVms())
	}
	_ = fsn
}

// TestStartServingProjectsPodEndpoint proves the D-R3.11.4 projection: with a pod IP
// configured, the response and NodeStatus carry (podIP, DNAT port), EnsureDNAT is
// installed against the TAP IP, and the driver's readiness/pin still used the tap IP
// (the pod IP never leaks into the probe target or the snapshot pin).
func TestStartServingProjectsPodEndpoint(t *testing.T) {
	_, port := healthServer(t, servingHealthPath)
	s, fsn, fsd := newServingTestServer(t)
	// Enable the projection: publish 10.42.5.7:34567 for whatever tap IP was allocated.
	fsn.podIP = "10.42.5.7"
	fsn.podPort = 34567

	resp := startFresh(t, s, port)
	// The response is the projected endpoint, NOT the node-internal tap IP.
	if resp.GetIp() != "10.42.5.7" || resp.GetPort() != 34567 {
		t.Errorf("response endpoint = %s:%d want the projected 10.42.5.7:34567", resp.GetIp(), resp.GetPort())
	}
	// EnsureDNAT was installed against the TAP IP the health-gate/probe used (127.0.0.1),
	// not the pod IP.
	if calls := fsn.dnatCalls(); len(calls) != 1 || calls[0] != "127.0.0.1" {
		t.Errorf("EnsureDNAT calls = %v want exactly [127.0.0.1] (the tap IP)", calls)
	}
	// The cold boot's NIC used the tap IP; the pod IP never reached the guest/pin path.
	if fsd.lastClaimNIC.IP != "127.0.0.1" {
		t.Errorf("NIC IP = %q; the pod IP must not leak into the guest NIC", fsd.lastClaimNIC.IP)
	}
	// NodeStatus projects the same endpoint.
	ns := s.nodeStatus()
	if len(ns.GetServingVms()) != 1 {
		t.Fatalf("serving_vms = %d want 1", len(ns.GetServingVms()))
	}
	if got := ns.GetServingVms()[0]; got.GetIp() != "10.42.5.7" || got.GetPort() != 34567 {
		t.Errorf("NodeStatus serving endpoint = %s:%d want 10.42.5.7:34567", got.GetIp(), got.GetPort())
	}
}

// TestStartServingDNATFailureReaps proves a DNAT install failure AFTER readiness reaps
// the VM and releases the tap (no half-published endpoint), returning FailedPrecondition.
func TestStartServingDNATFailureReaps(t *testing.T) {
	_, port := healthServer(t, servingHealthPath)
	s, fsn, fsd := newServingTestServer(t)
	fsn.podIP = "10.42.5.7"
	fsn.podPort = 34567
	fsn.failEnsureDNAT = true

	_, err := s.StartServing(context.Background(), &nodev1.StartServingRequest{
		Source:     &nodev1.StartServingRequest_Fresh{Fresh: &nodev1.FreshSource{ServingImageRef: "img-a"}},
		Port:       port,
		HealthPath: servingHealthPath,
		Trace:      &nodev1.Trace{Workload: "wl-serve"},
	})
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("DNAT failure: got %v want FailedPrecondition", err)
	}
	if fsn.releaseCount() != 1 {
		t.Errorf("tap not released on DNAT failure: releases = %d", fsn.releaseCount())
	}
	if fsd.liveCount() != 0 {
		t.Errorf("VM not reaped on DNAT failure: live = %d", fsd.liveCount())
	}
	if len(s.nodeStatus().GetServingVms()) != 0 {
		t.Error("a DNAT-failed start must leave no serving VM reported")
	}
}

func TestStartServingFreshUnknownImage(t *testing.T) {
	_, port := healthServer(t, servingHealthPath)
	s, _, _ := newServingTestServer(t)
	_, err := s.StartServing(context.Background(), &nodev1.StartServingRequest{
		Source:     &nodev1.StartServingRequest_Fresh{Fresh: &nodev1.FreshSource{ServingImageRef: "nope"}},
		Port:       port,
		HealthPath: servingHealthPath,
	})
	if status.Code(err) != codes.FailedPrecondition {
		t.Errorf("unknown image: got %v want FailedPrecondition", err)
	}
}

func TestStartServingReadinessFailureReapsAndReleases(t *testing.T) {
	// Point the health-gate at a port with NO server: readiness times out, the VM is
	// reaped and the tap released, and no serving VM is reported.
	s, fsn, fsd := newServingTestServer(t)
	s.cfg.BootReadyTimeout = 300 * time.Millisecond
	_, err := s.StartServing(context.Background(), &nodev1.StartServingRequest{
		Source:     &nodev1.StartServingRequest_Fresh{Fresh: &nodev1.FreshSource{ServingImageRef: "img-a"}},
		Port:       1, // nothing listens
		HealthPath: servingHealthPath,
	})
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("readiness failure: got %v want FailedPrecondition", err)
	}
	if fsn.releaseCount() != 1 {
		t.Errorf("tap not released on readiness failure: releases = %d", fsn.releaseCount())
	}
	if fsd.liveCount() != 0 {
		t.Errorf("VM not reaped on readiness failure: live = %d", fsd.liveCount())
	}
	if len(s.nodeStatus().GetServingVms()) != 0 {
		t.Error("a failed start must leave no serving VM reported")
	}
}

// TestServingBankRelightRoundTrip banks a fresh serving VM and relights it, asserting
// the pinned IP is re-acquired on relight (D-R3.4.1) and the banked snapshot inventory
// is maintained.
func TestServingBankRelightRoundTrip(t *testing.T) {
	_, port := healthServer(t, servingHealthPath)
	s, fsn, fsd := newServingTestServer(t)

	started := startFresh(t, s, port)

	// Bank it.
	bankResp, err := s.StopServing(context.Background(), &nodev1.StopServingRequest{
		VmId:  started.GetVmId(),
		Mode:  nodev1.StopServingMode_STOP_SERVING_MODE_BANK,
		Trace: &nodev1.Trace{Workload: "wl-serve"},
	})
	if err != nil {
		t.Fatalf("StopServing(bank): %v", err)
	}
	if bankResp.GetSnapshotRef() == "" || bankResp.GetSizeBytes() == 0 {
		t.Errorf("bank response missing ref/size: %+v", bankResp)
	}
	// The banked snapshot is in inventory; the live VM is gone; the tap was released.
	ns := s.nodeStatus()
	if len(ns.GetServingVms()) != 0 {
		t.Error("banked VM should no longer be live")
	}
	if len(ns.GetServingSnapshots()) != 1 {
		t.Fatalf("serving_snapshots = %d want 1", len(ns.GetServingSnapshots()))
	}
	if fsn.releaseCount() != 1 {
		t.Errorf("tap not released on bank: releases = %d", fsn.releaseCount())
	}
	// The driver recorded the pinned IP the VM was banked with.
	if got := fsd.ServingPinnedIP(bankResp.GetSnapshotRef()); got != "127.0.0.1" {
		t.Errorf("pinned ip = %q want 127.0.0.1", got)
	}

	// Relight it: the pinned IP must be re-acquired (D-R3.4.1).
	relit, err := s.StartServing(context.Background(), &nodev1.StartServingRequest{
		Source:     &nodev1.StartServingRequest_Relight{Relight: &nodev1.RelightSource{SnapshotRef: bankResp.GetSnapshotRef()}},
		Port:       port,
		HealthPath: servingHealthPath,
		Trace:      &nodev1.Trace{Workload: "wl-serve"},
	})
	if err != nil {
		t.Fatalf("StartServing(relight): %v", err)
	}
	if relit.GetIp() != "127.0.0.1" {
		t.Errorf("relit ip = %q want the pinned 127.0.0.1", relit.GetIp())
	}
	pins := fsn.pinReacquires()
	if len(pins) != 1 || pins[0] != "127.0.0.1" {
		t.Errorf("relight did not re-acquire the pinned IP: AllocateTapForIP calls = %v", pins)
	}
	if len(s.nodeStatus().GetServingVms()) != 1 {
		t.Error("relit serving VM should be live")
	}
}

// TestEvictServingSnapshotInUseGuard mirrors the session in-use guard: evicting a
// serving ref a LIVE relit VM depends on is refused FailedPrecondition; evicting a
// banked-but-not-live ref succeeds and removes the bundle. It proves the guard is
// ENFORCED (the pre-fix code deleted unconditionally).
func TestEvictServingSnapshotInUseGuard(t *testing.T) {
	_, port := healthServer(t, servingHealthPath)
	s, _, fsd := newServingTestServer(t)

	// Bank a fresh VM, then relight it so a LIVE VM depends on the ref.
	started := startFresh(t, s, port)
	bankResp, err := s.StopServing(context.Background(), &nodev1.StopServingRequest{
		VmId: started.GetVmId(), Mode: nodev1.StopServingMode_STOP_SERVING_MODE_BANK,
		Trace: &nodev1.Trace{Workload: "wl-serve"},
	})
	if err != nil {
		t.Fatalf("bank: %v", err)
	}
	ref := bankResp.GetSnapshotRef()
	if _, err := s.StartServing(context.Background(), &nodev1.StartServingRequest{
		Source:     &nodev1.StartServingRequest_Relight{Relight: &nodev1.RelightSource{SnapshotRef: ref}},
		Port:       port,
		HealthPath: servingHealthPath,
		Trace:      &nodev1.Trace{Workload: "wl-serve"},
	}); err != nil {
		t.Fatalf("relight: %v", err)
	}

	// Evict while a live VM was relit from ref: refused.
	_, err = s.EvictSnapshot(context.Background(), &nodev1.EvictSnapshotRequest{SnapshotRef: ref})
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("evict of in-use serving ref: got %v want FailedPrecondition", err)
	}
	// The bundle is still on disk (the fake still holds it).
	if _, ok := fsd.banked[ref]; !ok {
		t.Error("refused evict must not have removed the bundle")
	}

	// Bank the live VM again (frees the dependency: the OLD ref now has no live VM).
	// Find the live vm_id to bank it.
	liveVMs := s.nodeStatus().GetServingVms()
	if len(liveVMs) != 1 {
		t.Fatalf("expected 1 live serving VM, got %d", len(liveVMs))
	}
	if _, err := s.StopServing(context.Background(), &nodev1.StopServingRequest{
		VmId: liveVMs[0].GetVmId(), Mode: nodev1.StopServingMode_STOP_SERVING_MODE_DESTROY,
	}); err != nil {
		t.Fatalf("destroy live relit VM: %v", err)
	}
	// Now no live VM depends on ref: evict succeeds and removes the bundle.
	if _, err := s.EvictSnapshot(context.Background(), &nodev1.EvictSnapshotRequest{SnapshotRef: ref}); err != nil {
		t.Fatalf("evict of non-live serving ref should succeed, got %v", err)
	}
	if _, ok := fsd.banked[ref]; ok {
		t.Error("evict of a non-live ref should have removed the bundle")
	}
	if len(s.nodeStatus().GetServingSnapshots()) != 0 {
		t.Error("evicted serving snapshot should be gone from inventory")
	}
}

func TestStopServingBankConcurrentRefused(t *testing.T) {
	_, port := healthServer(t, servingHealthPath)
	s, _, _ := newServingTestServer(t)
	started := startFresh(t, s, port)

	// Manually take the bank in-flight guard, then a second bank must be refused.
	e, ok := s.servingVMs.beginBank(started.GetVmId())
	if !ok {
		t.Fatal("beginBank should succeed the first time")
	}
	_, err := s.StopServing(context.Background(), &nodev1.StopServingRequest{
		VmId: started.GetVmId(),
		Mode: nodev1.StopServingMode_STOP_SERVING_MODE_BANK,
	})
	if status.Code(err) != codes.FailedPrecondition {
		t.Errorf("concurrent bank: got %v want FailedPrecondition", err)
	}
	_ = e
}

func TestStopServingDestroy(t *testing.T) {
	_, port := healthServer(t, servingHealthPath)
	s, fsn, fsd := newServingTestServer(t)
	started := startFresh(t, s, port)

	_, err := s.StopServing(context.Background(), &nodev1.StopServingRequest{
		VmId: started.GetVmId(),
		Mode: nodev1.StopServingMode_STOP_SERVING_MODE_DESTROY,
	})
	if err != nil {
		t.Fatalf("StopServing(destroy): %v", err)
	}
	if fsd.liveCount() != 0 {
		t.Errorf("destroy left VM live: %d", fsd.liveCount())
	}
	if fsn.releaseCount() != 1 {
		t.Errorf("destroy did not release tap: %d", fsn.releaseCount())
	}
	if len(fsd.banked) != 0 {
		t.Error("destroy must not bank a snapshot")
	}
	// Idempotent: destroying an unknown vm_id is OK.
	if _, err := s.StopServing(context.Background(), &nodev1.StopServingRequest{
		VmId: "ghost", Mode: nodev1.StopServingMode_STOP_SERVING_MODE_DESTROY,
	}); err != nil {
		t.Errorf("destroy of unknown vm should be idempotent OK, got %v", err)
	}
}

func TestReconcileServingFromDisk(t *testing.T) {
	dir := t.TempDir()
	fsd := newFakeServingDriver(dir)
	// Seed a banked bundle on disk: serving/<ref>/{snapfile,ip}.
	ref := "serv-banked-1"
	bundle := filepath.Join(dir, "serving", ref)
	if err := os.MkdirAll(bundle, 0o700); err != nil {
		t.Fatal(err)
	}
	writeFile(t, filepath.Join(bundle, "snapfile"), "snap")
	writeFile(t, filepath.Join(bundle, "memfile"), "mem")
	writeFile(t, filepath.Join(bundle, "ip"), "172.31.0.9")
	fsd.banked[ref] = "172.31.0.9" // ServingPinnedIP reads from the fake, mirroring disk

	s := New(Options{
		Config:        config.Config{Arch: "amd64", Node: "node-4", SnapshotRoot: dir},
		Driver:        servingVMDriverAdapter{fsd},
		ServingNet:    newFakeServingNet(),
		ServingDriver: fsd,
		Logger:        slog.New(slog.NewTextHandler(io.Discard, nil)),
	})
	s.ReconcileServingFromDisk()

	ns := s.nodeStatus()
	if len(ns.GetServingSnapshots()) != 1 {
		t.Fatalf("rescan found %d serving snapshots want 1", len(ns.GetServingSnapshots()))
	}
	if ns.GetServingSnapshots()[0].GetSnapshotRef() != ref {
		t.Errorf("rescanned ref = %q want %q", ns.GetServingSnapshots()[0].GetSnapshotRef(), ref)
	}
	// The pinned IP was recovered so a post-restart relight re-acquires the same address.
	got, ok := s.servingSnap.get(ref)
	if !ok || got.ip != "172.31.0.9" {
		t.Errorf("rescan did not recover the pinned IP: %+v", got)
	}
}
