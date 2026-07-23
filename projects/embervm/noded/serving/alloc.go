package serving

import (
	"context"
	"fmt"
	"net"
	"os"
	"sync"
)

// ipAllocator hands out host IPs from the serving CIDR. It is an in-memory,
// lowest-free allocator: empty on daemon start (the live serving VMs a prior daemon
// held died with it, and the control plane reconciles them to banked/failed from
// post-restart inventory, so there is no live IP to recover). Allocation is
// lowest-free so IPs are reused densely; release returns an IP to the pool. The
// bank/relight IP pin (D-R3.4.1) is served by reserve(), which re-acquires a
// specific IP a banked snapshot recorded.
type ipAllocator struct {
	mu      sync.Mutex
	network *net.IPNet
	gateway net.IP
	// used marks host IPs currently allocated (keyed by 4-byte string form).
	used map[string]struct{}
	// first and last bound the usable host range [first, last], inclusive: first is
	// gateway+1 (the gateway .1 is the bridge, never handed to a VM), last is the
	// broadcast address minus one.
	first net.IP
	last  net.IP
}

// newIPAllocator builds an allocator over ipnet with gateway reserved (never
// allocated to a VM). The usable range is (gateway, broadcast).
func newIPAllocator(ipnet *net.IPNet, gateway net.IP) *ipAllocator {
	first := nextIP(gateway.To4())
	last := prevIP(broadcastIP(ipnet))
	return &ipAllocator{
		network: ipnet,
		gateway: gateway.To4(),
		used:    make(map[string]struct{}),
		first:   first,
		last:    last,
	}
}

// allocate returns the lowest free host IP in the usable range, or an error when the
// subnet is exhausted (the node's serving capacity is bounded by max_live_vms well
// below a /24, so exhaustion means a leak or a misconfigured CIDR).
func (a *ipAllocator) allocate() (net.IP, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	for ip := cloneIP(a.first); compareIP(ip, a.last) <= 0; ip = nextIP(ip) {
		key := string(ip)
		if _, taken := a.used[key]; !taken {
			a.used[key] = struct{}{}
			return cloneIP(ip), nil
		}
	}
	return nil, fmt.Errorf("serving: subnet %s exhausted", a.network)
}

// reserve re-acquires a SPECIFIC IP (the D-R3.4.1 relight pin). It errors when the IP
// is outside the usable range or already allocated (a conflict the caller maps to
// FAILED_PRECONDITION: two live VMs cannot hold one IP).
func (a *ipAllocator) reserve(ip net.IP) error {
	v4 := ip.To4()
	if v4 == nil {
		return fmt.Errorf("serving: reserve non-IPv4 %v", ip)
	}
	if compareIP(v4, a.first) < 0 || compareIP(v4, a.last) > 0 {
		return fmt.Errorf("serving: reserve %v outside usable range [%v, %v]", v4, a.first, a.last)
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	key := string(v4)
	if _, taken := a.used[key]; taken {
		return fmt.Errorf("serving: reserve %v already allocated", v4)
	}
	a.used[key] = struct{}{}
	return nil
}

// freeCount returns how many host IPs in the usable [first,last] range are not
// currently allocated. It is an O(1) read of already-maintained counters (the
// range size minus the used-set size), so a pressure check can query it on the
// hot reject path without a netlink enumeration or a scan of the range. Used by
// the node-side tap-pressure predicate (ADR embervm/014 decision 3): a zero free
// count is the `pressure:taps` rejection.
func (a *ipAllocator) freeCount() int {
	a.mu.Lock()
	defer a.mu.Unlock()
	// Range size is (last - first + 1); both bounds are inclusive and last >=
	// first is guaranteed by parseServingCIDR (it rejects a CIDR too small to hold
	// a gateway plus one VM). Subtract the allocated set to get the free count.
	total := ipRangeSize(a.first, a.last)
	free := total - len(a.used)
	if free < 0 {
		return 0
	}
	return free
}

// release returns an IP to the free pool (idempotent: releasing an unheld IP is a
// no-op).
func (a *ipAllocator) release(ip net.IP) {
	v4 := ip.To4()
	if v4 == nil {
		return
	}
	a.mu.Lock()
	delete(a.used, string(v4))
	a.mu.Unlock()
}

// parseServingCIDR parses the serving CIDR string and returns its gateway IP (the
// first usable address, .1) and the *net.IPNet. Only IPv4 is supported (serving taps
// are IPv4-only in v1). A malformed CIDR, a non-IPv4 CIDR, or a prefix too large to
// hold a gateway plus one VM is an error.
func parseServingCIDR(cidr string) (net.IP, *net.IPNet, error) {
	_, ipnet, err := net.ParseCIDR(cidr)
	if err != nil {
		return nil, nil, fmt.Errorf("serving: invalid CIDR %q: %w", cidr, err)
	}
	base := ipnet.IP.To4()
	if base == nil {
		return nil, nil, fmt.Errorf("serving: CIDR %q is not IPv4 (serving taps are IPv4-only in v1)", cidr)
	}
	ones, bits := ipnet.Mask.Size()
	if bits-ones < 2 {
		return nil, nil, fmt.Errorf("serving: CIDR %q too small (needs room for a gateway and at least one VM)", cidr)
	}
	gateway := nextIP(base) // network + 1 == the .1 gateway
	return gateway, ipnet, nil
}

// applyRuleset feeds an nft ruleset to `nft -f <path>` by writing it to a private
// temp file and passing the path. Piping via stdin would need a stdin seam on Runner;
// a 0600 temp file keeps Runner argv-only (so every exec is asserted as data) and the
// ruleset is generated by the separately-tested pure nftRuleset function. The file is
// removed after apply.
func applyRuleset(ctx context.Context, runner Runner, ruleset string) error {
	f, err := os.CreateTemp("", "embervm-serving-nft-*.nft")
	if err != nil {
		return fmt.Errorf("serving: create nft ruleset temp file: %w", err)
	}
	path := f.Name()
	defer func() { _ = os.Remove(path) }()
	if _, err := f.WriteString(ruleset); err != nil {
		_ = f.Close()
		return fmt.Errorf("serving: write nft ruleset: %w", err)
	}
	if err := f.Close(); err != nil {
		return fmt.Errorf("serving: close nft ruleset: %w", err)
	}
	if _, err := runner.Run(ctx, "nft", "-f", path); err != nil {
		return err
	}
	return nil
}

// ---- IPv4 arithmetic helpers (4-byte big-endian) ----------------------------

func cloneIP(ip net.IP) net.IP {
	v4 := ip.To4()
	out := make(net.IP, 4)
	copy(out, v4)
	return out
}

// nextIP returns ip+1 (4-byte). Overflow past 255.255.255.255 wraps, which the
// bounded [first,last] iteration never reaches.
func nextIP(ip net.IP) net.IP {
	v4 := cloneIP(ip)
	for i := 3; i >= 0; i-- {
		v4[i]++
		if v4[i] != 0 {
			break
		}
	}
	return v4
}

// prevIP returns ip-1 (4-byte).
func prevIP(ip net.IP) net.IP {
	v4 := cloneIP(ip)
	for i := 3; i >= 0; i-- {
		if v4[i] != 0 {
			v4[i]--
			break
		}
		v4[i] = 255
	}
	return v4
}

// broadcastIP is the all-host-bits-set address of the network.
func broadcastIP(ipnet *net.IPNet) net.IP {
	base := ipnet.IP.To4()
	mask := ipnet.Mask
	out := make(net.IP, 4)
	for i := 0; i < 4; i++ {
		out[i] = base[i] | ^mask[i]
	}
	return out
}

// ipRangeSize returns the count of addresses in the inclusive range [first,last]
// (first == last is a range of 1). Both are 4-byte IPv4; the serving CIDR is a
// /24-scale range, so the difference fits an int comfortably.
func ipRangeSize(first, last net.IP) int {
	f, l := first.To4(), last.To4()
	fu := uint32(f[0])<<24 | uint32(f[1])<<16 | uint32(f[2])<<8 | uint32(f[3])
	lu := uint32(l[0])<<24 | uint32(l[1])<<16 | uint32(l[2])<<8 | uint32(l[3])
	if lu < fu {
		return 0
	}
	return int(lu-fu) + 1
}

// compareIP returns -1, 0, 1 comparing two 4-byte IPs big-endian.
func compareIP(a, b net.IP) int {
	a4, b4 := a.To4(), b.To4()
	for i := 0; i < 4; i++ {
		switch {
		case a4[i] < b4[i]:
			return -1
		case a4[i] > b4[i]:
			return 1
		}
	}
	return 0
}
