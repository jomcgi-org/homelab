package server

import (
	"context"
	"os"
	"path/filepath"
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

// selfCgroupPath is the path the reader parses to discover the daemon's OWN
// cgroup v2 directory (the "0::<path>" line of /proc/self/cgroup). A package
// var so tests can point it at a fixture file, mirroring budgetFsRoot.
//
// Why this exists: the noded container runs privileged (chart _noded-pod.tpl),
// so on containerd it inherits the HOST cgroup namespace. There, plain
// budgetFsRoot ("/sys/fs/cgroup") is the host ROOT cgroup, which has no
// memory.max at all, so every capacity read returned 0 ("unknown"). The pod's
// real leaf cgroup (e.g. .../cri-containerd-<id>.scope) does carry memory.max,
// and /proc/self/cgroup names it. Under a PRIVATE cgroup namespace the line is
// "0::/" and budgetFsRoot already IS the pod's cgroup, so resolution falls
// back to budgetFsRoot and behaves exactly as before.
var selfCgroupPath = "/proc/self/cgroup"

// cgroupDir resolves the directory whose cgroup v2 controller files (memory.max,
// cpu.max, ...) describe THIS daemon's cgroup. It parses selfCgroupPath for the
// "0::<path>" entry and joins <path> under budgetFsRoot. It degrades gracefully:
//   - a "0::/" self-path (private cgroupns) or an unreadable/absent
//     /proc/self/cgroup falls back to budgetFsRoot;
//   - a joined dir that lacks memory.max (e.g. the host-root join does not
//     exist, or a fixture wrote files at budgetFsRoot itself) also falls back
//     to budgetFsRoot.
//
// The result is correct under BOTH host and private cgroup namespaces without a
// namespace probe: the join is only preferred when it actually carries the
// controller files.
func cgroupDir() string {
	rel := parseSelfCgroupV2Path(readSelfCgroup())
	if rel == "" || rel == "/" {
		return budgetFsRoot
	}
	joined := filepath.Join(budgetFsRoot, rel)
	if _, err := os.Stat(filepath.Join(joined, "memory.max")); err != nil {
		// The self-path does not resolve to a cgroup dir with controller files
		// under this root; fall back rather than read nothing.
		return budgetFsRoot
	}
	return joined
}

// readSelfCgroup returns the contents of selfCgroupPath, or "" on error (which
// cgroupDir treats as the budgetFsRoot fallback).
func readSelfCgroup() string {
	raw, err := os.ReadFile(selfCgroupPath)
	if err != nil {
		return ""
	}
	return string(raw)
}

// parseSelfCgroupV2Path extracts the <path> from the cgroup v2 "0::<path>" line
// of /proc/self/cgroup contents. Returns "" when no such line exists (a pure
// cgroup v1 host, which this daemon does not target). The leading "0::" marks
// the unified (v2) hierarchy; its path is relative to the cgroup v2 mount.
func parseSelfCgroupV2Path(raw string) string {
	for _, line := range strings.Split(raw, "\n") {
		if rest, ok := strings.CutPrefix(line, "0::"); ok {
			return strings.TrimSpace(rest)
		}
	}
	return ""
}

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

	// now returns the current time. A field (defaulting to time.Now) so tests
	// can pin the sample instant and assert an exact usage-rate delta instead
	// of racing sub-microsecond wall-clock jitter between two time.Now calls.
	now func() time.Time

	// memBudgetOverride, when set, replaces MemBudgetMib as the source
	// SlotCeiling divides. Test-only seam so the ceiling arithmetic can be
	// asserted against fixed budgets without a fixture cgroup filesystem; nil
	// in production, where SlotCeiling reads the real budget.
	memBudgetOverride func() uint64
}

// newBudget constructs a reader with the given daemon-RSS reserve.
func newBudget(reserveMib uint64) *budget {
	return &budget{reserveMib: reserveMib, now: time.Now}
}

// MemBudgetMib returns memory.max minus the reserve, in MiB. Unlimited or
// unreadable cgroups report 0 (unknown).
func (b *budget) MemBudgetMib() uint64 {
	dir := cgroupDir()
	maxRaw, err := os.ReadFile(filepath.Join(dir, "memory.max"))
	if err != nil {
		return 0
	}
	return parseMemBudgetMib(string(maxRaw), b.reserveMib)
}

// MemHeadroomMib returns free schedulable guest memory in MiB. Identical
// semantics to the retired readMemHeadroomMib, folded in here so the daemon
// has one cgroup reader instead of two. An unreadable memory.stat falls back
// to max minus current, which is conservative because it does not subtract
// any unverified reclaimable cache.
func (b *budget) MemHeadroomMib() uint64 {
	dir := cgroupDir()
	maxRaw, err := os.ReadFile(filepath.Join(dir, "memory.max"))
	if err != nil {
		return 0
	}
	curRaw, err := os.ReadFile(filepath.Join(dir, "memory.current"))
	if err != nil {
		return 0
	}
	statRaw, err := os.ReadFile(filepath.Join(dir, "memory.stat"))
	if err != nil {
		statRaw = nil
	}
	return parseMemHeadroomMib(string(maxRaw), string(curRaw), string(statRaw))
}

// CpuBudgetMillicores returns the cgroup v2 cpu.max quota/period pair
// expressed as millicores. Unlimited ("max" quota) or unreadable cgroups
// report 0 (unknown).
func (b *budget) CpuBudgetMillicores() uint64 {
	raw, err := os.ReadFile(filepath.Join(cgroupDir(), "cpu.max"))
	if err != nil {
		return 0
	}
	return parseCpuBudgetMillicores(string(raw))
}

// minSlotWorkloadMib is the smallest guest footprint a live-VM slot is assumed
// to hold, used only to turn the memory budget into a slot ceiling. It is a
// floor for the divisor, not a real per-workload size (workloads are sized from
// the registry): dividing the budget by it yields the MOST slots a brick could
// ever host, which is the honest ceiling maxLiveVMs must not exceed. Set to a
// conservative small-guest size so the ceiling errs high (the configured
// MaxLiveVMs backstop and real per-VM Claim accounting are the tighter caps);
// 512 MiB keeps a 2gi/512-reserve brick at 3 slots rather than the configured
// default of 8/16.
const minSlotWorkloadMib = 512

// SlotCeiling returns the brick's cgroup-derived live-VM slot ceiling per ADR
// embervm/013 section 7 ("maxLiveVMs is the brick's cgroup-derived slot
// ceiling, never a control-plane knob"): floor(MemBudgetMib /
// minSlotWorkloadMib), clamped so it never EXCEEDS the configured backstop
// (configured stays an upper bound). When the budget is unknown (0, an
// unlimited or unreadable cgroup) the ceiling is unknown too, so the configured
// backstop is used unchanged: an environment that cannot observe its cgroup
// keeps exactly the pre-budget behavior rather than collapsing to zero slots.
func (b *budget) SlotCeiling(configured uint64) uint64 {
	budgetMib := b.MemBudgetMib()
	if b.memBudgetOverride != nil {
		budgetMib = b.memBudgetOverride()
	}
	if budgetMib == 0 {
		return configured
	}
	derived := budgetMib / minSlotWorkloadMib
	if derived == 0 {
		// A budget smaller than one slot still hosts at least one VM (the
		// backstop and Claim accounting gate the real limit); never advertise
		// a zero ceiling that would wedge a small brick out of all placement.
		derived = 1
	}
	if configured > 0 && derived > configured {
		return configured
	}
	return derived
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
	dir := cgroupDir()
	cpuMaxRaw, err := os.ReadFile(filepath.Join(dir, "cpu.max"))
	if err != nil {
		return
	}
	budgetMilli := parseCpuBudgetMillicores(string(cpuMaxRaw))

	statRaw, err := os.ReadFile(filepath.Join(dir, "cpu.stat"))
	if err != nil {
		return
	}
	usageUsec, ok := parseCpuUsageUsec(string(statRaw))
	if !ok {
		return
	}

	now := b.now()
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

// parseMemHeadroomMib computes headroom in MiB from cgroup v2 memory.max,
// memory.current, and memory.stat contents. "max" (unlimited) or a parse
// error yields 0. Folded in from the retired free-function readMemHeadroomMib.
func parseMemHeadroomMib(maxRaw, curRaw, statRaw string) uint64 {
	maxStr := strings.TrimSpace(maxRaw)
	if maxStr == "max" || maxStr == "" {
		return 0
	}
	maxB, err := strconv.ParseInt(maxStr, 10, 64)
	if err != nil || maxB < 0 {
		return 0
	}
	curB, err := strconv.ParseInt(strings.TrimSpace(curRaw), 10, 64)
	if err != nil || curB < 0 {
		return 0
	}
	workingSetB := curB
	for _, line := range strings.Split(statRaw, "\n") {
		fields := strings.Fields(line)
		if len(fields) != 2 || fields[0] != "inactive_file" {
			continue
		}
		inactiveFileB, statErr := strconv.ParseUint(fields[1], 10, 64)
		if statErr != nil {
			break
		}
		// kubelet uses inactive_file for working_set because this cache is
		// reclaimed first under pressure. Do not subtract all file cache,
		// or anon plus slab, since those are not equivalent eviction signals.
		// The cgroup files are read separately, so inactive_file can race
		// memory.current. Keep the conservative current-based working set when
		// the sample is racy instead of treating it as fully reclaimable.
		if inactiveFileB < uint64(curB) {
			workingSetB = curB - int64(inactiveFileB)
		}
		break
	}

	if uint64(maxB) <= uint64(workingSetB) {
		return 0
	}
	return (uint64(maxB) - uint64(workingSetB)) / (1 << 20)
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
