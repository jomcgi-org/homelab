package serving

// Group (R5, composite-workload) networking is the multi-member sibling of the
// serving network above. Where serving owns ONE per-node bridge shared by every
// serving VM, a composite group owns ONE bridge PER GROUP INSTANCE: the subnet is
// the group's identity fabric, so members address each other by deterministic,
// pinned IPs on their own /24 and no member of group A can reach group B's subnet
// or the serving bridge. This file adds:
//
//   - per-group bridge create/teardown (one bridge per group_instance_id),
//   - /24-in-supernet validation and overlap refusal against the live bridge set,
//   - deterministic member addressing (index -> .10+i IP, derived MAC, pinned tap
//     name per (group_instance_id, member_name)),
//   - the inter-group nftables isolation (composite<->composite BOTH directions,
//     composite->serving) in a SEPARATE table so it never collides with the
//     serving forward/serving_dnat chains,
//   - the entry-path DNAT for the entry member (the exact D-R3.11.4 pod-IP lane
//     the serving network uses, reused verbatim).
//
// Everything privileged is exec'd through the same Runner seam and every rule /
// argv batch is a pure function so it is table-tested without root, matching
// net.go's discipline.

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"net"
	"sort"
	"strings"
	"sync"
)

// nftGroupTable is the DEDICATED nftables table for the composite-group posture.
// It is SEPARATE from nftTable (embervm_serving) so the group forward-isolation
// and entry-DNAT chains can never collide with, flush, or be flushed by the
// serving forward / serving_dnat chains: the two tables are torn down and rebuilt
// independently. Both live in noded's own pod netns.
const nftGroupTable = "embervm_group"

// nftGroupForwardChain is the forward-hook chain that enforces inter-group
// isolation. It runs at the SAME forward hook as the serving table's forward
// chain; nftables evaluates every registered chain at a hook, and a drop in ANY
// chain drops the packet, so the group DROP rules compose with (never fight) the
// serving posture. The chain drops: any packet whose input iface is one group
// bridge and output iface is ANOTHER group bridge (composite<->composite, both
// directions fall out of the all-pairs rule set), and any packet from a group
// bridge out to the serving bridge (composite->serving). Established/related is
// accepted first so the entry-path DNAT return traffic is never caught.
const nftGroupForwardChain = "group_forward"

// nftGroupDNATChain is the prerouting nat chain that exposes each group's ENTRY
// member as noded's routable pod IP + a per-entry port, the exact D-R3.11.4 lane
// the serving_dnat chain uses (podIP:port -> tapIP:guestPort). Only the entry
// member of a group gets a rule here; non-entry members are bridge-internal only.
const nftGroupDNATChain = "group_dnat"

// groupMemberBaseOffset is the host offset of member index 0 within the group
// /24: member i is assigned .10 + i (the committed R5 addressing rule). The .1 is
// the bridge gateway and .2..9 are reserved headroom (future per-group infra: a
// DNS shim, a sidecar) so member addressing starts at a stable, legible .10.
const groupMemberBaseOffset = 10

// groupIfnameMax is the Linux network-interface name length limit. Bridge and tap
// names are hash-derived and truncated to stay strictly under it (see
// groupBridgeName / groupTapName), so a long opaque group_instance_id or
// member_name can never overflow the kernel's IFNAMSIZ-1 budget.
const groupIfnameMax = 15

// groupBridgeName derives the deterministic per-group bridge device name from the
// opaque group_instance_id: "emg" + the first 6 hex chars of sha256(id). That is
// 9 chars, well under the 15-char ifname limit, and collision-safe in practice
// (24 bits of hash over the handful of groups a node holds). Deterministic so a
// re-issued CreateGroupNetwork for the same group_instance_id resolves the SAME
// bridge (idempotency), and so teardown/adoption need only the id.
func groupBridgeName(groupInstanceID string) string {
	sum := sha256.Sum256([]byte(groupInstanceID))
	return "emg" + hex.EncodeToString(sum[:])[:6]
}

// groupTapName derives the deterministic member tap device name from the pair
// (group_instance_id, member_name): "emgt" + the first 6 hex chars of
// sha256(group_instance_id\x00member_name). That is 10 chars, under the 15-char
// ifname limit, and pinned per member so a relight re-creates the SAME tap. The
// group id is folded in so two groups' identically-named members ("worker-0" in
// group A and in group B) never derive the same tap name.
func groupTapName(groupInstanceID, memberName string) string {
	sum := sha256.Sum256([]byte(groupInstanceID + "\x00" + memberName))
	return "emgt" + hex.EncodeToString(sum[:])[:6]
}

// groupMemberMAC derives a deterministic, locally-administered unicast MAC for a
// member from the pair (group_instance_id, member_name): the first octet is
// 0x02 (locally-administered bit set, multicast bit clear) and the remaining
// five octets are the leading bytes of sha256(group_instance_id\x00member_name).
// Deterministic so a relight (which keeps the guest's baked NIC) and a fresh boot
// agree, and folding the group id in keeps two groups' same-named members
// distinct. Returned as the canonical colon-separated lower-case string.
func groupMemberMAC(groupInstanceID, memberName string) string {
	sum := sha256.Sum256([]byte(groupInstanceID + "\x00" + memberName))
	return fmt.Sprintf("02:%02x:%02x:%02x:%02x:%02x", sum[0], sum[1], sum[2], sum[3], sum[4])
}

// groupMemberIP returns the pinned host IP for a member INDEX within a group /24:
// network base + (groupMemberBaseOffset + index). It errors when the resulting
// address falls outside the network's usable host range (a member index past the
// subnet, or a subnet too small), so a bad index fails at StartGroupMember rather
// than configuring a bogus tap. Pure and recomputable anywhere (a relight
// re-derives the same IP from the same index).
func groupMemberIP(network *net.IPNet, index uint32) (net.IP, error) {
	if network == nil {
		return nil, fmt.Errorf("serving: nil group network")
	}
	base := network.IP.To4()
	if base == nil {
		return nil, fmt.Errorf("serving: group network %v is not IPv4", network)
	}
	offset := uint64(groupMemberBaseOffset) + uint64(index)
	ip := addOffset(base, offset)
	if !network.Contains(ip) {
		return nil, fmt.Errorf("serving: group member index %d (host offset %d) is outside network %v", index, offset, network)
	}
	// The broadcast address (all host bits set) is never a usable member IP.
	if ip.Equal(broadcastIP(network)) {
		return nil, fmt.Errorf("serving: group member index %d resolves to the broadcast address of %v", index, network)
	}
	return ip, nil
}

// addOffset returns base + offset as a 4-byte IPv4 (big-endian add). Offsets are
// small (member index within a /24), so a carry never escapes the 32-bit space
// for any real group subnet.
func addOffset(base net.IP, offset uint64) net.IP {
	v4 := cloneIP(base)
	val := uint64(v4[0])<<24 | uint64(v4[1])<<16 | uint64(v4[2])<<8 | uint64(v4[3])
	val += offset
	return net.IP{byte(val >> 24), byte(val >> 16), byte(val >> 8), byte(val)}
}

// groupBridgeSetupArgs returns the ordered argv batches that create one group
// bridge, assign it the group gateway IP (the .1 of the group /24), and bring it
// up. It is the group counterpart of bridgeSetupArgs, kept as its own pure
// function so a reviewer sees the group bridge lifecycle explicitly; the caller
// execs each batch in order and treats "already exists" as idempotent.
func groupBridgeSetupArgs(bridge, gatewayIP string, prefixLen int) [][]string {
	return [][]string{
		{"ip", "link", "add", "name", bridge, "type", "bridge"},
		{"ip", "addr", "add", fmt.Sprintf("%s/%d", gatewayIP, prefixLen), "dev", bridge},
		{"ip", "link", "set", bridge, "up"},
	}
}

// groupBridgeTeardownArgs returns the argv to delete a group bridge device.
// Deleting the bridge detaches (and orphans) any tap still mastered by it, but
// the delete path only runs after the member-attached refusal, so a live member
// never has its bridge yanked. Pure for table testing.
func groupBridgeTeardownArgs(bridge string) []string {
	return []string{"ip", "link", "del", bridge}
}

// groupEntry is one group's ENTRY-member DNAT projection: the entry member's tap
// IP + guest port, and the deterministic per-entry port on noded's pod IP that
// maps to it. Only the entry member of a group is exposed on the pod IP; every
// other member is reachable only from inside the group bridge. vmPort is
// precomputed and stored so the ruleset generator stays a pure function of the
// entry set and the entries sort deterministically by vmPort.
type groupEntry struct {
	tapIP     string
	guestPort uint32
	vmPort    uint32
}

// nftGroupRuleset returns the `nft -f -` ruleset text for the composite-group
// posture as a self-contained, idempotent script over the DEDICATED
// embervm_group table (flush-then-define of ONLY that table, so it never touches
// embervm_serving or any other host firewall state). It installs, given the FULL
// current set of group bridges and the entry-member DNAT set:
//
//   - a group_forward filter chain that accepts established/related (so entry-path
//     return traffic and reply packets flow), then DROPS every ordered pair of
//     distinct group bridges (bridge A in, bridge B out): this is the
//     composite<->composite isolation, and enumerating BOTH ordered pairs covers
//     both directions. It also DROPS every group-bridge-in / serving-bridge-out
//     pair (composite->serving). Members thus have NO L3 egress beyond their own
//     group bridge plus the entry path (task-class zero-egress posture at L3);
//   - when podIP is set (the deployed D-R3.11.4 posture), a group_dnat prerouting
//     nat chain with one rule per group's ENTRY member rewriting podIP:vmPort ->
//     entryTapIP:guestPort, the exact serving_dnat lane reused. When podIP is
//     empty (local/test) no DNAT chain is emitted.
//
// It is a pure function of (bridges, servingBridge, podIP, entries) so the exact
// ruleset is asserted as data. bridges is sorted internally so the generated
// script is deterministic regardless of caller map order.
func nftGroupRuleset(bridges []string, servingBridge, podIP string, entries []groupEntry) string {
	sortedBridges := append([]string(nil), bridges...)
	sort.Strings(sortedBridges)

	var b strings.Builder
	fmt.Fprintf(&b, "add table inet %s\n", nftGroupTable)
	fmt.Fprintf(&b, "flush table inet %s\n", nftGroupTable)
	fmt.Fprintf(&b, "add chain inet %s %s { type filter hook forward priority 0; policy accept; }\n", nftGroupTable, nftGroupForwardChain)
	// Established/related return traffic is always allowed (entry-path replies and
	// any reply to an accepted flow), evaluated before the drops so isolation only
	// ever bites NEW cross-boundary forwarding.
	fmt.Fprintf(&b, "add rule inet %s %s ct state established,related accept\n", nftGroupTable, nftGroupForwardChain)
	// composite<->composite: drop every ordered pair of DISTINCT group bridges.
	// Enumerating ordered pairs (A->B and B->A both appear) covers both directions
	// with a single, deterministic loop.
	for _, in := range sortedBridges {
		for _, out := range sortedBridges {
			if in == out {
				continue
			}
			fmt.Fprintf(&b, "add rule inet %s %s iifname \"%s\" oifname \"%s\" drop\n", nftGroupTable, nftGroupForwardChain, in, out)
		}
	}
	// composite->serving: drop any packet from a group bridge out to the serving
	// bridge (members must never reach the serving subnet). Only emitted when a
	// serving bridge name is configured.
	if servingBridge != "" {
		for _, in := range sortedBridges {
			fmt.Fprintf(&b, "add rule inet %s %s iifname \"%s\" oifname \"%s\" drop\n", nftGroupTable, nftGroupForwardChain, in, servingBridge)
		}
	}
	if podIP != "" {
		fmt.Fprintf(&b, "add chain inet %s %s { type nat hook prerouting priority dstnat; policy accept; }\n", nftGroupTable, nftGroupDNATChain)
		sortedEntries := append([]groupEntry(nil), entries...)
		sort.Slice(sortedEntries, func(i, j int) bool { return sortedEntries[i].vmPort < sortedEntries[j].vmPort })
		for _, e := range sortedEntries {
			fmt.Fprintf(&b, "add rule inet %s %s ip daddr %s tcp dport %d dnat ip to %s:%d\n",
				nftGroupTable, nftGroupDNATChain, podIP, e.vmPort, e.tapIP, e.guestPort)
		}
	}
	return b.String()
}

// nftGroupTeardownArgs returns the argv to remove the dedicated group table
// entirely (scoped: only our table). Provided for teardown symmetry with
// nftTeardownArgs; `nft delete table` errors on an absent table, so a caller must
// tolerate that. The GroupManager does not use it in the happy path (it rebuilds
// the whole table on every create/delete), but it names the scoped-teardown
// contract. Pure for table testing.
func nftGroupTeardownArgs() []string {
	return []string{"nft", "delete", "table", "inet", nftGroupTable}
}

// groupNet is one live per-group network the GroupManager holds: its bridge, its
// /24, the gateway (.1), a per-group IP allocator (member IPs are index-pinned,
// so the allocator is used only to reserve/track, mirroring the serving pin
// path), and the entry-member DNAT entry (at most one per group). It is created
// by CreateGroupNetwork and removed by DeleteGroupNetwork.
type groupNet struct {
	groupInstanceID string
	bridge          string
	cidr            *net.IPNet
	gatewayIP       net.IP
	prefixLen       int
	alloc           *ipAllocator
	createdAtUnixMs int64
	// entry is the entry-member DNAT projection (podIP:vmPort -> tapIP:guestPort),
	// set once the entry member is live and its endpoint is DNAT'd. nil until then.
	entry *groupEntry
}

// GroupNetworkInfo is a read-only projection of one live group network, for the
// server's NodeStatus.group_networks assembly and for adoption. member_count is
// filled by the caller from the live member registry (0 until Task 5 populates
// live members), so this struct carries only what the manager owns.
type GroupNetworkInfo struct {
	GroupInstanceID string
	Bridge          string
	CIDR            string
	GatewayIP       string
	CreatedAtUnixMs int64
}

// GroupManager owns the host composite-group networking: the per-group bridges,
// their /24 allocations within the values-declared composite supernet, the
// inter-group nftables isolation, and the entry-member DNAT. It is the group
// analogue of the serving Manager, but keyed by group_instance_id (a registry of
// per-group networks) rather than a single shared bridge. It is safe for
// concurrent use: a single mutex guards the group map AND serializes the nft
// apply (the whole embervm_group table is regenerated from the current group set
// on every create/delete), so two concurrent CreateGroupNetwork/DeleteGroupNetwork
// calls cannot interleave ruleset writes.
type GroupManager struct {
	runner        Runner
	supernet      *net.IPNet
	servingBridge string
	// podIP is noded's routable pod IP for the entry-member DNAT projection; empty
	// disables entry DNAT (reports the tap IP) exactly as the serving Manager does.
	podIP string
	// portBase is the base of the deterministic per-entry DNAT port space (shared
	// derivation with serving: vmPort = portBase + hostOffset within the composite
	// supernet). Each group's entry IP is a distinct address in the supernet, so
	// the derived ports do not collide across groups.
	portBase int

	mu     sync.Mutex
	groups map[string]*groupNet
}

// NewGroupManager builds a GroupManager over the composite supernet CIDR. A
// malformed supernet is an error so a misconfiguration fails loudly at startup.
// servingBridge is the serving-class bridge name the isolation rules deny
// composite->serving toward (empty means no serving bridge is configured and that
// rule is omitted). podIP/portBase parameterise the entry-member DNAT exactly as
// the serving Manager: empty podIP disables it (local/test).
func NewGroupManager(runner Runner, supernetCIDR, servingBridge, podIP string, portBase int) (*GroupManager, error) {
	if runner == nil {
		runner = ExecRunner{}
	}
	_, supernet, err := net.ParseCIDR(supernetCIDR)
	if err != nil {
		return nil, fmt.Errorf("serving: invalid composite supernet %q: %w", supernetCIDR, err)
	}
	if supernet.IP.To4() == nil {
		return nil, fmt.Errorf("serving: composite supernet %q is not IPv4", supernetCIDR)
	}
	return &GroupManager{
		runner:        runner,
		supernet:      supernet,
		servingBridge: servingBridge,
		podIP:         podIP,
		portBase:      portBase,
		groups:        make(map[string]*groupNet),
	}, nil
}

// Supernet reports the composite supernet CIDR string (for logging / diagnostics).
func (m *GroupManager) Supernet() string {
	if m == nil || m.supernet == nil {
		return ""
	}
	return m.supernet.String()
}

// validateGroupCIDR parses cidr and returns its *net.IPNet after checking it is a
// /24 wholly inside the composite supernet. The control plane assigns the /24; the
// daemon VALIDATES it. A non-/24, a non-IPv4, or a range not contained by the
// supernet is an error the caller maps to FAILED_PRECONDITION per the proto.
func (m *GroupManager) validateGroupCIDR(cidr string) (*net.IPNet, error) {
	_, ipnet, err := net.ParseCIDR(cidr)
	if err != nil {
		return nil, fmt.Errorf("serving: invalid group cidr %q: %w", cidr, err)
	}
	base := ipnet.IP.To4()
	if base == nil {
		return nil, fmt.Errorf("serving: group cidr %q is not IPv4", cidr)
	}
	ones, bits := ipnet.Mask.Size()
	if ones != 24 || bits != 32 {
		return nil, fmt.Errorf("serving: group cidr %q must be a /24", cidr)
	}
	// Containment: both the network address and the broadcast address of the /24
	// must fall inside the supernet (a /24 straddling the supernet edge is refused).
	if !m.supernet.Contains(base) || !m.supernet.Contains(broadcastIP(ipnet)) {
		return nil, fmt.Errorf("serving: group cidr %q is not within composite supernet %v", cidr, m.supernet)
	}
	return ipnet, nil
}

// CreateGroupNetwork validates cidr (a /24 within the supernet, non-overlapping
// with an existing group bridge), creates the per-group bridge, and installs the
// inter-group isolation + entry-DNAT scaffolding by regenerating the whole
// embervm_group table. It is IDEMPOTENT per group_instance_id: a re-issue for a
// group that already exists with the SAME cidr returns the existing
// (bridge, gateway) without touching the bridge (so the control plane can safely
// re-issue before a relight or an adoption-time rebind). A re-issue with a
// DIFFERENT cidr, or a cidr that overlaps a DIFFERENT group's /24, is refused
// (overlapErr, which the caller maps to FAILED_PRECONDITION). It returns the
// bridge name and the group gateway IP.
func (m *GroupManager) CreateGroupNetwork(ctx context.Context, groupInstanceID, cidr string) (bridge, gatewayIP string, err error) {
	if groupInstanceID == "" {
		return "", "", fmt.Errorf("serving: group_instance_id required")
	}
	ipnet, err := m.validateGroupCIDR(cidr)
	if err != nil {
		return "", "", err
	}

	m.mu.Lock()
	defer m.mu.Unlock()

	// Idempotency: an existing group with the SAME /24 is a no-op hit. A different
	// /24 for the same id is a conflict (the control plane must delete first).
	if existing, ok := m.groups[groupInstanceID]; ok {
		if existing.cidr.String() != ipnet.String() {
			return "", "", fmt.Errorf("serving: group %q already exists with cidr %v, cannot recreate as %v", groupInstanceID, existing.cidr, ipnet)
		}
		return existing.bridge, existing.gatewayIP.String(), nil
	}

	// Overlap refusal: a NEW group's /24 must not overlap any OTHER live group's
	// /24 (two bridges cannot own overlapping address space on the same node).
	for id, g := range m.groups {
		if id == groupInstanceID {
			continue
		}
		if cidrOverlaps(ipnet, g.cidr) {
			return "", "", fmt.Errorf("serving: group cidr %v overlaps existing group %q cidr %v", ipnet, id, g.cidr)
		}
	}

	gwIP := nextIP(ipnet.IP.To4()) // network + 1 == the .1 gateway
	prefixLen, _ := ipnet.Mask.Size()
	bridgeName := groupBridgeName(groupInstanceID)

	// Create the bridge idempotently (tolerate an already-present device on a
	// re-entry after a partial prior create).
	for _, argv := range groupBridgeSetupArgs(bridgeName, gwIP.String(), prefixLen) {
		if _, rerr := m.runner.Run(ctx, argv[0], argv[1:]...); rerr != nil {
			if isAlreadyExists(rerr) {
				continue
			}
			// Roll back the bridge fragment so a failed create leaks no half-bridge.
			_, _ = m.runner.Run(ctx, "ip", groupBridgeTeardownArgs(bridgeName)[1:]...)
			return "", "", rerr
		}
	}

	g := &groupNet{
		groupInstanceID: groupInstanceID,
		bridge:          bridgeName,
		cidr:            ipnet,
		gatewayIP:       gwIP,
		prefixLen:       prefixLen,
		alloc:           newIPAllocator(ipnet, gwIP),
	}
	m.groups[groupInstanceID] = g

	// Regenerate the whole group table so the new bridge is covered by the
	// isolation rules immediately. On failure, undo the map insert and the bridge.
	if aerr := m.applyRulesetLocked(ctx); aerr != nil {
		delete(m.groups, groupInstanceID)
		_, _ = m.runner.Run(ctx, "ip", groupBridgeTeardownArgs(bridgeName)[1:]...)
		return "", "", aerr
	}
	return bridgeName, gwIP.String(), nil
}

// AdoptGroupNetwork re-seeds one group into the in-memory registry from an
// on-disk record WITHOUT touching the bridge or the ruleset (a boot rescan
// already found the record; EnsureNetwork rebuilds the table once at the end). It
// is idempotent and validates the recorded cidr the same way CreateGroupNetwork
// does; a malformed record is skipped (returned as an error the caller logs). Used
// only by the daemon-start rescan path.
func (m *GroupManager) AdoptGroupNetwork(groupInstanceID, cidr string, createdAtUnixMs int64) error {
	ipnet, err := m.validateGroupCIDR(cidr)
	if err != nil {
		return err
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	if _, ok := m.groups[groupInstanceID]; ok {
		return nil
	}
	gwIP := nextIP(ipnet.IP.To4())
	prefixLen, _ := ipnet.Mask.Size()
	m.groups[groupInstanceID] = &groupNet{
		groupInstanceID: groupInstanceID,
		bridge:          groupBridgeName(groupInstanceID),
		cidr:            ipnet,
		gatewayIP:       gwIP,
		prefixLen:       prefixLen,
		alloc:           newIPAllocator(ipnet, gwIP),
		createdAtUnixMs: createdAtUnixMs,
	}
	return nil
}

// EnsureNetwork enables IPv4 forwarding and applies the group ruleset ONCE from
// the current (post-adoption) group set on daemon start, mirroring the serving
// Manager's EnsureNetwork. It does NOT create bridges: group bridges are created
// on CreateGroupNetwork (and die with the pod), so on a fresh start there are no
// group bridges to rebuild and the ruleset is the empty-group posture; after an
// adoption rescan it covers whatever groups the on-disk records re-seeded. The
// bridges those adopted records name no longer exist (they died with the prior
// pod), so the control plane re-issues CreateGroupNetwork to rebuild them; this
// only makes the table present and forwarding enabled.
func (m *GroupManager) EnsureNetwork(ctx context.Context) error {
	if _, err := m.runner.Run(ctx, "sysctl", "-w", "net.ipv4.ip_forward=1"); err != nil {
		return err
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.applyRulesetLocked(ctx)
}

// DeleteGroupNetwork tears down a group's bridge, drops it from the isolation
// ruleset (by regenerating the table without it), and removes its in-memory
// record. The caller (the server handler) enforces the ATTACHED-MEMBER refusal
// BEFORE calling this: a group with a live member on its bridge must not be
// deleted (FAILED_PRECONDITION), because deleting the bridge would yank the live
// member's NIC. It is idempotent: deleting an unknown group is a no-op success
// (the desired end-state already holds).
func (m *GroupManager) DeleteGroupNetwork(ctx context.Context, groupInstanceID string) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	g, ok := m.groups[groupInstanceID]
	if !ok {
		return nil
	}
	// Delete the bridge first (a missing bridge is tolerated: it died with a prior
	// pod, or a partial create left none), then regenerate the ruleset without it.
	if _, err := m.runner.Run(ctx, "ip", groupBridgeTeardownArgs(g.bridge)[1:]...); err != nil {
		// A not-found bridge is fine; other errors are best-effort (the record is
		// going away regardless, and a restart rebuilds a clean table).
		_ = err
	}
	delete(m.groups, groupInstanceID)
	return m.applyRulesetLocked(ctx)
}

// Has reports whether a group network is currently held (for the server handler's
// idempotency / existence checks).
func (m *GroupManager) Has(groupInstanceID string) bool {
	m.mu.Lock()
	defer m.mu.Unlock()
	_, ok := m.groups[groupInstanceID]
	return ok
}

// BridgeFor returns the bridge name of a held group, or "" if absent.
func (m *GroupManager) BridgeFor(groupInstanceID string) string {
	m.mu.Lock()
	defer m.mu.Unlock()
	if g, ok := m.groups[groupInstanceID]; ok {
		return g.bridge
	}
	return ""
}

// List returns a projection of every held group network, for NodeStatus assembly.
func (m *GroupManager) List() []GroupNetworkInfo {
	m.mu.Lock()
	defer m.mu.Unlock()
	out := make([]GroupNetworkInfo, 0, len(m.groups))
	for _, g := range m.groups {
		out = append(out, GroupNetworkInfo{
			GroupInstanceID: g.groupInstanceID,
			Bridge:          g.bridge,
			CIDR:            g.cidr.String(),
			GatewayIP:       g.gatewayIP.String(),
			CreatedAtUnixMs: g.createdAtUnixMs,
		})
	}
	return out
}

// MemberAddressing returns the deterministic (tap, mac, ip) for a member of a
// group by (member_name, member_index), reserving the member IP in the group's
// allocator so a duplicate index/IP within the group is refused. It is the entry
// point Task 5's StartGroupMember uses to pin a member's NIC; exposed now (with
// its determinism table-tested) so the addressing contract is frozen before
// member lifecycle lands. A group that is not held, or an index outside the /24,
// is an error. The reservation is released via ReleaseMember.
func (m *GroupManager) MemberAddressing(groupInstanceID, memberName string, memberIndex uint32) (tap, mac string, ip net.IP, err error) {
	m.mu.Lock()
	g, ok := m.groups[groupInstanceID]
	m.mu.Unlock()
	if !ok {
		return "", "", nil, fmt.Errorf("serving: group %q not found", groupInstanceID)
	}
	ip, err = groupMemberIP(g.cidr, memberIndex)
	if err != nil {
		return "", "", nil, err
	}
	if rerr := g.alloc.reserve(ip); rerr != nil {
		return "", "", nil, rerr
	}
	return groupTapName(groupInstanceID, memberName), groupMemberMAC(groupInstanceID, memberName), ip, nil
}

// MemberAddressingFor derives the deterministic (tap, mac, ip) for a member
// WITHOUT reserving the IP (a pure derivation, for a relight re-pin or a
// read-only lookup). It errors only on an unknown group or an out-of-range index.
func (m *GroupManager) MemberAddressingFor(groupInstanceID, memberName string, memberIndex uint32) (tap, mac string, ip net.IP, err error) {
	m.mu.Lock()
	g, ok := m.groups[groupInstanceID]
	m.mu.Unlock()
	if !ok {
		return "", "", nil, fmt.Errorf("serving: group %q not found", groupInstanceID)
	}
	ip, err = groupMemberIP(g.cidr, memberIndex)
	if err != nil {
		return "", "", nil, err
	}
	return groupTapName(groupInstanceID, memberName), groupMemberMAC(groupInstanceID, memberName), ip, nil
}

// ReleaseMember returns a member IP to its group's allocator (idempotent). Used by
// Task 5's member teardown; provided now so the reserve in MemberAddressing has a
// symmetric release.
func (m *GroupManager) ReleaseMember(groupInstanceID string, ip net.IP) {
	m.mu.Lock()
	g, ok := m.groups[groupInstanceID]
	m.mu.Unlock()
	if !ok {
		return
	}
	g.alloc.release(ip)
}

// EnsureMemberTap pins a member's NIC world on the group bridge: it derives the
// deterministic (tap, mac, ip) for (member_name, member_index), verifies the derived
// IP matches the control-plane-supplied wantIP (a mismatch means the control plane
// and the daemon disagree on the pinned address, which must fail LOUDLY rather than
// silently boot on a different IP), reserves the IP in the group allocator, and
// creates + attaches the tap device to the group bridge. It is used by BOTH the
// FRESH boot and the RELIGHT resume: for a relight it recreates the SAME tap name,
// MAC, and IP the member had at bank time (the D-R3.4.1 pin applied to the group
// bridge), because the resumed guest keeps its baked eth0 and a snapshot restore
// never re-runs kernel init. On any tap-create failure the partial tap and the
// reserved IP are rolled back so a failed StartGroupMember leaks neither. It returns
// the (tap, mac) so the caller can build the NIC spec; the IP is wantIP unchanged.
func (m *GroupManager) EnsureMemberTap(ctx context.Context, groupInstanceID, memberName string, memberIndex uint32, wantIP net.IP) (tap, mac string, err error) {
	m.mu.Lock()
	g, ok := m.groups[groupInstanceID]
	bridge := ""
	if ok {
		bridge = g.bridge
	}
	m.mu.Unlock()
	if !ok {
		return "", "", fmt.Errorf("serving: group %q not found", groupInstanceID)
	}
	derivedIP, err := groupMemberIP(g.cidr, memberIndex)
	if err != nil {
		return "", "", err
	}
	if wantIP != nil && !derivedIP.Equal(wantIP) {
		return "", "", fmt.Errorf("serving: group %q member %q index %d derives IP %v but the request pinned %v", groupInstanceID, memberName, memberIndex, derivedIP, wantIP)
	}
	if rerr := g.alloc.reserve(derivedIP); rerr != nil {
		return "", "", rerr
	}
	tap = groupTapName(groupInstanceID, memberName)
	mac = groupMemberMAC(groupInstanceID, memberName)
	for _, argv := range tapSetupArgs(tap, bridge) {
		if _, rerr := m.runner.Run(ctx, argv[0], argv[1:]...); rerr != nil {
			// Roll back: delete whatever tap fragment exists, release the IP.
			_, _ = m.runner.Run(ctx, "ip", tapTeardownArgs(tap)[1:]...)
			g.alloc.release(derivedIP)
			return "", "", rerr
		}
	}
	return tap, mac, nil
}

// RemoveMemberTap deletes a member's tap device and releases its pinned IP back to
// the group allocator (idempotent at the ip layer: deleting an absent tap is
// tolerated). It is the symmetric teardown for EnsureMemberTap, called on a member
// bank, destroy, or a failed start's rollback. It takes the derived tap name and the
// pinned IP directly (the caller already holds both from the start path), so it never
// needs to re-derive or look the group up beyond the allocator release.
func (m *GroupManager) RemoveMemberTap(ctx context.Context, groupInstanceID, tap string, ip net.IP) {
	if _, err := m.runner.Run(ctx, "ip", tapTeardownArgs(tap)[1:]...); err != nil {
		// A missing tap is fine (already gone); keep teardown best-effort.
		_ = err
	}
	m.ReleaseMember(groupInstanceID, ip)
}

// GatewayIP returns a group's gateway IP (the .1 of its /24), for composing a
// member's default route. Nil if the group is not held.
func (m *GroupManager) GatewayIP(groupInstanceID string) net.IP {
	m.mu.Lock()
	defer m.mu.Unlock()
	if g, ok := m.groups[groupInstanceID]; ok {
		return g.gatewayIP
	}
	return nil
}

// PrefixLen returns a group's /24 prefix length (24), for a member's static IP
// mask. Zero if the group is not held.
func (m *GroupManager) PrefixLen(groupInstanceID string) int {
	m.mu.Lock()
	defer m.mu.Unlock()
	if g, ok := m.groups[groupInstanceID]; ok {
		return g.prefixLen
	}
	return 0
}

// EnsureEntryDNAT installs (or refreshes) the entry-member DNAT rule for a group,
// exposing the entry member's tap as podIP:vmPort (the D-R3.11.4 lane). It is a
// no-op when DNAT is disabled (empty podIP). The derived port is
// portBase + hostOffset(entryIP within the supernet); a derivation error is
// returned so the caller reaps rather than publishing an unreachable entry. Only
// the ENTRY member of a group is passed here (Task 5 decides which member is the
// entry); non-entry members are never DNAT'd. It regenerates the whole group
// table so the rule composes with the isolation drops.
func (m *GroupManager) EnsureEntryDNAT(ctx context.Context, groupInstanceID string, entryIP net.IP, guestPort uint32) error {
	if m.podIP == "" {
		return nil
	}
	vmPort, err := PortForIP(m.portBase, m.supernet, entryIP)
	if err != nil {
		return err
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	g, ok := m.groups[groupInstanceID]
	if !ok {
		return fmt.Errorf("serving: group %q not found", groupInstanceID)
	}
	g.entry = &groupEntry{tapIP: entryIP.String(), guestPort: guestPort, vmPort: vmPort}
	return m.applyRulesetLocked(ctx)
}

// RemoveEntryDNAT drops a group's entry-member DNAT rule and re-applies the table
// (best-effort: a re-apply error is swallowed, the entry is going away and a
// restart rebuilds a clean table). No-op when DNAT is disabled or the group is
// absent.
func (m *GroupManager) RemoveEntryDNAT(ctx context.Context, groupInstanceID string) {
	if m.podIP == "" {
		return
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	g, ok := m.groups[groupInstanceID]
	if !ok {
		return
	}
	g.entry = nil
	_ = m.applyRulesetLocked(ctx)
}

// EntryEndpoint projects a group entry member's (tap IP, guest port) into the
// endpoint the daemon REPORTS for the entry: (podIP, vmPort) when DNAT is
// enabled, else the tap IP + guest port unchanged (local/test fallback). Mirrors
// the serving Manager's Endpoint.
func (m *GroupManager) EntryEndpoint(entryIP net.IP, guestPort uint32) (string, uint32) {
	if m.podIP == "" {
		return entryIP.String(), guestPort
	}
	vmPort, err := PortForIP(m.portBase, m.supernet, entryIP)
	if err != nil {
		return entryIP.String(), guestPort
	}
	return m.podIP, vmPort
}

// applyRulesetLocked regenerates the whole group ruleset from the current group
// set (bridges + entry DNAT entries) and applies it via `nft -f <file>`. Caller
// holds m.mu.
func (m *GroupManager) applyRulesetLocked(ctx context.Context) error {
	bridges := make([]string, 0, len(m.groups))
	entries := make([]groupEntry, 0, len(m.groups))
	for _, g := range m.groups {
		bridges = append(bridges, g.bridge)
		if g.entry != nil {
			entries = append(entries, *g.entry)
		}
	}
	return applyRuleset(ctx, m.runner, nftGroupRuleset(bridges, m.servingBridge, m.podIP, entries))
}

// cidrOverlaps reports whether two IPv4 networks share any address (either
// contains the other's network base). For same-size /24s this is exact; for
// mixed sizes the containment test in either direction covers a nesting overlap.
func cidrOverlaps(a, b *net.IPNet) bool {
	return a.Contains(b.IP) || b.Contains(a.IP)
}
