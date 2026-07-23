package serving

import (
	"net"
	"testing"
)

func mustAllocator(t *testing.T, cidr string) *ipAllocator {
	t.Helper()
	gw, ipnet, err := parseServingCIDR(cidr)
	if err != nil {
		t.Fatalf("parseServingCIDR(%q): %v", cidr, err)
	}
	return newIPAllocator(ipnet, gw)
}

func TestAllocatorLowestFreeAndReuse(t *testing.T) {
	a := mustAllocator(t, "172.31.0.0/24")
	// First three allocations are .2, .3, .4 (.1 is the gateway).
	for _, want := range []string{"172.31.0.2", "172.31.0.3", "172.31.0.4"} {
		ip, err := a.allocate()
		if err != nil {
			t.Fatalf("allocate: %v", err)
		}
		if ip.String() != want {
			t.Fatalf("allocate = %s want %s", ip, want)
		}
	}
	// Release .3; the next allocation reuses it (lowest-free).
	a.release(net.ParseIP("172.31.0.3"))
	ip, err := a.allocate()
	if err != nil {
		t.Fatalf("allocate after release: %v", err)
	}
	if ip.String() != "172.31.0.3" {
		t.Fatalf("reuse = %s want 172.31.0.3", ip)
	}
}

// TestAllocatorFreeCount checks the O(1) freelist size the tap-pressure predicate
// reads (ADR embervm/014 decision 3): it starts at the full usable range, drops
// as IPs are allocated, rises on release, and hits 0 exactly at exhaustion.
func TestAllocatorFreeCount(t *testing.T) {
	// A /29 has usable VM range .2..6 (five addresses), gateway .1, broadcast .7.
	a := mustAllocator(t, "172.31.0.0/29")
	if got := a.freeCount(); got != 5 {
		t.Fatalf("initial freeCount = %d want 5", got)
	}
	first, err := a.allocate()
	if err != nil {
		t.Fatalf("allocate: %v", err)
	}
	if got := a.freeCount(); got != 4 {
		t.Fatalf("freeCount after one allocate = %d want 4", got)
	}
	// Drain the rest; the last free count is 0 and the next allocate errors.
	for i := 0; i < 4; i++ {
		if _, err := a.allocate(); err != nil {
			t.Fatalf("allocate %d: %v", i, err)
		}
	}
	if got := a.freeCount(); got != 0 {
		t.Fatalf("freeCount at exhaustion = %d want 0", got)
	}
	if _, err := a.allocate(); err == nil {
		t.Error("allocate on exhausted subnet should error")
	}
	// Releasing one raises the free count back to 1 (a released tap is reusable).
	a.release(first)
	if got := a.freeCount(); got != 1 {
		t.Fatalf("freeCount after release = %d want 1", got)
	}
}

func TestAllocatorSkipsGatewayAndBroadcast(t *testing.T) {
	// A /29 has hosts .1..6 with .0 network and .7 broadcast; gateway is .1, so the
	// usable VM range is .2..6 (five addresses).
	a := mustAllocator(t, "172.31.0.0/29")
	got := map[string]bool{}
	for i := 0; i < 5; i++ {
		ip, err := a.allocate()
		if err != nil {
			t.Fatalf("allocate %d: %v", i, err)
		}
		got[ip.String()] = true
	}
	for _, want := range []string{"172.31.0.2", "172.31.0.3", "172.31.0.4", "172.31.0.5", "172.31.0.6"} {
		if !got[want] {
			t.Errorf("expected %s to be allocatable", want)
		}
	}
	if got["172.31.0.1"] {
		t.Error("gateway .1 must never be allocated to a VM")
	}
	if got["172.31.0.7"] {
		t.Error("broadcast .7 must never be allocated to a VM")
	}
	// The subnet is now exhausted.
	if _, err := a.allocate(); err == nil {
		t.Error("allocate on an exhausted subnet should error")
	}
}

func TestAllocatorReservePin(t *testing.T) {
	a := mustAllocator(t, "172.31.0.0/24")
	// Reserve a specific IP (the D-R3.4.1 relight pin).
	pin := net.ParseIP("172.31.0.50")
	if err := a.reserve(pin); err != nil {
		t.Fatalf("reserve: %v", err)
	}
	// Re-reserving the same IP conflicts.
	if err := a.reserve(pin); err == nil {
		t.Error("re-reserving a held IP should conflict")
	}
	// Releasing lets it be reserved again.
	a.release(pin)
	if err := a.reserve(pin); err != nil {
		t.Errorf("reserve after release: %v", err)
	}
	// Out-of-range reservations error.
	if err := a.reserve(net.ParseIP("172.31.1.5")); err == nil {
		t.Error("reserving outside the CIDR should error")
	}
	if err := a.reserve(net.ParseIP("172.31.0.1")); err == nil {
		t.Error("reserving the gateway should error")
	}
}

func TestParseServingCIDR(t *testing.T) {
	gw, ipnet, err := parseServingCIDR("172.31.0.0/24")
	if err != nil {
		t.Fatalf("parseServingCIDR: %v", err)
	}
	if gw.String() != "172.31.0.1" {
		t.Errorf("gateway = %s want 172.31.0.1", gw)
	}
	if ipnet.String() != "172.31.0.0/24" {
		t.Errorf("ipnet = %s want 172.31.0.0/24", ipnet)
	}
}

func TestIPArithmetic(t *testing.T) {
	if nextIP(net.ParseIP("172.31.0.255").To4()).String() != "172.31.1.0" {
		t.Error("nextIP octet carry failed")
	}
	if prevIP(net.ParseIP("172.31.1.0").To4()).String() != "172.31.0.255" {
		t.Error("prevIP octet borrow failed")
	}
	_, ipnet, _ := net.ParseCIDR("172.31.0.0/24")
	if broadcastIP(ipnet).String() != "172.31.0.255" {
		t.Error("broadcastIP failed")
	}
}
