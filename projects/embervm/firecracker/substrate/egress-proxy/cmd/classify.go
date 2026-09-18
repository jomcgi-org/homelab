package main

// Split-horizon egress guardrail (ADR 023). Generic egress must not mean "reach
// anything": the public internet is open so an agent can read arbitrary docs and
// APIs, but the cluster is deny-by-default so a prompt-injected agent cannot pivot
// into internal services (the k8s API, other pods, the node, cloud metadata).
//
// Classification is on the RESOLVED IP the sidecar will actually dial, not the
// name the untrusted guest sent. That is what makes it un-bypassable: a name that
// resolves to an internal address (SSRF-by-name), and a literal internal IP, are
// both caught. The pinned IP is dialed without re-resolving, defeating DNS
// rebinding between the check and the connect.

import (
	"log/slog"
	"net"
	"strings"
)

// route resolves the preamble host, pins one IP, and applies the guardrail. It
// returns the exact ip:port to dial (pinned) and whether egress is permitted.
func (p *proxy) route(host, port string) (dialAddr string, ok bool) {
	ip := net.ParseIP(host)
	if ip == nil {
		ips, err := p.lookupIP(host)
		if err != nil || len(ips) == 0 {
			p.logger.Warn("egress resolve failed", "host", host, "err", err)
			return "", false
		}
		ip = pickIP(ips)
	}
	if p.isInternal(ip) {
		if p.internalDefaultAllow || p.internalAllows(host, ip, port) {
			return net.JoinHostPort(ip.String(), port), true
		}
		return "", false
	}
	if !p.externalAllow {
		return "", false
	}
	return net.JoinHostPort(ip.String(), port), true
}

// isInternal reports whether ip is in a fenced (cluster/private) range: loopback,
// RFC1918 plus IPv6 ULA, link-local (including cloud metadata 169.254.169.254),
// the unspecified address, or any operator-configured extra CIDR. A public IP is
// none of these. Go's stdlib predicates cover this cluster's pod (10.42/16),
// service (10.43/16), and node (192.168.1/24) ranges, since all fall inside
// RFC1918.
func (p *proxy) isInternal(ip net.IP) bool {
	if ip.IsLoopback() || ip.IsPrivate() || ip.IsLinkLocalUnicast() || ip.IsLinkLocalMulticast() || ip.IsUnspecified() {
		return true
	}
	for _, n := range p.extraInternalNets {
		if n.Contains(ip) {
			return true
		}
	}
	return false
}

// internalAllows reports whether an internal destination is explicitly permitted,
// matching either the requested host:port or the resolved ip:port against the
// internal allowlist.
func (p *proxy) internalAllows(host string, ip net.IP, port string) bool {
	return allowed(net.JoinHostPort(host, port), p.internalAllowlist) ||
		allowed(net.JoinHostPort(ip.String(), port), p.internalAllowlist)
}

// pickIP prefers the first IPv4 address, falling back to the first address. The
// guest hands out only IPv4 synthetic addresses and cluster DNS is IPv4, so this
// keeps the dialed family predictable.
func pickIP(ips []net.IP) net.IP {
	for _, ip := range ips {
		if ip.To4() != nil {
			return ip
		}
	}
	return ips[0]
}

// parseCIDRs parses a comma-separated list of extra internal CIDRs, logging and
// skipping malformed entries.
func parseCIDRs(logger *slog.Logger, s string) []*net.IPNet {
	var out []*net.IPNet
	for _, part := range strings.Split(s, ",") {
		part = strings.TrimSpace(part)
		if part == "" {
			continue
		}
		_, n, err := net.ParseCIDR(part)
		if err != nil {
			logger.Warn("egress: skipping malformed EGRESS_INTERNAL_CIDRS entry", "entry", part, "err", err)
			continue
		}
		out = append(out, n)
	}
	return out
}
