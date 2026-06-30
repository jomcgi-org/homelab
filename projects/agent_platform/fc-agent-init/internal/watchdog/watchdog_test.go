package watchdog

import (
	"context"
	"testing"
	"time"
)

func TestWriteMarksActivity(t *testing.T) {
	now := time.Unix(1000, 0)
	w := &Monitor{StallAfter: time.Minute, now: func() time.Time { return now }}
	if _, err := w.Write([]byte("goose output")); err != nil {
		t.Fatalf("Write: %v", err)
	}
	now = now.Add(30 * time.Second)
	if got := w.IdleFor(); got != 30*time.Second {
		t.Fatalf("IdleFor = %s, want 30s", got)
	}
}

func TestStalledReportsAfterWindow(t *testing.T) {
	now := time.Unix(1000, 0)
	w := &Monitor{StallAfter: time.Minute, now: func() time.Time { return now }}
	_, _ = w.Write([]byte("x")) // first activity
	now = now.Add(59 * time.Second)
	if w.Stalled() {
		t.Fatal("should not be stalled before the window elapses")
	}
	now = now.Add(2 * time.Second) // 61s since last write
	if !w.Stalled() {
		t.Fatal("should be stalled after the window elapses")
	}
	// Fresh output re-arms it.
	_, _ = w.Write([]byte("y"))
	if w.Stalled() {
		t.Fatal("a write should re-arm the monitor")
	}
}

func TestRunFiresOnceOnStall(t *testing.T) {
	now := time.Unix(1000, 0)
	w := &Monitor{StallAfter: time.Minute, now: func() time.Time { return now }}
	_, _ = w.Write([]byte("x"))

	fired := make(chan struct{}, 4)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	now = now.Add(2 * time.Minute)
	go w.Run(ctx, 5*time.Millisecond, func() { fired <- struct{}{} })

	select {
	case <-fired:
	case <-time.After(time.Second):
		t.Fatal("onStall never fired")
	}
	select {
	case <-fired:
		t.Fatal("onStall fired more than once for one stall")
	case <-time.After(50 * time.Millisecond):
	}
}
