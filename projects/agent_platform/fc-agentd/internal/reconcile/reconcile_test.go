package reconcile

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"sync"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/agent_platform/fc-agentd/internal/store"
	"github.com/jomcgi/homelab/projects/agent_platform/substrate"
)

// fakeRegistry is an in-memory store keyed by thread ID.
type fakeRegistry struct {
	mu      sync.Mutex
	threads map[string]*store.Thread
	removed []string
	listErr error
}

func newFakeRegistry(ts ...store.Thread) *fakeRegistry {
	m := map[string]*store.Thread{}
	for i := range ts {
		t := ts[i]
		m[t.ThreadID] = &t
	}
	return &fakeRegistry{threads: m}
}

func (f *fakeRegistry) ListThreadsForNode(_ context.Context, node string) ([]store.Thread, error) {
	return f.filter(func(t *store.Thread) bool { return t.Node == node })
}

func (f *fakeRegistry) ListByStateForNode(_ context.Context, node string, st substrate.State) ([]store.Thread, error) {
	if f.listErr != nil {
		return nil, f.listErr
	}
	return f.filter(func(t *store.Thread) bool { return t.Node == node && t.State == st })
}

func (f *fakeRegistry) ListWakeRequestedForNode(_ context.Context, node string) ([]store.Thread, error) {
	return f.filter(func(t *store.Thread) bool {
		return t.Node == node && t.State == substrate.StateIdle && !t.WakeRequestedAt.IsZero()
	})
}

func (f *fakeRegistry) ListIdleExpiredForNode(_ context.Context, node string) ([]store.Thread, error) {
	return f.filter(func(t *store.Thread) bool {
		return t.Node == node && t.State == substrate.StateIdle && t.WakeRequestedAt.IsZero() &&
			!t.LastActiveAt.IsZero() && time.Since(t.LastActiveAt) > time.Duration(t.TTLSeconds)*time.Second
	})
}

func (f *fakeRegistry) filter(pred func(*store.Thread) bool) ([]store.Thread, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	var out []store.Thread
	for _, t := range f.threads {
		if pred(t) {
			out = append(out, *t)
		}
	}
	return out, nil
}

func (f *fakeRegistry) SetState(_ context.Context, id string, st substrate.State) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if t, ok := f.threads[id]; ok {
		t.State = st
		t.LastActiveAt = time.Now()
	}
	return nil
}

func (f *fakeRegistry) SetThreadSnapshot(_ context.Context, id, ref string, sizeBytes int64) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if t, ok := f.threads[id]; ok {
		t.State = substrate.StateIdle
		t.ThreadSnapshotRef = ref
		t.SizeBytes = sizeBytes
		t.WakeRequestedAt = time.Time{}
		t.LastActiveAt = time.Now()
	}
	return nil
}

func (f *fakeRegistry) ClearWake(_ context.Context, id string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if t, ok := f.threads[id]; ok {
		t.State = substrate.StateRunning
		t.WakeRequestedAt = time.Time{}
	}
	return nil
}

func (f *fakeRegistry) Delete(_ context.Context, id string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	delete(f.threads, id)
	return nil
}

func (f *fakeRegistry) state(id string) substrate.State {
	f.mu.Lock()
	defer f.mu.Unlock()
	if t, ok := f.threads[id]; ok {
		return t.State
	}
	return "GONE"
}

// fakeExec is an in-memory executor + reclaimer.
type fakeExec struct {
	mu        sync.Mutex
	claims    int
	restores  int
	releases  int
	snapshots int
	removed   []string
	failNext  error
}

func (e *fakeExec) Claim(_ context.Context, spec substrate.ClaimSpec) (substrate.Handle, error) {
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.failNext != nil {
		err := e.failNext
		e.failNext = nil
		return substrate.Handle{}, err
	}
	e.claims++
	return substrate.Handle{ThreadID: spec.ThreadID, ID: "vm-" + spec.ThreadID, Node: "node-4"}, nil
}

func (e *fakeExec) Restore(_ context.Context, ref substrate.SnapshotRef) (substrate.Handle, error) {
	e.mu.Lock()
	defer e.mu.Unlock()
	e.restores++
	return substrate.Handle{ThreadID: ref.ThreadID, ID: "vm2-" + ref.ThreadID, Node: ref.Node}, nil
}

func (e *fakeExec) Release(_ context.Context, _ substrate.Handle) error {
	e.mu.Lock()
	defer e.mu.Unlock()
	e.releases++
	return nil
}

// Snapshot makes fakeExec satisfy substrate.Snapshotable so snapshot-on-idle is
// exercised.
func (e *fakeExec) Snapshot(_ context.Context, h substrate.Handle) (substrate.SnapshotRef, error) {
	e.mu.Lock()
	defer e.mu.Unlock()
	e.snapshots++
	return substrate.SnapshotRef{ID: "snap-" + h.ThreadID, ThreadID: h.ThreadID, Node: h.Node, SizeBytes: 1024}, nil
}

func (e *fakeExec) RemoveBundle(threadID string) error {
	e.mu.Lock()
	defer e.mu.Unlock()
	e.removed = append(e.removed, threadID)
	return nil
}

func newLoop(reg *fakeRegistry, ex *fakeExec) *Loop {
	return &Loop{Registry: reg, Executor: ex, Reclaimer: ex, Node: "node-4", live: map[string]substrate.Handle{}}
}

func TestReconcileCreatesPending(t *testing.T) {
	reg := newFakeRegistry(store.Thread{ThreadID: "t1", State: substrate.StatePending, Node: "node-4"})
	ex := &fakeExec{}
	l := newLoop(reg, ex)
	l.reconcileOnce(context.Background(), testLogger())

	if ex.claims != 1 {
		t.Fatalf("claims = %d, want 1", ex.claims)
	}
	if reg.state("t1") != substrate.StateRunning {
		t.Fatalf("state = %q, want RUNNING", reg.state("t1"))
	}
	if l.LiveThreads() != 1 {
		t.Fatalf("live = %d, want 1", l.LiveThreads())
	}
}

func TestReconcileSnapshotsOnIdle(t *testing.T) {
	reg := newFakeRegistry(store.Thread{ThreadID: "t1", State: substrate.StatePending, Node: "node-4"})
	ex := &fakeExec{}
	l := newLoop(reg, ex)
	ctx := context.Background()
	// Claim it so it is live + RUNNING.
	l.reconcileOnce(ctx, testLogger())
	if l.LiveThreads() != 1 {
		t.Fatalf("live = %d, want 1 before idle", l.LiveThreads())
	}
	// An idle boundary snapshots the VM, records it as the IDLE form, and releases.
	l.snapshotIdle(ctx, testLogger(), idleEvent{threadID: "t1"})
	if ex.snapshots != 1 {
		t.Fatalf("snapshots = %d, want 1", ex.snapshots)
	}
	if ex.releases != 1 {
		t.Fatalf("releases = %d, want 1", ex.releases)
	}
	if reg.state("t1") != substrate.StateIdle {
		t.Fatalf("state = %q, want IDLE", reg.state("t1"))
	}
	if l.LiveThreads() != 0 {
		t.Fatalf("live = %d, want 0 after snapshot", l.LiveThreads())
	}
}

func TestReconcilePendingClaimFailureMarksFailed(t *testing.T) {
	reg := newFakeRegistry(store.Thread{ThreadID: "t1", State: substrate.StatePending, Node: "node-4"})
	ex := &fakeExec{failNext: errors.New("no kvm")}
	l := newLoop(reg, ex)
	l.reconcileOnce(context.Background(), testLogger())
	if reg.state("t1") != substrate.StateFailed {
		t.Fatalf("state = %q, want FAILED", reg.state("t1"))
	}
}

func TestReconcileRestoresWakeRequested(t *testing.T) {
	reg := newFakeRegistry(store.Thread{
		ThreadID: "t1", State: substrate.StateIdle, Node: "node-4",
		ThreadSnapshotRef: "snap-1", WakeRequestedAt: time.Now(),
	})
	ex := &fakeExec{}
	l := newLoop(reg, ex)
	l.reconcileOnce(context.Background(), testLogger())

	if ex.restores != 1 {
		t.Fatalf("restores = %d, want 1", ex.restores)
	}
	if reg.state("t1") != substrate.StateRunning {
		t.Fatalf("state = %q, want RUNNING", reg.state("t1"))
	}
}

func TestReconcileReclaimsCompleted(t *testing.T) {
	reg := newFakeRegistry(store.Thread{ThreadID: "t1", State: substrate.StateCompleted, Node: "node-4"})
	ex := &fakeExec{}
	l := newLoop(reg, ex)
	l.live["t1"] = substrate.Handle{ThreadID: "t1", ID: "vm"}
	l.reconcileOnce(context.Background(), testLogger())

	if ex.releases != 1 {
		t.Fatalf("releases = %d, want 1", ex.releases)
	}
	if len(ex.removed) != 1 || ex.removed[0] != "t1" {
		t.Fatalf("removed = %v, want [t1]", ex.removed)
	}
	if reg.state("t1") != "GONE" {
		t.Fatalf("completed thread row should be deleted, got %q", reg.state("t1"))
	}
}

func TestReconcileGCsIdleExpired(t *testing.T) {
	reg := newFakeRegistry(store.Thread{
		ThreadID: "t1", State: substrate.StateIdle, Node: "node-4",
		TTLSeconds: 1, LastActiveAt: time.Now().Add(-time.Hour),
	})
	ex := &fakeExec{}
	l := newLoop(reg, ex)
	l.reconcileOnce(context.Background(), testLogger())

	if len(ex.removed) != 1 {
		t.Fatalf("expected idle-expired bundle removed, got %v", ex.removed)
	}
	if reg.state("t1") != "GONE" {
		t.Fatalf("idle-expired row should be deleted, got %q", reg.state("t1"))
	}
}

func TestReconcileAdoptsOrphanedRunning(t *testing.T) {
	// RUNNING with a snapshot but no live handle (post-restart) -> restored.
	reg := newFakeRegistry(store.Thread{
		ThreadID: "t1", State: substrate.StateRunning, Node: "node-4", ThreadSnapshotRef: "snap-1",
	})
	ex := &fakeExec{}
	l := newLoop(reg, ex)
	l.reconcileOnce(context.Background(), testLogger())
	if ex.restores != 1 || l.LiveThreads() != 1 {
		t.Fatalf("orphaned RUNNING with snapshot should be restored; restores=%d live=%d", ex.restores, l.LiveThreads())
	}
}

func TestReconcileOrphanedRunningNoSnapshotReinits(t *testing.T) {
	reg := newFakeRegistry(store.Thread{
		ThreadID: "t1", State: substrate.StateRunning, Node: "node-4", ThreadSnapshotRef: "",
	})
	ex := &fakeExec{}
	l := newLoop(reg, ex)
	l.reconcileOnce(context.Background(), testLogger())
	if reg.state("t1") != substrate.StatePending {
		t.Fatalf("orphaned RUNNING without snapshot should re-init to PENDING, got %q", reg.state("t1"))
	}
}

func TestRunStopsOnCancel(t *testing.T) {
	reg := newFakeRegistry()
	ex := &fakeExec{}
	l := &Loop{Registry: reg, Executor: ex, Reclaimer: ex, Node: "node-4", Interval: time.Millisecond}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- l.Run(ctx) }()
	time.Sleep(10 * time.Millisecond)
	cancel()
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("Run: %v", err)
		}
	case <-time.After(time.Second):
		t.Fatal("Run did not return after cancel")
	}
}

func TestDryRunWithoutRegistry(t *testing.T) {
	l := &Loop{Node: "node-4", Interval: time.Millisecond}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Millisecond)
	defer cancel()
	if err := l.Run(ctx); err != nil {
		t.Fatalf("dry-run Run should not error: %v", err)
	}
}

func testLogger() *slog.Logger { return slog.New(slog.NewTextHandler(io.Discard, nil)) }
