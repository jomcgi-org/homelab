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
		"memory.max":     "4294967296\n", // 4 GiB
		"memory.current": "1073741824\n", // 1 GiB
		"memory.stat":    "file 0\n",
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

// TestExhaustedCgroupHeadroomIsKnown distinguishes a full cgroup from an
// unlimited one without rounding a finite zero up to an allocatable MiB.
func TestExhaustedCgroupHeadroomIsKnown(t *testing.T) {
	withBudgetFsRoot(t, map[string]string{
		"memory.max":     "4294967296\n",
		"memory.current": "4294967296\n",
		"memory.stat":    "file 0\n",
	})
	if got, known := newBudget(0).memHeadroomReading(); got != 0 || !known {
		t.Fatalf("exhausted headroom = (%d, %t), want (0, true)", got, known)
	}
}

func TestMemHeadroomFallsBackWhenMemoryStatUnreadable(t *testing.T) {
	withBudgetFsRoot(t, map[string]string{
		"memory.max":     "4294967296\n",
		"memory.current": "1073741824\n",
	})

	if got, want := newBudget(0).MemHeadroomMib(), uint64(3072); got != want {
		t.Errorf("MemHeadroomMib() without memory.stat = %d, want %d", got, want)
	}
}

func TestParseMemHeadroomMibWithPageCache(t *testing.T) {
	cases := []struct {
		name                    string
		maxRaw, curRaw, statRaw string
		want                    uint64
		min                     uint64
	}{
		// #4058 was the same bug on node-2, with only 13 percent of cache active.
		// Its fix subtracted inactive_file alone and passed because of that ratio.
		// The ratio reflects recent access patterns, not the workload itself.
		// That is why #4157 recurred once active_file reached half the cache.
		{
			name:    "issue 4058 reclaimable cache regression",
			maxRaw:  "2147483648",
			curRaw:  "1482084352",
			statRaw: "file 1456840704\nshmem 0\n",
			want:    2023,
			min:     1024,
		},
		{
			name:    "issue 4157 production cache regression",
			maxRaw:  "2147483648",
			curRaw:  "2144251904",
			statRaw: "file 2111016960\nshmem 0\n",
			want:    2016,
			min:     1024,
		},
		{
			name:    "shmem is not reclaimable",
			maxRaw:  "2147483648",
			curRaw:  "1073741824",
			statRaw: "file 1073741824\nshmem 1073741824\n",
			want:    1024,
		},
		{
			name:    "racy file sample is clamped to current",
			maxRaw:  "2147483648",
			curRaw:  "536870912",
			statRaw: "file 1073741824\n",
			want:    2048,
		},
		{
			name:    "missing shmem defaults to zero",
			maxRaw:  "4294967296",
			curRaw:  "1073741824",
			statRaw: "file 987654\n",
			want:    3072,
		},
		{
			name:    "missing stat",
			maxRaw:  "4294967296",
			curRaw:  "1073741824",
			statRaw: "",
			want:    3072,
		},
		{
			name:    "max is unknown",
			maxRaw:  "max",
			curRaw:  "1073741824",
			statRaw: "file 1\n",
			want:    0,
		},
		{
			name:    "empty max is unknown",
			maxRaw:  "",
			curRaw:  "1073741824",
			statRaw: "file 1\n",
			want:    0,
		},
		{
			name:    "invalid max",
			maxRaw:  "garbage",
			curRaw:  "1",
			statRaw: "file 1\n",
			want:    0,
		},
		{
			name:    "invalid current",
			maxRaw:  "2147483648",
			curRaw:  "garbage",
			statRaw: "file 1\n",
			want:    0,
		},
		{
			name:    "max is no greater than nonreclaimable is known exhausted",
			maxRaw:  "2147483648",
			curRaw:  "2147483648",
			statRaw: "file 1\nshmem 1\n",
			want:    0,
		},
		{
			name:    "free under 1 MiB is known, not unknown",
			maxRaw:  "4294967296",
			curRaw:  "4294967000",
			statRaw: "file 0\nshmem 0\n",
			want:    0,
		},
		{
			name:    "current overshooting max is known exhausted",
			maxRaw:  "4294967296",
			curRaw:  "4295229440",
			statRaw: "file 0\n",
			want:    0,
		},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got := parseMemHeadroomMib(c.maxRaw, c.curRaw, c.statRaw)
			if got != c.want {
				t.Errorf("parseMemHeadroomMib(%q, %q, %q) = %d, want %d", c.maxRaw, c.curRaw, c.statRaw, got, c.want)
			}
			if c.min != 0 && got <= c.min {
				t.Errorf("parseMemHeadroomMib(%q, %q, %q) = %d, want comfortably above %d", c.maxRaw, c.curRaw, c.statRaw, got, c.min)
			}
		})
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
	if err := os.WriteFile(filepath.Join(leaf, "memory.stat"), []byte("file 0\n"), 0o644); err != nil {
		t.Fatalf("write leaf memory.stat: %v", err)
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
		configured uint64
	}{
		{"configured-default", 8},
		{"configured-small", 2},
		{"configured-unlimited", 0},
		{"configured-large", 64},
	}
	// Memory admission gates on headroom (need + floor), not count.
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			b := newBudget(0)
			if got := b.SlotCeiling(c.configured); got != c.configured {
				t.Errorf("SlotCeiling(%d) = %d, want configured value", c.configured, got)
			}
		})
	}

	b := newBudget(0)
	if got := b.SlotCeiling(8); got != 8 {
		t.Fatalf("SlotCeiling with unknown budget = %d, want 8", got)
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

func TestMemoryReadingDistinguishesFiniteZero(t *testing.T) {
	for _, tc := range []struct {
		name, max, current         string
		reserve                    uint64
		budget, headroom           uint64
		budgetKnown, headroomKnown bool
	}{
		{"zero limit", "0", "0", 0, 0, 0, true, true},
		{"sub MiB", "524288", "0", 0, 0, 0, true, true},
		{"reserve consumes capacity", "1048576", "0", 1, 0, 1, true, true},
		{"usage at limit", "1048576", "1048576", 0, 1, 0, true, true},
		{"usage above limit", "1048576", "2097152", 0, 1, 0, true, true},
		{"sub MiB headroom", "2097152", "1572864", 0, 2, 0, true, true},
		{"unlimited", "max", "0", 0, 0, 0, false, false},
		{"invalid limit", "invalid", "0", 0, 0, 0, false, false},
		{"invalid usage", "1048576", "invalid", 0, 1, 0, true, false},
		{"unsigned limit", "18446744073709551615", "0", 0, (1 << 44) - 1, (1 << 44) - 1, true, true},
		{"overflowing limit", "18446744073709551616", "0", 0, 0, 0, false, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			withBudgetFsRoot(t, map[string]string{"memory.max": tc.max, "memory.current": tc.current})
			got := newBudget(tc.reserve).readMemory()
			want := memoryReading{budgetMib: tc.budget, headroomMib: tc.headroom, budgetKnown: tc.budgetKnown, headroomKnown: tc.headroomKnown}
			if got != want {
				t.Fatalf("memory reading = %+v, want %+v", got, want)
			}
		})
	}
}

func TestMemoryReadingAccountsForFiniteAncestorsAndSiblingUsage(t *testing.T) {
	root := t.TempDir()
	oldRoot, oldSelf := budgetFsRoot, selfCgroupPath
	budgetFsRoot, selfCgroupPath = root, filepath.Join(root, "self")
	t.Cleanup(func() { budgetFsRoot, selfCgroupPath = oldRoot, oldSelf })
	if err := os.WriteFile(selfCgroupPath, []byte("0::/pod/container/embervm-noded-daemon\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	write := func(dir, max, current string) {
		t.Helper()
		path := filepath.Join(root, dir)
		if err := os.MkdirAll(path, 0o755); err != nil {
			t.Fatal(err)
		}
		for name, value := range map[string]string{"memory.max": max, "memory.current": current} {
			if err := os.WriteFile(filepath.Join(path, name), []byte(value), 0o600); err != nil {
				t.Fatal(err)
			}
		}
	}
	write("pod", "8589934592", "8053063680") // 8 GiB ceiling, only 512 MiB available.
	write("pod/container", "4294967296", "1073741824")
	write("pod/container/embervm-noded-daemon", "max", "134217728")
	b := newBudget(256)
	got := b.readMemory()
	if got.budgetMib != 3840 || got.headroomMib != 512 || !got.budgetKnown || !got.headroomKnown {
		t.Fatalf("ancestor reading = %+v, want budget 3840 and headroom 512", got)
	}
	// A finite exhausted ancestor must not be discarded because its leaf or
	// another ancestor still has headroom. All reads must observe resize.
	write("pod", "8589934592", "8589934592")
	if got := b.readMemory(); !got.headroomKnown || got.headroomMib != 0 {
		t.Fatalf("exhausted parent reading = %+v", got)
	}
	write("pod/container", "0", "0")
	if got := b.readMemory(); !got.budgetKnown || got.budgetMib != 0 {
		t.Fatalf("zero ancestor limit reading = %+v", got)
	}
}

// A private cgroup namespace only exposes its own root, so an apparent limit
// above that mount is not part of this reader's visible hierarchy.
func TestMemoryReadingStopsAtMountRoot(t *testing.T) {
	outer := t.TempDir()
	root := filepath.Join(outer, "mount")
	writeCgroupFixture(t, outer, map[string]string{"memory.max": "0", "memory.current": "0"})
	writeCgroupFixture(t, root, map[string]string{"memory.max": "4194304", "memory.current": "1048576"})
	oldRoot := budgetFsRoot
	budgetFsRoot = root
	t.Cleanup(func() { budgetFsRoot = oldRoot })
	withSelfCgroup(t, "0::/\n")
	got := newBudget(1).readMemory()
	if got != (memoryReading{budgetMib: 3, headroomMib: 3, budgetKnown: true, headroomKnown: true}) {
		t.Fatalf("private namespace reading = %+v", got)
	}
}

func TestMemoryReadingFiniteParentWithUnreadableOrUnlimitedChild(t *testing.T) {
	for _, leafLimit := range []string{"max", "invalid"} {
		t.Run(leafLimit, func(t *testing.T) {
			root := t.TempDir()
			writeCgroupFixture(t, root, map[string]string{"memory.max": "4194304", "memory.current": "3145728"})
			writeCgroupFixture(t, filepath.Join(root, "child"), map[string]string{"memory.max": leafLimit, "memory.current": "0"})
			oldRoot := budgetFsRoot
			budgetFsRoot = root
			t.Cleanup(func() { budgetFsRoot = oldRoot })
			withSelfCgroup(t, "0::/child\n")
			got := newBudget(1).readMemory()
			if got != (memoryReading{budgetMib: 3, headroomMib: 1, budgetKnown: true, headroomKnown: true}) {
				t.Fatalf("finite parent reading = %+v", got)
			}
		})
	}
}
