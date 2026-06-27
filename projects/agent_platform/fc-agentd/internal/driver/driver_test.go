package driver

import (
	"context"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"os"
	"sync"
	"testing"

	"github.com/jomcgi/homelab/projects/agent_platform/substrate"
)

// fakeLauncher stands up a fake Firecracker API server on each requested socket,
// so the driver's real fcclient drives a realistic API. CreateSnapshot writes
// the snapfile + memfile to disk so the bundle layout and Restore's existence
// checks are exercised end to end.
type fakeLauncher struct {
	mu       sync.Mutex
	launched int
}

type fakeProcess struct {
	srv    *http.Server
	killed bool
}

func (p *fakeProcess) Kill() error { p.killed = true; return p.srv.Close() }
func (p *fakeProcess) Wait() error { return nil }

func (l *fakeLauncher) Launch(_ context.Context, _ string, socketPath string) (Process, error) {
	l.mu.Lock()
	l.launched++
	l.mu.Unlock()
	_ = os.Remove(socketPath)
	ln, err := net.Listen("unix", socketPath)
	if err != nil {
		return nil, err
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/snapshot/create", func(w http.ResponseWriter, r *http.Request) {
		var body map[string]any
		b, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(b, &body)
		// Persist the bundle files the controller asked for.
		if sp, ok := body["snapshot_path"].(string); ok {
			_ = os.WriteFile(sp, []byte("snap"), 0o600)
		}
		if mp, ok := body["mem_file_path"].(string); ok {
			_ = os.WriteFile(mp, []byte("mem-image-bytes"), 0o600)
		}
		w.WriteHeader(http.StatusNoContent)
	})
	mux.HandleFunc("/", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNoContent)
	})
	srv := &http.Server{Handler: mux}
	go func() { _ = srv.Serve(ln) }()
	return &fakeProcess{srv: srv}, nil
}

func testDriver(t *testing.T) *Driver {
	t.Helper()
	return New(Config{
		KernelImagePath: "/opt/kata/vmlinux",
		RootfsPath:      "/dev/mapper/thread",
		SnapshotRoot:    t.TempDir(),
		Node:            "node-4",
		Arch:            "amd64",
	}, &fakeLauncher{}, nil)
}

func TestDriverClaimBootsMicroVM(t *testing.T) {
	d := testDriver(t)
	h, err := d.Claim(context.Background(), substrate.ClaimSpec{ThreadID: "t1", Repo: "homelab"})
	if err != nil {
		t.Fatalf("Claim: %v", err)
	}
	if h.ThreadID != "t1" || h.ID == "" || h.Node != "node-4" {
		t.Fatalf("unexpected handle: %+v", h)
	}
	if d.LiveCount() != 1 {
		t.Fatalf("LiveCount = %d, want 1", d.LiveCount())
	}
}

// TestDriverSnapshotRestoreContinuity is the Phase 1 done-criterion in unit
// form: boot -> snapshot -> release the original microVM -> restore a fresh
// microVM that keeps the stable ThreadID (continues, not a new identity).
func TestDriverSnapshotRestoreContinuity(t *testing.T) {
	ctx := context.Background()
	d := testDriver(t)

	h, err := d.Claim(ctx, substrate.ClaimSpec{ThreadID: "t-stable"})
	if err != nil {
		t.Fatalf("Claim: %v", err)
	}

	ref, err := d.Snapshot(ctx, h)
	if err != nil {
		t.Fatalf("Snapshot: %v", err)
	}
	if ref.ThreadID != "t-stable" || ref.Node != "node-4" || ref.Arch != "amd64" {
		t.Fatalf("unexpected ref: %+v", ref)
	}
	if ref.SizeBytes == 0 {
		t.Fatalf("snapshot SizeBytes should reflect the on-disk bundle")
	}

	if err := d.Release(ctx, h); err != nil {
		t.Fatalf("Release: %v", err)
	}
	if d.LiveCount() != 0 {
		t.Fatalf("LiveCount after release = %d, want 0", d.LiveCount())
	}

	h2, err := d.Restore(ctx, ref)
	if err != nil {
		t.Fatalf("Restore: %v", err)
	}
	if h2.ThreadID != "t-stable" {
		t.Fatalf("restored ThreadID = %q, want t-stable", h2.ThreadID)
	}
	if h2.ID == h.ID {
		t.Fatalf("restored microVM should have a fresh id; both %q", h2.ID)
	}
	if d.LiveCount() != 1 {
		t.Fatalf("LiveCount after restore = %d, want 1", d.LiveCount())
	}
}

func TestDriverRestoreRejectsArchMismatch(t *testing.T) {
	d := testDriver(t)
	_, err := d.Restore(context.Background(), substrate.SnapshotRef{ThreadID: "x", Arch: "arm64", Node: "node-4"})
	if err == nil {
		t.Fatal("restore should reject an arch-mismatched snapshot (non-portable)")
	}
}

func TestDriverClaimRejectsArchMismatch(t *testing.T) {
	d := testDriver(t)
	_, err := d.Claim(context.Background(), substrate.ClaimSpec{Arch: "arm64"})
	if err == nil {
		t.Fatal("claim should reject an arch-mismatched spec")
	}
}

func TestDriverReleaseUnknownHandleErrors(t *testing.T) {
	d := testDriver(t)
	if err := d.Release(context.Background(), substrate.Handle{ID: "nope"}); err == nil {
		t.Fatal("release of unknown handle should error")
	}
}

func TestDriverRestoreMissingBundleErrors(t *testing.T) {
	d := testDriver(t)
	// No snapshot ever taken for this thread.
	_, err := d.Restore(context.Background(), substrate.SnapshotRef{ThreadID: "ghost", Node: "node-4", Arch: "amd64"})
	if err == nil {
		t.Fatal("restore with no bundle on disk should error")
	}
}

func TestDriverExecNotHostProvided(t *testing.T) {
	d := testDriver(t)
	if _, err := d.Exec(context.Background(), substrate.Handle{}, substrate.Request{}); err == nil {
		t.Fatal("Exec should report it is handled by the in-VM harness")
	}
}
