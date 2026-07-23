package server

import (
	"context"
	"net"
	"sync"

	nodev1 "github.com/jomcgi/homelab/projects/embervm/proto/embervm/node/v1"

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
	// EnsureNetwork creates the bridge and installs the serving forward nftables posture
	// (idempotent), called once on daemon start.
	EnsureNetwork(ctx context.Context) error
	// AllocateTap allocates the next free IP and creates its tap; used by fresh start.
	AllocateTap(ctx context.Context) (tap string, ip net.IP, err error)
	// AllocateTapForIP re-acquires a SPECIFIC IP and creates its tap; used by relight
	// to pin the IP the banked snapshot recorded (D-R3.4.1).
	AllocateTapForIP(ctx context.Context, ip net.IP) (tap string, err error)
	// ReleaseTap deletes a VM's tap and frees its IP (teardown); it also drops the VM's
	// DNAT rule (RemoveDNAT is folded in), so every teardown path cleans up.
	ReleaseTap(ctx context.Context, ip net.IP)
	// EnsureDNAT installs the prerouting DNAT rule exposing a ready serving VM's tap as
	// noded's routable pod IP + a per-VM port (D-R3.11.4), called after readiness. A
	// no-op when DNAT is disabled (empty pod IP).
	EnsureDNAT(ctx context.Context, ip net.IP, guestPort uint32) error
	// Endpoint projects a VM's (tap IP, guest port) into the endpoint the daemon
	// REPORTS: (pod IP, DNAT port) when DNAT is enabled, else the tap IP unchanged. It
	// is a projection only; the registry keeps storing the tap IP (probe target, pin).
	Endpoint(ip net.IP, guestPort uint32) (string, uint32)
	// GatewayIP is the bridge IP a guest uses as its default route.
	GatewayIP() net.IP
	// PrefixLen is the serving CIDR prefix length, for the guest static IP mask.
	PrefixLen() int
	// CIDR reports the serving subnet CIDR for NodeStatus.serving_subnet_cidr.
	CIDR() string
	// AvailableTaps reports how many tap IPs are currently free (the IP
	// allocator's freelist size), for the node-side tap-pressure predicate (ADR
	// embervm/014 decision 3). An O(1) counter read: a zero count is the
	// `pressure:taps` cheap rejection. Never does netlink work.
	AvailableTaps() int
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
	// handlerDiskPath/handlerZipBytes, when set, attach the per-workload zip handler
	// artifact as a second read-only drive and tell the guest to import it before
	// serving (D-R3.11.2, the zip lane). Empty/zero for an image-lane serving cold
	// boot (whose handler is baked into the rootfs), so that path is unchanged.
	ClaimServing(ctx context.Context, rootfsPath, harnessInit string, vcpus, memMib int, nic substrate.NICSpec, handlerDiskPath string, handlerZipBytes int64) (substrate.Handle, error)
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
	// WriteServingHandlerArtifact persists the verified zip bytes for a serving base
	// as a cold-boot-readable handler artifact under the base bundle (bases/<baseKey>/
	// handler.zip) plus a runtime-ref sidecar (bases/<baseKey>/runtime.ref) and returns
	// its host path and exact byte length (D-R3.11.2). runtimeImageRef names the runtime
	// image whose rootfs is drive 1, recorded so a startup rescan resolves it without the
	// control plane. The driver owns the base-bundle disk layout, so noded goes through it
	// rather than composing the path itself. Idempotent: a repeat write overwrites in place.
	// The exact length is returned so noded reports it and the guest reads only the
	// payload, not the block device's sector padding.
	WriteServingHandlerArtifact(baseKey, runtimeImageRef string, zip []byte) (path string, sizeBytes int64, err error)
	// ServingHandlerArtifactPath returns the host path of a base's handler artifact
	// and whether it exists on disk. Used by the startup rescan to re-discover serving
	// images and by startServingFresh to resolve the drive to attach.
	ServingHandlerArtifactPath(baseKey string) (path string, ok bool)
	// ScanServingHandlerArtifacts globs the base bundles for handler artifacts on
	// startup and returns each base key that has one, so the daemon re-seeds its
	// serving-images inventory after a restart (mirroring the banked-snapshot rescan).
	ScanServingHandlerArtifacts() []substrate.ServingHandlerArtifact
}

// ---- serving-images inventory (cold-boot handler artifacts) ----------------

// servingImageEntry is one built cold-boot handler artifact for a serving base:
// the handler-disk path a serving cold boot attaches as /dev/vdb, the runtime image
// ref whose rootfs is drive 1, and the exact zip length conveyed to the guest so it
// reads past no sector padding (D-R3.11.2). Keyed by the base key (== the serving
// image ref the control plane places on).
type servingImageEntry struct {
	baseKey         string
	workload        string
	handlerPath     string
	runtimeImageRef string
	sizeBytes       int64
}

// servingImageRegistry is the daemon's inventory of built serving images (cold-boot
// handler artifacts), keyed by base key. Populated by BuildBase for a serving base and
// re-seeded from disk on startup, it is what startServingFresh resolves a serving image
// ref against (NOT the static runtime-rootfs image table), and what NodeStatus reports
// as WorkloadCapacity.serving_image_ref so serving placement can cold-boot it. Kept
// DISTINCT from baseRegistry (the vsock-only base memory snapshot) so the two refs never
// overload one field.
type servingImageRegistry struct {
	mu     sync.Mutex
	images map[string]*servingImageEntry
}

func newServingImageRegistry() *servingImageRegistry {
	return &servingImageRegistry{images: make(map[string]*servingImageEntry)}
}

// add records a built serving image (from a BuildBase or a startup rescan). Idempotent.
func (r *servingImageRegistry) add(e servingImageEntry) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.images[e.baseKey] = &servingImageEntry{
		baseKey:         e.baseKey,
		workload:        e.workload,
		handlerPath:     e.handlerPath,
		runtimeImageRef: e.runtimeImageRef,
		sizeBytes:       e.sizeBytes,
	}
}

// get returns a copy of the serving image entry for a base key.
func (r *servingImageRegistry) get(baseKey string) (servingImageEntry, bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	e, ok := r.images[baseKey]
	if !ok {
		return servingImageEntry{}, false
	}
	return *e, true
}

// snapshot returns a copy of every built serving image, for NodeStatus projection.
func (r *servingImageRegistry) snapshot() []servingImageEntry {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make([]servingImageEntry, 0, len(r.images))
	for _, e := range r.images {
		out = append(out, *e)
	}
	return out
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
	origin   nodev1.InstanceOrigin
	tap      string
	// snapshotRef is the serving snapshot this VM was RELIT from, or "" for a fresh
	// cold boot (which has no source snapshot). It correlates a live serving VM to the
	// banked ref it depends on, so EvictSnapshot can refuse to delete a bundle out from
	// under a live relit VM (mirrors the session in-use guard). A banked-then-relit VM
	// keeps its source ref banked until the next StopServing(BANK) supersedes it.
	snapshotRef string
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

// firstByWorkload returns any live serving VM for workload. The activator uses
// this to serve Envoy stragglers that arrive after another path made the VM live.
func (r *servingRegistry) firstByWorkload(workload string) (*servingEntry, bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	for _, e := range r.vms {
		if e.workload == workload {
			return e, true
		}
	}
	return nil, false
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
	snapshotRef     string
	origin          nodev1.InstanceOrigin
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
			origin:   e.origin,
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

// snapshotWithRefs returns every live serving VM's vmID and the source snapshotRef it
// was relit from, for the EvictSnapshot in-use guard (refusing to evict a bundle a
// live VM was relit from). Mirrors the session registry's snapshotWithRefs.
func (r *servingRegistry) snapshotWithRefs() []servingView {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make([]servingView, 0, len(r.vms))
	for _, e := range r.vms {
		out = append(out, servingView{vmID: e.vmID, snapshotRef: e.snapshotRef})
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

// freshestByWorkload returns the most recently banked serving snapshot for a
// workload. A lexical ref tie-break makes equal timestamps deterministic.
func (r *servingSnapshotRegistry) freshestByWorkload(workload string) (servingSnapshotEntry, bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	var best *servingSnapshotEntry
	for _, e := range r.snaps {
		if e.workload != workload {
			continue
		}
		if best == nil || e.createdAtUnixMs > best.createdAtUnixMs || (e.createdAtUnixMs == best.createdAtUnixMs && e.snapshotRef > best.snapshotRef) {
			best = e
		}
	}
	if best == nil {
		return servingSnapshotEntry{}, false
	}
	return *best, true
}
