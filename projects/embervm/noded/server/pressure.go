package server

import (
	"context"
	"math"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/jomcgi/homelab/projects/embervm/noded/substrate"
	nodev1 "github.com/jomcgi/homelab/projects/embervm/proto/embervm/node/v1"
)

const minSlotWorkloadMib = 512

// admissionModelReserved names the declared-claims memory admission model
// (EMBERVM_NODED_ADMISSION_MODEL=reserved). Every other value is the observed
// model.
const admissionModelReserved = "reserved"

// refusalLogInterval bounds how often one refusal token is logged. The first
// refusal in a window logs at once; later ones in the same window are only
// counted, and the next logged line reports how many were suppressed. A burst of
// placements against a full brick therefore costs one line per token per window,
// never one per RPC, while NodeStatus.admission_refusals still counts them all.
const refusalLogInterval = 10 * time.Second

// This file holds the node-side boot admission gate (ADR embervm/014 decision
// 3). Before a boot verb (Prime, Relight, StartServing, StartStateful,
// StartGroupMember, the node-local activator wakes that reuse them, and
// BuildBase) claims any resource, it calls admit, which either returns
// RESOURCE_EXHAUSTED with a machine-readable reason BEFORE any expensive work, or
// reserves the boot in the daemon's in-flight ledger.
//
// The check and the reservation happen under one mutex, so concurrent boots can
// never all pass against the same reading. Each admitted boot holds a ticket (its
// memMib and one VM slot) until the driver call that boots it returns: at that
// instant the VM is either in the driver's live map, where LiveCount and
// ClaimedMib count it, or it is gone, so the ticket is released exactly once and
// the boot is never uncounted in between. A BuildBase holds its ticket for the
// whole build, because build VMs run on their own drivers and never appear in
// LiveCount or ClaimedMib. Once its build guest has booted, the observed term
// stops subtracting that ticket (buildGuestBooted): the brick cgroup's headroom
// already charges every page the guest touches, so the guest would otherwise
// count twice for the minutes the build runs.
//
// Rejection stays O(1): every input is an already-maintained counter or a cheap
// cgroup file read (the budget reader's headroom and ceiling, the IP allocator's
// freelist size). No disk scan, netlink call or restore is on the reject path, so
// a wrong control-plane placement costs one extra RPC instead of a wasted boot.
// The control plane trusts the same predicate through NodeStatus
// mem_admittable_mib, so the daemon and the scheduler cannot disagree about what
// fits.

// rejectReason is the machine-readable token stamped into the RESOURCE_EXHAUSTED
// status message so the control plane (and ADR 015's admission layer) can tell
// WHY a boot was rejected without string-matching prose. The values are a stable
// contract (they also key NodeStatus.admission_refusals); keep them in sync with
// the CP-side classifier.
type rejectReason string

const (
	// reasonPressureMem: the boot's declared memory exceeds what the brick can
	// admit for one more VM (admissionFacts.admittableMib).
	reasonPressureMem rejectReason = "pressure:mem"
	// reasonPressureTaps: the serving IP/tap allocator has no free address for a
	// tap-bearing class (serving / stateful).
	reasonPressureTaps rejectReason = "pressure:taps"
	// reasonSlots: live VMs plus boots in flight already fill the configured
	// live-VM backstop. Its status message keeps the historical "live-VM cap"
	// text; the token only keys the refusal counter and log.
	reasonSlots rejectReason = "slots"
)

// pressureClass names which resources a boot verb actually consumes, so the
// predicate only checks tap capacity for the tap-bearing classes. The task class
// (Prime) is vsock-only and never allocates a tap, so it is memory-only.
type pressureClass int

const (
	// classMemOnly is a boot that allocates no serving tap (Prime, Relight,
	// StartGroupMember, BuildBase): memory and slots only.
	classMemOnly pressureClass = iota
	// classTapBearing is a boot that allocates a serving tap (StartServing /
	// StartStateful): memory, slots and tap pressure all apply.
	classTapBearing
)

// admissionLedger is the daemon's in-flight boot ledger and its refusal
// telemetry. The zero value is ready to use, so a Server built without New (some
// tests) still admits.
type admissionLedger struct {
	mu sync.Mutex
	// inflightMib and inflightVMs are boots admitted whose VM has not yet joined
	// the driver's live map (a build counts until it finishes). Every admission
	// decision subtracts them, and NodeStatus reports them.
	inflightMib uint64
	inflightVMs int
	// buildResidentMib is the part of inflightMib held by builds whose guest has
	// booted. The observed term leaves it out, because the cgroup headroom
	// already charges those guests; the reserved term and the slot count do not.
	buildResidentMib uint64
	// placedMib and placedVMs are the part of the in-flight boots that answer a
	// control-plane placement whose RPC is still open, each at the size the
	// control plane declared for it. NodeStatus reports them so the control plane
	// nets its unsettled claims against boots it placed, never against a
	// node-local wake (the activators) or a boot that outlived its RPC, which no
	// claim stands behind.
	placedMib uint64
	placedVMs int
	// refusals counts every refusal since daemon start by token
	// (NodeStatus.admission_refusals).
	refusals map[rejectReason]uint64
	// logWindows rate-limits the refusal log per token.
	logWindows map[rejectReason]*refusalLogWindow
	// now is the rate limiter's clock; nil means time.Now. Tests pin it.
	now func() time.Time
}

// refusalLogWindow is one token's log rate-limit state: when it last logged and
// how many refusals it has swallowed since.
type refusalLogWindow struct {
	loggedAt   time.Time
	suppressed uint64
}

// noteRefusalLocked counts one refusal and decides whether it is logged. It
// returns the suppressed count to report on a logged line. Caller holds l.mu.
func (l *admissionLedger) noteRefusalLocked(reason rejectReason) (logIt bool, suppressed uint64) {
	if l.refusals == nil {
		l.refusals = make(map[rejectReason]uint64)
	}
	l.refusals[reason]++
	if l.logWindows == nil {
		l.logWindows = make(map[rejectReason]*refusalLogWindow)
	}
	now := time.Now
	if l.now != nil {
		now = l.now
	}
	t := now()
	w := l.logWindows[reason]
	if w == nil {
		w = &refusalLogWindow{}
		l.logWindows[reason] = w
	}
	if !w.loggedAt.IsZero() && t.Sub(w.loggedAt) < refusalLogInterval {
		w.suppressed++
		return false, 0
	}
	suppressed = w.suppressed
	w.loggedAt = t
	w.suppressed = 0
	return true, suppressed
}

// admissionFacts is one consistent reading of every admission input, taken under
// the ledger mutex so the verdict and the reservation it guards cannot drift
// apart.
type admissionFacts struct {
	model string
	// budgetMib is the finite cgroup ceiling minus the daemon reserve;
	// budgetKnown distinguishes exhausted capacity from unknown capacity.
	budgetMib   uint64
	budgetKnown bool
	// headroomMib is the observed cgroup headroom; meaningful only when
	// headroomKnown.
	headroomMib   uint64
	headroomKnown bool
	// claimedMib is the declared memMib of every VM in the driver's live map.
	claimedMib  uint64
	live        int
	inflightMib uint64
	inflightVMs int
	// buildResidentMib is the in-flight memory of booted build guests, which
	// the observed headroom already reflects.
	buildResidentMib uint64
	overheadMib      uint64
	floorMib         uint64
	slotCeiling      int
}

// modelName is the admission model as logged and reported: reserved, or
// observed for every other configured value.
func (f admissionFacts) modelName() string {
	if f.model == admissionModelReserved {
		return admissionModelReserved
	}
	return "observed"
}

// admittableMib is THE memory predicate: the largest guest memMib this brick
// admits for ONE more VM right now, and whether that bound is finite. Every boot
// verb and NodeStatus.mem_admittable_mib call it, so the control plane admits
// need N exactly when the daemon would.
//
//	observed = headroom - (inflight - buildResident) - floor         (headroom known)
//	reserved = budget - claimed - inflight - O*(live+inflightVMs+1)  (budget known)
//
// The observed model uses observed alone. The reserved model (no overcommit)
// uses min(reserved, observed): declared claims bound it, and observed headroom
// stays a second guard against memory nothing declared (daemon RSS, page cache
// the kernel cannot reclaim). An unknown input contributes no bound, so a brick
// that can see neither number fails open exactly as it did before this predicate
// existed. The arithmetic is signed: a negative result admits nothing, not even a
// zero-need probe. A build guest that has booted is charged by the headroom
// itself, so the observed term subtracts only the tickets whose memory the
// cgroup cannot see yet.
func (f admissionFacts) admittableMib() (int64, bool) {
	if f.budgetKnown && f.budgetMib == 0 {
		return -1, true
	}
	var admittable int64
	finite := false
	if f.headroomKnown {
		unseen := f.inflightMib - min(f.buildResidentMib, f.inflightMib)
		admittable = subtractCharge(subtractCharge(clampInt64(f.headroomMib), unseen), f.floorMib)
		finite = true
	}
	if f.model == admissionModelReserved && (f.budgetKnown || f.budgetMib > 0) {
		reserved := subtractCharge(subtractCharge(clampInt64(f.budgetMib), f.claimedMib), f.inflightMib)
		if f.live < 0 || f.inflightVMs < 0 || f.live >= math.MaxInt-f.inflightVMs {
			reserved = math.MinInt64
		} else {
			vms := uint64(f.live) + uint64(f.inflightVMs) + 1
			if f.overheadMib > math.MaxInt64/vms {
				reserved = math.MinInt64
			} else {
				reserved = subtractCharge(reserved, f.overheadMib*vms)
			}
		}
		if !finite || reserved < admittable {
			admittable = reserved
		}
		finite = true
	}
	return admittable, finite
}

// Saturate debt instead of wrapping an overcommitted ledger into free capacity.
func subtractCharge(remaining int64, charge uint64) int64 {
	if charge > math.MaxInt64 || remaining < math.MinInt64+int64(charge) {
		return math.MinInt64
	}
	return remaining - int64(charge)
}

// memAdmits reports whether a boot declaring needMib passes the memory
// predicate.
func (f admissionFacts) memAdmits(needMib uint64) bool {
	admittable, finite := f.admittableMib()
	return !finite || (needMib <= math.MaxInt64 && int64(needMib) <= admittable)
}

// slotsFull reports whether the live-VM backstop refuses one more boot: live VMs
// plus boots in flight already reach the ceiling. A zero ceiling is unlimited.
func (f admissionFacts) slotsFull() bool {
	return f.slotCeiling > 0 && (f.live >= f.slotCeiling || f.inflightVMs >= f.slotCeiling-f.live)
}

func clampInt64(v uint64) int64 {
	if v > math.MaxInt64 {
		return math.MaxInt64
	}
	return int64(v)
}

// admissionFactsLocked reads every admission input. Caller holds
// s.admission.mu. The shared driver's live map is the authority for live VMs
// and their declared memory: task, session, serving, stateful and group VMs all
// claim through it, so LiveCount and ClaimedMib already sum every class.
func (s *Server) admissionFactsLocked() admissionFacts {
	headroom, known := s.observedHeadroomMib()
	budget, budgetKnown := s.memoryBudgetReading()
	live, claimed := s.liveMemoryClaims()
	f := admissionFacts{
		model:            s.cfg.AdmissionModel,
		budgetMib:        budget,
		budgetKnown:      budgetKnown,
		headroomMib:      headroom,
		headroomKnown:    known,
		claimedMib:       claimed,
		live:             live,
		inflightMib:      s.admission.inflightMib,
		inflightVMs:      s.admission.inflightVMs,
		buildResidentMib: s.admission.buildResidentMib,
		overheadMib:      s.vmOverheadMib(),
		floorMib:         s.memRejectFloorMib(),
		slotCeiling:      s.SlotCeiling(),
	}
	return f
}

// admissionTicket is one admitted boot's reservation in the in-flight ledger. A
// nil ticket is a no-op, so a refused admission's deferred release is harmless.
type admissionTicket struct {
	s       *Server
	needMib uint64
	// placedMib is the declared size the ticket counts toward the placed
	// in-flight figure while placed is true; 0 for a node-local boot.
	placedMib uint64
	// stopPlaced cancels the hook that unplaces the ticket when its RPC ends.
	stopPlaced func() bool
	once       sync.Once
	// resident, released and placed are guarded by s.admission.mu. resident
	// marks a build ticket whose guest has booted (buildGuestBooted); released
	// marks a ticket already returned, which can no longer become resident;
	// placed marks a ticket still counted in the placed in-flight figure.
	resident bool
	released bool
	placed   bool
}

// release returns the reservation exactly once. Boot paths call it the moment
// the driver call that boots the VM returns (on success the VM is now counted by
// LiveCount and ClaimedMib; on failure there is nothing to keep) and also defer
// it, so every early return between admission and the boot releases too.
func (t *admissionTicket) release() {
	if t == nil {
		return
	}
	t.once.Do(func() {
		l := &t.s.admission
		l.mu.Lock()
		l.inflightMib -= t.needMib
		l.inflightVMs--
		if t.resident {
			l.buildResidentMib -= t.needMib
		}
		t.unplaceLocked()
		t.released = true
		l.mu.Unlock()
		if t.stopPlaced != nil {
			t.stopPlaced()
		}
		// The released reservation changes mem_admittable_mib: tell the control
		// plane now rather than at the next liveness tick.
		t.s.signalChange()
	})
}

// unplaceLocked takes the ticket out of the placed in-flight figure, once.
// Caller holds s.admission.mu.
func (t *admissionTicket) unplaceLocked() {
	if !t.placed {
		return
	}
	l := &t.s.admission
	l.placedMib -= t.placedMib
	l.placedVMs--
	t.placed = false
}

// rpcEnded runs when the placing RPC's context ends while the ticket is still
// held: the control plane has had its answer (or its deadline) and has settled
// or released its claim, so a boot that carries on (a build past the client's
// deadline, say) keeps its reservation but no longer cancels a claim.
func (t *admissionTicket) rpcEnded() {
	l := &t.s.admission
	l.mu.Lock()
	was := t.placed
	t.unplaceLocked()
	l.mu.Unlock()
	if was {
		t.s.signalChange()
	}
}

// buildGuestBooted is called by a BuildBase once its build guest has booted. It
// moves the ticket's memory out of the observed term only: from here the brick
// cgroup's headroom charges every page the guest touches, so subtracting the
// whole ticket again would count the guest twice for the rest of the build. The
// reserved term and the slot count keep the ticket until release, because a
// build VM never appears in LiveCount or ClaimedMib. Until the guest boots the
// observed term still subtracts it, so a concurrent boot cannot pass against
// headroom the guest has not faulted in yet. Once only; a no-op on a nil or
// released ticket.
func (t *admissionTicket) buildGuestBooted() {
	if t == nil {
		return
	}
	l := &t.s.admission
	l.mu.Lock()
	if t.resident || t.released {
		l.mu.Unlock()
		return
	}
	t.resident = true
	l.buildResidentMib += t.needMib
	l.mu.Unlock()
	// The observed term just grew: tell the control plane now.
	t.s.signalChange()
}

type admissionTicketKey struct{}

// withAdmissionTicket carries a ticket down the shared serving, stateful and
// group boot paths, so the driver call deep in each mode can release it via
// admissionLanded without threading it through every helper signature.
func withAdmissionTicket(ctx context.Context, t *admissionTicket) context.Context {
	if t != nil {
		ctx = substrate.WithMemoryReservation(ctx, t.needMib)
	}
	return context.WithValue(ctx, admissionTicketKey{}, t)
}

// admissionLanded releases the ticket ctx carries, if any. Each boot path calls
// it right after its driver boot call returns, success or error.
func admissionLanded(ctx context.Context) {
	if t, ok := ctx.Value(admissionTicketKey{}).(*admissionTicket); ok {
		t.release()
	}
}

// admit is the single atomic admission gate every boot verb calls before it
// claims any resource. Under the ledger mutex it checks, in order, the live-VM
// backstop (live plus in-flight boots), the memory predicate (admittableMib) and,
// for a tap-bearing class, the serving tap freelist. On a pass it reserves
// needMib and one VM slot before unlocking and returns the ticket holding them.
// On a refusal it counts the token, logs (rate-limited per token) and returns a
// RESOURCE_EXHAUSTED error. verb names the caller in the log only.
//
// placedMib is the size the control plane declared (and claimed) for a boot it
// placed, or 0 for a node-local boot. A placed ticket also counts toward the
// placed in-flight figure until it is released or ctx (the placing RPC's
// context) ends, whichever comes first.
func (s *Server) admit(ctx context.Context, verb string, needMib, placedMib uint64, class pressureClass) (*admissionTicket, error) {
	l := &s.admission
	l.mu.Lock()
	f := s.admissionFactsLocked()
	var reason rejectReason
	switch {
	case f.slotsFull():
		reason = reasonSlots
	case needMib > math.MaxUint64-l.inflightMib || l.inflightVMs == math.MaxInt || placedMib > math.MaxUint64-l.placedMib || (placedMib > 0 && l.placedVMs == math.MaxInt) || !f.memAdmits(needMib):
		reason = reasonPressureMem
	case class == classTapBearing && s.tapsExhausted():
		reason = reasonPressureTaps
	}
	if reason != "" {
		logIt, suppressed := l.noteRefusalLocked(reason)
		l.mu.Unlock()
		return nil, s.refusal(verb, reason, needMib, f, logIt, suppressed)
	}
	l.inflightMib += needMib
	l.inflightVMs++
	t := &admissionTicket{s: s, needMib: needMib}
	if placedMib > 0 {
		t.placedMib, t.placed = placedMib, true
		l.placedMib += placedMib
		l.placedVMs++
	}
	l.mu.Unlock()
	if t.placed && ctx != nil {
		t.stopPlaced = context.AfterFunc(ctx, t.rpcEnded)
	}
	// The reservation changes mem_admittable_mib and inflight_vms: tell the
	// control plane within milliseconds so its next placement sees it.
	s.signalChange()
	return t, nil
}

// placedShare is the size a control-plane boot counts toward the placed
// in-flight figure: the size it declared, which is what the control plane
// claimed, else (no declared size) the whole reservation.
func placedShare(declaredMib, needMib uint64) uint64 {
	if declaredMib > 0 {
		return min(declaredMib, needMib)
	}
	return needMib
}

// refuseIfSlotsExhausted is a non-reserving early check of the live-VM backstop
// (live plus in-flight boots) for a verb that must report the cap before it can
// resolve its memory need (Relight checks it ahead of the snapshot lookup). admit
// re-checks the backstop atomically; this only preserves the error ordering. A
// refusal is counted and logged like any other.
func (s *Server) refuseIfSlotsExhausted(verb string) error {
	l := &s.admission
	l.mu.Lock()
	f := s.admissionFactsLocked()
	if !f.slotsFull() {
		l.mu.Unlock()
		return nil
	}
	logIt, suppressed := l.noteRefusalLocked(reasonSlots)
	l.mu.Unlock()
	return s.refusal(verb, reasonSlots, 0, f, logIt, suppressed)
}

// refusal logs one refusal (when the rate limiter allows) and builds its
// RESOURCE_EXHAUSTED status. The machine-readable token leads every message (the
// CP classifies on the status code and the token); the human tail is truthful for
// both models.
func (s *Server) refusal(verb string, reason rejectReason, needMib uint64, f admissionFacts, logIt bool, suppressed uint64) error {
	admittable, finite := f.admittableMib()
	if logIt {
		args := []any{
			"verb", verb,
			"reason", string(reason),
			"need_mib", needMib,
			"model", f.modelName(),
			"live", f.live,
			"inflight_vms", f.inflightVMs,
			"inflight_mib", f.inflightMib,
			"build_resident_mib", f.buildResidentMib,
			"slot_ceiling", f.slotCeiling,
			"claimed_mib", f.claimedMib,
			"budget_mib", f.budgetMib,
			"headroom_mib", f.headroomMib,
			"headroom_known", f.headroomKnown,
			"overhead_mib", f.overheadMib,
			"floor_mib", f.floorMib,
			"suppressed", suppressed,
		}
		if finite {
			args = append(args, "admittable_mib", admittable)
		}
		s.logger.Info("noded: admission refused", args...)
	}
	switch reason {
	case reasonSlots:
		return status.Errorf(codes.ResourceExhausted, "noded: node live-VM cap %d reached (live %d, in flight %d)", f.slotCeiling, f.live, f.inflightVMs)
	case reasonPressureMem:
		return status.Errorf(codes.ResourceExhausted, "noded: %s (need %d MiB, admittable %d MiB, model %s)", reason, needMib, admittable, f.modelName())
	default: // reasonPressureTaps
		return status.Errorf(codes.ResourceExhausted, "noded: %s (serving tap allocator exhausted)", reason)
	}
}

// admissionSnapshot is the NodeStatus projection of the ledger.
type admissionSnapshot struct {
	facts admissionFacts
	// admittableMib is meaningful only when reported (a finite bound).
	admittableMib int64
	reported      bool
	inflightMib   uint64
	inflightVMs   int
	placedMib     uint64
	placedVMs     int
	refusals      map[string]uint64
}

// admissionStatus evaluates the same predicate the boot verbs call, for one more
// VM and independent of any need, under the same mutex, so a reported
// mem_admittable_mib is exactly what admit would have used at that instant.
func (s *Server) admissionStatus() admissionSnapshot {
	l := &s.admission
	l.mu.Lock()
	defer l.mu.Unlock()
	f := s.admissionFactsLocked()
	snap := admissionSnapshot{
		facts:       f,
		inflightMib: l.inflightMib,
		inflightVMs: l.inflightVMs,
		placedMib:   l.placedMib,
		placedVMs:   l.placedVMs,
	}
	snap.admittableMib, snap.reported = f.admittableMib()
	if !snap.reported {
		snap.admittableMib = 0
	}
	if len(l.refusals) > 0 {
		snap.refusals = make(map[string]uint64, len(l.refusals))
		for reason, n := range l.refusals {
			snap.refusals[string(reason)] = n
		}
	}
	return snap
}

// underPressure is admit's verdict without the reservation, counter or log: the
// first reason that trips, or ("", false) to admit. It reads the same facts
// admit does (in-flight boots included) so the ADR 015 503 admission check can
// reuse the verdict without producing a gRPC error.
func (s *Server) underPressure(needMib uint64, class pressureClass) (rejectReason, bool) {
	if s.memExhausted(needMib) {
		return reasonPressureMem, true
	}
	if class == classTapBearing && s.tapsExhausted() {
		return reasonPressureTaps, true
	}
	return "", false
}

// memExhausted reports whether a boot declaring needMib fails the memory
// predicate of the configured model (admittableMib), boots in flight included.
func (s *Server) memExhausted(needMib uint64) bool {
	s.admission.mu.Lock()
	defer s.admission.mu.Unlock()
	return !s.admissionFactsLocked().memAdmits(needMib)
}

// memPressured is the observed half of the predicate on its own: whether known
// cgroup headroom, less the boots in flight it does not yet reflect, is below
// the workload's need plus the reject floor. An unknown headroom (unlimited or unreadable cgroup) fails
// open; a known but exhausted cgroup reads at least 1 MiB (parseMemHeadroomMib)
// and refuses.
func (s *Server) memPressured(needMib uint64) bool {
	s.admission.mu.Lock()
	defer s.admission.mu.Unlock()
	f := s.admissionFactsLocked()
	f.model = "observed"
	return !f.memAdmits(needMib)
}

// slotsExhausted reports whether live VMs plus boots in flight already fill the
// live-VM backstop. A zero ceiling is unlimited.
func (s *Server) slotsExhausted() bool {
	s.admission.mu.Lock()
	defer s.admission.mu.Unlock()
	return s.admissionFactsLocked().slotsFull()
}

// observedHeadroomMib reads the cgroup headroom and whether it is known. The
// budget reader reserves 0 for unknown (an unlimited, unreadable or unparseable
// cgroup) and reports a known but exhausted cgroup as at least 1 MiB, so the two
// never collide.
func (s *Server) observedHeadroomMib() (uint64, bool) {
	if s.memHeadroom == nil {
		return 0, false
	}
	return s.memHeadroom()
}

func (s *Server) memoryBudgetReading() (uint64, bool) {
	if s.memBudget == nil {
		return 0, false
	}
	return s.memBudget()
}
func (s *Server) memBudgetMib() uint64 { value, _ := s.memoryBudgetReading(); return value }

// vmOverheadMib is the per-VM host memory the reserved model charges on top of
// each guest's declared memory (EMBERVM_NODED_VM_OVERHEAD_MIB, validated
// non-negative at load). NodeStatus reports the same value.
func (s *Server) vmOverheadMib() uint64 {
	if s.cfg.VMOverheadMib > 0 {
		return uint64(s.cfg.VMOverheadMib)
	}
	return 0
}

func (s *Server) claimedMib() uint64 {
	if d, ok := s.driver.(interface{ ClaimedMib() uint64 }); ok {
		return d.ClaimedMib()
	}
	return 0
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

// memRejectFloorMib is the memory reject floor in MiB: observed free memory must
// exceed the workload's need PLUS this floor to admit, so the node keeps a
// cushion for the daemon's own allocations and never admits a boot that would
// drive it to the edge. Configured via EMBERVM_NODED_MEM_REJECT_FLOOR_MIB
// (config default minSlotWorkloadMib, one smallest-workload footprint); a
// zero/unset config falls back to minSlotWorkloadMib so the floor is never
// accidentally disabled.
func (s *Server) memRejectFloorMib() uint64 {
	if s.cfg.MemRejectFloorMib > 0 {
		return uint64(s.cfg.MemRejectFloorMib)
	}
	return minSlotWorkloadMib
}

// registrySizing returns the control-plane-pushed vCPUs and memMib for a
// workload. An absent entry (registry not yet synced, or a workload with no
// sizing) reports zeros.
func (s *Server) registrySizing(workload string) (vcpus, memMib uint32) {
	if s.registry == nil || workload == "" {
		return 0, 0
	}
	if e, ok := s.registry.get(workload); ok {
		return e.VCPUs, e.MemMib
	}
	return 0, 0
}

// primeNeedMib resolves a workload's declared memory (MiB) from the registry
// alone, 0 when unknown. Prime and Relight carry no ResourceSpec, so they use it
// before falling back to the size recorded with the bundle they restore
// (recordedBundleMemMib).
func (s *Server) primeNeedMib(workload string) uint64 {
	_, memMib := s.registrySizing(workload)
	return uint64(memMib)
}

// primeSizing resolves the vCPUs and memory (MiB) a Prime of base ref boots at,
// for both admission and a volume-forced cold boot. The registry entry for the
// base's workload is authoritative. Without one, memory falls back to the size
// the base was built at, so a reserved brick never charges 0 MiB for a real VM;
// vCPUs then stay 0 (the driver's own default).
func (s *Server) primeSizing(workload, ref string) (vcpus, memMib uint32) {
	vcpus, memMib = s.registrySizing(workload)
	if memMib == 0 {
		memMib = recordedBundleMemMib(filepath.Join(s.cfg.SnapshotRoot, "bases", ref))
	}
	if memMib == 0 {
		memMib = uint32(coldBootNeedMib(s.driver, 0))
	}
	return vcpus, memMib
}

// primeAdmitMib is the memory a Prime of base ref is charged: the larger of the
// size it would cold boot at (sizeMemMib, from primeSizing) and the size its
// base snapshot restores at. The driver's live map counts a restored VM at its
// snapshot's size, so charging a smaller registry size (a workload's memMib
// lowered after its base was built) would under-reserve the moment the VM
// lands (admission.tla, NoOvercommit).
func (s *Server) primeAdmitMib(sizeMemMib uint32, ref string) uint64 {
	return snapshotNeedMib(s.driver, uint64(max(sizeMemMib, recordedBundleMemMib(filepath.Join(s.cfg.SnapshotRoot, "bases", ref)))), "base", ref, "")
}

// relightNeedMib resolves a session relight's memory need: the larger of the
// workload's registry entry and the size recorded with the banked session
// bundle. A relight restores at the bundle's size, which the driver's live map
// then charges, so a memMib lowered while the session was banked must not shrink
// the reservation below the guest it restores.
func (s *Server) relightNeedMib(workload, ref string) uint64 {
	need := s.primeNeedMib(workload)
	if s.sessionDriver == nil {
		return need
	}
	return snapshotNeedMib(s.sessionDriver, max(need, uint64(recordedBundleMemMib(filepath.Join(s.sessionDriver.SessionsDir(), ref)))), "session", ref, "")
}

// bundleAdmitMib is the memory a tap or group RELIGHT is charged: the larger of
// the request's declared memMib and the size recorded with the banked bundle in
// dir. The driver's live map counts the restored VM at its snapshot's size, so a
// memMib lowered while the bundle was banked must not shrink the reservation
// below the guest the restore lands, exactly as relightNeedMib does for a
// session. An empty dir (no bundle the request can name) charges the declared
// size.
func bundleAdmitMib(declaredMib uint32, dir string) uint64 {
	if dir == "" {
		return uint64(declaredMib)
	}
	return uint64(max(declaredMib, recordedBundleMemMib(dir)))
}

// servingAdmitMib is StartServing's admission need: the declared size for a
// FRESH cold boot, and for a RELIGHT the larger of it and the banked serving
// bundle's recorded size.
func (s *Server) servingAdmitMib(req *nodev1.StartServingRequest) uint64 {
	dir := ""
	if ref := req.GetRelight().GetSnapshotRef(); safeSegment(ref) {
		dir = filepath.Join(s.servingDriver.ServingDir(), ref)
	}
	need := bundleAdmitMib(req.GetResources().GetMemMib(), dir)
	if dir != "" {
		return snapshotNeedMib(s.servingDriver, need, "serving", req.GetRelight().GetSnapshotRef(), "")
	}
	return need
}

// statefulAdmitMib is StartStateful's admission need: the declared size for a
// FRESH or COLD boot, and for a RELIGHT the larger of it and the banked
// stateful bundle's recorded size. A RELIGHT whose bundle turns out stale falls
// back to a cold boot at the declared size; it keeps the larger charge, which
// can refuse a fallback that would have fit but never under-reserves.
func (s *Server) statefulAdmitMib(req *nodev1.StartStatefulRequest) uint64 {
	dir := ""
	if ref := req.GetRelightSnapshotRef(); req.GetMode() == nodev1.StartStatefulMode_START_STATEFUL_MODE_RELIGHT && safeSegment(ref) {
		dir = filepath.Join(s.statefulDriver.StatefulDir(), ref)
	}
	need := bundleAdmitMib(req.GetResources().GetMemMib(), dir)
	if dir != "" {
		return snapshotNeedMib(s.statefulDriver, need, "stateful", req.GetRelightSnapshotRef(), "")
	}
	return need
}

// groupMemberAdmitMib is StartGroupMember's admission need: the declared size
// for a FRESH cold boot, and for a RELIGHT the larger of it and the recorded
// size of the member bundle its group/<set_id>/<member> snapshot_ref names.
func (s *Server) groupMemberAdmitMib(req *nodev1.StartGroupMemberRequest) uint64 {
	dir := ""
	if req.GetMode() == nodev1.StartGroupMemberMode_START_GROUP_MEMBER_MODE_RELIGHT {
		if setID, member, ok := parseGroupMemberRef(req.GetSnapshotRef()); ok && safeSegment(setID) && safeSegment(member) {
			dir = filepath.Join(s.groupDriver.GroupSetsDir(), setID, member)
		}
	}
	need := bundleAdmitMib(req.GetResources().GetMemMib(), dir)
	if dir != "" {
		set, member, _ := parseGroupMemberRef(req.GetSnapshotRef())
		return snapshotNeedMib(s.groupDriver, need, "group", set, member)
	}
	return need
}

// recordedBundleMemMib reads the guest memory size a snapshot bundle was taken
// at: the driver's mem_mib sidecar, else the memfile size rounded up to whole
// MiB (a full snapshot's memfile is exactly the guest memory). 0 when neither is
// readable, which leaves admission charging nothing for the VM (the pre-ledger
// behaviour) rather than refusing a restore over missing metadata.
func recordedBundleMemMib(dir string) uint32 {
	if raw, err := os.ReadFile(filepath.Join(dir, "mem_mib")); err == nil {
		if n, err := strconv.ParseUint(strings.TrimSpace(string(raw)), 10, 32); err == nil && n > 0 {
			return uint32(n)
		}
	}
	if info, err := os.Stat(filepath.Join(dir, "memfile")); err == nil && info.Size() >= 1<<20 {
		return uint32((info.Size() + (1<<20 - 1)) >> 20)
	}
	return 0
}

// requireBootResources refuses a boot request whose ResourceSpec is missing or
// zero. Without it admission would charge 0 MiB while the driver boots the VM at
// its own default (1 vCPU / 1024 MiB), so the ledger, the control plane and the
// guest would all disagree about the VM's size.
func requireBootResources(verb string, res *nodev1.ResourceSpec) error {
	if res.GetVcpus() == 0 || res.GetMemMib() == 0 {
		return status.Errorf(codes.InvalidArgument, "noded: %s requires resources with nonzero vcpus and mem_mib (got vcpus %d, mem_mib %d)", verb, res.GetVcpus(), res.GetMemMib())
	}
	return nil
}

func (s *Server) liveMemoryClaims() (int, uint64) {
	if d, ok := s.driver.(interface{ LiveMemoryClaims() (int, uint64) }); ok {
		return d.LiveMemoryClaims()
	}
	if s.driver == nil {
		return 0, 0
	}
	return s.driver.LiveCount(), s.claimedMib()
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
