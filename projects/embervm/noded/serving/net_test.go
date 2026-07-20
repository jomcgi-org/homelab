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
	if _, err := NewManager(&fakeRunner{}, "br0", "172.31.0.0/24", "10.42.0.9", 65500); err == nil {
		t.Error("NewManager should reject a port base that overflows with a pod IP set")
	}
	// The same base is fine when DNAT is disabled (empty pod IP): no derivation happens.
	if _, err := NewManager(&fakeRunner{}, "br0", "172.31.0.0/24", "", 65500); err != nil {
		t.Errorf("NewManager with no pod IP should not validate port base, got %v", err)
	}
}

// TestEnsureDNATAndRelease asserts each EnsureDNAT/RemoveDNAT triggers exactly one
// `nft -f` apply carrying the expected DNAT rule, and that ReleaseTap drops the entry.
func TestEnsureDNATAndRelease(t *testing.T) {
	fr := &fakeRunner{}
	m, err := NewManager(fr, "br0", "172.31.0.0/24", "10.42.0.9", 30000)
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
	m, err := NewManager(fr, "br0", "172.31.0.0/24", "", 30000)
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
	on, _ := NewManager(fr, "br0", "172.31.0.0/24", "10.42.0.9", 30000)
	gotIP, gotPort := on.Endpoint(net.ParseIP("172.31.0.2"), 8080)
	if gotIP != "10.42.0.9" || gotPort != 30002 {
		t.Errorf("Endpoint(on) = %s:%d want 10.42.0.9:30002", gotIP, gotPort)
	}
	off, _ := NewManager(fr, "br0", "172.31.0.0/24", "", 30000)
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
	m, err := NewManager(fr, "br0", "172.31.0.0/24", "", 30000)
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
	m, err := NewManager(fr, "br0", "172.31.0.0/24", "", 30000)
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
		m, err := NewManager(fr, "br0", "172.31.0.0/24", "", 30000)
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
		m, err := NewManager(fr, "br0", "172.31.0.0/24", "", 30000)
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

func TestAllocateTapForIPPinsAndConflicts(t *testing.T) {
	fr := &fakeRunner{}
	m, err := NewManager(fr, "br0", "172.31.0.0/24", "", 30000)
	if err != nil {
		t.Fatalf("NewManager: %v", err)
	}
	pin := net.ParseIP("172.31.0.7")
	tap, err := m.AllocateTapForIP(context.Background(), pin)
	if err != nil {
		t.Fatalf("AllocateTapForIP: %v", err)
	}
	if tap != TapNameForIP(pin) {
		t.Errorf("pinned tap name %q != %q", tap, TapNameForIP(pin))
	}
	// Re-pinning the same live IP conflicts (two live VMs cannot hold one IP).
	if _, err := m.AllocateTapForIP(context.Background(), pin); err == nil {
		t.Error("AllocateTapForIP on an already-held IP should conflict")
	}
	// A fresh allocation must skip the pinned .7 (it is taken) but still return the
	// lowest OTHER free address (.2).
	_, ip, err := m.AllocateTap(context.Background())
	if err != nil {
		t.Fatalf("AllocateTap: %v", err)
	}
	if ip.String() != "172.31.0.2" {
		t.Errorf("fresh alloc after pin = %s, want 172.31.0.2", ip)
	}
}

func TestAllocateTapRollsBackOnIPFailure(t *testing.T) {
	// If a tap `ip` command fails mid-setup, the reserved IP must be released so a
	// retry can reuse it (no leak).
	fr := &fakeRunner{failOn: map[string]error{
		"ip link": errors.New("boom"), // the `ip link set ... master` step fails
	}}
	m, err := NewManager(fr, "br0", "172.31.0.0/24", "", 30000)
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
		if _, err := NewManager(&fakeRunner{}, "br0", cidr, "", 30000); err == nil {
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
