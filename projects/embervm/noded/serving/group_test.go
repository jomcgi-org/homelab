package serving

import (
	"context"
	"errors"
	"fmt"
	"net"
	"strings"
	"testing"
)

// TestGroupBridgeSetupArgs asserts the ordered argv batches for a group bridge.
func TestGroupBridgeSetupArgs(t *testing.T) {
	got := groupBridgeSetupArgs("emg0a1b2c", "10.101.5.1", 24)
	want := [][]string{
		{"ip", "link", "add", "name", "emg0a1b2c", "type", "bridge"},
		{"ip", "addr", "add", "10.101.5.1/24", "dev", "emg0a1b2c"},
		{"ip", "link", "set", "emg0a1b2c", "up"},
	}
	if fmt.Sprint(got) != fmt.Sprint(want) {
		t.Fatalf("groupBridgeSetupArgs:\n got %v\nwant %v", got, want)
	}
	if td := groupBridgeTeardownArgs("emg0a1b2c"); fmt.Sprint(td) != fmt.Sprint([]string{"ip", "link", "del", "emg0a1b2c"}) {
		t.Fatalf("groupBridgeTeardownArgs: got %v", td)
	}
}

// TestGroupNameDerivationDeterministicAndBounded asserts the bridge/tap/MAC
// derivations are stable for the same inputs, distinct across groups for the same
// member name, and within the 15-char ifname limit.
func TestGroupNameDerivationDeterministicAndBounded(t *testing.T) {
	gid := "grp-instance-aaaa-bbbb-cccc"
	// Determinism: same inputs -> same outputs.
	if a, b := groupBridgeName(gid), groupBridgeName(gid); a != b {
		t.Fatalf("groupBridgeName not deterministic: %q != %q", a, b)
	}
	if a, b := groupTapName(gid, "worker-0"), groupTapName(gid, "worker-0"); a != b {
		t.Fatalf("groupTapName not deterministic: %q != %q", a, b)
	}
	if a, b := groupMemberMAC(gid, "worker-0"), groupMemberMAC(gid, "worker-0"); a != b {
		t.Fatalf("groupMemberMAC not deterministic: %q != %q", a, b)
	}

	// ifname length safety.
	if br := groupBridgeName(gid); len(br) > groupIfnameMax {
		t.Errorf("bridge name %q exceeds ifname limit %d", br, groupIfnameMax)
	}
	if tap := groupTapName(gid, "a-very-long-member-name-that-would-overflow"); len(tap) > groupIfnameMax {
		t.Errorf("tap name %q exceeds ifname limit %d", tap, groupIfnameMax)
	}

	// Bridge prefix + tap prefix so a reader can spot the class at a glance.
	if br := groupBridgeName(gid); !strings.HasPrefix(br, "emg") {
		t.Errorf("bridge name %q missing emg prefix", br)
	}
	if tap := groupTapName(gid, "worker-0"); !strings.HasPrefix(tap, "emgt") {
		t.Errorf("tap name %q missing emgt prefix", tap)
	}

	// A locally-administered unicast MAC (first octet 0x02).
	if mac := groupMemberMAC(gid, "worker-0"); !strings.HasPrefix(mac, "02:") {
		t.Errorf("MAC %q is not locally-administered (02: prefix)", mac)
	}

	// Two DIFFERENT groups' identically-named members must NOT collide on tap or MAC.
	other := "grp-instance-dddd-eeee-ffff"
	if groupTapName(gid, "worker-0") == groupTapName(other, "worker-0") {
		t.Error("tap names collide across groups for the same member name")
	}
	if groupMemberMAC(gid, "worker-0") == groupMemberMAC(other, "worker-0") {
		t.Error("MACs collide across groups for the same member name")
	}
	// Different members within a group must differ.
	if groupTapName(gid, "worker-0") == groupTapName(gid, "worker-1") {
		t.Error("tap names collide across members within a group")
	}
}

// TestGroupMemberIP asserts the .10+i addressing rule, its determinism, and its
// out-of-range refusal.
func TestGroupMemberIP(t *testing.T) {
	_, net24, _ := net.ParseCIDR("10.101.7.0/24")
	cases := []struct {
		index uint32
		want  string
	}{
		{0, "10.101.7.10"},
		{1, "10.101.7.11"},
		{5, "10.101.7.15"},
	}
	for _, c := range cases {
		ip, err := groupMemberIP(net24, c.index)
		if err != nil {
			t.Fatalf("groupMemberIP(%d): %v", c.index, err)
		}
		if ip.String() != c.want {
			t.Errorf("groupMemberIP(%d) = %s want %s", c.index, ip, c.want)
		}
		// Determinism: same inputs, same IP.
		if ip2, _ := groupMemberIP(net24, c.index); !ip.Equal(ip2) {
			t.Errorf("groupMemberIP(%d) not deterministic", c.index)
		}
	}
	// An index past the /24 host range is refused (10 + 250 = .260 overflows the /24).
	if _, err := groupMemberIP(net24, 250); err == nil {
		t.Error("groupMemberIP for an out-of-range index should error")
	}
}

// TestNftGroupRulesetIsolation asserts the inter-group forward DROPs: every
// ordered pair of distinct group bridges (composite<->composite, both directions)
// and every group-bridge -> serving-bridge (composite->serving), plus the
// established/related accept, all in the DEDICATED embervm_group table (never the
// serving table).
func TestNftGroupRulesetIsolation(t *testing.T) {
	rs := nftGroupRuleset([]string{"emgAAAAAA", "emgBBBBBB"}, "embervm-serv0", "", nil)

	// Dedicated table, own forward chain, established accept.
	for _, w := range []string{
		"add table inet embervm_group",
		"flush table inet embervm_group",
		"add chain inet embervm_group group_forward { type filter hook forward priority 0; policy accept; }",
		"add rule inet embervm_group group_forward ct state established,related accept",
	} {
		if !strings.Contains(rs, w) {
			t.Errorf("group ruleset missing:\n  %q\nfull:\n%s", w, rs)
		}
	}

	// Per-bridge ZERO-EGRESS denial (standing decision 4): each bridge drops every
	// NEW packet leaving it to anywhere other than itself (other groups, serving, AND
	// external/CNI). This is the primary egress denial; the cross-group and
	// composite->serving drops below are redundant belt-and-braces.
	for _, w := range []string{
		"add rule inet embervm_group group_forward iifname \"emgAAAAAA\" oifname != \"emgAAAAAA\" ct state new drop",
		"add rule inet embervm_group group_forward iifname \"emgBBBBBB\" oifname != \"emgBBBBBB\" ct state new drop",
	} {
		if !strings.Contains(rs, w) {
			t.Errorf("group ruleset missing per-bridge zero-egress drop:\n  %q\nfull:\n%s", w, rs)
		}
	}

	// composite<->composite BOTH directions.
	for _, w := range []string{
		"add rule inet embervm_group group_forward iifname \"emgAAAAAA\" oifname \"emgBBBBBB\" drop",
		"add rule inet embervm_group group_forward iifname \"emgBBBBBB\" oifname \"emgAAAAAA\" drop",
	} {
		if !strings.Contains(rs, w) {
			t.Errorf("group ruleset missing inter-group drop:\n  %q\nfull:\n%s", w, rs)
		}
	}

	// composite->serving for each group bridge.
	for _, w := range []string{
		"add rule inet embervm_group group_forward iifname \"emgAAAAAA\" oifname \"embervm-serv0\" drop",
		"add rule inet embervm_group group_forward iifname \"emgBBBBBB\" oifname \"embervm-serv0\" drop",
	} {
		if !strings.Contains(rs, w) {
			t.Errorf("group ruleset missing composite->serving drop:\n  %q\nfull:\n%s", w, rs)
		}
	}

	// It must NOT touch the serving table or its chains (no collision).
	if strings.Contains(rs, "embervm_serving") || strings.Contains(rs, "serving_dnat") {
		t.Errorf("group ruleset must not reference the serving table/chains:\n%s", rs)
	}
	// With no pod IP there is no DNAT chain.
	if strings.Contains(rs, "group_dnat") {
		t.Errorf("empty-podIP group ruleset must have no DNAT chain:\n%s", rs)
	}
	// flush after add-table for first-apply idempotency.
	if strings.Index(rs, "flush table") < strings.Index(rs, "add table") {
		t.Error("group ruleset: flush must come after add table")
	}
}

// TestNftGroupRulesetEntryDNAT asserts the entry-member DNAT lane renders (podIP)
// and that a LONE group (single bridge, no peer groups, no serving bridge) STILL
// emits its own per-bridge zero-egress drop: the egress denial must not depend on
// the presence of other bridges (standing decision 4). It also asserts the ONLY
// drop is that per-bridge egress rule (no cross-group / composite->serving rules
// when there is no peer group and no serving bridge).
func TestNftGroupRulesetEntryDNAT(t *testing.T) {
	entries := []groupEntry{{tapIP: "10.101.7.10", guestPort: 8080, vmPort: 41802}}
	rs := nftGroupRuleset([]string{"emgAAAAAA"}, "", "10.42.0.9", entries)
	for _, w := range []string{
		"add chain inet embervm_group group_dnat { type nat hook prerouting priority dstnat; policy accept; }",
		"add rule inet embervm_group group_dnat ip daddr 10.42.0.9 tcp dport 41802 dnat ip to 10.101.7.10:8080",
		// The lone group's own egress denial: present even with no peers/serving.
		"add rule inet embervm_group group_forward iifname \"emgAAAAAA\" oifname != \"emgAAAAAA\" ct state new drop",
	} {
		if !strings.Contains(rs, w) {
			t.Errorf("group entry-DNAT ruleset missing:\n  %q\nfull:\n%s", w, rs)
		}
	}
	// The per-bridge egress drop is the ONLY drop for a lone group (no cross-group
	// pair, no composite->serving). Assert exactly one "drop" in the forward chain.
	if n := strings.Count(rs, " drop\n"); n != 1 {
		t.Errorf("lone group must emit exactly one drop (its own egress denial), got %d:\n%s", n, rs)
	}
}

// TestNftGroupRulesetDeterministic asserts the ruleset is a pure function of the
// bridge/entry SET (sorted internally), so map iteration order never changes it.
func TestNftGroupRulesetDeterministic(t *testing.T) {
	a := nftGroupRuleset([]string{"emgBBBBBB", "emgAAAAAA"}, "serv0", "", nil)
	b := nftGroupRuleset([]string{"emgAAAAAA", "emgBBBBBB"}, "serv0", "", nil)
	if a != b {
		t.Errorf("nftGroupRuleset not order-independent:\n--- a ---\n%s\n--- b ---\n%s", a, b)
	}
}

func TestNftGroupTeardownArgs(t *testing.T) {
	got := nftGroupTeardownArgs()
	want := []string{"nft", "delete", "table", "inet", "embervm_group"}
	if fmt.Sprint(got) != fmt.Sprint(want) {
		t.Fatalf("nftGroupTeardownArgs: got %v want %v", got, want)
	}
}

// TestGroupManagerCIDRValidation covers the /24-in-supernet rule and overlap
// refusal.
func TestGroupManagerCIDRValidation(t *testing.T) {
	m, err := NewGroupManager(&fakeRunner{}, "10.101.0.0/16", "embervm-serv0", "", 40000)
	if err != nil {
		t.Fatalf("NewGroupManager: %v", err)
	}
	// A valid /24 inside the supernet is accepted and yields the .1 gateway.
	br, gw, err := m.CreateGroupNetwork(context.Background(), "grp-A", "10.101.1.0/24")
	if err != nil {
		t.Fatalf("CreateGroupNetwork(grp-A): %v", err)
	}
	if gw != "10.101.1.1" {
		t.Errorf("gateway = %s want 10.101.1.1", gw)
	}
	if br != groupBridgeName("grp-A") {
		t.Errorf("bridge = %s want %s", br, groupBridgeName("grp-A"))
	}

	// A non-/24 is refused.
	if _, _, err := m.CreateGroupNetwork(context.Background(), "grp-bad-prefix", "10.101.2.0/25"); err == nil {
		t.Error("a /25 group cidr should be refused")
	}
	// A /24 OUTSIDE the supernet is refused.
	if _, _, err := m.CreateGroupNetwork(context.Background(), "grp-outside", "10.200.1.0/24"); err == nil {
		t.Error("a group cidr outside the supernet should be refused")
	}
	// A /24 overlapping grp-A's is refused (same range, different id).
	if _, _, err := m.CreateGroupNetwork(context.Background(), "grp-overlap", "10.101.1.0/24"); err == nil {
		t.Error("an overlapping group cidr should be refused")
	}
}

// TestGroupManagerCreateIdempotent asserts a re-issue with the same cidr returns
// the same (bridge, gateway) and re-issues the idempotent bridge setup (so a bridge
// that outlived its record is rebuilt rather than trusted), and a re-issue with a
// DIFFERENT cidr for the same id is refused.
func TestGroupManagerCreateIdempotent(t *testing.T) {
	fr := &fakeRunner{}
	m, err := NewGroupManager(fr, "10.101.0.0/16", "", "", 40000)
	if err != nil {
		t.Fatalf("NewGroupManager: %v", err)
	}
	br1, gw1, err := m.CreateGroupNetwork(context.Background(), "grp-A", "10.101.1.0/24")
	if err != nil {
		t.Fatalf("first create: %v", err)
	}
	callsAfterFirst := len(fr.calls)

	br2, gw2, err := m.CreateGroupNetwork(context.Background(), "grp-A", "10.101.1.0/24")
	if err != nil {
		t.Fatalf("idempotent re-create: %v", err)
	}
	if br1 != br2 || gw1 != gw2 {
		t.Errorf("idempotent re-create changed identity: (%s,%s) vs (%s,%s)", br1, gw1, br2, gw2)
	}
	// A re-issue must re-run the idempotent bridge setup so a bridge that died with
	// a prior pod (record survives) is rebuilt, not silently trusted.
	sawAdd := false
	for _, c := range fr.calls[callsAfterFirst:] {
		if strings.Join(c, " ") == fmt.Sprintf("ip link add name %s type bridge", br2) {
			sawAdd = true
		}
	}
	if !sawAdd {
		t.Errorf("idempotent re-create did not re-issue the bridge setup; extra calls: %v", fr.calls[callsAfterFirst:])
	}

	// Same id, different cidr => conflict.
	if _, _, err := m.CreateGroupNetwork(context.Background(), "grp-A", "10.101.2.0/24"); err == nil {
		t.Error("re-creating grp-A with a different cidr should conflict")
	}
}

// TestGroupManagerCreateRebuildsBridgeAfterAdopt is the regression for the R6
// drill wedge: AdoptGroupNetwork re-seeds a group record on boot WITHOUT creating
// the bridge (it died with the prior pod), and a subsequent CreateGroupNetwork
// re-issue must REBUILD the bridge. Before the fix the idempotency hit short-
// circuited on the map entry and issued no bridge setup, so EnsureMemberTap later
// pinned a member tap to a nonexistent bridge ("Device does not exist").
func TestGroupManagerCreateRebuildsBridgeAfterAdopt(t *testing.T) {
	fr := &fakeRunner{}
	m, err := NewGroupManager(fr, "10.101.0.0/16", "", "", 40000)
	if err != nil {
		t.Fatalf("NewGroupManager: %v", err)
	}
	// Boot rescan seeds the record but not the device.
	if err := m.AdoptGroupNetwork("grp-A", "10.101.1.0/24", 0); err != nil {
		t.Fatalf("adopt: %v", err)
	}
	callsAfterAdopt := len(fr.calls)

	br, _, err := m.CreateGroupNetwork(context.Background(), "grp-A", "10.101.1.0/24")
	if err != nil {
		t.Fatalf("create after adopt: %v", err)
	}
	sawAdd := false
	for _, c := range fr.calls[callsAfterAdopt:] {
		if strings.Join(c, " ") == fmt.Sprintf("ip link add name %s type bridge", br) {
			sawAdd = true
		}
	}
	if !sawAdd {
		t.Fatalf("create after adopt did NOT rebuild the bridge (idempotency short-circuit skipped bridge setup); calls: %v", fr.argvStrings())
	}
}

// TestGroupManagerDeleteAndBridgeCommands asserts a create issues the bridge
// setup and a delete issues the bridge teardown, and that delete is idempotent.
func TestGroupManagerDeleteAndBridgeCommands(t *testing.T) {
	fr := &fakeRunner{}
	m, err := NewGroupManager(fr, "10.101.0.0/16", "", "", 40000)
	if err != nil {
		t.Fatalf("NewGroupManager: %v", err)
	}
	br, _, err := m.CreateGroupNetwork(context.Background(), "grp-A", "10.101.1.0/24")
	if err != nil {
		t.Fatalf("create: %v", err)
	}
	sawAdd := false
	for _, c := range fr.calls {
		if strings.Join(c, " ") == fmt.Sprintf("ip link add name %s type bridge", br) {
			sawAdd = true
		}
	}
	if !sawAdd {
		t.Errorf("create did not issue the bridge add; calls: %v", fr.argvStrings())
	}

	if err := m.DeleteGroupNetwork(context.Background(), "grp-A"); err != nil {
		t.Fatalf("delete: %v", err)
	}
	if m.Has("grp-A") {
		t.Error("group still held after delete")
	}
	sawDel := false
	for _, c := range fr.calls {
		if strings.Join(c, " ") == fmt.Sprintf("ip link del %s", br) {
			sawDel = true
		}
	}
	if !sawDel {
		t.Errorf("delete did not issue the bridge teardown; calls: %v", fr.argvStrings())
	}
	// Idempotent: deleting again is a no-op success.
	if err := m.DeleteGroupNetwork(context.Background(), "grp-A"); err != nil {
		t.Errorf("second delete should be a no-op, got %v", err)
	}
}

// TestGroupManagerMemberAddressing asserts the reserving derivation pins IPs
// per-group, refuses a duplicate, and is deterministic.
func TestGroupManagerMemberAddressing(t *testing.T) {
	m, err := NewGroupManager(&fakeRunner{}, "10.101.0.0/16", "", "", 40000)
	if err != nil {
		t.Fatalf("NewGroupManager: %v", err)
	}
	if _, _, err := m.CreateGroupNetwork(context.Background(), "grp-A", "10.101.1.0/24"); err != nil {
		t.Fatalf("create: %v", err)
	}
	tap, mac, ip, err := m.MemberAddressing("grp-A", "worker-0", 0)
	if err != nil {
		t.Fatalf("MemberAddressing: %v", err)
	}
	if ip.String() != "10.101.1.10" {
		t.Errorf("member 0 ip = %s want 10.101.1.10", ip)
	}
	if tap != groupTapName("grp-A", "worker-0") || mac != groupMemberMAC("grp-A", "worker-0") {
		t.Errorf("member addressing not deterministic with the pure derivers")
	}
	// Re-reserving the SAME index (its IP is held) conflicts.
	if _, _, _, err := m.MemberAddressing("grp-A", "worker-0", 0); err == nil {
		t.Error("re-reserving a held member IP should conflict")
	}
	// Releasing frees it for reuse.
	m.ReleaseMember("grp-A", ip)
	if _, _, _, err := m.MemberAddressing("grp-A", "worker-0", 0); err != nil {
		t.Errorf("MemberAddressing after release: %v", err)
	}
	// The read-only variant never reserves (callable twice).
	if _, _, _, err := m.MemberAddressingFor("grp-A", "worker-9", 9); err != nil {
		t.Fatalf("MemberAddressingFor: %v", err)
	}
	if _, _, _, err := m.MemberAddressingFor("grp-A", "worker-9", 9); err != nil {
		t.Errorf("MemberAddressingFor should not reserve (callable twice): %v", err)
	}
	// An unknown group errors.
	if _, _, _, err := m.MemberAddressing("nope", "worker-0", 0); err == nil {
		t.Error("MemberAddressing on an unknown group should error")
	}
}

// TestGroupManagerEntryDNAT asserts EnsureEntryDNAT installs the group's entry
// rule (with the derived port) and RemoveEntryDNAT drops it, both re-applying the
// group table.
func TestGroupManagerEntryDNAT(t *testing.T) {
	fr := &fakeRunner{}
	m, err := NewGroupManager(fr, "10.101.0.0/16", "embervm-serv0", "10.42.0.9", 40000)
	if err != nil {
		t.Fatalf("NewGroupManager: %v", err)
	}
	if _, _, err := m.CreateGroupNetwork(context.Background(), "grp-A", "10.101.1.0/24"); err != nil {
		t.Fatalf("create: %v", err)
	}
	entryIP := net.ParseIP("10.101.1.10")
	if err := m.EnsureEntryDNAT(context.Background(), "grp-A", entryIP, 8080); err != nil {
		t.Fatalf("EnsureEntryDNAT: %v", err)
	}
	last := lastNftContent(t, fr)
	if !strings.Contains(last, "group_dnat ip daddr 10.42.0.9") || !strings.Contains(last, "dnat ip to 10.101.1.10:8080") {
		t.Errorf("entry-DNAT rule not installed:\n%s", last)
	}
	// The endpoint projection reports podIP + the derived port.
	gotIP, gotPort := m.EntryEndpoint(entryIP, 8080)
	wantPort, _ := PortForIP(40000, m.supernet, entryIP)
	if gotIP != "10.42.0.9" || gotPort != wantPort {
		t.Errorf("EntryEndpoint = %s:%d want 10.42.0.9:%d", gotIP, gotPort, wantPort)
	}

	m.RemoveEntryDNAT(context.Background(), "grp-A")
	if last := lastNftContent(t, fr); strings.Contains(last, "dnat ip to 10.101.1.10:8080") {
		t.Errorf("RemoveEntryDNAT should have dropped the rule:\n%s", last)
	}
}

// TestGroupManagerEntryDNATDisabled asserts that with no pod IP EnsureEntryDNAT is
// a no-op (endpoint reports the tap IP unchanged).
func TestGroupManagerEntryDNATDisabled(t *testing.T) {
	fr := &fakeRunner{}
	m, err := NewGroupManager(fr, "10.101.0.0/16", "", "", 40000)
	if err != nil {
		t.Fatalf("NewGroupManager: %v", err)
	}
	if _, _, err := m.CreateGroupNetwork(context.Background(), "grp-A", "10.101.1.0/24"); err != nil {
		t.Fatalf("create: %v", err)
	}
	before := countNftApplies(fr)
	if err := m.EnsureEntryDNAT(context.Background(), "grp-A", net.ParseIP("10.101.1.10"), 8080); err != nil {
		t.Fatalf("EnsureEntryDNAT: %v", err)
	}
	if countNftApplies(fr) != before {
		t.Errorf("EnsureEntryDNAT with no pod IP should apply nothing")
	}
	ip, port := m.EntryEndpoint(net.ParseIP("10.101.1.10"), 8080)
	if ip != "10.101.1.10" || port != 8080 {
		t.Errorf("EntryEndpoint(off) = %s:%d want the tap 10.101.1.10:8080", ip, port)
	}
}

// TestGroupManagerAdoptRoundTrip asserts a group adopted from a record is held,
// listed, and validated the same way create validates.
func TestGroupManagerAdoptRoundTrip(t *testing.T) {
	m, err := NewGroupManager(&fakeRunner{}, "10.101.0.0/16", "", "", 40000)
	if err != nil {
		t.Fatalf("NewGroupManager: %v", err)
	}
	if err := m.AdoptGroupNetwork("grp-A", "10.101.3.0/24", 1234); err != nil {
		t.Fatalf("AdoptGroupNetwork: %v", err)
	}
	if !m.Has("grp-A") {
		t.Error("adopted group not held")
	}
	list := m.List()
	if len(list) != 1 || list[0].GroupInstanceID != "grp-A" || list[0].CIDR != "10.101.3.0/24" {
		t.Errorf("List after adopt = %+v", list)
	}
	if list[0].GatewayIP != "10.101.3.1" || list[0].CreatedAtUnixMs != 1234 {
		t.Errorf("adopted group fields wrong: %+v", list[0])
	}
	// Adopting again is idempotent.
	if err := m.AdoptGroupNetwork("grp-A", "10.101.3.0/24", 1234); err != nil {
		t.Errorf("re-adopt should be idempotent: %v", err)
	}
	// A malformed record is rejected.
	if err := m.AdoptGroupNetwork("grp-bad", "not-a-cidr", 0); err == nil {
		t.Error("adopting a malformed record should error")
	}
}

// TestNewGroupManagerRejectsBadSupernet asserts a malformed supernet fails loudly.
func TestNewGroupManagerRejectsBadSupernet(t *testing.T) {
	for _, sn := range []string{"nope", "::1/64", ""} {
		if _, err := NewGroupManager(&fakeRunner{}, sn, "", "", 40000); err == nil {
			t.Errorf("NewGroupManager(%q) should error", sn)
		}
	}
}

// TestGroupManagerCreateRollsBackOnBridgeFailure asserts a bridge-create failure
// leaves no group held and no ruleset applied for it.
func TestGroupManagerCreateRollsBackOnBridgeFailure(t *testing.T) {
	fr := &fakeRunner{failOn: map[string]error{
		"ip link": errors.New("boom"),
	}}
	m, err := NewGroupManager(fr, "10.101.0.0/16", "", "", 40000)
	if err != nil {
		t.Fatalf("NewGroupManager: %v", err)
	}
	if _, _, err := m.CreateGroupNetwork(context.Background(), "grp-A", "10.101.1.0/24"); err == nil {
		t.Fatal("create should fail when the bridge add fails")
	}
	if m.Has("grp-A") {
		t.Error("group must not be held after a failed create")
	}
}

// TestEnsureMemberTapPinsWorldAndRecreatesIdentically asserts EnsureMemberTap
// creates + attaches the member tap on the group bridge with the deterministic tap
// name and MAC, verifies the derived IP matches the request, and that a second call
// (a relight after ReleaseMember) recreates the SAME tap/MAC/IP: the pinned-world
// reconstruction the D-R3.4.1 pin requires on the group bridge.
func TestEnsureMemberTapPinsWorldAndRecreatesIdentically(t *testing.T) {
	fr := &fakeRunner{}
	m, err := NewGroupManager(fr, "10.101.0.0/16", "", "", 40000)
	if err != nil {
		t.Fatalf("NewGroupManager: %v", err)
	}
	if _, _, err := m.CreateGroupNetwork(context.Background(), "grp-A", "10.101.1.0/24"); err != nil {
		t.Fatalf("CreateGroupNetwork: %v", err)
	}
	wantIP := net.ParseIP("10.101.1.10") // index 0 -> .10
	tap, mac, err := m.EnsureMemberTap(context.Background(), "grp-A", "worker-0", 0, wantIP)
	if err != nil {
		t.Fatalf("EnsureMemberTap: %v", err)
	}
	if tap != groupTapName("grp-A", "worker-0") || mac != groupMemberMAC("grp-A", "worker-0") {
		t.Errorf("tap/mac not the deterministic derivation: tap=%q mac=%q", tap, mac)
	}
	// The tap must have been created and attached to the group bridge.
	bridge := groupBridgeName("grp-A")
	var sawAdd, sawMaster bool
	for _, c := range fr.argvStrings() {
		if c == "ip tuntap add dev "+tap+" mode tap" {
			sawAdd = true
		}
		if c == "ip link set "+tap+" master "+bridge {
			sawMaster = true
		}
	}
	if !sawAdd || !sawMaster {
		t.Errorf("member tap not created+attached to the group bridge; calls: %v", fr.argvStrings())
	}

	// Release (as a bank would) then re-pin (as a relight would): identical world.
	m.RemoveMemberTap(context.Background(), "grp-A", tap, wantIP)
	tap2, mac2, err := m.EnsureMemberTap(context.Background(), "grp-A", "worker-0", 0, wantIP)
	if err != nil {
		t.Fatalf("EnsureMemberTap (relight): %v", err)
	}
	if tap2 != tap || mac2 != mac {
		t.Errorf("relight recreated a DIFFERENT world: tap %q->%q mac %q->%q", tap, tap2, mac, mac2)
	}
}

// TestEnsureMemberTapRejectsMismatchedIP asserts a request IP that disagrees with
// the deterministic derivation fails loudly (never boots on a different IP).
func TestEnsureMemberTapRejectsMismatchedIP(t *testing.T) {
	m, err := NewGroupManager(&fakeRunner{}, "10.101.0.0/16", "", "", 40000)
	if err != nil {
		t.Fatalf("NewGroupManager: %v", err)
	}
	if _, _, err := m.CreateGroupNetwork(context.Background(), "grp-A", "10.101.1.0/24"); err != nil {
		t.Fatalf("CreateGroupNetwork: %v", err)
	}
	// index 0 derives .10, but the request claims .99: a mismatch must fail.
	if _, _, err := m.EnsureMemberTap(context.Background(), "grp-A", "worker-0", 0, net.ParseIP("10.101.1.99")); err == nil {
		t.Fatal("a request IP that disagrees with the derivation must fail")
	}
}

// TestEnsureMemberTapRollsBackOnTapFailure asserts a tap-create failure releases the
// reserved IP so a retry (or a different member) can still use the address space.
func TestEnsureMemberTapRollsBackOnTapFailure(t *testing.T) {
	fr := &fakeRunner{failOn: map[string]error{"ip tuntap": errors.New("boom")}}
	m, err := NewGroupManager(fr, "10.101.0.0/16", "", "", 40000)
	if err != nil {
		t.Fatalf("NewGroupManager: %v", err)
	}
	if _, _, err := m.CreateGroupNetwork(context.Background(), "grp-A", "10.101.1.0/24"); err != nil {
		t.Fatalf("CreateGroupNetwork: %v", err)
	}
	if _, _, err := m.EnsureMemberTap(context.Background(), "grp-A", "worker-0", 0, net.ParseIP("10.101.1.10")); err == nil {
		t.Fatal("tap create failure should surface")
	}
	// The IP must be free again: a retry succeeds (failOn cleared).
	fr.failOn = nil
	if _, _, err := m.EnsureMemberTap(context.Background(), "grp-A", "worker-0", 0, net.ParseIP("10.101.1.10")); err != nil {
		t.Fatalf("retry after rollback should succeed: %v", err)
	}
}
