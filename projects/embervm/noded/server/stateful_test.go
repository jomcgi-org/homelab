package server

import (
	"context"
	"errors"
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
	mu       sync.Mutex
	live     int
	claims   int
	restores int
	resumes  int
	// banked maps snapshotRef -> the generation it was stamped with.
	banked       map[string]uint64
	statefulDir  string
	failClaim    error
	lastVolPath  string
	lastVolMount string
	claimCount   int
	lastMmdsEnv  map[string]string
	// checkpoints maps a checkpoint token -> the pending checkpoint (ADR 008). A
	// checkpointed VM stays live (paused). failCheckpoint / failResume script the
	// failure paths.
	checkpoints    map[string]fakeCheckpoint
	failCheckpoint error
	failResume     error
	// pinnedIPs maps a banked snapshotRef -> the tap IP it was banked with, so a
	// test can assert relight re-pins it (ADR embervm/008 relight IP fix).
	pinnedIPs      map[string]string
	restoreStarted chan struct{}
	releaseRestore chan struct{}
	apiSocketPath  string
}

type fakeCheckpoint struct {
	snapshotRef string
	generation  uint64
	pinnedIP    string
}

func newFakeStatefulDriver(dir string) *fakeStatefulDriver {
	return &fakeStatefulDriver{
		banked:      map[string]uint64{},
		statefulDir: dir + "/stateful",
		checkpoints: map[string]fakeCheckpoint{},
		pinnedIPs:   map[string]string{},
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

func (f *fakeStatefulDriver) SnapshotStateful(_ context.Context, _ substrate.Handle, snapshotRef string, generation uint64, pinnedIP string) (substrate.SnapshotRef, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.banked[snapshotRef] = generation
	f.pinnedIPs[snapshotRef] = pinnedIP
	return substrate.SnapshotRef{ID: snapshotRef, SizeBytes: 4096}, nil
}

func (f *fakeStatefulDriver) StatefulPinnedIP(snapshotRef string) string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.pinnedIPs[snapshotRef]
}

func (f *fakeStatefulDriver) StatefulAPISocketPath(_ substrate.Handle) string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.apiSocketPath
}

func (f *fakeStatefulDriver) RestoreStateful(_ context.Context, snapshotRef, _ string) (substrate.Handle, error) {
	f.mu.Lock()
	if _, ok := f.banked[snapshotRef]; !ok {
		f.mu.Unlock()
		return substrate.Handle{}, status.Errorf(codes.FailedPrecondition, "no such banked stateful snapshot %q", snapshotRef)
	}
	started, release := f.restoreStarted, f.releaseRestore
	f.mu.Unlock()
	if started != nil {
		select {
		case started <- struct{}{}:
		default:
		}
	}
	if release != nil {
		<-release
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	f.live++
	f.restores++
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

// CheckpointStateful records a pending checkpoint and returns its token; the VM
// stays live (paused), so `live` is unchanged (ADR 008 phase one).
func (f *fakeStatefulDriver) CheckpointStateful(_ context.Context, _ substrate.Handle, snapshotRef string, generation uint64, pinnedIP string) (string, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.failCheckpoint != nil {
		return "", f.failCheckpoint
	}
	token := "ckpt-" + snapshotRef
	f.checkpoints[token] = fakeCheckpoint{snapshotRef: snapshotRef, generation: generation, pinnedIP: pinnedIP}
	return token, nil
}

// ResolveStatefulCommit publishes the checkpoint as a banked bundle and DESTROYS
// the VM (decrementing live, modeling the driver's internal Release), consuming
// the token (single-resolve).
func (f *fakeStatefulDriver) ResolveStatefulCommit(_ context.Context, token string) (substrate.SnapshotRef, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	cp, ok := f.checkpoints[token]
	if !ok {
		return substrate.SnapshotRef{}, status.Errorf(codes.FailedPrecondition, "unknown checkpoint token %q", token)
	}
	delete(f.checkpoints, token)
	f.banked[cp.snapshotRef] = cp.generation
	f.pinnedIPs[cp.snapshotRef] = cp.pinnedIP
	if f.live > 0 {
		f.live--
	}
	return substrate.SnapshotRef{ID: cp.snapshotRef, SizeBytes: 4096}, nil
}

// ResolveStatefulAbort resumes the VM (live unchanged) and consumes the token. If
// failResume is set it models the driver tearing the VM down on a resume failure.
func (f *fakeStatefulDriver) ResolveStatefulAbort(_ context.Context, token string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if _, ok := f.checkpoints[token]; !ok {
		return status.Errorf(codes.FailedPrecondition, "unknown checkpoint token %q", token)
	}
	delete(f.checkpoints, token)
	if f.failResume != nil {
		if f.live > 0 {
			f.live--
		}
		return f.failResume
	}
	f.resumes++
	return nil
}

// GCStatefulCheckpoints clears all pending checkpoints (a restart), returning the
// count swept.
func (f *fakeStatefulDriver) GCStatefulCheckpoints() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	n := len(f.checkpoints)
	f.checkpoints = map[string]fakeCheckpoint{}
	return n
}

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
	socketDir, err := os.MkdirTemp("/tmp", "stf-fake-api-")
	if err != nil {
		t.Fatalf("MkdirTemp fake Firecracker socket: %v", err)
	}
	socketPath := socketDir + "/api.sock"
	listener, err := net.Listen("unix", socketPath)
	if err != nil {
		_ = os.RemoveAll(socketDir)
		t.Fatalf("listen fake Firecracker socket: %v", err)
	}
	fsd.apiSocketPath = socketPath
	t.Cleanup(func() {
		_ = listener.Close()
		_ = os.RemoveAll(socketDir)
	})
	go func() {
		for {
			conn, acceptErr := listener.Accept()
			if acceptErr != nil {
				return
			}
			_ = conn.Close()
		}
	}()
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
		ServingDriver:  newFakeServingDriver(dir), // required by the serving lane; stateful boot resolves against the base registry
		StatefulDriver: fsd,
		VolumeRoot:     volumeRoot,
		Logger:         slog.New(slog.NewTextHandler(io.Discard, nil)),
	})
	s.memHeadroom = func() uint64 { return 0 }
	// Seed a READY base "img-a" the way a real BuildBase would have. An image-lane
	// stateful boot resolves boot_image_ref against the BASE registry (not the
	// serving-image inventory: an opaque-L4 guest has no handler artifact), then
	// cold-boots the runtime rootfs behind base.imageDigest ("img-a" is in cfg
	// Images above) with no drive-2 handler.
	s.bases.readyBuild("img-a", "wl-state", "img-a", "", "/shim/ready", 2048)
	return s, fsn, fsd
}

// startFreshStateful is a helper that FRESH-boots a stateful VM (creating the
// volume) against a ready loopback TCP endpoint. It carries the blessed
// generation the control plane would issue for a fresh volume (ledger 0, so 1):
// noded rejects an unblessed writable attach unconditionally (#4950).
func startFreshStateful(t *testing.T, s *Server, port uint32, workload string) *nodev1.StartStatefulResponse {
	t.Helper()
	resp, err := s.StartStateful(context.Background(), &nodev1.StartStatefulRequest{
		Trace:             &nodev1.Trace{Workload: workload},
		Mode:              nodev1.StartStatefulMode_START_STATEFUL_MODE_FRESH,
		BootImageRef:      "img-a",
		Port:              port,
		VolumeSizeBytes:   1 << 20,
		VolumeMount:       "/var/lib/postgresql/data",
		CreateIfMissing:   true,
		BlessedGeneration: 1,
	})
	if err != nil {
		t.Fatalf("StartStateful(fresh): %v", err)
	}
	return resp
}

// TestStartStatefulFreshResolvesFromPushedRegistry proves the artifact-decoupling
// Phase 2 prod condition: with cfg.Images EMPTY, a stateful cold boot resolves its
// runtime rootfs from the control-plane-PUSHED registry (by the base's runtime
// image_ref) instead of the config table.
func TestStartStatefulFreshResolvesFromPushedRegistry(t *testing.T) {
	port := tcpHealthServer(t)
	s, _, _ := newStatefulTestServer(t)

	s.cfg.Images = map[string]config.Image{}
	s.registry.sync([]workloadEntry{
		{Workload: "image:img-a", ImageRef: "img-a", RootfsRef: "/rootfs/a", HarnessInit: "/init"},
	})

	resp := startFreshStateful(t, s, port, "wl-state")
	if resp.GetVmId() == "" {
		t.Fatal("StartStateful(fresh) resolved no VM from the pushed registry")
	}
}

// TestStartStatefulFreshRefusedWhileStale proves a stale registry refuses a FRESH
// stateful boot (new-work placement, may create a volume) with FailedPrecondition.
func TestStartStatefulFreshRefusedWhileStale(t *testing.T) {
	port := tcpHealthServer(t)
	s, _, _ := newStatefulTestServer(t)
	s.registry.mu.Lock()
	s.registry.stale = true
	s.registry.mu.Unlock()

	_, err := s.StartStateful(context.Background(), &nodev1.StartStatefulRequest{
		Trace:           &nodev1.Trace{Workload: "wl-state"},
		Mode:            nodev1.StartStatefulMode_START_STATEFUL_MODE_FRESH,
		BootImageRef:    "img-a",
		Port:            port,
		VolumeSizeBytes: 1 << 20,
		VolumeMount:     "/var/lib/postgresql/data",
		CreateIfMissing: true,
	})
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("FRESH StartStateful while stale: err = %v, want FailedPrecondition", err)
	}
}

func TestStartStatefulFreshCreatesVolumeAndBoots(t *testing.T) {
	port := tcpHealthServer(t)
	s, _, fsd := newStatefulTestServer(t)

	resp := startFreshStateful(t, s, port, "wl-state")
	if resp.GetVmId() == "" {
		t.Error("StartStateful returned no vm_id")
	}
	if resp.GetGeneration() != 1 {
		t.Errorf("generation = %d want 1 (the CP-issued first blessed generation)", resp.GetGeneration())
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

// TestReleaseOrphanedAttachDropsDeadRegistryVM proves a registry entry is not
// accepted as attach-health testimony after its Firecracker API socket dies.
// The live socket preserves the attach; removing it drops the registry entry
// and lets ReleaseOrphaned reclaim the stale writable lock.
func TestReleaseOrphanedAttachDropsDeadRegistryVM(t *testing.T) {
	port := tcpHealthServer(t)
	s, _, fsd := newStatefulTestServer(t)
	resp := startFreshStateful(t, s, port, "wl-state")

	socketDir, err := os.MkdirTemp("", "stf-api-")
	if err != nil {
		t.Fatalf("MkdirTemp: %v", err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(socketDir) })
	socketPath := socketDir + "/api.sock"
	listener, err := net.Listen("unix", socketPath)
	if err != nil {
		t.Fatalf("listen on fake Firecracker socket: %v", err)
	}

	fsd.mu.Lock()
	fsd.apiSocketPath = socketPath
	fsd.mu.Unlock()

	s.releaseOrphanedAttach("wl-state")
	if entry, ok := s.statefulVMs.byWorkload("wl-state"); !ok || entry.vmID != resp.GetVmId() {
		t.Fatalf("healthy registry entry was dropped: entry = %+v, ok = %v", entry, ok)
	}
	if err := s.volumes.Attach("wl-state"); err == nil {
		s.volumes.Detach("wl-state")
		t.Fatal("healthy attach was reclaimed")
	}

	if err := listener.Close(); err != nil {
		t.Fatalf("close fake Firecracker socket: %v", err)
	}
	if err := os.Remove(socketPath); err != nil && !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("remove fake Firecracker socket: %v", err)
	}

	s.releaseOrphanedAttach("wl-state")
	if _, ok := s.statefulVMs.byWorkload("wl-state"); ok {
		t.Fatal("dead registry entry still reported by workload")
	}
	if err := s.volumes.Attach("wl-state"); err != nil {
		t.Fatalf("attach after dead-owner reclamation: %v", err)
	}
	s.volumes.Detach("wl-state")
}

// TestStartStatefulGenerationBumpedBeforeBootAndNotRolledBackOnFailure proves
// the pairing invariant: BumpGeneration runs BEFORE the boot attempt, and a
// boot (Claim) failure leaves the ledger bumped, not reverted.
func TestStartStatefulGenerationBumpedBeforeBootAndNotRolledBackOnFailure(t *testing.T) {
	port := tcpHealthServer(t)
	s, _, fsd := newStatefulTestServer(t)
	fsd.failClaim = status.Error(codes.Internal, "boom")

	_, err := s.StartStateful(context.Background(), &nodev1.StartStatefulRequest{
		Trace:             &nodev1.Trace{Workload: "wl-state"},
		Mode:              nodev1.StartStatefulMode_START_STATEFUL_MODE_FRESH,
		BootImageRef:      "img-a",
		Port:              port,
		VolumeSizeBytes:   1 << 20,
		VolumeMount:       "/data",
		CreateIfMissing:   true,
		BlessedGeneration: 1,
	})
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("boot failure: got %v want FailedPrecondition", err)
	}
	gen, gerr := s.volumes.Generation("wl-state")
	if gerr != nil {
		t.Fatalf("Generation after failed boot: %v", gerr)
	}
	if gen != 1 {
		t.Errorf("generation after a failed boot = %d want 1 (the record is never rolled back)", gen)
	}
	// The failed attach must have released the lock so a retry is possible.
	if s.volumes.IsAttached("wl-state") {
		t.Error("a failed ClaimStateful must release the attach lock")
	}
}

// TestStartStatefulBlessedGenerationRecordedVerbatim proves a nonzero
// blessed_generation on the request (R7, ADR embervm/011) is recorded onto
// the ledger EXACTLY as issued, not self-bumped, and the volume reads blessed
// afterward.
func TestStartStatefulBlessedGenerationRecordedVerbatim(t *testing.T) {
	port := tcpHealthServer(t)
	s, _, _ := newStatefulTestServer(t)

	resp, err := s.StartStateful(context.Background(), &nodev1.StartStatefulRequest{
		Trace:             &nodev1.Trace{Workload: "wl-state"},
		Mode:              nodev1.StartStatefulMode_START_STATEFUL_MODE_FRESH,
		BootImageRef:      "img-a",
		Port:              port,
		VolumeSizeBytes:   1 << 20,
		VolumeMount:       "/data",
		CreateIfMissing:   true,
		BlessedGeneration: 7,
	})
	if err != nil {
		t.Fatalf("StartStateful(fresh, blessed): %v", err)
	}
	if resp.GetGeneration() != 7 {
		t.Errorf("generation = %d want 7 (the control-plane-issued blessed_generation, recorded verbatim)", resp.GetGeneration())
	}
	if !s.volumes.GenerationBlessed("wl-state") {
		t.Error("volume should read as blessed after a blessed FRESH attach")
	}
	ns := s.nodeStatus()
	if len(ns.GetVolumes()) != 1 || !ns.GetVolumes()[0].GetGenerationBlessed() {
		t.Errorf("NodeStatus volumes = %+v want one volume with generation_blessed=true", ns.GetVolumes())
	}
}

// TestStartStatefulUnblessedRejected proves a writable attach carrying
// blessed_generation == 0 is refused FAILED_PRECONDITION rather than falling
// back to a legacy self-bump. The EMBERVM_NODED_REQUIRE_BLESSING rollout gate
// was collapsed in #4950: this is unconditional, no config knob.
func TestStartStatefulUnblessedRejected(t *testing.T) {
	port := tcpHealthServer(t)
	s, _, _ := newStatefulTestServer(t)

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
		t.Fatalf("unblessed attach: got %v want FailedPrecondition", err)
	}
	// The refusal must be fail-closed BEFORE any ledger write: an unblessed
	// attach never advances (or blesses) the generation.
	gen, gerr := s.volumes.Generation("wl-state")
	if gerr != nil {
		t.Fatalf("Generation after refused unblessed attach: %v", gerr)
	}
	if gen != 0 {
		t.Errorf("generation after refused unblessed attach = %d want 0 (no self-bump)", gen)
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
		BlessedGeneration:  2,
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
		t.Errorf("relight generation = %d want 2 (the CP-issued next generation, recorded on resume)", relit.GetGeneration())
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
		BootImageRef: "img-a", Port: port, VolumeMount: "/data", BlessedGeneration: 2,
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
		BlessedGeneration:  3,
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
		BlessedGeneration:  2,
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
		BlessedGeneration:  2,
	})
	// A genuinely corrupted ledger cannot be recorded even for the cold-boot
	// fallback (RecordBlessed must read the ledger first), so the call fails;
	// the important assertion is what happened BEFORE that failure (the stale
	// bundle was evicted), not the outer error.
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
		BootImageRef: "img-a", Port: port, VolumeMount: "/data", BlessedGeneration: 2,
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

// ---- interruptible bank checkpoint/resolve (ADR embervm/008) -----------------

// checkpointStateful CHECKPOINTs a running stateful VM and returns the response.
func checkpointStateful(t *testing.T, s *Server, vmID, workload string) *nodev1.StopStatefulResponse {
	t.Helper()
	resp, err := s.StopStateful(context.Background(), &nodev1.StopStatefulRequest{
		VmId:  vmID,
		Mode:  nodev1.StopStatefulMode_STOP_STATEFUL_MODE_CHECKPOINT,
		Trace: &nodev1.Trace{Workload: workload},
	})
	if err != nil {
		t.Fatalf("StopStateful(checkpoint): %v", err)
	}
	return resp
}

func statefulVMStatus(t *testing.T, s *Server, vmID string) *nodev1.StatefulVm {
	t.Helper()
	ns, err := s.GetNodeStatus(context.Background(), &nodev1.GetNodeStatusRequest{NodeId: "node-4"})
	if err != nil {
		t.Fatalf("GetNodeStatus: %v", err)
	}
	for _, v := range ns.GetStatefulVms() {
		if v.GetVmId() == vmID {
			return v
		}
	}
	return nil
}

// TestStatefulCheckpointReportsPending: a CHECKPOINT returns a token and leaves
// the VM live and reported checkpoint_pending, not destroyed and not banked.
func TestStatefulCheckpointReportsPending(t *testing.T) {
	port := tcpHealthServer(t)
	s, _, fsd := newStatefulTestServer(t)
	started := startFreshStateful(t, s, port, "wl-state")

	ckpt := checkpointStateful(t, s, started.GetVmId(), "wl-state")
	if ckpt.GetCheckpointToken() == "" {
		t.Fatal("checkpoint must return a token")
	}
	if ckpt.GetGeneration() != 1 {
		t.Fatalf("checkpoint generation = %d want 1", ckpt.GetGeneration())
	}
	if ckpt.GetSnapshotRef() != "" {
		t.Fatalf("checkpoint publishes no bundle yet, got snapshot_ref %q", ckpt.GetSnapshotRef())
	}
	// The VM is paused, not destroyed, so it still counts as live.
	if fsd.liveCount() != 1 {
		t.Fatalf("live count after checkpoint = %d want 1 (paused, not destroyed)", fsd.liveCount())
	}
	v := statefulVMStatus(t, s, started.GetVmId())
	if v == nil || !v.GetCheckpointPending() {
		t.Fatalf("status should report the VM checkpoint_pending; got %+v", v)
	}
	if v.GetCheckpointToken() != ckpt.GetCheckpointToken() {
		t.Fatalf("status checkpoint_token = %q want %q", v.GetCheckpointToken(), ckpt.GetCheckpointToken())
	}
}

// TestStatefulResolveCommit: COMMIT publishes the bundle, destroys the VM, and
// records the banked bundle stamped with the checkpoint generation.
func TestStatefulResolveCommit(t *testing.T) {
	port := tcpHealthServer(t)
	s, _, fsd := newStatefulTestServer(t)
	started := startFreshStateful(t, s, port, "wl-state")
	ckpt := checkpointStateful(t, s, started.GetVmId(), "wl-state")

	resp, err := s.ResolveStateful(context.Background(), &nodev1.ResolveStatefulRequest{
		VmId:            started.GetVmId(),
		CheckpointToken: ckpt.GetCheckpointToken(),
		Mode:            nodev1.ResolveMode_RESOLVE_MODE_COMMIT,
	})
	if err != nil {
		t.Fatalf("ResolveStateful(commit): %v", err)
	}
	if resp.GetSnapshotRef() == "" || resp.GetGeneration() != 1 || resp.GetSizeBytes() == 0 {
		t.Fatalf("commit response = %+v, want a ref, generation 1, non-zero size", resp)
	}
	// VM destroyed.
	if fsd.liveCount() != 0 {
		t.Fatalf("live count after commit = %d want 0 (destroyed)", fsd.liveCount())
	}
	// Bundle recorded, stamped at the checkpoint generation.
	if b, ok := s.statefulBundles.byWorkload("wl-state"); !ok || b.snapshotRef != resp.GetSnapshotRef() || b.generation != 1 {
		t.Fatalf("expected a banked bundle for wl-state at gen 1; got %+v ok=%v", b, ok)
	}
	// A relight off it succeeds (proves the committed bundle is valid + paired).
	relit, err := s.StartStateful(context.Background(), &nodev1.StartStatefulRequest{
		Trace: &nodev1.Trace{Workload: "wl-state"}, Mode: nodev1.StartStatefulMode_START_STATEFUL_MODE_RELIGHT,
		BootImageRef: "img-a", RelightSnapshotRef: resp.GetSnapshotRef(), Port: port, VolumeMount: "/data",
		BlessedGeneration: 2,
	})
	if err != nil {
		t.Fatalf("relight off committed bundle: %v", err)
	}
	if !relit.GetWasRelight() {
		t.Error("relight off a committed checkpoint bundle should be warm")
	}
}

// TestStatefulResolveAbort: ABORT resumes the VM (still live), bumps the
// generation via the legacy self-bump lane (no blessed_generation on the
// request), and records NO bundle. The response now reports the bumped
// generation (R7, ADR embervm/011: ResolveStatefulResponse.Generation is
// populated on ABORT too, so the control plane can confirm what noded
// recorded), but the volume must NOT read as blessed since this is the
// self-bump lane, not a CP-issued blessing.
func TestStatefulResolveAbort(t *testing.T) {
	port := tcpHealthServer(t)
	s, _, fsd := newStatefulTestServer(t)
	started := startFreshStateful(t, s, port, "wl-state")
	ckpt := checkpointStateful(t, s, started.GetVmId(), "wl-state")

	resp, err := s.ResolveStateful(context.Background(), &nodev1.ResolveStatefulRequest{
		VmId:            started.GetVmId(),
		CheckpointToken: ckpt.GetCheckpointToken(),
		Mode:            nodev1.ResolveMode_RESOLVE_MODE_ABORT,
	})
	if err != nil {
		t.Fatalf("ResolveStateful(abort): %v", err)
	}
	if resp.GetSnapshotRef() != "" || resp.GetSizeBytes() != 0 {
		t.Fatalf("abort response should carry no bundle; got %+v", resp)
	}
	if resp.GetGeneration() != 2 {
		t.Fatalf("abort response generation = %d want 2 (the self-bumped generation, reported so the control plane can confirm it)", resp.GetGeneration())
	}
	// VM resumed, still live; no bundle.
	if fsd.liveCount() != 1 {
		t.Fatalf("live count after abort = %d want 1 (resumed)", fsd.liveCount())
	}
	if _, ok := s.statefulBundles.byWorkload("wl-state"); ok {
		t.Fatal("abort must not record a bundle")
	}
	// The abort bumped the generation (1 -> 2); status reports the resumed VM as
	// no longer checkpoint_pending, at the bumped generation.
	v := statefulVMStatus(t, s, started.GetVmId())
	if v == nil || v.GetCheckpointPending() {
		t.Fatalf("resumed VM should not be checkpoint_pending; got %+v", v)
	}
	if v.GetGeneration() != 2 {
		t.Fatalf("resumed VM generation = %d want 2 (abort bumped)", v.GetGeneration())
	}
	// Legacy self-bump lane (blessed_generation unset): the volume must NOT read
	// as blessed, since no control plane issued this generation.
	if s.volumes.GenerationBlessed("wl-state") {
		t.Error("a legacy self-bumped abort must not read as blessed")
	}
}

// TestStatefulResolveAbortWithBlessedGenerationRecordsBlessed proves the R7 fix
// (ADR embervm/011, standing decision 4): when ResolveStateful(ABORT) carries a
// nonzero blessed_generation (the normal CP-driven resolve path), noded records
// it via RecordBlessed rather than self-bumping, so the volume's genFile and
// blessedFile agree (GenerationBlessed reports true) and the response echoes the
// CP-issued value verbatim, never desyncing from the control plane's blessing
// ledger the way the pre-fix self-bump did.
func TestStatefulResolveAbortWithBlessedGenerationRecordsBlessed(t *testing.T) {
	port := tcpHealthServer(t)
	s, _, fsd := newStatefulTestServer(t)
	started := startFreshStateful(t, s, port, "wl-state")
	ckpt := checkpointStateful(t, s, started.GetVmId(), "wl-state")

	resp, err := s.ResolveStateful(context.Background(), &nodev1.ResolveStatefulRequest{
		VmId:              started.GetVmId(),
		CheckpointToken:   ckpt.GetCheckpointToken(),
		Mode:              nodev1.ResolveMode_RESOLVE_MODE_ABORT,
		BlessedGeneration: 9,
	})
	if err != nil {
		t.Fatalf("ResolveStateful(abort, blessed): %v", err)
	}
	if resp.GetGeneration() != 9 {
		t.Errorf("abort response generation = %d want 9 (the CP-issued blessed_generation, recorded verbatim)", resp.GetGeneration())
	}
	if !s.volumes.GenerationBlessed("wl-state") {
		t.Error("volume should read as blessed after a blessed ABORT resolve")
	}
	gen, gerr := s.volumes.Generation("wl-state")
	if gerr != nil {
		t.Fatalf("Generation after blessed abort: %v", gerr)
	}
	if gen != 9 {
		t.Errorf("ledger generation = %d want 9 (recorded verbatim, not self-bumped)", gen)
	}
	// VM resumed, still live; no bundle.
	if fsd.liveCount() != 1 {
		t.Fatalf("live count after blessed abort = %d want 1 (resumed)", fsd.liveCount())
	}
	v := statefulVMStatus(t, s, started.GetVmId())
	if v == nil || v.GetGeneration() != 9 {
		t.Fatalf("resumed VM status generation = %+v want 9", v)
	}
}

// TestStatefulResolveUnknownTokenErrors: a resolve for an unknown vm or a wrong
// token is FAILED_PRECONDITION.
func TestStatefulResolveUnknownTokenErrors(t *testing.T) {
	port := tcpHealthServer(t)
	s, _, _ := newStatefulTestServer(t)
	started := startFreshStateful(t, s, port, "wl-state")
	ckpt := checkpointStateful(t, s, started.GetVmId(), "wl-state")

	// Wrong token for a real checkpoint-pending VM.
	_, err := s.ResolveStateful(context.Background(), &nodev1.ResolveStatefulRequest{
		VmId: started.GetVmId(), CheckpointToken: "wrong", Mode: nodev1.ResolveMode_RESOLVE_MODE_COMMIT,
	})
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("wrong token: err = %v, want FailedPrecondition", err)
	}
	// Unknown vm.
	_, err = s.ResolveStateful(context.Background(), &nodev1.ResolveStatefulRequest{
		VmId: "no-such-vm", CheckpointToken: ckpt.GetCheckpointToken(), Mode: nodev1.ResolveMode_RESOLVE_MODE_ABORT,
	})
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("unknown vm: err = %v, want FailedPrecondition", err)
	}
}

// TestStatefulSecondCheckpointInFlightErrors: a second CHECKPOINT (or a BANK) of
// an already-checkpointed VM is FAILED_PRECONDITION (the stop guard).
func TestStatefulSecondCheckpointInFlightErrors(t *testing.T) {
	port := tcpHealthServer(t)
	s, _, _ := newStatefulTestServer(t)
	started := startFreshStateful(t, s, port, "wl-state")
	_ = checkpointStateful(t, s, started.GetVmId(), "wl-state")

	_, err := s.StopStateful(context.Background(), &nodev1.StopStatefulRequest{
		VmId: started.GetVmId(), Mode: nodev1.StopStatefulMode_STOP_STATEFUL_MODE_CHECKPOINT,
		Trace: &nodev1.Trace{Workload: "wl-state"},
	})
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("second checkpoint: err = %v, want FailedPrecondition", err)
	}
}

// TestStatefulResolveSingleResolve: a second resolve of a consumed token is
// FAILED_PRECONDITION (single-resolve).
func TestStatefulResolveSingleResolve(t *testing.T) {
	port := tcpHealthServer(t)
	s, _, _ := newStatefulTestServer(t)
	started := startFreshStateful(t, s, port, "wl-state")
	ckpt := checkpointStateful(t, s, started.GetVmId(), "wl-state")

	if _, err := s.ResolveStateful(context.Background(), &nodev1.ResolveStatefulRequest{
		VmId: started.GetVmId(), CheckpointToken: ckpt.GetCheckpointToken(), Mode: nodev1.ResolveMode_RESOLVE_MODE_ABORT,
	}); err != nil {
		t.Fatalf("first resolve: %v", err)
	}
	_, err := s.ResolveStateful(context.Background(), &nodev1.ResolveStatefulRequest{
		VmId: started.GetVmId(), CheckpointToken: ckpt.GetCheckpointToken(), Mode: nodev1.ResolveMode_RESOLVE_MODE_COMMIT,
	})
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("second resolve of a consumed token: err = %v, want FailedPrecondition", err)
	}
}

// TestStatefulResolveTimeoutAutoAborts: noded auto-aborts an unresolved
// checkpoint after its resolve timeout, so a dead control plane cannot pin the
// paused VM; a COMMIT arriving after the auto-abort is refused (single-resolve).
func TestStatefulResolveTimeoutAutoAborts(t *testing.T) {
	port := tcpHealthServer(t)
	s, _, fsd := newStatefulTestServer(t)
	s.statefulResolveTimeout = 40 * time.Millisecond
	started := startFreshStateful(t, s, port, "wl-state")
	ckpt := checkpointStateful(t, s, started.GetVmId(), "wl-state")

	// Wait past the resolve timeout for the auto-abort to fire.
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		v := statefulVMStatus(t, s, started.GetVmId())
		if v != nil && !v.GetCheckpointPending() {
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	v := statefulVMStatus(t, s, started.GetVmId())
	if v == nil || v.GetCheckpointPending() {
		t.Fatalf("auto-abort should have resumed the VM (not checkpoint_pending); got %+v", v)
	}
	if fsd.liveCount() != 1 {
		t.Fatalf("live count after auto-abort = %d want 1 (resumed)", fsd.liveCount())
	}
	// A late COMMIT is refused: the timer already claimed the resolve.
	_, err := s.ResolveStateful(context.Background(), &nodev1.ResolveStatefulRequest{
		VmId: started.GetVmId(), CheckpointToken: ckpt.GetCheckpointToken(), Mode: nodev1.ResolveMode_RESOLVE_MODE_COMMIT,
	})
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("late commit after auto-abort: err = %v, want FailedPrecondition", err)
	}
}

// TestStatefulResolveInvalidModeDoesNotConsume: an invalid resolve mode is
// InvalidArgument and does NOT consume the checkpoint (a valid resolve still works).
func TestStatefulResolveInvalidModeDoesNotConsume(t *testing.T) {
	port := tcpHealthServer(t)
	s, _, _ := newStatefulTestServer(t)
	started := startFreshStateful(t, s, port, "wl-state")
	ckpt := checkpointStateful(t, s, started.GetVmId(), "wl-state")

	_, err := s.ResolveStateful(context.Background(), &nodev1.ResolveStatefulRequest{
		VmId: started.GetVmId(), CheckpointToken: ckpt.GetCheckpointToken(),
		Mode: nodev1.ResolveMode_RESOLVE_MODE_UNSPECIFIED,
	})
	if status.Code(err) != codes.InvalidArgument {
		t.Fatalf("invalid mode: err = %v, want InvalidArgument", err)
	}
	// The checkpoint is still resolvable (not consumed by the invalid attempt).
	if _, err := s.ResolveStateful(context.Background(), &nodev1.ResolveStatefulRequest{
		VmId: started.GetVmId(), CheckpointToken: ckpt.GetCheckpointToken(), Mode: nodev1.ResolveMode_RESOLVE_MODE_ABORT,
	}); err != nil {
		t.Fatalf("abort after invalid-mode attempt should still work: %v", err)
	}
}

// TestStartStatefulRelightRepinsTapIP proves the relight IP-pin fix: a bank
// records the VM's tap IP, and the relight re-acquires that SAME IP via
// AllocateTapForIP (not a fresh AllocateTap), so the resumed guest's baked-in
// eth0 matches the host tap and is reachable.
func TestStartStatefulRelightRepinsTapIP(t *testing.T) {
	port := tcpHealthServer(t)
	s, fsn, _ := newStatefulTestServer(t)
	started := startFreshStateful(t, s, port, "wl-state")

	bankResp, err := s.StopStateful(context.Background(), &nodev1.StopStatefulRequest{
		VmId: started.GetVmId(), Mode: nodev1.StopStatefulMode_STOP_STATEFUL_MODE_BANK,
		Trace: &nodev1.Trace{Workload: "wl-state"},
	})
	if err != nil {
		t.Fatalf("StopStateful(bank): %v", err)
	}

	relit, err := s.StartStateful(context.Background(), &nodev1.StartStatefulRequest{
		Trace: &nodev1.Trace{Workload: "wl-state"}, Mode: nodev1.StartStatefulMode_START_STATEFUL_MODE_RELIGHT,
		BootImageRef: "img-a", RelightSnapshotRef: bankResp.GetSnapshotRef(), Port: port, VolumeMount: "/data",
		BlessedGeneration: 2,
	})
	if err != nil {
		t.Fatalf("StartStateful(relight): %v", err)
	}
	if !relit.GetWasRelight() {
		t.Fatal("a matched-generation relight should report was_relight")
	}
	// The relight re-acquired the SAME tap IP the bank recorded (127.0.0.1, the
	// fake AllocateTap IP), via AllocateTapForIP.
	pins := fsn.pinReacquires()
	found := false
	for _, ip := range pins {
		if ip == "127.0.0.1" {
			found = true
		}
	}
	if !found {
		t.Fatalf("relight should re-pin the banked tap IP 127.0.0.1 via AllocateTapForIP; got pins %v", pins)
	}
}
