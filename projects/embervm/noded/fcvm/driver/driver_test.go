package driver

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"

	"github.com/jomcgi/homelab/projects/embervm/noded/substrate"
)

// fakeLauncher stands up a fake Firecracker API server on each requested socket,
// so the driver's real fcclient drives a realistic API. CreateSnapshot writes
// the snapfile + memfile to disk so the bundle layout and Restore's existence
// checks are exercised end to end.
type fakeLauncher struct {
	mu       sync.Mutex
	launched int
	requests []string
}

type fakeProcess struct {
	srv    *http.Server
	killed bool
	pid    int
}

func (p *fakeProcess) Kill() error { p.killed = true; return p.srv.Close() }
func (p *fakeProcess) Wait() error { return nil }
func (p *fakeProcess) Pid() int    { return p.pid }

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
	record := func(r *http.Request) {
		l.mu.Lock()
		l.requests = append(l.requests, r.Method+" "+r.URL.Path)
		l.mu.Unlock()
	}
	mux.HandleFunc("/snapshot/create", func(w http.ResponseWriter, r *http.Request) {
		record(r)
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
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		record(r)
		w.WriteHeader(http.StatusNoContent)
	})
	srv := &http.Server{Handler: mux}
	go func() { _ = srv.Serve(ln) }()
	return &fakeProcess{srv: srv}, nil
}

func (l *fakeLauncher) requestPaths() []string {
	l.mu.Lock()
	defer l.mu.Unlock()
	return append([]string(nil), l.requests...)
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

// testDriverWithVendor mirrors testDriver but stamps a CPU vendor, for the R7
// vendor-mismatch tests (mirroring the arch-mismatch tests above).
func testDriverWithVendor(t *testing.T, vendor string) *Driver {
	t.Helper()
	return New(Config{
		KernelImagePath: "/opt/kata/vmlinux",
		RootfsPath:      "/dev/mapper/thread",
		SnapshotRoot:    shortTempDir(t),
		Node:            "node-4",
		Arch:            "amd64",
		Vendor:          vendor,
	}, &fakeLauncher{}, nil)
}

// testDriverWithSku mirrors testDriverWithVendor but stamps a full cpu_sku
// (vendor, template), for the PR-E cpu_sku mismatch/grandfather tests.
func testDriverWithSku(t *testing.T, vendor, template string) *Driver {
	t.Helper()
	return New(Config{
		KernelImagePath: "/opt/kata/vmlinux",
		RootfsPath:      "/dev/mapper/thread",
		SnapshotRoot:    shortTempDir(t),
		Node:            "node-4",
		Arch:            "amd64",
		Vendor:          vendor,
		Template:        template,
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

func TestDriverClaimWithVolumeColdBootsWhenWarmRestoreDisabled(t *testing.T) {
	launcher := &fakeLauncher{}
	d := New(Config{
		KernelImagePath: "/opt/kata/vmlinux",
		RootfsPath:      "/dev/mapper/thread",
		SnapshotRoot:    shortTempDir(t),
		Node:            "node-4",
		Arch:            "amd64",
	}, launcher, nil)
	h, err := d.Claim(context.Background(), substrate.ClaimSpec{
		ThreadID:        "cold-volume",
		BaseSnapshotRef: substrate.SnapshotRef{ID: "base"},
		VolumeDiskPath:  "/sessions/s1/workspace.img",
	})
	if err != nil {
		t.Fatalf("Claim: %v", err)
	}
	t.Cleanup(func() { _ = d.Release(context.Background(), h) })
	paths := launcher.requestPaths()
	for _, path := range paths {
		if path == "PUT /snapshot/load" {
			t.Fatalf("disabled warm restore unexpectedly loaded a snapshot: %v", paths)
		}
	}
	foundVolume := false
	for _, path := range paths {
		if path == "PUT /drives/volume" {
			foundVolume = true
		}
	}
	if !foundVolume {
		t.Fatalf("cold boot did not attach volume: %v", paths)
	}
}

func TestDriverClaimWithVolumeLoadPatchResume(t *testing.T) {
	launcher := &fakeLauncher{}
	root := shortTempDir(t)
	d := New(Config{
		KernelImagePath:       "/opt/kata/vmlinux",
		RootfsPath:            "/dev/mapper/thread",
		SnapshotRoot:          root,
		Node:                  "node-4",
		Arch:                  "amd64",
		WarmRestoreWithVolume: true,
	}, launcher, nil)
	if err := os.MkdirAll(d.baseDir("base"), 0o750); err != nil {
		t.Fatalf("mkdir base: %v", err)
	}
	if err := os.WriteFile(d.baseSnapfile("base"), []byte("snap"), 0o600); err != nil {
		t.Fatalf("write snapfile: %v", err)
	}
	h, err := d.Claim(context.Background(), substrate.ClaimSpec{
		ThreadID:        "warm-volume",
		BaseSnapshotRef: substrate.SnapshotRef{ID: "base", Arch: "amd64"},
		VolumeDiskPath:  "/sessions/s1/workspace.img",
	})
	if err != nil {
		t.Fatalf("Claim: %v", err)
	}
	t.Cleanup(func() { _ = d.Release(context.Background(), h) })
	if h.ID == "" {
		t.Fatal("Claim returned empty handle")
	}
	paths := launcher.requestPaths()
	want := []string{"PUT /snapshot/load", "PATCH /drives/volume", "PATCH /vm"}
	if len(paths) != len(want) {
		t.Fatalf("request paths = %v, want %v", paths, want)
	}
	for i := range want {
		if paths[i] != want[i] {
			t.Fatalf("request path[%d] = %q, want %q", i, paths[i], want[i])
		}
	}
}

func TestDriverWarmRestoreFlagAttachesBasePlaceholderVolume(t *testing.T) {
	launcher := &fakeLauncher{}
	root := shortTempDir(t)
	d := New(Config{
		KernelImagePath:       "/opt/kata/vmlinux",
		RootfsPath:            "/dev/mapper/thread",
		SnapshotRoot:          root,
		Node:                  "node-4",
		Arch:                  "amd64",
		WarmRestoreWithVolume: true,
	}, launcher, nil)
	h, err := d.Claim(context.Background(), substrate.ClaimSpec{ThreadID: "base-build"})
	if err != nil {
		t.Fatalf("Claim: %v", err)
	}
	t.Cleanup(func() { _ = d.Release(context.Background(), h) })
	placeholder := filepath.Join(d.threadDir(h.ThreadID), "placeholder-volume.img")
	info, err := os.Stat(placeholder)
	if err != nil {
		t.Fatalf("placeholder volume: %v", err)
	}
	if info.Size() != 16*1024*1024 {
		t.Fatalf("placeholder size = %d, want %d", info.Size(), 16*1024*1024)
	}
	paths := launcher.requestPaths()
	foundVolume := false
	for _, path := range paths {
		if path == "PUT /drives/volume" {
			foundVolume = true
		}
	}
	if !foundVolume {
		t.Fatalf("warm base cold boot did not attach placeholder volume: %v", paths)
	}
}

func TestDriverClaimedMibProjectsLiveMap(t *testing.T) {
	d := testDriver(t)
	ctx := context.Background()
	handles := make([]substrate.Handle, 0, 4)
	h, err := d.Claim(ctx, substrate.ClaimSpec{ThreadID: "claimed-task"})
	if err != nil {
		t.Fatalf("Claim: %v", err)
	}
	handles = append(handles, h)
	for _, claim := range []func() (substrate.Handle, error){
		func() (substrate.Handle, error) {
			return d.ClaimServing(ctx, "", "", 1, 200, substrate.NICSpec{HostDevName: "tap-serving"}, "", 0)
		},
		func() (substrate.Handle, error) {
			return d.ClaimStateful(ctx, "", "", 1, 300, substrate.NICSpec{HostDevName: "tap-stateful"}, "", 0, "/volume", "/data", nil)
		},
		func() (substrate.Handle, error) {
			return d.ClaimGroupMember(ctx, "", "", 1, 400, substrate.NICSpec{HostDevName: "tap-group"}, nil)
		},
	} {
		h, err := claim()
		if err != nil {
			t.Fatalf("mixed claim path: %v", err)
		}
		handles = append(handles, h)
	}
	if got, want := d.ClaimedMib(), uint64(d.cfg.MemMib+200+300+400); got != want {
		t.Fatalf("ClaimedMib = %d, want %d", got, want)
	}
	for _, h := range handles {
		if err := d.Release(ctx, h); err != nil {
			t.Fatalf("Release %s: %v", h.ID, err)
		}
	}
	if got := d.ClaimedMib(); got != 0 {
		t.Fatalf("ClaimedMib after releasing all live entries = %d, want 0", got)
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

// TestDriverRestoreRejectsVendorMismatch proves a restore whose ref.Vendor
// disagrees with the node's own CPU vendor fails closed exactly like an Arch
// mismatch (R7, standing decisions 1 and 11).
func TestDriverRestoreRejectsVendorMismatch(t *testing.T) {
	d := testDriverWithVendor(t, "intel")
	_, err := d.Restore(context.Background(), substrate.SnapshotRef{ThreadID: "x", Arch: "amd64", Node: "node-4", Vendor: "amd"})
	if err == nil {
		t.Fatal("restore should reject a vendor-mismatched snapshot (non-portable across CPU vendor)")
	}
}

// TestDriverRestoreAllowsLegacyEmptyVendorOnAMD proves standing decision 11's
// node-4 alias: a pre-R7 snapshot with an empty Vendor restores cleanly on an
// AMD node (aliased to "amd") without a mismatch error, so an existing base or
// bundle never needlessly fails merely for predating vendor keying.
func TestDriverRestoreAllowsLegacyEmptyVendorOnAMD(t *testing.T) {
	d := testDriverWithVendor(t, "amd")
	ctx := context.Background()
	h, err := d.Claim(ctx, substrate.ClaimSpec{ThreadID: "t-legacy"})
	if err != nil {
		t.Fatalf("Claim: %v", err)
	}
	ref, err := d.Snapshot(ctx, h)
	if err != nil {
		t.Fatalf("Snapshot: %v", err)
	}
	ref.Vendor = "" // simulate a pre-R7 snapshot with no vendor stamped
	if err := d.Release(ctx, h); err != nil {
		t.Fatalf("Release: %v", err)
	}
	if _, err := d.Restore(ctx, ref); err != nil {
		t.Fatalf("restore of a legacy empty-vendor ref on an amd node should succeed (node-4 alias): %v", err)
	}
}

// TestDriverRestoreRejectsLegacyEmptyVendorOnIntel proves the node-4 alias is
// AMD-specific: a pre-R7 (empty-vendor) snapshot restored on an Intel node still
// fails closed, since aliasing legacy refs to "amd" must never let one silently
// cross onto the Intel tier.
func TestDriverRestoreRejectsLegacyEmptyVendorOnIntel(t *testing.T) {
	d := testDriverWithVendor(t, "intel")
	_, err := d.Restore(context.Background(), substrate.SnapshotRef{ThreadID: "x", Arch: "amd64", Node: "node-4", Vendor: ""})
	if err == nil {
		t.Fatal("restore of a legacy empty-vendor ref on an intel node should fail (alias is amd-only)")
	}
}

// TestDriverSnapshotStampsVendor proves Snapshot stamps the node's own vendor
// into the produced ref, mirroring how it stamps Arch.
func TestDriverSnapshotStampsVendor(t *testing.T) {
	ctx := context.Background()
	d := testDriverWithVendor(t, "intel")
	h, err := d.Claim(ctx, substrate.ClaimSpec{ThreadID: "t-vendor-stamp"})
	if err != nil {
		t.Fatalf("Claim: %v", err)
	}
	ref, err := d.Snapshot(ctx, h)
	if err != nil {
		t.Fatalf("Snapshot: %v", err)
	}
	if ref.Vendor != "intel" {
		t.Fatalf("snapshot ref Vendor = %q, want intel", ref.Vendor)
	}
}

// TestDriverSnapshotStampsTemplate proves Snapshot stamps the node's own CPU
// template into the produced ref (PR-E), mirroring how it stamps Vendor. Run
// for both fleet vendors: the template pin must be correct-by-construction on
// Intel even though no live Intel silicon can exercise it yet.
func TestDriverSnapshotStampsTemplate(t *testing.T) {
	for _, tc := range []struct{ vendor, template string }{
		{"amd", "amd-default"},
		{"intel", "t2-conservative"},
	} {
		t.Run(tc.vendor, func(t *testing.T) {
			ctx := context.Background()
			d := testDriverWithSku(t, tc.vendor, tc.template)
			h, err := d.Claim(ctx, substrate.ClaimSpec{ThreadID: "t-sku-stamp-" + tc.vendor})
			if err != nil {
				t.Fatalf("Claim: %v", err)
			}
			ref, err := d.Snapshot(ctx, h)
			if err != nil {
				t.Fatalf("Snapshot: %v", err)
			}
			if ref.Vendor != tc.vendor {
				t.Fatalf("snapshot ref Vendor = %q, want %q", ref.Vendor, tc.vendor)
			}
			if ref.Template != tc.template {
				t.Fatalf("snapshot ref Template = %q, want %q", ref.Template, tc.template)
			}
		})
	}
}

// TestDriverRestoreAllowsUnstampedTemplateGrandfathered proves the PR-E
// grandfather rule at the driver layer: a ref with an EMPTY Template (a legacy
// artifact cut before template stamping existed, or one from a pre-PR-E
// daemon) restores cleanly regardless of the node's own template, and is NEVER
// refused for the missing stamp. Refusing a grandfathered artifact would be
// data loss, the exact failure this rule exists to prevent. Checked for both
// fleet vendors.
func TestDriverRestoreAllowsUnstampedTemplateGrandfathered(t *testing.T) {
	for _, tc := range []struct{ vendor, template string }{
		{"amd", "amd-default"},
		{"intel", "t2-conservative"},
	} {
		t.Run(tc.vendor, func(t *testing.T) {
			d := testDriverWithSku(t, tc.vendor, tc.template)
			ctx := context.Background()
			h, err := d.Claim(ctx, substrate.ClaimSpec{ThreadID: "t-grandfathered-" + tc.vendor})
			if err != nil {
				t.Fatalf("Claim: %v", err)
			}
			ref, err := d.Snapshot(ctx, h)
			if err != nil {
				t.Fatalf("Snapshot: %v", err)
			}
			ref.Template = "" // simulate a pre-PR-E snapshot with no template stamped
			if err := d.Release(ctx, h); err != nil {
				t.Fatalf("Release: %v", err)
			}
			if _, err := d.Restore(ctx, ref); err != nil {
				t.Fatalf("restore of an unstamped-template ref should succeed (grandfathered): %v", err)
			}
		})
	}
}

// TestDriverRestoreAllowsMatchedSku proves a restore whose ref carries the
// SAME (vendor, template) as the node succeeds, for both fleet vendors: a
// present-and-matching stamp is the ordinary, non-legacy compatible case.
func TestDriverRestoreAllowsMatchedSku(t *testing.T) {
	for _, tc := range []struct{ vendor, template string }{
		{"amd", "amd-default"},
		{"intel", "t2-conservative"},
	} {
		t.Run(tc.vendor, func(t *testing.T) {
			d := testDriverWithSku(t, tc.vendor, tc.template)
			ctx := context.Background()
			h, err := d.Claim(ctx, substrate.ClaimSpec{ThreadID: "t-matched-" + tc.vendor})
			if err != nil {
				t.Fatalf("Claim: %v", err)
			}
			ref, err := d.Snapshot(ctx, h)
			if err != nil {
				t.Fatalf("Snapshot: %v", err)
			}
			if err := d.Release(ctx, h); err != nil {
				t.Fatalf("Release: %v", err)
			}
			if _, err := d.Restore(ctx, ref); err != nil {
				t.Fatalf("restore of a matched-sku ref should succeed: %v", err)
			}
		})
	}
}

// TestDriverRestoreRejectsMismatchedTemplate proves a PRESENT-but-mismatched
// template is refused fail-closed (loudly), never a silent wrong-template
// boot, exactly like a vendor mismatch. Checked for both fleet vendors: the
// mismatch gate must be correct-by-construction on the Intel pool even without
// live silicon to exercise it.
func TestDriverRestoreRejectsMismatchedTemplate(t *testing.T) {
	for _, tc := range []struct{ vendor, nodeTemplate, refTemplate string }{
		{"amd", "amd-default", "amd-other"},
		{"intel", "t2-conservative", "t2s-experimental"},
	} {
		t.Run(tc.vendor, func(t *testing.T) {
			d := testDriverWithSku(t, tc.vendor, tc.nodeTemplate)
			_, err := d.Restore(context.Background(), substrate.SnapshotRef{
				ThreadID: "x", Arch: "amd64", Node: "node-4", Vendor: tc.vendor, Template: tc.refTemplate,
			})
			if err == nil {
				t.Fatalf("restore should reject a template-mismatched snapshot (%s: %q != %q)", tc.vendor, tc.refTemplate, tc.nodeTemplate)
			}
		})
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
	baseDir := d.baseDir("base-homelab-amd64")
	for _, name := range []string{"snapfile", "memfile"} {
		if _, err := os.Stat(filepath.Join(baseDir, name)); err != nil {
			t.Fatalf("final base %s missing: %v", name, err)
		}
	}
	if _, err := os.Stat(baseDir + ".building"); !os.IsNotExist(err) {
		t.Fatalf("base staging dir still exists, stat err=%v", err)
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

// TestDriverSessionSnapshotRestoreRoundTrip proves the R2 session bank/relight
// mechanic on the real driver: snapshot a live session VM into a self-contained
// bundle under sessions/<ref> (SnapshotSession, no resume, so the caller destroys),
// then relight a fresh VM from that bundle (RestoreSession). It REUSES the base
// bundle format exactly (memfile + snapfile), so the session bundle is portable and
// restorable just like a base. Finally RemoveSessionBundle reclaims the bundle and
// a subsequent relight fails (the bundle is gone).
func TestDriverSessionSnapshotRestoreRoundTrip(t *testing.T) {
	ctx := context.Background()
	d := testDriver(t)

	h, err := d.Claim(ctx, substrate.ClaimSpec{ThreadID: "sess-live"})
	if err != nil {
		t.Fatalf("Claim: %v", err)
	}
	ref, err := d.SnapshotSession(ctx, h, "sref-abc123")
	if err != nil {
		t.Fatalf("SnapshotSession: %v", err)
	}
	if ref.ID != "sref-abc123" || ref.Node != "node-4" || ref.Arch != "amd64" || ref.SizeBytes == 0 {
		t.Fatalf("unexpected session ref: %+v", ref)
	}
	if ref.Base {
		t.Fatalf("a session snapshot must not be flagged Base")
	}
	// The bundle lives under sessions/<ref>, never bases/ or a per-thread dir.
	snap := d.sessionSnapfile("sref-abc123")
	if _, err := os.Stat(snap); err != nil {
		t.Fatalf("session bundle snapfile missing under sessions/: %v", err)
	}
	// SnapshotSession does not resume; the caller destroys the VM.
	if err := d.Release(ctx, h); err != nil {
		t.Fatalf("Release banked VM: %v", err)
	}
	if d.LiveCount() != 0 {
		t.Fatalf("LiveCount after bank+release = %d, want 0", d.LiveCount())
	}

	// Relight a fresh VM from the banked bundle: a new microVM id and a fresh thread.
	h2, err := d.RestoreSession(ctx, "sref-abc123")
	if err != nil {
		t.Fatalf("RestoreSession: %v", err)
	}
	if h2.ID == "" || h2.ID == h.ID {
		t.Fatalf("relit VM should have a fresh id, got %q (was %q)", h2.ID, h.ID)
	}
	if d.LiveCount() != 1 {
		t.Fatalf("LiveCount after relight = %d, want 1", d.LiveCount())
	}
	if err := d.Release(ctx, h2); err != nil {
		t.Fatalf("Release relit VM: %v", err)
	}

	// Evict the bundle; a subsequent relight then fails (the state is gone).
	if err := d.RemoveSessionBundle("sref-abc123"); err != nil {
		t.Fatalf("RemoveSessionBundle: %v", err)
	}
	if _, err := os.Stat(d.sessionDir("sref-abc123")); !os.IsNotExist(err) {
		t.Fatalf("session bundle dir should be gone after evict, stat err=%v", err)
	}
	if _, err := d.RestoreSession(ctx, "sref-abc123"); err == nil {
		t.Fatal("relight of an evicted session bundle should error")
	}
	// Idempotent evict: removing an already-gone bundle is not an error.
	if err := d.RemoveSessionBundle("sref-abc123"); err != nil {
		t.Fatalf("idempotent RemoveSessionBundle: %v", err)
	}
}

func TestDriverExecNotHostProvided(t *testing.T) {
	d := testDriver(t)
	if _, err := d.Exec(context.Background(), substrate.Handle{}, substrate.Request{}); err == nil {
		t.Fatal("Exec should report it is handled by the in-VM harness")
	}
}

// TestBootArgsForServingNIC asserts the serving cold-boot kernel ip= directive is
// appended for a NIC, and that a nil NIC (task/session) leaves the boot args
// byte-unchanged (the additive-boot invariant).
func TestBootArgsForServingNIC(t *testing.T) {
	d := New(Config{
		KernelBootArgs: "console=ttyS0",
		HarnessInit:    "/init",
	}, &fakeLauncher{}, nil)

	// Empty spec (nil NIC, no handler disk): task/session boot args, no ip= directive.
	got := d.bootArgsFor(coldBootSpec{})
	want := "console=ttyS0 init=/init"
	if got != want {
		t.Fatalf("bootArgsFor(empty) = %q, want %q", got, want)
	}

	// A per-boot harnessInit overrides the driver-global init= (the serving cold-boot
	// resolves the guest-init path from the runtime image it boots).
	if override := d.bootArgsFor(coldBootSpec{harnessInit: "/usr/bin/ember-guest-init"}); override != "console=ttyS0 init=/usr/bin/ember-guest-init" {
		t.Fatalf("bootArgsFor(harnessInit override) = %q, want per-boot init=", override)
	}

	// Regression guard for the serving cold-boot bug: with the driver-global HarnessInit
	// EMPTY (as on the daemon driver), a per-boot harnessInit MUST still emit init= or
	// the kernel drops to /bin/sh and the shim never runs.
	dNoGlobal := New(Config{KernelBootArgs: "console=ttyS0"}, &fakeLauncher{}, nil)
	if none := dNoGlobal.bootArgsFor(coldBootSpec{}); none != "console=ttyS0" {
		t.Fatalf("bootArgsFor(no global, no per-boot) = %q, want no init=", none)
	}
	if serving := dNoGlobal.bootArgsFor(coldBootSpec{harnessInit: "/init"}); serving != "console=ttyS0 init=/init" {
		t.Fatalf("bootArgsFor(no global, per-boot init) = %q, want init=/init", serving)
	}

	// Serving NIC with no ServingPort (0): appends the ip= directive with a dotted
	// netmask and NO ember.serving_port= token (the zero-guard: a serving VM whose
	// port is unset stays on the vsock path).
	nicArgs := d.bootArgsFor(coldBootSpec{nic: &substrate.NICSpec{
		IP:        "172.31.0.2",
		GatewayIP: "172.31.0.1",
		PrefixLen: 24,
		IfaceName: "eth0",
	}})
	wantSuffix := " ip=172.31.0.2::172.31.0.1:255.255.255.0::eth0:off"
	if nicArgs != want+wantSuffix {
		t.Fatalf("bootArgsFor(nic) = %q, want %q", nicArgs, want+wantSuffix)
	}
	if strings.Contains(nicArgs, "ember.serving_port") {
		t.Fatalf("bootArgsFor(nic, ServingPort=0) = %q, want no ember.serving_port token", nicArgs)
	}

	// Serving NIC WITH a ServingPort (R3, D-R3.11.1): appends the
	// ember.serving_port= token after ip=, so guest-init flips the shim to TCP on
	// exactly that port (the same port the daemon health-probes and publishes).
	servingArgs := d.bootArgsFor(coldBootSpec{nic: &substrate.NICSpec{
		IP:          "172.31.0.2",
		GatewayIP:   "172.31.0.1",
		PrefixLen:   24,
		IfaceName:   "eth0",
		ServingPort: 8080,
	}})
	wantServing := want + wantSuffix + " ember.serving_port=8080"
	if servingArgs != wantServing {
		t.Fatalf("bootArgsFor(nic, ServingPort=8080) = %q, want %q", servingArgs, wantServing)
	}

	// Serving cold boot WITH a handler disk (R3, D-R3.11.2): appends
	// ember.handler_disk=<dev> and the EXACT ember.handler_zip_bytes=<N> so the
	// guest reads only the zip payload and not the block device's sector padding.
	handlerArgs := d.bootArgsFor(coldBootSpec{
		nic: &substrate.NICSpec{
			IP:          "172.31.0.2",
			GatewayIP:   "172.31.0.1",
			PrefixLen:   24,
			IfaceName:   "eth0",
			ServingPort: 8080,
		},
		handlerDiskPath: "/disks/bases/wl__abc/handler.zip",
		handlerZipBytes: 4096,
	})
	wantHandler := wantServing + " ember.handler_disk=/dev/vdb ember.handler_zip_bytes=4096"
	if handlerArgs != wantHandler {
		t.Fatalf("bootArgsFor(handler disk) = %q, want %q", handlerArgs, wantHandler)
	}

	// A handler disk with no NIC (defensive: never happens in practice, a serving
	// boot always has a NIC) still emits the handler tokens and no ip= directive,
	// so the two branches are independent.
	handlerOnly := d.bootArgsFor(coldBootSpec{
		handlerDiskPath: "/disks/bases/wl__abc/handler.zip",
		handlerZipBytes: 10,
	})
	if strings.Contains(handlerOnly, "ip=") {
		t.Fatalf("bootArgsFor(handler, no nic) = %q, want no ip= directive", handlerOnly)
	}
	if !strings.Contains(handlerOnly, "ember.handler_disk=/dev/vdb ember.handler_zip_bytes=10") {
		t.Fatalf("bootArgsFor(handler, no nic) = %q, want handler tokens", handlerOnly)
	}
}

// TestBootArgsForStatefulVolumeDevice covers the R4 image-lane fix: the writable
// volume's device letter follows the ACTUAL drive attach order. Without a handler
// drive (an image-lane, opaque-L4 stateful guest like Postgres) the volume is
// drive 2 and lands on /dev/vdb; with a handler drive present the handler is drive
// 2 (/dev/vdb) and the volume shifts to drive 3 (/dev/vdc). A fixed device would
// signal a nonexistent block device on the handler-less path and guest-init would
// mount nothing.
func TestBootArgsForStatefulVolumeDevice(t *testing.T) {
	d := New(Config{KernelBootArgs: "console=ttyS0"}, &fakeLauncher{}, nil)

	// No handler drive: volume is drive 2 -> /dev/vdb.
	noHandler := d.bootArgsFor(coldBootSpec{
		volumeDiskPath: "/disks/vol/scratch-postgres.img",
		volumeMount:    "/data",
	})
	if !strings.Contains(noHandler, "ember.volume_dev=/dev/vdb ember.volume_mount=/data") {
		t.Fatalf("bootArgsFor(volume, no handler) = %q, want ember.volume_dev=/dev/vdb", noHandler)
	}
	if strings.Contains(noHandler, "ember.handler_disk") {
		t.Fatalf("bootArgsFor(volume, no handler) = %q, want no handler tokens", noHandler)
	}

	// With a handler drive: handler is drive 2 (/dev/vdb), volume shifts to /dev/vdc.
	withHandler := d.bootArgsFor(coldBootSpec{
		handlerDiskPath: "/disks/bases/wl/handler.zip",
		handlerZipBytes: 2048,
		volumeDiskPath:  "/disks/vol/scratch-postgres.img",
		volumeMount:     "/data",
	})
	if !strings.Contains(withHandler, "ember.volume_dev=/dev/vdc ember.volume_mount=/data") {
		t.Fatalf("bootArgsFor(volume, with handler) = %q, want ember.volume_dev=/dev/vdc", withHandler)
	}
}

// TestBootArgsForMmdsEnv covers the R4 D-R4.PR-7.1 MMDS-lite seam: mmdsEnv
// entries are rendered as sorted, base64url-encoded ember.env.<KEY>= tokens; an
// invalid key is skipped; an empty/nil map emits nothing (so RELIGHT, which
// never sets mmdsEnv, carries no ember.env.* tokens and the boot args stay
// byte-unchanged from before this feature).
func TestBootArgsForMmdsEnv(t *testing.T) {
	d := New(Config{KernelBootArgs: "console=ttyS0"}, &fakeLauncher{}, nil)

	// Nil/empty mmdsEnv: no ember.env.* tokens at all (covers the RELIGHT path,
	// which never sets this field on coldBootSpec).
	if got := d.bootArgsFor(coldBootSpec{}); strings.Contains(got, "ember.env.") {
		t.Fatalf("bootArgsFor(no mmdsEnv) = %q, want no ember.env.* tokens", got)
	}

	// Multiple keys: sorted, base64url-encoded, space-separated.
	got := d.bootArgsFor(coldBootSpec{mmdsEnv: map[string]string{
		"POSTGRES_PASSWORD": "hunter2",
		"POSTGRES_USER":     "app",
	}})
	wantPassword := "ember.env.POSTGRES_PASSWORD=" + base64.RawURLEncoding.EncodeToString([]byte("hunter2"))
	wantUser := "ember.env.POSTGRES_USER=" + base64.RawURLEncoding.EncodeToString([]byte("app"))
	want := "console=ttyS0 " + wantPassword + " " + wantUser
	if got != want {
		t.Fatalf("bootArgsFor(mmdsEnv) = %q, want %q", got, want)
	}

	// Round-trip: the encoded value decodes back to the original secret.
	decoded, err := base64.RawURLEncoding.DecodeString(base64.RawURLEncoding.EncodeToString([]byte("hunter2")))
	if err != nil || string(decoded) != "hunter2" {
		t.Fatalf("base64url round-trip failed: decoded=%q err=%v", decoded, err)
	}

	// An invalid key (not [A-Za-z0-9_]) is skipped, not fatal, and does not
	// corrupt the valid entries.
	mixed := d.bootArgsFor(coldBootSpec{mmdsEnv: map[string]string{
		"VALID_KEY":   "ok",
		"bad key!":    "skipped",
		"also=equals": "skipped",
	}})
	if strings.Contains(mixed, "bad key") || strings.Contains(mixed, "also=equals") {
		t.Fatalf("bootArgsFor(mmdsEnv with invalid keys) = %q, want invalid keys skipped", mixed)
	}
	wantValid := "ember.env.VALID_KEY=" + base64.RawURLEncoding.EncodeToString([]byte("ok"))
	if !strings.Contains(mixed, wantValid) {
		t.Fatalf("bootArgsFor(mmdsEnv with invalid keys) = %q, want %q present", mixed, wantValid)
	}
}

// TestMmdsEnvKeyNamesRedactsValues asserts the logging helper returns ONLY
// sorted key names, never values, so a caller logging its output cannot
// accidentally leak an mmds_env secret (D-R4.PR-7.1's redaction requirement).
func TestMmdsEnvKeyNamesRedactsValues(t *testing.T) {
	keys := mmdsEnvKeyNames(map[string]string{
		"POSTGRES_PASSWORD": "hunter2",
		"POSTGRES_USER":     "app",
	})
	want := []string{"POSTGRES_PASSWORD", "POSTGRES_USER"}
	if len(keys) != len(want) {
		t.Fatalf("mmdsEnvKeyNames() = %v, want %v", keys, want)
	}
	for i := range want {
		if keys[i] != want[i] {
			t.Fatalf("mmdsEnvKeyNames() = %v, want %v", keys, want)
		}
	}
	for _, k := range keys {
		if k == "hunter2" || k == "app" {
			t.Fatalf("mmdsEnvKeyNames() leaked a value: %v", keys)
		}
	}
}

func TestPrefixLenToMask(t *testing.T) {
	for _, tc := range []struct {
		prefix int
		want   string
	}{
		{24, "255.255.255.0"},
		{16, "255.255.0.0"},
		{29, "255.255.255.248"},
	} {
		if got := prefixLenToMask(tc.prefix); got != tc.want {
			t.Errorf("prefixLenToMask(%d) = %q want %q", tc.prefix, got, tc.want)
		}
	}
}

// TestClaimServingBootsWithNIC cold-boots a serving VM and asserts it lands in the
// live map (counted against LiveCount like any VM). The fake FC server's catch-all
// accepts PUT /network-interfaces, so a successful boot proves the NIC step ran.
func TestClaimServingBootsWithNIC(t *testing.T) {
	d := testDriver(t)
	h, err := d.ClaimServing(context.Background(), "/rootfs/serve", "/init", 2, 512, substrate.NICSpec{
		HostDevName: "emtap0002",
		IP:          "172.31.0.2",
		GatewayIP:   "172.31.0.1",
		PrefixLen:   24,
	}, "", 0)
	if err != nil {
		t.Fatalf("ClaimServing: %v", err)
	}
	if h.ID == "" || h.Node != "node-4" {
		t.Fatalf("unexpected serving handle: %+v", h)
	}
	if d.LiveCount() != 1 {
		t.Fatalf("serving VM not counted in LiveCount: %d", d.LiveCount())
	}
}

func TestClaimServingRequiresTap(t *testing.T) {
	d := testDriver(t)
	if _, err := d.ClaimServing(context.Background(), "/rootfs", "", 1, 128, substrate.NICSpec{}, "", 0); err == nil {
		t.Fatal("ClaimServing without a host tap device should error")
	}
}

// TestClaimServingWithHandlerDiskBoots proves the D-R3.11.2 serving cold boot with a
// second (handler) drive attaches it without error and lands a live VM. The fake FC
// API accepts every PUT /drives, so a successful boot proves the second-drive step ran.
func TestClaimServingWithHandlerDiskBoots(t *testing.T) {
	d := testDriver(t)
	h, err := d.ClaimServing(context.Background(), "/rootfs/serve", "/init", 1, 256,
		substrate.NICSpec{HostDevName: "emtap0003", IP: "172.31.0.3", GatewayIP: "172.31.0.1", PrefixLen: 24, ServingPort: 8080},
		"/disks/bases/wl__abc/handler.zip", 4096)
	if err != nil {
		t.Fatalf("ClaimServing with handler disk: %v", err)
	}
	if h.ID == "" || d.LiveCount() != 1 {
		t.Fatalf("serving VM with handler disk not live: %+v live=%d", h, d.LiveCount())
	}
}

// TestWriteAndScanServingHandlerArtifact round-trips the host-side handler artifact
// write and the startup rescan: the write persists handler.zip + a runtime.ref
// sidecar, and the rescan re-discovers the base key, path, runtime ref, and exact size.
func TestWriteAndScanServingHandlerArtifact(t *testing.T) {
	d := testDriver(t)
	zip := []byte("PK\x03\x04 fake zip bytes")
	path, n, err := d.WriteServingHandlerArtifact("wl__abc", "python312@sha256:deadbeef", zip)
	if err != nil {
		t.Fatalf("WriteServingHandlerArtifact: %v", err)
	}
	if n != int64(len(zip)) {
		t.Fatalf("written bytes = %d want %d", n, len(zip))
	}
	// The on-disk file MUST be padded up to a whole 512-byte sector: Firecracker
	// floors a sub-sector drive and drops the remainder, which would truncate the
	// zip's EOCD-bearing tail and short-read the guest. The returned length stays
	// exact (asserted above) so the guest reads only the real payload.
	if fi, serr := os.Stat(path); serr != nil {
		t.Fatalf("stat artifact: %v", serr)
	} else if fi.Size()%512 != 0 || fi.Size() < int64(len(zip)) {
		t.Fatalf("artifact file size %d must be a whole 512-byte sector and >= zip len %d", fi.Size(), len(zip))
	}
	if _, ok := d.ServingHandlerArtifactPath("wl__abc"); !ok {
		t.Fatal("ServingHandlerArtifactPath should find the just-written artifact")
	}
	got := d.ScanServingHandlerArtifacts()
	if len(got) != 1 {
		t.Fatalf("rescan found %d artifacts want 1", len(got))
	}
	a := got[0]
	if a.BaseKey != "wl__abc" || a.Path != path || a.RuntimeImageRef != "python312@sha256:deadbeef" || a.SizeBytes != int64(len(zip)) {
		t.Fatalf("rescanned artifact mismatch: %+v", a)
	}
}

// TestServingSnapshotRoundTrip banks a serving VM (writing the pinned-IP sidecar),
// reads the pin back, restores from the bundle, and evicts it, mirroring the session
// round-trip test but asserting the D-R3.4.1 IP pin is persisted and recovered.
func TestServingSnapshotRoundTrip(t *testing.T) {
	ctx := context.Background()
	d := testDriver(t)
	h, err := d.ClaimServing(ctx, "/rootfs/serve", "/init", 1, 256, substrate.NICSpec{HostDevName: "emtap0002", IP: "172.31.0.2", GatewayIP: "172.31.0.1", PrefixLen: 24}, "", 0)
	if err != nil {
		t.Fatalf("ClaimServing: %v", err)
	}
	ref, err := d.SnapshotServing(ctx, h, "servref-1", "172.31.0.2")
	if err != nil {
		t.Fatalf("SnapshotServing: %v", err)
	}
	if ref.ID != "servref-1" {
		t.Fatalf("ref = %+v", ref)
	}
	// The pinned IP was persisted and is recoverable (rescan / relight re-acquire).
	if got := d.ServingPinnedIP("servref-1"); got != "172.31.0.2" {
		t.Fatalf("ServingPinnedIP = %q want 172.31.0.2", got)
	}
	// Restore resumes from the bundle.
	rh, err := d.RestoreServing(ctx, "servref-1")
	if err != nil {
		t.Fatalf("RestoreServing: %v", err)
	}
	if rh.ID == "" {
		t.Fatal("RestoreServing returned an empty handle")
	}
	// Evict removes the bundle; a subsequent restore fails and the pin is gone.
	if err := d.RemoveServingBundle("servref-1"); err != nil {
		t.Fatalf("RemoveServingBundle: %v", err)
	}
	if d.ServingPinnedIP("servref-1") != "" {
		t.Error("pinned IP should be gone after evict")
	}
	if _, err := d.RestoreServing(ctx, "servref-1"); err == nil {
		t.Fatal("restore of an evicted serving bundle should error")
	}
}

// TestDriverGroupMemberSnapshotRestoreRoundTrip proves the R5 member bank/relight
// mechanic on the real driver: cold-boot a member on a group NIC (ClaimGroupMember),
// snapshot it into a self-contained bundle under group/<set_id>/<member_name>/
// (SnapshotGroupMember, no resume, so the caller destroys), then relight a fresh VM
// from that bundle (RestoreGroupMember). The server, not this driver method,
// writes member.json after the snapshot is complete. RemoveGroupMemberBundle
// reclaims it and a subsequent relight fails.
func TestDriverGroupMemberSnapshotRestoreRoundTrip(t *testing.T) {
	ctx := context.Background()
	d := testDriver(t)

	h, err := d.ClaimGroupMember(ctx, "/rootfs/member", "/init", 1, 256,
		substrate.NICSpec{HostDevName: "emgt0a1b2c", GuestMAC: "02:00:00:00:00:01", IP: "10.101.1.10", GatewayIP: "10.101.1.1", PrefixLen: 24}, map[string]string{"EMBER_GROUP_ROLE": "worker"})
	if err != nil {
		t.Fatalf("ClaimGroupMember: %v", err)
	}
	ref, err := d.SnapshotGroupMember(ctx, h, "set-abc", "worker-0")
	if err != nil {
		t.Fatalf("SnapshotGroupMember: %v", err)
	}
	wantRef := "group/set-abc/worker-0"
	if ref.ID != wantRef || ref.Node != "node-4" || ref.Arch != "amd64" || ref.SizeBytes == 0 {
		t.Fatalf("unexpected member ref: %+v (want ID %q)", ref, wantRef)
	}
	// The bundle lives under group/<set_id>/<member>/, never sessions/ or a per-thread dir.
	if _, err := os.Stat(d.groupMemberSnapfile("set-abc", "worker-0")); err != nil {
		t.Fatalf("member bundle snapfile missing under group/set-abc/worker-0: %v", err)
	}
	// SnapshotGroupMember itself writes no sidecar; the server owns member.json.
	if _, err := os.Stat(filepath.Join(d.groupMemberDir("set-abc", "worker-0"), "ip")); !os.IsNotExist(err) {
		t.Errorf("SnapshotGroupMember unexpectedly wrote an ip sidecar, stat err=%v", err)
	}
	// SnapshotGroupMember does not resume; the caller destroys.
	if err := d.Release(ctx, h); err != nil {
		t.Fatalf("Release banked member: %v", err)
	}

	h2, err := d.RestoreGroupMember(ctx, "set-abc", "worker-0")
	if err != nil {
		t.Fatalf("RestoreGroupMember: %v", err)
	}
	if h2.ID == "" || h2.ID == h.ID {
		t.Fatalf("relit member should have a fresh id, got %q (was %q)", h2.ID, h.ID)
	}
	if err := d.Release(ctx, h2); err != nil {
		t.Fatalf("Release relit member: %v", err)
	}

	if err := d.RemoveGroupMemberBundle("set-abc", "worker-0"); err != nil {
		t.Fatalf("RemoveGroupMemberBundle: %v", err)
	}
	if _, err := d.RestoreGroupMember(ctx, "set-abc", "worker-0"); err == nil {
		t.Fatal("relight of an evicted member bundle should error")
	}
	// Idempotent evict.
	if err := d.RemoveGroupMemberBundle("set-abc", "worker-0"); err != nil {
		t.Fatalf("idempotent RemoveGroupMemberBundle: %v", err)
	}
}

// TestDriverClaimGroupMemberRequiresTap proves ClaimGroupMember refuses a NIC with
// no host tap (a member is always on the group bridge).
func TestDriverClaimGroupMemberRequiresTap(t *testing.T) {
	d := testDriver(t)
	if _, err := d.ClaimGroupMember(context.Background(), "/rootfs/x", "/init", 1, 128, substrate.NICSpec{}, nil); err == nil {
		t.Fatal("ClaimGroupMember without a host tap should error")
	}
}

// TestDriverScanGroupBundleSets proves the startup rescan globs group/*/*/snapfile
// and returns each set with its per-member bundles GROUPED BY set dir, skipping a
// half-written member (a member dir with no snapfile).
func TestDriverScanGroupBundleSets(t *testing.T) {
	ctx := context.Background()
	d := testDriver(t)

	// Bank two members under one set and one under another.
	bank := func(setID, member string) {
		h, err := d.ClaimGroupMember(ctx, "/rootfs/member", "/init", 1, 128,
			substrate.NICSpec{HostDevName: "emgt-" + member, IP: "10.101.1.10"}, nil)
		if err != nil {
			t.Fatalf("ClaimGroupMember %s/%s: %v", setID, member, err)
		}
		if _, err := d.SnapshotGroupMember(ctx, h, setID, member); err != nil {
			t.Fatalf("SnapshotGroupMember %s/%s: %v", setID, member, err)
		}
		_ = d.Release(ctx, h)
	}
	bank("set-1", "worker-0")
	bank("set-1", "worker-1")
	bank("set-2", "leader")
	meta, err := json.Marshal(substrate.GroupBundleMemberMetadata{PinnedIP: "10.101.1.10", Port: 10250})
	if err != nil {
		t.Fatalf("marshal member metadata: %v", err)
	}
	if err := os.WriteFile(filepath.Join(d.groupMemberDir("set-1", "worker-0"), substrate.GroupBundleMemberMetadataFile), meta, 0o600); err != nil {
		t.Fatalf("write member metadata: %v", err)
	}

	// A half-written member dir (no snapfile) under set-1 must be skipped.
	if err := os.MkdirAll(d.groupMemberDir("set-1", "ghost"), 0o700); err != nil {
		t.Fatalf("mkdir ghost: %v", err)
	}

	sets := d.ScanGroupBundleSets()
	if len(sets) != 2 {
		t.Fatalf("ScanGroupBundleSets found %d sets, want 2: %+v", len(sets), sets)
	}
	bySet := map[string][]string{}
	byMember := map[string]substrate.GroupBundleMemberInfo{}
	for _, s := range sets {
		for _, m := range s.Members {
			bySet[s.SetID] = append(bySet[s.SetID], m.MemberName)
			byMember[s.SetID+"/"+m.MemberName] = m
			if m.SnapshotRef != filepath.Join("group", s.SetID, m.MemberName) {
				t.Errorf("member ref = %q want group/%s/%s", m.SnapshotRef, s.SetID, m.MemberName)
			}
			if m.SizeBytes == 0 {
				t.Errorf("member %s/%s reported zero size", s.SetID, m.MemberName)
			}
		}
	}
	if len(bySet["set-1"]) != 2 {
		t.Errorf("set-1 members = %v want [worker-0 worker-1] (ghost skipped)", bySet["set-1"])
	}
	if len(bySet["set-2"]) != 1 {
		t.Errorf("set-2 members = %v want [leader]", bySet["set-2"])
	}
	if got := byMember["set-1/worker-0"]; got.PinnedIP != "10.101.1.10" || got.Port != 10250 {
		t.Errorf("worker-0 metadata = %+v, want pinned IP and port", got)
	}
	if got := byMember["set-1/worker-1"]; got.PinnedIP != "" || got.Port != 0 {
		t.Errorf("legacy worker-1 metadata = %+v, want empty values", got)
	}
}

// ---- interruptible-bank checkpoint/resolve (ADR embervm/008) -----------------

// statefulCheckpointHandle boots a live VM the checkpoint tests pause. Claim gives
// a handle whose fake client answers Pause/CreateSnapshot/Resume over the socket,
// which is all CheckpointStateful and the resolves need.
func statefulCheckpointHandle(t *testing.T, d *Driver) substrate.Handle {
	t.Helper()
	h, err := d.Claim(context.Background(), substrate.ClaimSpec{ThreadID: "st-ckpt"})
	if err != nil {
		t.Fatalf("Claim: %v", err)
	}
	return h
}

// TestDriverCheckpointCommit is the COMMIT half of the two-phase bank: a
// checkpoint pauses (VM stays live, no bundle yet, temp written OUTSIDE stateful/)
// and the commit publishes the temp as the bundle (snapfile+memfile+gen) and
// destroys the VM.
func TestDriverCheckpointCommit(t *testing.T) {
	ctx := context.Background()
	d := testDriver(t)
	h := statefulCheckpointHandle(t, d)

	token, err := d.CheckpointStateful(ctx, h, "state-commit", 7, "10.0.0.5")
	if err != nil {
		t.Fatalf("CheckpointStateful: %v", err)
	}
	if token == "" {
		t.Fatal("checkpoint should return a non-empty token")
	}
	// The VM is PAUSED, not destroyed, so it still counts as live.
	if d.LiveCount() != 1 {
		t.Fatalf("LiveCount after checkpoint = %d, want 1 (VM paused, not destroyed)", d.LiveCount())
	}
	// No bundle published yet, and the temp is OUTSIDE stateful/ (rescan-invisible).
	if _, err := os.Stat(d.statefulSnapfile("state-commit")); !os.IsNotExist(err) {
		t.Fatalf("no bundle snapfile should exist before commit, stat err=%v", err)
	}
	if len(d.ScanStatefulBundles()) != 0 {
		t.Fatalf("ScanStatefulBundles must not see the checkpoint temp; got %d bundles", len(d.ScanStatefulBundles()))
	}

	ref, err := d.ResolveStatefulCommit(ctx, token)
	if err != nil {
		t.Fatalf("ResolveStatefulCommit: %v", err)
	}
	if ref.ID != "state-commit" || ref.Node != "node-4" || ref.Arch != "amd64" {
		t.Fatalf("unexpected committed ref: %+v", ref)
	}
	if ref.SizeBytes == 0 {
		t.Fatal("committed bundle SizeBytes should reflect the on-disk bundle")
	}
	for _, p := range []string{d.statefulSnapfile("state-commit"), d.statefulMemfile("state-commit"), d.statefulGenfile("state-commit")} {
		if _, err := os.Stat(p); err != nil {
			t.Fatalf("committed bundle file %s missing: %v", p, err)
		}
	}
	gen, _ := os.ReadFile(d.statefulGenfile("state-commit"))
	if string(gen) != "7" {
		t.Fatalf("gen sidecar = %q, want 7", string(gen))
	}
	// The pinned tap IP is published so a relight re-acquires the SAME IP (the
	// resumed guest's baked-in eth0 matches the fresh tap).
	if got := d.StatefulPinnedIP("state-commit"); got != "10.0.0.5" {
		t.Fatalf("StatefulPinnedIP after commit = %q, want 10.0.0.5", got)
	}
	// The VM was destroyed at commit, and the temp is gone.
	if d.LiveCount() != 0 {
		t.Fatalf("LiveCount after commit = %d, want 0 (VM destroyed)", d.LiveCount())
	}
	if _, err := os.Stat(d.checkpointTmpDir(token)); !os.IsNotExist(err) {
		t.Fatalf("checkpoint temp should be gone after commit, stat err=%v", err)
	}
}

// TestDriverCheckpointAbort is the ABORT half: the paused VM is resumed (stays
// live, same process image) and NO bundle is published; the temp is deleted.
func TestDriverCheckpointAbort(t *testing.T) {
	ctx := context.Background()
	d := testDriver(t)
	h := statefulCheckpointHandle(t, d)

	token, err := d.CheckpointStateful(ctx, h, "state-abort", 4, "10.0.0.5")
	if err != nil {
		t.Fatalf("CheckpointStateful: %v", err)
	}
	if err := d.ResolveStatefulAbort(ctx, token); err != nil {
		t.Fatalf("ResolveStatefulAbort: %v", err)
	}
	// Resumed, so still live; no bundle recorded; temp gone.
	if d.LiveCount() != 1 {
		t.Fatalf("LiveCount after abort = %d, want 1 (VM resumed)", d.LiveCount())
	}
	if _, err := os.Stat(d.statefulSnapfile("state-abort")); !os.IsNotExist(err) {
		t.Fatalf("abort must publish no bundle, stat err=%v", err)
	}
	if _, err := os.Stat(d.checkpointTmpDir(token)); !os.IsNotExist(err) {
		t.Fatalf("checkpoint temp should be gone after abort, stat err=%v", err)
	}
}

// TestDriverResolveUnknownTokenErrors: a resolve of an unknown or already-resolved
// token errors (the driver half of single-resolve).
func TestDriverResolveUnknownTokenErrors(t *testing.T) {
	ctx := context.Background()
	d := testDriver(t)
	if _, err := d.ResolveStatefulCommit(ctx, "ckpt-nope"); err == nil {
		t.Fatal("commit of an unknown token should error")
	}
	if err := d.ResolveStatefulAbort(ctx, "ckpt-nope"); err == nil {
		t.Fatal("abort of an unknown token should error")
	}
	// A second resolve of a consumed token also errors.
	h := statefulCheckpointHandle(t, d)
	token, err := d.CheckpointStateful(ctx, h, "state-once", 1, "10.0.0.5")
	if err != nil {
		t.Fatalf("CheckpointStateful: %v", err)
	}
	if err := d.ResolveStatefulAbort(ctx, token); err != nil {
		t.Fatalf("first abort: %v", err)
	}
	if err := d.ResolveStatefulAbort(ctx, token); err == nil {
		t.Fatal("second resolve of a consumed token should error (single-resolve)")
	}
}

// TestDriverGCStatefulCheckpoints sweeps orphaned temps (the noded-restart path),
// and an abort followed by a fresh checkpoint+commit still yields a valid bundle
// (no leaked temp state).
func TestDriverGCStatefulCheckpointsAndReuse(t *testing.T) {
	ctx := context.Background()
	d := testDriver(t)

	// An orphaned temp (a checkpoint whose noded died) is swept on start.
	h := statefulCheckpointHandle(t, d)
	token, err := d.CheckpointStateful(ctx, h, "state-orphan", 2, "10.0.0.5")
	if err != nil {
		t.Fatalf("CheckpointStateful: %v", err)
	}
	if _, err := os.Stat(d.checkpointTmpDir(token)); err != nil {
		t.Fatalf("temp should exist post-checkpoint: %v", err)
	}
	if n := d.GCStatefulCheckpoints(); n != 1 {
		t.Fatalf("GCStatefulCheckpoints removed %d, want 1", n)
	}
	if _, err := os.Stat(d.checkpointTmpDir(token)); !os.IsNotExist(err) {
		t.Fatalf("temp should be swept by GC, stat err=%v", err)
	}
	if n := d.GCStatefulCheckpoints(); n != 0 {
		t.Fatalf("GCStatefulCheckpoints on a clean dir removed %d, want 0", n)
	}

	// Abort, then a fresh checkpoint+commit still banks a valid bundle.
	h2 := statefulCheckpointHandle(t, d)
	tok2, err := d.CheckpointStateful(ctx, h2, "state-reuse", 5, "10.0.0.5")
	if err != nil {
		t.Fatalf("CheckpointStateful reuse: %v", err)
	}
	if err := d.ResolveStatefulAbort(ctx, tok2); err != nil {
		t.Fatalf("abort reuse: %v", err)
	}
	tok3, err := d.CheckpointStateful(ctx, h2, "state-reuse", 6, "10.0.0.5")
	if err != nil {
		t.Fatalf("second CheckpointStateful reuse: %v", err)
	}
	ref, err := d.ResolveStatefulCommit(ctx, tok3)
	if err != nil {
		t.Fatalf("commit reuse: %v", err)
	}
	if ref.ID != "state-reuse" || ref.SizeBytes == 0 {
		t.Fatalf("reuse commit produced an invalid bundle: %+v", ref)
	}
}

// TestWarmthRootLayout proves the per-instance warmth split (brick-capacity
// PR-2.5): a brick (WarmthRoot derived to SnapshotRoot/instances/<pod_uid>) nests
// every WARMTH dir under WarmthRoot while bases stay node-shared on SnapshotRoot;
// a Config with no WarmthRoot (the legacy DaemonSet, and every existing test)
// falls back to SnapshotRoot so the flat pre-brick layout is byte-for-byte
// unchanged.
func TestWarmthRootLayout(t *testing.T) {
	const root = "/scratch/embervm-noded/snapshots"

	brick := New(Config{SnapshotRoot: root, WarmthRoot: root + "/instances/pod-x"}, &fakeLauncher{}, nil)
	warmth := root + "/instances/pod-x"
	brickWant := map[string]string{
		"sessions":             warmth + "/sessions",
		"serving":              warmth + "/serving",
		"stateful":             warmth + "/stateful",
		"group":                warmth + "/group",
		"stateful-checkpoints": warmth + "/stateful-checkpoints",
		"group_networks":       warmth + "/group_networks",
	}
	got := map[string]string{
		"sessions":             brick.SessionsDir(),
		"serving":              brick.ServingDir(),
		"stateful":             brick.StatefulDir(),
		"group":                brick.GroupSetsDir(),
		"stateful-checkpoints": brick.CheckpointsDir(),
		"group_networks":       brick.GroupNetworksDir(),
	}
	for k, want := range brickWant {
		if got[k] != want {
			t.Errorf("brick %s dir = %q, want %q", k, got[k], want)
		}
	}
	// A per-thread bundle also nests under the warmth root.
	if d := brick.threadDir("thread-1"); d != warmth+"/thread-1" {
		t.Errorf("brick threadDir = %q, want %q", d, warmth+"/thread-1")
	}
	// Bases stay NODE-SHARED on SnapshotRoot, never under the instance warmth root.
	if b := brick.baseDir("base-key"); b != root+"/bases/base-key" {
		t.Errorf("brick baseDir = %q, want %q (bases must not move per-instance)", b, root+"/bases/base-key")
	}

	// No WarmthRoot: warmthRoot() falls back to SnapshotRoot (flat, pre-brick).
	ds := New(Config{SnapshotRoot: root}, &fakeLauncher{}, nil)
	if d := ds.SessionsDir(); d != root+"/sessions" {
		t.Errorf("DS SessionsDir = %q, want flat %q", d, root+"/sessions")
	}
	if d := ds.StatefulDir(); d != root+"/stateful" {
		t.Errorf("DS StatefulDir = %q, want flat %q", d, root+"/stateful")
	}
}

func TestBootArgsForCarriesSessionHarnessInit(t *testing.T) {
	// A session cold boot MUST emit init=: without it the kernel finds no init
	// and drops to /bin/sh, which is how the first live session cold boots
	// failed (guest silent, readiness never arrived).
	d := &Driver{}
	args := d.bootArgsFor(coldBootSpec{harnessInit: "/usr/local/bin/ember-runtime-guest-init"})
	if !strings.Contains(args, "init=/usr/local/bin/ember-runtime-guest-init") {
		t.Fatalf("boot args = %q, want the harness init", args)
	}
}
