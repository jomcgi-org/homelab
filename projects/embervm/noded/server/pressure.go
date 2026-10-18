package server

import (
	"sync"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

const minSlotWorkloadMib = 512

// Admission uses both declared guest commitments and measured cgroup pressure.
// Lazy page allocation must not let two guests reserve the same finite budget.
// Boot RPCs acquire a pending reservation before doing work; pressure probes only
// inspect the same predicate without acquiring resources.

// rejectReason is the machine-readable token stamped into the RESOURCE_EXHAUSTED
// status message so the control plane (and ADR 015's admission layer) can tell
// WHY a boot was rejected without string-matching prose. The values are a stable
// contract; keep them in sync with the CP-side classifier.
type rejectReason string

const (
	// reasonPressureMem: the node's free schedulable memory is below the
	// workload's need plus the configured reject floor.
	reasonPressureMem rejectReason = "pressure:mem"
	// reasonPressureTaps: the serving IP/tap allocator has no free address for a
	// tap-bearing class (serving / stateful / group member).
	reasonPressureTaps rejectReason = "pressure:taps"
)

// pressureClass names which resources a boot verb actually consumes, so the
// predicate only checks tap capacity for the tap-bearing classes. The task class
// (Prime) is vsock-only and never allocates a tap, so it is memory-only.
type pressureClass int

const (
	// classMemOnly is a vsock-only boot (Prime): memory pressure only, no tap.
	classMemOnly pressureClass = iota
	// classTapBearing is a boot that allocates a serving tap (StartServing /
	// StartStateful / StartGroupMember): both memory and tap pressure apply.
	classTapBearing
)

// admitOrReject is the read-only pressure probe. Boot paths must instead use
// reserveAdmission to make the check and pending claim atomic.
func (s *Server) admitOrReject(needMib uint64, class pressureClass) error {
	reason, exhausted := s.underPressure(needMib, class)
	if !exhausted {
		return nil
	}
	// The machine-readable reason token leads the message (the CP classifies on it);
	// the human context differs per reason so a taps rejection does not carry a
	// spurious memory floor.
	switch reason {
	case reasonPressureMem:
		return status.Errorf(codes.ResourceExhausted, "noded: %s (need %d MiB, floor %d MiB)", reason, needMib, s.memRejectFloorMib())
	default: // reasonPressureTaps
		return status.Errorf(codes.ResourceExhausted, "noded: %s (serving tap allocator exhausted)", reason)
	}
}

// underPressure is admitOrReject's pure core: it returns the first pressure
// reason that trips, or ("", false) to admit. Split out so the ADR 015 503
// admission check can reuse the verdict without producing a gRPC error, and so it
// is unit-testable in isolation from the wire path.
func (s *Server) underPressure(needMib uint64, class pressureClass) (rejectReason, bool) {
	if s.memExhausted(needMib) {
		return reasonPressureMem, true
	}
	if class == classTapBearing && s.tapsExhausted() {
		return reasonPressureTaps, true
	}
	return "", false
}

// reserveAdmission holds a pending claim until the boot RPC returns, on both
// success and failure. A successful driver claim enters the live map before the
// pending claim is released. This deliberately double-charges that VM during
// readiness, a conservative overlap that avoids a check-to-publication gap.
// If readiness cleanup cannot reap a VM, its driver claim remains charged.
func (s *Server) reserveAdmission(needMib uint64, class pressureClass) (func(), error) {
	s.admissionMu.Lock()
	defer s.admissionMu.Unlock()
	ceiling := s.SlotCeiling()
	live, _ := s.liveMemoryClaims()
	if ceiling > 0 && (live >= uint64(ceiling) || s.pendingVMs >= uint64(ceiling)-live) {
		return nil, status.Errorf(codes.ResourceExhausted, "noded: node live-VM cap %d reached", ceiling)
	}
	if needMib > ^uint64(0)-s.pendingMib || s.pendingVMs == ^uint64(0) || s.memExhaustedLocked(needMib) {
		return nil, status.Errorf(codes.ResourceExhausted, "noded: %s (need %d MiB, floor %d MiB)", reasonPressureMem, needMib, s.memRejectFloorMib())
	}
	if class == classTapBearing && s.tapsExhausted() {
		return nil, status.Errorf(codes.ResourceExhausted, "noded: %s (serving tap allocator exhausted)", reasonPressureTaps)
	}
	s.pendingMib += needMib
	s.pendingVMs++
	var once sync.Once
	return func() {
		once.Do(func() {
			s.admissionMu.Lock()
			defer s.admissionMu.Unlock()
			s.pendingMib -= needMib
			s.pendingVMs--
		})
	}, nil
}

func (s *Server) memExhausted(needMib uint64) bool {
	s.admissionMu.Lock()
	defer s.admissionMu.Unlock()
	return s.memExhaustedLocked(needMib)
}

// Both modes enforce declared commitments and actual headroom. Observed also
// keeps its configured reject floor; reserved uses the daemon reserve in the
// capacity calculation without adding that observed-mode floor.
func (s *Server) memExhaustedLocked(needMib uint64) bool {
	live, claimed := s.liveMemoryClaims()
	overhead := s.vmOverheadMib()
	if live == ^uint64(0) || s.pendingVMs > ^uint64(0)-live-1 {
		return true
	}
	if s.memBudget != nil {
		capacity, known := s.memBudget()
		if known && (capacity == 0 || needMib == 0 || !memoryFits(capacity, overhead, live+s.pendingVMs+1, claimed, s.pendingMib, needMib)) {
			return true
		}
	}
	if s.memHeadroom != nil {
		headroom, known := s.memHeadroom()
		floor := uint64(0)
		if s.cfg.AdmissionModel != "reserved" {
			floor = s.memRejectFloorMib()
		}
		// Live memory already contributes to memory.current. Pending claims
		// protect the interval before a new guest faults its pages in.
		if known && (headroom == 0 || !memoryFits(headroom, overhead, s.pendingVMs+1, s.pendingMib, needMib, floor)) {
			return true
		}
	}
	return false
}

// memoryFits subtracts charges instead of adding them, so overflow cannot turn
// an oversized admission into an apparently small request.
func memoryFits(capacity, overhead, count uint64, charges ...uint64) bool {
	for _, charge := range charges {
		if charge > capacity {
			return false
		}
		capacity -= charge
	}
	return overhead == 0 || count <= capacity/overhead
}

func (s *Server) vmOverheadMib() uint64 {
	if s.cfg.VMOverheadMib > 0 {
		return uint64(s.cfg.VMOverheadMib)
	}
	return 0
}

func (s *Server) liveMemoryClaims() (uint64, uint64) {
	// Production snapshots both numbers under the driver's lock, so a release
	// cannot pair a smaller VM count with an older declared-memory total.
	if d, ok := s.driver.(interface{ LiveMemoryClaims() (uint64, uint64) }); ok {
		return d.LiveMemoryClaims()
	}
	return uint64(s.driver.LiveCount()), s.claimedMib()
}

func (s *Server) claimedMib() uint64 {
	if d, ok := s.driver.(interface{ ClaimedMib() uint64 }); ok {
		return d.ClaimedMib()
	}
	return 0
}

// memPressured is the observed headroom-only probe, retained for diagnostics.
func (s *Server) memPressured(needMib uint64) bool {
	if s.memHeadroom == nil {
		return false
	}
	headroom, known := s.memHeadroom()
	return known && (headroom == 0 || !memoryFits(headroom, s.vmOverheadMib(), 1, needMib, s.memRejectFloorMib()))
}

// tapsExhausted reports whether the serving IP/tap allocator has no free address.
// A nil servingNet (serving disabled) reports not-exhausted (there is no tap
// notion to be under pressure). The freelist size is an O(1) read of the
// allocator's already-maintained counters, never a netlink enumeration.
func (s *Server) tapsExhausted() bool {
	if s.servingNet == nil {
		return false
	}
	return s.servingNet.AvailableTaps() == 0
}

// memRejectFloorMib is the memory reject floor in MiB: free memory must exceed
// the workload's need PLUS this floor to admit, so the node keeps a cushion for
// the daemon's own allocations and never admits a boot that would drive it to the
// edge. Configured via EMBERVM_NODED_MEM_REJECT_FLOOR_MIB (config default
// minSlotWorkloadMib, one smallest-workload footprint); a zero/unset config
// falls back to minSlotWorkloadMib so the floor is never accidentally disabled.
func (s *Server) memRejectFloorMib() uint64 {
	if s.cfg.MemRejectFloorMib > 0 {
		return uint64(s.cfg.MemRejectFloorMib)
	}
	return minSlotWorkloadMib
}

// primeNeedMib resolves the memory footprint (MiB) a Prime of the given base will
// claim, for the pressure predicate. Prime carries no ResourceSpec (it restores a
// base snapshot whose footprint is fixed at build time), so the need comes from
// the control-plane-pushed workload registry entry for the base's workload. An
// absent entry (registry not yet synced, or a workload with no sizing) yields 0,
// which refuses admission when a finite budget is known: an unknown guest size
// cannot safely reserve capacity. Unlimited/unreadable environments retain the
// measured-headroom fallback.
func (s *Server) primeNeedMib(workload string) uint64 {
	if s.registry == nil || workload == "" {
		return 0
	}
	if e, ok := s.registry.get(workload); ok {
		return uint64(e.MemMib)
	}
	return 0
}

// snapshotNeedMib protects restores of older, larger snapshots after a workload
// resize. Production uses the driver's own bundle paths and sizing rules.
func snapshotNeedMib(driver any, declared uint64, kind, ref, member string) uint64 {
	if d, ok := driver.(interface {
		SnapshotMemoryMib(string, string, string) uint64
	}); ok {
		return max(declared, d.SnapshotMemoryMib(kind, ref, member))
	}
	return declared
}

// coldBootNeedMib resolves driver defaults while the request is still outside
// admission. An omitted declaration is not a zero-byte launch reservation.
func coldBootNeedMib(driver any, declared uint64) uint64 {
	if d, ok := driver.(interface{ ColdBootMemoryMib(uint64) uint64 }); ok {
		return d.ColdBootMemoryMib(declared)
	}
	return declared
}
