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
	// without reserving the IP (read-only; Task 5 uses the reserving variant).
	MemberAddressingFor(groupInstanceID, memberName string, memberIndex uint32) (tap, mac string, ip net.IP, err error)
	// EntryEndpoint projects a group entry member's (tap IP, guest port) into the
	// reported endpoint (pod IP + DNAT port when enabled, else the tap unchanged).
	EntryEndpoint(entryIP net.IP, guestPort uint32) (string, uint32)
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
	ip              net.IP
	// isEntry marks the group's entry member (the one exposed via the entry DNAT).
	isEntry bool
	// probe is the running TCP-connect health-probe loop for this member (set by
	// Task 5). nil in Task 4 (no live members yet).
	probe *serving.ProbeHandle

	mu       sync.Mutex // guards inFlight (Task 5's stop serialization)
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

// count is the number of live member VMs (for NodeStatus live_vms summing once
// Task 5 lands; 0 in Task 4).
func (r *groupMemberRegistry) count() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return len(r.members)
}
