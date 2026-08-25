package server

import (
	"errors"
	"os"
	"path/filepath"
	"testing"

	"github.com/jomcgi/homelab/projects/embervm/noded/config"
	nodev1 "github.com/jomcgi/homelab/projects/embervm/proto/embervm/node/v1"
)

func TestParseSnapVersion(t *testing.T) {
	cases := []struct {
		name string
		out  string
		want snapVersion
		ok   bool
	}{
		{"plain", "v10.0.0\n", snapVersion{10, 0, 0}, true},
		{"no v prefix", "10.0.0\n", snapVersion{10, 0, 0}, true},
		{"among log noise", "2026-08-25T00:00:00.000 [x:main] booting\nv9.4.1\nmore noise exit_code=0\n", snapVersion{9, 4, 1}, true},
		{"crlf", "v10.2.3\r\n", snapVersion{10, 2, 3}, true},
		{"empty", "", snapVersion{}, false},
		{"garbage", "bitcode error\n", snapVersion{}, false},
		{"two parts only", "v10.0\n", snapVersion{}, false},
		{"non numeric", "v10.x.0\n", snapVersion{}, false},
		{
			// A log-style line must never parse as a version even though it embeds
			// dotted numbers; whole-line matching keeps stdout noise inert.
			"embedded numbers are not a version",
			"2026-08-25T00:00:00.000 [x:main] Firecracker exiting successfully. exit_code=0\n",
			snapVersion{},
			false,
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, ok := parseSnapVersion(tc.out)
			if ok != tc.ok || got != tc.want {
				t.Fatalf("parseSnapVersion(%q) = %+v, %v; want %+v, %v", tc.out, got, ok, tc.want, tc.ok)
			}
		})
	}
}

func TestSnapshotFormatCompatible(t *testing.T) {
	bin := snapVersion{Major: 10, Minor: 2, Patch: 0}
	cases := []struct {
		name string
		file snapVersion
		want bool
	}{
		{"identical", snapVersion{10, 2, 0}, true},
		{"older minor any patch", snapVersion{10, 1, 7}, true},
		{"same major zero minor", snapVersion{10, 0, 99}, true},
		{"newer minor refused", snapVersion{10, 3, 0}, false},
		{"older major refused", snapVersion{9, 9, 9}, false},
		{"newer major refused", snapVersion{11, 0, 0}, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := snapshotFormatCompatible(tc.file, bin); got != tc.want {
				t.Fatalf("snapshotFormatCompatible(%s, %s) = %v, want %v", tc.file, bin, got, tc.want)
			}
		})
	}
}

// newSnapFormatTestServer builds a Server over an empty snapshot root with the
// two format probes faked to the given versions/errors.
func newSnapFormatTestServer(t *testing.T, dir string) *Server {
	t.Helper()
	s := New(Options{
		Config: config.Config{
			Arch: "amd64", Node: "node-4", MaxLiveVMs: 4, SnapshotRoot: dir,
			Images: map[string]config.Image{},
		},
	})
	s.fcSupportedVersionFn = func(string) (snapVersion, error) { return snapVersion{10, 2, 0}, nil }
	s.fcDescribeVersionFn = func(_, _ string) (snapVersion, error) { return snapVersion{10, 2, 0}, nil }
	return s
}

// TestReconcileAdoptsFormatCompatibleBase proves the happy path is unchanged: a
// base whose snapfile format the local binary supports is adopted READY exactly
// as before #4407.
func TestReconcileAdoptsFormatCompatibleBase(t *testing.T) {
	dir := t.TempDir()
	s := newSnapFormatTestServer(t, dir)

	basesDir := filepath.Join(dir, "bases")
	writeReconcileBase(t, basesDir, "wl-a__current", "img-current")
	s.ReconcileBasesFromDisk()

	got, ok := s.bases.get("wl-a__current")
	if !ok || got.state != nodev1.BaseBuildState_BASE_BUILD_STATE_READY {
		t.Fatalf("compatible base = %+v ok=%v; want adopted READY", got, ok)
	}
}

// TestReconcileRefusesIncompatibleSnapshotFormat proves a base whose recorded
// data format the local binary cannot load (here: a newer minor from a newer
// Firecracker) is reported BASE_BUILD_STATE_NONE with the reason in buildErr,
// its dir SURVIVES on disk, and the capacity projection advertises the workload
// affirmatively absent so the control plane takes the normal rebuild path
// instead of dispatching into guaranteed LoadSnapshot aborts.
func TestReconcileRefusesIncompatibleSnapshotFormat(t *testing.T) {
	dir := t.TempDir()
	s := newSnapFormatTestServer(t, dir)
	const ref = "wl-a__newer"
	s.fcDescribeVersionFn = func(_, _ string) (snapVersion, error) {
		return snapVersion{Major: 10, Minor: 3, Patch: 0}, nil // newer than binary's 10.2
	}

	basesDir := filepath.Join(dir, "bases")
	writeReconcileBase(t, basesDir, ref, "img-current")
	s.ReconcileBasesFromDisk()

	got, ok := s.bases.get(ref)
	if !ok {
		t.Fatalf("refused base %q missing from registry; want a NONE entry", ref)
	}
	if got.state != nodev1.BaseBuildState_BASE_BUILD_STATE_NONE {
		t.Fatalf("refused base state = %v; want BASE_BUILD_STATE_NONE", got.state)
	}
	if got.buildErr == "" {
		t.Fatal("refused base should carry the refusal reason in buildErr")
	}
	if _, err := os.Stat(filepath.Join(basesDir, ref)); err != nil {
		t.Fatalf("refused base dir should survive on disk, got stat err %v", err)
	}

	// The capacity projection reports the workload affirmatively absent (state
	// NONE with the stale ref named), which is what node_reports_base_absent?
	// matches to drive hydrate/rebuild.
	var capEntry *nodev1.WorkloadCapacity
	for _, c := range s.workloadCapacities(map[string][]string{}) {
		if c.GetSnapshotRef() == ref {
			capEntry = c
			break
		}
	}
	if capEntry == nil {
		t.Fatal("capacity projection never reported the refused base ref")
	}
	if capEntry.GetBaseState() != nodev1.BaseBuildState_BASE_BUILD_STATE_NONE {
		t.Fatalf("capacity base_state = %v; want NONE so the control plane rebuilds", capEntry.GetBaseState())
	}

	// Re-running the reconcile is idempotent: still NONE, still present.
	s.ReconcileBasesFromDisk()
	if got, ok := s.bases.get(ref); !ok || got.state != nodev1.BaseBuildState_BASE_BUILD_STATE_NONE {
		t.Fatalf("re-reconciled base = %+v ok=%v; want still NONE and registered", got, ok)
	}
}

// TestReconcileRefusesUndescribableSnapfile proves a snapfile firecracker itself
// cannot read (corrupt, or foreign enough that its bitcode structs fail to
// parse) is refused: restore runs the identical parse plus stricter checks, so
// adopting it READY just schedules per-dispatch aborts.
func TestReconcileRefusesUndescribableSnapfile(t *testing.T) {
	dir := t.TempDir()
	s := newSnapFormatTestServer(t, dir)
	s.fcDescribeVersionFn = func(_, _ string) (snapVersion, error) {
		return snapVersion{}, errors.New("exit status 1: bitcode error")
	}

	basesDir := filepath.Join(dir, "bases")
	writeReconcileBase(t, basesDir, "wl-b__junk", "img-junk")
	s.ReconcileBasesFromDisk()

	got, ok := s.bases.get("wl-b__junk")
	if !ok || got.state != nodev1.BaseBuildState_BASE_BUILD_STATE_NONE {
		t.Fatalf("undescribable base = %+v ok=%v; want NONE", got, ok)
	}
	if got.buildErr == "" {
		t.Fatal("undescribable base should carry the failure reason in buildErr")
	}
}

// TestReconcileFailsOpenWhenBinaryProbeFails proves the fail-open rule: when
// noded cannot learn its own binary's supported format (BinPath unset in unit
// tests, exec failing), validation is skipped entirely, the describe probe is
// never consulted, and adoption behaves exactly as before #4407.
func TestReconcileFailsOpenWhenBinaryProbeFails(t *testing.T) {
	dir := t.TempDir()
	s := newSnapFormatTestServer(t, dir)
	s.fcSupportedVersionFn = func(string) (snapVersion, error) {
		return snapVersion{}, errors.New("fork/exec /opt/fc/firecracker: no such file or directory")
	}
	describes := 0
	s.fcDescribeVersionFn = func(_, _ string) (snapVersion, error) {
		describes++
		return snapVersion{}, errors.New("must not be called while support is unknown")
	}

	basesDir := filepath.Join(dir, "bases")
	writeReconcileBase(t, basesDir, "wl-c__current", "img-current")
	s.ReconcileBasesFromDisk()

	got, ok := s.bases.get("wl-c__current")
	if !ok || got.state != nodev1.BaseBuildState_BASE_BUILD_STATE_READY {
		t.Fatalf("base under unknown binary support = %+v ok=%v; want adopted READY (fail open)", got, ok)
	}
	if describes != 0 {
		t.Fatalf("describe probe called %d times; want 0 while supported version is unknown", describes)
	}
}

// TestReconcileCachesSupportedFormatProbe proves the supported-version probe
// runs once across multiple reconciles: the binary is baked into the image and
// cannot change under a running pod, so re-probing per adoption is waste.
func TestReconcileCachesSupportedFormatProbe(t *testing.T) {
	dir := t.TempDir()
	s := newSnapFormatTestServer(t, dir)
	probes := 0
	s.fcSupportedVersionFn = func(string) (snapVersion, error) {
		probes++
		return snapVersion{Major: 10, Minor: 2, Patch: 0}, nil
	}

	basesDir := filepath.Join(dir, "bases")
	writeReconcileBase(t, basesDir, "wl-d__one", "img-one")
	s.ReconcileBasesFromDisk()
	writeReconcileBase(t, basesDir, "wl-d__two", "img-two")
	s.ReconcileBasesFromDisk()

	if probes != 1 {
		t.Fatalf("supported-version probe ran %d times across two reconciles; want 1", probes)
	}
}
