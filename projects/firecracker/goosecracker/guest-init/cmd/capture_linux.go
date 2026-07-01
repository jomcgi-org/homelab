//go:build linux

package main

import (
	"context"
	"log/slog"
	"net"
	"net/netip"
	"os"
	"os/exec"
	"strconv"

	"golang.org/x/sys/unix"
)

// setupCapture installs the netfilter rules that REDIRECT the guest's outbound TCP
// to the loopback capture listener, then starts the listener (ADR 023 generic
// egress). Every step is best-effort: a failure is logged, not fatal (an
// off-cluster run has no egress to capture). The rules:
//   - route_localnet=1 so the kernel will REDIRECT onto 127.0.0.0/8;
//   - nat OUTPUT: RETURN for 127.0.0.1 (protect the DNS responder and the capture
//     listener from being redirected onto themselves), then REDIRECT every other
//     TCP connection to capturePort. SO_ORIGINAL_DST then recovers the pre-NAT
//     destination.
//
// Egress is name-based: every hostname the guest resolves gets a synthetic
// 127.0.0.0/8 address (routable to lo natively) that is captured and reverse-
// mapped to the name. A connection to a literal non-loopback IP with no DNS
// lookup is not routed to lo and so is not captured; agents reach the world by
// name, which is the supported path.
func setupCapture(ctx context.Context, logger *slog.Logger, res *synthResolver) {
	for _, path := range []string{
		"/proc/sys/net/ipv4/conf/all/route_localnet",
		"/proc/sys/net/ipv4/conf/lo/route_localnet",
	} {
		if err := os.WriteFile(path, []byte("1"), 0o644); err != nil {
			logger.Warn("egress capture: set route_localnet failed", "path", path, "err", err)
		}
	}

	port := strconv.Itoa(capturePort)
	rules := [][]string{
		// Protect the in-guest DNS responder and the capture listener (both on
		// 127.0.0.1) from being redirected onto themselves.
		{"-t", "nat", "-A", "OUTPUT", "-p", "tcp", "-d", "127.0.0.1/32", "-j", "RETURN"},
		// Redirect all other outbound TCP to the capture listener.
		{"-t", "nat", "-A", "OUTPUT", "-p", "tcp", "-j", "REDIRECT", "--to-ports", port},
	}
	for _, r := range rules {
		runCmd(logger, "iptables", r...)
	}

	go runCaptureListener(ctx, logger, res)
}

// runCmd runs an external command best-effort, logging a failure with its output.
func runCmd(logger *slog.Logger, name string, args ...string) {
	if out, err := exec.Command(name, args...).CombinedOutput(); err != nil {
		logger.Warn("egress capture: command failed", "cmd", name, "args", args, "err", err, "out", string(out))
	}
}

// soOriginalDst is the getsockopt option that returns a REDIRECTed connection's
// original pre-NAT destination (SO_ORIGINAL_DST from linux/netfilter_ipv4.h).
const soOriginalDst = 80

// originalDst returns the original (pre-REDIRECT) destination of a captured
// connection via getsockopt(SO_ORIGINAL_DST). The option returns a sockaddr_in;
// we read it out of the returned buffer: bytes [2:4] are the port (network byte
// order), [4:8] the IPv4 address.
func originalDst(conn *net.TCPConn) (netip.AddrPort, error) {
	raw, err := conn.SyscallConn()
	if err != nil {
		return netip.AddrPort{}, err // nosemgrep: no-bare-error-return
	}
	var ap netip.AddrPort
	var soErr error
	ctrlErr := raw.Control(func(fd uintptr) {
		mreq, e := unix.GetsockoptIPv6Mreq(int(fd), unix.IPPROTO_IP, soOriginalDst)
		if e != nil {
			soErr = e
			return
		}
		b := mreq.Multiaddr
		port := uint16(b[2])<<8 | uint16(b[3])
		ap = netip.AddrPortFrom(netip.AddrFrom4([4]byte{b[4], b[5], b[6], b[7]}), port)
	})
	if ctrlErr != nil {
		return netip.AddrPort{}, ctrlErr // nosemgrep: no-bare-error-return
	}
	return ap, soErr
}
