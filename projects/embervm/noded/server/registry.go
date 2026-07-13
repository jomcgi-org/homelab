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

// remove deletes an id from the map and returns its entry (nil if absent). Used
// by Assign's single-use teardown and by Destroy's idempotent reap.
func (r *vmRegistry) remove(id string) *vmEntry {
	r.mu.Lock()
	defer r.mu.Unlock()
	e := r.vms[id]
	delete(r.vms, id)
	return e
}

// capacity reports the count of PRIMED (unassigned) VMs per workload and the
// total number of live VMs (primed + assigning). The control plane reads the
// primed counts as free_primed_slots.
func (r *vmRegistry) capacity() (primedPerWorkload map[string]uint32, live int) {
	r.mu.Lock()
	defer r.mu.Unlock()
	primedPerWorkload = make(map[string]uint32)
	for _, e := range r.vms {
		e.mu.Lock()
		if e.state == vmPrimed {
			primedPerWorkload[e.workload]++
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

// baseEntry records the daemon's knowledge of one built (or building/failed)
// base snapshot, keyed by its snapshot_ref (== the driver base key). It is what
// BuildBase records for idempotency and what WatchNode reports so the control
// plane reconciles existing bases instead of rebuilding.
type baseEntry struct {
	snapshotRef string
	workload    string
	imageDigest string
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

// beginBuild marks a base BUILDING. It returns false if a build for this key is
// already in progress (BUILDING), so BuildBase serializes per key without
// blocking. A READY or FAILED entry is re-driven (a rebuild after failure, or a
// forced rebuild) - the READY idempotency short-circuit is checked by the caller
// before beginBuild.
func (b *baseRegistry) beginBuild(ref, workload, readyPath string) bool {
	b.mu.Lock()
	defer b.mu.Unlock()
	if e, ok := b.bases[ref]; ok && e.state == nodev1.BaseBuildState_BASE_BUILD_STATE_BUILDING {
		return false
	}
	b.bases[ref] = &baseEntry{
		snapshotRef: ref,
		workload:    workload,
		readyPath:   readyPath,
		state:       nodev1.BaseBuildState_BASE_BUILD_STATE_BUILDING,
	}
	return true
}

// readyBuild records a completed base build.
func (b *baseRegistry) readyBuild(ref, workload, imageDigest, readyPath string, sizeBytes int64) {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.bases[ref] = &baseEntry{
		snapshotRef: ref,
		workload:    workload,
		imageDigest: imageDigest,
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
		sizeBytes:   e.sizeBytes,
		readyPath:   e.readyPath,
		state:       e.state,
	}
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
