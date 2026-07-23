package server

import (
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// This file holds the node-side CHEAP rejection predicate (ADR embervm/014
// decision 3: "placement is reject/retry, not ledger-perfect"). Before a boot
// verb (Prime / StartServing / StartStateful / StartGroupMember) claims any
// resources, it asks admitOrReject whether the node is under real memory or tap
// pressure and, if so, returns RESOURCE_EXHAUSTED with a machine-readable reason
// BEFORE doing any expensive work.
//
// The whole point is that rejection is O(1): every predicate reads an
// already-maintained counter (the cgroup memory headroom the budget reader keeps,
// the IP allocator's freelist size). No disk read, no netlink call, no restore is
// on the reject path. That is what makes a wrong control-plane placement guess
// cost one extra RPC instead of a multi-second wasted boot, and it is the same
// predicate ADR 015's isolated-lane 503 admission check reuses (so it is written
// as a standalone helper, not inlined into each handler).

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

// admitOrReject is the single cheap-rejection gate every boot verb calls right
// after the existing max-live-VMs backstop and before it claims any resource. It
// returns a RESOURCE_EXHAUSTED error (same shape as the max-live-VMs rejection)
// when the node is under pressure, or nil to admit. needMib is the workload's
// memory footprint in MiB (0 when unknown, e.g. Prime for a base whose sizing the
// daemon has no registry entry for); class selects whether tap capacity is
// checked.
//
// Both predicates FAIL OPEN on an unknown reading, matching the budget reader's
// own convention that 0 means "unknown, never a guess":
//
//   - memHeadroom() == 0 means the cgroup is unlimited or unreadable (the daemon
//     cannot observe its own ceiling), so memory pressure is NOT asserted. This
//     is also why every existing test that stubs memHeadroom to 0 keeps admitting.
//   - a nil servingNet (serving disabled) has no tap notion, so tap pressure is
//     not asserted either.
//
// Asserting pressure only on a POSITIVE observation (a real headroom number below
// the threshold, a real freelist that is empty) is the safe direction: a brick
// that cannot see its budget behaves exactly as it did before this predicate
// existed rather than wedging itself out of all placement.
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
	if s.memPressured(needMib) {
		return reasonPressureMem, true
	}
	if class == classTapBearing && s.tapsExhausted() {
		return reasonPressureTaps, true
	}
	return "", false
}

// memPressured reports whether free schedulable memory is below the workload's
// need plus the reject floor. Reads the budget reader's cached cgroup headroom
// (memory.max - memory.current, a cheap best-effort file read, no restore/disk
// work). A zero headroom is UNKNOWN (unlimited or unreadable cgroup), not "zero
// free": it fails open (no pressure), matching the budget reader's convention and
// keeping a brick that cannot observe its cgroup exactly as permissive as before.
func (s *Server) memPressured(needMib uint64) bool {
	if s.memHeadroom == nil {
		return false
	}
	headroom := s.memHeadroom()
	if headroom == 0 {
		return false // unknown cgroup ceiling: fail open, never a guess.
	}
	return headroom < needMib+s.memRejectFloorMib()
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
// which makes the predicate gate on the floor alone (the conservative honest
// default the plan sanctions).
func (s *Server) primeNeedMib(workload string) uint64 {
	if s.registry == nil || workload == "" {
		return 0
	}
	if e, ok := s.registry.get(workload); ok {
		return uint64(e.MemMib)
	}
	return 0
}
