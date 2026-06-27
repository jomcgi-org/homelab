package reconcile

import (
	"context"
	"errors"
	"sync/atomic"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/agent_platform/fc-agentd/internal/store"
)

type fakeRegistry struct {
	calls   atomic.Int32
	threads []store.Thread
	err     error
}

func (f *fakeRegistry) ListThreadsForNode(_ context.Context, _ string) ([]store.Thread, error) {
	f.calls.Add(1)
	return f.threads, f.err
}

func TestLoopRunsAtLeastOnceAndStopsOnCancel(t *testing.T) {
	reg := &fakeRegistry{threads: []store.Thread{{ThreadID: "t1", Node: "node-4"}}}
	l := &Loop{Registry: reg, Node: "node-4", Interval: time.Millisecond}

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- l.Run(ctx) }()

	// Give it time to run several passes, then cancel.
	time.Sleep(20 * time.Millisecond)
	cancel()

	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("Run returned error: %v", err)
		}
	case <-time.After(time.Second):
		t.Fatal("Run did not return after cancel")
	}

	if reg.calls.Load() < 1 {
		t.Fatalf("expected at least one reconcile pass, got %d", reg.calls.Load())
	}
}

func TestReconcileSurvivesRegistryError(t *testing.T) {
	reg := &fakeRegistry{err: errors.New("db down")}
	l := &Loop{Registry: reg, Node: "node-4", Interval: time.Millisecond}

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Millisecond)
	defer cancel()
	if err := l.Run(ctx); err != nil {
		t.Fatalf("Run should swallow registry errors, got %v", err)
	}
	if reg.calls.Load() < 1 {
		t.Fatal("expected reconcile to attempt the registry")
	}
}

func TestDryRunWithoutRegistry(t *testing.T) {
	l := &Loop{Node: "node-4", Interval: time.Millisecond}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Millisecond)
	defer cancel()
	if err := l.Run(ctx); err != nil {
		t.Fatalf("dry-run Run should not error, got %v", err)
	}
}
