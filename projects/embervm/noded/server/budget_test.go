package server

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

// withBudgetFsRoot points budgetFsRoot at a fixture directory for the
// duration of the test, writing the given cgroup v2 files into it.
func withBudgetFsRoot(t *testing.T, files map[string]string) {
	t.Helper()
	dir := t.TempDir()
	for name, content := range files {
		if err := os.WriteFile(filepath.Join(dir, name), []byte(content), 0o644); err != nil {
			t.Fatalf("write fixture %s: %v", name, err)
		}
	}
	orig := budgetFsRoot
	budgetFsRoot = dir
	t.Cleanup(func() { budgetFsRoot = orig })
}

func writeBudgetFile(t *testing.T, name, content string) {
	t.Helper()
	if err := os.WriteFile(filepath.Join(budgetFsRoot, name), []byte(content), 0o644); err != nil {
		t.Fatalf("write fixture %s: %v", name, err)
	}
}

func TestBudgetReadsCgroupV2(t *testing.T) {
	withBudgetFsRoot(t, map[string]string{
		"memory.max":     "4294967296\n",    // 4 GiB
		"memory.current": "1073741824\n",    // 1 GiB
		"cpu.max":        "200000 100000\n", // 2 cores
		"cpu.stat":       "usage_usec 1000000\nnr_periods 5\n",
	})
	b := newBudget(512)

	if got, want := b.MemBudgetMib(), uint64(4096-512); got != want {
		t.Errorf("MemBudgetMib() = %d, want %d", got, want)
	}
	if got, want := b.MemHeadroomMib(), uint64(4096-1024); got != want {
		t.Errorf("MemHeadroomMib() = %d, want %d", got, want)
	}
	if got, want := b.CpuBudgetMillicores(), uint64(2000); got != want {
		t.Errorf("CpuBudgetMillicores() = %d, want %d", got, want)
	}
}

func TestUnlimitedCgroupReportsZero(t *testing.T) {
	withBudgetFsRoot(t, map[string]string{
		"memory.max":     "max\n",
		"memory.current": "1073741824\n",
		"cpu.max":        "max 100000\n",
		"cpu.stat":       "usage_usec 1000000\n",
	})
	b := newBudget(512)

	if got := b.MemBudgetMib(); got != 0 {
		t.Errorf("MemBudgetMib() on unlimited cgroup = %d, want 0 (unknown)", got)
	}
	if got := b.MemHeadroomMib(); got != 0 {
		t.Errorf("MemHeadroomMib() on unlimited cgroup = %d, want 0", got)
	}
	if got := b.CpuBudgetMillicores(); got != 0 {
		t.Errorf("CpuBudgetMillicores() on unlimited cgroup = %d, want 0 (unknown)", got)
	}
}

func TestResizeObservedOnRefresh(t *testing.T) {
	withBudgetFsRoot(t, map[string]string{
		"memory.max":     "2147483648\n", // 2 GiB
		"memory.current": "1073741824\n",
		"cpu.max":        "100000 100000\n", // 1 core
		"cpu.stat":       "usage_usec 0\n",
	})
	b := newBudget(0)

	if got, want := b.MemBudgetMib(), uint64(2048); got != want {
		t.Fatalf("MemBudgetMib() before resize = %d, want %d", got, want)
	}

	// Simulate an in-place pod resize (ADR 012): the cgroup's ceiling changes
	// underneath the running daemon.
	writeBudgetFile(t, "memory.max", "4294967296\n") // 4 GiB
	writeBudgetFile(t, "cpu.max", "200000 100000\n") // 2 cores

	if got, want := b.MemBudgetMib(), uint64(4096); got != want {
		t.Errorf("MemBudgetMib() after resize = %d, want %d (resize must be observed without restart)", got, want)
	}

	b.Refresh()
	if got, want := b.CpuBudgetMillicores(), uint64(2000); got != want {
		t.Errorf("CpuBudgetMillicores() after resize = %d, want %d", got, want)
	}
}

func TestCpuHeadroomFromUsageDelta(t *testing.T) {
	withBudgetFsRoot(t, map[string]string{
		"memory.max":     "max\n",
		"memory.current": "0\n",
		"cpu.max":        "200000 100000\n", // 2000 millicores budget
		"cpu.stat":       "usage_usec 0\n",
	})
	b := newBudget(0)

	// First sample only seeds the baseline; headroom is unknown (0) until a
	// second sample exists.
	b.Refresh()
	if got := b.CpuHeadroomMillicores(); got != 0 {
		t.Errorf("CpuHeadroomMillicores() after first sample = %d, want 0 (no delta yet)", got)
	}

	// Second sample exactly one second later, having used 1000000usec (1 full
	// core) of CPU time in that window: usage rate is 1000 millicores, so
	// headroom against a 2000-millicore budget is 1000. Pin both instants via
	// the injected clock so the delta is exactly 1s (racing two real time.Now
	// calls yields a sub-microsecond-off elapsed and an off-by-one headroom).
	base := b.lastSampleAt
	b.now = func() time.Time { return base.Add(1 * time.Second) }
	writeBudgetFile(t, "cpu.stat", "usage_usec 1000000\n")
	b.Refresh()

	if got, want := b.CpuHeadroomMillicores(), uint64(1000); got != want {
		t.Errorf("CpuHeadroomMillicores() after second sample = %d, want %d", got, want)
	}
}

func TestParseCpuBudgetMillicores(t *testing.T) {
	cases := []struct {
		raw  string
		want uint64
	}{
		{"max 100000\n", 0},
		{"100000 100000\n", 1000},
		{"200000 100000\n", 2000},
		{"50000 100000\n", 500},
		{"garbage\n", 0},
		{"", 0},
	}
	for _, c := range cases {
		if got := parseCpuBudgetMillicores(c.raw); got != c.want {
			t.Errorf("parseCpuBudgetMillicores(%q) = %d, want %d", c.raw, got, c.want)
		}
	}
}

func TestParseCpuUsageUsec(t *testing.T) {
	cases := []struct {
		raw    string
		want   uint64
		wantOk bool
	}{
		{"usage_usec 12345\nnr_periods 1\n", 12345, true},
		{"nr_periods 1\nusage_usec 999\n", 999, true},
		{"nr_periods 1\n", 0, false},
		{"", 0, false},
		{"usage_usec garbage\n", 0, false},
	}
	for _, c := range cases {
		got, ok := parseCpuUsageUsec(c.raw)
		if got != c.want || ok != c.wantOk {
			t.Errorf("parseCpuUsageUsec(%q) = (%d, %v), want (%d, %v)", c.raw, got, ok, c.want, c.wantOk)
		}
	}
}

// withSelfCgroup points selfCgroupPath at a fixture /proc/self/cgroup file for
// the duration of the test.
func withSelfCgroup(t *testing.T, content string) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "self-cgroup")
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatalf("write self-cgroup fixture: %v", err)
	}
	orig := selfCgroupPath
	selfCgroupPath = path
	t.Cleanup(func() { selfCgroupPath = orig })
}

func TestParseSelfCgroupV2Path(t *testing.T) {
	cases := []struct {
		raw  string
		want string
	}{
		// Host cgroup namespace: the pod's leaf scope under kubepods.
		{"0::/kubepods.slice/kubepods-burstable.slice/kubepods-burstable-podabc.slice/cri-containerd-def.scope\n", "/kubepods.slice/kubepods-burstable.slice/kubepods-burstable-podabc.slice/cri-containerd-def.scope"},
		// Private cgroup namespace: root path, resolution falls back to budgetFsRoot.
		{"0::/\n", "/"},
		// Legacy v1 lines present but no v2 unified line.
		{"12:memory:/kubepods\n11:cpu:/kubepods\n", ""},
		// v2 line among v1 lines.
		{"1:name=systemd:/foo\n0::/leaf.scope\n", "/leaf.scope"},
		{"", ""},
	}
	for _, c := range cases {
		if got := parseSelfCgroupV2Path(c.raw); got != c.want {
			t.Errorf("parseSelfCgroupV2Path(%q) = %q, want %q", c.raw, got, c.want)
		}
	}
}

// TestCgroupDirResolvesHostNsPath proves the host-cgroupns case: /proc/self/cgroup
// names a leaf scope under budgetFsRoot, and cgroupDir resolves the reader to
// that leaf (where memory.max actually lives), NOT the controller-file-less
// root the privileged container's plain /sys/fs/cgroup points at.
func TestCgroupDirResolvesHostNsPath(t *testing.T) {
	root := t.TempDir()
	orig := budgetFsRoot
	budgetFsRoot = root
	t.Cleanup(func() { budgetFsRoot = orig })

	// The host root has NO memory.max (the real bug), the pod leaf does.
	leaf := filepath.Join(root, "kubepods.slice", "cri-containerd-abc.scope")
	if err := os.MkdirAll(leaf, 0o755); err != nil {
		t.Fatalf("mkdir leaf: %v", err)
	}
	if err := os.WriteFile(filepath.Join(leaf, "memory.max"), []byte("2147483648\n"), 0o644); err != nil {
		t.Fatalf("write leaf memory.max: %v", err)
	}
	if err := os.WriteFile(filepath.Join(leaf, "memory.current"), []byte("536870912\n"), 0o644); err != nil {
		t.Fatalf("write leaf memory.current: %v", err)
	}
	withSelfCgroup(t, "0::/kubepods.slice/cri-containerd-abc.scope\n")

	if got, want := cgroupDir(), leaf; got != want {
		t.Fatalf("cgroupDir() = %q, want leaf %q", got, want)
	}

	b := newBudget(512)
	// 2 GiB - 512 MiB reserve = 1536, the exact 2gi-brick expectation.
	if got, want := b.MemBudgetMib(), uint64(2048-512); got != want {
		t.Errorf("MemBudgetMib() host-ns = %d, want %d", got, want)
	}
	if got, want := b.MemHeadroomMib(), uint64(2048-512); got != want {
		t.Errorf("MemHeadroomMib() host-ns = %d, want %d", got, want)
	}
}

// TestCgroupDirFallsBackOnRootSelfPath proves the private-cgroupns case: a
// "0::/" self-path means budgetFsRoot IS already the pod's cgroup, so cgroupDir
// returns budgetFsRoot unchanged and the reader behaves exactly as before the
// resolution was added.
func TestCgroupDirFallsBackOnRootSelfPath(t *testing.T) {
	root := t.TempDir()
	orig := budgetFsRoot
	budgetFsRoot = root
	t.Cleanup(func() { budgetFsRoot = orig })
	if err := os.WriteFile(filepath.Join(root, "memory.max"), []byte("4294967296\n"), 0o644); err != nil {
		t.Fatalf("write root memory.max: %v", err)
	}
	withSelfCgroup(t, "0::/\n")

	if got, want := cgroupDir(), root; got != want {
		t.Fatalf("cgroupDir() on 0::/ = %q, want budgetFsRoot %q", got, want)
	}
	b := newBudget(512)
	if got, want := b.MemBudgetMib(), uint64(4096-512); got != want {
		t.Errorf("MemBudgetMib() private-ns fallback = %d, want %d", got, want)
	}
}

// TestCgroupDirFallsBackWhenJoinedDirLacksControllers proves the graceful-
// degradation path: a host-ns self-path whose joined dir does NOT exist (or has
// no memory.max) under this root falls back to budgetFsRoot rather than reading
// nothing. This is also why the pre-existing budget_test fixtures (files written
// at budgetFsRoot itself) keep working with an unset selfCgroupPath.
func TestCgroupDirFallsBackWhenJoinedDirLacksControllers(t *testing.T) {
	root := t.TempDir()
	orig := budgetFsRoot
	budgetFsRoot = root
	t.Cleanup(func() { budgetFsRoot = orig })
	if err := os.WriteFile(filepath.Join(root, "memory.max"), []byte("4294967296\n"), 0o644); err != nil {
		t.Fatalf("write root memory.max: %v", err)
	}
	// Self-path names a leaf that does not exist under root.
	withSelfCgroup(t, "0::/kubepods.slice/nonexistent.scope\n")

	if got, want := cgroupDir(), root; got != want {
		t.Fatalf("cgroupDir() with missing leaf = %q, want fallback %q", got, want)
	}
}

func TestSlotCeiling(t *testing.T) {
	cases := []struct {
		name       string
		budgetMib  uint64
		configured uint64
		want       uint64
	}{
		// 2gi brick, 512 reserve -> 1536 budget -> floor(1536/512)=3 slots,
		// clamped under the configured default of 8 (the whole point: not 8/16).
		{"2gi-brick", 1536, 8, 3},
		// 4gi brick -> 3584 budget -> 7 slots, still under configured 8.
		{"4gi-brick", 3584, 8, 7},
		// 16gi brick -> derived exceeds configured 8, so configured clamps it.
		{"large-brick-clamped", 15872, 8, 8},
		// Unknown budget (unlimited/unreadable cgroup) -> configured unchanged.
		{"unknown-budget", 0, 8, 8},
		// Budget smaller than one slot still reports at least 1.
		{"sub-slot-budget", 256, 8, 1},
		// Configured 0 (unbounded backstop) -> report the derived ceiling.
		{"unbounded-config", 1536, 0, 3},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			b := newBudget(0)
			b.memBudgetOverride = func() uint64 { return c.budgetMib }
			if got := b.SlotCeiling(c.configured); got != c.want {
				t.Errorf("SlotCeiling(%d) with budget %d = %d, want %d", c.configured, c.budgetMib, got, c.want)
			}
		})
	}
}

func TestParseMemBudgetMib(t *testing.T) {
	cases := []struct {
		maxRaw     string
		reserveMib uint64
		want       uint64
	}{
		{"max\n", 512, 0},
		{"4294967296\n", 512, 3584}, // 4096 - 512
		{"536870912\n", 512, 0},     // 512MiB - 512MiB reserve = 0
		{"garbage", 512, 0},
	}
	for _, c := range cases {
		if got := parseMemBudgetMib(c.maxRaw, c.reserveMib); got != c.want {
			t.Errorf("parseMemBudgetMib(%q, %d) = %d, want %d", c.maxRaw, c.reserveMib, got, c.want)
		}
	}
}
