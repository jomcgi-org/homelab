package substrate

import (
	"context"
	"io"
	"testing"
)

func TestFakeClaimExecRelease(t *testing.T) {
	ctx := context.Background()
	f := NewFake()
	f.ExecOutput = "hello"

	h, err := f.Claim(ctx, ClaimSpec{Repo: "homelab", Branch: "main"})
	if err != nil {
		t.Fatalf("Claim: %v", err)
	}
	if h.ThreadID == "" || h.ID == "" {
		t.Fatalf("Claim returned empty identity: %+v", h)
	}
	if got := f.LiveCount(); got != 1 {
		t.Fatalf("LiveCount = %d, want 1", got)
	}

	stream, err := f.Exec(ctx, h, Request{Argv: []string{"echo", "hi"}})
	if err != nil {
		t.Fatalf("Exec: %v", err)
	}
	out, _ := io.ReadAll(stream)
	stream.Close()
	if string(out) != "hello" {
		t.Fatalf("Exec output = %q, want %q", out, "hello")
	}

	if err := f.Release(ctx, h); err != nil {
		t.Fatalf("Release: %v", err)
	}
	if got := f.LiveCount(); got != 0 {
		t.Fatalf("LiveCount after release = %d, want 0", got)
	}
}

// TestFakeSnapshotRestoreContinuity proves the contract ADR 022 is built on:
// snapshot a live thread, release the original microVM, then restore a fresh
// microVM that preserves the stable ThreadID (continues, does not get a new
// identity).
func TestFakeSnapshotRestoreContinuity(t *testing.T) {
	ctx := context.Background()
	f := NewFake()

	h, err := f.Claim(ctx, ClaimSpec{ThreadID: "t-stable"})
	if err != nil {
		t.Fatalf("Claim: %v", err)
	}

	ref, err := f.Snapshot(ctx, h)
	if err != nil {
		t.Fatalf("Snapshot: %v", err)
	}
	if ref.ThreadID != "t-stable" {
		t.Fatalf("snapshot ThreadID = %q, want t-stable", ref.ThreadID)
	}
	if ref.SizeBytes == 0 {
		t.Fatalf("snapshot SizeBytes should be set for GC budgeting")
	}

	// Release the original microVM (compute released; thread is idle).
	if err := f.Release(ctx, h); err != nil {
		t.Fatalf("Release: %v", err)
	}
	if got := f.LiveCount(); got != 0 {
		t.Fatalf("LiveCount after release = %d, want 0", got)
	}

	// Wake: restore a fresh microVM from the snapshot.
	h2, err := f.Restore(ctx, ref)
	if err != nil {
		t.Fatalf("Restore: %v", err)
	}
	if h2.ThreadID != "t-stable" {
		t.Fatalf("restored ThreadID = %q, want t-stable (continuity)", h2.ThreadID)
	}
	if h2.ID == h.ID {
		t.Fatalf("restored microVM ID should differ from original; both %q", h2.ID)
	}
	if got := f.LiveCount(); got != 1 {
		t.Fatalf("LiveCount after restore = %d, want 1", got)
	}
}

func TestFakeErrorsOnUnknownHandle(t *testing.T) {
	ctx := context.Background()
	f := NewFake()
	bogus := Handle{ThreadID: "x", ID: "nope"}

	if _, err := f.Exec(ctx, bogus, Request{}); err == nil {
		t.Fatal("Exec on unknown handle should error")
	}
	if err := f.Release(ctx, bogus); err == nil {
		t.Fatal("Release of unknown handle should error")
	}
	if _, err := f.Snapshot(ctx, bogus); err == nil {
		t.Fatal("Snapshot of unknown handle should error")
	}
	if _, err := f.Restore(ctx, SnapshotRef{ID: "nope"}); err == nil {
		t.Fatal("Restore of unknown snapshot should error")
	}
}

// TestFakeImplementsCapabilities asserts the Fake satisfies the capability
// interfaces consumers type-assert against.
func TestFakeImplementsCapabilities(t *testing.T) {
	var s Substrate = NewFake()
	if _, ok := s.(Snapshotable); !ok {
		t.Fatal("Fake should implement Snapshotable")
	}
}
