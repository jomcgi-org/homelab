package reconcile

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/agent_platform/fc-agentd/internal/store"
	"github.com/jomcgi/homelab/projects/agent_platform/substrate"
)

// fakeRegistry is an in-memory store keyed by thread ID.
type fakeRegistry struct {
	mu            sync.Mutex
	threads       map[string]*store.Thread
	removed       []string
	listErr       error
	outbox        []outboxRow
	claimAttempts map[string]int
}

// outboxRow records an EnqueueDiscordOutbox call for assertions.
type outboxRow struct {
	channelID string
	content   string
}

func newFakeRegistry(ts ...store.Thread) *fakeRegistry {
	m := map[string]*store.Thread{}
	for i := range ts {
		t := ts[i]
		m[t.ThreadID] = &t
	}
	return &fakeRegistry{threads: m, claimAttempts: map[string]int{}}
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

func (f *fakeRegistry) RecordClaimFailure(_ context.Context, id string, maxAttempts int) (bool, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.claimAttempts[id]++
	if f.claimAttempts[id] < maxAttempts {
		return false, nil // stays PENDING; mirrors the store leaving state untouched
	}
	if t, ok := f.threads[id]; ok {
		t.State = substrate.StateFailed
		t.LastActiveAt = time.Now()
	}
	return true, nil
}

func (f *fakeRegistry) MarkRunningAfterClaim(_ context.Context, id string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.claimAttempts[id] = 0
	if t, ok := f.threads[id]; ok {
		t.State = substrate.StateRunning
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

func (f *fakeRegistry) EnqueueDiscordOutbox(_ context.Context, channelID, content string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.outbox = append(f.outbox, outboxRow{channelID: channelID, content: content})
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

func (f *fakeRegistry) attempts(id string) int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.claimAttempts[id]
}

// fakeExec is an in-memory executor + reclaimer.
type fakeExec struct {
	mu         sync.Mutex
	claims     int
	restores   int
	releases   int
	snapshots  int
	removed    []string
	failNext   error
	failAlways error
}

func (e *fakeExec) Claim(_ context.Context, spec substrate.ClaimSpec) (substrate.Handle, error) {
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.failAlways != nil {
		return substrate.Handle{}, e.failAlways
	}
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

func TestReconcileAdmissionCapDefersExcess(t *testing.T) {
	reg := newFakeRegistry(
		store.Thread{ThreadID: "t1", State: substrate.StatePending, Node: "node-4"},
		store.Thread{ThreadID: "t2", State: substrate.StatePending, Node: "node-4"},
		store.Thread{ThreadID: "t3", State: substrate.StatePending, Node: "node-4"},
	)
	ex := &fakeExec{}
	l := newLoop(reg, ex)
	l.MaxConcurrent = 2
	l.reconcileOnce(context.Background(), testLogger())

	if ex.claims != 2 {
		t.Fatalf("claims = %d, want 2 (capped at MaxConcurrent)", ex.claims)
	}
	if l.LiveThreads() != 2 {
		t.Fatalf("live = %d, want 2", l.LiveThreads())
	}
	// Exactly one of the three stays PENDING (map order is non-deterministic, so
	// assert the count, not which one).
	pending := 0
	for _, id := range []string{"t1", "t2", "t3"} {
		if reg.state(id) == substrate.StatePending {
			pending++
		}
	}
	if pending != 1 {
		t.Fatalf("pending = %d, want 1 deferred past the cap", pending)
	}
}

func TestReconcileAdmissionCapDrainsAsSlotsFree(t *testing.T) {
	reg := newFakeRegistry(
		store.Thread{ThreadID: "t1", State: substrate.StatePending, Node: "node-4"},
		store.Thread{ThreadID: "t2", State: substrate.StatePending, Node: "node-4"},
	)
	ex := &fakeExec{}
	l := newLoop(reg, ex)
	l.MaxConcurrent = 1
	l.reconcileOnce(context.Background(), testLogger())
	if l.LiveThreads() != 1 {
		t.Fatalf("live after first pass = %d, want 1", l.LiveThreads())
	}

	// Complete whichever thread was admitted; the loop reclaims it and frees the
	// slot, and a later pass admits the queued one (so the queue drains).
	for _, id := range []string{"t1", "t2"} {
		if reg.state(id) == substrate.StateRunning {
			_ = reg.SetState(context.Background(), id, substrate.StateCompleted)
		}
	}
	l.reconcileOnce(context.Background(), testLogger()) // reclaims completed, frees the slot
	l.reconcileOnce(context.Background(), testLogger()) // admits the queued thread

	if ex.claims != 2 {
		t.Fatalf("claims = %d, want 2 (both admitted over time)", ex.claims)
	}
	if l.LiveThreads() != 1 {
		t.Fatalf("live = %d, want 1 (still capped)", l.LiveThreads())
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

// TestReconcileReapsDiedGuest covers the host-side death reaper: when a guest's
// control channel drops without a clean Done (a panic / OOM / ungraceful exit),
// the loop marks the thread FAILED, releases the dead VM, and posts a failure to
// the Discord thread instead of leaving the build counter frozen forever.
func TestReconcileReapsDiedGuest(t *testing.T) {
	reg := newFakeRegistry(store.Thread{ThreadID: "t1", State: substrate.StatePending, Node: "node-4", DiscordThread: "d-123"})
	ex := &fakeExec{}
	l := newLoop(reg, ex)
	ctx := context.Background()
	// Claim it so it is live + RUNNING.
	l.reconcileOnce(ctx, testLogger())
	if l.LiveThreads() != 1 || reg.state("t1") != substrate.StateRunning {
		t.Fatalf("precondition: want 1 live RUNNING thread, got live=%d state=%q", l.LiveThreads(), reg.state("t1"))
	}

	// Its guest drops the control channel without completing.
	l.reapDied(ctx, testLogger(), diedEvent{threadID: "t1", discordThread: "d-123"})

	if reg.state("t1") != substrate.StateFailed {
		t.Fatalf("state = %q, want FAILED", reg.state("t1"))
	}
	if l.LiveThreads() != 0 {
		t.Fatalf("live = %d, want 0 (dead VM released)", l.LiveThreads())
	}
	if ex.releases != 1 {
		t.Fatalf("releases = %d, want 1", ex.releases)
	}
	if len(reg.outbox) != 1 || reg.outbox[0].channelID != "d-123" {
		t.Fatalf("outbox = %+v, want one failure message to d-123", reg.outbox)
	}
}

// TestReconcileReapDiedIgnoresStaleSignal proves the live-handle gate: a death
// signal for a thread with no live handle (already idled, completed, or
// reclaimed) is a no-op, so a late signal never double-fails a thread or spams
// Discord.
func TestReconcileReapDiedIgnoresStaleSignal(t *testing.T) {
	reg := newFakeRegistry()
	ex := &fakeExec{}
	l := newLoop(reg, ex)
	l.reapDied(context.Background(), testLogger(), diedEvent{threadID: "ghost", discordThread: "d-x"})
	if ex.releases != 0 || len(reg.outbox) != 0 {
		t.Fatalf("reap of unknown thread should be a no-op: releases=%d outbox=%d", ex.releases, len(reg.outbox))
	}
}

// With MaxClaimAttempts=1 a single launch failure exhausts the budget on the
// first attempt, so the thread is marked FAILED immediately (the pre-retry
// behaviour, preserved for genuinely fail-fast configs).
func TestReconcilePendingClaimFailureMarksFailed(t *testing.T) {
	reg := newFakeRegistry(store.Thread{ThreadID: "t1", State: substrate.StatePending, Node: "node-4"})
	ex := &fakeExec{failNext: errors.New("no kvm")}
	l := newLoop(reg, ex)
	l.MaxClaimAttempts = 1
	l.reconcileOnce(context.Background(), testLogger())
	if reg.state("t1") != substrate.StateFailed {
		t.Fatalf("state = %q, want FAILED", reg.state("t1"))
	}
}

// A transient launch failure (the daemon warming up during a rollout) must not be
// terminal: below the retry cap the thread stays PENDING, and a later poll once
// the substrate is ready claims it successfully and resets the attempt counter.
func TestReconcileClaimRetriesThenSucceeds(t *testing.T) {
	reg := newFakeRegistry(store.Thread{ThreadID: "t1", State: substrate.StatePending, Node: "node-4"})
	ex := &fakeExec{failNext: errors.New("kvm not warm yet")} // fails once, then succeeds
	l := newLoop(reg, ex)
	l.MaxClaimAttempts = 3

	// First pass: claim fails, thread stays PENDING for a retry (not FAILED).
	l.reconcileOnce(context.Background(), testLogger())
	if reg.state("t1") != substrate.StatePending {
		t.Fatalf("after transient failure state = %q, want PENDING (retry pending)", reg.state("t1"))
	}
	if reg.attempts("t1") != 1 {
		t.Fatalf("attempts = %d, want 1 after one failure", reg.attempts("t1"))
	}

	// Second pass: substrate is ready, claim succeeds, thread goes RUNNING and the
	// attempt counter resets so a later re-init starts fresh.
	l.reconcileOnce(context.Background(), testLogger())
	if reg.state("t1") != substrate.StateRunning {
		t.Fatalf("after recovery state = %q, want RUNNING", reg.state("t1"))
	}
	if reg.attempts("t1") != 0 {
		t.Fatalf("attempts = %d, want 0 after successful claim", reg.attempts("t1"))
	}
	if ex.claims != 1 {
		t.Fatalf("claims = %d, want 1 successful claim", ex.claims)
	}
}

// A genuinely unrecoverable launch (the substrate never comes back) must still
// terminate: the thread stays PENDING up to the cap, then is marked FAILED.
func TestReconcileClaimExhaustsRetriesMarksFailed(t *testing.T) {
	reg := newFakeRegistry(store.Thread{ThreadID: "t1", State: substrate.StatePending, Node: "node-4"})
	ex := &fakeExec{failAlways: errors.New("no kvm")}
	l := newLoop(reg, ex)
	l.MaxClaimAttempts = 3

	// Passes below the cap leave the thread PENDING.
	for i := 1; i < 3; i++ {
		l.reconcileOnce(context.Background(), testLogger())
		if reg.state("t1") != substrate.StatePending {
			t.Fatalf("attempt %d: state = %q, want PENDING (still retrying)", i, reg.state("t1"))
		}
	}
	// The pass that reaches the cap fails the thread terminally.
	l.reconcileOnce(context.Background(), testLogger())
	if reg.state("t1") != substrate.StateFailed {
		t.Fatalf("after exhausting retries state = %q, want FAILED", reg.state("t1"))
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

func TestReconcileReclaimsFailedPastRetention(t *testing.T) {
	// A thread that failed after provisioning has a CoW device; once past the
	// short retention window the loop must release the bundle + delete the row.
	reg := newFakeRegistry(store.Thread{
		ThreadID: "t1", State: substrate.StateFailed, Node: "node-4",
		LastActiveAt: time.Now().Add(-failedRetention - time.Minute),
	})
	ex := &fakeExec{}
	l := newLoop(reg, ex)
	l.reconcileOnce(context.Background(), testLogger())

	if len(ex.removed) != 1 || ex.removed[0] != "t1" {
		t.Fatalf("removed = %v, want [t1] (FAILED past retention should be reclaimed)", ex.removed)
	}
	if reg.state("t1") != "GONE" {
		t.Fatalf("reclaimed FAILED thread row should be deleted, got %q", reg.state("t1"))
	}
}

func TestReconcileKeepsRecentlyFailed(t *testing.T) {
	// A just-failed thread is kept for the retention window so the failure stays
	// inspectable; it must not be reclaimed yet.
	reg := newFakeRegistry(store.Thread{
		ThreadID: "t1", State: substrate.StateFailed, Node: "node-4",
		LastActiveAt: time.Now(),
	})
	ex := &fakeExec{}
	l := newLoop(reg, ex)
	l.reconcileOnce(context.Background(), testLogger())

	if len(ex.removed) != 0 {
		t.Fatalf("removed = %v, want [] (recently FAILED should be kept within retention)", ex.removed)
	}
	if reg.state("t1") != substrate.StateFailed {
		t.Fatalf("recently FAILED thread should remain FAILED, got %q", reg.state("t1"))
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

// TestEnvForTier covers ADR 024 tier selection: a tier's env is merged over the
// common GooseEnv, an empty tier resolves to "default", and an unknown tier
// fails safe to the common env alone (no other tier's credentials leak in).
func TestEnvForTier(t *testing.T) {
	l := &Loop{
		GooseEnv: map[string]string{"EGRESS_CA_CERT": "ca"},
		TierEnv: map[string]map[string]string{
			"default":  {"OPENAI_HOST": "http://qwen:8080", "GITHUB_TOKEN": "kloak:gh:x"},
			"artifact": {"OPENAI_HOST": "https://openrouter.ai/api", "OPENAI_API_KEY": "kloak:or:x"},
		},
	}
	log := testLogger()

	// Empty tier -> "default", merged over the common env.
	def := l.envForTier(log, "")
	if def["EGRESS_CA_CERT"] != "ca" || def["OPENAI_HOST"] != "http://qwen:8080" {
		t.Fatalf("empty tier should resolve to default merged over common env: %v", def)
	}

	// Artifact tier holds the OpenRouter placeholder and, crucially, NOT the gh
	// token (the credential trust boundary, ADR 024).
	art := l.envForTier(log, "artifact")
	if art["OPENAI_API_KEY"] != "kloak:or:x" {
		t.Fatalf("artifact tier should hold the openrouter placeholder: %v", art)
	}
	if _, leaked := art["GITHUB_TOKEN"]; leaked {
		t.Fatal("artifact tier must NOT hold the gh token placeholder (tier boundary leak)")
	}
	if art["EGRESS_CA_CERT"] != "ca" {
		t.Fatalf("artifact tier should still get the common CA cert: %v", art)
	}

	// Unknown tier fails safe to the common env alone.
	unk := l.envForTier(log, "nope")
	if _, ok := unk["OPENAI_HOST"]; ok {
		t.Fatalf("unknown tier should not get any model credential: %v", unk)
	}
	if unk["EGRESS_CA_CERT"] != "ca" {
		t.Fatalf("unknown tier should still get the common env: %v", unk)
	}

	// The merge must not mutate the shared input maps.
	if _, mutated := l.GooseEnv["OPENAI_HOST"]; mutated {
		t.Fatal("envForTier mutated the shared GooseEnv map")
	}
}

// TestEnvForThread covers the per-thread additions (ADR 026): every thread gets
// OTEL_RESOURCE_ATTRIBUTES (thread.id + tier, so goose's spans correlate with the
// dispatcher launch span), and a Discord-backed thread also gets ARTIFACT_ID and
// discord.thread.
func TestEnvForThread(t *testing.T) {
	l := &Loop{
		GooseEnv: map[string]string{"EGRESS_CA_CERT": "ca"},
		TierEnv: map[string]map[string]string{
			"artifact": {"OPENAI_HOST": "https://openrouter.ai/api"},
		},
	}
	log := testLogger()

	// Artifact thread with a Discord thread: full correlation set.
	art := l.envForThread(log, store.Thread{ThreadID: "t-abc", Tier: "artifact", DiscordThread: "12345"})
	wantAttrs := "thread.id=t-abc,tier=artifact,discord.thread=12345"
	if art["OTEL_RESOURCE_ATTRIBUTES"] != wantAttrs {
		t.Fatalf("artifact thread OTEL_RESOURCE_ATTRIBUTES = %q, want %q", art["OTEL_RESOURCE_ATTRIBUTES"], wantAttrs)
	}
	if art["ARTIFACT_ID"] != "12345" {
		t.Fatalf("artifact thread should carry ARTIFACT_ID=12345: %v", art)
	}

	// A thread with no Discord thread and empty tier: thread.id + tier=default,
	// no ARTIFACT_ID / discord.thread.
	def := l.envForThread(log, store.Thread{ThreadID: "t-xyz", Tier: ""})
	if def["OTEL_RESOURCE_ATTRIBUTES"] != "thread.id=t-xyz,tier=default" {
		t.Fatalf("default thread OTEL_RESOURCE_ATTRIBUTES = %q", def["OTEL_RESOURCE_ATTRIBUTES"])
	}
	if _, ok := def["ARTIFACT_ID"]; ok {
		t.Fatalf("non-Discord thread must not carry ARTIFACT_ID: %v", def)
	}
}

func TestPostProgressDoneSendsDoneMarker(t *testing.T) {
	var got struct {
		body []byte
		ct   string
		hits int
	}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		got.hits++
		got.ct = r.Header.Get("Content-Type")
		got.body, _ = io.ReadAll(r.Body)
		w.WriteHeader(http.StatusNoContent)
	}))
	defer srv.Close()

	postProgressDone(context.Background(), slog.New(slog.NewTextHandler(io.Discard, nil)), srv.URL, "12345")

	if got.hits != 1 {
		t.Fatalf("want 1 request, got %d", got.hits)
	}
	if got.ct != "application/json" {
		t.Fatalf("content-type = %q", got.ct)
	}
	var payload struct {
		ID   string `json:"id"`
		Done bool   `json:"done"`
	}
	if err := json.Unmarshal(got.body, &payload); err != nil {
		t.Fatalf("unmarshal body %q: %v", got.body, err)
	}
	if payload.ID != "12345" || !payload.Done {
		t.Fatalf("payload = %+v, want id=12345 done=true", payload)
	}
}

func TestPostProgressDoneNoopWhenUnconfigured(t *testing.T) {
	var hits int
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hits++
	}))
	defer srv.Close()
	log := slog.New(slog.NewTextHandler(io.Discard, nil))

	// No artifact id (non-Discord thread): must not post.
	postProgressDone(context.Background(), log, srv.URL, "")
	// No url (tier without PROGRESS_PUBLISH_URL): must not post.
	postProgressDone(context.Background(), log, "", "12345")

	if hits != 0 {
		t.Fatalf("want 0 requests when unconfigured, got %d", hits)
	}
}
