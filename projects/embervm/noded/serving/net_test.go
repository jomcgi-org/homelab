package serving

import (
	"context"
	"errors"
	"fmt"
	"net"
	"os"
	"strconv"
	"strings"
	"testing"
)

// fakeRunner records every command it is asked to run and returns canned
// output/errors keyed by the first two argv tokens, so tests assert the exact
// ip/nft invocations as data without touching the host. For `nft -f <path>` it also
// reads and records the ruleset file content at Run time, because applyRuleset removes
// the temp file immediately after Run returns.
type fakeRunner struct {
	calls       [][]string
	nftContents []string         // ruleset text of each `nft -f` apply, in order
	failOn      map[string]error // key: "name arg0"; a matching call returns the error
}

func (f *fakeRunner) Run(_ context.Context, name string, args ...string) ([]byte, error) {
	call := append([]string{name}, args...)
	f.calls = append(f.calls, call)
	if name == "nft" && len(args) == 2 && args[0] == "-f" {
		if data, err := os.ReadFile(args[1]); err == nil {
			f.nftContents = append(f.nftContents, string(data))
		}
	}
	if f.failOn != nil {
		key := name
		if len(args) > 0 {
			key = name + " " + args[0]
		}
		if err, ok := f.failOn[key]; ok {
			return nil, err
		}
	}
	return nil, nil
}

// countNftApplies is the number of `nft -f` applies the runner saw.
func countNftApplies(f *fakeRunner) int { return len(f.nftContents) }

// lastNftContent is the ruleset text of the most recent `nft -f` apply.
func lastNftContent(t *testing.T, f *fakeRunner) string {
	t.Helper()
	if len(f.nftContents) == 0 {
		t.Fatal("no nft -f apply recorded")
	}
	return f.nftContents[len(f.nftContents)-1]
}

func (f *fakeRunner) argvStrings() []string {
	out := make([]string, 0, len(f.calls))
	for _, c := range f.calls {
		out = append(out, strings.Join(c, " "))
	}
	return out
}

func TestBridgeSetupArgs(t *testing.T) {
	got := bridgeSetupArgs("br0", "172.31.0.1", 24)
	want := [][]string{
		{"ip", "link", "add", "name", "br0", "type", "bridge"},
		{"ip", "addr", "add", "172.31.0.1/24", "dev", "br0"},
		{"ip", "link", "set", "br0", "up"},
	}
	if fmt.Sprint(got) != fmt.Sprint(want) {
		t.Fatalf("bridgeSetupArgs:\n got %v\nwant %v", got, want)
	}
}

func TestTapSetupAndTeardownArgs(t *testing.T) {
	setup := tapSetupArgs("emtap0102", "br0")
	wantSetup := [][]string{
		{"ip", "tuntap", "add", "dev", "emtap0102", "mode", "tap"},
		{"ip", "link", "set", "emtap0102", "master", "br0"},
		{"ip", "link", "set", "emtap0102", "up"},
	}
	if fmt.Sprint(setup) != fmt.Sprint(wantSetup) {
		t.Fatalf("tapSetupArgs:\n got %v\nwant %v", setup, wantSetup)
	}
	teardown := tapTeardownArgs("emtap0102")
	if fmt.Sprint(teardown) != fmt.Sprint([]string{"ip", "link", "del", "emtap0102"}) {
		t.Fatalf("tapTeardownArgs: got %v", teardown)
	}
}

// TestNftRulesetLocalFallback asserts that with an empty pod IP (local/test) the
// ruleset is EXACTLY the v1 forward posture: a dedicated table, a forward chain,
// established/related accept, and a drop of VM-originated NEW forwarding, with NO MSS
// clamp and NO DNAT chain (keeps old behaviour for tests/local).
func TestNftRulesetLocalFallback(t *testing.T) {
	rs := nftRuleset("br0", "", nil)
	wantLines := []string{
		"add table inet embervm_serving",
		"flush table inet embervm_serving",
		"add chain inet embervm_serving forward { type filter hook forward priority 0; policy accept; }",
		"add rule inet embervm_serving forward ct state established,related accept",
		"add rule inet embervm_serving forward iifname \"br0\" ct state new drop",
	}
	for _, w := range wantLines {
		if !strings.Contains(rs, w) {
			t.Errorf("nftRuleset missing line:\n  %q\nfull ruleset:\n%s", w, rs)
		}
	}
	if strings.Contains(rs, nftDNATChain) {
		t.Errorf("empty-podIP ruleset must NOT include the DNAT chain:\n%s", rs)
	}
	if strings.Contains(rs, "maxseg") {
		t.Errorf("empty-podIP ruleset must NOT include the MSS clamp:\n%s", rs)
	}
	// The flush must come AFTER the add-table so the script is idempotent on first
	// apply (flushing a not-yet-created table errors).
	if strings.Index(rs, "flush table") < strings.Index(rs, "add table") {
		t.Error("nftRuleset: flush table must come after add table for first-apply idempotency")
	}
}

// TestNftRulesetDNATEmpty asserts that with a pod IP but no live VMs the ruleset gains
// the MSS clamp and the (rule-less) serving_dnat prerouting chain.
func TestNftRulesetDNATEmpty(t *testing.T) {
	rs := nftRuleset("br0", "10.42.0.9", nil)
	for _, w := range []string{
		"add rule inet embervm_serving forward oifname \"br0\" tcp flags syn tcp option maxseg size set rt mtu",
		"add chain inet embervm_serving serving_dnat { type nat hook prerouting priority dstnat; policy accept; }",
	} {
		if !strings.Contains(rs, w) {
			t.Errorf("DNAT-enabled ruleset missing line:\n  %q\nfull ruleset:\n%s", w, rs)
		}
	}
	// No per-VM rule yet.
	if strings.Contains(rs, "dnat ip to") {
		t.Errorf("empty-entries ruleset must have no DNAT rule:\n%s", rs)
	}
}

// TestNftRulesetDNATEntries asserts the per-VM DNAT rules render with the required
// `dnat ip to <tap>:<guestPort>` inet syntax, ordered by vmPort (tap-IP order).
func TestNftRulesetDNATEntries(t *testing.T) {
	// Deliberately pass entries out of vmPort order to prove the generator sorts.
	entries := []dnatEntry{
		{tapIP: "172.31.0.4", guestPort: 8080, vmPort: 30004},
		{tapIP: "172.31.0.2", guestPort: 8080, vmPort: 30002},
	}
	rs := nftRuleset("br0", "10.42.0.9", entries)
	first := "add rule inet embervm_serving serving_dnat ip daddr 10.42.0.9 tcp dport 30002 dnat ip to 172.31.0.2:8080"
	second := "add rule inet embervm_serving serving_dnat ip daddr 10.42.0.9 tcp dport 30004 dnat ip to 172.31.0.4:8080"
	if !strings.Contains(rs, first) || !strings.Contains(rs, second) {
		t.Fatalf("DNAT rules missing/incorrect:\n%s", rs)
	}
	if strings.Index(rs, first) > strings.Index(rs, second) {
		t.Errorf("DNAT rules must be ordered by vmPort (30002 before 30004):\n%s", rs)
	}
}

// TestPortForIP asserts the deterministic port derivation, its bounds, and uniqueness
// across a /24.
func TestPortForIP(t *testing.T) {
	_, network, _ := net.ParseCIDR("172.31.0.0/24")
	cases := []struct {
		ip   string
		want uint32
	}{
		{"172.31.0.2", 30002},
		{"172.31.0.254", 30254},
	}
	for _, c := range cases {
		got, err := PortForIP(30000, network, net.ParseIP(c.ip))
		if err != nil {
			t.Fatalf("PortForIP(%s): %v", c.ip, err)
		}
		if got != c.want {
			t.Errorf("PortForIP(%s) = %d want %d", c.ip, got, c.want)
		}
	}
	// An IP outside the network is rejected.
	if _, err := PortForIP(30000, network, net.ParseIP("10.0.0.2")); err == nil {
		t.Error("PortForIP for an out-of-network IP should error")
	}
	// A base that pushes the top offset past 65535 overflows for the .254 host.
	if _, err := PortForIP(65500, network, net.ParseIP("172.31.0.254")); err == nil {
		t.Error("PortForIP should reject a derived port > 65535")
	}
	// No collisions across every usable host in the /24 (.2..254).
	seen := map[uint32]string{}
	for i := 2; i <= 254; i++ {
		ip := net.ParseIP("172.31.0." + strconv.Itoa(i))
		p, err := PortForIP(30000, network, ip)
		if err != nil {
			t.Fatalf("PortForIP(%v): %v", ip, err)
		}
		if prev, dup := seen[p]; dup {
			t.Fatalf("port collision %d: %v and %v", p, prev, ip)
		}
		seen[p] = ip.String()
	}
}

// TestNewManagerRejectsPortOverflow proves the range guard fires when a pod IP is set
// and the base would push the top offset past 65535, but is skipped when DNAT is off.
func TestNewManagerRejectsPortOverflow(t *testing.T) {
	if _, err := NewManager(&fakeRunner{}, "br0", "172.31.0.0/24", "10.42.0.9", 65500, 0); err == nil {
		t.Error("NewManager should reject a port base that overflows with a pod IP set")
	}
	// The same base is fine when DNAT is disabled (empty pod IP): no derivation happens.
	if _, err := NewManager(&fakeRunner{}, "br0", "172.31.0.0/24", "", 65500, 0); err != nil {
		t.Errorf("NewManager with no pod IP should not validate port base, got %v", err)
	}
}

// TestEnsureDNATAndRelease asserts each EnsureDNAT/RemoveDNAT triggers exactly one
// `nft -f` apply carrying the expected DNAT rule, and that ReleaseTap drops the entry.
func TestEnsureDNATAndRelease(t *testing.T) {
	fr := &fakeRunner{}
	m, err := NewManager(fr, "br0", "172.31.0.0/24", "10.42.0.9", 30000, 0)
	if err != nil {
		t.Fatalf("NewManager: %v", err)
	}
	ip := net.ParseIP("172.31.0.2")
	if err := m.EnsureDNAT(context.Background(), ip, 8080); err != nil {
		t.Fatalf("EnsureDNAT: %v", err)
	}
	if n := countNftApplies(fr); n != 1 {
		t.Errorf("EnsureDNAT nft applies = %d want 1", n)
	}
	if last := lastNftContent(t, fr); !strings.Contains(last, "dnat ip to 172.31.0.2:8080") {
		t.Errorf("EnsureDNAT ruleset missing the VM rule:\n%s", last)
	}
	// ReleaseTap folds in RemoveDNAT: the entry is dropped and the table re-applied.
	m.ReleaseTap(context.Background(), ip)
	if last := lastNftContent(t, fr); strings.Contains(last, "dnat ip to 172.31.0.2:8080") {
		t.Errorf("ReleaseTap should have dropped the DNAT rule:\n%s", last)
	}
}

// TestEnsureDNATDisabledIsNoop asserts that with no pod IP, EnsureDNAT installs nothing.
func TestEnsureDNATDisabledIsNoop(t *testing.T) {
	fr := &fakeRunner{}
	m, err := NewManager(fr, "br0", "172.31.0.0/24", "", 30000, 0)
	if err != nil {
		t.Fatalf("NewManager: %v", err)
	}
	if err := m.EnsureDNAT(context.Background(), net.ParseIP("172.31.0.2"), 8080); err != nil {
		t.Fatalf("EnsureDNAT: %v", err)
	}
	if n := countNftApplies(fr); n != 0 {
		t.Errorf("EnsureDNAT with no pod IP should apply nothing, got %d applies", n)
	}
}

// TestEndpointProjection asserts Endpoint returns (podIP, derivedPort) when DNAT is on
// and the tap IP unchanged when it is off.
func TestEndpointProjection(t *testing.T) {
	fr := &fakeRunner{}
	on, _ := NewManager(fr, "br0", "172.31.0.0/24", "10.42.0.9", 30000, 0)
	gotIP, gotPort := on.Endpoint(net.ParseIP("172.31.0.2"), 8080)
	if gotIP != "10.42.0.9" || gotPort != 30002 {
		t.Errorf("Endpoint(on) = %s:%d want 10.42.0.9:30002", gotIP, gotPort)
	}
	off, _ := NewManager(fr, "br0", "172.31.0.0/24", "", 30000, 0)
	gotIP, gotPort = off.Endpoint(net.ParseIP("172.31.0.2"), 8080)
	if gotIP != "172.31.0.2" || gotPort != 8080 {
		t.Errorf("Endpoint(off) = %s:%d want the tap 172.31.0.2:8080", gotIP, gotPort)
	}
}

func TestNftTeardownArgs(t *testing.T) {
	got := nftTeardownArgs()
	want := []string{"nft", "delete", "table", "inet", "embervm_serving"}
	if fmt.Sprint(got) != fmt.Sprint(want) {
		t.Fatalf("nftTeardownArgs: got %v want %v", got, want)
	}
}

func TestEnsureNetworkTolLeratesExistingBridge(t *testing.T) {
	// The first bridge-add fails "File exists" (a daemon restart re-enters); EnsureNetwork
	// must treat that as idempotent success and still install nftables.
	fr := &fakeRunner{failOn: map[string]error{
		"ip link": errors.New("RTNETLINK answers: File exists"),
	}}
	m, err := NewManager(fr, "br0", "172.31.0.0/24", "", 30000, 0)
	if err != nil {
		t.Fatalf("NewManager: %v", err)
	}
	if err := m.EnsureNetwork(context.Background()); err != nil {
		t.Fatalf("EnsureNetwork should tolerate existing bridge, got %v", err)
	}
	// nft -f must still have been invoked (the ruleset apply is not skipped), and the
	// ip_forward sysctl must have been set (the routed DNAT path needs it).
	sawNft, sawSysctl := false, false
	for _, c := range fr.calls {
		if c[0] == "nft" {
			sawNft = true
		}
		if c[0] == "sysctl" && strings.Join(c, " ") == "sysctl -w net.ipv4.ip_forward=1" {
			sawSysctl = true
		}
	}
	if !sawNft {
		t.Errorf("EnsureNetwork did not apply nftables; calls: %v", fr.argvStrings())
	}
	if !sawSysctl {
		t.Errorf("EnsureNetwork did not enable ip_forward; calls: %v", fr.argvStrings())
	}
	// With no pod IP, the applied ruleset is the plain v1 forward posture (no DNAT).
	if rs := lastNftContent(t, fr); strings.Contains(rs, "serving_dnat") {
		t.Errorf("EnsureNetwork with no pod IP should apply no DNAT chain:\n%s", rs)
	}
}

func TestAllocateAndReleaseTap(t *testing.T) {
	fr := &fakeRunner{}
	m, err := NewManager(fr, "br0", "172.31.0.0/24", "", 30000, 0)
	if err != nil {
		t.Fatalf("NewManager: %v", err)
	}
	tap, ip, err := m.AllocateTap(context.Background())
	if err != nil {
		t.Fatalf("AllocateTap: %v", err)
	}
	// First VM gets .2 (.1 is the gateway/bridge).
	if ip.String() != "172.31.0.2" {
		t.Errorf("first allocation = %s, want 172.31.0.2", ip)
	}
	if tap != TapNameForIP(ip) {
		t.Errorf("tap name %q != deterministic %q", tap, TapNameForIP(ip))
	}
	// Releasing frees the IP for reuse (lowest-free hands it back out).
	m.ReleaseTap(context.Background(), ip)
	_, ip2, err := m.AllocateTap(context.Background())
	if err != nil {
		t.Fatalf("AllocateTap after release: %v", err)
	}
	if ip2.String() != "172.31.0.2" {
		t.Errorf("reuse allocation = %s, want 172.31.0.2 (freed IP reused)", ip2)
	}
}

// TestAllocateTapDeletesStaleTapBeforeCreate locks in the idempotent-create fix:
// both allocation paths must issue `ip link del <tap>` BEFORE `ip tuntap add <tap>`,
// so a tap orphaned by a prior VM whose setup failed after tap creation (e.g. a
// downstream volume attach) is cleared instead of wedging the recreate with EBUSY
// (the 2026-07-20 demo-postgres relight wedge that 503'd jomcgi.dev/health).
func TestAllocateTapDeletesStaleTapBeforeCreate(t *testing.T) {
	assertDelBeforeAdd := func(t *testing.T, calls [][]string, tap string) {
		t.Helper()
		delIdx, addIdx := -1, -1
		for i, c := range calls {
			if len(c) >= 4 && c[0] == "ip" && c[1] == "link" && c[2] == "del" && c[3] == tap {
				if delIdx == -1 {
					delIdx = i
				}
			}
			if len(c) >= 5 && c[0] == "ip" && c[1] == "tuntap" && c[2] == "add" && c[4] == tap {
				addIdx = i
			}
		}
		if delIdx == -1 {
			t.Fatalf("no `ip link del %s` recorded; calls: %v", tap, calls)
		}
		if addIdx == -1 {
			t.Fatalf("no `ip tuntap add ... %s` recorded; calls: %v", tap, calls)
		}
		if delIdx > addIdx {
			t.Errorf("`ip link del %s` (call %d) must precede `ip tuntap add` (call %d)", tap, delIdx, addIdx)
		}
	}

	t.Run("AllocateTap", func(t *testing.T) {
		fr := &fakeRunner{}
		m, err := NewManager(fr, "br0", "172.31.0.0/24", "", 30000, 0)
		if err != nil {
			t.Fatalf("NewManager: %v", err)
		}
		tap, _, err := m.AllocateTap(context.Background())
		if err != nil {
			t.Fatalf("AllocateTap: %v", err)
		}
		assertDelBeforeAdd(t, fr.calls, tap)
	})

	t.Run("AllocateTapForIP", func(t *testing.T) {
		fr := &fakeRunner{}
		m, err := NewManager(fr, "br0", "172.31.0.0/24", "", 30000, 0)
		if err != nil {
			t.Fatalf("NewManager: %v", err)
		}
		pin := net.ParseIP("172.31.0.2")
		tap, err := m.AllocateTapForIP(context.Background(), pin)
		if err != nil {
			t.Fatalf("AllocateTapForIP: %v", err)
		}
		assertDelBeforeAdd(t, fr.calls, tap)
	})
}

// TestAllocateTapForIPPinsAndConflicts runs with tapPrealloc > 0 (a 1-tap pool
// covering only .2) so the pin .7 is NOT pool-origin: AllocateTapForIP must take the
// on-demand alloc.reserve() branch for it (exactly as with prealloc disabled), while
// the separately pooled .2 stays untouched and available to a plain AllocateTap.
// This asserts the pooled-pin-vs-allocator-conflict interaction: a relight pin
// outside the pool must still conflict correctly against the allocator, and must
// not accidentally consult or disturb the pool.
func TestAllocateTapForIPPinsAndConflicts(t *testing.T) {
	fr := &fakeRunner{}
	m, err := NewManager(fr, "br0", "172.31.0.0/24", "", 30000, 1)
	if err != nil {
		t.Fatalf("NewManager: %v", err)
	}
	if err := m.EnsureNetwork(context.Background()); err != nil {
		t.Fatalf("EnsureNetwork: %v", err)
	}
	pin := net.ParseIP("172.31.0.7")
	tap, err := m.AllocateTapForIP(context.Background(), pin)
	if err != nil {
		t.Fatalf("AllocateTapForIP: %v", err)
	}
	if tap != TapNameForIP(pin) {
		t.Errorf("pinned tap name %q != %q", tap, TapNameForIP(pin))
	}
	// Re-pinning the same live IP conflicts (two live VMs cannot hold one IP), same
	// as with prealloc disabled: the pin path outside the pool is untouched.
	if _, err := m.AllocateTapForIP(context.Background(), pin); err == nil {
		t.Error("AllocateTapForIP on an already-held IP should conflict")
	}
	// A plain AllocateTap must still draw the pooled .2 (untouched by the .7 pin),
	// not skip to some fresh on-demand address.
	_, ip, err := m.AllocateTap(context.Background())
	if err != nil {
		t.Fatalf("AllocateTap: %v", err)
	}
	if ip.String() != "172.31.0.2" {
		t.Errorf("pool draw after unrelated pin = %s, want the pooled 172.31.0.2", ip)
	}
}

func TestAllocateTapRollsBackOnIPFailure(t *testing.T) {
	// If a tap `ip` command fails mid-setup, the reserved IP must be released so a
	// retry can reuse it (no leak).
	fr := &fakeRunner{failOn: map[string]error{
		"ip link": errors.New("boom"), // the `ip link set ... master` step fails
	}}
	m, err := NewManager(fr, "br0", "172.31.0.0/24", "", 30000, 0)
	if err != nil {
		t.Fatalf("NewManager: %v", err)
	}
	if _, _, err := m.AllocateTap(context.Background()); err == nil {
		t.Fatal("AllocateTap should fail when an ip command fails")
	}
	// The failed IP (.2) must be free again: allocate once the failure is cleared.
	fr.failOn = nil
	_, ip, err := m.AllocateTap(context.Background())
	if err != nil {
		t.Fatalf("AllocateTap after cleared failure: %v", err)
	}
	if ip.String() != "172.31.0.2" {
		t.Errorf("rolled-back IP not reused: got %s want 172.31.0.2", ip)
	}
}

func TestNewManagerRejectsBadCIDR(t *testing.T) {
	for _, cidr := range []string{"not-a-cidr", "::1/128", "10.0.0.0/31"} {
		if _, err := NewManager(&fakeRunner{}, "br0", cidr, "", 30000, 0); err == nil {
			t.Errorf("NewManager(%q) should error", cidr)
		}
	}
}

func TestTapNameDeterministicAndBounded(t *testing.T) {
	ip := net.ParseIP("172.31.5.42")
	got := TapNameForIP(ip)
	if got != "emtap052a" {
		t.Errorf("TapNameForIP(172.31.5.42) = %q, want emtap052a", got)
	}
	if len(got) > 15 {
		t.Errorf("tap name %q exceeds the 15-char ifname limit", got)
	}
}

// ---- ADR embervm/014 decision 4: tap pre-provisioning ----------------------

// countCreateCalls counts `ip tuntap add` invocations, the on-demand tap-create step
// that a pool draw must NOT issue.
func countCreateCalls(f *fakeRunner) int {
	n := 0
	for _, c := range f.calls {
		if len(c) >= 3 && c[0] == "ip" && c[1] == "tuntap" && c[2] == "add" {
			n++
		}
	}
	return n
}

// TestEnsureNetworkPrecreatesPool asserts EnsureNetwork with tapPrealloc > 0 creates
// exactly that many taps, attached to the bridge but left DOWN (no `ip link set ...
// up` for a pool tap), and del-before-add is exercised for each (the #3745 repair).
func TestEnsureNetworkPrecreatesPool(t *testing.T) {
	fr := &fakeRunner{}
	m, err := NewManager(fr, "br0", "172.31.0.0/24", "", 30000, 3)
	if err != nil {
		t.Fatalf("NewManager: %v", err)
	}
	if err := m.EnsureNetwork(context.Background()); err != nil {
		t.Fatalf("EnsureNetwork: %v", err)
	}
	if n := countCreateCalls(fr); n != 3 {
		t.Errorf("precreate calls = %d, want 3", n)
	}
	wantTaps := []string{TapNameForIP(net.ParseIP("172.31.0.2")), TapNameForIP(net.ParseIP("172.31.0.3")), TapNameForIP(net.ParseIP("172.31.0.4"))}
	for _, tap := range wantTaps {
		delIdx, addIdx, masterIdx, upIdx := -1, -1, -1, -1
		for i, c := range fr.calls {
			if len(c) >= 4 && c[0] == "ip" && c[1] == "link" && c[2] == "del" && c[3] == tap {
				delIdx = i
			}
			if len(c) >= 5 && c[0] == "ip" && c[1] == "tuntap" && c[2] == "add" && c[4] == tap {
				addIdx = i
			}
			if len(c) >= 4 && c[0] == "ip" && c[1] == "link" && c[2] == "set" && c[3] == tap && len(c) >= 5 && c[4] == "master" {
				masterIdx = i
			}
			if len(c) == 5 && c[0] == "ip" && c[1] == "link" && c[2] == "set" && c[3] == tap && c[4] == "up" {
				upIdx = i
			}
		}
		if delIdx == -1 || addIdx == -1 {
			t.Fatalf("tap %s: missing del (%d) or add (%d); calls: %v", tap, delIdx, addIdx, fr.argvStrings())
		}
		if delIdx > addIdx {
			t.Errorf("tap %s: del-before-add violated (del=%d add=%d)", tap, delIdx, addIdx)
		}
		if masterIdx == -1 {
			t.Errorf("tap %s: not attached to bridge (no `ip link set %s master br0`)", tap, tap)
		}
		if upIdx != -1 {
			t.Errorf("tap %s: pre-created tap must be left DOWN, but `ip link set %s up` was issued", tap, tap)
		}
	}
}

// TestAllocateTapDrawsFromPoolWithoutCreate asserts a pool draw brings the link up
// with no `ip tuntap add`/`master` create-attach work, unlike on-demand allocation.
func TestAllocateTapDrawsFromPoolWithoutCreate(t *testing.T) {
	fr := &fakeRunner{}
	m, err := NewManager(fr, "br0", "172.31.0.0/24", "", 30000, 2)
	if err != nil {
		t.Fatalf("NewManager: %v", err)
	}
	if err := m.EnsureNetwork(context.Background()); err != nil {
		t.Fatalf("EnsureNetwork: %v", err)
	}
	createsBefore := countCreateCalls(fr)
	tap, ip, err := m.AllocateTap(context.Background())
	if err != nil {
		t.Fatalf("AllocateTap: %v", err)
	}
	if ip.String() != "172.31.0.2" {
		t.Errorf("pool draw = %s, want the first pre-created IP 172.31.0.2", ip)
	}
	if tap != TapNameForIP(ip) {
		t.Errorf("tap name %q != deterministic %q", tap, TapNameForIP(ip))
	}
	if got := countCreateCalls(fr); got != createsBefore {
		t.Errorf("AllocateTap from a non-empty pool issued a create call: before=%d after=%d", createsBefore, got)
	}
	// The draw brought the tap up.
	sawUp := false
	for _, c := range fr.calls {
		if fmt.Sprint(c) == fmt.Sprint(tapUpArgs(tap)) {
			sawUp = true
		}
	}
	if !sawUp {
		t.Errorf("AllocateTap pool draw did not bring the tap up; calls: %v", fr.argvStrings())
	}
}

// TestPoolDrainedThenRefilledOnRelease drains a 2-tap pool with two AllocateTap
// calls, then asserts ReleaseTap returns a tap to the pool (down, no delete) so a
// third AllocateTap reuses it without any create call.
func TestPoolDrainedThenRefilledOnRelease(t *testing.T) {
	fr := &fakeRunner{}
	m, err := NewManager(fr, "br0", "172.31.0.0/24", "", 30000, 2)
	if err != nil {
		t.Fatalf("NewManager: %v", err)
	}
	if err := m.EnsureNetwork(context.Background()); err != nil {
		t.Fatalf("EnsureNetwork: %v", err)
	}
	_, ip1, err := m.AllocateTap(context.Background())
	if err != nil {
		t.Fatalf("AllocateTap 1: %v", err)
	}
	_, ip2, err := m.AllocateTap(context.Background())
	if err != nil {
		t.Fatalf("AllocateTap 2: %v", err)
	}
	if ip1.String() == ip2.String() {
		t.Fatalf("pool draws returned the same IP twice: %s", ip1)
	}
	createsAfterDrain := countCreateCalls(fr)

	// Release ip1: prealloc is on, so it must go DOWN and back to the pool, not be
	// deleted. Only inspect calls issued FROM HERE: fr.calls already carries
	// precreatePool's own del-before-add repair for ip1's tap (EnsureNetwork ran
	// above), and scanning the whole history would misidentify that boot-time
	// idempotent delete as a release-time one.
	callsBeforeRelease := len(fr.calls)
	m.ReleaseTap(context.Background(), ip1)
	releaseCalls := fr.calls[callsBeforeRelease:]
	tap1 := TapNameForIP(ip1)
	for _, c := range releaseCalls {
		if len(c) >= 4 && c[0] == "ip" && c[1] == "link" && c[2] == "del" && c[3] == tap1 {
			t.Errorf("ReleaseTap deleted a pooled tap %s; prealloc mode must return it, not delete it", tap1)
		}
	}
	sawDown := false
	for _, c := range releaseCalls {
		if fmt.Sprint(c) == fmt.Sprint(tapDownArgs(tap1)) {
			sawDown = true
		}
	}
	if !sawDown {
		t.Errorf("ReleaseTap did not bring the pooled tap down; calls: %v", fr.argvStrings())
	}

	// A third AllocateTap must reuse ip1 from the pool with no new create call.
	_, ip3, err := m.AllocateTap(context.Background())
	if err != nil {
		t.Fatalf("AllocateTap 3: %v", err)
	}
	if ip3.String() != ip1.String() {
		t.Errorf("refilled draw = %s, want the just-released %s", ip3, ip1)
	}
	if got := countCreateCalls(fr); got != createsAfterDrain {
		t.Errorf("reused pool tap issued a create call: before=%d after=%d", createsAfterDrain, got)
	}
}

// TestAllocateTapFallsBackWhenPoolExhausted asserts a drained pool falls through to
// on-demand creation (today's AllocateTap path) rather than erroring, and that the
// fallback IP is a fresh one, not a pool member.
func TestAllocateTapFallsBackWhenPoolExhausted(t *testing.T) {
	fr := &fakeRunner{}
	m, err := NewManager(fr, "br0", "172.31.0.0/24", "", 30000, 1)
	if err != nil {
		t.Fatalf("NewManager: %v", err)
	}
	if err := m.EnsureNetwork(context.Background()); err != nil {
		t.Fatalf("EnsureNetwork: %v", err)
	}
	_, poolIP, err := m.AllocateTap(context.Background())
	if err != nil {
		t.Fatalf("AllocateTap (pool draw): %v", err)
	}
	createsBeforeFallback := countCreateCalls(fr)
	tap, fallbackIP, err := m.AllocateTap(context.Background())
	if err != nil {
		t.Fatalf("AllocateTap (fallback): %v", err)
	}
	if fallbackIP.String() == poolIP.String() {
		t.Fatalf("fallback allocation reused the checked-out pool IP %s", poolIP)
	}
	if got := countCreateCalls(fr); got != createsBeforeFallback+1 {
		t.Errorf("fallback allocation did not create a fresh tap: creates before=%d after=%d", createsBeforeFallback, got)
	}
	if tap != TapNameForIP(fallbackIP) {
		t.Errorf("fallback tap name %q != deterministic %q", tap, TapNameForIP(fallbackIP))
	}
}

// TestReleaseTapDeletesWhenPreallocDisabled locks in that ReleaseTap's pool-return
// behaviour is gated strictly on tapPrealloc > 0: with it unset (0, the default),
// release still deletes the tap and frees the IP to the allocator exactly as before
// this feature existed.
func TestReleaseTapDeletesWhenPreallocDisabled(t *testing.T) {
	fr := &fakeRunner{}
	m, err := NewManager(fr, "br0", "172.31.0.0/24", "", 30000, 0)
	if err != nil {
		t.Fatalf("NewManager: %v", err)
	}
	_, ip, err := m.AllocateTap(context.Background())
	if err != nil {
		t.Fatalf("AllocateTap: %v", err)
	}
	m.ReleaseTap(context.Background(), ip)
	tap := TapNameForIP(ip)
	sawDelete := false
	for _, c := range fr.calls {
		if len(c) >= 4 && c[0] == "ip" && c[1] == "link" && c[2] == "del" && c[3] == tap {
			sawDelete = true
		}
	}
	if !sawDelete {
		t.Errorf("ReleaseTap with prealloc disabled must delete the tap; calls: %v", fr.argvStrings())
	}
	// The IP is free again in the allocator (not held for a pool that does not exist).
	_, ip2, err := m.AllocateTap(context.Background())
	if err != nil {
		t.Fatalf("AllocateTap after release: %v", err)
	}
	if ip2.String() != ip.String() {
		t.Errorf("released IP not reused: got %s want %s", ip2, ip)
	}
}

// TestAllocateTapForIPDrawsPooledPin asserts a relight pin (D-R3.4.1) whose recorded
// IP happens to be idle in the prealloc pool is drawn out of the pool (bring up,
// no create/attach), rather than failing "already allocated" against the
// allocator's permanent pool reservation.
func TestAllocateTapForIPDrawsPooledPin(t *testing.T) {
	fr := &fakeRunner{}
	m, err := NewManager(fr, "br0", "172.31.0.0/24", "", 30000, 2)
	if err != nil {
		t.Fatalf("NewManager: %v", err)
	}
	if err := m.EnsureNetwork(context.Background()); err != nil {
		t.Fatalf("EnsureNetwork: %v", err)
	}
	pin := net.ParseIP("172.31.0.2") // the first IP precreatePool reserved.
	createsBefore := countCreateCalls(fr)
	tap, err := m.AllocateTapForIP(context.Background(), pin)
	if err != nil {
		t.Fatalf("AllocateTapForIP: %v", err)
	}
	if tap != TapNameForIP(pin) {
		t.Errorf("tap name %q != deterministic %q", tap, TapNameForIP(pin))
	}
	if got := countCreateCalls(fr); got != createsBefore {
		t.Errorf("AllocateTapForIP on a pooled IP issued a create call: before=%d after=%d", createsBefore, got)
	}
	// The pool must no longer offer this IP: a subsequent AllocateTap draw gets the
	// OTHER pooled IP, not this one.
	_, drawn, err := m.AllocateTap(context.Background())
	if err != nil {
		t.Fatalf("AllocateTap: %v", err)
	}
	if drawn.String() == pin.String() {
		t.Errorf("pool still offered the pinned IP %s after AllocateTapForIP drew it", pin)
	}
}

// argvFailer wraps a Runner and fails the FIRST call whose argv exactly matches
// want, passing every other call straight through to inner. It exists because
// fakeRunner.failOn only keys on the first two argv tokens ("name arg0"), too
// coarse to fail one specific `ip link set <tap> up` or `ip tuntap add ...
// <tap>` call among several without also breaking every other call that shares
// those tokens (precreate's `ip link set <tap> master <bridge>` is also "ip
// link", for instance).
type argvFailer struct {
	inner  *fakeRunner
	want   []string
	err    error
	failed bool // fires only once, so a retry after the failure succeeds
	fired  bool
}

func (f *argvFailer) Run(ctx context.Context, name string, args ...string) ([]byte, error) {
	call := append([]string{name}, args...)
	if !f.failed && argvEqual(call, f.want) {
		f.failed = true
		f.fired = true
		f.inner.calls = append(f.inner.calls, call)
		return nil, f.err
	}
	return f.inner.Run(ctx, name, args...)
}

func argvEqual(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

// TestReleaseTapDeletesFallbackTapUnderPrealloc locks in the PR4-review fix: a tap
// AllocateTap created ON DEMAND because the pool was exhausted is NOT pool-origin,
// so releasing it (even with tapPrealloc > 0) must delete the tap and free the
// allocator reservation exactly like the prealloc-disabled path, not return it to
// the pool. Gating release on "tapPrealloc > 0" alone (the pre-fix behaviour) would
// let the idle pool grow past tapPrealloc entries and the allocator accumulate
// reservations above the brick's slot ceiling.
func TestReleaseTapDeletesFallbackTapUnderPrealloc(t *testing.T) {
	fr := &fakeRunner{}
	m, err := NewManager(fr, "br0", "172.31.0.0/24", "", 30000, 1)
	if err != nil {
		t.Fatalf("NewManager: %v", err)
	}
	if err := m.EnsureNetwork(context.Background()); err != nil {
		t.Fatalf("EnsureNetwork: %v", err)
	}
	// Drain the 1-tap pool, then force a second AllocateTap through the on-demand
	// fallback path.
	_, poolIP, err := m.AllocateTap(context.Background())
	if err != nil {
		t.Fatalf("AllocateTap (pool draw): %v", err)
	}
	_, fallbackIP, err := m.AllocateTap(context.Background())
	if err != nil {
		t.Fatalf("AllocateTap (fallback): %v", err)
	}
	if fallbackIP.String() == poolIP.String() {
		t.Fatalf("fallback allocation reused the checked-out pool IP %s", poolIP)
	}

	m.ReleaseTap(context.Background(), fallbackIP)
	fallbackTap := TapNameForIP(fallbackIP)
	sawDelete := false
	for _, c := range fr.calls {
		if len(c) >= 4 && c[0] == "ip" && c[1] == "link" && c[2] == "del" && c[3] == fallbackTap {
			sawDelete = true
		}
	}
	if !sawDelete {
		t.Errorf("ReleaseTap on a fallback-created tap must delete it even with prealloc on; calls: %v", fr.argvStrings())
	}
	// The allocator reservation must be freed too: a fresh AllocateTap (poolIP is
	// still checked out, so this must come from the allocator, not the pool) reuses
	// fallbackIP rather than advancing past it or erroring.
	_, reused, err := m.AllocateTap(context.Background())
	if err != nil {
		t.Fatalf("AllocateTap after release: %v", err)
	}
	if reused.String() != fallbackIP.String() {
		t.Errorf("released fallback IP not freed to the allocator: got %s want %s", reused, fallbackIP)
	}
}

// TestPrecreatePoolDegradesOnTransientFailure locks in the PR4-review fix: a single
// tap's precreate failure (a transient ip error, e.g. an EBUSY racing a prior
// incarnation's teardown) must be best-effort, EnsureNetwork still succeeds with a
// SHORTER pool, never propagating the error and crash-looping the brick.
func TestPrecreatePoolDegradesOnTransientFailure(t *testing.T) {
	// tapPrealloc is 2 (.2 and .3); fail only .3's `ip tuntap add`, leaving .2 to
	// precreate successfully.
	failTap := TapNameForIP(net.ParseIP("172.31.0.3"))
	inner := &fakeRunner{}
	failer := &argvFailer{
		inner: inner,
		want:  []string{"ip", "tuntap", "add", "dev", failTap, "mode", "tap"},
		err:   errors.New("simulated transient EBUSY"),
	}
	m, err := NewManager(failer, "br0", "172.31.0.0/24", "", 30000, 2)
	if err != nil {
		t.Fatalf("NewManager: %v", err)
	}
	if err := m.EnsureNetwork(context.Background()); err != nil {
		t.Fatalf("EnsureNetwork must degrade to a shorter pool on a transient precreate failure, not error: %v", err)
	}
	if !failer.fired {
		t.Fatal("test did not actually exercise the targeted precreate failure")
	}
	// Exactly the one successful precreate (.2) is in the pool; draw it.
	tap, ip, err := m.AllocateTap(context.Background())
	if err != nil {
		t.Fatalf("AllocateTap: %v", err)
	}
	if ip.String() != "172.31.0.2" {
		t.Errorf("surviving pool member = %s, want 172.31.0.2 (the one precreate that succeeded)", ip)
	}
	if tap != TapNameForIP(ip) {
		t.Errorf("tap name %q != deterministic %q", tap, TapNameForIP(ip))
	}
	// A further draw falls back to on-demand creation (the pool is exhausted at 1
	// member, not the configured 2), proving the short pool degrades cleanly.
	createsBefore := countCreateCalls(inner)
	_, ip2, err := m.AllocateTap(context.Background())
	if err != nil {
		t.Fatalf("AllocateTap (fallback after short pool): %v", err)
	}
	if ip2.String() == ip.String() {
		t.Fatalf("fallback reused the checked-out pool IP %s", ip)
	}
	if got := countCreateCalls(inner); got != createsBefore+1 {
		t.Errorf("expected fallback to create a fresh tap: creates before=%d after=%d", createsBefore, got)
	}
}

// TestAllocateTapPushesBackPoolTapOnLinkUpFailure locks in AllocateTap's pool-draw
// failure path: when bringing a popped pool tap's link up fails, the IP must be
// pushed back into the pool (not lost) before AllocateTap falls through to
// on-demand creation, so a transient failure degrades gracefully instead of leaking
// a pool slot.
func TestAllocateTapPushesBackPoolTapOnLinkUpFailure(t *testing.T) {
	poolTap := TapNameForIP(net.ParseIP("172.31.0.2"))
	inner := &fakeRunner{}
	m, err := NewManager(inner, "br0", "172.31.0.0/24", "", 30000, 1)
	if err != nil {
		t.Fatalf("NewManager: %v", err)
	}
	if err := m.EnsureNetwork(context.Background()); err != nil {
		t.Fatalf("EnsureNetwork: %v", err)
	}
	// Fail only the pool draw's `ip link set <poolTap> up`; precreate already ran
	// (above) with a clean runner, so this cannot touch precreate's own calls.
	failer := &argvFailer{inner: inner, want: tapUpArgs(poolTap), err: errors.New("simulated link-up failure")}
	m.runner = failer

	_, ip, err := m.AllocateTap(context.Background())
	if err != nil {
		t.Fatalf("AllocateTap should fall back to on-demand creation, not error: %v", err)
	}
	if !failer.fired {
		t.Fatal("test did not actually exercise the pool tap's link-up path")
	}
	if ip.String() == "172.31.0.2" {
		t.Errorf("AllocateTap returned the pool IP %s despite its link-up failing; fallback should allocate a different IP", ip)
	}
	// The pool IP must have been pushed back (not lost): switching back to a clean
	// runner, a further AllocateTap draws it again from the pool.
	m.runner = inner
	_, ip2, err := m.AllocateTap(context.Background())
	if err != nil {
		t.Fatalf("AllocateTap after push-back: %v", err)
	}
	if ip2.String() != "172.31.0.2" {
		t.Errorf("pool IP not pushed back after link-up failure: got %s want 172.31.0.2", ip2)
	}
}
