// Package serving is embervm-noded's serving-class networking and health layer.
// A serving-class microVM (R3) is long-lived and answers HTTP DIRECTLY over a tap
// NIC on a per-node bridge, unlike the vsock-only task/session classes. This
// package owns the host side of that: the per-node bridge, one tap per VM attached
// to it, static IP allocation from a reserved CIDR, the forward-hook nftables posture
// on the bridge, and the per-VM health-probe loop the daemon REPORTS (but never acts
// on) in NodeStatus. v1 filters only the forward hook: it drops VM-originated NEW
// FORWARDING (external egress) while allowing established/related return traffic. It
// does NOT filter the input hook, so a serving guest can still reach host-local
// services (the bridge gateway, the node IP); constraining that is folded into the
// recorded egress-hardening follow-on (standing decision 6, brokered egress).
//
// Networking is done by execing the host `ip` and `nft` binaries (baked into the
// noded image via apko), NOT a netlink library: there is no netlink dependency in
// this repo and no in-repo template, so a thin exec wrapper keeps the privileged
// surface auditable as literal argv and the rule generation trivially testable as
// pure functions returning argv / nft ruleset text as data. The noded pod already
// runs privileged (uid 0) for Firecracker's /dev/kvm and mount-namespace re-exec,
// so the CAP_NET_ADMIN these calls need is already granted; no new capability.
package serving

import (
	"context"
	"fmt"
	"net"
	"os/exec"
	"sort"
	"strings"
	"sync"
)

// nftTable is the DEDICATED nftables table the daemon owns for the serving forward
// posture. Teardown flushes only this table, so the daemon never touches or stomps
// any other host firewall state (kube-proxy, CNI, node firewall).
const nftTable = "embervm_serving"

// nftChain is the forward-hook chain inside nftTable. v1 filters the forward hook
// only: established/related return traffic is accepted (so responses to inbound
// requests flow), and VM-originated NEW forwarding is dropped (external egress is
// deny-by-default in v1). It does NOT filter the input hook, so VM-to-host-local
// traffic is unconstrained in v1; that is part of the recorded egress-hardening
// follow-on (standing decision 6, brokered egress).
const nftChain = "forward"

// nftDNATChain is the nat-hook chain inside nftTable that exposes each live serving
// VM as noded's routable pod IP + a per-VM port (D-R3.11.4). It is a prerouting DNAT
// chain: a packet to podIP:vmPort is rewritten dest -> tapIP:guestPort in the kernel,
// routed onto the serving bridge in noded's own netns, and conntrack reverses replies,
// so noded userspace never sits on the request hit path. The chain and its rules exist
// ONLY when a pod IP is configured; empty PodIP is the local/test fallback (report the
// tap IP, install no DNAT).
const nftDNATChain = "serving_dnat"

// Runner executes a host command and returns its combined output. The real
// implementation execs; tests inject a fake that records argv and returns canned
// output/errors, so every ip/nft invocation is asserted as data.
type Runner interface {
	Run(ctx context.Context, name string, args ...string) ([]byte, error)
}

// ExecRunner is the production Runner: it execs the named binary with CombinedOutput
// so a non-zero exit carries the tool's stderr into the returned error.
type ExecRunner struct{}

// Run execs name with args and returns combined stdout+stderr.
func (ExecRunner) Run(ctx context.Context, name string, args ...string) ([]byte, error) {
	cmd := exec.CommandContext(ctx, name, args...)
	out, err := cmd.CombinedOutput()
	if err != nil {
		return out, fmt.Errorf("serving: %s %s: %w: %s", name, strings.Join(args, " "), err, strings.TrimSpace(string(out)))
	}
	return out, nil
}

// bridgeSetupArgs returns the ordered argv batches that create the per-node bridge,
// assign it the gateway IP (the .1 of the serving CIDR), and bring it up. It is a
// pure function of (bridge name, gateway IP, prefix length) so the exact commands
// are asserted in a table test; the caller execs each batch in order and treats an
// "already exists" as idempotent (see EnsureBridge).
func bridgeSetupArgs(bridge, gatewayIP string, prefixLen int) [][]string {
	return [][]string{
		{"ip", "link", "add", "name", bridge, "type", "bridge"},
		{"ip", "addr", "add", fmt.Sprintf("%s/%d", gatewayIP, prefixLen), "dev", bridge},
		{"ip", "link", "set", bridge, "up"},
	}
}

// tapSetupArgs returns the ordered argv batches that create one tap device owned by
// uid 0 (the privileged daemon/Firecracker), attach it to the bridge, and bring it
// up. Pure for table testing.
func tapSetupArgs(tap, bridge string) [][]string {
	return [][]string{
		{"ip", "tuntap", "add", "dev", tap, "mode", "tap"},
		{"ip", "link", "set", tap, "master", bridge},
		{"ip", "link", "set", tap, "up"},
	}
}

// tapPrecreateArgs is tapSetupArgs' pre-provisioning sibling: it creates a tap and
// attaches it to the bridge but leaves it DOWN, since a pre-created pool tap sits
// idle until AllocateTap draws it and brings it up. Pure for table testing.
func tapPrecreateArgs(tap, bridge string) [][]string {
	return [][]string{
		{"ip", "tuntap", "add", "dev", tap, "mode", "tap"},
		{"ip", "link", "set", tap, "master", bridge},
	}
}

// tapUpArgs and tapDownArgs bring a pre-created pool tap up (on AllocateTap draw)
// or down (on ReleaseTap return), without creating or deleting the device. Pure for
// table testing.
func tapUpArgs(tap string) []string   { return []string{"ip", "link", "set", tap, "up"} }
func tapDownArgs(tap string) []string { return []string{"ip", "link", "set", tap, "down"} }

// tapTeardownArgs returns the argv to delete a tap device. Deleting the tap detaches
// it from the bridge implicitly. Pure for table testing.
func tapTeardownArgs(tap string) []string {
	return []string{"ip", "link", "del", tap}
}

// dnatEntry is one live serving VM's DNAT projection: the tap IP + guest port the VM
// actually listens on, and the deterministic per-VM port on noded's pod IP that maps to
// it (vmPort = portBase + hostOffset(tapIP)). vmPort is precomputed and stored so the
// ruleset generator stays a pure function of the entries and the entries sort
// deterministically (by vmPort, which is monotonic in the tap IP within a subnet).
type dnatEntry struct {
	tapIP     string
	guestPort uint32
	vmPort    uint32
}

// nftRuleset returns the `nft -f -` ruleset text that installs the serving posture on
// the bridge as a self-contained, idempotent script: it flushes and recreates ONLY the
// dedicated embervm_serving table, so applying it is safe to repeat and never touches
// other tables. The FORWARD chain accepts established/related return traffic and drops
// NEW traffic whose input interface is the bridge (VM-originated forwarding, i.e.
// external egress), leaving inbound request forwarding (dest = a VM) to the kernel's
// normal forward path. This filters the forward hook ONLY: v1 does not constrain
// VM-to-host-local traffic on the input hook (that is the recorded egress follow-on,
// standing decision 6).
//
// When podIP is non-empty (the deployed DNAT-through-noded posture, D-R3.11.4) it ALSO
// installs: an MSS clamp on the bridge-egress (VM-return) path so the guest-side MSS
// cannot exceed the CNI overlay MTU; and a prerouting `serving_dnat` nat chain with one
// rule per live VM rewriting podIP:vmPort -> tapIP:guestPort. `dnat ip to` (not bare
// `dnat to`) is required syntax in an inet table. When podIP is empty (local/test) the
// ruleset is exactly the v1 forward posture: no clamp, no nat chain, no DNAT. It is a
// pure function of (bridge, podIP, entries) so the exact ruleset is asserted as data.
func nftRuleset(bridge, podIP string, entries []dnatEntry) string {
	// `flush table` before defining makes the script idempotent: a re-apply replaces
	// the table's contents wholesale. `add table` is a no-op if it exists, so the
	// flush-then-define pair converges regardless of prior state. The flush also means
	// established conntrack flows are unaffected by a rebuild (translations live in
	// conntrack, not the ruleset), so re-applying on every VM add/remove is safe.
	var b strings.Builder
	fmt.Fprintf(&b, "add table inet %s\n", nftTable)
	fmt.Fprintf(&b, "flush table inet %s\n", nftTable)
	fmt.Fprintf(&b, "add chain inet %s %s { type filter hook forward priority 0; policy accept; }\n", nftTable, nftChain)
	// Return traffic for an established inbound flow is always allowed.
	fmt.Fprintf(&b, "add rule inet %s %s ct state established,related accept\n", nftTable, nftChain)
	// VM-originated NEW forwarding (packets entering the forward path FROM the bridge,
	// i.e. sourced by a serving VM) is dropped: v1 denies external egress at the forward
	// hook. VM-to-host-local traffic on the input hook is not filtered in v1.
	fmt.Fprintf(&b, "add rule inet %s %s iifname \"%s\" ct state new drop\n", nftTable, nftChain, bridge)
	if podIP != "" {
		// MSS clamp on the VM-return (bridge-egress) path: cap the SYN maxseg to the
		// route MTU so a guest with a 1500-MTU eth0 cannot advertise an MSS the CNI
		// overlay return path (smaller MTU) would have to fragment or black-hole.
		fmt.Fprintf(&b, "add rule inet %s %s oifname \"%s\" tcp flags syn tcp option maxseg size set rt mtu\n", nftTable, nftChain, bridge)
		// Prerouting DNAT chain: podIP:vmPort -> tapIP:guestPort for each live VM. The
		// forward chain's `iifname bridge new drop` never matches these (their iif is
		// eth0, not the bridge), and their reply is ct-established, so no filter rule is
		// needed; inbound NEW falls through to the forward policy accept.
		fmt.Fprintf(&b, "add chain inet %s %s { type nat hook prerouting priority dstnat; policy accept; }\n", nftTable, nftDNATChain)
		// Sort a copy by vmPort (== tap-IP order) so the generated script is a
		// deterministic pure function of the entry SET regardless of input order.
		sorted := append([]dnatEntry(nil), entries...)
		sort.Slice(sorted, func(i, j int) bool { return sorted[i].vmPort < sorted[j].vmPort })
		for _, e := range sorted {
			fmt.Fprintf(&b, "add rule inet %s %s ip daddr %s tcp dport %d dnat ip to %s:%d\n",
				nftTable, nftDNATChain, podIP, e.vmPort, e.tapIP, e.guestPort)
		}
	}
	return b.String()
}

// hostOffset returns ip's offset within network (its host part as a big-endian int),
// or -1 if ip is not an IPv4 address inside network. For 172.31.0.0/24 the .2 address
// has offset 2, .254 has offset 254; this is exactly the IP allocator's per-VM
// uniqueness reused as a port key, so no separate port allocator is needed.
func hostOffset(network *net.IPNet, ip net.IP) int {
	v4 := ip.To4()
	if v4 == nil || network == nil || len(network.Mask) != 4 || !network.Contains(ip) {
		return -1
	}
	off := 0
	for i := 0; i < 4; i++ {
		off = off<<8 | int(v4[i]&^network.Mask[i])
	}
	return off
}

// PortForIP derives the deterministic per-VM DNAT port for a tap IP: base +
// hostOffset(ip). It errors when ip is outside network or the derived port is out of
// the 1..65535 range, so a misconfiguration surfaces at StartServing rather than
// installing a bogus rule. Pure and recomputable anywhere (a relight pinning the same
// IP re-derives the same port).
func PortForIP(base int, network *net.IPNet, ip net.IP) (uint32, error) {
	off := hostOffset(network, ip)
	if off < 0 {
		return 0, fmt.Errorf("serving: ip %v not within serving network %v", ip, network)
	}
	port := base + off
	if port < 1 || port > 65535 {
		return 0, fmt.Errorf("serving: derived port %d for ip %v out of range 1..65535", port, ip)
	}
	return uint32(port), nil
}

// nftTeardownArgs returns the argv to remove the dedicated serving table entirely
// (scoped: only our table). It is provided for a FUTURE teardown call site (the daemon
// never tears the table down today: EnsureNetwork re-applies it idempotently on every
// start). `nft delete table` errors on an absent table, so whatever call site wires
// this in MUST tolerate that (e.g. ignore a not-found error). Pure for table testing.
func nftTeardownArgs() []string {
	return []string{"nft", "delete", "table", "inet", nftTable}
}

// Manager owns the host serving network: the shared per-node bridge, the nftables
// posture, and per-VM taps. It is safe for concurrent use by StartServing/
// StopServing (the IP allocator it embeds is separately locked; ip/nft calls are
// each atomic execs and the kernel serializes device mutations).
type Manager struct {
	runner    Runner
	bridge    string
	cidr      *net.IPNet
	gatewayIP net.IP
	prefixLen int
	alloc     *ipAllocator

	// podIP is noded's routable pod IP; serving endpoints are projected as podIP:vmPort
	// and reached through the serving_dnat chain. Empty disables DNAT (report the tap IP).
	podIP string
	// portBase is the base of the deterministic per-VM DNAT port space (vmPort =
	// portBase + hostOffset(tapIP)).
	portBase int
	// dnat is the live per-VM DNAT map keyed by tap IP string; the whole serving ruleset
	// is regenerated from it (pure function) and applied atomically on every add/remove.
	// dnatMu guards the map AND serializes the nft apply so two concurrent Start/Stop
	// calls cannot interleave ruleset writes.
	dnatMu sync.Mutex
	dnat   map[string]dnatEntry

	// tapPrealloc is the number of taps EnsureNetwork pre-creates at brick boot
	// (ADR embervm/014 decision 4). Zero disables pre-provisioning: AllocateTap/
	// ReleaseTap fall back to today's create-on-demand/delete-on-release behaviour.
	tapPrealloc int
	// poolMu guards pool (idle pre-created IPs available for AllocateTap to draw)
	// and poolOrigin (every IP precreatePool ever gave a pool identity, whether
	// currently idle in pool or checked out). An IP leaves pool while its tap is
	// checked out (link up, DNAT wired) and returns to it on ReleaseTap (link down,
	// DNAT removed) WHEN that IP is a pool-origin IP; it is never released back to
	// the allocator, since the tap device persists for the IP's whole
	// pre-provisioned lifetime. A fallback IP that AllocateTap created on demand
	// (pool empty or disabled) is NOT pool-origin: ReleaseTap deletes it and frees
	// the allocator reservation exactly as it always has, so the idle pool never
	// grows past tapPrealloc entries and the allocator never accumulates
	// reservations above the slot ceiling.
	poolMu     sync.Mutex
	pool       []net.IP
	poolOrigin map[string]struct{}
}

// NewManager builds a serving network Manager for a bridge name and a CIDR. The
// gateway (bridge) IP is the first usable address (.1) of the CIDR; VM IPs are
// allocated from .2 upward. podIP is noded's routable pod IP for the DNAT projection
// (empty disables DNAT and reports tap IPs); portBase is the DNAT port-space base.
// tapPrealloc is the ADR embervm/014 decision-4 pool size EnsureNetwork pre-creates
// at boot (0 disables pre-provisioning; see AllocateTap/ReleaseTap). A malformed
// CIDR, or (when podIP is set) a portBase whose top offset would overflow 65535, is
// an error so a misconfiguration fails loudly at startup rather than at the first
// StartServing.
func NewManager(runner Runner, bridge, cidr, podIP string, portBase, tapPrealloc int) (*Manager, error) {
	if runner == nil {
		runner = ExecRunner{}
	}
	gatewayIP, ipnet, err := parseServingCIDR(cidr)
	if err != nil {
		return nil, err
	}
	prefixLen, _ := ipnet.Mask.Size()
	if podIP != "" {
		ones, bits := ipnet.Mask.Size()
		subnetSize := 1 << (bits - ones)
		if portBase < 1 {
			return nil, fmt.Errorf("serving: port base %d must be >= 1", portBase)
		}
		if maxPort := portBase + subnetSize - 2; maxPort > 65535 {
			return nil, fmt.Errorf("serving: port base %d + subnet %s top offset overflows 65535 (max derived port %d)", portBase, cidr, maxPort)
		}
	}
	if tapPrealloc < 0 {
		return nil, fmt.Errorf("serving: tap prealloc %d must be >= 0", tapPrealloc)
	}
	return &Manager{
		runner:      runner,
		bridge:      bridge,
		cidr:        ipnet,
		gatewayIP:   gatewayIP,
		prefixLen:   prefixLen,
		alloc:       newIPAllocator(ipnet, gatewayIP),
		podIP:       podIP,
		portBase:    portBase,
		dnat:        make(map[string]dnatEntry),
		tapPrealloc: tapPrealloc,
		poolOrigin:  make(map[string]struct{}),
	}, nil
}

// CIDR reports the serving subnet CIDR string for NodeStatus.serving_subnet_cidr.
func (m *Manager) CIDR() string {
	if m == nil || m.cidr == nil {
		return ""
	}
	return m.cidr.String()
}

// ClampTapPrealloc lowers the configured tap-prealloc count to ceiling when it
// exceeds it, a no-op otherwise. It must be called before EnsureNetwork. The
// daemon entrypoint uses it to cap the pool at the brick's cgroup-derived slot
// ceiling (server.Server.SlotCeiling, ADR embervm/013 section 7): that ceiling is
// only known once server.New has built the budget reader, which happens after this
// Manager is constructed, so the clamp is applied as a separate step rather than a
// NewManager argument. A ceiling of 0 (MaxLiveVMs configured to 0 AND the cgroup
// memory budget unreadable/unlimited, the one combination server.Server.SlotCeiling
// can return 0 for, see budget.SlotCeiling) disables pre-provisioning entirely
// rather than erroring: fails safe to today's create-on-demand behaviour instead of
// wedging a brick whose capacity cannot be observed.
func (m *Manager) ClampTapPrealloc(ceiling int) {
	if ceiling >= 0 && m.tapPrealloc > ceiling {
		m.tapPrealloc = ceiling
	}
}

// EnsureNetwork creates the bridge (idempotently), installs the serving forward
// nftables posture (drop VM-originated NEW forwarding; see nftRuleset), and, when
// tapPrealloc > 0, pre-creates that many taps left DOWN in the pool (ADR
// embervm/014 decision 4: pre-provisioning removes netlink work from the instance
// boot path). It is called once on daemon start before any serving VM is brought
// up. Bridge-create batches that fail because the device already exists are
// tolerated (a daemon restart re-enters here with the bridge already present); the
// nftables apply is inherently idempotent (flush-then-define of our own table).
func (m *Manager) EnsureNetwork(ctx context.Context) error {
	for _, argv := range bridgeSetupArgs(m.bridge, m.gatewayIP.String(), m.prefixLen) {
		if _, err := m.runner.Run(ctx, argv[0], argv[1:]...); err != nil {
			if isAlreadyExists(err) {
				continue
			}
			return err
		}
	}
	// Enable IPv4 forwarding: the routed eth0 -> serving-bridge DNAT path needs it, and
	// nothing on this node forwarded before serving (busybox sysctl applet, baked into
	// the noded image). Harmless when DNAT is disabled.
	if _, err := m.runner.Run(ctx, "sysctl", "-w", "net.ipv4.ip_forward=1"); err != nil {
		return err
	}
	if err := m.precreatePool(ctx); err != nil {
		return err
	}
	// Apply the ruleset from the CURRENT (boot: empty) DNAT map. Post-boot, EnsureDNAT/
	// RemoveDNAT regenerate the same table including the forward posture.
	m.dnatMu.Lock()
	defer m.dnatMu.Unlock()
	return m.applyRulesetLocked(ctx)
}

// precreatePool pre-creates up to m.tapPrealloc taps (a no-op when it is 0) using
// the same deterministic TapNameForIP naming AllocateTap uses on-demand, over IPs
// drawn from the allocator's usable range. Each tap is created and attached to the
// bridge but left DOWN (see tapPrecreateArgs); del-before-add is retained per tap,
// exactly the #3745 stale-tap repair AllocateTap already relies on, since a daemon
// restart re-enters here with the prior incarnation's pool taps still attached.
//
// A single tap's precreate is best-effort: a transient ip failure (e.g. an EBUSY
// racing a prior incarnation's teardown) rolls back that one IP's reservation and
// CONTINUES to the next slot rather than returning an error. AllocateTap's
// on-demand fallback covers a pool that ends up short, so a brief netlink hiccup at
// boot degrading to a smaller (or empty) pool is the intended behaviour, never a
// reason to fail EnsureNetwork and crash-loop the brick.
func (m *Manager) precreatePool(ctx context.Context) error {
	for i := 0; i < m.tapPrealloc; i++ {
		ip, err := m.alloc.allocate()
		if err != nil {
			// Subnet exhausted (tapPrealloc configured larger than the CIDR can hold):
			// nothing further to try, same as an EnsureNetwork-fatal misconfiguration
			// elsewhere in this function (bad CIDR, sysctl failure).
			return fmt.Errorf("serving: pre-provisioning tap %d/%d: %w", i+1, m.tapPrealloc, err)
		}
		tap := TapNameForIP(ip)
		_, _ = m.runner.Run(ctx, "ip", tapTeardownArgs(tap)[1:]...)
		failed := false
		for _, argv := range tapPrecreateArgs(tap, m.bridge) {
			if _, rerr := m.runner.Run(ctx, argv[0], argv[1:]...); rerr != nil {
				_, _ = m.runner.Run(ctx, "ip", tapTeardownArgs(tap)[1:]...)
				m.alloc.release(ip)
				failed = true
				break
			}
		}
		if failed {
			continue
		}
		m.poolMu.Lock()
		m.pool = append(m.pool, ip)
		m.poolOrigin[ip.String()] = struct{}{}
		m.poolMu.Unlock()
	}
	return nil
}

// applyRulesetLocked regenerates the whole serving ruleset from the current DNAT map
// and applies it via `nft -f <file>` (see applyRuleset for why a temp file rather than
// a stdin pipe). Caller holds dnatMu.
func (m *Manager) applyRulesetLocked(ctx context.Context) error {
	return applyRuleset(ctx, m.runner, nftRuleset(m.bridge, m.podIP, m.dnatEntriesLocked()))
}

// dnatEntriesLocked returns the DNAT map as a slice (unordered); nftRuleset sorts it
// deterministically, so the map's iteration order does not matter. Caller holds dnatMu.
func (m *Manager) dnatEntriesLocked() []dnatEntry {
	out := make([]dnatEntry, 0, len(m.dnat))
	for _, e := range m.dnat {
		out = append(out, e)
	}
	return out
}

// EnsureDNAT installs (or refreshes) the prerouting DNAT rule that exposes a live
// serving VM's tap as podIP:vmPort, regenerating and atomically re-applying the whole
// serving table. It is a no-op when DNAT is disabled (empty PodIP). A derivation error
// (ip outside the subnet, port overflow) is returned so the caller reaps rather than
// publishing an unreachable endpoint.
func (m *Manager) EnsureDNAT(ctx context.Context, ip net.IP, guestPort uint32) error {
	if m.podIP == "" {
		return nil
	}
	vmPort, err := PortForIP(m.portBase, m.cidr, ip)
	if err != nil {
		return err
	}
	m.dnatMu.Lock()
	defer m.dnatMu.Unlock()
	m.dnat[ip.String()] = dnatEntry{tapIP: ip.String(), guestPort: guestPort, vmPort: vmPort}
	return m.applyRulesetLocked(ctx)
}

// RemoveDNAT drops a VM's DNAT rule and re-applies the table. It is folded into
// ReleaseTap so every teardown path (fresh-fail rollback, bank, destroy, reap) cleans
// the rule with no call-site edits. Best-effort: a re-apply error is swallowed (the tap
// is going away regardless, and EnsureNetwork rebuilds a clean table on restart).
func (m *Manager) RemoveDNAT(ctx context.Context, ip net.IP) {
	if m.podIP == "" {
		return
	}
	m.dnatMu.Lock()
	defer m.dnatMu.Unlock()
	delete(m.dnat, ip.String())
	_ = m.applyRulesetLocked(ctx)
}

// Endpoint projects a VM's (tap IP, guest port) into the endpoint the daemon REPORTS:
// (podIP, vmPort) when DNAT is enabled, else the tap IP + guest port unchanged (local/
// test fallback). It is strictly a projection: the registry keeps storing the tap IP
// (the probe target and bank pin), so the pod IP never leaks into readiness or the
// snapshot pin. A derivation failure for an allocated IP should never happen; if it
// does, fall back to the tap endpoint rather than publish a bogus port.
func (m *Manager) Endpoint(ip net.IP, guestPort uint32) (string, uint32) {
	if m.podIP == "" {
		return ip.String(), guestPort
	}
	vmPort, err := PortForIP(m.portBase, m.cidr, ip)
	if err != nil {
		return ip.String(), guestPort
	}
	return m.podIP, vmPort
}

// AllocateTap draws a pre-created pool tap when one is idle (ADR embervm/014
// decision 4: bring the link up, no netlink create/attach on the instance boot
// path), falling back to allocating the next free IP and creating a fresh tap when
// the pool is empty or disabled (tapPrealloc == 0), exactly today's behaviour. It
// returns (tapName, ip); on any ip failure the partially-created tap and the
// reserved IP are rolled back so a failed StartServing leaks neither. The returned
// tap name is deterministic from the IP so teardown needs only the IP.
func (m *Manager) AllocateTap(ctx context.Context) (tap string, ip net.IP, err error) {
	if pooled, ok := m.popPool(); ok {
		tap = TapNameForIP(pooled)
		if _, rerr := m.runner.Run(ctx, "ip", tapUpArgs(tap)[1:]...); rerr != nil {
			// Bringing an already-attached tap up failed: put it back rather than leak
			// it out of the pool, then fall through to on-demand creation below so the
			// caller still gets a usable tap.
			m.pushPool(pooled)
		} else {
			return tap, pooled, nil
		}
	}
	ip, err = m.alloc.allocate()
	if err != nil {
		return "", nil, err
	}
	tap = TapNameForIP(ip)
	// Idempotent create: best-effort delete any stale tap of this name first.
	// The allocator just handed us this IP exclusively (allocate() marks it used),
	// so a device already bearing its deterministic name is orphaned, not live: a
	// prior VM whose setup failed AFTER tap creation (e.g. a downstream volume
	// attach) and left the device behind. Without this, the leaked tap makes
	// `ip tuntap add` fail EBUSY and wedges every retry (observed 2026-07-20: a
	// leaked emtap left demo-postgres unable to relight and 503'd jomcgi.dev/health).
	// Deleting an absent tap is a tolerated error.
	_, _ = m.runner.Run(ctx, "ip", tapTeardownArgs(tap)[1:]...)
	for _, argv := range tapSetupArgs(tap, m.bridge) {
		if _, rerr := m.runner.Run(ctx, argv[0], argv[1:]...); rerr != nil {
			// Roll back: delete whatever tap fragment exists, release the IP.
			_, _ = m.runner.Run(ctx, "ip", tapTeardownArgs(tap)[1:]...)
			m.alloc.release(ip)
			return "", nil, rerr
		}
	}
	return tap, ip, nil
}

// AllocateTapForIP re-acquires a SPECIFIC IP (the D-R3.4.1 relight pin) and creates
// its tap. It is the relight counterpart of AllocateTap: a banked serving snapshot
// records the IP it was assigned, and a relight MUST re-acquire that same IP because
// the guest's eth0 keeps the IP baked at fresh boot (a snapshot resume never re-runs
// kernel init), so a different IP would black-hole traffic. The IP must be free in the
// allocator (it will be after a bank released it, or after a restart rescan re-reserved
// nothing); a conflict is an error the caller maps to FAILED_PRECONDITION. When ip is
// currently idle in the prealloc pool (already reserved in the allocator by
// precreatePool, ADR embervm/014 decision 4), it is drawn out of the pool instead of
// re-reserved, since the allocator already holds it for that tap's whole
// pre-provisioned lifetime; if bringing the pool tap up fails, the on-demand path
// below still runs against the freshly-reserved IP.
func (m *Manager) AllocateTapForIP(ctx context.Context, ip net.IP) (tap string, err error) {
	if m.popPoolIP(ip) {
		tap = TapNameForIP(ip)
		if _, rerr := m.runner.Run(ctx, "ip", tapUpArgs(tap)[1:]...); rerr == nil {
			return tap, nil
		}
		// Bringing the pool tap up failed: fall through to the on-demand path, which
		// re-does the full create+attach+up over an IP the allocator already reserved.
	} else if err = m.alloc.reserve(ip); err != nil {
		return "", err
	}
	tap = TapNameForIP(ip)
	// Idempotent create: best-effort delete any stale tap of this name first. The
	// relight just reserve()'d this exact IP (D-R3.4.1 pin) and reserve errors on a
	// live conflict, so a device already bearing its name is orphaned, not live, and
	// recreating over it would fail EBUSY and wedge every retry (see AllocateTap).
	// Deleting an absent tap is a tolerated error.
	_, _ = m.runner.Run(ctx, "ip", tapTeardownArgs(tap)[1:]...)
	for _, argv := range tapSetupArgs(tap, m.bridge) {
		if _, rerr := m.runner.Run(ctx, argv[0], argv[1:]...); rerr != nil {
			_, _ = m.runner.Run(ctx, "ip", tapTeardownArgs(tap)[1:]...)
			m.alloc.release(ip)
			return "", rerr
		}
	}
	return tap, nil
}

// ReleaseTap returns a VM's tap. When ip is POOL-ORIGIN (a tap precreatePool
// pre-created, ADR embervm/014 decision 4, tracked in poolOrigin regardless of
// whether tapPrealloc is still positive), the tap is brought down and returned to
// the pool instead of deleted, so a later AllocateTap can reuse it without netlink
// create/attach work; the IP stays reserved in the allocator the whole time (the
// tap device persists for it, up or down). Otherwise (prealloc disabled, or this IP
// was created ON DEMAND by AllocateTap's fallback because the pool was exhausted)
// it is today's behaviour: delete the tap and free the IP back to the allocator.
// This distinction (pool-origin, not "is prealloc currently on") matters: gating on
// tapPrealloc > 0 alone would return every fallback-created IP to the pool too,
// letting the idle pool ratchet up past tapPrealloc entries and the allocator
// accumulate reservations above the brick's slot ceiling. Either way this is
// idempotent at the ip layer (deleting/downing an absent tap is tolerated) so a
// teardown after a partial start still frees the IP.
func (m *Manager) ReleaseTap(ctx context.Context, ip net.IP) {
	// Drop the VM's DNAT rule first so the kernel stops advertising the endpoint before
	// the tap goes down or disappears (no-op when DNAT is disabled).
	m.RemoveDNAT(ctx, ip)
	tap := TapNameForIP(ip)
	if m.isPoolOrigin(ip) {
		if _, err := m.runner.Run(ctx, "ip", tapDownArgs(tap)[1:]...); err != nil {
			_ = err // best-effort: the tap stays attached (possibly still up) rather than wedging release
		}
		m.pushPool(ip)
		return
	}
	if _, err := m.runner.Run(ctx, "ip", tapTeardownArgs(tap)[1:]...); err != nil {
		// A missing tap is fine (already gone); other errors are logged by the caller
		// via the returned nothing, so swallow here to keep teardown best-effort.
		_ = err
	}
	m.alloc.release(ip)
}

// popPool removes and returns the lowest-IP idle pool entry, or (nil, false) when
// the pool is empty or disabled. FIFO (pool is built in ascending IP order by
// precreatePool and refilled at the tail by pushPool), matching the allocator's own
// lowest-free convention so pool draws are deterministic and easy to reason about.
func (m *Manager) popPool() (net.IP, bool) {
	m.poolMu.Lock()
	defer m.poolMu.Unlock()
	if len(m.pool) == 0 {
		return nil, false
	}
	ip := m.pool[0]
	m.pool = m.pool[1:]
	return ip, true
}

// pushPool returns an IP to the idle pool. It is a no-op if ip is already present
// in pool, so a double ReleaseTap on the same IP (a caller bug, or a retried
// teardown racing itself) cannot duplicate the entry and later hand the same tap to
// two different VMs out of two AllocateTap calls. Mirrors the idempotence
// alloc.release already has for the on-demand path this replaces.
func (m *Manager) pushPool(ip net.IP) {
	m.poolMu.Lock()
	defer m.poolMu.Unlock()
	for _, pooled := range m.pool {
		if pooled.Equal(ip) {
			return
		}
	}
	m.pool = append(m.pool, ip)
}

// isPoolOrigin reports whether ip was ever given a pool identity by precreatePool
// (ADR embervm/014 decision 4), regardless of whether it is currently idle in pool
// or checked out. ReleaseTap uses this (not "is tapPrealloc currently positive") to
// decide whether an IP returns to the pool or is deleted/freed on release.
func (m *Manager) isPoolOrigin(ip net.IP) bool {
	m.poolMu.Lock()
	defer m.poolMu.Unlock()
	_, ok := m.poolOrigin[ip.String()]
	return ok
}

// popPoolIP removes a SPECIFIC IP from the idle pool if present, reporting whether
// it was found. Used by AllocateTapForIP: a relight pin may name an IP that happens
// to be sitting idle in the prealloc pool, in which case it must be drawn out (not
// re-reserved; precreatePool already holds it in the allocator).
func (m *Manager) popPoolIP(ip net.IP) bool {
	m.poolMu.Lock()
	defer m.poolMu.Unlock()
	for i, pooled := range m.pool {
		if pooled.Equal(ip) {
			m.pool = append(m.pool[:i], m.pool[i+1:]...)
			return true
		}
	}
	return false
}

// GatewayIP is the bridge IP a guest uses as its default route.
func (m *Manager) GatewayIP() net.IP { return m.gatewayIP }

// PrefixLen is the serving CIDR prefix length, for the guest static IP mask.
func (m *Manager) PrefixLen() int { return m.prefixLen }

// isAlreadyExists reports whether an ip command failed because the object already
// exists (RTNETLINK "File exists"), which EnsureNetwork treats as idempotent success.
func isAlreadyExists(err error) bool {
	if err == nil {
		return false
	}
	s := strings.ToLower(err.Error())
	// "file exists" / "already exists": a duplicate `ip link add` (bridge or tap).
	// "already assigned": a duplicate `ip addr add` on a device that already holds
	// the address (iproute2 prints "Error: ipv4: Address already assigned."), which
	// is the desired end-state when an idempotent bridge re-issue re-runs the setup
	// against an already-addressed bridge. Tolerating it keeps ensureBridgeDevice
	// idempotent instead of tearing a healthy bridge back down.
	return strings.Contains(s, "file exists") ||
		strings.Contains(s, "already exists") ||
		strings.Contains(s, "already assigned")
}

// TapNameForIP derives the deterministic tap device name from a VM IP: "emtap" plus
// the last two octets in hex, kept under the 15-char Linux ifname limit. Deterministic
// so teardown needs only the IP, and unique within a /16 (the last two octets are the
// host part for any serving CIDR /16 or longer).
func TapNameForIP(ip net.IP) string {
	v4 := ip.To4()
	if v4 == nil {
		return "emtap0"
	}
	return fmt.Sprintf("emtap%02x%02x", v4[2], v4[3])
}
