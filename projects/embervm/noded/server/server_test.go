package server

import (
	"bytes"
	"context"
	"io"
	"log/slog"
	"net"
	"net/http"
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
	failClaim     error
}

func (f *fakeDriver) Claim(_ context.Context, spec substrate.ClaimSpec) (substrate.Handle, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.failClaim != nil {
		return substrate.Handle{}, f.failClaim
	}
	f.claims++
	f.live++
	return substrate.Handle{ThreadID: spec.ThreadID, ID: "vm-" + spec.ThreadID, Node: "node-4"}, nil
}

func (f *fakeDriver) Release(_ context.Context, _ substrate.Handle) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.releases++
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

func (f *fakeDriver) SnapshotBase(_ context.Context, _ substrate.Handle, baseKey string) (substrate.SnapshotRef, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.snapshots++
	return substrate.SnapshotRef{ID: baseKey, Base: true, SizeBytes: 4096}, nil
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
}

func (f *fakeTransport) WaitReady(_ context.Context, _, _ string) error { return f.waitReadyErr }
func (f *fakeTransport) Prime(_ context.Context, _ string) error        { return nil }

func (f *fakeTransport) RoundTrip(_ context.Context, _ string, req *http.Request) (*http.Response, error) {
	f.mu.Lock()
	f.roundTrips++
	rtErr := f.roundTripErr
	f.mu.Unlock()
	if rtErr != nil {
		return nil, rtErr
	}
	body, _ := io.ReadAll(req.Body)
	return &http.Response{
		StatusCode: http.StatusOK,
		Header:     http.Header{"X-Echo": []string{"1"}},
		Body:       io.NopCloser(bytes.NewReader([]byte("ok:" + string(body)))),
	}, nil
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
	s.memHeadroom = func() uint64 { return 0 } // deterministic, no cgroup read

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

func seedBase(s *Server, snapshotRef, workload string) {
	s.bases.readyBuild(snapshotRef, workload, "img@sha256:deadbeef", "/shim/ready", 2048)
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

// TestAssignUnknownVM rejects an Assign for a vm_id that was never primed, with
// no driver interaction at all.
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

// TestPrimeCapExhausted rejects a Prime that would exceed the node backstop cap.
func TestPrimeCapExhausted(t *testing.T) {
	drv := &fakeDriver{}
	client, srv := newTestServer(t, drv, &fakeTransport{}, 1)
	seedBase(srv, "echo__cap0001", "echo")
	ctx := context.Background()

	if _, err := client.Prime(ctx, &nodev1.PrimeRequest{SnapshotRef: "echo__cap0001"}); err != nil {
		t.Fatalf("first Prime: %v", err)
	}
	_, err := client.Prime(ctx, &nodev1.PrimeRequest{SnapshotRef: "echo__cap0001"})
	if status.Code(err) != codes.ResourceExhausted {
		t.Fatalf("second Prime code = %v, want ResourceExhausted", status.Code(err))
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
	if _, err := client.Destroy(ctx, &nodev1.DestroyRequest{VmId: pr.GetVmId()}); err != nil {
		t.Fatalf("Destroy: %v", err)
	}
	if _, releases, removeBundles, _ := drv.counts(); releases != 1 || removeBundles != 1 {
		t.Errorf("Destroy did not reap: releases=%d removeBundles=%d, want 1/1", releases, removeBundles)
	}
	// Idempotent: repeat Destroy is OK with no extra teardown.
	if _, err := client.Destroy(ctx, &nodev1.DestroyRequest{VmId: pr.GetVmId()}); err != nil {
		t.Fatalf("second Destroy: %v", err)
	}
	if _, releases, _, _ := drv.counts(); releases != 1 {
		t.Errorf("second Destroy re-reaped: releases=%d, want 1", releases)
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
		if got := parseMemHeadroomMib(c.maxRaw, c.curRaw); got != c.want {
			t.Errorf("parseMemHeadroomMib(%q,%q) = %d, want %d", c.maxRaw, c.curRaw, got, c.want)
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

// ensure the fakes satisfy the seams the server uses.
var (
	_ vmDriver    = (*fakeDriver)(nil)
	_ BuildDriver = (*fakeDriver)(nil)
	_ transport   = (*fakeTransport)(nil)
)
