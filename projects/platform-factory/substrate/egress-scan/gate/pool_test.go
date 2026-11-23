package main

import (
	"context"
	"fmt"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/prometheus/client_golang/prometheus/testutil"
	"google.golang.org/grpc/codes"
)

func TestPoolFillsWithNamedChildren(t *testing.T) {
	e := newEnv(t, nil)
	e.start()
	names := e.api.running("eg-gate-0-")
	if len(names) != 2 {
		t.Fatalf("running children = %v, want 2", names)
	}
	for _, n := range names {
		// eg-<pod>-<start epoch>-<n>
		parts := strings.Split(n, "-")
		if len(parts) != 5 || parts[0] != "eg" || parts[1] != "gate" || parts[2] != "0" {
			t.Fatalf("child name %q has the wrong shape", n)
		}
	}
	if e.api.previewMissing.Load() != 0 || e.api.authMissing.Load() != 0 {
		t.Fatalf("calls without the preview header or the bearer token: %d, %d", e.api.previewMissing.Load(), e.api.authMissing.Load())
	}
	if e.api.wrongAtespace.Load() != 0 || e.api.wrongTemplate.Load() != 0 {
		t.Fatal("a create named another atespace or template")
	}
	if !e.pool.ready() {
		t.Fatal("pool is not ready")
	}
	if got := testutil.ToFloat64(e.m.children.WithLabelValues("waiting")); got != 2 {
		t.Fatalf("waiting gauge = %v", got)
	}
}

func TestStartupDeletesOnlyOwnLeftovers(t *testing.T) {
	e := newEnv(t, nil)
	for _, n := range []string{
		"eg-gate-0-100-1", "eg-gate-0-100-2", "eg-gate-0-99-7", // leftovers of this pod, two incarnations
		"eg-gate-1-100-1",  // another pod
		"eg-gate-01-100-1", // another pod whose name starts the same
		"seed-0123456789abcdef", "other",
	} {
		e.api.add(n, 2)
	}
	e.start()
	for _, n := range []string{"eg-gate-0-100-1", "eg-gate-0-100-2", "eg-gate-0-99-7"} {
		if len(e.api.names(n)) != 0 {
			t.Fatalf("leftover %s was not deleted", n)
		}
	}
	for _, n := range []string{"eg-gate-1-100-1", "eg-gate-01-100-1", "seed-0123456789abcdef", "other"} {
		if len(e.api.names(n)) != 1 {
			t.Fatalf("%s must stay", n)
		}
	}
	// The pool starts only after the cleanup: no new child shares a leftover's name.
	if got := e.api.running("eg-gate-0-"); len(got) != 2 {
		t.Fatalf("children after cleanup = %v", got)
	}
}

func TestStartupWaitsForTheAPI(t *testing.T) {
	e := newEnv(t, nil)
	e.api.down.Store(true)
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { defer close(done); e.pool.run(ctx) }()
	t.Cleanup(func() { cancel(); <-done; e.pool.bg.Wait() })
	time.Sleep(150 * time.Millisecond)
	if e.pool.ready() || e.api.creates.Load() != 0 {
		t.Fatal("the pool must not create before its cleanup works")
	}
	e.api.down.Store(false)
	e.eventually("pool full after the API returns", func() bool { return e.waitingCount() == 2 && e.pool.ready() })
}

func TestClaimIsOneShotAndReplenishIsOffThePath(t *testing.T) {
	e := newEnv(t, nil)
	e.start()
	// Slow creates: the claim must not wait for them.
	e.api.createDelay.Store(int64(300 * time.Millisecond))
	before := e.waitingNames()
	began := time.Now()
	c, ok := e.pool.claim()
	if !ok || time.Since(began) > 50*time.Millisecond {
		t.Fatalf("claim = %v, %v in %v", c, ok, time.Since(began))
	}
	if e.waitingCount() != 1 {
		t.Fatalf("waiting after a claim = %d", e.waitingCount())
	}
	for _, n := range e.waitingNames() {
		if n == c.name {
			t.Fatal("a claimed child is still in the pool")
		}
	}
	// The child serves and exits; the pool refills in the background.
	e.api.exit(c.name)
	e.eventually("replenish", func() bool { return e.waitingCount() == 2 })
	for _, n := range e.waitingNames() {
		if n == c.name {
			t.Fatal("a spent child came back")
		}
	}
	if len(before) != 2 {
		t.Fatal("setup")
	}
}

func TestConcurrentClaimsNeverShareAChild(t *testing.T) {
	e := newEnv(t, func(c *config) { c.TargetWaiting = 8; c.CreateConcurrency = 8 })
	e.start()
	var mu sync.Mutex
	seen := map[string]int{}
	var wg sync.WaitGroup
	for i := 0; i < 64; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for j := 0; j < 20; j++ {
				if c, ok := e.pool.claim(); ok {
					mu.Lock()
					seen[c.name]++
					mu.Unlock()
					e.api.exit(c.name)
				}
			}
		}()
	}
	wg.Wait()
	if len(seen) == 0 {
		t.Fatal("no child was claimed")
	}
	for n, k := range seen {
		if k != 1 {
			t.Fatalf("child %s handed out %d times", n, k)
		}
	}
}

func TestReleaseReturnsAnUnspentChild(t *testing.T) {
	e := newEnv(t, nil)
	e.start()
	c, _ := e.pool.claim()
	e.pool.release(c)
	if e.waitingCount() < 2 {
		t.Fatalf("waiting = %d after release", e.waitingCount())
	}
	e.stop()
}

func TestRecycleReplacesBeforeMaxLifetimeWithoutAGap(t *testing.T) {
	e := newEnv(t, nil)
	e.start()
	old := e.waitingNames()
	// The fresh children take 200 ms to make. The old ones must stay claimable
	// until the fresh ones wait: the pool is never empty in between.
	e.api.createDelay.Store(int64(200 * time.Millisecond))
	e.clock.advance(e.cfg.RecycleAfter + time.Second)
	deadline := time.Now().Add(5 * time.Second)
	for {
		if e.waitingCount() == 0 {
			t.Fatal("the pool was empty while it recycled")
		}
		fresh := 0
		for _, n := range e.waitingNames() {
			if n != old[0] && n != old[1] {
				fresh++
			}
		}
		if fresh == 2 && len(e.waitingNames()) == 2 {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("recycle did not finish: waiting %v, old %v", e.waitingNames(), old)
		}
		time.Sleep(2 * time.Millisecond)
	}
	e.eventually("old children deleted", func() bool { return len(e.api.names(old[0])) == 0 && len(e.api.names(old[1])) == 0 })
	if got := testutil.ToFloat64(e.m.recycled); got != 2 {
		t.Fatalf("recycled = %v, want 2", got)
	}
	// A delete, not an expiry: the control plane would report an expiry as a crash.
	if got := e.api.deletedNames(); len(got) != 2 {
		t.Fatalf("deleted = %v", got)
	}
}

func TestRecycleKeepsStaleChildrenUntilFreshOnesWait(t *testing.T) {
	e := newEnv(t, nil)
	e.start()
	old := e.waitingNames()
	e.clock.advance(e.cfg.RecycleAfter + time.Second)
	e.api.failCreate.Store(uint32(codes.Unavailable))
	e.pool.reconcile()
	time.Sleep(50 * time.Millisecond)
	if e.waitingCount() != 2 {
		t.Fatalf("stale children were dropped with no replacement: %d left (%v)", e.waitingCount(), old)
	}
	// Past the hard limit a child is never handed out.
	e.clock.advance(e.cfg.HardAfter)
	if c, ok := e.pool.claim(); ok {
		t.Fatalf("claimed %s past the hard limit", c.name)
	}
	e.eventually("expired children deleted", func() bool { return len(e.api.running("eg-gate-0-")) == 0 })
}

func TestReplenishBacksOffAndRecovers(t *testing.T) {
	e := newEnv(t, nil)
	e.api.failCreate.Store(uint32(codes.ResourceExhausted))
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { defer close(done); e.pool.run(ctx) }()
	t.Cleanup(func() { cancel(); <-done; e.pool.bg.Wait() })
	e.eventually("failures counted", func() bool { return testutil.ToFloat64(e.m.replenish.WithLabelValues("create")) >= 2 })
	if e.pool.ready() {
		t.Fatal("ready with no child")
	}
	attempts := e.api.creates.Load()
	time.Sleep(300 * time.Millisecond)
	// Back-off: no hot loop.
	if e.api.creates.Load()-attempts > 6 {
		t.Fatalf("%d creates in 300 ms: no back-off", e.api.creates.Load()-attempts)
	}
	e.api.failCreate.Store(uint32(codes.OK))
	e.eventually("recovers", func() bool { return e.waitingCount() == 2 })
}

func TestFailedResumeDeletesTheChild(t *testing.T) {
	e := newEnv(t, nil)
	e.api.failResume.Store(uint32(codes.Internal))
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { defer close(done); e.pool.run(ctx) }()
	t.Cleanup(func() { cancel(); <-done; e.pool.bg.Wait() })
	e.eventually("resume failure", func() bool { return testutil.ToFloat64(e.m.replenish.WithLabelValues("resume")) >= 1 })
	e.eventually("failed child deleted", func() bool { return len(e.api.deletedNames()) >= 1 })
}

func TestSlowResumeIsPolledUntilRunning(t *testing.T) {
	e := newEnv(t, nil)
	e.api.resumeSlow.Store(true)
	e.start()
	if got := e.api.running("eg-gate-0-"); len(got) != 2 {
		t.Fatalf("running = %v", got)
	}
}

func TestSweepDeletesOrphansAfterTwoSightings(t *testing.T) {
	e := newEnv(t, nil)
	e.start()
	// A served child that the control plane never deleted, and an unknown one.
	c, _ := e.pool.claim()
	e.api.add("eg-gate-0-1-99", 2)
	ctx := context.Background()
	e.pool.sweep(ctx)
	if len(e.api.names("eg-gate-0-1-99")) != 1 {
		t.Fatal("a child is deleted on the first sighting")
	}
	e.pool.sweep(ctx)
	e.eventually("orphan deleted", func() bool { return len(e.api.names("eg-gate-0-1-99")) == 0 })
	// The served child is inside its grace period.
	e.pool.sweep(ctx)
	e.pool.sweep(ctx)
	if len(e.api.names(c.name)) != 1 {
		t.Fatal("a served child was swept inside its grace period")
	}
	e.clock.advance(e.cfg.SweepGrace + time.Second)
	e.pool.sweep(ctx)
	e.pool.sweep(ctx)
	e.eventually("stale served child deleted", func() bool { return len(e.api.names(c.name)) == 0 })
	// Waiting children are never swept.
	for _, n := range e.waitingNames() {
		if len(e.api.names(n)) != 1 {
			t.Fatalf("waiting child %s was deleted", n)
		}
	}
}

func TestShutdownDeletesEveryChild(t *testing.T) {
	e := newEnv(t, nil)
	e.start()
	e.api.add("eg-gate-0-5-5", 2)
	e.stop()
	e.pool.shutdown(context.Background())
	if got := e.api.names("eg-gate-0-"); len(got) != 0 {
		t.Fatalf("children left after shutdown: %v", got)
	}
}

func TestOldChildrenRecycleOneAtATime(t *testing.T) {
	e := newEnv(t, nil)
	e.start()
	old := e.waitingNames()
	// Both children were made together and grow old together.
	e.api.createDelay.Store(int64(150 * time.Millisecond))
	e.clock.advance(e.cfg.RecycleAfter + time.Second)
	isOld := func(n string) bool { return n == old[0] || n == old[1] }
	deadline := time.Now().Add(8 * time.Second)
	for {
		e.pool.mu.Lock()
		creating, waiting := len(e.pool.creating), len(e.pool.waiting)
		e.pool.mu.Unlock()
		if creating > 1 {
			t.Fatalf("%d children are made at once while two old ones recycle", creating)
		}
		if waiting == 0 {
			t.Fatal("the pool was empty while it recycled")
		}
		fresh := 0
		for _, n := range e.waitingNames() {
			if !isOld(n) {
				fresh++
			}
		}
		if fresh == 2 && e.waitingCount() == 2 {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("recycle did not finish: %v", e.waitingNames())
		}
		time.Sleep(time.Millisecond)
	}
	if got := testutil.ToFloat64(e.m.recycled); got != 2 {
		t.Fatalf("recycled = %v", got)
	}
}

func TestReadyStaysTrueAfterABurstEmptiesThePool(t *testing.T) {
	e := newEnv(t, nil)
	if e.pool.ready() {
		t.Fatal("ready before the first fill")
	}
	e.start()
	if !e.pool.ready() {
		t.Fatal("not ready with a full pool")
	}
	// A burst takes both children, and creation is slow.
	e.api.createDelay.Store(int64(300 * time.Millisecond))
	e.pool.claim()
	e.pool.claim()
	if e.waitingCount() != 0 || !e.pool.ready() {
		t.Fatalf("waiting %d, ready %v: a burst must not make the pod unready", e.waitingCount(), e.pool.ready())
	}
	// Only a dead API does.
	e.clock.advance(time.Minute)
	if e.pool.ready() {
		t.Fatal("ready with no API answer")
	}
}

func TestDrainFailsReadiness(t *testing.T) {
	e := newEnv(t, nil)
	e.start()
	e.pool.drain()
	if e.pool.ready() {
		t.Fatal("ready while draining")
	}
}

func TestDeletesRunOnAFewWorkers(t *testing.T) {
	e := newEnv(t, nil)
	e.api.deleteDelay.Store(int64(10 * time.Millisecond))
	for i := 0; i < 40; i++ {
		e.api.add(fmt.Sprintf("eg-gate-0-9-%d", i), 2)
		e.pool.deleteAsync(fmt.Sprintf("eg-gate-0-9-%d", i))
	}
	e.eventually("all deleted", func() bool { return len(e.api.names("eg-gate-0-9-")) == 0 })
	if got := e.api.deleteMax.Load(); got > deleteWorkers || got < 2 {
		t.Fatalf("up to %d deletes at once, want 2 to %d", got, deleteWorkers)
	}
}
