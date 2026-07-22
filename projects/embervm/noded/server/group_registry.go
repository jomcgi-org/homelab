package server

import (
	"context"
	"net"
	"sync"

	"github.com/jomcgi/homelab/projects/embervm/noded/serving"
	"github.com/jomcgi/homelab/projects/embervm/noded/substrate"
)

// groupNetwork is the host composite-group networking seam the server depends on:
// per-group bridge create/teardown, deterministic member addressing, inter-group
// isolation, and the entry-member DNAT. The real *serving.GroupManager satisfies
// it; tests inject a fake. It is a SEPARATE seam from servingNetwork (a group owns
// one bridge PER INSTANCE, not a single shared bridge) so a Server built without
// group support (older tests) still compiles: a nil groupNet leaves
// CreateGroupNetwork/DeleteGroupNetwork returning Unimplemented and
// NodeStatus.group_networks empty.
type groupNetwork interface {
	// EnsureNetwork enables IPv4 forwarding and applies the (initially empty, or
	// post-adoption) group nftables posture once on daemon start.
	EnsureNetwork(ctx context.Context) error
	// CreateGroupNetwork validates the /24 (within the supernet, non-overlapping),
	// creates the per-group bridge, installs the inter-group isolation, and returns
	// (bridge, gateway). Idempotent per group_instance_id.
	CreateGroupNetwork(ctx context.Context, groupInstanceID, cidr string) (bridge, gatewayIP string, err error)
	// DeleteGroupNetwork tears down a group's bridge and drops it from the ruleset.
	// The attached-member refusal is enforced by the CALLER before this runs.
	DeleteGroupNetwork(ctx context.Context, groupInstanceID string) error
	// AdoptGroupNetwork re-seeds one group from an on-disk record on boot, without
	// touching the bridge (EnsureNetwork rebuilds the table once afterwards).
	AdoptGroupNetwork(groupInstanceID, cidr string, createdAtUnixMs int64) error
	// Has reports whether a group network is currently held.
	Has(groupInstanceID string) bool
	// List projects every held group network for NodeStatus assembly.
	List() []serving.GroupNetworkInfo
	// MemberAddressingFor derives the deterministic (tap, mac, ip) for a member
	// without reserving the IP (read-only; the reserving/creating variant is
	// EnsureMemberTap).
	MemberAddressingFor(groupInstanceID, memberName string, memberIndex uint32) (tap, mac string, ip net.IP, err error)
	// EnsureMemberTap pins a member's NIC world on the group bridge (derive + verify
	// against the request IP, reserve the IP, create + attach the tap) and returns
	// the (tap, mac). Used by BOTH FRESH boot and RELIGHT resume: a relight recreates
	// the SAME tap/MAC/IP the member had at bank time (the pinned-world reconstruction).
	EnsureMemberTap(ctx context.Context, groupInstanceID, memberName string, memberIndex uint32, wantIP net.IP) (tap, mac string, err error)
	// RemoveMemberTap deletes a member's tap and releases its pinned IP (idempotent),
	// the symmetric teardown for EnsureMemberTap.
	RemoveMemberTap(ctx context.Context, groupInstanceID, tap string, ip net.IP)
	// GatewayIP is the group's .1 gateway (the member's default route); nil if absent.
	GatewayIP(groupInstanceID string) net.IP
	// PrefixLen is the group's /24 prefix length (24); 0 if the group is absent.
	PrefixLen(groupInstanceID string) int
	// EntryEndpoint projects a group entry member's (tap IP, guest port) into the
	// reported endpoint (pod IP + DNAT port when enabled, else the tap unchanged).
	EntryEndpoint(entryIP net.IP, guestPort uint32) (string, uint32)
	// EnsureEntryDNAT installs (or refreshes) the entry-member DNAT exposing the entry
	// member's tap as {pod IP, vmPort}. Called on a ready entry member so the endpoint
	// the control plane publishes actually routes to tap:guestPort. No-op when DNAT is
	// disabled (no pod IP); dropped with the group by DeleteGroupNetwork.
	EnsureEntryDNAT(ctx context.Context, groupInstanceID string, entryIP net.IP, guestPort uint32) error
}

// groupMemberDriver is the subset of the fcvm driver the R5 member lifecycle needs
// on top of vmDriver: cold-boot a member VM WITH a tap NIC on the group bridge and
// its first-boot env (FRESH), bank a live member to a self-contained bundle under
// group/<set_id>/<member_name>/ (BANK), resume from a banked member bundle (RELIGHT),
// remove a banked member bundle, and rescan banked group bundle SETS on start. The
// real *driver.Driver satisfies it; tests inject a fake. A separate seam (like
// statefulDriver) so a Server built without group support still compiles and a
// reviewer sees exactly which driver mechanics the member path reuses.
type groupMemberDriver interface {
	// ClaimGroupMember cold-boots a member VM from a per-member rootfs WITH the given
	// tap NIC on the group bridge and its first-boot env (MMDS-lite over boot-args).
	// No handler artifact and no writable volume: a member is a plain NIC guest.
	ClaimGroupMember(ctx context.Context, rootfsPath, harnessInit string, vcpus, memMib int, nic substrate.NICSpec, env map[string]string) (substrate.Handle, error)
	// SnapshotGroupMember pauses a live member VM and writes a self-contained member
	// bundle under group/<set_id>/<member_name>/; does not resume (the caller Releases).
	SnapshotGroupMember(ctx context.Context, h substrate.Handle, setID, memberName string) (substrate.SnapshotRef, error)
	// RestoreGroupMember launches a fresh VM from a banked member bundle and resumes
	// it, WITH the NIC captured at bank time. The caller has already recreated the
	// pinned tap world before calling this.
	RestoreGroupMember(ctx context.Context, setID, memberName string) (substrate.Handle, error)
	// RemoveGroupMemberBundle deletes a banked member bundle from disk (idempotent).
	RemoveGroupMemberBundle(setID, memberName string) error
	// ScanGroupBundleSets globs the group bundle dir on startup and returns each set
	// dir with the per-member bundles found under it, GROUPED BY set (no completeness
	// judgment; that is the control plane's).
	ScanGroupBundleSets() []substrate.GroupBundleSetInfo
	// GroupSetsDir is the directory holding banked group member bundles, rescanned
	// on start.
	GroupSetsDir() string
}

// groupClock is the host-side clock-resync seam the RELIGHT path uses to re-set a
// resumed member guest's wall clock over the port-1024 length-prefixed JSON agent
// channel and VERIFY the read-back within one second. The real groupclock.Resync
// (with a groupclock.VsockDialer) satisfies it; tests inject a fake scripting
// success / >1s failure / timeout without a real guest.
type groupClock interface {
	// Resync dials the member guest's clock agent, sends the host epoch, reads the
	// clock back, and returns an error (failing the relight) when the read-back is
	// more than one second off the host's epoch at send.
	Resync(ctx context.Context, udsPath string) error
}

// groupRecordStore is the on-disk group-network record seam: write a record on
// create, remove it on delete, and rescan records on boot. The real
// *driver.Driver satisfies it; tests inject a fake. Separate from groupNetwork so
// the durable-truth persistence is testable independently of the live networking.
type groupRecordStore interface {
	// WriteGroupNetworkRecord persists a group-network record atomically.
	WriteGroupNetworkRecord(rec substrate.GroupNetworkRecord) error
	// RemoveGroupNetworkRecord deletes a group's on-disk record (idempotent).
	RemoveGroupNetworkRecord(groupInstanceID string) error
	// ScanGroupNetworks returns every valid on-disk record for the boot rescan.
	ScanGroupNetworks() []substrate.GroupNetworkRecord
	// GroupNetworksDir is the directory holding the records, ensured on start.
	GroupNetworksDir() string
}

// groupMemberEntry is one LIVE composite-group member microVM the daemon
// supervises (R5). It is Task 5 that ADDS entries here (via StartGroupMember);
// Task 4 only needs the registry to EXIST and to answer memberCount (0 until Task
// 5) and the attached-member check for DeleteGroupNetwork. The shape mirrors
// statefulEntry so Task 5 can extend it with a probe, generation-free.
type groupMemberEntry struct {
	vmID            string
	groupInstanceID string
	memberName      string
	memberIndex     uint32
	ip              net.IP
	// tap is the member's host tap device on the group bridge (derived from the
	// group + member), kept so teardown deletes the exact device without re-deriving.
	tap string
	// port is the guest TCP port the member health-gates and probes on (health_port).
	port uint32
	// handle is the live Firecracker handle, for bank/destroy/reap.
	handle substrate.Handle
	// isEntry marks the group's entry member (the one exposed via the entry DNAT).
	isEntry bool
	// snapshotRef is the group member bundle this VM was RELIT from
	// (group/<set_id>/<member_name>), or "" for a fresh/cold boot (which has no
	// source snapshot). Mirrors statefulEntry.snapshotRef; it is the primary key
	// the per-member eviction in-use guard (memberInUse) matches on, so a live
	// relit member protects its bundle even when the banked-bundle entry lost its
	// group_instance_id to a pre-sidecar boot scan (#38 F3).
	snapshotRef string
	// probe is the running TCP-connect health-probe loop for this member.
	probe *serving.ProbeHandle

	mu       sync.Mutex // guards inFlight (the per-vm stop serialization)
	inFlight bool
}

// groupMemberRegistry is the daemon's inventory of LIVE composite-group member
// microVMs, keyed by the opaque vm_id. Kept DISTINCT from every other class
// registry so a member VM is never reported in primed_vm_ids, session_vms,
// serving_vms, or stateful_vms (a member VM is never in the task pool), while
// still counting against the node live-VM cap via the shared driver's LiveCount.
// In Task 4 it is created empty and only READ (memberCount, attached-member
// check); Task 5 fills it.
type groupMemberRegistry struct {
	mu      sync.Mutex
	members map[string]*groupMemberEntry
}

func newGroupMemberRegistry() *groupMemberRegistry {
	return &groupMemberRegistry{members: make(map[string]*groupMemberEntry)}
}

// add registers a live member VM (Task 5).
func (r *groupMemberRegistry) add(e *groupMemberEntry) {
	r.mu.Lock()
	r.members[e.vmID] = e
	r.mu.Unlock()
}

// remove deletes a vm_id and returns its entry (nil if absent).
func (r *groupMemberRegistry) remove(id string) *groupMemberEntry {
	r.mu.Lock()
	defer r.mu.Unlock()
	e := r.members[id]
	delete(r.members, id)
	return e
}

// get returns the entry for a vm_id (nil if absent).
func (r *groupMemberRegistry) get(id string) *groupMemberEntry {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.members[id]
}

// beginStop marks a member VM busy for a StopGroupMember, mirroring
// statefulRegistry.beginStop. It returns (entry, true) only when the id names a
// known live member with no stop already in flight; a concurrent stop on the same
// vm_id gets (nil, false), which the handler maps to FAILED_PRECONDITION. This is
// the concurrent-stop refusal per vm_id.
func (r *groupMemberRegistry) beginStop(id string) (*groupMemberEntry, bool) {
	r.mu.Lock()
	e, ok := r.members[id]
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

// memberCount is the number of live members currently attached to a group's
// bridge, read by nodeStatus (per-group member_count) and by the
// DeleteGroupNetwork attached-member refusal.
func (r *groupMemberRegistry) memberCount(groupInstanceID string) int {
	r.mu.Lock()
	defer r.mu.Unlock()
	n := 0
	for _, e := range r.members {
		if e.groupInstanceID == groupInstanceID {
			n++
		}
	}
	return n
}

// hasMembers reports whether any live member is attached to a group (the
// DeleteGroupNetwork backstop).
func (r *groupMemberRegistry) hasMembers(groupInstanceID string) bool {
	return r.memberCount(groupInstanceID) > 0
}

// memberInUse reports whether a LIVE member VM is currently attached that was
// relit from the given bundle, i.e. removing the bundle would lose the state a
// running member needs to re-bank. It is the in-use guard for a per-member local
// group eviction (#38). It matches REF-FIRST on the live member's snapshotRef
// (set at relight, mirroring the stateful guard), which is authoritative and
// survives a boot scan that lost the bundle's group_instance_id (F3); it falls
// back to matching (groupInstanceID, memberName) so a member relit before this
// snapshotRef plumbing shipped, or whose bundle entry still carries a gid, is
// still protected. An empty snapshotRef (fresh/cold member) never ref-matches.
// Returns the live member's vm_id.
func (r *groupMemberRegistry) memberInUse(ref, groupInstanceID, memberName string) (string, bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	for id, e := range r.members {
		if ref != "" && e.snapshotRef == ref {
			return id, true
		}
	}
	for id, e := range r.members {
		if groupInstanceID != "" && e.groupInstanceID == groupInstanceID && e.memberName == memberName {
			return id, true
		}
	}
	return "", false
}

// groupMemberView is a lock-free, read-only projection of a groupMemberEntry, for
// NodeStatus.group_member_vms (Task 5 populates the health verdict).
type groupMemberView struct {
	vmID            string
	groupInstanceID string
	memberName      string
	ip              string
	healthy         bool
	lastProbeUnixMs int64
}

// snapshot returns a copy of every live member VM, for NodeStatus.group_member_vms.
// Empty in Task 4; Task 5's live members flow through here.
func (r *groupMemberRegistry) snapshot() []groupMemberView {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make([]groupMemberView, 0, len(r.members))
	for _, e := range r.members {
		v := groupMemberView{
			vmID:            e.vmID,
			groupInstanceID: e.groupInstanceID,
			memberName:      e.memberName,
			ip:              e.ip.String(),
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

// count is the number of live member VMs (for NodeStatus live_vms summing).
func (r *groupMemberRegistry) count() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return len(r.members)
}

// ---- banked group bundle inventory ------------------------------------------

// groupBundleEntry is one member's banked snapshot within a group bundle set on
// disk (R5), stored under group/<set_id>/<member_name>/. Seeded from disk on start
// (ScanGroupBundleSets) and updated on a member bank. The daemon reports these
// GROUPED BY set_id and makes NO completeness judgment (the control plane decides
// whether a set has every member it needs to relight).
type groupBundleEntry struct {
	setID           string
	memberName      string
	groupInstanceID string
	snapshotRef     string
	sizeBytes       int64
	createdAtUnixMs int64
}

// groupBundleRegistry is the in-memory banked-group-bundle inventory, keyed by the
// opaque per-member snapshot_ref (group/<set_id>/<member_name>). Seeded from disk on
// start and updated on bank; the NodeStatus projection groups entries by set_id.
type groupBundleRegistry struct {
	mu    sync.Mutex
	snaps map[string]*groupBundleEntry
}

func newGroupBundleRegistry() *groupBundleRegistry {
	return &groupBundleRegistry{snaps: make(map[string]*groupBundleEntry)}
}

// add records a banked group member bundle (from a bank or a startup rescan).
func (r *groupBundleRegistry) add(e groupBundleEntry) {
	r.mu.Lock()
	defer r.mu.Unlock()
	cp := e
	r.snaps[e.snapshotRef] = &cp
}

// get returns a copy of the banked group bundle entry for a member snapshot_ref
// (group/<set_id>/<member_name>), recovering the (set_id, member_name,
// group_instance_id) a per-member local eviction needs. Absent -> (_, false),
// which the caller treats as an idempotent already-gone success.
func (r *groupBundleRegistry) get(ref string) (groupBundleEntry, bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	e, ok := r.snaps[ref]
	if !ok {
		return groupBundleEntry{}, false
	}
	return *e, true
}

// remove deletes a snapshot_ref from the inventory (idempotent).
func (r *groupBundleRegistry) remove(ref string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	delete(r.snaps, ref)
}

// snapshot returns a copy of every banked group bundle entry.
func (r *groupBundleRegistry) snapshot() []groupBundleEntry {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make([]groupBundleEntry, 0, len(r.snaps))
	for _, e := range r.snaps {
		out = append(out, *e)
	}
	return out
}
