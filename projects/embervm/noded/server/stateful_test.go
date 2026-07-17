package server

import (
	"context"
	"io"
	"log/slog"
	"net"
	"os"
	"strconv"
	"sync"
	"testing"
	"time"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	nodev1 "github.com/jomcgi/homelab/projects/embervm/proto/embervm/node/v1"

	"github.com/jomcgi/homelab/projects/embervm/noded/config"
	"github.com/jomcgi/homelab/projects/embervm/noded/substrate"
	"github.com/jomcgi/homelab/projects/embervm/noded/volume"
)

// fakeStatefulDriver is a statefulDriver that cold-boots (ClaimStateful) and
// banks/relights stateful VMs in memory, recording the volume path and the
// generation each bank was stamped with, mirroring fakeServingDriver's shape.
type fakeStatefulDriver struct {
	mu     sync.Mutex
	live   int
	claims int
	// banked maps snapshotRef -> the generation it was stamped with.
	banked       map[string]uint64
	statefulDir  string
	failClaim    error
	lastVolPath  string
	lastVolMount string
	claimCount   int
	lastMmdsEnv  map[string]string
}

func newFakeStatefulDriver(dir string) *fakeStatefulDriver {
	return &fakeStatefulDriver{
		banked:      map[string]uint64{},
		statefulDir: dir + "/stateful",
	}
}

func (f *fakeStatefulDriver) ClaimStateful(_ context.Context, _ string, _ string, _ int, _ int, _ substrate.NICSpec, _ string, _ int64, volumeDiskPath, volumeMount string, mmdsEnv map[string]string) (substrate.Handle, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.failClaim != nil {
		return substrate.Handle{}, f.failClaim
	}
	f.live++
	f.claims++
	f.claimCount++
	f.lastVolPath = volumeDiskPath
	f.lastVolMount = volumeMount
	f.lastMmdsEnv = mmdsEnv
	return substrate.Handle{ID: "state-vm-" + strconv.Itoa(f.claims), ThreadID: "t-" + strconv.Itoa(f.claims), Node: "node-4"}, nil
}

func (f *fakeStatefulDriver) SnapshotStateful(_ context.Context, _ substrate.Handle, snapshotRef string, generation uint64) (substrate.SnapshotRef, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.banked[snapshotRef] = generation
	return substrate.SnapshotRef{ID: snapshotRef, SizeBytes: 4096}, nil
}

func (f *fakeStatefulDriver) RestoreStateful(_ context.Context, snapshotRef, _ string) (substrate.Handle, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if _, ok := f.banked[snapshotRef]; !ok {
		return substrate.Handle{}, status.Errorf(codes.FailedPrecondition, "no such banked stateful snapshot %q", snapshotRef)
	}
	f.live++
	return substrate.Handle{ID: "relit-" + snapshotRef, ThreadID: "t-relit", Node: "node-4"}, nil
}

func (f *fakeStatefulDriver) RemoveStatefulBundle(snapshotRef string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	delete(f.banked, snapshotRef)
	return nil
}

func (f *fakeStatefulDriver) ScanStatefulBundles() []substrate.StatefulBundleInfo {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := make([]substrate.StatefulBundleInfo, 0, len(f.banked))
	for ref, gen := range f.banked {
		out = append(out, substrate.StatefulBundleInfo{SnapshotRef: ref, Generation: gen, SizeBytes: 4096})
	}
	return out
}

func (f *fakeStatefulDriver) StatefulDir() string { return f.statefulDir }

func (f *fakeStatefulDriver) liveCount() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.live
}

// statefulVMDriverAdapter makes fakeStatefulDriver satisfy the task vmDriver
// seam (LiveCount/Release/RemoveBundle) so the server's node cap and reap path
// see stateful VMs, mirroring servingVMDriverAdapter.
type statefulVMDriverAdapter struct {
	*fakeStatefulDriver
}

func (a statefulVMDriverAdapter) Claim(_ context.Context, _ substrate.ClaimSpec) (substrate.Handle, error) {
	return substrate.Handle{}, status.Error(codes.Unimplemented, "unused")
}

func (a statefulVMDriverAdapter) Release(_ context.Context, _ substrate.Handle) error {
	a.fakeStatefulDriver.mu.Lock()
	if a.fakeStatefulDriver.live > 0 {
		a.fakeStatefulDriver.live--
	}
	a.fakeStatefulDriver.mu.Unlock()
	return nil
}
func (a statefulVMDriverAdapter) RemoveBundle(_ string) error         { return nil }
func (a statefulVMDriverAdapter) VsockUDSPath(threadID string) string { return "/tmp/" + threadID }
func (a statefulVMDriverAdapter) Stats(_ substrate.Handle) (substrate.GuestStats, error) {
	return substrate.GuestStats{}, nil
}
func (a statefulVMDriverAdapter) LiveCount() int { return a.fakeStatefulDriver.liveCount() }

// tcpHealthServer stands up a loopback TCP listener that accepts and
// immediately closes connections, so the stateful health-gate and probe
// (TCP CONNECT only, no application protocol) can reach a real endpoint. It
// returns the port and a stop func.
func tcpHealthServer(t *testing.T) uint32 {
	t.Helper()
	lis, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	t.Cleanup(func() { _ = lis.Close() })
	go func() {
		for {
			conn, err := lis.Accept()
			if err != nil {
				return
			}
			_ = conn.Close()
		}
	}()
	_, portStr, err := net.SplitHostPort(lis.Addr().String())
	if err != nil {
		t.Fatalf("split addr: %v", err)
	}
	p, _ := strconv.Atoi(portStr)
	return uint32(p)
}

// newStatefulTestServer wires a Server with stateful support (reusing the
// serving fakes for the network seam, since stateful shares servingNet for
// tap/DNAT). BootReadyTimeout/RestoreReadyTimeout are short so a ready
// endpoint gates fast.
func newStatefulTestServer(t *testing.T) (*Server, *fakeServingNet, *fakeStatefulDriver) {
	t.Helper()
	dir := t.TempDir()
	volumeRoot := dir + "/volumes"
	fsd := newFakeStatefulDriver(dir)
	fsn := newFakeServingNet()
	s := New(Options{
		Config: config.Config{
			Arch: "amd64", Node: "node-4", MaxLiveVMs: 4, SnapshotRoot: dir,
			BootReadyTimeout:    2 * time.Second,
			RestoreReadyTimeout: 2 * time.Second,
			Images:              map[string]config.Image{"img-a": {RootfsPath: "/rootfs/a"}},
			VolumeRoot:          volumeRoot,
		},
		Driver:         statefulVMDriverAdapter{fsd},
		ServingNet:     fsn,
		ServingDriver:  newFakeServingDriver(dir), // stateful boot resolves against servingImage inventory
		StatefulDriver: fsd,
		VolumeRoot:     volumeRoot,
		Logger:         slog.New(slog.NewTextHandler(io.Discard, nil)),
	})
	s.memHeadroom = func() uint64 { return 0 }
	// Seed a built serving image "img-a" the same way a real BuildBase would
	// have populated it; StartStateful resolves boot_image_ref against the
	// SAME inventory a serving cold boot uses (D-R3.11.2).
	s.servingImage.add(servingImageEntry{
		baseKey:         "img-a",
		workload:        "wl-state",
		handlerPath:     "/disks/bases/img-a/handler.zip",
		runtimeImageRef: "img-a",
		sizeBytes:       2048,
	})
	return s, fsn, fsd
}

// startFreshStateful is a helper that FRESH-boots a stateful VM (creating the
// volume) against a ready loopback TCP endpoint.
func startFreshStateful(t *testing.T, s *Server, port uint32, workload string) *nodev1.StartStatefulResponse {
	t.Helper()
	resp, err := s.StartStateful(context.Background(), &nodev1.StartStatefulRequest{
		Trace:           &nodev1.Trace{Workload: workload},
		Mode:            nodev1.StartStatefulMode_START_STATEFUL_MODE_FRESH,
		BootImageRef:    "img-a",
		Port:            port,
		VolumeSizeBytes: 1 << 20,
		VolumeMount:     "/var/lib/postgresql/data",
		CreateIfMissing: true,
	})
	if err != nil {
		t.Fatalf("StartStateful(fresh): %v", err)
	}
	return resp
}

func TestStartStatefulFreshCreatesVolumeAndBoots(t *testing.T) {
	port := tcpHealthServer(t)
	s, _, fsd := newStatefulTestServer(t)

	resp := startFreshStateful(t, s, port, "wl-state")
	if resp.GetVmId() == "" {
		t.Error("StartStateful returned no vm_id")
	}
	if resp.GetGeneration() != 1 {
		t.Errorf("generation = %d want 1 (first FRESH attach bumps from 0)", resp.GetGeneration())
	}
	if resp.GetWasRelight() {
		t.Error("a FRESH boot must not report was_relight")
	}
	if resp.GetColdBootReason() != "" {
		t.Errorf("cold_boot_reason = %q want empty for FRESH", resp.GetColdBootReason())
	}
	if fsd.claimCount != 1 {
		t.Errorf("ClaimStateful calls = %d want 1", fsd.claimCount)
	}
	if fsd.lastVolMount != "/var/lib/postgresql/data" {
		t.Errorf("volume mount = %q want the requested mount path", fsd.lastVolMount)
	}
	ns := s.nodeStatus()
	if len(ns.GetStatefulVms()) != 1 {
		t.Fatalf("stateful_vms = %d want 1", len(ns.GetStatefulVms()))
	}
	if !ns.GetStatefulVms()[0].GetHealthy() {
		t.Error("a freshly started stateful VM should report healthy")
	}
	if ns.GetStatefulVms()[0].GetGeneration() != 1 {
		t.Errorf("NodeStatus generation = %d want 1", ns.GetStatefulVms()[0].GetGeneration())
	}
	if len(ns.GetVolumes()) != 1 || !ns.GetVolumes()[0].GetAttached() {
		t.Errorf("volumes = %+v want one attached volume", ns.GetVolumes())
	}
}

// TestStartStatefulFreshWithoutCreateIfMissingRefused proves FRESH against an
// absent volume with create_if_missing=false is FAILED_PRECONDITION (data
// fails closed: a missing volume is never silently recreated).
func TestStartStatefulFreshWithoutCreateIfMissingRefused(t *testing.T) {
	port := tcpHealthServer(t)
	s, _, _ := newStatefulTestServer(t)
	_, err := s.StartStateful(context.Background(), &nodev1.StartStatefulRequest{
		Trace:           &nodev1.Trace{Workload: "wl-state"},
		Mode:            nodev1.StartStatefulMode_START_STATEFUL_MODE_FRESH,
		BootImageRef:    "img-a",
		Port:            port,
		VolumeSizeBytes: 1 << 20,
		CreateIfMissing: false,
	})
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("FRESH without create_if_missing on an absent volume: got %v want FailedPrecondition", err)
	}
}

// TestStartStatefulAttachLockRefusesSecondAttach proves the singleton
// writable-attach invariant end to end: a second StartStateful for the SAME
// workload while the first is still live is refused FAILED_PRECONDITION.
func TestStartStatefulAttachLockRefusesSecondAttach(t *testing.T) {
	port := tcpHealthServer(t)
	s, _, _ := newStatefulTestServer(t)
	startFreshStateful(t, s, port, "wl-state")

	_, err := s.StartStateful(context.Background(), &nodev1.StartStatefulRequest{
		Trace:        &nodev1.Trace{Workload: "wl-state"},
		Mode:         nodev1.StartStatefulMode_START_STATEFUL_MODE_COLD,
		BootImageRef: "img-a",
		Port:         port,
		VolumeMount:  "/var/lib/postgresql/data",
	})
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("second attach of an already-attached workload: got %v want FailedPrecondition", err)
	}
}

// TestStartStatefulGenerationBumpedBeforeBootAndNotRolledBackOnFailure proves
// the pairing invariant: BumpGeneration runs BEFORE the boot attempt, and a
// boot (Claim) failure leaves the ledger bumped, not reverted.
func TestStartStatefulGenerationBumpedBeforeBootAndNotRolledBackOnFailure(t *testing.T) {
	port := tcpHealthServer(t)
	s, _, fsd := newStatefulTestServer(t)
	fsd.failClaim = status.Error(codes.Internal, "boom")

	_, err := s.StartStateful(context.Background(), &nodev1.StartStatefulRequest{
		Trace:           &nodev1.Trace{Workload: "wl-state"},
		Mode:            nodev1.StartStatefulMode_START_STATEFUL_MODE_FRESH,
		BootImageRef:    "img-a",
		Port:            port,
		VolumeSizeBytes: 1 << 20,
		VolumeMount:     "/data",
		CreateIfMissing: true,
	})
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("boot failure: got %v want FailedPrecondition", err)
	}
	gen, gerr := s.volumes.Generation("wl-state")
	if gerr != nil {
		t.Fatalf("Generation after failed boot: %v", gerr)
	}
	if gen != 1 {
		t.Errorf("generation after a failed boot = %d want 1 (bump is never rolled back)", gen)
	}
	// The failed attach must have released the lock so a retry is possible.
	if s.volumes.IsAttached("wl-state") {
		t.Error("a failed ClaimStateful must release the attach lock")
	}
}

// TestStartStatefulRelightMatchedGeneration proves RELIGHT with a matching
// stamped generation resumes the bundle (was_relight=true, no cold_boot_reason).
func TestStartStatefulRelightMatchedGeneration(t *testing.T) {
	port := tcpHealthServer(t)
	s, _, fsd := newStatefulTestServer(t)
	started := startFreshStateful(t, s, port, "wl-state")

	bankResp, err := s.StopStateful(context.Background(), &nodev1.StopStatefulRequest{
		VmId: started.GetVmId(), Mode: nodev1.StopStatefulMode_STOP_STATEFUL_MODE_BANK,
		Trace: &nodev1.Trace{Workload: "wl-state"},
	})
	if err != nil {
		t.Fatalf("StopStateful(bank): %v", err)
	}
	if bankResp.GetGeneration() != 1 {
		t.Fatalf("bank generation = %d want 1", bankResp.GetGeneration())
	}

	relit, err := s.StartStateful(context.Background(), &nodev1.StartStatefulRequest{
		Trace:              &nodev1.Trace{Workload: "wl-state"},
		Mode:               nodev1.StartStatefulMode_START_STATEFUL_MODE_RELIGHT,
		BootImageRef:       "img-a",
		RelightSnapshotRef: bankResp.GetSnapshotRef(),
		Port:               port,
		VolumeMount:        "/data",
	})
	if err != nil {
		t.Fatalf("StartStateful(relight): %v", err)
	}
	if !relit.GetWasRelight() {
		t.Error("matched-generation relight should report was_relight=true")
	}
	if relit.GetColdBootReason() != "" {
		t.Errorf("cold_boot_reason = %q want empty for a matched relight", relit.GetColdBootReason())
	}
	if relit.GetGeneration() != 2 {
		t.Errorf("relight generation = %d want 2 (bumped again on resume)", relit.GetGeneration())
	}
	_ = fsd
}

// TestStartStatefulRelightGenerationMismatchFallsBackAndEvicts proves a
// generation mismatch (a cold/fresh boot happened after the bank, advancing
// the ledger past what the bundle was stamped with) evicts the bundle and
// falls back to a cold boot with cold_boot_reason=generation_mismatch.
func TestStartStatefulRelightGenerationMismatchFallsBackAndEvicts(t *testing.T) {
	port := tcpHealthServer(t)
	s, _, fsd := newStatefulTestServer(t)
	started := startFreshStateful(t, s, port, "wl-state")

	bankResp, err := s.StopStateful(context.Background(), &nodev1.StopStatefulRequest{
		VmId: started.GetVmId(), Mode: nodev1.StopStatefulMode_STOP_STATEFUL_MODE_BANK,
		Trace: &nodev1.Trace{Workload: "wl-state"},
	})
	if err != nil {
		t.Fatalf("bank: %v", err)
	}
	staleRef := bankResp.GetSnapshotRef()

	// Advance the ledger past the bundle's stamped generation with an explicit
	// COLD boot + destroy (no bank), so the bundle is now stale.
	cold, err := s.StartStateful(context.Background(), &nodev1.StartStatefulRequest{
		Trace: &nodev1.Trace{Workload: "wl-state"}, Mode: nodev1.StartStatefulMode_START_STATEFUL_MODE_COLD,
		BootImageRef: "img-a", Port: port, VolumeMount: "/data",
	})
	if err != nil {
		t.Fatalf("cold boot to advance generation: %v", err)
	}
	if _, err := s.StopStateful(context.Background(), &nodev1.StopStatefulRequest{
		VmId: cold.GetVmId(), Mode: nodev1.StopStatefulMode_STOP_STATEFUL_MODE_DESTROY,
	}); err != nil {
		t.Fatalf("destroy: %v", err)
	}

	relit, err := s.StartStateful(context.Background(), &nodev1.StartStatefulRequest{
		Trace:              &nodev1.Trace{Workload: "wl-state"},
		Mode:               nodev1.StartStatefulMode_START_STATEFUL_MODE_RELIGHT,
		BootImageRef:       "img-a",
		RelightSnapshotRef: staleRef,
		Port:               port,
		VolumeMount:        "/data",
	})
	if err != nil {
		t.Fatalf("StartStateful(relight, mismatched): %v", err)
	}
	if relit.GetWasRelight() {
		t.Error("a mismatched relight must fall back to cold boot (was_relight=false)")
	}
	if relit.GetColdBootReason() != coldBootReasonGenerationMismatch {
		t.Errorf("cold_boot_reason = %q want %q", relit.GetColdBootReason(), coldBootReasonGenerationMismatch)
	}
	// The stale bundle must have been evicted.
	if _, ok := s.statefulBundles.get(staleRef); ok {
		t.Error("a mismatched relight should evict the stale bundle from the registry")
	}
	if _, ok := fsd.banked[staleRef]; ok {
		t.Error("a mismatched relight should have called RemoveStatefulBundle")
	}
}

// TestStartStatefulRelightNoBundleFallsBack proves an unknown
// relight_snapshot_ref falls back to cold boot with cold_boot_reason=no_bundle.
func TestStartStatefulRelightNoBundleFallsBack(t *testing.T) {
	port := tcpHealthServer(t)
	s, _, _ := newStatefulTestServer(t)
	// Create the volume via a FRESH boot then destroy, so RELIGHT's
	// volume-must-exist precondition is satisfied but no bundle exists.
	started := startFreshStateful(t, s, port, "wl-state")
	if _, err := s.StopStateful(context.Background(), &nodev1.StopStatefulRequest{
		VmId: started.GetVmId(), Mode: nodev1.StopStatefulMode_STOP_STATEFUL_MODE_DESTROY,
	}); err != nil {
		t.Fatalf("destroy: %v", err)
	}

	relit, err := s.StartStateful(context.Background(), &nodev1.StartStatefulRequest{
		Trace:              &nodev1.Trace{Workload: "wl-state"},
		Mode:               nodev1.StartStatefulMode_START_STATEFUL_MODE_RELIGHT,
		BootImageRef:       "img-a",
		RelightSnapshotRef: "ghost-ref",
		Port:               port,
		VolumeMount:        "/data",
	})
	if err != nil {
		t.Fatalf("StartStateful(relight, no bundle): %v", err)
	}
	if relit.GetColdBootReason() != coldBootReasonNoBundle {
		t.Errorf("cold_boot_reason = %q want %q", relit.GetColdBootReason(), coldBootReasonNoBundle)
	}
}

// TestStartStatefulRelightLedgerUnreadableEvictsBundle proves a RELIGHT
// against a workload whose ledger cannot be read (simulated by corrupting the
// file directly) is DETECTED as ledger_unreadable and evicts the bundle before
// attempting the cold-boot fallback. The fallback itself also needs to bump
// the (corrupted) ledger, so with a genuinely unreadable ledger the whole call
// fails fail-closed (an operational hazard needing manual repair, not a value
// this daemon should silently paper over) -- but the eviction must have
// already happened, so a subsequent RELIGHT against the same stale ref reports
// no_bundle rather than repeatedly trying to resume unusable warmth.
func TestStartStatefulRelightLedgerUnreadableEvictsBundle(t *testing.T) {
	port := tcpHealthServer(t)
	s, _, fsd := newStatefulTestServer(t)
	started := startFreshStateful(t, s, port, "wl-state")
	bankResp, err := s.StopStateful(context.Background(), &nodev1.StopStatefulRequest{
		VmId: started.GetVmId(), Mode: nodev1.StopStatefulMode_STOP_STATEFUL_MODE_BANK,
		Trace: &nodev1.Trace{Workload: "wl-state"},
	})
	if err != nil {
		t.Fatalf("bank: %v", err)
	}
	staleRef := bankResp.GetSnapshotRef()

	// Corrupt the generation ledger directly on disk.
	genPath := s.cfg.VolumeRoot + "/wl-state/gen"
	if err := os.WriteFile(genPath, []byte("not-a-number"), 0o600); err != nil {
		t.Fatalf("corrupt ledger: %v", err)
	}

	_, err = s.StartStateful(context.Background(), &nodev1.StartStatefulRequest{
		Trace:              &nodev1.Trace{Workload: "wl-state"},
		Mode:               nodev1.StartStatefulMode_START_STATEFUL_MODE_RELIGHT,
		BootImageRef:       "img-a",
		RelightSnapshotRef: staleRef,
		Port:               port,
		VolumeMount:        "/data",
	})
	// A genuinely corrupted ledger cannot be bumped even for the cold-boot
	// fallback, so the call fails; the important assertion is what happened
	// BEFORE that failure (the stale bundle was evicted), not the outer error.
	if err == nil {
		t.Fatal("StartStateful against a corrupted ledger should fail (fail-closed)")
	}
	if _, ok := s.statefulBundles.get(staleRef); ok {
		t.Error("an unreadable-ledger relight should evict the bundle before attempting the fallback boot")
	}
	if _, ok := fsd.banked[staleRef]; ok {
		t.Error("an unreadable-ledger relight should have called RemoveStatefulBundle")
	}
}

// TestStopStatefulBankEvictsPriorBundle proves at most one banked bundle per
// workload: a second bank for the same workload evicts the first.
func TestStopStatefulBankEvictsPriorBundle(t *testing.T) {
	port := tcpHealthServer(t)
	s, _, fsd := newStatefulTestServer(t)
	started := startFreshStateful(t, s, port, "wl-state")
	firstBank, err := s.StopStateful(context.Background(), &nodev1.StopStatefulRequest{
		VmId: started.GetVmId(), Mode: nodev1.StopStatefulMode_STOP_STATEFUL_MODE_BANK,
		Trace: &nodev1.Trace{Workload: "wl-state"},
	})
	if err != nil {
		t.Fatalf("first bank: %v", err)
	}

	cold, err := s.StartStateful(context.Background(), &nodev1.StartStatefulRequest{
		Trace: &nodev1.Trace{Workload: "wl-state"}, Mode: nodev1.StartStatefulMode_START_STATEFUL_MODE_COLD,
		BootImageRef: "img-a", Port: port, VolumeMount: "/data",
	})
	if err != nil {
		t.Fatalf("cold boot: %v", err)
	}
	secondBank, err := s.StopStateful(context.Background(), &nodev1.StopStatefulRequest{
		VmId: cold.GetVmId(), Mode: nodev1.StopStatefulMode_STOP_STATEFUL_MODE_BANK,
		Trace: &nodev1.Trace{Workload: "wl-state"},
	})
	if err != nil {
		t.Fatalf("second bank: %v", err)
	}
	if secondBank.GetSnapshotRef() == firstBank.GetSnapshotRef() {
		t.Fatal("test setup: expected distinct snapshot refs")
	}
	if _, ok := s.statefulBundles.get(firstBank.GetSnapshotRef()); ok {
		t.Error("second bank should have evicted the first bundle from the registry")
	}
	if _, ok := fsd.banked[firstBank.GetSnapshotRef()]; ok {
		t.Error("second bank should have called RemoveStatefulBundle for the prior ref")
	}
	if _, ok := s.statefulBundles.get(secondBank.GetSnapshotRef()); !ok {
		t.Error("second bundle should be recorded")
	}
	ns := s.nodeStatus()
	if len(ns.GetStatefulBundles()) != 1 {
		t.Errorf("stateful_bundles = %d want 1 (at most one per workload)", len(ns.GetStatefulBundles()))
	}
}

// TestDeleteVolumeRefusedWhileAttached proves DeleteVolume is FAILED_PRECONDITION
// against a live attach and succeeds once detached.
func TestDeleteVolumeRefusedWhileAttached(t *testing.T) {
	port := tcpHealthServer(t)
	s, _, _ := newStatefulTestServer(t)
	started := startFreshStateful(t, s, port, "wl-state")

	_, err := s.DeleteVolume(context.Background(), &nodev1.DeleteVolumeRequest{Workload: "wl-state"})
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("DeleteVolume while attached: got %v want FailedPrecondition", err)
	}

	if _, err := s.StopStateful(context.Background(), &nodev1.StopStatefulRequest{
		VmId: started.GetVmId(), Mode: nodev1.StopStatefulMode_STOP_STATEFUL_MODE_DESTROY,
	}); err != nil {
		t.Fatalf("destroy: %v", err)
	}
	if _, err := s.DeleteVolume(context.Background(), &nodev1.DeleteVolumeRequest{Workload: "wl-state"}); err != nil {
		t.Errorf("DeleteVolume after detach should succeed: %v", err)
	}
	if len(s.nodeStatus().GetVolumes()) != 0 {
		t.Error("deleted volume should be gone from inventory")
	}
	// Idempotent.
	if _, err := s.DeleteVolume(context.Background(), &nodev1.DeleteVolumeRequest{Workload: "wl-state"}); err != nil {
		t.Errorf("DeleteVolume of an already-absent volume should be idempotent OK: %v", err)
	}
}

// TestReconcileStatefulFromDiskRediscoversBundles proves the boot-rescan
// source: a fresh Server pointed at the same driver/volume root reports the
// banked bundles and volumes a prior daemon incarnation left behind.
func TestReconcileStatefulFromDiskRediscoversBundles(t *testing.T) {
	dir := t.TempDir()
	fsd := newFakeStatefulDriver(dir)
	fsd.banked["state-banked-1"] = 3 // simulate a bundle already on "disk" (in the fake)

	vm := volume.NewManager(dir + "/volumes")
	if err := vm.Create("wl-state", 1<<20); err != nil {
		t.Fatalf("seed volume: %v", err)
	}
	if _, err := vm.BumpGeneration("wl-state"); err != nil {
		t.Fatalf("seed generation: %v", err)
	}

	s := New(Options{
		Config:         config.Config{Arch: "amd64", Node: "node-4", SnapshotRoot: dir},
		Driver:         statefulVMDriverAdapter{fsd},
		ServingNet:     newFakeServingNet(),
		ServingDriver:  newFakeServingDriver(dir),
		StatefulDriver: fsd,
		VolumeRoot:     dir + "/volumes",
		Logger:         slog.New(slog.NewTextHandler(io.Discard, nil)),
	})
	s.ReconcileStatefulFromDisk()

	ns := s.nodeStatus()
	if len(ns.GetStatefulBundles()) != 1 {
		t.Fatalf("rescan found %d stateful bundles want 1", len(ns.GetStatefulBundles()))
	}
	if ns.GetStatefulBundles()[0].GetSnapshotRef() != "state-banked-1" {
		t.Errorf("rescanned ref = %q want state-banked-1", ns.GetStatefulBundles()[0].GetSnapshotRef())
	}
	if ns.GetStatefulBundles()[0].GetGeneration() != 3 {
		t.Errorf("rescanned generation = %d want 3", ns.GetStatefulBundles()[0].GetGeneration())
	}
	if len(ns.GetVolumes()) != 1 || ns.GetVolumes()[0].GetGeneration() != 1 {
		t.Errorf("volumes = %+v want one volume at generation 1", ns.GetVolumes())
	}
}
