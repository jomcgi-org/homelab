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

	// Second sample one second later, having used 1000000usec (1 full core)
	// of CPU time in that window: usage rate is 1000 millicores, so headroom
	// against a 2000-millicore budget is 1000.
	b.lastSampleAt = time.Now().Add(-1 * time.Second)
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
