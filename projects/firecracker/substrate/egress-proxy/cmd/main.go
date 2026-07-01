// Command egress-proxy is a tiny, dependency-free transparent egress proxy that
// mediates every Firecracker guest's outbound traffic (ADR 023). The guest is
// vsock-only: the guest init gives every DNS name a synthetic 127.0.0.0/8
// address, REDIRECTs all outbound TCP to a loopback capture listener, recovers
// each connection's original destination via SO_ORIGINAL_DST, and tunnels it here
// (through the fc-invoke daemon) with a one-line "host:port" preamble. This
// sidecar is the only process that reaches the network on the guest's behalf.
//
// Split-horizon egress guardrail (see classify.go): the sidecar resolves the
// preamble host, pins one IP, and classifies it. External (public) destinations
// are allowed by default so the agent can read the open internet; internal
// (cluster / private / loopback / link-local) destinations are denied unless
// explicitly allowlisted, which closes the cluster-pivot vector. Classification is
// on the RESOLVED IP the sidecar will actually dial (not the guest-claimed name),
// and the pinned IP is dialed without re-resolving, so a hostile guest cannot name
// its way to an internal host nor race DNS.
//
// Secret placeholder-swap (ADR 023 6b, see swap.go) is orthogonal to the zone
// policy: for a TLS destination whose host is in a secret's egressTo, the sidecar
// terminates TLS with a leaf minted from the egress CA, swaps the placeholder for
// the real value, and re-originates. The swap fires only at that host, so the real
// value is unreachable for any other destination.
//
// Configuration (env):
//   - EGRESS_LISTEN: where the fc-invoke daemon forwards guest egress (":8888").
//   - EGRESS_EXTERNAL: "allow" (default) or "deny" for public destinations.
//   - EGRESS_INTERNAL_DEFAULT: "deny" (default) or "allow" for internal ones.
//   - EGRESS_INTERNAL_ALLOWLIST: comma-separated host[:port] permitted internally.
//   - EGRESS_INTERNAL_CIDRS: comma-separated extra CIDRs classified as internal.
//   - EGRESS_SECRETS / EGRESS_CA_CERT_FILE / EGRESS_CA_KEY_FILE: secret swap.
package main

import (
	"bufio"
	"io"
	"log/slog"
	"net"
	"os"
	"strings"
	"time"
)

// dialTimeout bounds the upstream connect; it does not cap a tunnel's lifetime.
const dialTimeout = 30 * time.Second

func main() {
	logger := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: slog.LevelInfo}))
	slog.SetDefault(logger)

	// EGRESS_LISTEN is where the fc-invoke daemon forwards guest egress (pod-local).
	listen := envOr("EGRESS_LISTEN", ":8888")

	// Split-horizon posture: the public internet is open by default; the cluster is
	// deny-by-default and confined to the internal allowlist.
	externalAllow := envOr("EGRESS_EXTERNAL", "allow") != "deny"
	internalDefaultAllow := envOr("EGRESS_INTERNAL_DEFAULT", "deny") == "allow"
	internalAllowlist := parseAllowlist(os.Getenv("EGRESS_INTERNAL_ALLOWLIST"))
	extraInternalNets := parseCIDRs(logger, os.Getenv("EGRESS_INTERNAL_CIDRS"))

	logger.Info("egress-proxy starting",
		"listen", listen,
		"externalAllow", externalAllow,
		"internalDefaultAllow", internalDefaultAllow,
		"internalAllowlist", internalAllowlist,
		"extraInternalCIDRs", len(extraInternalNets),
	)
	if !internalDefaultAllow && len(internalAllowlist) == 0 {
		logger.Warn("internal egress deny-by-default with an empty allowlist; all internal destinations will be denied")
	}

	// Secret placeholder-swap (ADR 023 6b): load the catalog and the CA the sidecar
	// uses to TLS-terminate secret-bearing destinations. Absent CA paths or an empty
	// catalog leave the proxy a plain transparent router.
	secrets := loadSecrets(logger)
	var minter *caMinter
	if caCert, caKey := os.Getenv("EGRESS_CA_CERT_FILE"), os.Getenv("EGRESS_CA_KEY_FILE"); caCert != "" && caKey != "" && len(secrets) > 0 {
		m, err := newCAMinter(caCert, caKey)
		if err != nil {
			logger.Error("egress CA load failed; secret swap disabled", "err", err)
		} else {
			minter = m
			logger.Info("secret swap enabled", "secrets", len(secrets))
		}
	}

	p := &proxy{
		externalAllow:        externalAllow,
		internalDefaultAllow: internalDefaultAllow,
		internalAllowlist:    internalAllowlist,
		extraInternalNets:    extraInternalNets,
		lookupIP:             net.LookupIP,
		secrets:              secrets,
		minter:               minter,
		logger:               logger,
	}

	ln, err := net.Listen("tcp", listen)
	if err != nil {
		logger.Error("egress-proxy listen failed", "addr", listen, "err", err)
		os.Exit(1)
	}
	for {
		conn, err := ln.Accept()
		if err != nil {
			logger.Error("egress-proxy accept failed", "err", err)
			os.Exit(1)
		}
		go p.handle(conn)
	}
}

// proxy holds the split-horizon posture, the secret catalog, and the CA minter.
type proxy struct {
	// externalAllow permits public (non-internal) destinations.
	externalAllow bool
	// internalDefaultAllow permits every internal destination; when false, only
	// internalAllowlist entries are reachable inside the cluster.
	internalDefaultAllow bool
	// internalAllowlist is the set of internal host[:port] destinations permitted
	// under deny-by-default.
	internalAllowlist []string
	// extraInternalNets are operator-configured CIDRs classified as internal on top
	// of the baked private/loopback/link-local ranges.
	extraInternalNets []*net.IPNet
	// lookupIP resolves a host to IPs (net.LookupIP in production, injectable in
	// tests).
	lookupIP func(host string) ([]net.IP, error)
	// secrets is the placeholder-swap catalog (ADR 023 6b); empty disables swap.
	secrets []secretEntry
	// minter mints leaf certs from the egress CA for TLS termination; nil disables
	// the swap path (the proxy stays a plain transparent router).
	minter *caMinter
	logger *slog.Logger
}

// handle services one guest connection: read the "host:port" preamble, apply the
// split-horizon guardrail (resolve + classify + pin), then either terminate-swap a
// secret-bearing TLS destination or blind-tunnel to the pinned upstream.
func (p *proxy) handle(client net.Conn) {
	defer client.Close()
	br := bufio.NewReader(client)

	line, err := br.ReadString('\n')
	if err != nil {
		p.logger.Warn("egress preamble read failed", "err", err)
		return
	}
	host, port := splitHostPort(strings.TrimSpace(line))
	if host == "" || port == "" {
		p.logger.Warn("egress preamble invalid", "preamble", strings.TrimSpace(line))
		return
	}
	dest := net.JoinHostPort(host, port)

	// Resolve + classify + pin. dialAddr is the exact ip:port we will connect to,
	// so the policy decision and the connect cannot diverge (no DNS-rebind race).
	dialAddr, ok := p.route(host, port)
	if !ok {
		p.logger.Warn("egress denied", "dest", dest)
		return
	}

	// Secret-bearing TLS destination: terminate, swap the placeholder, re-originate
	// (ADR 023 6b). We only need the first byte to tell TLS from plaintext; the host
	// already came from the preamble, so no SNI/Host sniffing is required.
	if p.minter != nil {
		if first, err := br.Peek(1); err == nil && first[0] == 0x16 {
			if sec := p.secretFor(host); sec != nil {
				p.logger.Info("egress allowed (swap)", "dest", dest, "dial", dialAddr)
				p.terminateAndSwap(br, client, dialAddr, host, sec)
				return
			}
		}
	}
	p.logger.Info("egress allowed", "dest", dest, "dial", dialAddr)

	up, err := net.DialTimeout("tcp", dialAddr, dialTimeout)
	if err != nil {
		p.logger.Error("egress upstream dial failed", "dest", dest, "dial", dialAddr, "err", err)
		return
	}
	defer up.Close()

	// Pump both directions. br carries the peeked-but-unconsumed client bytes, so
	// the upstream sees the exact original stream. Either close unblocks the other.
	done := make(chan struct{}, 2)
	go func() { _, _ = io.Copy(up, br); done <- struct{}{} }()
	go func() { _, _ = io.Copy(client, up); done <- struct{}{} }()
	<-done
}

// allowed reports whether dest is permitted by allowlist (used for the internal
// allowlist under deny-by-default).
//
// dest is "host" or "host:port". Each allowlist entry is "host" (any port) or
// "host:port" (that exact port). Host comparison is exact and case-insensitive:
// there is deliberately NO suffix or wildcard matching, so "api.example.com" does
// not match "evil-api.example.com". An empty allowlist (or no match) denies.
func allowed(dest string, allowlist []string) bool {
	destHost, destPort := splitHostPort(dest)
	for _, entry := range allowlist {
		entry = strings.TrimSpace(entry)
		if entry == "" {
			continue
		}
		entryHost, entryPort := splitHostPort(entry)
		if !strings.EqualFold(destHost, entryHost) {
			continue
		}
		if entryPort == "" || entryPort == destPort {
			return true
		}
	}
	return false
}

// splitHostPort splits "host:port"; a value with no parseable port returns host
// with an empty port.
func splitHostPort(s string) (host, port string) {
	if h, p, err := net.SplitHostPort(s); err == nil {
		return h, p
	}
	return s, ""
}

// parseAllowlist splits a comma-separated allowlist, trimming and dropping empties.
func parseAllowlist(s string) []string {
	parts := strings.Split(s, ",")
	out := make([]string, 0, len(parts))
	for _, part := range parts {
		if part = strings.TrimSpace(part); part != "" {
			out = append(out, part)
		}
	}
	return out
}

// envOr returns the env var key, or fallback if unset/empty.
func envOr(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}
