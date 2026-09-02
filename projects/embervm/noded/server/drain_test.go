package server

import (
	"bytes"
	"context"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	nodev1 "github.com/jomcgi/homelab/projects/embervm/proto/embervm/node/v1"

	"github.com/jomcgi/homelab/projects/embervm/noded/config"
)

// blockingTransport blocks WaitReady until the build context is cancelled,
// standing in for a BuildBase cold boot still waiting on guest readiness when a
// drain fires. It closes started once, so the test can wait until the build has
// actually reached the readiness wait (and is therefore registered as in-flight)
// before it drains.
type blockingTransport struct {
	started chan struct{}
}

func (b *blockingTransport) WaitReady(ctx context.Context, _, _ string) error {
	close(b.started)
	<-ctx.Done()
	return ctx.Err()
}
func (b *blockingTransport) Prime(context.Context, string) error           { return nil }
func (b *blockingTransport) Hydrate(context.Context, string, []byte) error { return nil }
func (b *blockingTransport) SetClock(context.Context, string, int64) error { return nil }
func (b *blockingTransport) RoundTrip(context.Context, string, *http.Request) (*http.Response, error) {
	return nil, nil
}

// TestSetDrainingPublishesDeadline proves SetDraining flips the draining flag,
// records the deadline, and surfaces both on NodeStatus (the fact the control
// plane reads off WatchNode to bound its force-bank pass).
func TestSetDrainingPublishesDeadline(t *testing.T) {
	drv := &fakeDriver{}
	_, s := newTestServer(t, drv, &fakeTransport{}, 10)

	if s.isDraining() {
		t.Fatal("server should not start draining")
	}
	if got := s.nodeStatus().GetDrainDeadlineUnixMs(); got != 0 {
		t.Fatalf("drain deadline should be 0 before drain, got %d", got)
	}

	deadline := time.Now().Add(120 * time.Second)
	s.SetDraining(deadline)

	if !s.isDraining() {
		t.Fatal("server should be draining after SetDraining")
	}
	ns := s.nodeStatus()
	if !ns.GetDraining() {
		t.Fatal("NodeStatus.draining should be true")
	}
	if got, want := ns.GetDrainDeadlineUnixMs(), deadline.UnixMilli(); got != want {
		t.Fatalf("NodeStatus.drain_deadline_unix_ms = %d, want %d", got, want)
	}
}

func TestPreemptionNoticeStartsShortDrain(t *testing.T) {
	_, s := newTestServer(t, &fakeDriver{}, &fakeTransport{}, 10)
	s.cfg.PreemptionNoticeEnabled = true
	s.cfg.PreemptionDrainTimeout = 2 * time.Second
	s.cfg.DrainTimeout = 110 * time.Second

	metadata := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if got := r.Header.Get("Metadata-Flavor"); got != "Google" {
			t.Errorf("Metadata-Flavor = %q, want Google", got)
		}
		query := r.URL.Query()
		if got := query.Get("wait_for_change"); got != "true" {
			t.Errorf("wait_for_change = %q, want true", got)
		}
		if got := query.Get("last_etag"); got != "0" {
			t.Errorf("last_etag = %q, want 0", got)
		}
		if got := query.Get("timeout_sec"); got != "360" {
			t.Errorf("timeout_sec = %q, want 360", got)
		}
		w.Header().Set("ETag", "preempted-etag")
		_, _ = io.WriteString(w, "TRUE\n")
	}))
	defer metadata.Close()

	ctx, cancel := context.WithCancel(context.Background())
	before := time.Now()
	s.WatchPreemptionNotices(ctx, metadata.Client(), metadata.URL, cancel)
	after := time.Now()

	select {
	case <-ctx.Done():
	default:
		t.Fatal("preemption notice did not request shutdown")
	}
	deadline := time.UnixMilli(s.drainDeadline())
	if deadline.Before(before.Add(s.cfg.PreemptionDrainTimeout-time.Millisecond)) ||
		deadline.After(after.Add(s.cfg.PreemptionDrainTimeout)) {
		t.Fatalf("drain deadline = %s, want now + %s", deadline, s.cfg.PreemptionDrainTimeout)
	}
	if !deadline.Before(before.Add(s.cfg.DrainTimeout)) {
		t.Fatalf("preemption used graceful drain deadline: %s", deadline)
	}
}

func TestPreemptionWatcherErrorDisablesAfterOneLog(t *testing.T) {
	var requests atomic.Int32
	metadata := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		requests.Add(1)
		http.Error(w, "unavailable", http.StatusServiceUnavailable)
	}))
	defer metadata.Close()

	var logs bytes.Buffer
	s := New(Options{
		Config: config.Config{
			PreemptionNoticeEnabled: true,
			PreemptionDrainTimeout:  20 * time.Second,
		},
		Driver:    &fakeDriver{},
		Transport: &fakeTransport{},
		Logger:    slog.New(slog.NewTextHandler(&logs, nil)),
	})
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	s.WatchPreemptionNotices(ctx, metadata.Client(), metadata.URL, cancel)
	if got := requests.Load(); got != 1 {
		t.Fatalf("metadata requests = %d, want 1", got)
	}
	// Exactly one line, at INFO: a metadata server that never answered means this
	// is not GCP, which is a fact about the environment rather than a fault. The
	// assertion is on the count and the level, not on the wording, so renaming a
	// message does not fail a test about behaviour.
	if got := strings.Count(logs.String(), "level=INFO"); got != 1 {
		t.Fatalf("info log count = %d, want 1; logs: %s", got, logs.String())
	}
	if strings.Contains(logs.String(), "level=WARN") {
		t.Fatalf("a server that never answered must not warn; logs: %s", logs.String())
	}
	if s.isDraining() {
		t.Fatal("metadata error should not start draining")
	}
}

func TestPreemptionWatcherNonTrueKeepsWatching(t *testing.T) {
	var requests atomic.Int32
	secondStarted := make(chan struct{})
	releaseSecond := make(chan struct{})
	defer func() {
		select {
		case <-releaseSecond:
		default:
			close(releaseSecond)
		}
	}()
	metadata := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch requestNumber := requests.Add(1); requestNumber {
		case 1:
			w.Header().Set("ETag", "false-etag")
			_, _ = io.WriteString(w, "FALSE")
		case 2:
			if got := r.URL.Query().Get("last_etag"); got != "false-etag" {
				t.Errorf("rotated last_etag = %q, want false-etag", got)
			}
			close(secondStarted)
			<-releaseSecond
			w.Header().Set("ETag", "true-etag")
			_, _ = io.WriteString(w, "TRUE")
		default:
			t.Errorf("unexpected metadata request %d", requestNumber)
		}
	}))
	defer metadata.Close()

	_, s := newTestServer(t, &fakeDriver{}, &fakeTransport{}, 10)
	s.cfg.PreemptionNoticeEnabled = true
	s.cfg.PreemptionDrainTimeout = time.Second
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan struct{})
	go func() {
		s.WatchPreemptionNotices(ctx, metadata.Client(), metadata.URL, cancel)
		close(done)
	}()

	select {
	case <-secondStarted:
	case <-time.After(5 * time.Second):
		t.Fatal("watcher did not continue after FALSE response")
	}
	if s.isDraining() {
		t.Fatal("non-TRUE metadata response started draining")
	}
	close(releaseSecond)
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("watcher did not finish after TRUE response")
	}
	if !s.isDraining() {
		t.Fatal("watcher did not drain after subsequent TRUE response")
	}
}

func TestSignalAfterPreemptionDoesNotExtendDeadline(t *testing.T) {
	_, s := newTestServer(t, &fakeDriver{}, &fakeTransport{}, 10)
	preemptionDeadline := time.Now().Add(20 * time.Second)
	effectivePreemptionDeadline := s.SetDraining(preemptionDeadline)

	effectiveSignalDeadline := s.SetDraining(time.Now().Add(110 * time.Second))
	if got, want := effectiveSignalDeadline.UnixMilli(), effectivePreemptionDeadline.UnixMilli(); got != want {
		t.Fatalf("deadline after SIGTERM = %d, want earlier preemption deadline %d", got, want)
	}
	if got, want := s.drainDeadline(), preemptionDeadline.UnixMilli(); got != want {
		t.Fatalf("published deadline after SIGTERM = %d, want %d", got, want)
	}
}

func TestPreemptionWatcherDisabledDoesNotRun(t *testing.T) {
	var requests atomic.Int32
	metadata := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		requests.Add(1)
		w.Header().Set("ETag", "unused")
		_, _ = io.WriteString(w, "TRUE")
	}))
	defer metadata.Close()

	_, s := newTestServer(t, &fakeDriver{}, &fakeTransport{}, 10)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	s.WatchPreemptionNotices(ctx, metadata.Client(), metadata.URL, cancel)

	if got := requests.Load(); got != 0 {
		t.Fatalf("metadata requests = %d, want 0 when disabled", got)
	}
	if s.isDraining() {
		t.Fatal("disabled watcher started draining")
	}
}

// TestWaitForManagedDrainEmpty proves the wait returns immediately with zero
// remaining when there are no managed VMs to bank.
func TestWaitForManagedDrainEmpty(t *testing.T) {
	drv := &fakeDriver{}
	_, s := newTestServer(t, drv, &fakeTransport{}, 10)

	// A generous deadline: an empty registry must return well before it.
	remaining := s.WaitForManagedDrain(context.Background(), time.Now().Add(10*time.Second))
	if remaining != 0 {
		t.Fatalf("empty drain should return 0, got %d", remaining)
	}
}

// TestWaitForManagedDrainEarlyExit proves the wait ends as soon as the control
// plane has banked (removed) the last managed VM, long before the deadline. The
// removal signals a NodeStatus change exactly as a real Bank/Stop does.
func TestWaitForManagedDrainEarlyExit(t *testing.T) {
	drv := &fakeDriver{}
	_, s := newTestServer(t, drv, &fakeTransport{}, 10)

	s.statefulVMs.add(&statefulEntry{vmID: "vm-st1", workload: "scratch-postgres"})
	if got := s.managedLiveVMCount(); got != 1 {
		t.Fatalf("managedLiveVMCount = %d, want 1", got)
	}

	// Simulate the control plane force-banking the VM shortly after drain begins.
	go func() {
		time.Sleep(20 * time.Millisecond)
		s.statefulVMs.remove("vm-st1")
		s.signalChange()
	}()

	start := time.Now()
	// A far deadline: the early exit must be driven by the empty registry, not it.
	remaining := s.WaitForManagedDrain(context.Background(), start.Add(30*time.Second))
	if remaining != 0 {
		t.Fatalf("early-exit drain should return 0, got %d", remaining)
	}
	if elapsed := time.Since(start); elapsed > 5*time.Second {
		t.Fatalf("early exit took %s; should have returned promptly on the bank signal", elapsed)
	}
}

// TestWaitForManagedDrainDeadline proves a VM that cannot bank in the window is
// left live at the deadline (its count is returned so the caller can log it; the
// pod then reaps it under spot semantics).
func TestWaitForManagedDrainDeadline(t *testing.T) {
	drv := &fakeDriver{}
	_, s := newTestServer(t, drv, &fakeTransport{}, 10)

	s.statefulVMs.add(&statefulEntry{vmID: "vm-st1", workload: "scratch-postgres"})

	// A short deadline with the VM never removed: the wait must return it as still
	// live once the deadline passes.
	remaining := s.WaitForManagedDrain(context.Background(), time.Now().Add(80*time.Millisecond))
	if remaining != 1 {
		t.Fatalf("deadline drain should return 1 straggler, got %d", remaining)
	}
}

// TestWaitForBuildsOrAbortDeadline proves an in-flight BuildBase that cannot
// finish inside the budget is cleanly ABORTED at the deadline: the build VM is
// torn down (Release), no snapshot is written, the base is left re-queueable (not
// READY), and the BuildBase RPC returns an error rather than orphaning a
// half-built guest. This is the Phase 0 preStop build drain (ADR embervm/009
// resolved-question 5: builds finish-or-abort last, they are reconstructible).
func TestWaitForBuildsOrAbortDeadline(t *testing.T) {
	build := &fakeDriver{}
	tr := &blockingTransport{started: make(chan struct{})}
	s := New(Options{
		Config: config.Config{
			Arch: "amd64", Node: "node-4", SnapshotRoot: t.TempDir(),
			BootReadyTimeout: 30 * time.Second,
			Images:           map[string]config.Image{"img:1": {RootfsPath: "/rootfs.ext4"}},
		},
		Driver:         &fakeDriver{},
		Transport:      tr,
		NewBuildDriver: func(BuildDriverSpec) BuildDriver { return build },
		Logger:         slog.New(slog.NewTextHandler(io.Discard, nil)),
	})

	// Kick off a build that will block in WaitReady (the guest never goes ready).
	buildErr := make(chan error, 1)
	go func() {
		_, err := s.BuildBase(context.Background(), &nodev1.BuildBaseRequest{
			Trace:            &nodev1.Trace{Workload: "echo"},
			ImageRef:         "img:1",
			WorkloadRevision: "r1",
			ReadyPath:        "/shim/ready",
			Resources:        &nodev1.ResourceSpec{Vcpus: 2, MemMib: 2048},
		})
		buildErr <- err
	}()

	// Wait until the build is in flight (has reached the readiness wait and is
	// therefore registered), so the drain has something to abort.
	select {
	case <-tr.started:
	case <-time.After(5 * time.Second):
		t.Fatal("build never reached the readiness wait")
	}

	// Drain with a deadline already in the past: the build cannot finish, so it
	// must be aborted immediately.
	s.SetDraining(time.Now())
	if aborted := s.WaitForBuildsOrAbort(time.Now()); aborted != 1 {
		t.Fatalf("WaitForBuildsOrAbort aborted = %d, want 1", aborted)
	}

	// The aborted BuildBase returns an error (no snapshot ref).
	select {
	case err := <-buildErr:
		if status.Code(err) != codes.FailedPrecondition {
			t.Fatalf("aborted build error = %v (code %v), want FailedPrecondition", err, status.Code(err))
		}
	case <-time.After(5 * time.Second):
		t.Fatal("aborted build did not return")
	}

	// The build VM was torn down and no snapshot was written.
	build.mu.Lock()
	releases, snapshots := build.releases, build.snapshots
	build.mu.Unlock()
	if releases != 1 {
		t.Errorf("build VM releases = %d, want 1 (torn down on abort)", releases)
	}
	if snapshots != 0 {
		t.Errorf("snapshots = %d, want 0 (no half-written snapshot)", snapshots)
	}

	// The base is left re-queueable: a later BuildBase can rebuild it, so its state
	// is not READY.
	baseKey := baseKeyFor("echo", "img:1", "r1", s.cfg.CpuVendor)
	if e, ok := s.bases.get(baseKey); ok && e.state == nodev1.BaseBuildState_BASE_BUILD_STATE_READY {
		t.Errorf("aborted base %q is READY; want re-queueable", baseKey)
	}
}

// A metadata server that answered and THEN failed is a fault, not an absence, so
// the watcher must keep watching. Going quiet here is how a watcher stops
// covering the event it exists for without anyone noticing.
func TestPreemptionWatcherRetriesAfterAnEarlierSuccess(t *testing.T) {
	var requests atomic.Int32
	metadata := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		switch requests.Add(1) {
		case 1:
			// One good answer, so the watcher learns the server is real.
			w.Header().Set("ETag", "etag-1")
			_, _ = w.Write([]byte("FALSE"))
		case 2:
			http.Error(w, "unavailable", http.StatusServiceUnavailable)
		default:
			// Preempted on the retry: proves the watcher was still watching.
			w.Header().Set("ETag", "etag-2")
			_, _ = w.Write([]byte("TRUE"))
		}
	}))
	defer metadata.Close()

	var logs bytes.Buffer
	s := New(Options{
		Config: config.Config{
			PreemptionNoticeEnabled: true,
			PreemptionDrainTimeout:  20 * time.Second,
		},
		Driver:    &fakeDriver{},
		Transport: &fakeTransport{},
		Logger:    slog.New(slog.NewTextHandler(&logs, nil)),
	})
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	s.WatchPreemptionNotices(ctx, metadata.Client(), metadata.URL, cancel)

	if got := requests.Load(); got < 3 {
		t.Fatalf("metadata requests = %d, want at least 3 (success, failure, retry)", got)
	}
	if !strings.Contains(logs.String(), "level=WARN") {
		t.Fatalf("a failure after a success must warn; logs: %s", logs.String())
	}
	if !s.isDraining() {
		t.Fatal("the notice seen on the retry should have started the drain")
	}
}
