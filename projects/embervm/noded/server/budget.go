package server

import (
	"context"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"
)

// budgetFsRoot is the cgroup v2 mount point the reader inspects. A package
// var (not a Config field) so tests can point it at a fixture directory
// without threading a new knob through every other Config consumer; mirrors
// how parseMemHeadroomMib already isolates the parsing logic for injection.
var budgetFsRoot = "/sys/fs/cgroup"

// budget is the daemon's self-reported resource ceiling and real headroom,
// read from its own cgroup v2 controller files rather than static config
// (ADR embervm/005 item 4, ADR embervm/013 section 7 as amended: a brick's
// slot count is derived from its own cgroup budget, never configured). It
// replaces the advisory-only readMemHeadroomMib with a budget PLUS headroom
// pair, and adds the CPU side (CpuHeadroomMillicores was hard-coded 0).
//
// "max" (unlimited) reports a zero budget, never a guess: a brick's cgroup
// is the honest ceiling this whole mechanism exists to observe, so an
// unbounded cgroup is unknown, not infinite.
type budget struct {
	mu sync.Mutex

	// reserveMib is subtracted from memory.max before it is reported as
	// MemBudgetMib, covering the daemon's own RSS so the reported budget is
	// guest-schedulable memory, not the raw cgroup ceiling.
	reserveMib uint64

	// last CPU usage sample, for computing the usage-rate delta between
	// refreshes. Zero time means no sample yet (first refresh reports 0
	// headroom rather than a bogus rate from a zero-width window).
	lastSampleAt   time.Time
	lastUsageUsec  uint64
	cpuBudgetMilli uint64
	cpuHeadroomMc  uint64
}

// newBudget constructs a reader with the given daemon-RSS reserve.
func newBudget(reserveMib uint64) *budget {
	return &budget{reserveMib: reserveMib}
}

// MemBudgetMib returns memory.max minus the reserve, in MiB. Unlimited or
// unreadable cgroups report 0 (unknown).
func (b *budget) MemBudgetMib() uint64 {
	maxRaw, err := os.ReadFile(budgetFsRoot + "/memory.max")
	if err != nil {
		return 0
	}
	return parseMemBudgetMib(string(maxRaw), b.reserveMib)
}

// MemHeadroomMib returns free schedulable guest memory in MiB. Identical
// semantics to the retired readMemHeadroomMib, folded in here so the daemon
// has one cgroup reader instead of two.
func (b *budget) MemHeadroomMib() uint64 {
	maxRaw, err := os.ReadFile(budgetFsRoot + "/memory.max")
	if err != nil {
		return 0
	}
	curRaw, err := os.ReadFile(budgetFsRoot + "/memory.current")
	if err != nil {
		return 0
	}
	return parseMemHeadroomMib(string(maxRaw), string(curRaw))
}

// CpuBudgetMillicores returns the cgroup v2 cpu.max quota/period pair
// expressed as millicores. Unlimited ("max" quota) or unreadable cgroups
// report 0 (unknown).
func (b *budget) CpuBudgetMillicores() uint64 {
	raw, err := os.ReadFile(budgetFsRoot + "/cpu.max")
	if err != nil {
		return 0
	}
	return parseCpuBudgetMillicores(string(raw))
}

// CpuHeadroomMillicores returns the last computed CPU headroom: budget minus
// the observed usage rate sampled across the two most recent Refresh calls.
// 0 until at least two samples exist, or when the budget is unknown.
func (b *budget) CpuHeadroomMillicores() uint64 {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.cpuHeadroomMc
}

// Refresh samples cpu.max and cpu.stat, updating the CPU budget and the
// usage-rate-derived headroom. Call it on a ticker (see StartBudgetLoop) so
// an in-place resize of the pod (ADR 012) and normal usage swings are both
// observed without a daemon restart. Mem fields are cheap best-effort reads
// and are NOT cached; only the CPU side needs a two-sample delta.
func (b *budget) Refresh() {
	cpuMaxRaw, err := os.ReadFile(budgetFsRoot + "/cpu.max")
	if err != nil {
		return
	}
	budgetMilli := parseCpuBudgetMillicores(string(cpuMaxRaw))

	statRaw, err := os.ReadFile(budgetFsRoot + "/cpu.stat")
	if err != nil {
		return
	}
	usageUsec, ok := parseCpuUsageUsec(string(statRaw))
	if !ok {
		return
	}

	now := time.Now()
	b.mu.Lock()
	defer b.mu.Unlock()
	b.cpuBudgetMilli = budgetMilli
	if b.lastSampleAt.IsZero() || budgetMilli == 0 {
		b.lastSampleAt = now
		b.lastUsageUsec = usageUsec
		b.cpuHeadroomMc = 0
		return
	}
	elapsed := now.Sub(b.lastSampleAt)
	if elapsed <= 0 || usageUsec < b.lastUsageUsec {
		// Clock skew or a counter reset (cgroup recreated across a resize);
		// reseed rather than report a nonsensical negative rate.
		b.lastSampleAt = now
		b.lastUsageUsec = usageUsec
		return
	}
	usedUsec := usageUsec - b.lastUsageUsec
	// Usage rate in millicores: (used-usec / elapsed-usec) * 1000.
	usageMilli := uint64(float64(usedUsec) / float64(elapsed.Microseconds()) * 1000)
	if usageMilli > budgetMilli {
		b.cpuHeadroomMc = 0
	} else {
		b.cpuHeadroomMc = budgetMilli - usageMilli
	}
	b.lastSampleAt = now
	b.lastUsageUsec = usageUsec
}

// StartBudgetLoop refreshes the CPU-headroom sample on the same cadence as
// the WatchNode liveness stream, so a status probe always has a fresh
// usage-rate delta rather than a stale or empty one. Returns immediately;
// the goroutine stops when ctx is cancelled (daemon shutdown).
func (s *Server) StartBudgetLoop(ctx context.Context) {
	// One immediate sample so the first NodeStatus after boot has a seeded
	// baseline instead of waiting a full tick.
	s.budget.Refresh()
	go func() {
		ticker := time.NewTicker(livenessInterval)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				s.budget.Refresh()
			}
		}
	}()
}

// parseMemHeadroomMib computes (max-current) in MiB from cgroup v2
// memory.max and memory.current contents. "max" (unlimited) or a parse error
// yields 0. Folded in from the retired free-function readMemHeadroomMib.
func parseMemHeadroomMib(maxRaw, curRaw string) uint64 {
	maxStr := strings.TrimSpace(maxRaw)
	if maxStr == "max" || maxStr == "" {
		return 0
	}
	maxB, err := strconv.ParseInt(maxStr, 10, 64)
	if err != nil {
		return 0
	}
	curB, err := strconv.ParseInt(strings.TrimSpace(curRaw), 10, 64)
	if err != nil {
		return 0
	}
	if curB >= maxB {
		return 0
	}
	return uint64(maxB-curB) / (1 << 20)
}

// parseMemBudgetMib computes (max - reserve) in MiB from cgroup v2
// memory.max contents. "max" (unlimited) or a parse error yields 0
// (unknown, never a guess). A reserve that would drive the result negative
// also yields 0.
func parseMemBudgetMib(maxRaw string, reserveMib uint64) uint64 {
	maxStr := strings.TrimSpace(maxRaw)
	if maxStr == "max" || maxStr == "" {
		return 0
	}
	maxB, err := strconv.ParseInt(maxStr, 10, 64)
	if err != nil || maxB < 0 {
		return 0
	}
	maxMib := uint64(maxB) / (1 << 20)
	if maxMib <= reserveMib {
		return 0
	}
	return maxMib - reserveMib
}

// parseCpuBudgetMillicores computes millicores from cgroup v2 cpu.max
// contents ("$quota $period" in microseconds, or "max $period" for
// unlimited). Unlimited or a parse error yields 0 (unknown).
func parseCpuBudgetMillicores(raw string) uint64 {
	fields := strings.Fields(strings.TrimSpace(raw))
	if len(fields) != 2 {
		return 0
	}
	if fields[0] == "max" {
		return 0
	}
	quota, err := strconv.ParseInt(fields[0], 10, 64)
	if err != nil || quota < 0 {
		return 0
	}
	period, err := strconv.ParseInt(fields[1], 10, 64)
	if err != nil || period <= 0 {
		return 0
	}
	// millicores = (quota/period) * 1000, i.e. quota*1000/period.
	return uint64(quota) * 1000 / uint64(period)
}

// parseCpuUsageUsec extracts the usage_usec field from cgroup v2 cpu.stat
// contents (newline-separated "key value" pairs). ok is false when the key
// is absent or unparseable.
func parseCpuUsageUsec(raw string) (usec uint64, ok bool) {
	for _, line := range strings.Split(raw, "\n") {
		fields := strings.Fields(line)
		if len(fields) != 2 || fields[0] != "usage_usec" {
			continue
		}
		v, err := strconv.ParseUint(fields[1], 10, 64)
		if err != nil {
			return 0, false
		}
		return v, true
	}
	return 0, false
}
