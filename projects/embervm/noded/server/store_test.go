package server

import (
	"context"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	nodev1 "github.com/jomcgi/homelab/projects/embervm/proto/embervm/node/v1"

	"github.com/jomcgi/homelab/projects/embervm/noded/config"
	"github.com/jomcgi/homelab/projects/embervm/noded/substrate"
)

// fakeStore is an in-memory artifactStore for the R6 server tests: it records
// every artifact export (prefix -> the exported meta) so tests assert an export
// fired, and models restore/evict/present without touching the network. It
// satisfies the server package's artifactStore seam, so the Server holds it
// exactly as it would a real *store.Store.
type fakeStore struct {
	mu sync.Mutex
	// arts maps a store prefix to its exported files (name -> content) and gen.
	arts map[string]fakeArtifact
	// exportCalls counts Export invocations per prefix (skipped or not).
	exportCalls map[string]int
	// reachable is what Reachable reports.
	reachable bool
	// order records prefixes in export order (for asserting meta-last is N/A here;
	// we assert on the fact of export, not object ordering, since the fake stores
	// whole artifacts atomically).
	order []string
}

type fakeArtifact struct {
	files       map[string]string
	gen         uint64
	cpuVendor   string
	cpuTemplate string
	// createdAtMs mirrors the real store's meta.json CreatedAtUnixMs, which is
	// what remote retention orders on.
	createdAtMs int64
}

func newFakeStore() *fakeStore {
	return &fakeStore{
		arts:        make(map[string]fakeArtifact),
		exportCalls: make(map[string]int),
		reachable:   true,
	}
}

func (f *fakeStore) Export(_ context.Context, prefix, localDir string, files []string, generation uint64, nowMs int64, cpuVendor, cpuTemplate string) (int64, bool, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.exportCalls[prefix]++
	// Read the local files into memory, mirroring the real client's read-then-put.
	got := make(map[string]string, len(files))
	var total int64
	for _, name := range files {
		b, err := os.ReadFile(filepath.Join(localDir, name))
		if err != nil {
			return 0, false, err
		}
		got[name] = string(b)
		total += int64(len(b))
	}
	// Idempotency: if the stored content is identical, skip.
	if existing, ok := f.arts[prefix]; ok && sameStringMap(existing.files, got) {
		return 0, true, nil
	}
	f.arts[prefix] = fakeArtifact{files: got, gen: generation, cpuVendor: cpuVendor, cpuTemplate: cpuTemplate, createdAtMs: nowMs}
	f.order = append(f.order, prefix)
	return total, false, nil
}

func (f *fakeStore) Restore(_ context.Context, prefix, localDir string) (int64, uint64, error) {
	f.mu.Lock()
	art, ok := f.arts[prefix]
	f.mu.Unlock()
	if !ok {
		return 0, 0, errFakeNotPresent
	}
	if err := os.MkdirAll(localDir, 0o700); err != nil {
		return 0, 0, err
	}
	var total int64
	for name, content := range art.files {
		dst := filepath.Join(localDir, name)
		if err := os.MkdirAll(filepath.Dir(dst), 0o700); err != nil {
			return 0, 0, err
		}
		if err := os.WriteFile(dst, []byte(content), 0o600); err != nil {
			return 0, 0, err
		}
		total += int64(len(content))
	}
	return total, art.gen, nil
}

func (f *fakeStore) DeleteArtifact(_ context.Context, prefix string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	delete(f.arts, prefix)
	return nil
}

func (f *fakeStore) Present(_ context.Context, prefix string) (bool, uint64, string, string, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	art, ok := f.arts[prefix]
	if !ok {
		return false, 0, "", "", nil
	}
	return true, art.gen, art.cpuVendor, art.cpuTemplate, nil
}

// ListRefs mirrors the real store's DELIMITED list: it returns the immediate
// child segment under prefix for every seeded artifact beneath it, deduped, so a
// caller sees refs rather than files. Sorted for deterministic assertions.
func (f *fakeStore) ListRefs(_ context.Context, prefix string, limit int) ([]string, bool, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	p := strings.TrimSuffix(prefix, "/") + "/"
	seen := make(map[string]bool)
	for key := range f.arts {
		if !strings.HasPrefix(key, p) {
			continue
		}
		rest := strings.Trim(strings.TrimPrefix(key, p), "/")
		if rest == "" || strings.Contains(rest, "/") {
			continue
		}
		seen[rest] = true
	}
	refs := make([]string, 0, len(seen))
	for r := range seen {
		refs = append(refs, r)
	}
	sort.Strings(refs)
	if limit > 0 && len(refs) > limit {
		return refs[:limit], true, nil
	}
	return refs, false, nil
}

func (f *fakeStore) ArtifactInfo(_ context.Context, prefix string) (bool, int64, uint64, string, string, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	art, ok := f.arts[prefix]
	if !ok {
		return false, 0, 0, "", "", nil
	}
	var total uint64
	for _, c := range art.files {
		total += uint64(len(c))
	}
	return true, art.createdAtMs, total, art.cpuVendor, art.cpuTemplate, nil
}

func (f *fakeStore) Reachable(_ context.Context) bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.reachable
}

func (f *fakeStore) calls(prefix string) int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.exportCalls[prefix]
}

func (f *fakeStore) has(prefix string) bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	_, ok := f.arts[prefix]
	return ok
}

// seedArtifact directly places an artifact in the fake store under prefix with
// the given files and cpu_sku stamp, bypassing Export, so cpu_sku gate tests
// can construct an UNSTAMPED (both "") or a specific-sku artifact precisely,
// including one this node never wrote itself.
func (f *fakeStore) seedArtifact(prefix string, files map[string]string, generation uint64, cpuVendor, cpuTemplate string) {
	f.seedArtifactAt(prefix, files, generation, cpuVendor, cpuTemplate, 0)
}

// seedArtifactAt is seedArtifact with an explicit meta.json created-at, for the
// remote-retention tests that assert ordering by age.
func (f *fakeStore) seedArtifactAt(prefix string, files map[string]string, generation uint64, cpuVendor, cpuTemplate string, createdAtMs int64) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.arts[prefix] = fakeArtifact{files: files, gen: generation, cpuVendor: cpuVendor, cpuTemplate: cpuTemplate, createdAtMs: createdAtMs}
}

// errFakeNotPresent stands in for store.ErrNotPresent in these in-package tests
// (the server maps any Restore error to FAILED_PRECONDITION regardless of which
// sentinel it is).
var errFakeNotPresent = errFake("not present")

type errFake string

func (e errFake) Error() string { return string(e) }

func sameStringMap(a, b map[string]string) bool {
	if len(a) != len(b) {
		return false
	}
	for k, v := range a {
		if b[k] != v {
			return false
		}
	}
	return true
}

// diskScanStatefulDriver is a stateful-driver stub whose ScanStatefulBundles
// globs the REAL on-disk stateful/ dir exactly as the production *driver.Driver
// does (keyed by dir name == snapshot_ref, gen read from the sidecar). It embeds
// fakeStatefulDriver for the rest of the interface so a store test can exercise
// the faithful restore -> ReconcileStatefulFromDisk -> re-register path a real
// daemon takes, rather than the in-memory banked-map scan the base fake does.
type diskScanStatefulDriver struct {
	*fakeStatefulDriver
	statefulRoot string
}

func newDiskScanStatefulDriver(snapshotRoot string) *diskScanStatefulDriver {
	root := filepath.Join(snapshotRoot, "stateful")
	f := newFakeStatefulDriver(snapshotRoot)
	f.statefulDir = root
	return &diskScanStatefulDriver{fakeStatefulDriver: f, statefulRoot: root}
}

func (d *diskScanStatefulDriver) StatefulDir() string { return d.statefulRoot }

// RemoveStatefulBundle does a REAL os.RemoveAll of stateful/<ref> under the temp
// root (mirroring the production *driver.Driver), so an eviction test can assert
// the on-disk dir actually leaves disk rather than a faked in-memory map mutation
// masking the misroute #38 fixes. It also drops the in-memory banked entry so a
// subsequent ScanStatefulBundles agrees with disk.
func (d *diskScanStatefulDriver) RemoveStatefulBundle(snapshotRef string) error {
	if err := os.RemoveAll(filepath.Join(d.statefulRoot, snapshotRef)); err != nil {
		return err
	}
	return d.fakeStatefulDriver.RemoveStatefulBundle(snapshotRef)
}

// ScanStatefulBundles reads the on-disk stateful/ dir (mirroring the real
// driver): a bundle is any subdir holding a snapfile, keyed by the dir name, its
// generation read from the gen sidecar. This is what makes a restored bundle dir
// visible to ReconcileStatefulFromDisk exactly as a startup rescan would.
func (d *diskScanStatefulDriver) ScanStatefulBundles() []substrate.StatefulBundleInfo {
	entries, err := os.ReadDir(d.statefulRoot)
	if err != nil {
		return nil
	}
	var out []substrate.StatefulBundleInfo
	for _, e := range entries {
		if !e.IsDir() {
			continue
		}
		ref := e.Name()
		if _, serr := os.Stat(filepath.Join(d.statefulRoot, ref, "snapfile")); serr != nil {
			continue
		}
		var gen uint64
		if raw, rerr := os.ReadFile(filepath.Join(d.statefulRoot, ref, "gen")); rerr == nil {
			if n, perr := strconv.ParseUint(strings.TrimSpace(string(raw)), 10, 64); perr == nil {
				gen = n
			}
		}
		out = append(out, substrate.StatefulBundleInfo{SnapshotRef: ref, Generation: gen, SizeBytes: 4096})
	}
	return out
}

// newStoreTestServer builds a Server with the fake store wired and a real
// on-disk SnapshotRoot/VolumeRoot temp layout, so artifact enumeration reads
// genuine files. A disk-scanning stateful driver is wired so the RestoreArtifact
// re-registration path (ReconcileStatefulFromDisk -> ScanStatefulBundles) sees a
// restored bundle dir exactly as a startup disk rescan would, matching prod.
func newStoreTestServer(t *testing.T, fs *fakeStore) *Server {
	t.Helper()
	return newStoreTestServerWithVendor(t, fs, "amd")
}

// newStoreTestServerWithVendor mirrors newStoreTestServer but lets a test pick
// the node's own CPU vendor. The legacy-alias short-circuit in enqueueIfMissing
// is gated to nodes whose OWN vendor is the legacy alias ("amd"), so testing
// that gate needs a server whose vendor is something else (e.g. "intel").
func newStoreTestServerWithVendor(t *testing.T, fs *fakeStore, vendor string) *Server {
	t.Helper()
	root := t.TempDir()
	volRoot := t.TempDir()
	s := New(Options{
		Config:         config.Config{Arch: "amd64", Node: "node-4", CpuVendor: vendor, SnapshotRoot: root, VolumeRoot: volRoot},
		Driver:         &fakeDriver{},
		StatefulDriver: newDiskScanStatefulDriver(root),
		Transport:      &fakeTransport{},
		Store:          fs,
		Logger:         slog.New(slog.NewTextHandler(io.Discard, nil)),
	})
	s.memHeadroom = func() uint64 { return 0 }
	return s
}

// newStoreTestServerWithSku mirrors newStoreTestServer but lets a test pick the
// node's own (vendor, template) cpu_sku, for the PR-E mismatch/grandfather gate
// tests.
func newStoreTestServerWithSku(t *testing.T, fs *fakeStore, vendor, template string) *Server {
	t.Helper()
	root := t.TempDir()
	volRoot := t.TempDir()
	s := New(Options{
		Config:         config.Config{Arch: "amd64", Node: "node-4", CpuVendor: vendor, CpuTemplate: template, SnapshotRoot: root, VolumeRoot: volRoot},
		Driver:         &fakeDriver{},
		StatefulDriver: newDiskScanStatefulDriver(root),
		Transport:      &fakeTransport{},
		Store:          fs,
		Logger:         slog.New(slog.NewTextHandler(io.Discard, nil)),
	})
	s.memHeadroom = func() uint64 { return 0 }
	return s
}

// writeBundle writes a fake bundle dir (snapfile + memfile + optional sidecars)
// under the given kind's local dir, so enumeration finds a complete artifact.
func writeBundleFiles(t *testing.T, dir string, files map[string]string) {
	t.Helper()
	if err := os.MkdirAll(dir, 0o700); err != nil {
		t.Fatalf("mkdir %s: %v", dir, err)
	}
	for name, content := range files {
		if err := os.WriteFile(filepath.Join(dir, name), []byte(content), 0o600); err != nil {
			t.Fatalf("write %s: %v", name, err)
		}
	}
}

// waitForExport polls until the fake store holds the prefix or the deadline
// passes (the export queue is async).
func waitForExport(t *testing.T, fs *fakeStore, prefix string) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if fs.has(prefix) {
			return
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatalf("export of %q did not land within the deadline", prefix)
}

// TestReconcileStatefulReadsWorkloadSidecar proves a boot-scan reconciliation
// recovers the workload from the on-disk sidecar (#38 F1), so a bundle banked
// (with its sidecar) before a restart re-seeds with its REAL workload and is
// therefore remotely evictable, instead of seeding with "".
func TestReconcileStatefulReadsWorkloadSidecar(t *testing.T) {
	fs := newFakeStore()
	s := newStoreTestServer(t, fs)

	ref := "scratch-postgres__g9"
	dir := filepath.Join(s.cfg.SnapshotRoot, "stateful", ref)
	// A complete bundle (snapfile makes ScanStatefulBundles report it) WITH the
	// workload sidecar written at bank time.
	writeBundleFiles(t, dir, map[string]string{
		"snapfile":              "snap",
		"memfile":               "mem",
		"gen":                   "9",
		statefulWorkloadSidecar: "scratch-postgres",
	})

	s.ReconcileStatefulFromDisk()

	got, ok := s.statefulBundles.get(ref)
	if !ok {
		t.Fatalf("reconcile should have seeded stateful bundle %q", ref)
	}
	if got.workload != "scratch-postgres" {
		t.Fatalf("reconciled workload = %q, want scratch-postgres (from sidecar)", got.workload)
	}
}

// TestReconcileStatefulNoSidecarSeedsEmptyWorkload proves a pre-sidecar bundle
// (no workload file) reconciles with workload "", which the reaper then SKIPS
// (fix C) rather than draining a bundle whose S3 copy it cannot address.
func TestReconcileStatefulNoSidecarSeedsEmptyWorkload(t *testing.T) {
	fs := newFakeStore()
	s := newStoreTestServer(t, fs)

	ref := "legacy__nobind"
	dir := filepath.Join(s.cfg.SnapshotRoot, "stateful", ref)
	writeBundleFiles(t, dir, map[string]string{"snapfile": "snap", "memfile": "mem", "gen": "1"})

	s.ReconcileStatefulFromDisk()

	got, ok := s.statefulBundles.get(ref)
	if !ok {
		t.Fatalf("reconcile should still seed the bundle %q", ref)
	}
	if got.workload != "" {
		t.Fatalf("pre-sidecar bundle should seed workload \"\", got %q", got.workload)
	}
}

// TestExportArtifactStateful proves a direct ExportArtifact of a banked stateful
// bundle uploads its files and reports it exported in NodeStatus.
func TestExportArtifactStateful(t *testing.T) {
	fs := newFakeStore()
	s := newStoreTestServer(t, fs)
	ctx := context.Background()

	ref := "state-abc"
	dir := filepath.Join(s.cfg.SnapshotRoot, "stateful", ref)
	writeBundleFiles(t, dir, map[string]string{"snapfile": "snap", "memfile": "mem", "gen": "3"})
	s.statefulBundles.add(statefulBundleEntry{snapshotRef: ref, workload: "scratch-postgres", generation: 3})

	resp, err := s.ExportArtifact(ctx, &nodev1.ExportArtifactRequest{
		Artifact: &nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_STATEFUL, Workload: "scratch-postgres", Ref: ref},
	})
	if err != nil {
		t.Fatalf("ExportArtifact: %v", err)
	}
	if resp.GetSkipped() {
		t.Fatal("first export should not be skipped")
	}
	if resp.GetBytesMoved() == 0 {
		t.Fatal("export moved 0 bytes")
	}
	prefix := "stateful/amd/scratch-postgres/state-abc"
	if !fs.has(prefix) {
		t.Fatalf("store missing %q after export", prefix)
	}
	// NodeStatus reports the bundle exported.
	var reported *nodev1.StatefulBundle
	for _, b := range s.nodeStatus().GetStatefulBundles() {
		if b.GetSnapshotRef() == ref {
			reported = b
			break
		}
	}
	if reported == nil {
		t.Fatal("exported stateful bundle is absent from NodeStatus")
	}
	if !reported.GetExported() {
		t.Fatal("stateful bundle should report exported=true")
	}
}

// TestExportArtifactBaseReportsExported proves a direct ExportArtifact of a base
// snapshot uploads its files under base/<vendor>/<workload>/<ref> and, crucially
// for base-durability PR-1, that the workload's NodeStatus capacity then reports
// exported=true, projected from the same exportedCache the other artifact kinds
// read. The base must be a provisioned READY base to be advertised at all, so the
// test syncs the runtime image and marks the base READY.
func TestExportArtifactBaseReportsExported(t *testing.T) {
	fs := newFakeStore()
	s := newStoreTestServer(t, fs)
	ctx := context.Background()

	ref := "semgrep__0148fb2f0ac5"
	workload := "semgrep"
	digest := "img@sha256:deadbeef"

	// Provision the runtime image so imageProvisioned(digest) is true (else the
	// READY base is filtered out of NodeStatus and never reports exported).
	s.registry.sync([]workloadEntry{{Workload: workload, ImageRef: digest, RootfsRef: "/rootfs/semgrep"}})
	s.bases.readyBuild(ref, workload, digest, "", "/shim/ready", 2048)

	dir := filepath.Join(s.cfg.SnapshotRoot, "bases", ref)
	writeBundleFiles(t, dir, map[string]string{"imageref": "img", "memfile": "mem", "snapfile": "snap"})

	// Before export, the base is present but NOT exported.
	if exported := baseExportedInStatus(t, s, workload, ref); exported {
		t.Fatal("base should report exported=false before export")
	}

	resp, err := s.ExportArtifact(ctx, &nodev1.ExportArtifactRequest{
		Artifact: &nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_BASE, Workload: workload, Ref: ref},
	})
	if err != nil {
		t.Fatalf("ExportArtifact: %v", err)
	}
	if resp.GetSkipped() {
		t.Fatal("first export should not be skipped")
	}
	prefix := "base/amd/semgrep/semgrep__0148fb2f0ac5"
	if !fs.has(prefix) {
		t.Fatalf("store missing %q after export", prefix)
	}

	// After export, the workload capacity reports exported=true.
	if exported := baseExportedInStatus(t, s, workload, ref); !exported {
		t.Fatal("base should report exported=true after export")
	}

	// A second export is an idempotent skipped no-op (store already holds the
	// same checksum), so a re-issue from the control plane is harmless.
	resp2, err := s.ExportArtifact(ctx, &nodev1.ExportArtifactRequest{
		Artifact: &nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_BASE, Workload: workload, Ref: ref},
	})
	if err != nil {
		t.Fatalf("second ExportArtifact: %v", err)
	}
	if !resp2.GetSkipped() {
		t.Fatal("re-export of an unchanged base should be skipped")
	}
}

// TestExportArtifactBaseIsAsyncWhenQueueStarted proves the fast-durability-export
// fix: when the async export queue is running (as it is in production, via
// StartStoreLoops), a BASE ExportArtifact returns a FAST ACK (not-skipped,
// bytes_moved 0) without streaming the base inside the RPC, and the base's bytes
// land in the store asynchronously via the queue worker. This is what keeps a
// multi-minute large-base upload from holding the CP->noded gRPC call open long
// enough for an on-path L4 idle timeout to reap the idle flow. Non-BASE kinds stay
// synchronous (covered by the stateful/volume export tests above).
func TestExportArtifactBaseIsAsyncWhenQueueStarted(t *testing.T) {
	fs := newFakeStore()
	s := newStoreTestServer(t, fs)
	ctx := context.Background()
	// Start the queue so ExportArtifact takes the async BASE path (production shape).
	s.startExportQueue(ctx)

	ref := "bazel-query__abcdef012345"
	workload := "bazel-query"
	digest := "img@sha256:cafef00d"

	s.registry.sync([]workloadEntry{{Workload: workload, ImageRef: digest, RootfsRef: "/rootfs/bazel-query"}})
	s.bases.readyBuild(ref, workload, digest, "", "/shim/ready", 2048)

	dir := filepath.Join(s.cfg.SnapshotRoot, "bases", ref)
	writeBundleFiles(t, dir, map[string]string{"imageref": "img", "memfile": "mem", "snapfile": "snap"})

	prefix := "base/amd/bazel-query/bazel-query__abcdef012345"

	resp, err := s.ExportArtifact(ctx, &nodev1.ExportArtifactRequest{
		Artifact: &nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_BASE, Workload: workload, Ref: ref},
	})
	if err != nil {
		t.Fatalf("ExportArtifact: %v", err)
	}
	// Fast ack: the RPC returned before the upload; the enqueue path reports 0 bytes
	// moved and not-skipped (the real outcome is settled asynchronously).
	if resp.GetSkipped() {
		t.Fatal("async base export ack should not be skipped")
	}
	if resp.GetBytesMoved() != 0 {
		t.Fatalf("async base export ack bytes_moved = %d, want 0 (upload is deferred)", resp.GetBytesMoved())
	}

	// The bytes land in the store asynchronously via the queue worker.
	waitForExport(t, fs, prefix)
}

// TestExportArtifactBaseMissingLocalFailsEvenAsync proves the async BASE path still
// validates presence synchronously: a base with no local files is refused
// FAILED_PRECONDITION at the RPC (never a false fast-ack for an absent base), so the
// control plane can still distinguish "not there" from "export in flight".
func TestExportArtifactBaseMissingLocalFailsEvenAsync(t *testing.T) {
	fs := newFakeStore()
	s := newStoreTestServer(t, fs)
	s.startExportQueue(context.Background())

	_, err := s.ExportArtifact(context.Background(), &nodev1.ExportArtifactRequest{
		Artifact: &nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_BASE, Workload: "ghost", Ref: "gone"},
	})
	if err == nil {
		t.Fatal("async base export of an absent base should still fail FAILED_PRECONDITION")
	}
}

// TestExportArtifactBaseSkipsWhenSiblingVendorCopyExists proves the (vendor, ref)
// presence gate (#4079): when another node of the SAME vendor already exported
// this base, the control-plane-driven export must not re-upload, EVEN THOUGH the
// local bytes differ from the stored ones.
//
// Two nodes that independently build the same base ref hold different bytes (a
// Firecracker memory snapshot is not byte-reproducible; verified live on
// 2026-07-27, bazel-query__00ada79f752f differed between node-1 and node-2). So
// Store.Export's content-addressed compare never matches across nodes, and before
// this gate the CP-driven path re-uploaded the whole base OVER the sibling's
// object under the same key. Files are PUT before meta.json, so that overwrite
// also left a window where a concurrent restore saw files inconsistent with the
// meta it had already fetched.
func TestExportArtifactBaseSkipsWhenSiblingVendorCopyExists(t *testing.T) {
	fs := newFakeStore()
	s := newStoreTestServer(t, fs)
	ctx := context.Background()
	// Production shape: the async BASE queue is the path the CP drives.
	s.startExportQueue(ctx)

	ref := "bazel-query__00ada79f752f"
	workload := "bazel-query"
	digest := "img@sha256:cafef00d"
	prefix := "base/amd/bazel-query/bazel-query__00ada79f752f"

	// A sibling node of the same vendor already exported this ref, with ITS OWN
	// snapshot bytes.
	sibling := map[string]string{"imageref": "img", "memfile": "SIBLING-mem", "snapfile": "SIBLING-snap"}
	fs.seedArtifact(prefix, sibling, 0, "amd", "")

	s.registry.sync([]workloadEntry{{Workload: workload, ImageRef: digest, RootfsRef: "/rootfs/bazel-query"}})
	s.bases.readyBuild(ref, workload, digest, "", "/shim/ready", 2048)

	dir := filepath.Join(s.cfg.SnapshotRoot, "bases", ref)
	// Deliberately DIFFERENT bytes from the seeded sibling copy, which is the
	// real cross-node situation.
	writeBundleFiles(t, dir, map[string]string{"imageref": "img", "memfile": "LOCAL-mem", "snapfile": "LOCAL-snap"})

	if _, err := s.ExportArtifact(ctx, &nodev1.ExportArtifactRequest{
		Artifact: &nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_BASE, Workload: workload, Ref: ref},
	}); err != nil {
		t.Fatalf("ExportArtifact: %v", err)
	}

	// The queue worker settles into "already durable" and flips the exported flag.
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) && !baseExportedInStatus(t, s, workload, ref) {
		time.Sleep(5 * time.Millisecond)
	}
	if !baseExportedInStatus(t, s, workload, ref) {
		t.Fatal("base should report exported=true once a same-vendor sibling copy is durable")
	}

	// No upload was even attempted.
	if got := fs.calls(prefix); got != 0 {
		t.Fatalf("store.Export called %d times for an already-durable ref, want 0", got)
	}

	// And the sibling's bytes were NOT clobbered.
	fs.mu.Lock()
	stored := fs.arts[prefix].files
	fs.mu.Unlock()
	if !sameStringMap(stored, sibling) {
		t.Fatalf("sibling copy was overwritten: got %v, want %v", stored, sibling)
	}
}

// TestExportQueueVolumeStillReExportsAtStaleGeneration proves the presence gate
// does NOT weaken VOLUME durability. A volume is data rather than a memory
// snapshot, so presence alone is not enough: a stored copy at a STALE generation
// must still be re-exported, and only the generation-matched case skips.
//
// Driven through the queue (enqueueExport -> runExportJob) because that is the
// path the gate is on; a VOLUME ExportArtifact RPC is synchronous and does not
// reach it.
func TestExportQueueVolumeStillReExportsAtStaleGeneration(t *testing.T) {
	fs := newFakeStore()
	s := newStoreTestServer(t, fs)
	ctx := context.Background()
	s.startExportQueue(ctx)

	workload := "scratch-postgres"
	prefix := "volume/scratch-postgres"

	if err := s.volumes.Create(workload, 1<<20); err != nil {
		t.Fatalf("create volume: %v", err)
	}
	// Advance the generation via a CP-style BLESSING rather than a bare self-bump.
	// This test is about the PRESENCE gate, not the ADR 011 blessing gate, but the
	// queue now refuses an unblessed volume ahead of the presence check, so a
	// self-bumped fixture would make this pass for the wrong reason (0 export calls
	// because it was quarantined, not because presence skipped it). RecordBlessed
	// advances the ledger and writes the blessed marker together, exactly as a
	// CP-issued blessed_generation does, and requires the new generation to exceed
	// the current one.
	gen0, err := s.volumes.Generation(workload)
	if err != nil {
		t.Fatalf("generation: %v", err)
	}
	if _, err := s.volumes.RecordBlessed(workload, gen0+1); err != nil {
		t.Fatalf("RecordBlessed: %v", err)
	}
	cur, err := s.volumes.Generation(workload)
	if err != nil {
		t.Fatalf("generation: %v", err)
	}

	// A stored copy at a STALE generation (one behind the node's current volume).
	fs.seedArtifact(prefix, map[string]string{"disk.img": "OLD"}, cur-1, "", "")

	s.enqueueExport(&nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_VOLUME, Workload: workload})

	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) && fs.calls(prefix) == 0 {
		time.Sleep(5 * time.Millisecond)
	}
	if got := fs.calls(prefix); got == 0 {
		t.Fatal("a VOLUME present at a STALE generation must still be re-exported, got 0 export calls")
	}
}

// TestListArtifactsReturnsStoredRefsWithMeta proves the remote inventory read
// (PR-4, #3947): ListArtifacts enumerates the refs stored under one
// kind+vendor+workload prefix with the created-at retention orders on, and does
// NOT leak refs from a sibling vendor's prefix.
func TestListArtifactsReturnsStoredRefsWithMeta(t *testing.T) {
	fs := newFakeStore()
	s := newStoreTestServer(t, fs)
	ctx := context.Background()

	files := map[string]string{"imageref": "img", "memfile": "m", "snapfile": "s"}
	fs.seedArtifactAt("base/amd/bazel-query/bazel-query__old", files, 0, "amd", "", 1000)
	fs.seedArtifactAt("base/amd/bazel-query/bazel-query__new", files, 0, "amd", "", 2000)
	// A different vendor's copy of the same workload must not appear.
	fs.seedArtifactAt("base/intel/bazel-query/bazel-query__intel", files, 0, "intel", "", 3000)
	// A different workload under the same vendor must not appear either.
	fs.seedArtifactAt("base/amd/semgrep/semgrep__x", files, 0, "amd", "", 4000)

	resp, err := s.ListArtifacts(ctx, &nodev1.ListArtifactsRequest{
		Kind:     nodev1.ArtifactKind_ARTIFACT_KIND_BASE,
		Workload: "bazel-query",
		Vendor:   "amd",
	})
	if err != nil {
		t.Fatalf("ListArtifacts: %v", err)
	}

	got := map[string]int64{}
	for _, e := range resp.GetEntries() {
		got[e.GetRef()] = e.GetCreatedAtUnixMs()
	}
	if len(got) != 2 {
		t.Fatalf("entries = %v, want exactly the two amd bazel-query refs", got)
	}
	if got["bazel-query__old"] != 1000 || got["bazel-query__new"] != 2000 {
		t.Fatalf("created-at not reported per ref: %v", got)
	}
	if resp.GetTruncated() {
		t.Fatal("listing of 2 refs should not report truncated")
	}
}

// TestListArtifactsOmitsAnArtifactWithNoMarker proves an artifact whose
// completeness marker is unreadable is left OUT rather than reported with a zero
// date. meta.json is the store's definition of "present", and retention must
// never order on (or evict) something it cannot describe.
func TestListArtifactsOmitsAnArtifactWithNoMarker(t *testing.T) {
	fs := newFakeStore()
	s := newStoreTestServer(t, fs)

	files := map[string]string{"imageref": "img", "memfile": "m", "snapfile": "s"}
	fs.seedArtifactAt("base/amd/bazel-query/bazel-query__good", files, 0, "amd", "", 1000)
	// A ref directory that exists in the listing but has no readable marker: the
	// fake's ListRefs derives refs from seeded keys, so seed a CHILD key to make
	// the ref appear without the ref prefix itself being a described artifact.
	fs.seedArtifactAt("base/amd/bazel-query/bazel-query__partial/inner", files, 0, "amd", "", 2000)

	resp, err := s.ListArtifacts(context.Background(), &nodev1.ListArtifactsRequest{
		Kind:     nodev1.ArtifactKind_ARTIFACT_KIND_BASE,
		Workload: "bazel-query",
		Vendor:   "amd",
	})
	if err != nil {
		t.Fatalf("ListArtifacts: %v", err)
	}
	if len(resp.GetEntries()) != 1 {
		t.Fatalf("entries = %+v, want only the complete artifact", resp.GetEntries())
	}
	if got := resp.GetEntries()[0].GetRef(); got != "bazel-query__good" {
		t.Fatalf("listed ref = %q, want bazel-query__good", got)
	}
}

// TestEvictArtifactRemoteHonoursRequestedVendor proves a node can reclaim ANOTHER
// vendor's store object (#3947). The store key has no node segment, so one object
// is shared by every node of that vendor; without an explicit vendor a node could
// only ever compose its own, and a mixed fleet's bucket never converged.
func TestEvictArtifactRemoteHonoursRequestedVendor(t *testing.T) {
	fs := newFakeStore()
	s := newStoreTestServer(t, fs) // this node is amd (newStoreTestServer's vendor)
	ctx := context.Background()

	files := map[string]string{"imageref": "img", "memfile": "m", "snapfile": "s"}
	fs.seedArtifact("base/amd/bazel-query/bazel-query__amd", files, 0, "amd", "")
	fs.seedArtifact("base/intel/bazel-query/bazel-query__intel", files, 0, "intel", "")

	// Evict the INTEL object from this AMD node.
	if _, err := s.EvictArtifact(ctx, &nodev1.EvictArtifactRequest{
		Artifact: &nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_BASE, Workload: "bazel-query", Ref: "bazel-query__intel"},
		Remote:   true,
		Vendor:   "intel",
	}); err != nil {
		t.Fatalf("EvictArtifact(vendor=intel): %v", err)
	}

	if fs.has("base/intel/bazel-query/bazel-query__intel") {
		t.Fatal("the intel object should have been evicted")
	}
	if !fs.has("base/amd/bazel-query/bazel-query__amd") {
		t.Fatal("this node's own amd object must be untouched")
	}
}

// TestEvictArtifactRemoteEmptyVendorUsesOwn proves the additive field is
// backward compatible: an empty vendor still targets the daemon's own prefix, so
// a control plane that predates the field behaves exactly as before.
func TestEvictArtifactRemoteEmptyVendorUsesOwn(t *testing.T) {
	fs := newFakeStore()
	s := newStoreTestServer(t, fs)

	files := map[string]string{"imageref": "img", "memfile": "m", "snapfile": "s"}
	fs.seedArtifact("base/amd/bazel-query/bazel-query__amd", files, 0, "amd", "")

	if _, err := s.EvictArtifact(context.Background(), &nodev1.EvictArtifactRequest{
		Artifact: &nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_BASE, Workload: "bazel-query", Ref: "bazel-query__amd"},
		Remote:   true,
	}); err != nil {
		t.Fatalf("EvictArtifact(no vendor): %v", err)
	}
	if fs.has("base/amd/bazel-query/bazel-query__amd") {
		t.Fatal("an empty vendor should target this node's own prefix")
	}
}

// evictBase is a small helper: EvictArtifact{remote:false} of a BASE ref.
func evictBase(s *Server, workload, ref string) error {
	_, err := s.EvictArtifact(context.Background(), &nodev1.EvictArtifactRequest{
		Artifact: &nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_BASE, Workload: workload, Ref: ref},
		Remote:   false,
	})
	return err
}

// seedLocalBase writes a base dir on disk and registers it READY. Returns the dir path.
func seedLocalBase(t *testing.T, s *Server, workload, ref string) string {
	t.Helper()
	dir := filepath.Join(s.cfg.SnapshotRoot, "bases", ref)
	writeBundleFiles(t, dir, map[string]string{"imageref": "img", "memfile": "mem", "snapfile": "snap"})
	s.bases.readyBuild(ref, workload, "img@sha256:seed", "", "/shim/ready", 2048)
	return dir
}

// dirExists reports whether path is an existing directory (test-only helper).
func dirExists(path string) bool {
	info, err := os.Stat(path)
	return err == nil && info.IsDir()
}

// TestEvictBaseLocalRemovesSupersededDir proves the PR-3 BASE arm: an
// EvictArtifact{remote:false} of a superseded base ref removes bases/<ref> from
// disk and forgets its registry entry, while the workload's CURRENT base survives.
// This is the arm that fixes the leak: before it existed the control plane's
// EvictSnapshot misrouted a base ref into the session path and no-op'd.
func TestEvictBaseLocalRemovesSupersededDir(t *testing.T) {
	s := newStoreTestServer(t, newFakeStore())

	current := "semgrep__current00"
	superseded := "semgrep__old0000000"
	seedLocalBase(t, s, "semgrep", current)
	oldDir := seedLocalBase(t, s, "semgrep", superseded)

	if err := evictBase(s, "semgrep", superseded); err != nil {
		t.Fatalf("evict superseded base: %v", err)
	}
	if dirExists(oldDir) {
		t.Fatalf("superseded base dir %q survived eviction", oldDir)
	}
	if _, ok := s.bases.get(superseded); ok {
		t.Fatal("superseded base still registered after eviction")
	}
	// The current base is untouched.
	if _, ok := s.bases.get(current); !ok {
		t.Fatal("current base was wrongly forgotten")
	}
	if !dirExists(filepath.Join(s.cfg.SnapshotRoot, "bases", current)) {
		t.Fatal("current base dir was wrongly removed")
	}
}

// TestEvictBaseLocalIdempotentAlreadyGone proves an evict of an already-absent
// base ref (never on disk, never registered) is success, so a retry or a
// duplicate reconcile sweep is harmless.
func TestEvictBaseLocalIdempotentAlreadyGone(t *testing.T) {
	s := newStoreTestServer(t, newFakeStore())
	if err := evictBase(s, "semgrep", "semgrep__neverexisted"); err != nil {
		t.Fatalf("evict of absent base should be idempotent success, got %v", err)
	}
}

// TestEvictBaseLocalRefusesInUse proves the in-use guard: a base a live VM was
// restored from is NOT evicted (its dir survives), so a running guest never loses
// its birth lineage.
func TestEvictBaseLocalRefusesInUse(t *testing.T) {
	s := newStoreTestServer(t, newFakeStore())

	inUse := "sandbox__inuse00000"
	dir := seedLocalBase(t, s, "sandbox", inUse)
	s.vms.add(&vmEntry{id: "vm-live-1", workload: "sandbox", snapshotRef: inUse, state: vmPrimed})

	err := evictBase(s, "sandbox", inUse)
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("in-use base evict: want FailedPrecondition, got %v", err)
	}
	if !dirExists(dir) {
		t.Fatal("in-use base dir was removed despite the guard")
	}
}

// TestEvictBaseLocalRefusesBuilding proves the BUILDING guard: a base a BuildBase
// is currently writing is NOT evicted (removing it mid-build would corrupt the
// in-progress snapshot).
func TestEvictBaseLocalRefusesBuilding(t *testing.T) {
	s := newStoreTestServer(t, newFakeStore())

	// A current READY base plus a BUILDING one for the same workload.
	seedLocalBase(t, s, "bazel-query", "bazel-query__ready000")
	building := "bazel-query__building"
	dir := filepath.Join(s.cfg.SnapshotRoot, "bases", building)
	writeBundleFiles(t, dir, map[string]string{"imageref": "img", "memfile": "mem", "snapfile": "snap"})
	s.bases.beginBuild(building, "bazel-query", "", "/shim/ready")

	err := evictBase(s, "bazel-query", building)
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("BUILDING base evict: want FailedPrecondition, got %v", err)
	}
	if !dirExists(dir) {
		t.Fatal("BUILDING base dir was removed despite the guard")
	}
}

// TestEvictBaseLocalEvictsUnregisteredOrphan proves the reclaim requirement: a
// base dir with NO registry entry (a superseded dir not re-registered, or a build
// that died leaving memfile.tmp/snapfile.tmp with no snapfile so
// ReconcileBasesFromDisk never registered it) IS evicted, not refused. A
// registry-gated guard would strand exactly these bytes, which is the leak PR-3
// exists to drain. Current-base protection is the control plane's job (its sweep
// never targets a current ref), not a noded registry guard.
func TestEvictBaseLocalEvictsUnregisteredOrphan(t *testing.T) {
	s := newStoreTestServer(t, newFakeStore())

	// A .tmp orphan: no snapfile, never registered.
	orphan := "scratch-k8s__fc281c2c3e10"
	dir := filepath.Join(s.cfg.SnapshotRoot, "bases", orphan)
	writeBundleFiles(t, dir, map[string]string{"memfile.tmp": "partial", "snapfile.tmp": "partial"})
	if _, ok := s.bases.get(orphan); ok {
		t.Fatal("precondition: orphan should not be registered")
	}

	if err := evictBase(s, "scratch-k8s", orphan); err != nil {
		t.Fatalf("evict unregistered orphan: %v (want success)", err)
	}
	if dirExists(dir) {
		t.Fatal("unregistered .tmp orphan dir survived eviction")
	}
}

// TestEvictBaseLocalEvictsOnlyBaseWhenTargeted proves noded has NO not-last guard:
// when the control plane targets a base for local eviction (its desired-set
// computation and durability gate having already run), noded removes it even if it
// is the workload's last local READY base. The current-base floor lives in the CP
// (which never targets a current ref), not in noded; a not-last guard here would
// refuse a legitimately-superseded ref that happened to be the last on-disk copy.
func TestEvictBaseLocalEvictsOnlyBaseWhenTargeted(t *testing.T) {
	s := newStoreTestServer(t, newFakeStore())

	only := "scratch-k8s__only0000"
	dir := seedLocalBase(t, s, "scratch-k8s", only)

	if err := evictBase(s, "scratch-k8s", only); err != nil {
		t.Fatalf("evict targeted base: %v (want success)", err)
	}
	if dirExists(dir) {
		t.Fatal("targeted base dir survived eviction")
	}
	if _, ok := s.bases.get(only); ok {
		t.Fatal("evicted base still registered")
	}
}

// TestLocalBasesStatusReportsOrphans proves the Option-B inventory SCANS the
// bases/ dir (not just the registry), so unregistered/.tmp orphan dirs appear in
// NodeStatus.local_bases and the control plane's sweep can target them. A
// registry-only projection would hide the orphans and strand them.
func TestLocalBasesStatusReportsOrphans(t *testing.T) {
	s := newStoreTestServer(t, newFakeStore())

	// A registered READY base and an unregistered .tmp orphan for the same workload.
	seedLocalBase(t, s, "scratch-k8s", "scratch-k8s__current0")
	orphanDir := filepath.Join(s.cfg.SnapshotRoot, "bases", "scratch-k8s__orphan00")
	writeBundleFiles(t, orphanDir, map[string]string{"memfile.tmp": "partial"})

	inv := s.localBasesStatus()
	byRef := make(map[string]*nodev1.BaseInventoryEntry)
	for _, e := range inv {
		byRef[e.GetRef()] = e
	}

	reg, ok := byRef["scratch-k8s__current0"]
	if !ok || reg.GetBaseState() != nodev1.BaseBuildState_BASE_BUILD_STATE_READY {
		t.Fatalf("registered base missing/not READY in inventory: %+v", reg)
	}
	orphan, ok := byRef["scratch-k8s__orphan00"]
	if !ok {
		t.Fatal("unregistered .tmp orphan not reported in local_bases (would be stranded)")
	}
	if orphan.GetBaseState() != nodev1.BaseBuildState_BASE_BUILD_STATE_UNSPECIFIED {
		t.Fatalf("orphan base_state = %v, want UNSPECIFIED", orphan.GetBaseState())
	}
	if orphan.GetWorkload() != "scratch-k8s" {
		t.Fatalf("orphan workload = %q, want scratch-k8s (from base-key prefix)", orphan.GetWorkload())
	}
}

func TestLocalBasesStatusSkipsStagingDirectories(t *testing.T) {
	s := newStoreTestServer(t, newFakeStore())
	stagingDir := filepath.Join(s.cfg.SnapshotRoot, "bases", "scratch-k8s__stale.building")
	if err := os.MkdirAll(stagingDir, 0o700); err != nil {
		t.Fatal(err)
	}

	inv := s.localBasesStatus()
	if len(inv) != 0 {
		t.Fatalf("localBasesStatus() returned %d entries, want no staging directory", len(inv))
	}
}

func TestLocalBasesStatusBuildingReportsOnceNotTwice(t *testing.T) {
	s := newStoreTestServer(t, newFakeStore())
	ref := "scratch-k8s__building0"
	s.bases.beginBuild(ref, "scratch-k8s", "", "/shim/ready")
	stagingDir := filepath.Join(s.cfg.SnapshotRoot, "bases", ref+".building")
	if err := os.MkdirAll(stagingDir, 0o700); err != nil {
		t.Fatal(err)
	}

	inv := s.localBasesStatus()
	if len(inv) != 1 {
		t.Fatalf("localBasesStatus() returned %d entries, want exactly one", len(inv))
	}
	if inv[0].GetRef() != ref || inv[0].GetBaseState() != nodev1.BaseBuildState_BASE_BUILD_STATE_BUILDING {
		t.Fatalf("inventory = %+v, want registry BUILDING entry for %q", inv[0], ref)
	}
}

func TestCleanupStagingDirsRemovesStaleDirectories(t *testing.T) {
	s := newStoreTestServer(t, newFakeStore())
	root := filepath.Join(s.cfg.SnapshotRoot, "bases")
	stale := filepath.Join(root, "scratch-k8s__stale.building")
	regular := filepath.Join(root, "scratch-k8s__ready")
	if err := os.MkdirAll(stale, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(regular, 0o700); err != nil {
		t.Fatal(err)
	}

	s.CleanupStagingDirs()
	if dirExists(stale) {
		t.Fatal("stale staging directory survived cleanup")
	}
	if !dirExists(regular) {
		t.Fatal("non-staging directory was removed by cleanup")
	}
}

func TestIsCompleteBase(t *testing.T) {
	tests := []struct {
		name  string
		files map[string]string
		state nodev1.BaseBuildState
		want  bool
	}{
		{name: "complete", files: map[string]string{"imageref": "img", "memfile": "mem", "snapfile": "snap"}, want: true},
		{name: "missing imageref", files: map[string]string{"memfile": "mem", "snapfile": "snap"}},
		{name: "missing memfile", files: map[string]string{"imageref": "img", "snapfile": "snap"}},
		{name: "missing snapfile", files: map[string]string{"imageref": "img", "memfile": "mem"}},
		{name: "temporary file", files: map[string]string{"imageref": "img", "memfile": "mem", "snapfile": "snap", "snapfile.tmp": "partial"}},
		{name: "building", state: nodev1.BaseBuildState_BASE_BUILD_STATE_BUILDING, files: map[string]string{"memfile.tmp": "partial"}, want: true},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			dir := t.TempDir()
			writeBundleFiles(t, dir, tt.files)
			if got := isCompleteBase(dir, tt.state); got != tt.want {
				t.Fatalf("isCompleteBase() = %v, want %v", got, tt.want)
			}
		})
	}
}

func TestLocalBasesStatusReportsIncompleteRegisteredBaseAsAbsent(t *testing.T) {
	s := newStoreTestServer(t, newFakeStore())
	ref := "scratch-k8s__incomplete0"
	dir := filepath.Join(s.cfg.SnapshotRoot, "bases", ref)
	writeBundleFiles(t, dir, map[string]string{"memfile.tmp": "partial"})
	s.bases.register(baseEntry{snapshotRef: ref, workload: "scratch-k8s", sizeBytes: 123, state: nodev1.BaseBuildState_BASE_BUILD_STATE_READY})

	inv := s.localBasesStatus()
	if len(inv) != 1 {
		t.Fatalf("localBasesStatus() returned %d entries, want 1", len(inv))
	}
	if inv[0].GetSizeBytes() != 0 || inv[0].GetBaseState() != nodev1.BaseBuildState_BASE_BUILD_STATE_UNSPECIFIED {
		t.Fatalf("incomplete base inventory = %+v, want zero-sized unspecified", inv[0])
	}
}

// baseExportedInStatus finds the workload's capacity entry in NodeStatus whose
// snapshot_ref matches and returns its exported flag. A missing entry fails the
// caller so a false assertion cannot pass on an absent capacity.
func baseExportedInStatus(t *testing.T, s *Server, workload, ref string) bool {
	t.Helper()
	for _, wc := range s.nodeStatus().GetWorkloads() {
		if wc.GetWorkload() == workload && wc.GetSnapshotRef() == ref {
			return wc.GetExported()
		}
	}
	t.Fatalf("workload capacity %q with snapshot_ref %q was not reported", workload, ref)
	return false
}

// TestExportArtifactMissingLocalFails proves ExportArtifact refuses
// FAILED_PRECONDITION when the local artifact is absent.
func waitForBaseOnDisk(t *testing.T, s *Server, ref string) {
	t.Helper()
	snapfile := filepath.Join(s.cfg.SnapshotRoot, "bases", ref, "snapfile")
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if _, err := os.Stat(snapfile); err == nil {
			return
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatalf("base %q did not land on disk within the deadline", ref)
}

func TestRestoreArtifactBaseIsAsync(t *testing.T) {
	fs := newFakeStore()
	s := newStoreTestServer(t, fs)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	// Start the async queues (export + restore share the lifecycle).
	s.StartStoreLoops(ctx)

	workload := "bazel-query"
	ref := "bazel-query__abcdef012345"
	// The runtime must be provisioned so the re-registered base is not GC'd by the
	// reconcile's imageref gate; seed the store artifact with the imageref file the
	// disk scan reads back.
	digest := "sha256:runtime-bazel"
	s.registry.sync([]workloadEntry{{Workload: workload, ImageRef: digest, RootfsRef: "/rootfs/bazel"}})
	prefix := "base/amd/" + workload + "/" + ref
	fs.seedArtifact(prefix, map[string]string{"imageref": digest, "memfile": "mem", "snapfile": "snap"}, 0, "amd", "")

	resp, err := s.RestoreArtifact(ctx, &nodev1.RestoreArtifactRequest{
		Artifact: &nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_BASE, Workload: workload, Ref: ref},
		Vendor:   "amd",
	})
	if err != nil {
		t.Fatalf("RestoreArtifact(base): %v", err)
	}
	if !resp.GetAccepted() {
		t.Fatal("a BASE restore that needs a download must fast-ACK accepted=true")
	}
	if resp.GetBytesMoved() != 0 {
		t.Fatal("the fast-ACK must not report bytes moved (the download runs async)")
	}

	// The async worker downloads the base onto disk and re-registers it READY.
	waitForBaseOnDisk(t, s, ref)
	if got, _ := os.ReadFile(filepath.Join(s.cfg.SnapshotRoot, "bases", ref, "snapfile")); string(got) != "snap" {
		t.Fatalf("restored base snapfile = %q, want snap", got)
	}
	if b, ok := s.bases.get(ref); !ok || b.state != nodev1.BaseBuildState_BASE_BUILD_STATE_READY {
		t.Fatal("restored base not re-registered READY")
	}
}

// TestRestoreArtifactBaseNotPresentFailsFast proves a BASE restore whose store
// copy is absent returns FAILED_PRECONDITION SYNCHRONOUSLY (never an accepted
// ack that would make the caller wait a poll timeout), so the control plane
// falls back to BuildBase at once.
func TestRestoreArtifactBaseNotPresentFailsFast(t *testing.T) {
	fs := newFakeStore()
	s := newStoreTestServer(t, fs)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	s.StartStoreLoops(ctx)

	_, err := s.RestoreArtifact(ctx, &nodev1.RestoreArtifactRequest{
		Artifact: &nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_BASE, Workload: "bazel-query", Ref: "missing__000000000000"},
		Vendor:   "amd",
	})
	if err == nil {
		t.Fatal("a BASE restore of an absent store copy must fail, not ACK")
	}
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("absent-base restore error code = %v, want FailedPrecondition", status.Code(err))
	}
}

// TestRestoreArtifactBaseAlreadyLocalSkips proves a BASE restore is an inline
// skipped no-op (NOT accepted/async) when the base is already present locally,
// so a redundant hydrate trigger costs no download.
func TestRestoreArtifactBaseAlreadyLocalSkips(t *testing.T) {
	fs := newFakeStore()
	s := newStoreTestServer(t, fs)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	s.StartStoreLoops(ctx)

	workload := "bazel-query"
	ref := "bazel-query__abcdef012345"
	digest := "sha256:runtime-bazel"
	s.registry.sync([]workloadEntry{{Workload: workload, ImageRef: digest, RootfsRef: "/rootfs/bazel"}})
	prefix := "base/amd/" + workload + "/" + ref
	fs.seedArtifact(prefix, map[string]string{"imageref": digest, "memfile": "mem", "snapfile": "snap"}, 0, "amd", "")
	// The base is ALSO already on local disk.
	writeBundleFiles(t, filepath.Join(s.cfg.SnapshotRoot, "bases", ref), map[string]string{"imageref": digest, "memfile": "mem", "snapfile": "snap"})

	resp, err := s.RestoreArtifact(ctx, &nodev1.RestoreArtifactRequest{
		Artifact: &nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_BASE, Workload: workload, Ref: ref},
		Vendor:   "amd",
	})
	if err != nil {
		t.Fatalf("RestoreArtifact(already-local base): %v", err)
	}
	if !resp.GetSkipped() {
		t.Fatal("an already-local base restore must be a skipped no-op")
	}
	if resp.GetAccepted() {
		t.Fatal("an already-local base restore must NOT enqueue an async download")
	}
}

func TestCleanupStagingDirsNotReachedDuringRestore(t *testing.T) {
	fs := newFakeStore()
	s := newStoreTestServer(t, fs)

	workload := "bazel-query"
	ref := "bazel-query__abcdef012345"
	digest := "sha256:runtime-bazel"
	s.registry.sync([]workloadEntry{{Workload: workload, ImageRef: digest, RootfsRef: "/rootfs/bazel"}})
	prefix := "base/amd/" + workload + "/" + ref
	fs.seedArtifact(prefix, map[string]string{"imageref": digest, "memfile": "mem", "snapfile": "snap"}, 0, "amd", "")

	stale := filepath.Join(s.cfg.SnapshotRoot, "bases", "scratch-k8s__stale.building")
	if err := os.MkdirAll(stale, 0o700); err != nil {
		t.Fatal(err)
	}
	writeBundleFiles(t, filepath.Join(s.cfg.SnapshotRoot, "bases", ref), map[string]string{"imageref": digest, "memfile": "mem", "snapfile": "snap"})

	resp, err := s.RestoreArtifact(context.Background(), &nodev1.RestoreArtifactRequest{
		Artifact: &nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_BASE, Workload: workload, Ref: ref},
		Vendor:   "amd",
	})
	if err != nil {
		t.Fatalf("RestoreArtifact(already-local base): %v", err)
	}
	if !resp.GetSkipped() {
		t.Fatal("an already-local base restore must be skipped")
	}
	if !dirExists(stale) {
		t.Fatal("staging directory was removed during restore")
	}
}

// baseExportedInStatus finds the workload's capacity entry in NodeStatus whose
// snapshot_ref matches and returns its exported flag; false if not reported.

// TestExportArtifactMissingLocalFails proves ExportArtifact refuses
// FAILED_PRECONDITION when the local artifact is absent.
func TestExportArtifactMissingLocalFails(t *testing.T) {
	fs := newFakeStore()
	s := newStoreTestServer(t, fs)
	_, err := s.ExportArtifact(context.Background(), &nodev1.ExportArtifactRequest{
		Artifact: &nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_STATEFUL, Workload: "nope", Ref: "gone"},
	})
	if err == nil {
		t.Fatal("export of an absent artifact should fail")
	}
}

// TestExportArtifactStoreDisabled proves the verb refuses when no store is
// configured (nil).
func TestExportArtifactStoreDisabled(t *testing.T) {
	root := t.TempDir()
	s := New(Options{
		Config:    config.Config{Arch: "amd64", Node: "node-4", SnapshotRoot: root},
		Driver:    &fakeDriver{},
		Transport: &fakeTransport{},
		Logger:    slog.New(slog.NewTextHandler(io.Discard, nil)),
	})
	_, err := s.ExportArtifact(context.Background(), &nodev1.ExportArtifactRequest{
		Artifact: &nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_STATEFUL, Workload: "w", Ref: "r"},
	})
	if err == nil {
		t.Fatal("export with no store should fail FAILED_PRECONDITION")
	}
}

// TestBankCommitTriggersExport proves a session Bank enqueues an async export
// that lands in the store (the export-after-commit path), via the internal
// enqueue plus the worker pool.
func TestBankCommitTriggersExport(t *testing.T) {
	fs := newFakeStore()
	s := newStoreTestServer(t, fs)
	ctx := context.Background()
	s.startExportQueue(ctx)

	ref := "sess-1"
	dir := filepath.Join(s.cfg.SnapshotRoot, "sessions", ref)
	writeBundleFiles(t, dir, map[string]string{"snapfile": "s", "memfile": "m"})

	// Simulate the tail of Bank: enqueue the export for the banked bundle.
	s.enqueueExport(&nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_SESSION, Workload: "sandbox-session", Ref: ref})
	waitForExport(t, fs, "session/amd/sandbox-session/sess-1")
}

// TestVolumeExportSkipsUnchangedGeneration proves a second volume export at the
// same generation is skipped (the gen-unchanged short-circuit).
func TestVolumeExportSkipsUnchangedGeneration(t *testing.T) {
	fs := newFakeStore()
	s := newStoreTestServer(t, fs)
	ctx := context.Background()

	// Create a volume at generation 5.
	if err := s.volumes.Create("scratch-postgres", 1<<20); err != nil {
		t.Fatalf("create volume: %v", err)
	}
	// RecordBlessed, not BumpGeneration: a self-bump leaves the volume UNBLESSED,
	// which ADR embervm/011 quarantines from export ("an artifact whose generation
	// was never blessed is quarantined, never exported"). This test is about the
	// same-generation export short-circuit, so it needs a realistic blessed volume.
	if _, err := s.volumes.RecordBlessed("scratch-postgres", 5); err != nil {
		t.Fatalf("bless: %v", err)
	}
	volRef := &nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_VOLUME, Workload: "scratch-postgres"}

	// First export lands.
	if _, err := s.ExportArtifact(ctx, &nodev1.ExportArtifactRequest{Artifact: volRef}); err != nil {
		t.Fatalf("first volume export: %v", err)
	}
	prefix := "volume/scratch-postgres"
	firstCalls := fs.calls(prefix)
	if firstCalls != 1 {
		t.Fatalf("first export call count = %d, want 1", firstCalls)
	}

	// Enqueue an async re-export at the SAME generation: the local short-circuit
	// (exported cache generation == current) drops it without an Export call.
	s.startExportQueue(ctx)
	s.enqueueExport(volRef)
	// Give the worker a moment; the call count must NOT increase.
	time.Sleep(100 * time.Millisecond)
	if got := fs.calls(prefix); got != firstCalls {
		t.Fatalf("gen-unchanged re-export issued %d extra Export calls, want 0", got-firstCalls)
	}
}

// TestRestoreArtifactRoundTrip proves RestoreArtifact fetches a stateful bundle
// AND a volume pair back onto disk and re-registers the bundle.
func TestRestoreArtifactRoundTrip(t *testing.T) {
	fs := newFakeStore()
	s := newStoreTestServer(t, fs)
	ctx := context.Background()

	// Seed the store with a stateful bundle and a volume by exporting them from a
	// throwaway source layout.
	bundleRef := "state-xyz"
	srcBundle := filepath.Join(s.cfg.SnapshotRoot, "stateful", bundleRef)
	writeBundleFiles(t, srcBundle, map[string]string{"snapfile": "snapdata", "memfile": "memdata", "gen": "4"})
	s.statefulBundles.add(statefulBundleEntry{snapshotRef: bundleRef, workload: "scratch-postgres", generation: 4})
	if _, err := s.ExportArtifact(ctx, &nodev1.ExportArtifactRequest{
		Artifact: &nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_STATEFUL, Workload: "scratch-postgres", Ref: bundleRef},
	}); err != nil {
		t.Fatalf("seed bundle export: %v", err)
	}
	if err := s.volumes.Create("scratch-postgres", 1<<20); err != nil {
		t.Fatalf("create volume: %v", err)
	}
	// Blessed, not self-bumped: ADR embervm/011 quarantines an unblessed volume
	// from export, so a round-trip fixture must model a CP-blessed attach.
	if _, err := s.volumes.RecordBlessed("scratch-postgres", 1); err != nil {
		t.Fatalf("bless: %v", err)
	}
	if _, err := s.ExportArtifact(ctx, &nodev1.ExportArtifactRequest{
		Artifact: &nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_VOLUME, Workload: "scratch-postgres"},
	}); err != nil {
		t.Fatalf("seed volume export: %v", err)
	}

	// Wipe local disk copies to force a real restore.
	if err := os.RemoveAll(srcBundle); err != nil {
		t.Fatalf("rm bundle: %v", err)
	}
	s.statefulBundles.remove(bundleRef)
	if err := os.RemoveAll(filepath.Dir(s.volumes.VolumePath("scratch-postgres"))); err != nil {
		t.Fatalf("rm volume: %v", err)
	}

	// Restore the bundle.
	resp, err := s.RestoreArtifact(ctx, &nodev1.RestoreArtifactRequest{
		Artifact: &nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_STATEFUL, Workload: "scratch-postgres", Ref: bundleRef},
		Vendor:   "amd",
	})
	if err != nil {
		t.Fatalf("restore bundle: %v", err)
	}
	if resp.GetGeneration() != 4 {
		t.Fatalf("restored bundle gen = %d, want 4", resp.GetGeneration())
	}
	if got, _ := os.ReadFile(filepath.Join(srcBundle, "snapfile")); string(got) != "snapdata" {
		t.Fatalf("restored snapfile = %q, want snapdata", got)
	}
	// The reconcile re-registered the bundle so a rescan sees it.
	if _, ok := s.statefulBundles.get(bundleRef); !ok {
		t.Fatal("restored stateful bundle not re-registered")
	}

	// Restore the volume pair.
	if _, err := s.RestoreArtifact(ctx, &nodev1.RestoreArtifactRequest{
		Artifact: &nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_VOLUME, Workload: "scratch-postgres"},
	}); err != nil {
		t.Fatalf("restore volume: %v", err)
	}
	if !s.volumes.Exists("scratch-postgres") {
		t.Fatal("restored volume file missing")
	}
	gen, err := s.volumes.Generation("scratch-postgres")
	if err != nil || gen != 1 {
		t.Fatalf("restored volume generation = (%d, %v), want (1, nil)", gen, err)
	}
}

// TestRestoreArtifactAbsentFails proves RestoreArtifact refuses
// FAILED_PRECONDITION when the store copy is absent.
func TestRestoreArtifactAbsentFails(t *testing.T) {
	fs := newFakeStore()
	s := newStoreTestServer(t, fs)
	_, err := s.RestoreArtifact(context.Background(), &nodev1.RestoreArtifactRequest{
		Artifact: &nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_STATEFUL, Workload: "w", Ref: "missing"},
		Vendor:   "amd",
	})
	if err == nil {
		t.Fatal("restore of an absent store copy should fail")
	}
}

func TestRestoreSessionWorkspaceAbsentIsNotFound(t *testing.T) {
	s := newStoreTestServer(t, newFakeStore())
	_, err := s.RestoreArtifact(context.Background(), &nodev1.RestoreArtifactRequest{
		Artifact: &nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_SESSION_WORKSPACE, Workload: "w", Ref: "missing"},
	})
	if status.Code(err) != codes.NotFound {
		t.Fatalf("workspace restore code = %v, want NotFound", status.Code(err))
	}
}

// TestRestoreArtifactUnstampedSkuGrandfathered proves the PR-E grandfather
// rule at the RestoreArtifact seam: an artifact stamped with NO cpu_sku at all
// (exported before PR-E landed) restores successfully regardless of the
// node's own (vendor, template), because refusing an UNSTAMPED artifact is
// data loss, the exact failure this rule exists to prevent.
func TestRestoreArtifactUnstampedSkuGrandfathered(t *testing.T) {
	fs := newFakeStore()
	s := newStoreTestServerWithSku(t, fs, "intel", "t2-conservative")
	ctx := context.Background()

	prefix := "stateful/intel/scratch-postgres/legacy-1"
	fs.seedArtifact(prefix, map[string]string{"snapfile": "snap", "memfile": "mem"}, 3, "", "")

	resp, err := s.RestoreArtifact(ctx, &nodev1.RestoreArtifactRequest{
		Artifact: &nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_STATEFUL, Workload: "scratch-postgres", Ref: "legacy-1"},
		Vendor:   "intel",
	})
	if err != nil {
		t.Fatalf("restore of an unstamped (grandfathered) artifact should succeed: %v", err)
	}
	if resp.GetGeneration() != 3 {
		t.Fatalf("restored generation = %d, want 3", resp.GetGeneration())
	}
}

// TestRestoreArtifactMatchedSkuRestores proves an artifact stamped with the
// SAME (vendor, template) as the node restores successfully, for both fleet
// vendors: a present-and-matching stamp is the ordinary, non-legacy
// compatible case.
func TestRestoreArtifactMatchedSkuRestores(t *testing.T) {
	for _, tc := range []struct{ vendor, template string }{
		{"amd", "amd-default"},
		{"intel", "t2-conservative"},
	} {
		t.Run(tc.vendor, func(t *testing.T) {
			fs := newFakeStore()
			s := newStoreTestServerWithSku(t, fs, tc.vendor, tc.template)
			ctx := context.Background()

			prefix := "stateful/" + tc.vendor + "/scratch-postgres/matched-1"
			fs.seedArtifact(prefix, map[string]string{"snapfile": "snap", "memfile": "mem"}, 5, tc.vendor, tc.template)

			resp, err := s.RestoreArtifact(ctx, &nodev1.RestoreArtifactRequest{
				Artifact: &nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_STATEFUL, Workload: "scratch-postgres", Ref: "matched-1"},
				Vendor:   tc.vendor,
			})
			if err != nil {
				t.Fatalf("restore of a matched-sku artifact should succeed: %v", err)
			}
			if resp.GetGeneration() != 5 {
				t.Fatalf("restored generation = %d, want 5", resp.GetGeneration())
			}
		})
	}
}

// TestRestoreArtifactMismatchedSkuRefusesLoudly proves a PRESENT-but-
// mismatched cpu_sku (same vendor, different template) is refused
// FAILED_PRECONDITION, loudly, never a silent wrong-sku boot. Checked for
// both fleet vendors: the mismatch gate must be correct-by-construction on
// the Intel pool even without live silicon to exercise it.
func TestRestoreArtifactMismatchedSkuRefusesLoudly(t *testing.T) {
	for _, tc := range []struct{ vendor, nodeTemplate, artifactTemplate string }{
		{"amd", "amd-default", "amd-other"},
		{"intel", "t2-conservative", "t2s-experimental"},
	} {
		t.Run(tc.vendor, func(t *testing.T) {
			fs := newFakeStore()
			s := newStoreTestServerWithSku(t, fs, tc.vendor, tc.nodeTemplate)
			ctx := context.Background()

			prefix := "stateful/" + tc.vendor + "/scratch-postgres/mismatch-1"
			fs.seedArtifact(prefix, map[string]string{"snapfile": "snap", "memfile": "mem"}, 2, tc.vendor, tc.artifactTemplate)

			_, err := s.RestoreArtifact(ctx, &nodev1.RestoreArtifactRequest{
				Artifact: &nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_STATEFUL, Workload: "scratch-postgres", Ref: "mismatch-1"},
				Vendor:   tc.vendor,
			})
			if err == nil {
				t.Fatalf("restore of a mismatched-sku artifact should be refused (%s: %q != %q)", tc.vendor, tc.artifactTemplate, tc.nodeTemplate)
			}
			if status.Code(err) != codes.FailedPrecondition {
				t.Fatalf("mismatch error code = %v, want FailedPrecondition", status.Code(err))
			}
		})
	}
}

// TestCpuSkuMismatchGrandfatherRule unit-tests cpuSkuMismatch directly against
// every grandfather-rule case: unstamped always compatible, matched
// compatible, mismatched refused, and an unresolved node sku never refusing.
func TestCpuSkuMismatchGrandfatherRule(t *testing.T) {
	cases := []struct {
		name                           string
		stampedVendor, stampedTemplate string
		nodeVendor, nodeTemplate       string
		wantMismatch                   bool
	}{
		{"unstamped always compatible (amd node)", "", "", "amd", "amd-default", false},
		{"unstamped always compatible (intel node)", "", "", "intel", "t2-conservative", false},
		{"matched sku compatible (amd)", "amd", "amd-default", "amd", "amd-default", false},
		{"matched sku compatible (intel)", "intel", "t2-conservative", "intel", "t2-conservative", false},
		{"mismatched template refused (amd)", "amd", "amd-other", "amd", "amd-default", true},
		{"mismatched template refused (intel)", "intel", "t2s-experimental", "intel", "t2-conservative", true},
		{"mismatched vendor refused", "amd", "amd-default", "intel", "t2-conservative", true},
		{"unresolved node sku never refuses", "intel", "t2-conservative", "", "", false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			mismatch, _, _ := cpuSkuMismatch(tc.stampedVendor, tc.stampedTemplate, tc.nodeVendor, tc.nodeTemplate)
			if mismatch != tc.wantMismatch {
				t.Errorf("cpuSkuMismatch(%q,%q,%q,%q) = %v, want %v", tc.stampedVendor, tc.stampedTemplate, tc.nodeVendor, tc.nodeTemplate, mismatch, tc.wantMismatch)
			}
		})
	}
}

// TestEvictArtifactRemote proves EvictArtifact(remote=true) removes the store
// copy and is idempotent.
func TestEvictArtifactRemote(t *testing.T) {
	fs := newFakeStore()
	s := newStoreTestServer(t, fs)
	ctx := context.Background()

	ref := "serv-1"
	dir := filepath.Join(s.cfg.SnapshotRoot, "serving", ref)
	writeBundleFiles(t, dir, map[string]string{"snapfile": "s", "memfile": "m"})
	s.servingSnap.add(servingSnapshotEntry{snapshotRef: ref, workload: "serving-test"})
	if _, err := s.ExportArtifact(ctx, &nodev1.ExportArtifactRequest{
		Artifact: &nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_SERVING, Workload: "serving-test", Ref: ref},
	}); err != nil {
		t.Fatalf("export: %v", err)
	}
	prefix := "serving/amd/serving-test/serv-1"
	if !fs.has(prefix) {
		t.Fatal("store missing artifact before evict")
	}
	if _, err := s.EvictArtifact(ctx, &nodev1.EvictArtifactRequest{
		Artifact: &nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_SERVING, Workload: "serving-test", Ref: ref},
		Remote:   true,
	}); err != nil {
		t.Fatalf("evict remote: %v", err)
	}
	if fs.has(prefix) {
		t.Fatal("store still holds artifact after remote evict")
	}
	// Idempotent.
	if _, err := s.EvictArtifact(ctx, &nodev1.EvictArtifactRequest{
		Artifact: &nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_SERVING, Workload: "serving-test", Ref: ref},
		Remote:   true,
	}); err != nil {
		t.Fatalf("second evict should be idempotent: %v", err)
	}
}

// TestEvictArtifactVolumePairingGuard proves a volume evict is refused while its
// current generation still pairs with a banked local bundle (standing decision 8).
func TestEvictArtifactVolumePairingGuard(t *testing.T) {
	fs := newFakeStore()
	s := newStoreTestServer(t, fs)
	ctx := context.Background()

	if err := s.volumes.Create("scratch-postgres", 1<<20); err != nil {
		t.Fatalf("create volume: %v", err)
	}
	gen, err := s.volumes.BumpGeneration("scratch-postgres")
	if err != nil {
		t.Fatalf("bump: %v", err)
	}
	// A banked bundle stamped with the CURRENT generation: the pair is live.
	s.statefulBundles.add(statefulBundleEntry{snapshotRef: "b1", workload: "scratch-postgres", generation: gen})

	_, err = s.EvictArtifact(ctx, &nodev1.EvictArtifactRequest{
		Artifact: &nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_VOLUME, Workload: "scratch-postgres"},
		Remote:   true,
	})
	if err == nil {
		t.Fatal("evicting a volume still paired with a banked bundle should be refused")
	}

	// After the bundle is gone, the evict is allowed.
	s.statefulBundles.remove("b1")
	if _, err := s.EvictArtifact(ctx, &nodev1.EvictArtifactRequest{
		Artifact: &nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_VOLUME, Workload: "scratch-postgres"},
		Remote:   true,
	}); err != nil {
		t.Fatalf("evict after unpairing should succeed: %v", err)
	}
}

// TestDrainingDoesNotBlockOnExportQueue proves a full export queue never blocks
// the enqueue path (fire-and-forget): even with no workers draining it, many
// enqueues return promptly rather than deadlocking a drain.
func TestDrainingDoesNotBlockOnExportQueue(t *testing.T) {
	fs := newFakeStore()
	s := newStoreTestServer(t, fs)
	// Start the queue with workers, then flood far past its depth. The enqueue
	// must never block even when the queue is saturated (it drops instead).
	s.startExportQueue(context.Background())

	done := make(chan struct{})
	go func() {
		for i := 0; i < exportQueueDepth*4; i++ {
			s.enqueueExport(&nodev1.ArtifactRef{
				Kind:     nodev1.ArtifactKind_ARTIFACT_KIND_SESSION,
				Workload: "w",
				Ref:      "flood-" + strconv.Itoa(i),
			})
		}
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("enqueue flood blocked; the export queue must never stall the caller")
	}
}

// TestArtifactPrefixVendorLayout proves the store key layout: vendor-bound
// kinds (BASE/SESSION/SERVING/STATEFUL/GROUP_SET) key as
// <kind>/<vendor>/<workload>/<ref>, and VOLUME stays unvendored at
// volume/<workload> (R7, standing decision 11: volumes are vendor-portable).
func TestArtifactPrefixVendorLayout(t *testing.T) {
	cases := []struct {
		name string
		ref  *nodev1.ArtifactRef
		want string
	}{
		{
			name: "stateful",
			ref:  &nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_STATEFUL, Workload: "pg", Ref: "r1"},
			want: "stateful/intel/pg/r1",
		},
		{
			name: "base",
			ref:  &nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_BASE, Workload: "echo", Ref: "b1"},
			want: "base/intel/echo/b1",
		},
		{
			name: "session",
			ref:  &nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_SESSION, Workload: "sbx", Ref: "s1"},
			want: "session/intel/sbx/s1",
		},
		{
			name: "serving",
			ref:  &nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_SERVING, Workload: "hot", Ref: "sv1"},
			want: "serving/intel/hot/sv1",
		},
		{
			name: "group_set",
			ref:  &nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_GROUP_SET, Workload: "grp", Ref: "set1"},
			want: "group_set/intel/grp/set1",
		},
		{
			name: "volume unvendored",
			ref:  &nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_VOLUME, Workload: "pg"},
			want: "volume/pg",
		},
		{
			name: "session workspace unvendored",
			ref:  &nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_SESSION_WORKSPACE, Workload: "sbx", Ref: "lineage-1"},
			want: "session-workspace/sbx/lineage-1",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := artifactPrefix(tc.ref, "intel"); got != tc.want {
				t.Errorf("artifactPrefix(%s) = %q, want %q", tc.name, got, tc.want)
			}
		})
	}
}

func TestSessionWorkspaceArtifactLocalDir(t *testing.T) {
	s := newStoreTestServer(t, newFakeStore())
	ref := &nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_SESSION_WORKSPACE, Workload: "sbx", Ref: "lineage-1"}
	want := filepath.Join(s.cfg.VolumeRoot, "session", "sbx", "lineage-1")
	if got := s.artifactLocalDir(ref); got != want {
		t.Fatalf("artifactLocalDir(session workspace) = %q, want %q", got, want)
	}
}

func TestArchiveVolume(t *testing.T) {
	t.Run("fast ACK and skipped when already durable", func(t *testing.T) {
		fs := newFakeStore()
		s := newStoreTestServer(t, fs)
		if err := s.volumes.CreateSession("sbx", "lineage-1", 1<<20); err != nil {
			t.Fatal(err)
		}
		resp, err := s.ArchiveVolume(context.Background(), &nodev1.ArchiveVolumeRequest{Workload: "sbx", LineageId: "lineage-1"})
		if err != nil || resp.GetSkipped() {
			t.Fatalf("ArchiveVolume first call = %#v, %v", resp, err)
		}
		s.startExportQueue(context.Background())
		s.enqueueExport(&nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_SESSION_WORKSPACE, Workload: "sbx", Ref: "lineage-1"})
		waitForExport(t, fs, "session-workspace/sbx/lineage-1")
		// A repeat archive re-enqueues rather than short-circuiting on presence:
		// the workspace key is stable but its content mutates, so only Export's
		// checksum compare may decide nothing needs uploading. Skipped stays
		// false; the worker's compare is the idempotency gate.
		resp, err = s.ArchiveVolume(context.Background(), &nodev1.ArchiveVolumeRequest{Workload: "sbx", LineageId: "lineage-1"})
		if err != nil || resp.GetSkipped() {
			t.Fatalf("ArchiveVolume repeat = %#v, %v", resp, err)
		}
	})

	t.Run("missing image", func(t *testing.T) {
		s := newStoreTestServer(t, newFakeStore())
		_, err := s.ArchiveVolume(context.Background(), &nodev1.ArchiveVolumeRequest{Workload: "sbx", LineageId: "gone"})
		if status.Code(err) != codes.NotFound {
			t.Fatalf("ArchiveVolume missing = %v, want NotFound", err)
		}
	})

	t.Run("nil store", func(t *testing.T) {
		s := newStoreTestServer(t, newFakeStore())
		s.store = nil
		_, err := s.ArchiveVolume(context.Background(), &nodev1.ArchiveVolumeRequest{Workload: "sbx", LineageId: "lineage-1"})
		if status.Code(err) != codes.FailedPrecondition {
			t.Fatalf("ArchiveVolume nil store = %v, want FailedPrecondition", err)
		}
	})
}

func TestRetireVolumeFastACKDeletesOnlyAfterDurableExport(t *testing.T) {
	fs := newFakeStore()
	s := newStoreTestServer(t, fs)
	if err := s.volumes.CreateSession("sbx", "lineage-retire", 1<<20); err != nil {
		t.Fatal(err)
	}
	s.startExportQueue(context.Background())
	resp, err := s.RetireVolume(context.Background(), &nodev1.RetireVolumeRequest{Workload: "sbx", LineageId: "lineage-retire"})
	if err != nil || resp == nil {
		t.Fatalf("RetireVolume = %#v, %v", resp, err)
	}
	if !s.volumes.HasRetirementIntent("sbx", "lineage-retire") {
		t.Fatal("retirement intent was not persisted")
	}
	waitForExport(t, fs, "session-workspace/sbx/lineage-retire")
	deadline := time.Now().Add(2 * time.Second)
	for s.volumes.HasRetirementIntent("sbx", "lineage-retire") && time.Now().Before(deadline) {
		time.Sleep(5 * time.Millisecond)
	}
	if s.volumes.HasRetirementIntent("sbx", "lineage-retire") {
		t.Fatal("retirement intent remained after durable export")
	}
}

// TestPrimeClearsPendingRetirementIntentOnAdopt is #4306 slice 2's groundwork
// for a future adopting prime (Slice 3): expiry may have already enqueued
// retirement (RetireVolume writes the intent, then export-then-delete) for a
// lineage before a new generation primes onto it. The prime must cancel that
// intent so the retirement retry sweep does not delete the volume out from
// under the live heir. No CP/proto change and no adoption logic exists yet
// (nothing drives this path live until Slice 3), so this proves the guard in
// isolation: pre-arm an intent as if RetireVolume had already run, then prime
// the same lineage and confirm the intent is gone.
func TestPrimeClearsPendingRetirementIntentOnAdopt(t *testing.T) {
	fs := newFakeStore()
	s := newStoreTestServer(t, fs)
	if err := s.volumes.CreateSession("sbx", "lineage-adopt", 1<<20); err != nil {
		t.Fatal(err)
	}
	if err := s.volumes.WriteRetirementIntent("sbx", "lineage-adopt"); err != nil {
		t.Fatal(err)
	}
	if !s.volumes.HasRetirementIntent("sbx", "lineage-adopt") {
		t.Fatal("retirement intent should be armed before the adopting prime")
	}

	seedBase(s, "sbx__deadbeef01", "sbx")
	resp, err := s.Prime(context.Background(), &nodev1.PrimeRequest{
		SnapshotRef:     "sbx__deadbeef01",
		LineageId:       "lineage-adopt",
		VolumeMount:     "/session",
		VolumeSizeBytes: 1 << 20,
	})
	if err != nil {
		t.Fatalf("Prime (adopting): %v", err)
	}
	if resp.GetVmId() == "" {
		t.Fatal("Prime returned empty vm_id")
	}
	if s.volumes.HasRetirementIntent("sbx", "lineage-adopt") {
		t.Fatal("adopting prime should have cleared the pending retirement intent")
	}
}

// TestPrimeClearRetirementIntentIsANoOpWithoutOne proves the unconditional call
// on every lineage prime (adopting or a plain first create) is harmless: no
// intent existed, so nothing must be cleared, and Prime must still succeed.
func TestPrimeClearRetirementIntentIsANoOpWithoutOne(t *testing.T) {
	fs := newFakeStore()
	s := newStoreTestServer(t, fs)

	seedBase(s, "sbx__deadbeef02", "sbx")
	resp, err := s.Prime(context.Background(), &nodev1.PrimeRequest{
		SnapshotRef:     "sbx__deadbeef02",
		LineageId:       "lineage-fresh",
		VolumeMount:     "/session",
		VolumeSizeBytes: 1 << 20,
	})
	if err != nil {
		t.Fatalf("Prime (fresh lineage, no pending intent): %v", err)
	}
	if resp.GetVmId() == "" {
		t.Fatal("Prime returned empty vm_id")
	}
	if s.volumes.HasRetirementIntent("sbx", "lineage-fresh") {
		t.Fatal("a fresh lineage prime must not create a retirement intent")
	}
}

// TestArtifactPrefixRefusesEmptyVendorForVendorBoundKind proves a vendor-bound
// kind composes NO prefix when the caller passes an empty vendor, so a caller
// that forgot to resolve one can never compose an ambiguous (ex-vendor) key.
func TestArtifactPrefixRefusesEmptyVendorForVendorBoundKind(t *testing.T) {
	ref := &nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_STATEFUL, Workload: "pg", Ref: "r1"}
	if got := artifactPrefix(ref, ""); got != "" {
		t.Errorf("artifactPrefix with empty vendor = %q, want empty", got)
	}
}

// TestRestoreArtifactLegacyAliasResolves proves standing decision 11's alias: an
// artifact present only under the pre-R7 un-vendored prefix restores when the
// requested vendor is "amd" (the node-4 alias), without needing a re-export
// under the new vendor-keyed layout.
func TestRestoreArtifactLegacyAliasResolves(t *testing.T) {
	fs := newFakeStore()
	s := newStoreTestServer(t, fs)
	ctx := context.Background()

	// Seed the store directly under the LEGACY (un-vendored) prefix, as if this
	// bundle had been exported before vendor keying shipped.
	legacyPrefix := "stateful/scratch-postgres/legacy-1"
	dir := filepath.Join(s.cfg.SnapshotRoot, "stateful", "legacy-1")
	writeBundleFiles(t, dir, map[string]string{"snapfile": "snap", "memfile": "mem"})
	if _, _, err := fs.Export(ctx, legacyPrefix, dir, []string{"snapfile", "memfile"}, 7, 0, "", ""); err != nil {
		t.Fatalf("seed legacy export: %v", err)
	}
	if err := os.RemoveAll(dir); err != nil {
		t.Fatalf("rm local bundle: %v", err)
	}

	resp, err := s.RestoreArtifact(ctx, &nodev1.RestoreArtifactRequest{
		Artifact: &nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_STATEFUL, Workload: "scratch-postgres", Ref: "legacy-1"},
		Vendor:   "amd",
	})
	if err != nil {
		t.Fatalf("restore via legacy alias should succeed: %v", err)
	}
	if resp.GetGeneration() != 7 {
		t.Fatalf("restored generation = %d, want 7 (from the legacy marker)", resp.GetGeneration())
	}
	if got, _ := os.ReadFile(filepath.Join(dir, "snapfile")); string(got) != "snap" {
		t.Fatalf("restored snapfile = %q, want snap", got)
	}
	// The vendor-keyed prefix was never populated; the legacy prefix served it.
	if fs.has("stateful/amd/scratch-postgres/legacy-1") {
		t.Fatal("legacy restore should read the legacy prefix directly, not populate the new vendor-keyed one")
	}
}

// TestEnqueueIfMissingLegacyAliasGatedToAMDNode proves the enqueueIfMissing
// legacy short-circuit is gated to the node's OWN vendor being the legacy alias
// ("amd"): an Intel node whose vendor-keyed prefix is absent must still enqueue
// its OWN export even when a same-workload/ref legacy (pre-R7, necessarily AMD)
// artifact happens to exist in the store. Without the gate, the Intel node would
// wrongly mark that unrelated AMD artifact as satisfying its own export and skip
// uploading, silently losing the Intel node's durability.
func TestEnqueueIfMissingLegacyAliasGatedToAMDNode(t *testing.T) {
	fs := newFakeStore()
	s := newStoreTestServerWithVendor(t, fs, "intel")
	ctx := context.Background()
	s.startExportQueue(ctx)

	// Seed the store with an artifact under the LEGACY (un-vendored) prefix, as
	// if a pre-R7 AMD node had exported it. The intel node's own vendor-keyed
	// prefix ("stateful/intel/scratch-postgres/r1") is deliberately left empty.
	legacyPrefix := "stateful/scratch-postgres/r1"
	legacySrc := t.TempDir()
	writeBundleFiles(t, legacySrc, map[string]string{"snapfile": "amd-snap"})
	if _, _, err := fs.Export(ctx, legacyPrefix, legacySrc, []string{"snapfile"}, 9, 0, "", ""); err != nil {
		t.Fatalf("seed legacy export: %v", err)
	}

	// This node's own local bundle for the same workload/ref, not yet exported.
	dir := filepath.Join(s.cfg.SnapshotRoot, "stateful", "r1")
	writeBundleFiles(t, dir, map[string]string{"snapfile": "intel-snap", "memfile": "intel-mem"})
	s.statefulBundles.add(statefulBundleEntry{snapshotRef: "r1", workload: "scratch-postgres", generation: 1})

	s.enqueueIfMissing(ctx, &nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_STATEFUL, Workload: "scratch-postgres", Ref: "r1"})
	waitForExport(t, fs, "stateful/intel/scratch-postgres/r1")

	// The unrelated legacy (AMD) artifact must be untouched by this node's export.
	if got, _, err := fs.Restore(ctx, legacyPrefix, t.TempDir()); err != nil || got == 0 {
		t.Fatalf("legacy artifact should be untouched, restore err=%v bytes=%d", err, got)
	}
}

// TestExportArtifactVolumeRefusedWhenUnblessed implements ADR embervm/011's
// declared invariant: "an artifact whose generation was never blessed is
// quarantined, never exported."
//
// VOLUME keys as a SINGLETON (volume/<workload>: no ref, no vendor segment,
// because volume data is vendor-portable), so every node that has ever held the
// volume writes the SAME object. The generation fence refuses an OLDER
// generation; this refuses the subtle case it cannot see. A node that self-bumped
// its ledger past the blessed watermark carries a HIGHER generation, so it would
// WIN the generation fence while holding state the control plane never blessed.
func TestExportArtifactVolumeRefusedWhenUnblessed(t *testing.T) {
	fs := newFakeStore()
	s := newStoreTestServer(t, fs)
	ctx := context.Background()

	// A self-bumped ledger: generation advances, the blessed marker never does.
	if err := s.volumes.Create("demo-postgres", 1); err != nil {
		t.Fatalf("Create volume: %v", err)
	}
	if _, err := s.volumes.BumpGeneration("demo-postgres"); err != nil {
		t.Fatalf("BumpGeneration: %v", err)
	}
	if s.volumes.GenerationBlessed("demo-postgres") {
		t.Fatal("precondition: a self-bumped generation must not read as blessed")
	}

	resp, err := s.ExportArtifact(ctx, &nodev1.ExportArtifactRequest{
		Artifact: &nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_VOLUME, Workload: "demo-postgres"},
	})
	if err != nil {
		t.Fatalf("ExportArtifact should ack a refusal, not error: %v", err)
	}
	if !resp.GetSkipped() || resp.GetBytesMoved() != 0 {
		t.Fatalf("refused export = (skipped=%v, moved=%d), want (true, 0)", resp.GetSkipped(), resp.GetBytesMoved())
	}
	if fs.has("volume/demo-postgres") {
		t.Fatal("an unblessed volume must NOT reach the store")
	}
}

// TestExportArtifactVolumeAllowedWhenBlessed is the other half: a CP-blessed
// generation exports normally. Without this the gate could pass by refusing
// everything.
func TestExportArtifactVolumeAllowedWhenBlessed(t *testing.T) {
	fs := newFakeStore()
	s := newStoreTestServer(t, fs)
	ctx := context.Background()

	if err := s.volumes.Create("demo-postgres", 1); err != nil {
		t.Fatalf("Create volume: %v", err)
	}
	// RecordBlessed writes BOTH the ledger and the blessed marker, which is what a
	// CP-issued blessed_generation does via attachGeneration.
	if _, err := s.volumes.RecordBlessed("demo-postgres", 7); err != nil {
		t.Fatalf("RecordBlessed: %v", err)
	}
	if !s.volumes.GenerationBlessed("demo-postgres") {
		t.Fatal("precondition: a CP-blessed generation must read as blessed")
	}

	resp, err := s.ExportArtifact(ctx, &nodev1.ExportArtifactRequest{
		Artifact: &nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_VOLUME, Workload: "demo-postgres"},
	})
	if err != nil {
		t.Fatalf("ExportArtifact: %v", err)
	}
	if resp.GetSkipped() {
		t.Fatal("a blessed volume export must not be skipped")
	}
	if !fs.has("volume/demo-postgres") {
		t.Fatal("store missing volume/demo-postgres after a blessed export")
	}
}

// TestRunExportJobVolumeExportsUnblessedWithWarning pins the OBSERVE-ONLY
// decision on the async queue. Enforcing ADR 011 here stopped volume durability
// fleet-wide (31 skips, zero successful volume exports) because `genblessed`
// only advances on a WAKE while `gen` bumps on every BANK, so a volume is almost
// never blessed at the moment its export-after-commit fires.
//
// The sync ExportArtifact path still REFUSES, and that asymmetry is the point:
// it is the path a control-plane-driven move uses, where an unblessed copy would
// be promoted to authoritative on a peer. Losing every off-node copy to guard a
// rarer, conditional hazard is the worse trade.
func TestRunExportJobVolumeExportsUnblessedWithWarning(t *testing.T) {
	fs := newFakeStore()
	s := newStoreTestServer(t, fs)
	ctx := context.Background()

	if err := s.volumes.Create("demo-postgres", 1); err != nil {
		t.Fatalf("Create volume: %v", err)
	}
	if _, err := s.volumes.BumpGeneration("demo-postgres"); err != nil {
		t.Fatalf("BumpGeneration: %v", err)
	}
	if s.volumes.GenerationBlessed("demo-postgres") {
		t.Fatal("precondition: a self-bumped generation must not read as blessed")
	}

	ref := &nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_VOLUME, Workload: "demo-postgres"}
	s.runExportJob(ctx, exportJob{ref: ref, key: artifactPrefix(ref, s.cfg.CpuVendor)})

	if !fs.has("volume/demo-postgres") {
		t.Fatal("durability regression: an unblessed volume must still reach the store via the async queue")
	}
}

// TestRunExportJobVolumeExportsWhenBlessed is the paired positive, so the async
// gate cannot pass by skipping everything.
func TestRunExportJobVolumeExportsWhenBlessed(t *testing.T) {
	fs := newFakeStore()
	s := newStoreTestServer(t, fs)
	ctx := context.Background()

	if err := s.volumes.Create("demo-postgres", 1); err != nil {
		t.Fatalf("Create volume: %v", err)
	}
	if _, err := s.volumes.RecordBlessed("demo-postgres", 7); err != nil {
		t.Fatalf("RecordBlessed: %v", err)
	}

	ref := &nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_VOLUME, Workload: "demo-postgres"}
	key := artifactPrefix(ref, s.cfg.CpuVendor)
	s.runExportJob(ctx, exportJob{ref: ref, key: key})

	if !fs.has("volume/demo-postgres") {
		t.Fatal("a blessed volume must reach the store via the async queue")
	}
}

// TestRunExportJobSkipsAttachedSessionWorkspace is #4306 slice 2's other half:
// the export worker must never export a SESSION_WORKSPACE whose lineage is
// currently attached to a live VM. A live guest is the single writer to this
// image (no generation ledger the way a VOLUME has), so exporting mid-write
// would produce a torn snapshot. Mirrors RetireVolume's own attached-lineage
// refusal (store.go's lineageAttached guard) via the same predicate, so the
// two cannot drift.
func TestRunExportJobSkipsAttachedSessionWorkspace(t *testing.T) {
	fs := newFakeStore()
	s := newStoreTestServer(t, fs)
	ctx := context.Background()

	seedBase(s, "sbx__deadbeef03", "sbx")
	resp, err := s.Prime(ctx, &nodev1.PrimeRequest{
		SnapshotRef:     "sbx__deadbeef03",
		LineageId:       "lineage-live",
		VolumeMount:     "/session",
		VolumeSizeBytes: 1 << 20,
	})
	if err != nil {
		t.Fatalf("Prime: %v", err)
	}
	if resp.GetVmId() == "" {
		t.Fatal("Prime returned empty vm_id")
	}
	if !s.lineageAttached("sbx", "lineage-live") {
		t.Fatal("precondition: the primed lineage must read as attached")
	}

	ref := &nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_SESSION_WORKSPACE, Workload: "sbx", Ref: "lineage-live"}
	key := artifactPrefix(ref, s.cfg.CpuVendor)
	s.runExportJob(ctx, exportJob{ref: ref, key: key})

	if fs.has(key) {
		t.Fatal("export must be skipped while the lineage is attached to a live VM")
	}
}

// TestRunExportJobExportsDetachedSessionWorkspace is the paired positive: once
// a lineage is not attached, the same job proceeds normally, so the guard
// above cannot pass by refusing every SESSION_WORKSPACE export.
func TestRunExportJobExportsDetachedSessionWorkspace(t *testing.T) {
	fs := newFakeStore()
	s := newStoreTestServer(t, fs)
	ctx := context.Background()

	if err := s.volumes.CreateSession("sbx", "lineage-detached", 1<<20); err != nil {
		t.Fatal(err)
	}
	if s.lineageAttached("sbx", "lineage-detached") {
		t.Fatal("precondition: a never-attached lineage must not read as attached")
	}

	ref := &nodev1.ArtifactRef{Kind: nodev1.ArtifactKind_ARTIFACT_KIND_SESSION_WORKSPACE, Workload: "sbx", Ref: "lineage-detached"}
	key := artifactPrefix(ref, s.cfg.CpuVendor)
	s.runExportJob(ctx, exportJob{ref: ref, key: key})

	if !fs.has(key) {
		t.Fatal("a detached lineage's workspace export must proceed")
	}
}
