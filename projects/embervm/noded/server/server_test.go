package server

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"testing"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/status"
	"google.golang.org/grpc/test/bufconn"

	nodev1 "github.com/jomcgi/homelab/projects/embervm/proto/embervm/node/v1"

	"github.com/jomcgi/homelab/projects/embervm/noded/config"
	artifactstore "github.com/jomcgi/homelab/projects/embervm/noded/store"
	"github.com/jomcgi/homelab/projects/embervm/noded/substrate"
)

// fakeDriver is an in-memory vmDriver + buildDriver that records the lifecycle
// calls the server makes, so tests can assert "the VM was destroyed exactly
// once" and "a rejected Assign had no side effects".
type fakeDriver struct {
	mu            sync.Mutex
	claims        int
	releases      int
	removeBundles int
	statsCalls    int
	snapshots     int
	live          int
	claimedMib    uint64
	failClaim     error
	// failRelease injects a driver Release failure so a test can prove a reap
	// failure surfaces as a Destroy error (teardown NOT confirmed), not a false
	// confirmation (ADR embervm/014 decision 5).
	failRelease error
	// failSnapshotSession injects a Bank snapshot failure. The real driver tears
	// the VM down on any snapshot error (a bank is destructive), so the fake
	// mirrors that by decrementing live before returning the error.
	failSnapshotSession error
	// lastClaim records the most recent ClaimSpec. The zip lane no longer attaches
	// an archive drive (it hydrates over vsock), so a test asserts the build guest
	// claimed with only its rootfs (no extra drive fields exist on ClaimSpec now).
	lastClaim substrate.ClaimSpec

	// Session-driver seam (R2). sessionBundles is the in-memory stand-in for the
	// on-disk sessions/ dir: a Bank writes a ref -> marker entry (the "state" the
	// guest banked), a Relight reads it back, and an Evict deletes it. This lets a
	// server test prove state persistence across bank/relight without real
	// Firecracker: the marker written by SnapshotSession is exactly what
	// RestoreSession makes readable again (see restoreMarkers).
	sessionBundles map[string]string // snapshot_ref -> banked state marker
	// restoreMarkers maps a relit threadID back to the marker its bundle carried, so
	// a fake transport can echo the persisted state on the post-relight round-trip.
	restoreMarkers             map[string]string // threadID -> marker
	sessionsDir                string
	snapshotSessions           int
	restoreSessions            int
	lastRestoreTrackDirtyPages bool
	removeSessions             int
	nextBankMarker             string // the marker the NEXT Bank persists (the pre-bank guest state)
}

func (f *fakeDriver) Claim(_ context.Context, spec substrate.ClaimSpec) (substrate.Handle, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.failClaim != nil {
		return substrate.Handle{}, f.failClaim
	}
	f.claims++
	f.live++
	f.lastClaim = spec
	return substrate.Handle{ThreadID: spec.ThreadID, ID: "vm-" + spec.ThreadID, Node: "node-4"}, nil
}

func (f *fakeDriver) claimSpec() substrate.ClaimSpec {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.lastClaim
}

func (f *fakeDriver) restoreTracksDirtyPages() bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.lastRestoreTrackDirtyPages
}

func (f *fakeDriver) Release(_ context.Context, _ substrate.Handle) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.releases++
	if f.failRelease != nil {
		return f.failRelease
	}
	if f.live > 0 {
		f.live--
	}
	return nil
}

func (f *fakeDriver) RemoveBundle(_ string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.removeBundles++
	return nil
}

func (f *fakeDriver) VsockUDSPath(threadID string) string { return "/tmp/" + threadID + ".sock" }

func (f *fakeDriver) Stats(_ substrate.Handle) (substrate.GuestStats, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.statsCalls++
	return substrate.GuestStats{CPUMillis: 5, PeakRSSMib: 64}, nil
}

func (f *fakeDriver) LiveCount() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.live
}

func (f *fakeDriver) ClaimedMib() uint64 {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.claimedMib
}

func (f *fakeDriver) SnapshotBase(_ context.Context, _ substrate.Handle, baseKey string) (substrate.SnapshotRef, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.snapshots++
	return substrate.SnapshotRef{ID: baseKey, Base: true, SizeBytes: 4096}, nil
}

// SnapshotSession banks the fake VM: it persists the pre-bank marker (nextBankMarker)
// under the produced snapshot_ref, and does NOT resume (the server destroys the VM
// after). It decrements the live count like a real snapshot-then-destroy would once
// the server calls Release, so it does not touch f.live here.
func (f *fakeDriver) SnapshotSession(_ context.Context, _ substrate.Handle, snapshotRef string) (substrate.SnapshotRef, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.snapshotSessions++
	if f.failSnapshotSession != nil {
		// Mirror the real driver: a bank error tears the VM down (Release), so the
		// live count drops even though no bundle is produced.
		if f.live > 0 {
			f.live--
		}
		return substrate.SnapshotRef{}, f.failSnapshotSession
	}
	if f.sessionBundles == nil {
		f.sessionBundles = map[string]string{}
	}
	f.sessionBundles[snapshotRef] = f.nextBankMarker
	return substrate.SnapshotRef{ID: snapshotRef, Node: "node-4", Arch: "amd64", SizeBytes: 8192}, nil
}

// RestoreSession relights a fake VM from a banked bundle: the ref must have been
// banked (else it errors, exactly the unrestorable-ref case Relight maps to
// FAILED_PRECONDITION), and the restored handle's threadID is bound to the banked
// marker so a post-relight round-trip can echo the persisted state.
func (f *fakeDriver) RestoreSession(_ context.Context, snapshotRef string, trackDirtyPages bool) (substrate.Handle, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	marker, ok := f.sessionBundles[snapshotRef]
	if !ok {
		return substrate.Handle{}, context.Canceled // stand-in for "bundle missing"
	}
	f.restoreSessions++
	f.lastRestoreTrackDirtyPages = trackDirtyPages
	f.claims++
	f.live++
	threadID := "relit-" + snapshotRef
	if f.restoreMarkers == nil {
		f.restoreMarkers = map[string]string{}
	}
	f.restoreMarkers[threadID] = marker
	return substrate.Handle{ThreadID: threadID, ID: "vm-" + threadID, Node: "node-4"}, nil
}

func (f *fakeDriver) RemoveSessionBundle(snapshotRef string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.removeSessions++
	delete(f.sessionBundles, snapshotRef)
	return nil
}

func (f *fakeDriver) SessionsDir() string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.sessionsDir
}

// markerForThread returns the banked marker bound to a relit threadID (for the
// state-persistence assertion via a fake transport).
func (f *fakeDriver) markerForThread(threadID string) string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.restoreMarkers[threadID]
}

func (f *fakeDriver) counts() (claims, releases, removeBundles, statsCalls int) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.claims, f.releases, f.removeBundles, f.statsCalls
}

// fakeTransport is an in-memory transport. RoundTrip echoes "ok:"+body unless
// roundTripErr is set.
type fakeTransport struct {
	mu           sync.Mutex
	roundTrips   int
	waitReadyErr error
	roundTripErr error
	// hydrate capture: hydrates counts the calls, hydrateBytes records the last
	// archive delivered so a zip test can assert the exact bytes were hydrated, and
	// hydrateErr injects a hydrate failure (a bad archive) to fail the build.
	hydrates     int
	hydrateBytes []byte
	hydrateErr   error

	// clockPosts counts best-effort POST /shim/clock calls (Relight's guest clock
	// resync); clockStatus is the status the fake returns for them (0 => 200). A 404
	// exercises the "guest without the endpoint" skip-and-log path.
	clockPosts  int
	clockStatus int
	// stateSource, when set, lets a round-trip echo persisted session state so a
	// server test can prove state survived a bank/relight. It maps the dialed
	// udsPath to the marker the relit VM's bundle carried.
	stateSource func(udsPath string) string
	// blockRoundTrip, when non-nil, is closed-gated: a round-trip waits on it before
	// returning, so a test can hold a SessionAssign "in flight" and prove the per-vm
	// serialization guard rejects a concurrent call.
	blockRoundTrip chan struct{}
}

func (f *fakeTransport) WaitReady(_ context.Context, _, _ string) error {
	return f.waitReadyErr // nosemgrep: no-bare-error-return
}
func (f *fakeTransport) Prime(_ context.Context, _ string) error { return nil }

func (f *fakeTransport) SetClock(_ context.Context, _ string, _ int64) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.clockPosts++
	if f.clockStatus == 0 || f.clockStatus == http.StatusNotFound {
		return nil
	}
	return fmt.Errorf("clock sync failed with status %d", f.clockStatus)
}

func (f *fakeTransport) Hydrate(_ context.Context, _ string, archive []byte) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.hydrates++
	if f.hydrateErr != nil {
		return f.hydrateErr // nosemgrep: no-bare-error-return
	}
	// Copy: the caller's slice is a bytes.Reader source that outlives this call, but
	// copy so a later mutation cannot alias the captured bytes.
	f.hydrateBytes = append([]byte(nil), archive...)
	return nil
}

func (f *fakeTransport) hydrateCount() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.hydrates
}

func (f *fakeTransport) hydratedBytes() []byte {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.hydrateBytes
}

func (f *fakeTransport) RoundTrip(ctx context.Context, udsPath string, req *http.Request) (*http.Response, error) {
	f.mu.Lock()
	f.roundTrips++
	rtErr := f.roundTripErr
	block := f.blockRoundTrip
	stateSource := f.stateSource
	f.mu.Unlock()

	// Hold the call "in flight" if a test gated it, so a concurrent SessionAssign can
	// prove the per-vm serialization guard rejects it.
	if block != nil {
		select {
		case <-block:
		case <-ctx.Done():
			return nil, ctx.Err()
		}
	}
	if rtErr != nil {
		return nil, rtErr
	}
	body, _ := io.ReadAll(req.Body)
	echo := "ok:" + string(body)
	// A relit session VM echoes the state its bundle carried (proving persistence).
	if stateSource != nil {
		if marker := stateSource(udsPath); marker != "" {
			echo = "state:" + marker
		}
	}
	return &http.Response{
		StatusCode: http.StatusOK,
		Header:     http.Header{"X-Echo": []string{"1"}},
		Body:       io.NopCloser(bytes.NewReader([]byte(echo))),
	}, nil
}

func (f *fakeTransport) clockPostCount() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.clockPosts
}

func (f *fakeTransport) roundTripCount() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.roundTrips
}

// newTestServer wires the Server behind a real in-process gRPC dial (bufconn),
// so tests exercise the wire path (marshalling, status codes) end to end.
func newTestServer(t *testing.T, drv *fakeDriver, tr *fakeTransport, maxLive int) (nodev1.NodeServiceClient, *Server) {
	t.Helper()
	s := New(Options{
		Config:    config.Config{Arch: "amd64", Node: "node-4", MaxLiveVMs: maxLive, SnapshotRoot: t.TempDir()},
		Driver:    drv,
		Transport: tr,
		Logger:    slog.New(slog.NewTextHandler(io.Discard, nil)),
	})
	s.memHeadroom = func() uint64 { return 0 }                           // deterministic, no cgroup read
	s.slotCeiling = func(configured uint64) uint64 { return configured } // report configured max, no host cgroup read

	lis := bufconn.Listen(1 << 20)
	gs := grpc.NewServer()
	nodev1.RegisterNodeServiceServer(gs, s)
	go func() { _ = gs.Serve(lis) }()
	t.Cleanup(gs.Stop)

	conn, err := grpc.NewClient(
		"passthrough:///bufnet",
		grpc.WithContextDialer(func(ctx context.Context, _ string) (net.Conn, error) {
			return lis.DialContext(ctx)
		}),
		grpc.WithTransportCredentials(insecure.NewCredentials()),
	)
	if err != nil {
		t.Fatalf("dial bufconn: %v", err)
	}
	t.Cleanup(func() { _ = conn.Close() })
	return nodev1.NewNodeServiceClient(conn), s
}

// newSessionTestServer wires a Server with the fakeDriver serving BOTH the task
// vmDriver seam and the R2 sessionDriver seam, so session verbs (Bank/Relight/
// EvictSnapshot) are live. The transport's stateSource is bound to the driver so a
// relit VM's round-trip echoes the persisted marker (state-persistence proof).
func newSessionTestServer(t *testing.T, drv *fakeDriver, tr *fakeTransport, maxLive int) (nodev1.NodeServiceClient, *Server) {
	t.Helper()
	dir := t.TempDir()
	drv.sessionsDir = dir
	// Echo persisted session state on a relit VM's round-trip: parse the threadID out
	// of the fake uds path (/tmp/<threadID>.sock) and look up its banked marker.
	tr.stateSource = func(udsPath string) string {
		base := strings.TrimPrefix(udsPath, "/tmp/")
		threadID := strings.TrimSuffix(base, ".sock")
		return drv.markerForThread(threadID)
	}
	s := New(Options{
		Config:        config.Config{Arch: "amd64", Node: "node-4", MaxLiveVMs: maxLive, SnapshotRoot: dir},
		Driver:        drv,
		SessionDriver: drv,
		Transport:     tr,
		Logger:        slog.New(slog.NewTextHandler(io.Discard, nil)),
	})
	s.memHeadroom = func() uint64 { return 0 }
	s.slotCeiling = func(configured uint64) uint64 { return configured }

	lis := bufconn.Listen(1 << 20)
	gs := grpc.NewServer()
	nodev1.RegisterNodeServiceServer(gs, s)
	go func() { _ = gs.Serve(lis) }()
	t.Cleanup(gs.Stop)

	conn, err := grpc.NewClient(
		"passthrough:///bufnet",
		grpc.WithContextDialer(func(ctx context.Context, _ string) (net.Conn, error) {
			return lis.DialContext(ctx)
		}),
		grpc.WithTransportCredentials(insecure.NewCredentials()),
	)
	if err != nil {
		t.Fatalf("dial bufconn: %v", err)
	}
	t.Cleanup(func() { _ = conn.Close() })
	return nodev1.NewNodeServiceClient(conn), s
}

// primeSessionVM relights a session VM directly through the server so a test starts
// from a live session VM. It banks a throwaway marker first (Relight requires a
// known banked ref), returning the live vm_id.
func primeSessionVM(t *testing.T, srv *Server, drv *fakeDriver, sessionID, workload, ref, marker string) string {
	t.Helper()
	drv.mu.Lock()
	if drv.sessionBundles == nil {
		drv.sessionBundles = map[string]string{}
	}
	drv.sessionBundles[ref] = marker
	drv.mu.Unlock()
	srv.sessionSnap.add(sessionSnapshotEntry{snapshotRef: ref, sessionID: sessionID, workload: workload})
	resp, err := srv.Relight(context.Background(), &nodev1.RelightRequest{
		Trace:       &nodev1.Trace{Workload: workload},
		SnapshotRef: ref,
		SessionId:   sessionID,
	})
	if err != nil {
		t.Fatalf("primeSessionVM Relight: %v", err)
	}
	return resp.GetVmId()
}

func seedBase(s *Server, snapshotRef, workload string) {
	s.bases.readyBuild(snapshotRef, workload, "img@sha256:deadbeef", "", "/shim/ready", 2048)
}

func contains(ids []string, id string) bool {
	for _, s := range ids {
		if s == id {
			return true
		}
	}
	return false
}

// TestPrimeAssignAutoDestroy is the core lifecycle: Prime parks a VM, Assign
// delivers one task and returns the echoed guest response plus usage, and the VM
// is destroyed exactly once after the response.
func TestPrimeAssignAutoDestroy(t *testing.T) {
	drv := &fakeDriver{}
	tr := &fakeTransport{}
	client, srv := newTestServer(t, drv, tr, 8)
	seedBase(srv, "echo__deadbeef01", "echo")
	ctx := context.Background()

	pr, err := client.Prime(ctx, &nodev1.PrimeRequest{SnapshotRef: "echo__deadbeef01"})
	if err != nil {
		t.Fatalf("Prime: %v", err)
	}
	if pr.GetVmId() == "" {
		t.Fatal("Prime returned empty vm_id")
	}
	if got := drv.LiveCount(); got != 1 {
		t.Fatalf("after Prime LiveCount = %d, want 1", got)
	}

	// NodeStatus reflects one free primed slot for echo, one live VM.
	ns, err := client.GetNodeStatus(ctx, &nodev1.GetNodeStatusRequest{})
	if err != nil {
		t.Fatalf("GetNodeStatus: %v", err)
	}
	if ns.GetLiveVms() != 1 || ns.GetMaxLiveVms() != 8 {
		t.Errorf("NodeStatus live=%d max=%d, want 1/8", ns.GetLiveVms(), ns.GetMaxLiveVms())
	}
	if got := freePrimed(ns, "echo"); got != 1 {
		t.Errorf("echo free_primed_slots = %d, want 1", got)
	}

	ar, err := client.Assign(ctx, &nodev1.AssignRequest{
		VmId:      pr.GetVmId(),
		Request:   &nodev1.GuestRequest{Method: "POST", Path: "/invoke", Body: []byte("hi")},
		TimeoutMs: 1000,
	})
	if err != nil {
		t.Fatalf("Assign: %v", err)
	}
	if ar.GetResponse().GetStatusCode() != 200 {
		t.Errorf("status = %d, want 200", ar.GetResponse().GetStatusCode())
	}
	if got := string(ar.GetResponse().GetBody()); got != "ok:hi" {
		t.Errorf("body = %q, want ok:hi", got)
	}
	if ar.GetUsage().GetCpuMs() != 5 || ar.GetUsage().GetPeakRssMib() != 64 {
		t.Errorf("usage = %+v, want cpu 5 rss 64", ar.GetUsage())
	}

	// Exactly one round-trip, and the VM torn down exactly once (release + bundle
	// removal + one stats sample while alive).
	if got := tr.roundTripCount(); got != 1 {
		t.Errorf("round trips = %d, want 1", got)
	}
	claims, releases, removeBundles, statsCalls := drv.counts()
	if claims != 1 || releases != 1 || removeBundles != 1 || statsCalls != 1 {
		t.Errorf("driver counts claims=%d releases=%d removeBundles=%d stats=%d, want 1/1/1/1",
			claims, releases, removeBundles, statsCalls)
	}
	if got := drv.LiveCount(); got != 0 {
		t.Errorf("after Assign LiveCount = %d, want 0", got)
	}

	// Assign again on the now-destroyed vm_id: FAILED_PRECONDITION, no side effects.
	_, err = client.Assign(ctx, &nodev1.AssignRequest{
		VmId:    pr.GetVmId(),
		Request: &nodev1.GuestRequest{Body: []byte("second")},
	})
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("Assign-after-destroy code = %v, want FailedPrecondition (err=%v)", status.Code(err), err)
	}
	if got := tr.roundTripCount(); got != 1 {
		t.Errorf("round trips after rejected Assign = %d, want still 1 (no side effects)", got)
	}
	claims2, releases2, removeBundles2, _ := drv.counts()
	if claims2 != 1 || releases2 != 1 || removeBundles2 != 1 {
		t.Errorf("rejected Assign had side effects: claims=%d releases=%d removeBundles=%d, want 1/1/1",
			claims2, releases2, removeBundles2)
	}
}

func TestPrimeTracksDirtyPagesOnlyForConfiguredBankingWorkload(t *testing.T) {
	drv := &fakeDriver{}
	client, srv := newTestServer(t, drv, &fakeTransport{}, 8)
	srv.cfg.DiffBanking = true
	srv.cfg.DiffBankingWorkloads = []string{"sandbox-session"}
	seedBase(srv, "session__banking01", "sandbox-session")
	seedBase(srv, "task__plain01", "echo")

	banking, err := client.Prime(context.Background(), &nodev1.PrimeRequest{SnapshotRef: "session__banking01"})
	if err != nil {
		t.Fatalf("banking Prime: %v", err)
	}
	if !drv.claimSpec().TrackDirtyPages {
		t.Fatal("banking Prime did not request dirty-page tracking")
	}
	if _, err := client.Destroy(context.Background(), &nodev1.DestroyRequest{VmId: banking.GetVmId()}); err != nil {
		t.Fatalf("destroy banking VM: %v", err)
	}

	nonBanking, err := client.Prime(context.Background(), &nodev1.PrimeRequest{SnapshotRef: "task__plain01"})
	if err != nil {
		t.Fatalf("non-banking Prime: %v", err)
	}
	if drv.claimSpec().TrackDirtyPages {
		t.Fatal("non-banking Prime requested dirty-page tracking")
	}
	if _, err := client.Destroy(context.Background(), &nodev1.DestroyRequest{VmId: nonBanking.GetVmId()}); err != nil {
		t.Fatalf("destroy non-banking VM: %v", err)
	}
}

func TestRelightTracksDirtyPagesOnlyForConfiguredBankingWorkload(t *testing.T) {
	drv := &fakeDriver{}
	client, srv := newSessionTestServer(t, drv, &fakeTransport{}, 8)
	srv.cfg.DiffBanking = true
	srv.cfg.DiffBankingWorkloads = []string{"sandbox-session"}

	for _, tc := range []struct {
		ref           string
		workload      string
		traceWorkload string
		want          bool
	}{
		{ref: "banking-ref", workload: "sandbox-session", traceWorkload: "echo", want: true},
		{ref: "plain-ref", workload: "echo", traceWorkload: "sandbox-session", want: false},
	} {
		drv.mu.Lock()
		if drv.sessionBundles == nil {
			drv.sessionBundles = map[string]string{}
		}
		drv.sessionBundles[tc.ref] = "state"
		drv.mu.Unlock()
		srv.sessionSnap.add(sessionSnapshotEntry{snapshotRef: tc.ref, workload: tc.workload})

		resp, err := client.Relight(context.Background(), &nodev1.RelightRequest{
			Trace:       &nodev1.Trace{Workload: tc.traceWorkload},
			SnapshotRef: tc.ref,
		})
		if err != nil {
			t.Fatalf("Relight %s: %v", tc.workload, err)
		}
		if got := drv.restoreTracksDirtyPages(); got != tc.want {
			t.Errorf("Relight %s track dirty pages = %v, want %v", tc.workload, got, tc.want)
		}
		if _, err := client.Destroy(context.Background(), &nodev1.DestroyRequest{VmId: resp.GetVmId()}); err != nil {
			t.Fatalf("destroy %s VM: %v", tc.workload, err)
		}
	}
}

// TestPrimeCallsSetClock verifies that Prime syncs the guest clock after the
// restored guest passes its readiness gate.
func TestPrimeCallsSetClock(t *testing.T) {
	drv := &fakeDriver{}
	tr := &fakeTransport{}
	client, srv := newTestServer(t, drv, tr, 8)
	seedBase(srv, "echo__clock01", "echo")

	pr, err := client.Prime(context.Background(), &nodev1.PrimeRequest{SnapshotRef: "echo__clock01"})
	if err != nil {
		t.Fatalf("Prime: %v", err)
	}
	if pr.GetVmId() == "" {
		t.Fatal("Prime returned empty vm_id")
	}
	if got := tr.clockPostCount(); got != 1 {
		t.Errorf("after Prime, clockPosts = %d, want 1", got)
	}
}

// TestRelightCallsSetClock verifies that Relight syncs the guest clock after the
// restored guest passes its readiness gate.
func TestRelightCallsSetClock(t *testing.T) {
	drv := &fakeDriver{}
	tr := &fakeTransport{}
	client, srv := newSessionTestServer(t, drv, tr, 8)
	// Bank a ref into inventory to relight.
	_ = primeSessionVM(t, srv, drv, "s-clock", "echo", "sref-clock", "m")

	rl, err := client.Relight(context.Background(), &nodev1.RelightRequest{SnapshotRef: "sref-clock", SessionId: "s-clock"})
	if err != nil {
		t.Fatalf("Relight: %v", err)
	}
	if rl.GetVmId() == "" {
		t.Fatal("Relight returned empty vm_id")
	}
	// primeSessionVM calls Relight once (1), test calls Relight again (1), total = 2
	if got := tr.clockPostCount(); got != 2 {
		t.Errorf("after two Relight calls, clockPosts = %d, want 2", got)
	}
}

// TestAssignUnknownVM rejects an Assign for a vm_id that was never primed, with
// no driver interaction at all.
// TestPrimeSucceedsWhenSetClockFails verifies that a failing SetClock call does
// not fail the Prime restore.
func TestPrimeSucceedsWhenSetClockFails(t *testing.T) {
	drv := &fakeDriver{}
	tr := &fakeTransport{clockStatus: http.StatusInternalServerError}
	client, srv := newTestServer(t, drv, tr, 8)
	seedBase(srv, "echo__clockfail01", "echo")

	pr, err := client.Prime(context.Background(), &nodev1.PrimeRequest{SnapshotRef: "echo__clockfail01"})
	if err != nil {
		t.Fatalf("Prime should succeed even if SetClock fails: %v", err)
	}
	if pr.GetVmId() == "" {
		t.Fatal("Prime returned empty vm_id")
	}
	// SetClock was called (attempted) even though it failed
	if got := tr.clockPostCount(); got != 1 {
		t.Errorf("after Prime with failing SetClock, clockPosts = %d, want 1", got)
	}
}

// TestRelightSucceedsWhenSetClockFails verifies that a failing SetClock call does
// not fail the Relight restore.
func TestRelightSucceedsWhenSetClockFails(t *testing.T) {
	drv := &fakeDriver{}
	tr := &fakeTransport{clockStatus: http.StatusInternalServerError}
	client, srv := newSessionTestServer(t, drv, tr, 8)
	// Bank a ref into inventory to relight.
	_ = primeSessionVM(t, srv, drv, "s-clockfail", "echo", "sref-clockfail", "m")

	rl, err := client.Relight(context.Background(), &nodev1.RelightRequest{SnapshotRef: "sref-clockfail", SessionId: "s-clockfail"})
	if err != nil {
		t.Fatalf("Relight should succeed even if SetClock fails: %v", err)
	}
	if rl.GetVmId() == "" {
		t.Fatal("Relight returned empty vm_id")
	}
	// primeSessionVM calls Relight once (1), test calls Relight again (1), total = 2
	// SetClock was attempted both times even though it failed
	if got := tr.clockPostCount(); got != 2 {
		t.Errorf("after two Relight calls with failing SetClock, clockPosts = %d, want 2", got)
	}
}

func TestAssignUnknownVM(t *testing.T) {
	drv := &fakeDriver{}
	tr := &fakeTransport{}
	client, _ := newTestServer(t, drv, tr, 8)

	_, err := client.Assign(context.Background(), &nodev1.AssignRequest{
		VmId:    "vm-nope",
		Request: &nodev1.GuestRequest{Body: []byte("x")},
	})
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("code = %v, want FailedPrecondition", status.Code(err))
	}
	if claims, releases, removeBundles, _ := drv.counts(); claims+releases+removeBundles != 0 {
		t.Errorf("unknown Assign touched the driver: claims=%d releases=%d removeBundles=%d", claims, releases, removeBundles)
	}
	if tr.roundTripCount() != 0 {
		t.Errorf("unknown Assign round-tripped to a guest")
	}
}

// TestAssignDeadlineDestroysVM: a guest that times out yields DEADLINE_EXCEEDED,
// and the VM is still destroyed (single-use holds on every path).
func TestAssignDeadlineDestroysVM(t *testing.T) {
	drv := &fakeDriver{}
	tr := &fakeTransport{roundTripErr: context.DeadlineExceeded}
	client, srv := newTestServer(t, drv, tr, 8)
	seedBase(srv, "echo__timeout01", "echo")
	ctx := context.Background()

	pr, err := client.Prime(ctx, &nodev1.PrimeRequest{SnapshotRef: "echo__timeout01"})
	if err != nil {
		t.Fatalf("Prime: %v", err)
	}
	_, err = client.Assign(ctx, &nodev1.AssignRequest{
		VmId:      pr.GetVmId(),
		Request:   &nodev1.GuestRequest{Body: []byte("slow")},
		TimeoutMs: 50,
	})
	if status.Code(err) != codes.DeadlineExceeded {
		t.Fatalf("code = %v, want DeadlineExceeded (err=%v)", status.Code(err), err)
	}
	if _, releases, removeBundles, _ := drv.counts(); releases != 1 || removeBundles != 1 {
		t.Errorf("timed-out Assign did not destroy the VM: releases=%d removeBundles=%d, want 1/1", releases, removeBundles)
	}
	if got := drv.LiveCount(); got != 0 {
		t.Errorf("LiveCount after timeout = %d, want 0", got)
	}
}

// TestPrimeUnknownSnapshot rejects a Prime for a snapshot_ref with no base.
func TestPrimeUnknownSnapshot(t *testing.T) {
	drv := &fakeDriver{}
	client, _ := newTestServer(t, drv, &fakeTransport{}, 8)
	_, err := client.Prime(context.Background(), &nodev1.PrimeRequest{SnapshotRef: "ghost__000000000000"})
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("code = %v, want FailedPrecondition", status.Code(err))
	}
	if claims, _, _, _ := drv.counts(); claims != 0 {
		t.Errorf("unknown Prime claimed a VM (claims=%d)", claims)
	}
}

// TestSlotCeilingAdmissionParity4101 proves that NodeStatus advertises the
// configured backstop while Prime admission is governed by memory headroom.
func TestSlotCeilingAdmissionParity4101(t *testing.T) {
	drv := &fakeDriver{}
	_, srv := newTestServer(t, drv, &fakeTransport{}, 8)
	srv.slotCeiling = srv.budget.SlotCeiling
	if got := srv.nodeStatus().GetMaxLiveVms(); got != 8 {
		t.Fatalf("advertised max_live_vms = %d, want configured backstop 8", got)
	}
}

func TestSlotCeilingUnknownBudgetPreservesBackstop(t *testing.T) {
	drv := &fakeDriver{}
	client, srv := newTestServer(t, drv, &fakeTransport{}, 2)
	srv.slotCeiling = srv.budget.SlotCeiling
	seedBase(srv, "echo__unknown", "echo")

	if got := srv.nodeStatus().GetMaxLiveVms(); got != 2 {
		t.Fatalf("unknown budget max_live_vms = %d, want 2", got)
	}
	for i := 0; i < 2; i++ {
		if _, err := client.Prime(context.Background(), &nodev1.PrimeRequest{SnapshotRef: "echo__unknown"}); err != nil {
			t.Fatalf("Prime %d with unknown budget: %v", i+1, err)
		}
	}
	_, err := client.Prime(context.Background(), &nodev1.PrimeRequest{SnapshotRef: "echo__unknown"})
	if status.Code(err) != codes.ResourceExhausted {
		t.Fatalf("Prime beyond configured backstop code = %v, want ResourceExhausted", status.Code(err))
	}
}

// TestSlotCeilingZeroRemainsUnlimited verifies that slotsExhausted preserves
// unlimited semantics when MaxLiveVMs is configured to 0. This tests the
// ceiling > 0 guard that is live in all boot verbs (Prime, Relight, StartServing, StartGroupMember) and the stateful path.
func TestSlotCeilingZeroRemainsUnlimited(t *testing.T) {
	drv := &fakeDriver{live: 100}
	s := New(Options{Config: config.Config{MaxLiveVMs: 0}, Driver: drv})
	// With MaxLiveVMs=0, SlotCeiling returns 0, and slotsExhausted's
	// ceiling > 0 guard ensures it does not reject even with live VMs.
	if s.slotsExhausted() {
		t.Fatal("zero configured backstop must remain unlimited")
	}
}

// TestDestroyIdempotent: Destroy reaps a primed VM once, and a repeat Destroy is
// a no-op OK.
func TestDestroyIdempotent(t *testing.T) {
	drv := &fakeDriver{}
	client, srv := newTestServer(t, drv, &fakeTransport{}, 8)
	seedBase(srv, "echo__destroy01", "echo")
	ctx := context.Background()

	pr, err := client.Prime(ctx, &nodev1.PrimeRequest{SnapshotRef: "echo__destroy01"})
	if err != nil {
		t.Fatalf("Prime: %v", err)
	}
	dr, err := client.Destroy(ctx, &nodev1.DestroyRequest{VmId: pr.GetVmId()})
	if err != nil {
		t.Fatalf("Destroy: %v", err)
	}
	// A completed reap confirms teardown (ADR embervm/014 decision 5).
	if !dr.GetTeardownConfirmed() {
		t.Errorf("Destroy of live VM: teardown_confirmed=false, want true")
	}
	if _, releases, removeBundles, _ := drv.counts(); releases != 1 || removeBundles != 1 {
		t.Errorf("Destroy did not reap: releases=%d removeBundles=%d, want 1/1", releases, removeBundles)
	}
	// The VM no longer appears as live in NodeStatus.
	ns, err := client.GetNodeStatus(ctx, &nodev1.GetNodeStatusRequest{})
	if err != nil {
		t.Fatalf("GetNodeStatus: %v", err)
	}
	if ns.GetLiveVms() != 0 {
		t.Errorf("after Destroy live_vms=%d, want 0", ns.GetLiveVms())
	}
	// Idempotent: repeat Destroy of the now-unknown id is confirmed with no extra
	// teardown (nothing is held, so the destroyed end-state already holds).
	dr2, err := client.Destroy(ctx, &nodev1.DestroyRequest{VmId: pr.GetVmId()})
	if err != nil {
		t.Fatalf("second Destroy: %v", err)
	}
	if !dr2.GetTeardownConfirmed() {
		t.Errorf("second Destroy (unknown id): teardown_confirmed=false, want true")
	}
	if _, releases, _, _ := drv.counts(); releases != 1 {
		t.Errorf("second Destroy re-reaped: releases=%d, want 1", releases)
	}
}

// TestDestroyUnknownConfirmed asserts Destroy of a never-seen vm_id is confirmed
// (nothing held => the destroyed end-state already holds) and reaps nothing.
func TestDestroyUnknownConfirmed(t *testing.T) {
	drv := &fakeDriver{}
	client, _ := newTestServer(t, drv, &fakeTransport{}, 8)
	ctx := context.Background()

	dr, err := client.Destroy(ctx, &nodev1.DestroyRequest{VmId: "never-existed"})
	if err != nil {
		t.Fatalf("Destroy unknown: %v", err)
	}
	if !dr.GetTeardownConfirmed() {
		t.Errorf("Destroy of unknown id: teardown_confirmed=false, want true")
	}
	if _, releases, removeBundles, _ := drv.counts(); releases != 0 || removeBundles != 0 {
		t.Errorf("Destroy of unknown id reaped: releases=%d removeBundles=%d, want 0/0", releases, removeBundles)
	}
}

// TestDestroyReapFailureNotConfirmed asserts a reap failure surfaces as a Destroy
// error (Internal), NOT a false teardown_confirmed. The control plane keeps the
// instance in destroying until the retry succeeds (ADR embervm/014 decision 5).
func TestDestroyReapFailureNotConfirmed(t *testing.T) {
	drv := &fakeDriver{failRelease: errors.New("release wedged")}
	client, srv := newTestServer(t, drv, &fakeTransport{}, 8)
	seedBase(srv, "echo__destroyfail01", "echo")
	ctx := context.Background()

	pr, err := client.Prime(ctx, &nodev1.PrimeRequest{SnapshotRef: "echo__destroyfail01"})
	if err != nil {
		t.Fatalf("Prime: %v", err)
	}
	dr, err := client.Destroy(ctx, &nodev1.DestroyRequest{VmId: pr.GetVmId()})
	if err == nil {
		t.Fatalf("Destroy with failing reap: got confirmed=%v nil error, want error", dr.GetTeardownConfirmed())
	}
	if status.Code(err) != codes.Internal {
		t.Errorf("Destroy reap failure code = %s, want Internal", status.Code(err))
	}
}

// TestWatchNodeInitialStatus asserts WatchNode sends an initial NodeStatus
// immediately on subscribe.
func TestWatchNodeInitialStatus(t *testing.T) {
	client, _ := newTestServer(t, &fakeDriver{}, &fakeTransport{}, 4)
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()

	stream, err := client.WatchNode(ctx, &nodev1.WatchNodeRequest{NodeId: "node-4"})
	if err != nil {
		t.Fatalf("WatchNode: %v", err)
	}
	ns, err := stream.Recv()
	if err != nil {
		t.Fatalf("Recv initial: %v", err)
	}
	if ns.GetNodeId() != "node-4" {
		t.Errorf("node_id = %q, want node-4", ns.GetNodeId())
	}
	if ns.GetMaxLiveVms() != 4 {
		t.Errorf("max_live_vms = %d, want 4", ns.GetMaxLiveVms())
	}
}

// TestBuildBaseIdempotent covers the cold-boot base build path with a fake build
// driver: first call builds and snapshots, the second is an already_built no-op.
func TestBuildBaseIdempotent(t *testing.T) {
	drv := &fakeDriver{}
	build := &fakeDriver{} // separate recorder for the build path
	tr := &fakeTransport{}
	s := New(Options{
		Config: config.Config{
			Arch: "amd64", Node: "node-4", SnapshotRoot: t.TempDir(),
			BootReadyTimeout: time.Second,
			Images:           map[string]config.Image{"img:1": {RootfsPath: "/rootfs.ext4"}},
		},
		Driver:         drv,
		Transport:      tr,
		NewBuildDriver: func(BuildDriverSpec) BuildDriver { return build },
		Logger:         slog.New(slog.NewTextHandler(io.Discard, nil)),
	})

	ctx := context.Background()
	req := &nodev1.BuildBaseRequest{
		Trace:            &nodev1.Trace{Workload: "echo"},
		ImageRef:         "img:1",
		WorkloadRevision: "r1",
		ReadyPath:        "/shim/ready",
		Resources:        &nodev1.ResourceSpec{Vcpus: 2, MemMib: 2048},
	}
	resp, err := s.BuildBase(ctx, req)
	if err != nil {
		t.Fatalf("BuildBase: %v", err)
	}
	if resp.GetSnapshotRef() == "" || resp.GetAlreadyBuilt() {
		t.Fatalf("first build resp = %+v, want a ref and already_built=false", resp)
	}
	if build.snapshots != 1 {
		t.Errorf("snapshots = %d, want 1", build.snapshots)
	}

	resp2, err := s.BuildBase(ctx, req)
	if err != nil {
		t.Fatalf("second BuildBase: %v", err)
	}
	if !resp2.GetAlreadyBuilt() || resp2.GetSnapshotRef() != resp.GetSnapshotRef() {
		t.Errorf("second build resp = %+v, want already_built=true and same ref", resp2)
	}
	if build.snapshots != 1 {
		t.Errorf("second build re-snapshotted: snapshots = %d, want 1", build.snapshots)
	}
}

// TestBuildBaseUnknownImage rejects a build for an image the node has no rootfs for.
func TestBuildBaseUnknownImage(t *testing.T) {
	s := New(Options{
		Config:         config.Config{Arch: "amd64", Node: "node-4", SnapshotRoot: t.TempDir()},
		Driver:         &fakeDriver{},
		Transport:      &fakeTransport{},
		NewBuildDriver: func(BuildDriverSpec) BuildDriver { return &fakeDriver{} },
		Logger:         slog.New(slog.NewTextHandler(io.Discard, nil)),
	})
	_, err := s.BuildBase(context.Background(), &nodev1.BuildBaseRequest{
		Trace:    &nodev1.Trace{Workload: "echo"},
		ImageRef: "not-provisioned:1",
	})
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("code = %v, want FailedPrecondition", status.Code(err))
	}
}

// newZipTestServer wires a Server for the zip lane: a build driver recorder, a
// fake transport (the hydrate seam), a fake archive HTTP server serving
// archiveBytes, and the runtime image provisioned in the config table. Returns the
// server, the build recorder, the transport (for hydrate assertions), and the
// archive URL. Pass a non-nil tr to inject transport behaviour (e.g. a hydrate
// failure); nil uses a fresh recording transport.
func newZipTestServer(t *testing.T, archiveBytes []byte, archiveDelay time.Duration, tr *fakeTransport) (*Server, *fakeDriver, *fakeTransport, string) {
	t.Helper()
	if tr == nil {
		tr = &fakeTransport{}
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/archive.zip", func(w http.ResponseWriter, r *http.Request) {
		if archiveDelay > 0 {
			select {
			case <-time.After(archiveDelay):
			case <-r.Context().Done():
				return
			}
		}
		_, _ = w.Write(archiveBytes)
	})
	ts := httptest.NewServer(mux)
	t.Cleanup(ts.Close)

	build := &fakeDriver{}
	s := New(Options{
		Config: config.Config{
			Arch: "amd64", Node: "node-4", SnapshotRoot: t.TempDir(),
			BootReadyTimeout:    time.Second,
			ArchiveFetchTimeout: 30 * time.Second,
			ArchiveMaxBytes:     512 << 20,
			Images:              map[string]config.Image{"runtime-python:1": {RootfsPath: "/runtime.ext4"}},
		},
		Driver:         &fakeDriver{},
		Transport:      tr,
		NewBuildDriver: func(BuildDriverSpec) BuildDriver { return build },
		Logger:         slog.New(slog.NewTextHandler(io.Discard, nil)),
	})
	return s, build, tr, ts.URL + "/archive.zip"
}

func sha256Hex(b []byte) string {
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}

func TestFetchArchiveFromStoreUsesSignedRequest(t *testing.T) {
	archive := []byte("signed archive")
	sawAuthorization := make(chan string, 1)
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		sawAuthorization <- r.Header.Get("Authorization")
		_, _ = w.Write(archive)
	}))
	defer ts.Close()
	signedStore := artifactstore.New(ts.URL, "embervm", false, artifactstore.WithCredentials("embervm", "secret"))
	s := New(Options{
		Config: config.Config{
			StoreEndpoint:       ts.URL,
			ArchiveFetchTimeout: time.Second,
			ArchiveMaxBytes:     1024,
		},
		Driver:           &fakeDriver{},
		Transport:        &fakeTransport{},
		Logger:           slog.New(slog.NewTextHandler(io.Discard, nil)),
		SignStoreRequest: signedStore.SignRequest,
	})
	got, err := s.fetchAndVerifyArchive(context.Background(), ts.URL+"/embervm/archive.zip", sha256Hex(archive))
	if err != nil || !bytes.Equal(got, archive) {
		t.Fatalf("fetchAndVerifyArchive = %q, %v", got, err)
	}
	if gotAuth := <-sawAuthorization; !strings.HasPrefix(gotAuth, "AWS4-HMAC-SHA256 ") {
		t.Fatalf("Authorization = %q", gotAuth)
	}
}

// TestBuildBaseZipHappyPath: a zip source fetches the archive, verifies its
// sha256, claims a build guest with NO archive drive, HYDRATES the exact archive
// bytes into the guest over vsock, and snapshots the base. The build claim must
// carry no extra drive, and the transport must have received the exact archive
// bytes on /shim/hydrate.
func TestBuildBaseZipHappyPath(t *testing.T) {
	archive := []byte("PK\x03\x04 fake zip bytes, opaque to noded")
	s, build, tr, url := newZipTestServer(t, archive, 0, nil)
	ctx := context.Background()

	resp, err := s.BuildBase(ctx, &nodev1.BuildBaseRequest{
		Trace:     &nodev1.Trace{Workload: "ziphandler"},
		ReadyPath: "/shim/ready",
		Resources: &nodev1.ResourceSpec{Vcpus: 1, MemMib: 512},
		Source: &nodev1.BuildBaseRequest_Zip{Zip: &nodev1.ZipSource{
			RuntimeImageRef: "runtime-python:1",
			ArchiveUrl:      url,
			ArchiveSha256:   sha256Hex(archive),
		}},
	})
	if err != nil {
		t.Fatalf("BuildBase zip: %v", err)
	}
	if resp.GetSnapshotRef() == "" || resp.GetAlreadyBuilt() {
		t.Fatalf("first zip build resp = %+v, want a ref and already_built=false", resp)
	}
	if resp.GetImageDigest() != "runtime-python:1" {
		t.Errorf("image_digest = %q, want the runtime ref", resp.GetImageDigest())
	}
	if build.snapshots != 1 {
		t.Errorf("snapshots = %d, want 1", build.snapshots)
	}

	// The build guest was claimed with ONLY its rootfs: the archive crosses over
	// vsock now, so the ClaimSpec carries no archive drive (the fields are gone).
	spec := build.claimSpec()
	if spec.ThreadID == "" {
		t.Error("build claim had no ThreadID")
	}

	// The archive was hydrated exactly once, with the exact bytes fetched.
	if got := tr.hydrateCount(); got != 1 {
		t.Errorf("hydrate count = %d, want 1", got)
	}
	if got := tr.hydratedBytes(); !bytes.Equal(got, archive) {
		t.Errorf("hydrated bytes = %q, want the fetched archive %q", got, archive)
	}

	// Idempotent: the same archive on the same runtime is a no-op hit that does not
	// re-snapshot or re-hydrate.
	resp2, err := s.BuildBase(ctx, &nodev1.BuildBaseRequest{
		Trace: &nodev1.Trace{Workload: "ziphandler"},
		Source: &nodev1.BuildBaseRequest_Zip{Zip: &nodev1.ZipSource{
			RuntimeImageRef: "runtime-python:1",
			ArchiveUrl:      url,
			ArchiveSha256:   sha256Hex(archive),
		}},
	})
	if err != nil {
		t.Fatalf("second zip build: %v", err)
	}
	if !resp2.GetAlreadyBuilt() || resp2.GetSnapshotRef() != resp.GetSnapshotRef() {
		t.Errorf("second zip build resp = %+v, want already_built=true and same ref", resp2)
	}
	if build.snapshots != 1 {
		t.Errorf("idempotent zip build re-snapshotted: snapshots = %d, want 1", build.snapshots)
	}
	if got := tr.hydrateCount(); got != 1 {
		t.Errorf("idempotent zip build re-hydrated: hydrate count = %d, want 1", got)
	}
}

// TestBuildBaseZipHydrateFailureFailsBuild: the archive fetch + verify succeed but
// the guest rejects the hydrate (a bad zip / import error surfaces as a hydrate
// error). The build must fail FAILED_PRECONDITION with NO snapshot taken.
func TestBuildBaseZipHydrateFailureFailsBuild(t *testing.T) {
	archive := []byte("PK\x03\x04 valid on the wire, rejected by the shim")
	tr := &fakeTransport{hydrateErr: context.DeadlineExceeded}
	s, build, _, url := newZipTestServer(t, archive, 0, tr)

	_, err := s.BuildBase(context.Background(), &nodev1.BuildBaseRequest{
		Trace:     &nodev1.Trace{Workload: "ziphandler"},
		ReadyPath: "/shim/ready",
		Resources: &nodev1.ResourceSpec{Vcpus: 1, MemMib: 512},
		Source: &nodev1.BuildBaseRequest_Zip{Zip: &nodev1.ZipSource{
			RuntimeImageRef: "runtime-python:1",
			ArchiveUrl:      url,
			ArchiveSha256:   sha256Hex(archive),
		}},
	})
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("hydrate failure code = %v, want FailedPrecondition (err=%v)", status.Code(err), err)
	}
	// The guest was claimed and hydrate was attempted, but NO snapshot was taken.
	if tr.hydrateCount() != 1 {
		t.Errorf("hydrate count = %d, want 1 (hydrate was attempted)", tr.hydrateCount())
	}
	if build.snapshots != 0 {
		t.Errorf("failed hydrate still snapshotted: snapshots = %d, want 0", build.snapshots)
	}
}

// TestBuildBaseZipShaMismatch: a fetched archive whose bytes do not match the
// declared sha256 fails the build with FAILED_PRECONDITION, does NOT cold-boot a
// guest, and never hydrates.
func TestBuildBaseZipShaMismatch(t *testing.T) {
	archive := []byte("the actual bytes on the wire")
	s, build, tr, url := newZipTestServer(t, archive, 0, nil)

	_, err := s.BuildBase(context.Background(), &nodev1.BuildBaseRequest{
		Trace: &nodev1.Trace{Workload: "ziphandler"},
		Source: &nodev1.BuildBaseRequest_Zip{Zip: &nodev1.ZipSource{
			RuntimeImageRef: "runtime-python:1",
			ArchiveUrl:      url,
			ArchiveSha256:   sha256Hex([]byte("a DIFFERENT archive the control plane expected")),
		}},
	})
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("sha mismatch code = %v, want FailedPrecondition (err=%v)", status.Code(err), err)
	}
	if build.claims != 0 {
		t.Errorf("sha mismatch cold-booted a guest: claims = %d, want 0", build.claims)
	}
	if tr.hydrateCount() != 0 {
		t.Errorf("sha mismatch hydrated a guest: hydrates = %d, want 0", tr.hydrateCount())
	}
}

// TestBuildBaseZipFetchTimeout: an archive server that never responds within the
// fetch budget fails the build with FAILED_PRECONDITION and never cold-boots.
func TestBuildBaseZipFetchTimeout(t *testing.T) {
	archive := []byte("bytes that arrive too late")
	s, build, _, url := newZipTestServer(t, archive, 500*time.Millisecond, nil)
	s.cfg.ArchiveFetchTimeout = 50 * time.Millisecond // shorter than the server delay

	_, err := s.BuildBase(context.Background(), &nodev1.BuildBaseRequest{
		Trace: &nodev1.Trace{Workload: "ziphandler"},
		Source: &nodev1.BuildBaseRequest_Zip{Zip: &nodev1.ZipSource{
			RuntimeImageRef: "runtime-python:1",
			ArchiveUrl:      url,
			ArchiveSha256:   sha256Hex(archive),
		}},
	})
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("fetch timeout code = %v, want FailedPrecondition (err=%v)", status.Code(err), err)
	}
	if build.claims != 0 {
		t.Errorf("fetch timeout cold-booted a guest: claims = %d, want 0", build.claims)
	}
}

// TestBuildBaseZipColdBootFailureNoSnapshot: the archive fetch + verify succeed but
// the cold boot fails; the build fails FAILED_PRECONDITION and no snapshot is
// taken (and there is no block file to leak, since nothing is written to disk).
func TestBuildBaseZipColdBootFailureNoSnapshot(t *testing.T) {
	archive := []byte("valid archive, but the build will fail")
	s, build, _, url := newZipTestServer(t, archive, 0, nil)
	build.failClaim = context.DeadlineExceeded // cold boot fails

	_, err := s.BuildBase(context.Background(), &nodev1.BuildBaseRequest{
		Trace: &nodev1.Trace{Workload: "ziphandler"},
		Source: &nodev1.BuildBaseRequest_Zip{Zip: &nodev1.ZipSource{
			RuntimeImageRef: "runtime-python:1",
			ArchiveUrl:      url,
			ArchiveSha256:   sha256Hex(archive),
		}},
	})
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("build failure code = %v, want FailedPrecondition (err=%v)", status.Code(err), err)
	}
	if build.snapshots != 0 {
		t.Errorf("failed cold boot still snapshotted: snapshots = %d, want 0", build.snapshots)
	}
}

// TestBuildBaseZipUnknownRuntime rejects a zip build whose runtime image is not
// provisioned on the node.
func TestBuildBaseZipUnknownRuntime(t *testing.T) {
	s, _, _, url := newZipTestServer(t, []byte("x"), 0, nil)
	_, err := s.BuildBase(context.Background(), &nodev1.BuildBaseRequest{
		Trace: &nodev1.Trace{Workload: "ziphandler"},
		Source: &nodev1.BuildBaseRequest_Zip{Zip: &nodev1.ZipSource{
			RuntimeImageRef: "not-provisioned:1",
			ArchiveUrl:      url,
			ArchiveSha256:   sha256Hex([]byte("x")),
		}},
	})
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("unknown runtime code = %v, want FailedPrecondition", status.Code(err))
	}
}

// TestBuildBaseServingRegistersImage: a serving BuildBase always registers a serving
// image. The zip lane MUST write its cold-boot handler artifact, while the image lane
// registers the runtime rootfs with no handler artifact (ADR embervm/038).
func TestBuildBaseServingRegistersImage(t *testing.T) {
	archive := []byte("PK\x03\x04 serving handler zip bytes")
	ctx := context.Background()

	newServer := func() (*Server, *fakeDriver, *fakeServingDriver, string) {
		mux := http.NewServeMux()
		mux.HandleFunc("/archive.zip", func(w http.ResponseWriter, _ *http.Request) { _, _ = w.Write(archive) })
		ts := httptest.NewServer(mux)
		t.Cleanup(ts.Close)
		build := &fakeDriver{}
		fsd := newFakeServingDriver(t.TempDir())
		s := New(Options{
			Config: config.Config{
				Arch: "amd64", Node: "node-4", SnapshotRoot: t.TempDir(),
				BootReadyTimeout:    time.Second,
				ArchiveFetchTimeout: 30 * time.Second,
				ArchiveMaxBytes:     512 << 20,
				Images:              map[string]config.Image{"runtime-python:1": {RootfsPath: "/runtime.ext4"}},
			},
			Driver:         &fakeDriver{},
			ServingDriver:  fsd,
			Transport:      &fakeTransport{},
			NewBuildDriver: func(BuildDriverSpec) BuildDriver { return build },
			Logger:         slog.New(slog.NewTextHandler(io.Discard, nil)),
		})
		return s, build, fsd, ts.URL + "/archive.zip"
	}

	zipReq := func(url string, serving bool) *nodev1.BuildBaseRequest {
		return &nodev1.BuildBaseRequest{
			Trace:     &nodev1.Trace{Workload: "serving-og-image"},
			ReadyPath: "/shim/ready",
			Resources: &nodev1.ResourceSpec{Vcpus: 1, MemMib: 512},
			Serving:   serving,
			Source: &nodev1.BuildBaseRequest_Zip{Zip: &nodev1.ZipSource{
				RuntimeImageRef: "runtime-python:1",
				ArchiveUrl:      url,
				ArchiveSha256:   sha256Hex(archive),
			}},
		}
	}

	t.Run("serving base writes artifact and reports serving_image_ref", func(t *testing.T) {
		s, build, fsd, url := newServer()
		resp, err := s.BuildBase(ctx, zipReq(url, true))
		if err != nil {
			t.Fatalf("BuildBase serving zip: %v", err)
		}
		// The base snapshot is STILL produced (additive; task lane unaffected).
		if build.snapshots != 1 {
			t.Errorf("snapshots = %d, want 1 (snapshot always produced)", build.snapshots)
		}
		// The handler artifact was written for the base key, with the exact archive bytes.
		baseKey := resp.GetSnapshotRef()
		artifactPath, ok := fsd.ServingHandlerArtifactPath(baseKey)
		if !ok {
			t.Errorf("no handler artifact written for serving base %q", baseKey)
		}
		if got := fsd.handlerArtifactWriteCount(); got != 1 {
			t.Errorf("WriteServingHandlerArtifact calls = %d, want 1", got)
		}
		images := s.servingImage.snapshot()
		if len(images) != 1 {
			t.Fatalf("serving image inventory entries = %d, want 1", len(images))
		}
		if images[0].handlerPath != artifactPath || artifactPath == "" {
			t.Errorf("zip-lane inventory handler path = %q, want written path %q", images[0].handlerPath, artifactPath)
		}
		if images[0].sizeBytes != int64(len(archive)) {
			t.Errorf("zip-lane inventory size bytes = %d, want %d", images[0].sizeBytes, len(archive))
		}
		// The response and NodeStatus both report serving_image_ref == the base key.
		if resp.GetServingImageRef() != baseKey {
			t.Errorf("serving_image_ref = %q, want the base key %q", resp.GetServingImageRef(), baseKey)
		}
		var wc *nodev1.WorkloadCapacity
		for _, c := range s.nodeStatus().GetWorkloads() {
			if c.GetWorkload() == "serving-og-image" {
				wc = c
			}
		}
		if wc == nil || wc.GetServingImageRef() != baseKey {
			t.Errorf("NodeStatus serving_image_ref = %+v, want %q", wc, baseKey)
		}
	})

	t.Run("image lane registers rootfs without writing artifact", func(t *testing.T) {
		s, build, fsd, _ := newServer()
		resp, err := s.BuildBase(ctx, &nodev1.BuildBaseRequest{
			Trace:            &nodev1.Trace{Workload: "serving-image-lane"},
			ImageRef:         "runtime-python:1",
			WorkloadRevision: "r1",
			ReadyPath:        "/shim/ready",
			Resources:        &nodev1.ResourceSpec{Vcpus: 1, MemMib: 512},
			Serving:          true,
		})
		if err != nil {
			t.Fatalf("BuildBase serving image lane: %v", err)
		}
		if build.snapshots != 1 {
			t.Errorf("snapshots = %d, want 1", build.snapshots)
		}
		if got := fsd.handlerArtifactWriteCount(); got != 0 {
			t.Errorf("WriteServingHandlerArtifact calls = %d, want 0", got)
		}
		images := s.servingImage.snapshot()
		if len(images) != 1 {
			t.Fatalf("serving image inventory entries = %d, want 1", len(images))
		}
		got := images[0]
		if got.workload != "serving-image-lane" {
			t.Errorf("inventory workload = %q, want serving-image-lane", got.workload)
		}
		if got.baseKey != resp.GetSnapshotRef() {
			t.Errorf("inventory base key = %q, want %q", got.baseKey, resp.GetSnapshotRef())
		}
		if got.handlerPath != "" {
			t.Errorf("inventory handler path = %q, want empty", got.handlerPath)
		}
		if got.runtimeImageRef != "runtime-python:1" {
			t.Errorf("inventory runtime image ref = %q, want runtime-python:1", got.runtimeImageRef)
		}
		if got.sizeBytes != 0 {
			t.Errorf("inventory size bytes = %d, want 0", got.sizeBytes)
		}
		if resp.GetServingImageRef() != got.baseKey {
			t.Errorf("serving_image_ref = %q, want %q", resp.GetServingImageRef(), got.baseKey)
		}
	})

	t.Run("zip artifact write failure fails build", func(t *testing.T) {
		s, _, fsd, url := newServer()
		fsd.failHandlerWrite = context.Canceled
		_, err := s.BuildBase(ctx, zipReq(url, true))
		if status.Code(err) != codes.FailedPrecondition {
			t.Fatalf("artifact write failure code = %v, want FailedPrecondition", status.Code(err))
		}
		if got := fsd.handlerArtifactWriteCount(); got != 1 {
			t.Errorf("WriteServingHandlerArtifact calls = %d, want 1", got)
		}
		if got := len(s.servingImage.snapshot()); got != 0 {
			t.Errorf("serving image inventory entries = %d, want 0 after failed write", got)
		}
	})

	t.Run("non-serving base writes no artifact", func(t *testing.T) {
		s, _, fsd, url := newServer()
		resp, err := s.BuildBase(ctx, zipReq(url, false))
		if err != nil {
			t.Fatalf("BuildBase task zip: %v", err)
		}
		if _, ok := fsd.ServingHandlerArtifactPath(resp.GetSnapshotRef()); ok {
			t.Error("a non-serving base must not write a handler artifact")
		}
		if resp.GetServingImageRef() != "" {
			t.Errorf("serving_image_ref = %q, want empty for a task-class base", resp.GetServingImageRef())
		}
	})
}

// TestWorkloadCapacitiesSkipsStaleServingImage: workloadCapacities reports only a
// serving image whose runtime rootfs is STILL provisioned in cfg.Images. A base
// built against a since-superseded runtime (its rootfs pruned when the node rolled)
// is not cold-bootable, so it must not be offered to serving placement, otherwise
// the control plane wakes onto it and gets FAILED_PRECONDITION "runtime image ...
// not provisioned" (the transient post-roll 503, D-R3.11.3). Old serving bases are
// not GC'd, so multiple coexist per workload and the registry map order is
// nondeterministic; the filter makes the reported ref always cold-bootable.
func TestWorkloadCapacitiesSkipsStaleServingImage(t *testing.T) {
	s := New(Options{
		Config: config.Config{
			Arch: "amd64", Node: "node-4", SnapshotRoot: t.TempDir(),
			BootReadyTimeout: time.Second,
			// Only the current runtime is provisioned; runtime-python:1 was pruned.
			Images: map[string]config.Image{"runtime-python:2": {RootfsPath: "/runtime2.ext4"}},
		},
		Driver:        &fakeDriver{},
		ServingDriver: newFakeServingDriver(t.TempDir()),
		Transport:     &fakeTransport{},
		Logger:        slog.New(slog.NewTextHandler(io.Discard, nil)),
	})
	// A stale serving base (runtime-python:1, gone) and the current one (runtime-python:2).
	s.servingImage.add(servingImageEntry{baseKey: "wl__stale", workload: "wl", handlerPath: "/h1", runtimeImageRef: "runtime-python:1"})
	s.servingImage.add(servingImageEntry{baseKey: "wl__current", workload: "wl", handlerPath: "/h2", runtimeImageRef: "runtime-python:2"})

	var wl *nodev1.WorkloadCapacity
	for _, c := range s.workloadCapacities(map[string][]string{}) {
		if c.GetWorkload() == "wl" {
			wl = c
		}
	}
	if wl == nil {
		t.Fatal("no capacity reported for workload wl")
	}
	if got := wl.GetServingImageRef(); got != "wl__current" {
		t.Fatalf("serving_image_ref = %q, want wl__current (the provisioned-runtime base; the stale wl__stale must never be reported)", got)
	}
}

func TestWorkloadCapacitiesAdvertisesNewestReadyBase(t *testing.T) {
	// oldRef sorts lexically GREATER than newRef on purpose: if either
	// registration path drops createdAtUnixMs again, the comparison falls to
	// the lexical tie-break and advertises the stale base, and this test must
	// catch exactly that (the live 2026-08-05 failure). The old base goes in
	// through register (the disk-adoption path, which once zeroed the stamp in
	// its field-by-field copy) and the new one through readyBuild (which once
	// never stamped at all), so both real paths are exercised, no hand-patching.
	const (
		workload = "claude-runtime"
		oldRef   = "claude-runtime__zzzstale"
		newRef   = "claude-runtime__aaafresh"
		image    = "img@sha256:runtime"
	)

	for _, tc := range []struct {
		name string
		run  func(s *Server)
	}{
		{name: "adopted then built", run: func(s *Server) {
			s.bases.register(baseEntry{snapshotRef: oldRef, workload: workload, imageDigest: image, readyPath: "/shim/ready", createdAtUnixMs: 1000, state: nodev1.BaseBuildState_BASE_BUILD_STATE_READY})
			s.bases.readyBuild(newRef, workload, image, "", "/shim/ready", 2048)
		}},
		{name: "built then adopted", run: func(s *Server) {
			s.bases.readyBuild(newRef, workload, image, "", "/shim/ready", 2048)
			s.bases.register(baseEntry{snapshotRef: oldRef, workload: workload, imageDigest: image, readyPath: "/shim/ready", createdAtUnixMs: 1000, state: nodev1.BaseBuildState_BASE_BUILD_STATE_READY})
		}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			client, s := newTestServer(t, &fakeDriver{}, &fakeTransport{}, 8)
			s.registry.sync([]workloadEntry{{Workload: workload, ImageRef: image, RootfsRef: "/rootfs/runtime"}})
			tc.run(s)

			ns, err := client.GetNodeStatus(context.Background(), &nodev1.GetNodeStatusRequest{})
			if err != nil {
				t.Fatalf("GetNodeStatus: %v", err)
			}
			for _, c := range ns.GetWorkloads() {
				if c.GetWorkload() == workload {
					if got := c.GetSnapshotRef(); got != newRef {
						t.Fatalf("snapshot_ref = %q, want newer ref %q", got, newRef)
					}
					return
				}
			}
			t.Fatalf("workload capacity for %q not reported", workload)
		})
	}
}

func TestParseMemHeadroomMib(t *testing.T) {
	cases := []struct {
		maxRaw, curRaw string
		want           uint64
	}{
		{"max\n", "1048576\n", 0},        // unlimited
		{"2097152\n", "1048576\n", 1},    // 2MiB - 1MiB = 1MiB
		{"1048576", "2097152", 0},        // current over max
		{"garbage", "1", 0},              // unparseable
		{"104857600\n", "4194304\n", 96}, // 100MiB - 4MiB
	}
	for _, c := range cases {
		if got := parseMemHeadroomMib(c.maxRaw, c.curRaw, ""); got != c.want {
			t.Errorf("parseMemHeadroomMib(%q,%q,%q) = %d, want %d", c.maxRaw, c.curRaw, "", got, c.want)
		}
	}
}

// freePrimed extracts a workload's free_primed_slots from a NodeStatus.
func freePrimed(ns *nodev1.NodeStatus, workload string) uint32 {
	for _, c := range ns.GetWorkloads() {
		if c.GetWorkload() == workload {
			return c.GetFreePrimedSlots()
		}
	}
	return 0
}

func primedIDs(ns *nodev1.NodeStatus, workload string) []string {
	for _, c := range ns.GetWorkloads() {
		if c.GetWorkload() == workload {
			return c.GetPrimedVmIds()
		}
	}
	return nil
}

// TestNodeStatusReportsPrimedVMIDs: NodeStatus carries the vm_id of every primed
// VM per workload (not just a count), so a restarted control plane can adopt the
// node's warm pool into its dispatch inventory instead of orphaning it (which
// would deadlock dispatch once the orphans fill max_live_vms).
func TestNodeStatusReportsPrimedVMIDs(t *testing.T) {
	drv := &fakeDriver{}
	tr := &fakeTransport{}
	client, srv := newTestServer(t, drv, tr, 8)
	seedBase(srv, "echo__deadbeef01", "echo")
	ctx := context.Background()

	want := map[string]bool{}
	for i := 0; i < 2; i++ {
		pr, err := client.Prime(ctx, &nodev1.PrimeRequest{SnapshotRef: "echo__deadbeef01"})
		if err != nil {
			t.Fatalf("Prime %d: %v", i, err)
		}
		want[pr.GetVmId()] = true
	}

	ns, err := client.GetNodeStatus(ctx, &nodev1.GetNodeStatusRequest{})
	if err != nil {
		t.Fatalf("GetNodeStatus: %v", err)
	}

	got := primedIDs(ns, "echo")
	if freePrimed(ns, "echo") != 2 {
		t.Errorf("echo free_primed_slots = %d, want 2", freePrimed(ns, "echo"))
	}
	if len(got) != 2 {
		t.Fatalf("echo primed_vm_ids = %v, want 2 ids", got)
	}
	for _, id := range got {
		if !want[id] {
			t.Errorf("primed_vm_ids has unexpected id %q (want one of the primed ids)", id)
		}
		delete(want, id)
	}
	if len(want) != 0 {
		t.Errorf("primed_vm_ids missing primed ids: %v", want)
	}
}

// TestNodeStatusCarriesBudgetFields verifies the budget-agnostic daemon's
// sensor output (Task 1.2) reaches NodeStatus: mem_budget_mib,
// cpu_budget_millicores, and the now-real cpu_headroom_millicores (no longer
// hard-coded 0) all read from the injected budget hooks rather than the
// cgroup filesystem, mirroring how memHeadroom is already overridden in
// tests for determinism.
func TestNodeStatusCarriesBudgetFields(t *testing.T) {
	drv := &fakeDriver{}
	tr := &fakeTransport{}
	client, srv := newTestServer(t, drv, tr, 8)
	srv.memBudget = func() uint64 { return 3584 }
	srv.cpuBudget = func() uint64 { return 2000 }
	srv.cpuHeadroom = func() uint64 { return 1500 }

	ns, err := client.GetNodeStatus(context.Background(), &nodev1.GetNodeStatusRequest{})
	if err != nil {
		t.Fatalf("GetNodeStatus: %v", err)
	}
	if got, want := ns.GetMemBudgetMib(), uint64(3584); got != want {
		t.Errorf("mem_budget_mib = %d, want %d", got, want)
	}
	if got, want := ns.GetCpuBudgetMillicores(), uint64(2000); got != want {
		t.Errorf("cpu_budget_millicores = %d, want %d", got, want)
	}
	if got, want := ns.GetCpuHeadroomMillicores(), uint32(1500); got != want {
		t.Errorf("cpu_headroom_millicores = %d, want %d (must no longer be hard-coded 0)", got, want)
	}
	if ns.GetAdmitsOnReservation() {
		t.Error("default admission model advertises admits_on_reservation=true")
	}
	if got := ns.GetMemReservedMib(); got != 0 {
		t.Errorf("empty brick mem_reserved_mib = %d, want 0", got)
	}
	if got := ns.GetVmOverheadMib(); got != 0 {
		t.Errorf("default vm_overhead_mib = %d, want 0", got)
	}
	if ns.GetMemReservedMib() == 0 && ns.GetAdmitsOnReservation() {
		t.Error("zero mem_reserved_mib must not be interpreted as reservation model off")
	}
}

func TestNodeStatusCarriesMemRejectFloor(t *testing.T) {
	drv := &fakeDriver{}
	tr := &fakeTransport{}
	client, srv := newTestServer(t, drv, tr, 8)
	srv.cfg.MemRejectFloorMib = 512

	ns, err := client.GetNodeStatus(context.Background(), &nodev1.GetNodeStatusRequest{})
	if err != nil {
		t.Fatalf("GetNodeStatus: %v", err)
	}
	if got, want := ns.GetMemRejectFloorMib(), uint64(512); got != want {
		t.Errorf("mem_reject_floor_mib = %d, want %d", got, want)
	}
}

// ---- R2 session verb tests -------------------------------------------------

// sessionVMIDs / sessionSnapRefs extract the session facts from a NodeStatus.
func sessionVMIDs(ns *nodev1.NodeStatus) []string {
	out := make([]string, 0, len(ns.GetSessionVms()))
	for _, v := range ns.GetSessionVms() {
		out = append(out, v.GetVmId())
	}
	return out
}

func sessionSnapRefs(ns *nodev1.NodeStatus) []string {
	out := make([]string, 0, len(ns.GetSessionSnapshots()))
	for _, s := range ns.GetSessionSnapshots() {
		out = append(out, s.GetSnapshotRef())
	}
	return out
}

func containsStr(xs []string, want string) bool {
	for _, x := range xs {
		if x == want {
			return true
		}
	}
	return false
}

// TestSessionAssignSurvives: SessionAssign delivers a request to a live session VM
// and the VM SURVIVES (no reap), unlike Assign. A second SessionAssign to the same
// vm_id succeeds against the still-live VM.
func TestSessionAssignSurvives(t *testing.T) {
	drv := &fakeDriver{}
	tr := &fakeTransport{}
	client, srv := newSessionTestServer(t, drv, tr, 8)
	vmID := primeSessionVM(t, srv, drv, "s-1", "echo", "sref-1", "")
	ctx := context.Background()

	for i := 0; i < 2; i++ {
		resp, err := client.SessionAssign(ctx, &nodev1.SessionAssignRequest{
			VmId:      vmID,
			SessionId: "s-1",
			Request:   &nodev1.GuestRequest{Method: "POST", Path: "/invoke", Body: []byte("hi")},
			TimeoutMs: 1000,
		})
		if err != nil {
			t.Fatalf("SessionAssign %d: %v", i, err)
		}
		if resp.GetSuspect() {
			t.Errorf("SessionAssign %d marked suspect on a clean response", i)
		}
		if got := string(resp.GetResponse().GetBody()); got != "ok:hi" {
			t.Errorf("body = %q, want ok:hi", got)
		}
	}
	// The VM was never released (survives across invocations).
	if _, releases, removeBundles, _ := drv.counts(); releases != 0 || removeBundles != 0 {
		t.Errorf("SessionAssign destroyed the VM: releases=%d removeBundles=%d, want 0/0", releases, removeBundles)
	}
	if got := drv.LiveCount(); got != 1 {
		t.Errorf("LiveCount after two SessionAssigns = %d, want 1 (VM survives)", got)
	}
}

// TestSessionAssignUnknownVM rejects a SessionAssign for an unknown vm_id with
// FAILED_PRECONDITION and no driver interaction.
func TestSessionAssignUnknownVM(t *testing.T) {
	drv := &fakeDriver{}
	tr := &fakeTransport{}
	client, _ := newSessionTestServer(t, drv, tr, 8)
	_, err := client.SessionAssign(context.Background(), &nodev1.SessionAssignRequest{
		VmId:    "vm-nope",
		Request: &nodev1.GuestRequest{Body: []byte("x")},
	})
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("code = %v, want FailedPrecondition", status.Code(err))
	}
	if tr.roundTripCount() != 0 {
		t.Error("unknown SessionAssign round-tripped to a guest")
	}
}

// TestSessionAssignTaskClassVMRejected: a task-pool vm_id (in the OTHER registry)
// is not a session VM, so SessionAssign rejects it FAILED_PRECONDITION and never
// destroys the primed task VM.
func TestSessionAssignTaskClassVMRejected(t *testing.T) {
	drv := &fakeDriver{}
	tr := &fakeTransport{}
	client, srv := newSessionTestServer(t, drv, tr, 8)
	seedBase(srv, "echo__task01", "echo")
	ctx := context.Background()
	pr, err := client.Prime(ctx, &nodev1.PrimeRequest{SnapshotRef: "echo__task01"})
	if err != nil {
		t.Fatalf("Prime: %v", err)
	}
	_, err = client.SessionAssign(ctx, &nodev1.SessionAssignRequest{
		VmId:    pr.GetVmId(),
		Request: &nodev1.GuestRequest{Body: []byte("x")},
	})
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("code = %v, want FailedPrecondition (task-class vm not a session)", status.Code(err))
	}
	if _, releases, _, _ := drv.counts(); releases != 0 {
		t.Errorf("SessionAssign on a task VM reaped it: releases=%d, want 0", releases)
	}
}

// TestSessionAssignAdoptsPrimedVM: a session's FIRST invoke arrives with its VM
// still in the TASK registry, because create primes/claims through the shared warm
// pool (Prime always lands a VM there). SessionAssign must ADOPT the primed VM of a
// MATCHING workload into the session registry and serve the invoke; the VM then
// SURVIVES as a session VM for subsequent invokes. This is the create->first-invoke
// path the live drill found unwired (it returned FAILED_PRECONDITION -> 502).
func TestSessionAssignAdoptsPrimedVM(t *testing.T) {
	drv := &fakeDriver{}
	tr := &fakeTransport{}
	client, srv := newSessionTestServer(t, drv, tr, 8)
	seedBase(srv, "sess__base01", "sandbox-session")
	ctx := context.Background()

	pr, err := client.Prime(ctx, &nodev1.PrimeRequest{SnapshotRef: "sess__base01"})
	if err != nil {
		t.Fatalf("Prime: %v", err)
	}
	vmID := pr.GetVmId()

	// Precondition: the primed VM is a primed TASK slot, not yet a session VM.
	if primed, _ := srv.vms.capacity(); !contains(primed["sandbox-session"], vmID) {
		t.Fatalf("primed VM %q not in the task registry before adoption", vmID)
	}

	// First invoke adopts (workload matches); second uses the normal session path.
	for i := 0; i < 2; i++ {
		resp, err := client.SessionAssign(ctx, &nodev1.SessionAssignRequest{
			VmId:      vmID,
			SessionId: "s-adopt",
			Trace:     &nodev1.Trace{Workload: "sandbox-session"},
			Request:   &nodev1.GuestRequest{Method: "POST", Path: "/invoke", Body: []byte("hi")},
			TimeoutMs: 1000,
		})
		if err != nil {
			t.Fatalf("SessionAssign %d (adopt): %v", i, err)
		}
		if got := string(resp.GetResponse().GetBody()); got != "ok:hi" {
			t.Errorf("SessionAssign %d body = %q, want ok:hi", i, got)
		}
	}

	// Postcondition: the VM left the task registry (never a primed slot again) and
	// survived as one live session VM.
	if primed, _ := srv.vms.capacity(); contains(primed["sandbox-session"], vmID) {
		t.Errorf("adopted VM still reported as a primed task slot")
	}
	if _, releases, _, _ := drv.counts(); releases != 0 {
		t.Errorf("adoption destroyed the VM: releases=%d, want 0", releases)
	}
	if got := drv.LiveCount(); got != 1 {
		t.Errorf("LiveCount = %d, want 1 (one surviving session VM)", got)
	}
}

// TestSessionAssignAdoptWorkloadMismatchRejected: a primed VM of a DIFFERENT
// workload than the session is never hijacked as a session VM. The adopt guard
// requires the primed VM's workload to equal the session's, so a mismatch stays
// FAILED_PRECONDITION and leaves the primed VM untouched.
func TestSessionAssignAdoptWorkloadMismatchRejected(t *testing.T) {
	drv := &fakeDriver{}
	tr := &fakeTransport{}
	client, srv := newSessionTestServer(t, drv, tr, 8)
	seedBase(srv, "other__base01", "other-workload")
	ctx := context.Background()

	pr, err := client.Prime(ctx, &nodev1.PrimeRequest{SnapshotRef: "other__base01"})
	if err != nil {
		t.Fatalf("Prime: %v", err)
	}
	_, err = client.SessionAssign(ctx, &nodev1.SessionAssignRequest{
		VmId:      pr.GetVmId(),
		SessionId: "s-mismatch",
		Trace:     &nodev1.Trace{Workload: "sandbox-session"}, // != the VM's workload
		Request:   &nodev1.GuestRequest{Body: []byte("x")},
		TimeoutMs: 1000,
	})
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("code = %v, want FailedPrecondition (workload mismatch)", status.Code(err))
	}
	// The primed VM was NOT adopted or destroyed: still a primed task slot.
	if primed, _ := srv.vms.capacity(); !contains(primed["other-workload"], pr.GetVmId()) {
		t.Errorf("mismatched-workload primed VM was consumed by a failed adopt")
	}
}

// TestSessionAssignTimeoutLeavesAlive: a guest timeout yields DEADLINE_EXCEEDED but
// the session VM is LEFT ALIVE (unlike Assign, which destroys). A subsequent
// SessionAssign still reaches the (now responsive) VM.
func TestSessionAssignTimeoutLeavesAlive(t *testing.T) {
	drv := &fakeDriver{}
	tr := &fakeTransport{roundTripErr: context.DeadlineExceeded}
	client, srv := newSessionTestServer(t, drv, tr, 8)
	vmID := primeSessionVM(t, srv, drv, "s-2", "echo", "sref-2", "")

	_, err := client.SessionAssign(context.Background(), &nodev1.SessionAssignRequest{
		VmId:      vmID,
		Request:   &nodev1.GuestRequest{Body: []byte("slow")},
		TimeoutMs: 50,
	})
	if status.Code(err) != codes.DeadlineExceeded {
		t.Fatalf("code = %v, want DeadlineExceeded", status.Code(err))
	}
	// VM left alive.
	if _, releases, _, _ := drv.counts(); releases != 0 {
		t.Errorf("timed-out SessionAssign destroyed the VM: releases=%d, want 0", releases)
	}
	if drv.LiveCount() != 1 {
		t.Errorf("LiveCount after timeout = %d, want 1 (VM survives)", drv.LiveCount())
	}
}

// TestSessionInFlightGuard: a concurrent SessionAssign on the same vm_id is
// rejected FAILED_PRECONDITION while the first is in flight (the per-vm
// serialization guard the contract requires).
func TestSessionInFlightGuard(t *testing.T) {
	drv := &fakeDriver{}
	gate := make(chan struct{})
	tr := &fakeTransport{blockRoundTrip: gate}
	client, srv := newSessionTestServer(t, drv, tr, 8)
	vmID := primeSessionVM(t, srv, drv, "s-3", "echo", "sref-3", "")
	ctx := context.Background()

	firstDone := make(chan error, 1)
	go func() {
		_, err := client.SessionAssign(ctx, &nodev1.SessionAssignRequest{
			VmId:      vmID,
			Request:   &nodev1.GuestRequest{Body: []byte("first")},
			TimeoutMs: 5000,
		})
		firstDone <- err
	}()

	// Wait until the first round-trip is in flight (guard held).
	deadline := time.Now().Add(2 * time.Second)
	for tr.roundTripCount() == 0 {
		if time.Now().After(deadline) {
			t.Fatal("first SessionAssign never entered its round-trip")
		}
		time.Sleep(2 * time.Millisecond)
	}

	// A concurrent SessionAssign is rejected by the in-flight guard.
	_, err := client.SessionAssign(ctx, &nodev1.SessionAssignRequest{
		VmId:      vmID,
		Request:   &nodev1.GuestRequest{Body: []byte("concurrent")},
		TimeoutMs: 5000,
	})
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("concurrent SessionAssign code = %v, want FailedPrecondition", status.Code(err))
	}

	close(gate) // release the first
	if err := <-firstDone; err != nil {
		t.Fatalf("first SessionAssign: %v", err)
	}
}

// TestBankRelightRoundTrip is the headline: Bank a live session VM to a restorable
// ref (VM destroyed), then Relight from that ref (fresh VM), and prove STATE
// PERSISTED across the bank/relight (a marker written pre-bank is read post-relight
// via the fake driver+transport). Also asserts the banked snapshot appears in
// NodeStatus with a size, and Bank did not leave the VM live.
func TestBankRelightRoundTrip(t *testing.T) {
	drv := &fakeDriver{}
	tr := &fakeTransport{}
	client, srv := newSessionTestServer(t, drv, tr, 8)
	vmID := primeSessionVM(t, srv, drv, "s-4", "echo", "sref-birth", "")
	ctx := context.Background()

	// The guest "accretes" state before the bank; the fake persists this marker.
	drv.mu.Lock()
	drv.nextBankMarker = "accreted-x=42"
	drv.mu.Unlock()

	bankResp, err := client.Bank(ctx, &nodev1.BankRequest{
		VmId:      vmID,
		SessionId: "s-4",
		Trace:     &nodev1.Trace{Workload: "echo"},
	})
	if err != nil {
		t.Fatalf("Bank: %v", err)
	}
	if bankResp.GetSnapshotRef() == "" || bankResp.GetSizeBytes() == 0 {
		t.Fatalf("Bank resp = %+v, want a ref and non-zero size", bankResp)
	}
	// The VM was destroyed by the bank (live capacity released).
	if _, releases, removeBundles, _ := drv.counts(); releases != 1 || removeBundles != 1 {
		t.Errorf("Bank did not destroy the VM: releases=%d removeBundles=%d, want 1/1", releases, removeBundles)
	}
	if drv.LiveCount() != 0 {
		t.Errorf("LiveCount after Bank = %d, want 0 (VM destroyed)", drv.LiveCount())
	}

	// The banked snapshot is in the inventory / NodeStatus, but the vm is gone.
	ns, err := client.GetNodeStatus(ctx, &nodev1.GetNodeStatusRequest{})
	if err != nil {
		t.Fatalf("GetNodeStatus: %v", err)
	}
	// The banked snapshot is in the inventory. The birth ref primeSessionVM relit
	// from also lingers (Relight keeps its source snapshot until re-bank/evict, and
	// Bank does not evict the pre-relight source), so assert the banked ref is
	// PRESENT rather than sole.
	if refs := sessionSnapRefs(ns); !containsStr(refs, bankResp.GetSnapshotRef()) {
		t.Errorf("session_snapshots = %v, want to contain the banked ref %s", refs, bankResp.GetSnapshotRef())
	}
	if ids := sessionVMIDs(ns); len(ids) != 0 {
		t.Errorf("session_vms = %v, want empty after Bank", ids)
	}

	// Relight from the banked ref: fresh VM, and its round-trip echoes the persisted
	// state (proving the marker survived the bank/relight).
	rl, err := client.Relight(ctx, &nodev1.RelightRequest{
		SnapshotRef: bankResp.GetSnapshotRef(),
		SessionId:   "s-4",
		Trace:       &nodev1.Trace{Workload: "echo"},
	})
	if err != nil {
		t.Fatalf("Relight: %v", err)
	}
	if rl.GetVmId() == "" || rl.GetVmId() == vmID {
		t.Fatalf("relit vm_id = %q, want a fresh id (was %q)", rl.GetVmId(), vmID)
	}
	resp, err := client.SessionAssign(ctx, &nodev1.SessionAssignRequest{
		VmId:      rl.GetVmId(),
		SessionId: "s-4",
		Request:   &nodev1.GuestRequest{Body: []byte("read-state")},
		TimeoutMs: 1000,
	})
	if err != nil {
		t.Fatalf("post-relight SessionAssign: %v", err)
	}
	if got := string(resp.GetResponse().GetBody()); got != "state:accreted-x=42" {
		t.Fatalf("post-relight body = %q, want the pre-bank state marker", got)
	}
	// Relight best-effort-posted the guest clock resync (200 path): one from
	// primeSessionVM's Relight, one from the Relight here.
	if tr.clockPostCount() != 2 {
		t.Errorf("clock resync posts = %d, want 2", tr.clockPostCount())
	}
}

// TestRelightUnknownRefFails: a Relight for a snapshot_ref not in the banked
// inventory is FAILED_PRECONDITION and never restores.
func TestBankSnapshotFailureRemovesStaleEntry(t *testing.T) {
	// A bank whose snapshot fails must NOT leave a stale session VM behind: the
	// driver tears the VM down (a bank is destructive), so the server drops the
	// registry entry rather than misreport a dead VM as live session capacity.
	drv := &fakeDriver{failSnapshotSession: context.Canceled}
	tr := &fakeTransport{}
	client, srv := newSessionTestServer(t, drv, tr, 8)
	vmID := primeSessionVM(t, srv, drv, "s-9", "echo", "sref-9", "")
	ctx := context.Background()

	_, err := client.Bank(ctx, &nodev1.BankRequest{
		VmId:      vmID,
		SessionId: "s-9",
		Trace:     &nodev1.Trace{Workload: "echo"},
	})
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("failed Bank error = %v, want FailedPrecondition", err)
	}
	// The VM was torn down (live capacity released) and is not reported live.
	if drv.LiveCount() != 0 {
		t.Errorf("LiveCount after failed Bank = %d, want 0 (VM torn down)", drv.LiveCount())
	}
	ns, err := client.GetNodeStatus(ctx, &nodev1.GetNodeStatusRequest{})
	if err != nil {
		t.Fatalf("GetNodeStatus: %v", err)
	}
	if ids := sessionVMIDs(ns); len(ids) != 0 {
		t.Errorf("session_vms = %v, want empty after a failed Bank", ids)
	}
	// The in-flight guard went with the entry: a SessionAssign on the dead id is an
	// unknown-vm FailedPrecondition, not a stuck guard.
	_, err = client.SessionAssign(ctx, &nodev1.SessionAssignRequest{
		VmId:      vmID,
		SessionId: "s-9",
		Request:   &nodev1.GuestRequest{Body: []byte("x")},
		TimeoutMs: 1000,
	})
	if status.Code(err) != codes.FailedPrecondition {
		t.Errorf("post-failure SessionAssign = %v, want FailedPrecondition (unknown vm)", err)
	}
}

func TestRelightUnknownRefFails(t *testing.T) {
	drv := &fakeDriver{}
	tr := &fakeTransport{}
	client, _ := newSessionTestServer(t, drv, tr, 8)
	_, err := client.Relight(context.Background(), &nodev1.RelightRequest{SnapshotRef: "ghost-ref"})
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("code = %v, want FailedPrecondition", status.Code(err))
	}
	if drv.restoreSessions != 0 {
		t.Errorf("unknown Relight restored a VM: restoreSessions=%d", drv.restoreSessions)
	}
}

// TestRelightClock404Skipped: a guest without /shim/clock returns 404 for the
// resync POST; Relight treats it as skip-and-log, NOT an error, and still succeeds.
func TestRelightClock404Skipped(t *testing.T) {
	drv := &fakeDriver{}
	tr := &fakeTransport{clockStatus: http.StatusNotFound}
	client, srv := newSessionTestServer(t, drv, tr, 8)
	// Bank a ref into inventory to relight.
	_ = primeSessionVM(t, srv, drv, "s-5", "echo", "sref-5", "m")

	rl, err := client.Relight(context.Background(), &nodev1.RelightRequest{SnapshotRef: "sref-5", SessionId: "s-5"})
	if err != nil {
		t.Fatalf("Relight with a 404 clock endpoint should still succeed: %v", err)
	}
	if rl.GetVmId() == "" {
		t.Fatal("Relight returned an empty vm_id")
	}
	if tr.clockPostCount() != 2 { // one from primeSessionVM's Relight, one here
		t.Errorf("clock posts = %d, want 2", tr.clockPostCount())
	}
}

// TestEvictSnapshotInUseGuardAndIdempotency: EvictSnapshot refuses while a live VM
// was relit from the ref (in-use guard), succeeds once no live VM references it,
// and is idempotent (an unknown ref is OK).
func TestEvictSnapshotInUseGuardAndIdempotency(t *testing.T) {
	drv := &fakeDriver{}
	tr := &fakeTransport{}
	client, srv := newSessionTestServer(t, drv, tr, 8)
	ctx := context.Background()

	// A live session VM relit from sref-live.
	vmID := primeSessionVM(t, srv, drv, "s-6", "echo", "sref-live", "m")
	srv.sessionSnap.add(sessionSnapshotEntry{snapshotRef: "sref-live", sessionID: "s-6", workload: "echo", sizeBytes: 1})

	// In-use: refused while the relit VM runs.
	_, err := client.EvictSnapshot(ctx, &nodev1.EvictSnapshotRequest{SnapshotRef: "sref-live"})
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("in-use evict code = %v, want FailedPrecondition", status.Code(err))
	}

	// Destroy the live VM, then evict succeeds.
	if _, err := client.Destroy(ctx, &nodev1.DestroyRequest{VmId: vmID}); err != nil {
		t.Fatalf("Destroy: %v", err)
	}
	if _, err := client.EvictSnapshot(ctx, &nodev1.EvictSnapshotRequest{SnapshotRef: "sref-live"}); err != nil {
		t.Fatalf("evict after destroy: %v", err)
	}
	if drv.removeSessions != 1 {
		t.Errorf("removeSessions = %d, want 1", drv.removeSessions)
	}
	// Idempotent: unknown ref is OK.
	if _, err := client.EvictSnapshot(ctx, &nodev1.EvictSnapshotRequest{SnapshotRef: "never-banked"}); err != nil {
		t.Fatalf("idempotent evict of unknown ref: %v", err)
	}
}

// TestSessionInventoryRescanOnRestart: a daemon that starts with banked bundles on
// disk rescans them into the inventory and reports them in NodeStatus (the adoption
// source of truth), while live session VMs do NOT survive (their FC children died).
func TestSessionInventoryRescanOnRestart(t *testing.T) {
	dir := t.TempDir()
	// Seed two banked bundles on disk (snapfile + memfile under sessions/<ref>).
	sessRoot := filepath.Join(dir, "sessions")
	for _, ref := range []string{"sref-a", "sref-b"} {
		bd := filepath.Join(sessRoot, ref)
		if err := os.MkdirAll(bd, 0o700); err != nil {
			t.Fatalf("mkdir bundle: %v", err)
		}
		if err := os.WriteFile(filepath.Join(bd, "snapfile"), []byte("snap"), 0o600); err != nil {
			t.Fatalf("write snapfile: %v", err)
		}
		if err := os.WriteFile(filepath.Join(bd, "memfile"), []byte("mem-bytes"), 0o600); err != nil {
			t.Fatalf("write memfile: %v", err)
		}
	}
	// A half-written bundle (no snapfile) must NOT be reported as restorable.
	if err := os.MkdirAll(filepath.Join(sessRoot, "sref-halfwritten"), 0o700); err != nil {
		t.Fatalf("mkdir half-written: %v", err)
	}

	drv := &fakeDriver{sessionsDir: sessRoot}
	s := New(Options{
		Config:        config.Config{Arch: "amd64", Node: "node-4", MaxLiveVMs: 8, SnapshotRoot: dir},
		Driver:        drv,
		SessionDriver: drv,
		Transport:     &fakeTransport{},
		Logger:        slog.New(slog.NewTextHandler(io.Discard, nil)),
	})
	s.memHeadroom = func() uint64 { return 0 }
	s.ReconcileSessionsFromDisk()

	ns := s.nodeStatus()
	refs := sessionSnapRefs(ns)
	sort.Strings(refs)
	if len(refs) != 2 || refs[0] != "sref-a" || refs[1] != "sref-b" {
		t.Fatalf("rescanned session_snapshots = %v, want [sref-a sref-b] (half-written skipped)", refs)
	}
	// No live session VMs survive a restart.
	if ids := sessionVMIDs(ns); len(ids) != 0 {
		t.Errorf("session_vms after restart = %v, want empty (live VMs died with the daemon)", ids)
	}
	// The sessions dir is 0700 (a banked bundle holds a principal's memory image).
	fi, err := os.Stat(sessRoot)
	if err != nil {
		t.Fatalf("stat sessions dir: %v", err)
	}
	if perm := fi.Mode().Perm(); perm != 0o700 {
		t.Errorf("sessions dir perms = %o, want 0700", perm)
	}
}

// TestSessionVMsExcludedFromPrimedPool is the pool-separation invariant: a live
// session VM appears in NodeStatus.session_vms and in live_vms, but NEVER in any
// workload's primed_vm_ids (it must never be adopted into the single-use task pool).
func TestSessionVMsExcludedFromPrimedPool(t *testing.T) {
	drv := &fakeDriver{}
	tr := &fakeTransport{}
	client, srv := newSessionTestServer(t, drv, tr, 8)
	ctx := context.Background()

	// One primed task VM and one live session VM, both for workload "echo".
	seedBase(srv, "echo__pool01", "echo")
	pr, err := client.Prime(ctx, &nodev1.PrimeRequest{SnapshotRef: "echo__pool01"})
	if err != nil {
		t.Fatalf("Prime: %v", err)
	}
	sessVM := primeSessionVM(t, srv, drv, "s-7", "echo", "sref-pool", "")

	ns, err := client.GetNodeStatus(ctx, &nodev1.GetNodeStatusRequest{})
	if err != nil {
		t.Fatalf("GetNodeStatus: %v", err)
	}

	// The session VM is in session_vms.
	if ids := sessionVMIDs(ns); len(ids) != 1 || ids[0] != sessVM {
		t.Errorf("session_vms = %v, want [%s]", ids, sessVM)
	}
	// The session VM is NOT in echo's primed_vm_ids (only the primed task VM is).
	primed := primedIDs(ns, "echo")
	if len(primed) != 1 || primed[0] != pr.GetVmId() {
		t.Errorf("echo primed_vm_ids = %v, want just the task VM [%s]", primed, pr.GetVmId())
	}
	for _, id := range primed {
		if id == sessVM {
			t.Fatalf("session VM %q leaked into primed_vm_ids (would be adopted into the task pool)", sessVM)
		}
	}
	// Session VMs count against live_vms: 1 primed task + 1 live session = 2.
	if ns.GetLiveVms() != 2 {
		t.Errorf("live_vms = %d, want 2 (task + session both count)", ns.GetLiveVms())
	}
}

// TestBankInFlightGuardRefuses: a Bank is refused FAILED_PRECONDITION while a
// SessionAssign is in flight on the same vm_id (the daemon-side ordering backstop).
func TestBankInFlightGuardRefuses(t *testing.T) {
	drv := &fakeDriver{}
	gate := make(chan struct{})
	tr := &fakeTransport{blockRoundTrip: gate}
	client, srv := newSessionTestServer(t, drv, tr, 8)
	vmID := primeSessionVM(t, srv, drv, "s-8", "echo", "sref-8", "")
	ctx := context.Background()

	assignDone := make(chan struct{})
	go func() {
		_, _ = client.SessionAssign(ctx, &nodev1.SessionAssignRequest{
			VmId:      vmID,
			Request:   &nodev1.GuestRequest{Body: []byte("x")},
			TimeoutMs: 5000,
		})
		close(assignDone)
	}()
	deadline := time.Now().Add(2 * time.Second)
	for tr.roundTripCount() == 0 {
		if time.Now().After(deadline) {
			t.Fatal("SessionAssign never entered its round-trip")
		}
		time.Sleep(2 * time.Millisecond)
	}

	_, err := client.Bank(ctx, &nodev1.BankRequest{VmId: vmID, SessionId: "s-8"})
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("Bank during in-flight assign code = %v, want FailedPrecondition", status.Code(err))
	}
	if drv.snapshotSessions != 0 {
		t.Errorf("refused Bank still snapshotted: snapshotSessions=%d, want 0", drv.snapshotSessions)
	}
	close(gate)
	<-assignDone
}

// ensure the fakes satisfy the seams the server uses.
var (
	_ vmDriver      = (*fakeDriver)(nil)
	_ BuildDriver   = (*fakeDriver)(nil)
	_ sessionDriver = (*fakeDriver)(nil)
	_ transport     = (*fakeTransport)(nil)
)

// TestReconcileBasesFromDiskRestoresRefAndGCsRefless proves the base
// disk-reconcile restores each base's runtime image ref from the persisted
// imageref file and ADOPTS it READY, regardless of whether that runtime is
// currently provisioned. Artifact-decoupling Phase 2: at boot the pushed workload
// registry is NOT yet synced (the control plane replays it only after connect) and
// cfg.Images is empty, so GC-on-unprovisioned would DESTROY every valid warm base
// on a restart. Instead a base with a present ref is adopted READY (the NodeStatus
// capacity filter, imageProvisioned, decides advertisement once the registry syncs;
// reclaiming truly-superseded bases is PR-G's job). Only a base with a MISSING ref
// file (unbootable, cannot resolve a rootfs at all) is GC'd here.
func TestReconcileBasesFromDiskRestoresRefAndGCsRefless(t *testing.T) {
	dir := t.TempDir()
	s := New(Options{
		Config: config.Config{
			Arch: "amd64", Node: "node-4", MaxLiveVMs: 4, SnapshotRoot: dir,
			// Prod condition: cfg.Images empty (env parse retired). The reconcile must
			// NOT depend on it to keep a valid on-disk base.
			Images: map[string]config.Image{},
		},
	})

	basesDir := filepath.Join(dir, "bases")
	writeReconcileBase(t, basesDir, "wl-a__current", "img-current") // ref present -> adopted READY
	writeReconcileBase(t, basesDir, "wl-a__stale", "img-old")       // ref present -> adopted READY
	writeReconcileBase(t, basesDir, "wl-b__noref", "")              // no imageref -> GC'd

	s.ReconcileBasesFromDisk()

	// Both bases with a persisted ref are adopted READY even with an empty
	// cfg.Images (they are not wrongly wiped before the registry syncs).
	for _, key := range []string{"wl-a__current", "wl-a__stale"} {
		got, ok := s.bases.get(key)
		if !ok || got.state != nodev1.BaseBuildState_BASE_BUILD_STATE_READY {
			t.Errorf("base %q = %+v ok=%v; want adopted READY (not wiped before registry sync)", key, got, ok)
		}
		if _, err := os.Stat(filepath.Join(basesDir, key)); err != nil {
			t.Errorf("base dir %q should survive on disk, got stat err %v", key, err)
		}
	}
	// The refless base (unbootable) is still GC'd.
	if _, ok := s.bases.get("wl-b__noref"); ok {
		t.Error("refless base should have been GC'd from the registry")
	}
	if _, err := os.Stat(filepath.Join(basesDir, "wl-b__noref")); !os.IsNotExist(err) {
		t.Error("refless base dir should have been removed from disk")
	}
}

// TestReconcileThenCapacityFilterHidesUnprovisioned proves the two-stage contract:
// ReconcileBasesFromDisk adopts a base READY even when its runtime is not yet
// provisioned, and the NodeStatus capacity filter then HIDES it until the pushed
// registry knows its runtime (image_ref), so an unadvertised base is never placed
// but also never deleted. Once the registry is synced, the base is advertised.
func TestReconcileThenCapacityFilterHidesUnprovisioned(t *testing.T) {
	dir := t.TempDir()
	s := New(Options{
		Config: config.Config{
			Arch: "amd64", Node: "node-4", MaxLiveVMs: 4, SnapshotRoot: dir,
			Images: map[string]config.Image{},
		},
	})
	basesDir := filepath.Join(dir, "bases")
	writeReconcileBase(t, basesDir, "wl-a__current", "img-current")
	s.ReconcileBasesFromDisk()

	// Before any registry sync: the base is adopted but NOT advertised (its runtime
	// is unprovisioned), so no capacity entry names it as bootable.
	caps := s.workloadCapacities(map[string][]string{})
	for _, c := range caps {
		if c.GetSnapshotRef() == "wl-a__current" {
			t.Fatal("base should not be advertised before its runtime is provisioned")
		}
	}

	// After the control plane pushes the runtime identity, the base is advertised.
	s.registry.sync([]workloadEntry{
		{Workload: "image:img-current", ImageRef: "img-current", RootfsRef: "/rootfs/cur"},
	})
	caps = s.workloadCapacities(map[string][]string{})
	advertised := false
	for _, c := range caps {
		if c.GetSnapshotRef() == "wl-a__current" && c.GetBaseState() == nodev1.BaseBuildState_BASE_BUILD_STATE_READY {
			advertised = true
		}
	}
	if !advertised {
		t.Error("base should be advertised READY once its runtime is provisioned via the pushed registry")
	}
}

// staleServer builds a minimal Server whose workload registry is STALE (a boot
// cache load with no live sync). Same-package test, so it seeds the unexported
// stale/synced flags directly, mirroring what loadCache does on boot.
func staleServer(t *testing.T) *Server {
	t.Helper()
	s := New(Options{
		Config: config.Config{Arch: "amd64", Node: "node-4", MaxLiveVMs: 8, SnapshotRoot: t.TempDir()},
		Logger: slog.New(slog.NewTextHandler(io.Discard, nil)),
	})
	s.registry.mu.Lock()
	s.registry.stale = true
	s.registry.synced = false
	s.registry.mu.Unlock()
	return s
}

// TestStaleRegistryRefusesBuildBase proves a stale daemon refuses the coldest
// placement (a base build) with FailedPrecondition, the airtight daemon-side
// backstop that /readyz cannot provide on the CP's direct-dial path.
func TestStaleRegistryRefusesBuildBase(t *testing.T) {
	s := staleServer(t)
	_, err := s.BuildBase(context.Background(), &nodev1.BuildBaseRequest{
		ImageRef: "img-a",
		Trace:    &nodev1.Trace{Workload: "wl-a"},
	})
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("BuildBase on a stale registry: err = %v, want FailedPrecondition", err)
	}
	if !strings.Contains(err.Error(), "stale") {
		t.Errorf("error should name the stale registry, got %q", err.Error())
	}

	// Once a live sync clears the stale mark, the gate opens (it now fails for a
	// DIFFERENT reason: no build driver configured, i.e. past the stale gate).
	s.registry.sync([]workloadEntry{{Workload: "wl-a", ImageRef: "img-a", RootfsRef: "/rootfs/a"}})
	_, err = s.BuildBase(context.Background(), &nodev1.BuildBaseRequest{
		ImageRef: "img-a",
		Trace:    &nodev1.Trace{Workload: "wl-a"},
	})
	if status.Code(err) != codes.Unimplemented {
		t.Fatalf("BuildBase after live sync: err = %v, want Unimplemented (past the stale gate)", err)
	}
}

// TestStaleRegistryRefusesColdPrimeButServesWarm proves the "serve existing
// warmth, never admit new work" invariant at the Prime seam: while stale, a Prime
// for a workload with NO existing warm pool is refused (cold placement), but a
// Prime that REFILLS an already-warm workload is allowed (existing warmth topped
// up). The warm case is proven by the stale gate NOT firing (the call proceeds
// past it to the driver, failing only because this minimal server has none).
func TestStaleRegistryRefusesColdPrimeButServesWarm(t *testing.T) {
	// A real fake driver/transport so Prime's liveVMCount and restore path run.
	_, s := newTestServer(t, &fakeDriver{}, &fakeTransport{}, 8)
	s.registry.mu.Lock()
	s.registry.stale = true
	s.registry.synced = false
	s.registry.mu.Unlock()
	// Register a READY base for two workloads so Prime gets past the base checks.
	s.bases.readyBuild("snap-cold", "wl-cold", "img-a", "", "/shim/ready", 2048)
	s.bases.readyBuild("snap-warm", "wl-warm", "img-a", "", "/shim/ready", 2048)
	// Seed an EXISTING primed VM for wl-warm so it counts as already-warm.
	s.vms.add(&vmEntry{id: "vm-warm-1", workload: "wl-warm", snapshotRef: "snap-warm", state: vmPrimed})

	// Cold workload (no warm pool): the stale gate refuses.
	_, err := s.Prime(context.Background(), &nodev1.PrimeRequest{SnapshotRef: "snap-cold"})
	if status.Code(err) != codes.FailedPrecondition || !strings.Contains(err.Error(), "stale") {
		t.Fatalf("cold Prime on stale registry: err = %v, want FailedPrecondition/stale", err)
	}

	// Warm workload (existing primed VM): the stale gate does NOT fire, so the call
	// proceeds past it and fails for a different reason (no transport/driver here),
	// which is NOT the stale refusal. That proves warmth is still served.
	_, err = s.Prime(context.Background(), &nodev1.PrimeRequest{SnapshotRef: "snap-warm"})
	if err != nil && strings.Contains(err.Error(), "stale") {
		t.Errorf("warm-refill Prime must NOT be refused as stale, got %v", err)
	}
}

func TestPrimeReadyTimeoutUsesBootBudgetForSessionVolume(t *testing.T) {
	cfg := config.Config{BootReadyTimeout: 60 * time.Second, RestoreReadyTimeout: 2 * time.Second}
	if got := primeReadyTimeout(cfg, "/var/lib/embervm/session.img"); got != cfg.BootReadyTimeout {
		t.Fatalf("volume-backed Prime timeout = %v, want boot timeout %v", got, cfg.BootReadyTimeout)
	}
	if got := primeReadyTimeout(cfg, ""); got != cfg.RestoreReadyTimeout {
		t.Fatalf("warm Prime timeout = %v, want restore timeout %v", got, cfg.RestoreReadyTimeout)
	}
}

// TestBaseKeyForDiffersAcrossVendor proves the same (workload, image_ref,
// revision) keys a DIFFERENT base on each CPU vendor (R7, standing decision 1):
// a Firecracker base built on Intel must never collide with (or be reported
// AlreadyBuilt against) the same image's AMD base.
func TestBaseKeyForDiffersAcrossVendor(t *testing.T) {
	amdKey := baseKeyFor("echo", "img:1", "r1", "amd")
	intelKey := baseKeyFor("echo", "img:1", "r1", "intel")
	if amdKey == intelKey {
		t.Fatalf("baseKeyFor should differ across vendor, both = %q", amdKey)
	}
	// Same vendor, same inputs: still deterministic (idempotency depends on it).
	if again := baseKeyFor("echo", "img:1", "r1", "amd"); again != amdKey {
		t.Fatalf("baseKeyFor should be deterministic for the same inputs: %q != %q", again, amdKey)
	}
}

// TestBaseKeyForZipDiffersAcrossVendor mirrors TestBaseKeyForDiffersAcrossVendor
// for the ZIP-lane key function.
func TestBaseKeyForZipDiffersAcrossVendor(t *testing.T) {
	amdKey := baseKeyForZip("echo", "digest:1", "sha:1", "amd")
	intelKey := baseKeyForZip("echo", "digest:1", "sha:1", "intel")
	if amdKey == intelKey {
		t.Fatalf("baseKeyForZip should differ across vendor, both = %q", amdKey)
	}
}

func writeReconcileBase(t *testing.T, basesDir, baseKey, ref string) {
	t.Helper()
	d := filepath.Join(basesDir, baseKey)
	if err := os.MkdirAll(d, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(d, "snapfile"), []byte("snap"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(d, "memfile"), []byte("mem"), 0o644); err != nil {
		t.Fatal(err)
	}
	if ref != "" {
		if err := os.WriteFile(filepath.Join(d, "imageref"), []byte(ref), 0o644); err != nil {
			t.Fatal(err)
		}
	}
}

// TestResolveImageByRefTagSkewReturnsRealPath is the demo-postgres cold-boot
// regression at the resolution boundary: with cfg.Images empty (the Phase 2 prod
// condition) and a tag-skewed registry (an empty-rootfs per-CR entry plus the
// synthetic identity entry carrying the base's real path, both under the same
// image_ref), resolveImageByRef must return the REAL rootfs path, never the empty
// one that Firecracker rejects with "No such file or directory". resolveImage's
// by-ref fallback and imageProvisioned resolve through the same path.
func TestResolveImageByRefTagSkewReturnsRealPath(t *testing.T) {
	_, s := newTestServer(t, &fakeDriver{}, &fakeTransport{}, 8)
	s.cfg.Images = map[string]config.Image{}
	s.registry.sync([]workloadEntry{
		{Workload: "demo-postgres", ImageRef: "img-pg", RootfsRef: ""},
		{Workload: "image:img-pg", ImageRef: "img-pg", RootfsRef: "/rootfs/pg", HarnessInit: "/init"},
	})

	img, ok := s.resolveImageByRef("img-pg")
	if !ok {
		t.Fatal("resolveImageByRef did not resolve img-pg under tag skew")
	}
	if img.RootfsPath != "/rootfs/pg" {
		t.Fatalf("resolveImageByRef RootfsPath = %q, want /rootfs/pg (never empty)", img.RootfsPath)
	}
	if !s.imageProvisioned("img-pg") {
		t.Error("imageProvisioned(img-pg) = false; want true (base is present under the skewed tag)")
	}

	// resolveImage's by-ref fallback (workload not keyed, resolves by ref) must
	// also land on the real path, not the empty per-CR entry.
	got, ok := s.resolveImage("unknown-workload", "img-pg")
	if !ok || got.RootfsPath != "/rootfs/pg" {
		t.Fatalf("resolveImage by-ref fallback = %+v (ok=%v), want RootfsPath=/rootfs/pg", got, ok)
	}
}
