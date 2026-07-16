package server

import (
	"context"
	"net"
	"sync"

	"github.com/jomcgi/homelab/projects/embervm/noded/serving"
	"github.com/jomcgi/homelab/projects/embervm/noded/substrate"
)

// servingNetwork is the host serving-network seam the server depends on: bridge +
// nftables setup, per-VM tap allocation/teardown, and the pinned-IP re-acquire for
// relight (D-R3.4.1). The real *serving.Manager satisfies it; tests inject a fake
// that records ip/nft intent without touching the host. It is a seam (not the
// concrete Manager) so a Server built without serving support still compiles and so
// a reviewer sees exactly which network operations the serving handlers perform.
type servingNetwork interface {
	// EnsureNetwork creates the bridge and installs the ingress-only nftables posture
	// (idempotent), called once on daemon start.
	EnsureNetwork(ctx context.Context) error
	// AllocateTap allocates the next free IP and creates its tap; used by fresh start.
	AllocateTap(ctx context.Context) (tap string, ip net.IP, err error)
	// AllocateTapForIP re-acquires a SPECIFIC IP and creates its tap; used by relight
	// to pin the IP the banked snapshot recorded (D-R3.4.1).
	AllocateTapForIP(ctx context.Context, ip net.IP) (tap string, err error)
	// ReleaseTap deletes a VM's tap and frees its IP (teardown).
	ReleaseTap(ctx context.Context, ip net.IP)
	// GatewayIP is the bridge IP a guest uses as its default route.
	GatewayIP() net.IP
	// PrefixLen is the serving CIDR prefix length, for the guest static IP mask.
	PrefixLen() int
	// CIDR reports the serving subnet CIDR for NodeStatus.serving_subnet_cidr.
	CIDR() string
}

// servingDriver is the subset of the fcvm driver the serving verbs need on top of
// vmDriver: cold-boot a serving VM WITH a tap NIC (fresh), bank a live serving VM to a
// self-contained bundle under serving/, relight (restore) from a banked serving
// bundle, read a banked bundle's pinned IP, and remove a banked bundle. The real
// *driver.Driver satisfies it; tests inject a fake. A separate seam (like sessionDriver)
// so a Server built without serving support still compiles and a reviewer sees exactly
// which driver mechanics serving reuses.
type servingDriver interface {
	// ClaimServing cold-boots a serving VM from a per-workload rootfs WITH the given
	// tap NIC configured pre-Start and the static IP delivered via boot-args. It is the
	// fresh-serving counterpart of the task Prime's restore, except it is a COLD boot
	// (a resumed snapshot cannot gain a NIC, D-R3.4.2). rootfsPath/vcpus/memMib come
	// from the serving workload's image identity, resolved by the server.
	ClaimServing(ctx context.Context, rootfsPath, harnessInit string, vcpus, memMib int, nic substrate.NICSpec) (substrate.Handle, error)
	// SnapshotServing pauses a live serving VM and writes a self-contained serving
	// bundle under serving/<ref> plus the pinned-IP sidecar; does not resume (the
	// caller Releases). Mirrors SnapshotSession.
	SnapshotServing(ctx context.Context, h substrate.Handle, snapshotRef, pinnedIP string) (substrate.SnapshotRef, error)
	// RestoreServing launches a fresh VM from a banked serving bundle and resumes it,
	// WITH the NIC captured at bank time. Mirrors RestoreSession.
	RestoreServing(ctx context.Context, snapshotRef string) (substrate.Handle, error)
	// ServingPinnedIP reads the tap IP a banked serving snapshot recorded (D-R3.4.1),
	// or "" if absent.
	ServingPinnedIP(snapshotRef string) string
	// RemoveServingBundle deletes a banked serving bundle from disk (idempotent).
	RemoveServingBundle(snapshotRef string) error
	// ServingDir is the directory holding banked serving bundles, rescanned on start.
	ServingDir() string
}

// servingEntry is one LIVE serving microVM the daemon supervises. Like a session VM
// it is NOT single-use: it survives every request (the daemon is off the request hit
// path entirely, requests reach it directly over its tap) and is removed only by a
// StopServing (bank or destroy) or an out-of-band Destroy. The inFlight flag is the
// per-vm bank serialization guard the PR-1 contract requires: at most one StopServing
// (BANK) may hold a serving VM at a time. The probe handle owns the per-VM health loop
// and is stopped on teardown; ip/port/tap are the endpoint facts the daemon reports.
type servingEntry struct {
	vmID     string
	workload string
	handle   substrate.Handle
	ip       net.IP
	port     uint32
	tap      string
	// probe is the running health-probe loop for this VM; Stop() cancels it on
	// teardown. Its latest Result() feeds NodeStatus.serving_vms.healthy.
	probe *serving.ProbeHandle

	mu       sync.Mutex // guards inFlight
	inFlight bool
}

// servingRegistry is the daemon's inventory of LIVE serving microVMs, keyed by the
// opaque vm_id the control plane holds. It is kept DISTINCT from the task and session
// registries so a serving VM is never reported in primed_vm_ids and never adopted into
// the task pool, while still counting against the node live-VM cap (the shared driver's
// LiveCount sums all classes at the call sites).
type servingRegistry struct {
	mu  sync.Mutex
	vms map[string]*servingEntry
}

func newServingRegistry() *servingRegistry {
	return &servingRegistry{vms: make(map[string]*servingEntry)}
}

// add registers a freshly started (fresh or relit) live serving VM.
func (r *servingRegistry) add(e *servingEntry) {
	r.mu.Lock()
	r.vms[e.vmID] = e
	r.mu.Unlock()
}

// beginBank marks a serving VM busy for a StopServing(BANK). It returns (entry, true)
// only when the id names a known serving VM with no bank already in flight; a
// concurrent bank on the same vm_id gets (nil, false), which the caller maps to
// FAILED_PRECONDITION (the PR-1 contract's concurrent-bank refusal). An unknown id
// also returns (nil, false).
func (r *servingRegistry) beginBank(id string) (*servingEntry, bool) {
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

// remove deletes an id from the map and returns its entry (nil if absent). Used by
// StopServing's bank/destroy tail and by an out-of-band Destroy.
func (r *servingRegistry) remove(id string) *servingEntry {
	r.mu.Lock()
	defer r.mu.Unlock()
	e := r.vms[id]
	delete(r.vms, id)
	return e
}

// servingView is a lock-free, read-only projection of a servingEntry, omitting the
// mutex so callers can range without tripping copylocks. It carries the reported
// endpoint facts plus the current health verdict read from the probe handle.
type servingView struct {
	vmID            string
	workload        string
	ip              string
	port            uint32
	healthy         bool
	lastProbeUnixMs int64
}

// snapshot returns a copy of every live serving VM including its current health
// verdict, for building NodeStatus.serving_vms. The health fact is read from each
// entry's probe handle (its own lock), never computed here.
func (r *servingRegistry) snapshot() []servingView {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make([]servingView, 0, len(r.vms))
	for _, e := range r.vms {
		v := servingView{
			vmID:     e.vmID,
			workload: e.workload,
			ip:       e.ip.String(),
			port:     e.port,
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

// count is the number of live serving VMs (for NodeStatus live_vms summing).
func (r *servingRegistry) count() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return len(r.vms)
}

// ---- banked serving snapshot inventory -------------------------------------

// servingSnapshotEntry is one BANKED serving snapshot bundle on node disk, stored
// under the serving/ prefix of the sessions-style bundle dir. It mirrors
// sessionSnapshotEntry but ALSO records the pinned tap IP (D-R3.4.1): a relight MUST
// re-acquire the same IP because the guest's eth0 keeps the IP baked at fresh boot, so
// the IP travels with the snapshot. The daemon rescans the serving/ dir on start to
// seed this inventory and maintains it as StopServing(BANK) adds and EvictSnapshot
// removes entries.
type servingSnapshotEntry struct {
	snapshotRef     string
	workload        string
	ip              string // the pinned tap IP (D-R3.4.1), re-acquired on relight
	sizeBytes       int64
	createdAtUnixMs int64
}

// servingSnapshotRegistry is the in-memory banked-serving-snapshot inventory, keyed by
// snapshot_ref. Seeded from disk on start (workload may be empty for a disk-only entry
// until the control plane rebinds by adoption; the pinned IP is recovered from the
// bundle metadata) and updated on bank/evict.
type servingSnapshotRegistry struct {
	mu    sync.Mutex
	snaps map[string]*servingSnapshotEntry
}

func newServingSnapshotRegistry() *servingSnapshotRegistry {
	return &servingSnapshotRegistry{snaps: make(map[string]*servingSnapshotEntry)}
}

// add records a banked serving snapshot (from a bank or a startup rescan).
func (r *servingSnapshotRegistry) add(e servingSnapshotEntry) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.snaps[e.snapshotRef] = &servingSnapshotEntry{
		snapshotRef:     e.snapshotRef,
		workload:        e.workload,
		ip:              e.ip,
		sizeBytes:       e.sizeBytes,
		createdAtUnixMs: e.createdAtUnixMs,
	}
}

// get returns a copy of the banked snapshot entry for a ref.
func (r *servingSnapshotRegistry) get(ref string) (servingSnapshotEntry, bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	e, ok := r.snaps[ref]
	if !ok {
		return servingSnapshotEntry{}, false
	}
	return *e, true
}

// remove deletes a snapshot_ref from the inventory (after EvictSnapshot). Idempotent.
func (r *servingSnapshotRegistry) remove(ref string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	delete(r.snaps, ref)
}

// snapshot returns a copy of every banked serving snapshot entry, for NodeStatus.
func (r *servingSnapshotRegistry) snapshot() []servingSnapshotEntry {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make([]servingSnapshotEntry, 0, len(r.snaps))
	for _, e := range r.snaps {
		out = append(out, *e)
	}
	return out
}
