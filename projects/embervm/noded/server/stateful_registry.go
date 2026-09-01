package server

import (
	"context"
	"net"
	"sync"
	"time"

	nodev1 "github.com/jomcgi/homelab/projects/embervm/proto/embervm/node/v1"

	"github.com/jomcgi/homelab/projects/embervm/noded/serving"
	"github.com/jomcgi/homelab/projects/embervm/noded/substrate"
)

// statefulDriver is the subset of the fcvm driver the R4 stateful verbs need on
// top of vmDriver: cold-boot a stateful VM WITH a tap NIC and a writable volume
// (mirroring ClaimServing plus the volume drive), bank a live stateful VM to a
// self-contained bundle stamped with a generation, relight (restore) from a
// banked bundle, remove a banked bundle, and rescan banked bundles on start.
// The real *driver.Driver satisfies it; tests inject a fake. A separate seam
// (like servingDriver) so a Server built without stateful support still
// compiles and a reviewer sees exactly which driver mechanics stateful reuses.
type statefulDriver interface {
	// ClaimStateful cold-boots a stateful VM from a per-workload rootfs WITH the
	// given tap NIC, the optional handler artifact drive (mirroring the serving
	// cold-boot lane), the workload's writable volume attached as a third
	// drive, and mmdsEnv (R4, D-R4.PR-7.1: MMDS-lite over boot-args), the
	// workload's first-boot secrets encoded into ember.env.<KEY>= kernel
	// boot-args. Mirrors ClaimServing with the volume + mmdsEnv parameters
	// appended. mmdsEnv is meaningful only for FRESH/COLD; callers on a
	// RELIGHT path never reach this method at all (relight resumes a memory
	// snapshot via RestoreStateful instead).
	ClaimStateful(ctx context.Context, rootfsPath, harnessInit string, vcpus, memMib int, nic substrate.NICSpec, handlerDiskPath string, handlerZipBytes int64, volumeDiskPath, volumeMount string, mmdsEnv map[string]string) (substrate.Handle, error)
	// SnapshotStateful pauses a live stateful VM and writes a self-contained
	// stateful bundle stamped with the given volume generation and the pinnedIP the
	// VM held; does not resume (the caller Releases). Mirrors SnapshotServing plus
	// the generation stamp. pinnedIP is re-acquired on relight so the resumed
	// guest's baked-in eth0 IP matches the fresh tap.
	SnapshotStateful(ctx context.Context, h substrate.Handle, snapshotRef string, generation uint64, pinnedIP string) (substrate.SnapshotRef, error)
	// CheckpointStateful is phase one of the interruptible bank (ADR embervm/008):
	// it pauses a live stateful VM and writes its snapshot to a temp OUTSIDE the
	// stateful/ bundle dir, leaving the VM PAUSED and resumable (no bundle
	// published, no Release), and returns an opaque token for the resolve. pinnedIP
	// is carried so the COMMIT publishes it as the bundle's pinned-IP sidecar.
	CheckpointStateful(ctx context.Context, h substrate.Handle, snapshotRef string, generation uint64, pinnedIP string) (string, error)
	// StatefulPinnedIP reads the tap IP a bundle was banked with (empty if absent),
	// so a relight re-acquires the SAME IP before restoring.
	StatefulPinnedIP(snapshotRef string) string
	// ResolveStatefulCommit publishes a checkpoint's temp as the workload's bundle
	// (snapfile last) and DESTROYS the paused VM, returning the bundle ref. It
	// consumes the token (single-resolve).
	ResolveStatefulCommit(ctx context.Context, token string) (substrate.SnapshotRef, error)
	// ResolveStatefulAbort deletes a checkpoint's temp and RESUMES the paused VM
	// (same process image, hot). It consumes the token (single-resolve). The
	// caller bumps the volume generation BEFORE calling this (bump, delete, resume).
	ResolveStatefulAbort(ctx context.Context, token string) error
	// GCStatefulCheckpoints sweeps orphaned interruptible-bank checkpoint temps on
	// startup (a restart kills every paused checkpoint VM), returning the count
	// removed. Idempotent.
	GCStatefulCheckpoints() int
	// RestoreStateful launches a fresh VM from a banked stateful bundle and
	// resumes it, WITH the NIC captured at bank time. volumeDiskPath is the
	// workload's volume file the caller intends this restored VM to hold (see
	// the driver method's doc for why the restore mechanic itself does not use
	// it directly). Mirrors RestoreServing.
	RestoreStateful(ctx context.Context, snapshotRef, volumeDiskPath string) (substrate.Handle, error)
	// RemoveStatefulBundle deletes a banked stateful bundle from disk
	// (idempotent). Never touches the volume file itself.
	RemoveStatefulBundle(snapshotRef string) error
	// ScanStatefulBundles globs the stateful bundle dir on startup and returns
	// each discovered bundle with its stamped generation, so the daemon
	// re-seeds its banked-stateful inventory after a restart.
	ScanStatefulBundles() []substrate.StatefulBundleInfo
	// StatefulDir is the directory holding banked stateful bundles, rescanned
	// on start.
	StatefulDir() string
	// StatefulAPISocketPath returns the Firecracker API socket for a live handle.
	// The wake path uses a connect probe to reject stale registry entries whose
	// process has died without running normal teardown.
	StatefulAPISocketPath(substrate.Handle) string
}

// statefulEntry is one LIVE stateful microVM the daemon supervises (R4). Like a
// serving VM it is NOT single-use: it survives every request (opaque L4 TCP,
// no daemon involvement on the data hit path) and is removed only by a
// StopStateful (bank or destroy) or an out-of-band Destroy. Unlike serving,
// exactly one live VM may exist per WORKLOAD at a time (singleton, enforced by
// the volume package's attach lock, not by this registry), so vmID is still the
// map key (mirroring every other registry's shape) but workload uniqueness is
// the real invariant callers rely on. generation is the volume generation this
// VM was booted with; snapshotRef is the bundle it was relit from ("" for a
// cold/fresh boot). The inFlight flag is the per-vm stop serialization guard,
// identical to servingEntry's bank guard.
type statefulEntry struct {
	vmID       string
	workload   string
	handle     substrate.Handle
	ip         net.IP
	port       uint32
	tap        string
	generation uint64
	origin     nodev1.InstanceOrigin
	// snapshotRef is the stateful bundle this VM was RELIT from, or "" for a
	// cold/fresh boot (which has no source snapshot). Mirrors servingEntry's
	// snapshotRef, though R4 v1 does not add an in-use eviction guard on it
	// (there is at most one bundle per workload and BANK always evicts the
	// prior one unconditionally, so no live VM can outlive the bundle it
	// depends on the way a serving relight can).
	snapshotRef string
	// probe is the running TCP-connect health-probe loop for this VM.
	probe *serving.ProbeHandle

	// checkpointToken is set (under the registry lock) while an interruptible-bank
	// CHECKPOINT has this VM PAUSED awaiting a ResolveStateful (ADR embervm/008);
	// "" for a normally-serving VM. checkpointTimer arms noded's resolve-timeout
	// auto-abort (a dead control plane must not pin a paused VM forever); it is
	// Stopped when a resolve is claimed. Both are guarded by the registry mu (not
	// the entry mu), so snapshot() can read the checkpoint state under the one lock
	// it already holds.
	checkpointToken string
	checkpointTimer *time.Timer

	mu       sync.Mutex // guards inFlight
	inFlight bool
}

// statefulRegistry is the daemon's inventory of LIVE stateful microVMs, keyed
// by the opaque vm_id the control plane holds. Kept DISTINCT from every other
// class registry so a stateful VM is never reported in primed_vm_ids and never
// confused with a session or serving VM, while still counting against the node
// live-VM cap (the shared driver's LiveCount sums all classes).
type statefulRegistry struct {
	mu  sync.Mutex
	vms map[string]*statefulEntry
}

func newStatefulRegistry() *statefulRegistry {
	return &statefulRegistry{vms: make(map[string]*statefulEntry)}
}

// add registers a freshly started (fresh, cold, or relit) live stateful VM.
func (r *statefulRegistry) add(e *statefulEntry) {
	r.mu.Lock()
	r.vms[e.vmID] = e
	r.mu.Unlock()
}

// beginStop marks a stateful VM busy for a StopStateful(BANK), mirroring
// servingRegistry.beginBank. It returns (entry, true) only when the id names a
// known stateful VM with no stop already in flight; a concurrent bank on the
// same vm_id gets (nil, false), mapped to FAILED_PRECONDITION.
func (r *statefulRegistry) beginStop(id string) (*statefulEntry, bool) {
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

// clearInFlight releases the stop-serialization guard set by beginStop without
// removing the entry, so a VM whose CHECKPOINT failed returns to serving (still
// live, still probed) rather than being wrongly pinned as stop-in-flight.
func (r *statefulRegistry) clearInFlight(id string) {
	r.mu.Lock()
	e := r.vms[id]
	r.mu.Unlock()
	if e == nil {
		return
	}
	e.mu.Lock()
	e.inFlight = false
	e.mu.Unlock()
}

// markCheckpointed records that a CHECKPOINT has paused this VM awaiting a
// resolve (ADR embervm/008). The entry stays inFlight (beginStop set it) so it is
// neither bankable nor checkpointable again until the resolve lands. The
// resolve-timeout auto-abort timer is armed SEPARATELY (setCheckpointTimer) AFTER
// this, so a timer that fires immediately always finds the token already set and
// its auto-abort works rather than no-opping.
func (r *statefulRegistry) markCheckpointed(id, token string) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	e := r.vms[id]
	if e == nil {
		return false
	}
	e.checkpointToken = token
	return true
}

// setCheckpointTimer installs the resolve-timeout auto-abort timer on a
// checkpoint-pending entry. Called right after markCheckpointed so the token is
// already set when the timer can first fire. If the entry vanished or was already
// resolved (token cleared by a racing claimResolve) between the two calls, the
// timer is stopped and not installed, so it never fires on a resolved entry.
func (r *statefulRegistry) setCheckpointTimer(id string, timer *time.Timer) {
	r.mu.Lock()
	defer r.mu.Unlock()
	e := r.vms[id]
	if e == nil || e.checkpointToken == "" {
		timer.Stop()
		return
	}
	e.checkpointTimer = timer
}

// claimResolve is the node's single-resolve gate: it returns the entry ONLY if it
// is still checkpoint-pending with the given token, and atomically clears the
// token and stops the timer so a concurrent resolve (the control plane vs the
// auto-abort timer) loses the race. A COMMIT arriving after the timeout auto-abort
// already claimed the resolve gets (nil, false), mapped to FAILED_PRECONDITION.
func (r *statefulRegistry) claimResolve(id, token string) (*statefulEntry, bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	e := r.vms[id]
	if e == nil || e.checkpointToken == "" || e.checkpointToken != token {
		return nil, false
	}
	if e.checkpointTimer != nil {
		e.checkpointTimer.Stop()
		e.checkpointTimer = nil
	}
	e.checkpointToken = ""
	return e, true
}

// resumeFromCheckpoint returns a checkpoint-aborted VM to serving: it updates the
// generation to the post-abort bump and clears the stop-in-flight guard (the
// token was already cleared by claimResolve). The probe kept running across the
// pause, so no probe restart is needed.
func (r *statefulRegistry) resumeFromCheckpoint(id string, newGeneration uint64) {
	r.mu.Lock()
	e := r.vms[id]
	if e != nil {
		e.generation = newGeneration
	}
	r.mu.Unlock()
	if e != nil {
		e.mu.Lock()
		e.inFlight = false
		e.mu.Unlock()
	}
}

// remove deletes an id from the map and returns its entry (nil if absent).
func (r *statefulRegistry) remove(id string) *statefulEntry {
	r.mu.Lock()
	defer r.mu.Unlock()
	e := r.vms[id]
	delete(r.vms, id)
	return e
}

// byWorkload finds the live stateful VM for a workload, if any. Since a
// stateful workload is singleton (the volume attach lock enforces at most one
// writable attach), this is at most one entry; used for lookups by workload
// rather than by vm_id.
func (r *statefulRegistry) byWorkload(workload string) (*statefulEntry, bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	for _, e := range r.vms {
		if e.workload == workload {
			return e, true
		}
	}
	return nil, false
}

// byWorkloadCheckpoint returns the workload's live entry, checkpoint token, and
// stop-in-flight guard under the registry lock, so the activator can decide
// whether it must claim and abort a paused checkpoint before splicing. The
// entry guard is read while holding e.mu, with r.mu held first.
func (r *statefulRegistry) byWorkloadCheckpoint(workload string) (e *statefulEntry, token string, inFlight bool, ok bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	for _, entry := range r.vms {
		if entry.workload == workload {
			entry.mu.Lock()
			inFlight = entry.inFlight
			entry.mu.Unlock()
			return entry, entry.checkpointToken, inFlight, true
		}
	}
	return nil, "", false, false
}

// snapshotRefInUse reports whether any LIVE stateful VM was relit from the given
// stateful bundle ref, i.e. the bundle is still needed by a running guest that
// resumed from it. It is the in-use guard for a local STATEFUL eviction (#38):
// evicting a bundle out from under a live relit VM would lose the state needed to
// re-bank it if the VM dies before the next bank. It scans the LIVE registry's
// snapshotRef (set on relight, "" on a fresh/cold boot, so a fresh VM never
// matches). The R4 v1 note on statefulEntry.snapshotRef deferred exactly this
// guard; the reaper's per-bundle evict makes it load-bearing, so it lands now. An
// empty ref never matches. Returns the vm_id of the first live VM holding the ref.
func (r *statefulRegistry) snapshotRefInUse(ref string) (string, bool) {
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

// statefulView is a lock-free, read-only projection of a statefulEntry, for
// NodeStatus.stateful_vms.
type statefulView struct {
	vmID            string
	workload        string
	ip              string
	port            uint32
	healthy         bool
	lastProbeUnixMs int64
	generation      uint64
	origin          nodev1.InstanceOrigin
	// checkpointPending + checkpointToken report a VM PAUSED awaiting a resolve
	// (ADR embervm/008) so a restarted control plane adopts and resolves it.
	checkpointPending bool
	checkpointToken   string
}

// snapshot returns a copy of every live stateful VM including its current
// TCP-probe health verdict, for building NodeStatus.stateful_vms.
func (r *statefulRegistry) snapshot() []statefulView {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make([]statefulView, 0, len(r.vms))
	for _, e := range r.vms {
		v := statefulView{
			vmID:              e.vmID,
			workload:          e.workload,
			ip:                e.ip.String(),
			port:              e.port,
			generation:        e.generation,
			origin:            e.origin,
			checkpointPending: e.checkpointToken != "",
			checkpointToken:   e.checkpointToken,
		}
		if e.probe != nil {
			res := e.probe.Result()
			v.healthy = res.Healthy
			v.lastProbeUnixMs = res.LastProbeUnixMs
		}
		out = append(out, v)
	}
	return out
}

// count is the number of live stateful VMs (for NodeStatus live_vms summing).
func (r *statefulRegistry) count() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return len(r.vms)
}

// ---- banked stateful bundle inventory ---------------------------------------

// statefulBundleEntry is the ONE banked stateful bundle for a workload on node
// disk (D-R4: at most one per workload; a new bank evicts the prior). It sits
// under the stateful/ prefix and is STAMPED with the volume generation it was
// paused at, the pair key a relight checks against the volume's current
// generation.
type statefulBundleEntry struct {
	snapshotRef     string
	workload        string
	generation      uint64
	sizeBytes       int64
	createdAtUnixMs int64
}

// statefulBundleRegistry is the in-memory banked-stateful-bundle inventory,
// keyed by snapshot_ref. Seeded from disk on start and updated on bank/evict.
// Unlike servingSnapshotRegistry (which may hold many snapshots per workload),
// callers enforce the at-most-one-per-workload invariant by evicting any prior
// bundle for a workload before adding a new one (see evictByWorkload).
type statefulBundleRegistry struct {
	mu    sync.Mutex
	snaps map[string]*statefulBundleEntry
}

func newStatefulBundleRegistry() *statefulBundleRegistry {
	return &statefulBundleRegistry{snaps: make(map[string]*statefulBundleEntry)}
}

// add records a banked stateful bundle (from a bank or a startup rescan).
func (r *statefulBundleRegistry) add(e statefulBundleEntry) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.snaps[e.snapshotRef] = &statefulBundleEntry{
		snapshotRef:     e.snapshotRef,
		workload:        e.workload,
		generation:      e.generation,
		sizeBytes:       e.sizeBytes,
		createdAtUnixMs: e.createdAtUnixMs,
	}
}

// get returns a copy of the banked bundle entry for a ref.
func (r *statefulBundleRegistry) get(ref string) (statefulBundleEntry, bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	e, ok := r.snaps[ref]
	if !ok {
		return statefulBundleEntry{}, false
	}
	return *e, true
}

// byWorkload returns the current banked bundle for a workload, if any (at most
// one by construction).
func (r *statefulBundleRegistry) byWorkload(workload string) (statefulBundleEntry, bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	for _, e := range r.snaps {
		if e.workload == workload {
			return *e, true
		}
	}
	return statefulBundleEntry{}, false
}

// remove deletes a snapshot_ref from the inventory. Idempotent.
func (r *statefulBundleRegistry) remove(ref string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	delete(r.snaps, ref)
}

// snapshot returns a copy of every banked stateful bundle entry, for NodeStatus.
func (r *statefulBundleRegistry) snapshot() []statefulBundleEntry {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make([]statefulBundleEntry, 0, len(r.snaps))
	for _, e := range r.snaps {
		out = append(out, *e)
	}
	return out
}
