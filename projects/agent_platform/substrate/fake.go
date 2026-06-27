package substrate

import (
	"context"
	"fmt"
	"io"
	"strings"
	"sync"
)

// Fake is an in-memory Substrate + Snapshotable implementation for testing
// consumers with no cluster (ADR 019: "the fake also lets the consumers be
// tested with no cluster, which matters given this repo has no local test
// loop"). It records every call so tests can assert on the lifecycle.
type Fake struct {
	mu sync.Mutex

	seq       int
	live      map[string]Handle      // handle ID -> handle
	snapshots map[string]SnapshotRef // snapshot ID -> ref

	// Calls records the ordered sequence of method names invoked.
	Calls []string

	// ExecOutput is the canned stdout returned by Exec.
	ExecOutput string

	// ClaimErr, when set, is returned by the next Claim.
	ClaimErr error
}

// NewFake returns an empty in-memory fake.
func NewFake() *Fake {
	return &Fake{
		live:      make(map[string]Handle),
		snapshots: make(map[string]SnapshotRef),
	}
}

var (
	_ Substrate    = (*Fake)(nil)
	_ Snapshotable = (*Fake)(nil)
)

func (f *Fake) record(name string) { f.Calls = append(f.Calls, name) }

func (f *Fake) next(prefix string) string {
	f.seq++
	return fmt.Sprintf("%s-%d", prefix, f.seq)
}

// Claim acquires a fake environment. If spec.BaseSnapshotRef is set it behaves
// like a restore from that base; otherwise it boots cold.
func (f *Fake) Claim(_ context.Context, spec ClaimSpec) (Handle, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.record("Claim")
	if f.ClaimErr != nil {
		err := f.ClaimErr
		f.ClaimErr = nil
		return Handle{}, err
	}
	threadID := spec.ThreadID
	if threadID == "" {
		threadID = f.next("thread")
	}
	h := Handle{
		ThreadID: threadID,
		ID:       f.next("vm"),
		Node:     "node-4",
	}
	f.live[h.ID] = h
	return h, nil
}

// Exec returns the canned output for a live handle.
func (f *Fake) Exec(_ context.Context, h Handle, _ Request) (Stream, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.record("Exec")
	if _, ok := f.live[h.ID]; !ok {
		return nil, fmt.Errorf("substrate: exec on unknown handle %q", h.ID)
	}
	return io.NopCloser(strings.NewReader(f.ExecOutput)), nil
}

// Release destroys a live handle.
func (f *Fake) Release(_ context.Context, h Handle) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.record("Release")
	if _, ok := f.live[h.ID]; !ok {
		return fmt.Errorf("substrate: release of unknown handle %q", h.ID)
	}
	delete(f.live, h.ID)
	return nil
}

// Snapshot captures a live handle into a restorable ref. The handle stays live
// (FC pauses then resumes); callers Release separately.
func (f *Fake) Snapshot(_ context.Context, h Handle) (SnapshotRef, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.record("Snapshot")
	if _, ok := f.live[h.ID]; !ok {
		return SnapshotRef{}, fmt.Errorf("substrate: snapshot of unknown handle %q", h.ID)
	}
	ref := SnapshotRef{
		ID:        f.next("snap"),
		ThreadID:  h.ThreadID,
		Node:      h.Node,
		SizeBytes: 1 << 20,
	}
	f.snapshots[ref.ID] = ref
	return ref, nil
}

// Restore creates a new live handle from a snapshot ref. The handle ID is fresh
// (a new microVM) but the thread identity is preserved.
func (f *Fake) Restore(_ context.Context, ref SnapshotRef) (Handle, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.record("Restore")
	if _, ok := f.snapshots[ref.ID]; !ok {
		return Handle{}, fmt.Errorf("substrate: restore of unknown snapshot %q", ref.ID)
	}
	h := Handle{
		ThreadID: ref.ThreadID,
		ID:       f.next("vm"),
		Node:     ref.Node,
	}
	f.live[h.ID] = h
	return h, nil
}

// LiveCount reports how many handles are currently live (test helper).
func (f *Fake) LiveCount() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.live)
}
