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

	"github.com/jomcgi/homelab/projects/firecracker/substrate/substrate"
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

// shortTempDir returns a temp dir under /tmp with a short path. The fake
// launcher binds a unix socket inside it, and macOS caps sun_path at 104 bytes,
// which t.TempDir()'s long /var/folders/... paths exceed (bind: invalid
// argument). On node-4 the snapshot root is the short /disks/nvme-02, so this is
// a test-only portability shim.
func shortTempDir(t *testing.T) string {
	t.Helper()
	d, err := os.MkdirTemp("/tmp", "fc")
	if err != nil {
		t.Fatalf("mkdir temp: %v", err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(d) })
	return d
}

func testDriver(t *testing.T) *Driver {
	t.Helper()
	return New(Config{
		KernelImagePath: "/opt/kata/vmlinux",
		RootfsPath:      "/dev/mapper/thread",
		SnapshotRoot:    shortTempDir(t),
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

// TestDriverClaimClearsStaleVsockUDS reproduces the orphan-recovery failure
// after a pod roll: a thread's bundle dir persists on the snapshot disk, so a
// vsock.sock left by the dead incarnation makes Firecracker's PUT /vsock bind
// fail with EADDRINUSE, looping the reconcile claim until it marks the thread
// FAILED. Claim must unlink the stale UDS (and its per-port children) first. The
// fake launcher does not bind the vsock UDS, so we assert the unlink directly:
// without it, Claim never touches vsock.sock and the seeded file survives.
func TestDriverClaimClearsStaleVsockUDS(t *testing.T) {
	d := testDriver(t)
	dir := d.threadDir("t-orphan")
	if err := os.MkdirAll(dir, 0o750); err != nil {
		t.Fatalf("mkdir thread dir: %v", err)
	}
	stale := d.VsockUDSPath("t-orphan")
	staleChild := stale + "_1024"
	for _, p := range []string{stale, staleChild} {
		if err := os.WriteFile(p, nil, 0o600); err != nil {
			t.Fatalf("seed stale socket %s: %v", p, err)
		}
	}

	if _, err := d.Claim(context.Background(), substrate.ClaimSpec{ThreadID: "t-orphan"}); err != nil {
		t.Fatalf("Claim over a stale vsock UDS: %v", err)
	}
	if _, err := os.Stat(stale); !os.IsNotExist(err) {
		t.Fatalf("stale vsock.sock should have been removed before bind, stat err=%v", err)
	}
	if _, err := os.Stat(staleChild); !os.IsNotExist(err) {
		t.Fatalf("stale vsock.sock_1024 should have been removed, stat err=%v", err)
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

// TestDriverWarmBaseStartReusesBaseBundle proves Phase 4's warm-base path: snap
// a warmed VM to a base bundle, then a new thread claims from that base for an
// instant ready start (a fresh microVM keyed by its own thread id).
func TestDriverWarmBaseStartReusesBaseBundle(t *testing.T) {
	ctx := context.Background()
	d := testDriver(t)

	warm, err := d.Claim(ctx, substrate.ClaimSpec{ThreadID: "warm"})
	if err != nil {
		t.Fatalf("Claim warm: %v", err)
	}
	baseRef, err := d.SnapshotBase(ctx, warm, "base-homelab-amd64")
	if err != nil {
		t.Fatalf("SnapshotBase: %v", err)
	}
	if !baseRef.Base || baseRef.ID != "base-homelab-amd64" || baseRef.SizeBytes == 0 {
		t.Fatalf("unexpected base ref: %+v", baseRef)
	}
	if err := d.Release(ctx, warm); err != nil {
		t.Fatalf("Release warm: %v", err)
	}

	// New thread starts from the base.
	h, err := d.Claim(ctx, substrate.ClaimSpec{
		ThreadID:        "fresh",
		BaseSnapshotRef: substrate.SnapshotRef{ID: "base-homelab-amd64", Arch: "amd64", Base: true},
	})
	if err != nil {
		t.Fatalf("Claim from base: %v", err)
	}
	if h.ThreadID != "fresh" {
		t.Fatalf("base-started thread id = %q, want fresh", h.ThreadID)
	}
	if d.LiveCount() != 1 {
		t.Fatalf("LiveCount = %d, want 1", d.LiveCount())
	}
}

func TestDriverClaimFromMissingBaseErrors(t *testing.T) {
	d := testDriver(t)
	_, err := d.Claim(context.Background(), substrate.ClaimSpec{
		ThreadID:        "x",
		BaseSnapshotRef: substrate.SnapshotRef{ID: "ghost-base", Arch: "amd64", Base: true},
	})
	if err == nil {
		t.Fatal("claim from a non-existent base bundle should error")
	}
}

func TestDriverExecNotHostProvided(t *testing.T) {
	d := testDriver(t)
	if _, err := d.Exec(context.Background(), substrate.Handle{}, substrate.Request{}); err == nil {
		t.Fatal("Exec should report it is handled by the in-VM harness")
	}
}
