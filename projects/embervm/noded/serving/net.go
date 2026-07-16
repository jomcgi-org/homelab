// Package serving is embervm-noded's serving-class networking and health layer.
// A serving-class microVM (R3) is long-lived and answers HTTP DIRECTLY over a tap
// NIC on a per-node bridge, unlike the vsock-only task/session classes. This
// package owns the host side of that: the per-node bridge, one tap per VM attached
// to it, static IP allocation from a reserved CIDR, the ingress-only nftables
// posture on the bridge, and the per-VM health-probe loop the daemon REPORTS (but
// never acts on) in NodeStatus.
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
	"strings"
)

// nftTable is the DEDICATED nftables table the daemon owns for serving ingress
// posture. Teardown flushes only this table, so the daemon never touches or stomps
// any other host firewall state (kube-proxy, CNI, node firewall).
const nftTable = "embervm_serving"

// nftChain is the forward-hook chain inside nftTable that enforces the ingress-only
// posture: established/related return traffic is accepted (so responses to inbound
// requests flow), and VM-originated NEW forwarding is dropped (serving egress stays
// deny-by-default at the tap in v1; brokered egress is a recorded follow-on).
const nftChain = "forward"

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

// tapTeardownArgs returns the argv to delete a tap device. Deleting the tap detaches
// it from the bridge implicitly. Pure for table testing.
func tapTeardownArgs(tap string) []string {
	return []string{"ip", "link", "del", tap}
}

// nftRuleset returns the `nft -f -` ruleset text that installs the ingress-only
// posture on the serving bridge as a self-contained, idempotent script: it flushes
// and recreates ONLY the dedicated embervm_serving table, so applying it is safe to
// repeat and never touches other tables. The forward chain accepts established/
// related return traffic and drops NEW traffic whose input interface is the bridge
// (VM-originated forwarding), leaving inbound request forwarding (dest = a VM) to the
// kernel's normal forward path. It is a pure function of the bridge name so the exact
// ruleset is asserted as data in a table test.
func nftRuleset(bridge string) string {
	// `flush table` before `table` makes the script idempotent: a re-apply replaces
	// the table's contents wholesale. `add table` is a no-op if it exists, so the
	// flush-then-define pair converges regardless of prior state. delete+add of the
	// table would also work but errors if the table is absent on first apply; the
	// `add table` then `flush table` ordering below is the idempotent form.
	var b strings.Builder
	fmt.Fprintf(&b, "add table inet %s\n", nftTable)
	fmt.Fprintf(&b, "flush table inet %s\n", nftTable)
	fmt.Fprintf(&b, "add chain inet %s %s { type filter hook forward priority 0; policy accept; }\n", nftTable, nftChain)
	// Return traffic for an established inbound flow is always allowed.
	fmt.Fprintf(&b, "add rule inet %s %s ct state established,related accept\n", nftTable, nftChain)
	// VM-originated NEW forwarding (packets entering the forward path FROM the bridge,
	// i.e. sourced by a serving VM) is dropped: serving VMs are ingress-only in v1.
	fmt.Fprintf(&b, "add rule inet %s %s iifname \"%s\" ct state new drop\n", nftTable, nftChain, bridge)
	return b.String()
}

// nftTeardownArgs returns the argv to remove the dedicated serving table entirely
// (scoped: only our table). Idempotent at the call site (an absent table is treated
// as already-gone). Pure for table testing.
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
}

// NewManager builds a serving network Manager for a bridge name and a CIDR. The
// gateway (bridge) IP is the first usable address (.1) of the CIDR; VM IPs are
// allocated from .2 upward. A malformed CIDR is an error so a misconfiguration
// fails loudly at startup rather than at the first StartServing.
func NewManager(runner Runner, bridge, cidr string) (*Manager, error) {
	if runner == nil {
		runner = ExecRunner{}
	}
	gatewayIP, ipnet, err := parseServingCIDR(cidr)
	if err != nil {
		return nil, err
	}
	prefixLen, _ := ipnet.Mask.Size()
	return &Manager{
		runner:    runner,
		bridge:    bridge,
		cidr:      ipnet,
		gatewayIP: gatewayIP,
		prefixLen: prefixLen,
		alloc:     newIPAllocator(ipnet, gatewayIP),
	}, nil
}

// CIDR reports the serving subnet CIDR string for NodeStatus.serving_subnet_cidr.
func (m *Manager) CIDR() string {
	if m == nil || m.cidr == nil {
		return ""
	}
	return m.cidr.String()
}

// EnsureNetwork creates the bridge (idempotently) and installs the ingress-only
// nftables posture. It is called once on daemon start before any serving VM is
// brought up. Bridge-create batches that fail because the device already exists are
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
	return m.applyNftables(ctx)
}

// applyNftables installs the ingress-only ruleset via `nft -f <file>` (see
// applyRuleset for why a temp file rather than a stdin pipe).
func (m *Manager) applyNftables(ctx context.Context) error {
	return applyRuleset(ctx, m.runner, nftRuleset(m.bridge))
}

// AllocateTap allocates the next free IP, creates and attaches a tap named for that
// IP, and returns (tapName, ip). On any ip failure the partially-created tap and the
// reserved IP are rolled back so a failed StartServing leaks neither. The returned
// tap name is deterministic from the IP so teardown needs only the IP.
func (m *Manager) AllocateTap(ctx context.Context) (tap string, ip net.IP, err error) {
	ip, err = m.alloc.allocate()
	if err != nil {
		return "", nil, err
	}
	tap = TapNameForIP(ip)
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
// nothing); a conflict is an error the caller maps to FAILED_PRECONDITION.
func (m *Manager) AllocateTapForIP(ctx context.Context, ip net.IP) (tap string, err error) {
	if err = m.alloc.reserve(ip); err != nil {
		return "", err
	}
	tap = TapNameForIP(ip)
	for _, argv := range tapSetupArgs(tap, m.bridge) {
		if _, rerr := m.runner.Run(ctx, argv[0], argv[1:]...); rerr != nil {
			_, _ = m.runner.Run(ctx, "ip", tapTeardownArgs(tap)[1:]...)
			m.alloc.release(ip)
			return "", rerr
		}
	}
	return tap, nil
}

// ReleaseTap deletes the tap for an IP and releases the IP back to the allocator. It
// is idempotent at the ip layer (deleting an absent tap is tolerated) so a teardown
// after a partial start still frees the IP.
func (m *Manager) ReleaseTap(ctx context.Context, ip net.IP) {
	tap := TapNameForIP(ip)
	if _, err := m.runner.Run(ctx, "ip", tapTeardownArgs(tap)[1:]...); err != nil {
		// A missing tap is fine (already gone); other errors are logged by the caller
		// via the returned nothing, so swallow here to keep teardown best-effort.
		_ = err
	}
	m.alloc.release(ip)
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
	return strings.Contains(s, "file exists") || strings.Contains(s, "already exists")
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
