package serving

import (
	"context"
	"errors"
	"fmt"
	"net"
	"strings"
	"testing"
)

// fakeRunner records every command it is asked to run and returns canned
// output/errors keyed by the first two argv tokens, so tests assert the exact
// ip/nft invocations as data without touching the host.
type fakeRunner struct {
	calls  [][]string
	failOn map[string]error // key: "name arg0"; a matching call returns the error
}

func (f *fakeRunner) Run(_ context.Context, name string, args ...string) ([]byte, error) {
	call := append([]string{name}, args...)
	f.calls = append(f.calls, call)
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

// TestNftRuleset asserts the ingress-only ruleset as data: a dedicated table, a
// forward chain, established/related accept, and a drop of VM-originated NEW
// forwarding off the bridge interface.
func TestNftRuleset(t *testing.T) {
	rs := nftRuleset("br0")
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
	// The flush must come AFTER the add-table so the script is idempotent on first
	// apply (flushing a not-yet-created table errors).
	if strings.Index(rs, "flush table") < strings.Index(rs, "add table") {
		t.Error("nftRuleset: flush table must come after add table for first-apply idempotency")
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
	m, err := NewManager(fr, "br0", "172.31.0.0/24")
	if err != nil {
		t.Fatalf("NewManager: %v", err)
	}
	if err := m.EnsureNetwork(context.Background()); err != nil {
		t.Fatalf("EnsureNetwork should tolerate existing bridge, got %v", err)
	}
	// nft -f must still have been invoked (the ruleset apply is not skipped).
	sawNft := false
	for _, c := range fr.calls {
		if c[0] == "nft" {
			sawNft = true
		}
	}
	if !sawNft {
		t.Errorf("EnsureNetwork did not apply nftables; calls: %v", fr.argvStrings())
	}
}

func TestAllocateAndReleaseTap(t *testing.T) {
	fr := &fakeRunner{}
	m, err := NewManager(fr, "br0", "172.31.0.0/24")
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

func TestAllocateTapForIPPinsAndConflicts(t *testing.T) {
	fr := &fakeRunner{}
	m, err := NewManager(fr, "br0", "172.31.0.0/24")
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
	m, err := NewManager(fr, "br0", "172.31.0.0/24")
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
		if _, err := NewManager(&fakeRunner{}, "br0", cidr); err == nil {
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
