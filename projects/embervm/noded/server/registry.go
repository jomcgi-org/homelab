package server

import (
	"sync"

	nodev1 "github.com/jomcgi/homelab/projects/embervm/proto/embervm/node/v1"

	"github.com/jomcgi/homelab/projects/embervm/noded/substrate"
)

// vmState is the lifecycle of a primed microVM in the daemon's registry. Task
// VMs are single-use: a VM is Primed until an Assign claims it, after which it is
// Assigned and then destroyed (removed from the registry). There is no path back
// to Primed - reuse across principals is forbidden by the contract.
type vmState int

const (
	vmPrimed   vmState = iota // parked, pristine, unassigned
	vmAssigned                // an Assign has claimed it; single-use in flight
)

// vmEntry is one live microVM the daemon supervises. Teardown is single-owned by
// registry removal: Assign and Destroy both remove() the entry before reaping,
// and the registry lock hands the non-nil entry to exactly one of them, so a
// racing Destroy and the single-use Assign teardown can never double-Release one
// VM (see Server.reap).
type vmEntry struct {
	id           string
	workload     string
	snapshotRef  string
	handle       substrate.Handle
	egressCancel func()

	mu    sync.Mutex // guards state
	state vmState
}

// vmRegistry is the daemon's inventory of primed/assigned microVMs, keyed by the
// opaque vm_id the control plane holds. It is the authority for free-primed-slot
// capacity and the single-use Assign transition.
type vmRegistry struct {
	mu  sync.Mutex
	vms map[string]*vmEntry
}

func newVMRegistry() *vmRegistry {
	return &vmRegistry{vms: make(map[string]*vmEntry)}
}

// add registers a freshly primed VM.
func (r *vmRegistry) add(e *vmEntry) {
	r.mu.Lock()
	r.vms[e.id] = e
	r.mu.Unlock()
}

// claimForAssign atomically transitions a primed VM to assigned and returns it.
// It returns (nil,false) when the id is unknown or not primed, which is exactly
// the "already-assigned or destroyed vm_id" case Assign must reject with
// FAILED_PRECONDITION and NO side effects: the caller does nothing else on a
// false result, so the VM is never touched and no second task can run. The entry
// stays in the map so a racing Destroy can still find and reap it; Assign removes
// it when its single use completes.
func (r *vmRegistry) claimForAssign(id string) (*vmEntry, bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	e, ok := r.vms[id]
	if !ok {
		return nil, false
	}
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.state != vmPrimed {
		return nil, false
	}
	e.state = vmAssigned
	return e, true
}

// claimForSession atomically REMOVES a primed VM from the task registry so it can
// be adopted into the session registry on a session's first SessionAssign. The
// create path primes/claims a session's VM through the shared warm pool, so it
// lands here in the task registry (Prime always adds here); the first SessionAssign
// promotes it out of this registry and into the session registry (see
// Server.adoptPrimedSession). Returns (nil,false) when the id is unknown or not
// primed (already assigned, destroyed, or already adopted), which SessionAssign
// maps to FAILED_PRECONDITION. It also requires the primed VM's workload to MATCH
// the adopting session's workload: a session may only adopt a VM primed from its own
// (class:session) base, so a task-class primed vm_id can never be hijacked as a
// session VM (defense in depth; the control plane never names a foreign vm_id). An
// empty workload never matches a real VM, so it always rejects. Unlike claimForAssign
// this DELETES the entry: the VM is leaving the task registry entirely to live as a
// session VM, so it must never be reported as a primed task slot again.
func (r *vmRegistry) claimForSession(id, workload string) (*vmEntry, bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	e, ok := r.vms[id]
	if !ok {
		return nil, false
	}
	e.mu.Lock()
	if e.state != vmPrimed || e.workload != workload {
		e.mu.Unlock()
		return nil, false
	}
	e.state = vmAssigned
	e.mu.Unlock()
	delete(r.vms, id)
	return e, true
}

// remove deletes an id from the map and returns its entry (nil if absent). Used
// by Assign's single-use teardown and by Destroy's idempotent reap.
func (r *vmRegistry) remove(id string) *vmEntry {
	r.mu.Lock()
	defer r.mu.Unlock()
	e := r.vms[id]
	delete(r.vms, id)
	return e
}

// capacity reports the PRIMED (unassigned) vm_ids per workload and the total
// number of live VMs (primed + assigning). The control plane reads the primed
// ids to ADOPT the node's warm pool into its dispatch inventory (so a control-
// plane restart does not orphan them) and their count as free_primed_slots.
func (r *vmRegistry) capacity() (primedPerWorkload map[string][]string, live int) {
	r.mu.Lock()
	defer r.mu.Unlock()
	primedPerWorkload = make(map[string][]string)
	for id, e := range r.vms {
		e.mu.Lock()
		if e.state == vmPrimed {
			primedPerWorkload[e.workload] = append(primedPerWorkload[e.workload], id)
		}
		e.mu.Unlock()
	}
	return primedPerWorkload, len(r.vms)
}

// liveCount is the current number of supervised VMs, for the node-level backstop
// cap check in Prime.
func (r *vmRegistry) liveCount() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return len(r.vms)
}

// snapshotRefInUse reports whether any live task VM (primed or assigned) was
// restored from the given base ref, i.e. its birth base is still needed by a
// running guest. It is the in-use guard for a local BASE eviction (PR-3): a base
// dir must never be removed out from under a VM that restored from it. A primed
// session VM before its first SessionAssign also lives here (claimForSession
// promotes it out only on adoption), so this covers the pre-adoption session
// lane too; post-adoption session/serving VMs ride their own session/serving
// bundle refs, never a base ref, so they are correctly not consulted here. An
// empty ref never matches a real VM.
func (r *vmRegistry) snapshotRefInUse(ref string) (string, bool) {
	if ref == "" {
		return "", false
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	for id, e := range r.vms {
		if e.snapshotRef == ref {
			return id, true
		}
	}
	return "", false
}

// ---- session VM registry ---------------------------------------------------

// sessionEntry is one LIVE session microVM the daemon supervises. Unlike a task
// vmEntry it is NOT single-use: a session VM survives every SessionAssign and is
// removed only by a Bank (snapshot + destroy) or an out-of-band Destroy. The
// inFlight flag is the per-vm in-flight serialization guard: at most one
// SessionAssign or Bank may hold a session VM at a time (the control plane
// serializes anyway; this is the daemon-side backstop the contract requires).
type sessionEntry struct {
	vmID        string
	sessionID   string
	workload    string
	snapshotRef string // the base ref it was relit/primed from (for correlation)
	handle      substrate.Handle
	// egressCancel stops this VM's egress forwarder (ADR 023 phase 6a). Never
	// nil: a VM with egress disabled carries a no-op, so every teardown path can
	// call it unconditionally. A session adopted from a primed task VM INHERITS
	// the forwarder Prime started rather than opening a second one, so the cancel
	// travels with the VM across that registry move.
	egressCancel func()

	mu       sync.Mutex // guards inFlight
	inFlight bool
}

// sessionRegistry is the daemon's inventory of LIVE session microVMs, keyed by the
// opaque vm_id the control plane holds. It is kept DISTINCT from the task
// vmRegistry so a session VM is never reported in primed_vm_ids and never adopted
// into the single-use task pool, while still counting against the node live-VM cap
// (liveCount sums both registries at the call sites).
type sessionRegistry struct {
	mu  sync.Mutex
	vms map[string]*sessionEntry
}

func newSessionRegistry() *sessionRegistry {
	return &sessionRegistry{vms: make(map[string]*sessionEntry)}
}

// add registers a freshly relit (or primed-into-session) live session VM.
func (r *sessionRegistry) add(e *sessionEntry) {
	r.mu.Lock()
	r.vms[e.vmID] = e
	r.mu.Unlock()
}

// beginInFlight marks a session VM busy for a SessionAssign or Bank. It returns
// (entry, true) only when the id names a known session VM with no operation
// already in flight; a concurrent op on the same vm_id gets (nil, false), which
// the caller maps to FAILED_PRECONDITION. An unknown id also returns (nil, false).
func (r *sessionRegistry) beginInFlight(id string) (*sessionEntry, bool) {
	r.mu.Lock()
	e, ok := r.vms[id]
	r.mu.Unlock()
	if !ok {
		return nil, false
	}
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.inFlight {
		return nil, false
	}
	e.inFlight = true
	return e, true
}

// endInFlight clears the in-flight guard for a session VM that is still live
// (after a SessionAssign returns). A Bank instead remove()s the entry, so it never
// calls this.
func (e *sessionEntry) endInFlight() {
	e.mu.Lock()
	e.inFlight = false
	e.mu.Unlock()
}

// remove deletes an id from the map and returns its entry (nil if absent). Used by
// Bank's snapshot-and-destroy tail and by an out-of-band Destroy.
func (r *sessionRegistry) remove(id string) *sessionEntry {
	r.mu.Lock()
	defer r.mu.Unlock()
	e := r.vms[id]
	delete(r.vms, id)
	return e
}

// snapshot returns a copy of every live session VM, for building NodeStatus.
// sessionView is a lock-free, read-only projection of a sessionEntry. It omits the
// mutex so callers can range over the returned slice without tripping the copylocks
// vet check (a sessionEntry embeds sync.Mutex and must never be copied by value).
type sessionView struct {
	vmID        string
	sessionID   string
	workload    string
	snapshotRef string
}

func (r *sessionRegistry) snapshot() []sessionView {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make([]sessionView, 0, len(r.vms))
	for _, e := range r.vms {
		out = append(out, sessionView{
			vmID:      e.vmID,
			sessionID: e.sessionID,
			workload:  e.workload,
		})
	}
	return out
}

// snapshotWithRefs returns a copy of every live session VM including its source
// snapshotRef, for the EvictSnapshot in-use guard (evicting a bundle a live VM was
// relit from is refused).
func (r *sessionRegistry) snapshotWithRefs() []sessionView {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make([]sessionView, 0, len(r.vms))
	for _, e := range r.vms {
		out = append(out, sessionView{
			vmID:        e.vmID,
			sessionID:   e.sessionID,
			workload:    e.workload,
			snapshotRef: e.snapshotRef,
		})
	}
	return out
}

// ---- banked session snapshot inventory -------------------------------------

// sessionSnapshotEntry is one BANKED session snapshot bundle on node disk. The
// daemon rescans the sessions dir on start to seed this inventory and maintains it
// in memory as Bank adds and EvictSnapshot removes entries. It is the source of
// truth the control plane reconciles banked-session state from (a banked session
// survives a daemon restart; a live one does not, because its Firecracker child
// dies with the daemon).
type sessionSnapshotEntry struct {
	snapshotRef     string
	sessionID       string
	workload        string
	sizeBytes       int64
	createdAtUnixMs int64
}

// sessionSnapshotRegistry is the in-memory banked-snapshot inventory, keyed by
// snapshot_ref. It is seeded from disk on start (session_id/workload are unknown
// for a disk-only entry until the control plane rebinds by adoption) and updated
// on Bank/EvictSnapshot.
type sessionSnapshotRegistry struct {
	mu    sync.Mutex
	snaps map[string]*sessionSnapshotEntry
}

func newSessionSnapshotRegistry() *sessionSnapshotRegistry {
	return &sessionSnapshotRegistry{snaps: make(map[string]*sessionSnapshotEntry)}
}

// add records a banked snapshot (from a Bank or a startup rescan). A rescan-seeded
// entry may carry an empty session_id/workload; a later Bank for the same ref
// overwrites it with the known identity.
func (r *sessionSnapshotRegistry) add(e sessionSnapshotEntry) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.snaps[e.snapshotRef] = &sessionSnapshotEntry{
		snapshotRef:     e.snapshotRef,
		sessionID:       e.sessionID,
		workload:        e.workload,
		sizeBytes:       e.sizeBytes,
		createdAtUnixMs: e.createdAtUnixMs,
	}
}

// has reports whether a snapshot_ref is a known banked session snapshot.
func (r *sessionSnapshotRegistry) has(ref string) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	_, ok := r.snaps[ref]
	return ok
}

// remove deletes a snapshot_ref from the inventory (after EvictSnapshot). Idempotent.
func (r *sessionSnapshotRegistry) remove(ref string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	delete(r.snaps, ref)
}

// snapshot returns a copy of every banked snapshot entry, for building NodeStatus.
func (r *sessionSnapshotRegistry) snapshot() []sessionSnapshotEntry {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make([]sessionSnapshotEntry, 0, len(r.snaps))
	for _, e := range r.snaps {
		out = append(out, *e)
	}
	return out
}

// baseEntry records the daemon's knowledge of one built (or building/failed)
// base snapshot, keyed by its snapshot_ref (== the driver base key). It is what
// BuildBase records for idempotency and what WatchNode reports so the control
// plane reconciles existing bases instead of rebuilding.
type baseEntry struct {
	snapshotRef string
	workload    string
	imageDigest string
	rootfsPath  string
	sizeBytes   int64
	readyPath   string
	state       nodev1.BaseBuildState
	buildErr    string
}

// baseRegistry tracks base build state per snapshot_ref. It survives across a VM
// pool being drained (bases are node-local snapshot files, not tied to any live
// VM) and is repopulated from disk on daemon start.
type baseRegistry struct {
	mu    sync.Mutex
	bases map[string]*baseEntry
}

func newBaseRegistry() *baseRegistry {
	return &baseRegistry{bases: make(map[string]*baseEntry)}
}

// get returns a copy of the base entry for a snapshot_ref.
func (b *baseRegistry) get(ref string) (baseEntry, bool) {
	b.mu.Lock()
	defer b.mu.Unlock()
	e, ok := b.bases[ref]
	if !ok {
		return baseEntry{}, false
	}
	return *e, true
}

// readyByWorkload returns the snapshot_ref of a READY base for the workload, so
// the node-local stateful activator can resolve boot_image_ref itself (the
// control plane supplies it per-call in StartStatefulRequest, but the activator
// has no control plane during a gap). The base key is node-local, so it cannot
// ride the global workload registry; the daemon owns it here, exactly as the
// serving activator resolves its serving image from its own inventory. On more
// than one READY base for a workload (a mid-turnover overlap) the lexically
// greatest ref wins, deterministically.
func (b *baseRegistry) readyByWorkload(workload string) (string, bool) {
	b.mu.Lock()
	defer b.mu.Unlock()
	best := ""
	for ref, e := range b.bases {
		if e.workload == workload && e.state == nodev1.BaseBuildState_BASE_BUILD_STATE_READY {
			if best == "" || ref > best {
				best = ref
			}
		}
	}
	return best, best != ""
}

// beginBuild marks a base BUILDING. It returns false if a build for this key is
// already in progress (BUILDING), so BuildBase serializes per key without
// blocking. A READY or FAILED entry is re-driven (a rebuild after failure, or a
// forced rebuild) - the READY idempotency short-circuit is checked by the caller
// before beginBuild.
func (b *baseRegistry) beginBuild(ref, workload, rootfsPath, readyPath string) bool {
	b.mu.Lock()
	defer b.mu.Unlock()
	if e, ok := b.bases[ref]; ok && e.state == nodev1.BaseBuildState_BASE_BUILD_STATE_BUILDING {
		return false
	}
	b.bases[ref] = &baseEntry{
		snapshotRef: ref,
		workload:    workload,
		rootfsPath:  rootfsPath,
		readyPath:   readyPath,
		state:       nodev1.BaseBuildState_BASE_BUILD_STATE_BUILDING,
	}
	return true
}

// readyBuild records a completed base build.
func (b *baseRegistry) readyBuild(ref, workload, imageDigest, rootfsPath, readyPath string, sizeBytes int64) {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.bases[ref] = &baseEntry{
		snapshotRef: ref,
		workload:    workload,
		imageDigest: imageDigest,
		rootfsPath:  rootfsPath,
		sizeBytes:   sizeBytes,
		readyPath:   readyPath,
		state:       nodev1.BaseBuildState_BASE_BUILD_STATE_READY,
	}
}

// failBuild records a failed base build with its error, surfaced in NodeStatus.
func (b *baseRegistry) failBuild(ref, buildErr string) {
	b.mu.Lock()
	defer b.mu.Unlock()
	if e, ok := b.bases[ref]; ok {
		e.state = nodev1.BaseBuildState_BASE_BUILD_STATE_FAILED
		e.buildErr = buildErr
		return
	}
	b.bases[ref] = &baseEntry{
		snapshotRef: ref,
		state:       nodev1.BaseBuildState_BASE_BUILD_STATE_FAILED,
		buildErr:    buildErr,
	}
}

// register records a base discovered on disk at startup (state READY) so the
// control plane reconciles rather than rebuilding.
func (b *baseRegistry) register(e baseEntry) {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.bases[e.snapshotRef] = &baseEntry{
		snapshotRef: e.snapshotRef,
		workload:    e.workload,
		imageDigest: e.imageDigest,
		rootfsPath:  e.rootfsPath,
		sizeBytes:   e.sizeBytes,
		readyPath:   e.readyPath,
		state:       e.state,
	}
}

// rootfsPaths returns rootfs files referenced by READY bases. Snapshot state
// stores these paths directly, so GC must preserve them even when the workload
// registry has moved on to a newer rootfs.
func (b *baseRegistry) rootfsPaths() []string {
	b.mu.Lock()
	defer b.mu.Unlock()
	var paths []string
	for _, e := range b.bases {
		// Any state, not just READY: a BUILDING base is actively writing against its
		// rootfs, and sweeping it mid-build is the same corruption by a narrower
		// window. A base that names a rootfs is holding it open, whatever its state.
		if e.rootfsPath != "" {
			paths = append(paths, e.rootfsPath)
		}
	}
	return paths
}

// unknownRootfsWorkloads names the workloads holding at least one base that does
// NOT record which rootfs it was built against: every base that predates the
// rootfsPath field, since it is repopulated from disk with no path to read.
//
// Their true keep-set is unknowable, so sweepRootfs declines to sweep those
// workloads' directories entirely rather than deleting a file that might be the
// one a base still has open. That trades reclaimed bytes for correctness on
// exactly the bases that caused the outage, and it drains by itself: each rebuild
// records a path, so a workload leaves this set for good once its bases turn over.
func (b *baseRegistry) unknownRootfsWorkloads() map[string]bool {
	b.mu.Lock()
	defer b.mu.Unlock()
	unknown := make(map[string]bool)
	for _, e := range b.bases {
		if e.rootfsPath == "" && e.workload != "" {
			unknown[e.workload] = true
		}
	}
	return unknown
}

// remove forgets a base entry by snapshot_ref (after its on-disk dir is deleted),
// so NodeStatus stops advertising it. Idempotent on an absent ref.
func (b *baseRegistry) remove(ref string) {
	b.mu.Lock()
	delete(b.bases, ref)
	b.mu.Unlock()
}

// snapshot returns a copy of all base entries, for building NodeStatus.
func (b *baseRegistry) snapshot() []baseEntry {
	b.mu.Lock()
	defer b.mu.Unlock()
	out := make([]baseEntry, 0, len(b.bases))
	for _, e := range b.bases {
		out = append(out, *e)
	}
	return out
}

// firstBuildError returns the build error of any FAILED base (for the
// NodeStatus.build_error field), or "".
func (b *baseRegistry) firstBuildError() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	for _, e := range b.bases {
		if e.state == nodev1.BaseBuildState_BASE_BUILD_STATE_FAILED && e.buildErr != "" {
			return e.buildErr
		}
	}
	return ""
}
