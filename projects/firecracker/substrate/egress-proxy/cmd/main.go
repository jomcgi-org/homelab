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
// Credential injection (ADR 023 6b, see swap.go) is orthogonal to the zone
// policy: for a destination whose host is in a secret's egressTo, the sidecar
// reads the plaintext request, SETS the configured header to the real value, and
// re-originates TLS upstream. It fires only at that host, so the credential is
// unreachable for any other destination. The guest holds nothing that maps to it.
//
// A credentialed host whose secret has not resolved is DENIED, never tunnelled:
// the guest addresses it in cleartext, so a fall-through would put the whole
// request on the public internet unencrypted.
//
// A guest reaches a credentialed destination one of two ways, distinguished by
// peeking a single byte:
//
//   - Plaintext (an ASCII method). The guest was configured to speak http:// to
//     us, so there is nothing to terminate: we inject and originate TLS on 443.
//     The guest hop is a host-local vsock with no network segment on it, so
//     guest-side TLS would be encrypting against an attacker who cannot be there.
//     This is the lane the embervm claude runtime uses.
//   - TLS (0x16). The guest speaks https:// and the sidecar MITMs it with a leaf
//     minted from the egress CA, which the guest must already trust. Needs
//     EGRESS_CA_CERT_FILE/KEY; without them this falls through to a blind tunnel
//     and the request reaches the destination with no credential attached.
//
// Configuration (env):
//   - EGRESS_LISTEN: where the fc-invoke daemon forwards guest egress
//     ("127.0.0.1:8888"). The loopback bind is load-bearing: this sidecar has no
//     client authentication, so it is the only barrier preventing an arbitrary
//     cluster workload from using the credentialed response path.
//   - EGRESS_EXTERNAL: "allow" (default) or "deny" for public destinations.
//   - EGRESS_INTERNAL_DEFAULT: "deny" (default) or "allow" for internal ones.
//   - EGRESS_INTERNAL_ALLOWLIST: comma-separated host[:port] permitted internally.
//   - EGRESS_INTERNAL_CIDRS: comma-separated extra CIDRs classified as internal.
//   - EGRESS_MAX_CONNS: maximum concurrent guest connections (default 256).
//   - EGRESS_SECRETS: the credential catalog; enables the plaintext inject lane.
//   - EGRESS_CA_CERT_FILE / EGRESS_CA_KEY_FILE: optional, adds the TLS-MITM lane.
package main

import (
	"bufio"
	"io"
	"log/slog"
	"net"
	"net/url"
	"os"
	"strconv"
	"strings"
	"sync/atomic"
	"time"
)

const (
	// dialTimeout bounds the upstream connect; it does not cap a tunnel's lifetime.
	dialTimeout      = 30 * time.Second
	maxPreambleBytes = 1024
	defaultMaxConns  = 256
)

var handshakeTimeout = 10 * time.Second

// exitFn is os.Exit, indirected so the fail-closed config paths are testable.
var exitFn = os.Exit

func main() {
	logger := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: slog.LevelInfo}))
	slog.SetDefault(logger)

	// The sidecar has no client authentication. Binding to loopback is therefore
	// the only barrier preventing an arbitrary cluster workload from connecting to
	// the credential-holding process and using its response path.
	listen := envOr("EGRESS_LISTEN", defaultListenAddr)

	// Split-horizon posture: the public internet is open by default; the cluster is
	// deny-by-default and confined to the internal allowlist.
	externalAllow := envOr("EGRESS_EXTERNAL", "allow") != "deny"
	internalDefaultAllow := envOr("EGRESS_INTERNAL_DEFAULT", "deny") == "allow"
	internalAllowlist := parseAllowlist(os.Getenv("EGRESS_INTERNAL_ALLOWLIST"))
	extraInternalNets := parseCIDRs(logger, os.Getenv("EGRESS_INTERNAL_CIDRS"))
	maxConns := maxConnsFromEnv(logger)

	logger.Info("egress-proxy starting",
		"listen", listen,
		"externalAllow", externalAllow,
		"internalDefaultAllow", internalDefaultAllow,
		"internalAllowlist", internalAllowlist,
		"extraInternalCIDRs", len(extraInternalNets),
		"maxConns", maxConns,
	)
	if !internalDefaultAllow && len(internalAllowlist) == 0 {
		logger.Warn("internal egress deny-by-default with an empty allowlist; all internal destinations will be denied")
	}

	// Credential injection (ADR 023 6b): the catalog alone enables the plaintext
	// lane. The CA is optional and only adds the TLS-MITM lane for guests that speak
	// https:// to us; an empty catalog leaves the proxy a plain transparent router.
	brokerURL := os.Getenv("EGRESS_TOKEN_BROKER_URL")
	secrets := loadSecretsWithBroker(logger, brokerURL)
	var minter *caMinter
	if caCert, caKey := os.Getenv("EGRESS_CA_CERT_FILE"), os.Getenv("EGRESS_CA_KEY_FILE"); caCert != "" && caKey != "" && len(secrets) > 0 {
		m, err := newCAMinter(caCert, caKey)
		if err != nil {
			logger.Error("egress CA load failed; TLS-MITM inject lane disabled", "err", err)
		} else {
			minter = m
		}
	}
	if len(secrets) > 0 {
		logger.Info("credential injection enabled", "secrets", len(secrets), "tlsMitmLane", minter != nil)
	}

	p := &proxy{
		externalAllow:        externalAllow,
		internalDefaultAllow: internalDefaultAllow,
		internalAllowlist:    internalAllowlist,
		extraInternalNets:    extraInternalNets,
		lookupIP:             net.LookupIP,
		secrets:              secrets,
		brokerURL:            brokerURL,
		minter:               minter,
		logger:               logger,
		conns:                make(chan struct{}, maxConns),
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
		// Git's protocol is request/response in small messages. Nagle holds each
		// write until the prior segment is ACKed, while delayed ACK waits up to
		// 40ms. This measured as ~55ms per 64 KiB chunk and about 10 seconds added
		// to an 11.24 MiB clone, so disable Nagle on this socket.
		if tcpConn, ok := conn.(*net.TCPConn); ok {
			_ = tcpConn.SetNoDelay(true)
		}
		if !p.acquireConn() {
			total := p.rejectedConns.Load()
			logger.Warn("egress connection cap reached; rejecting", "cap", cap(p.conns), "rejected", total)
			_ = conn.Close()
			continue
		}
		go p.handle(conn)
	}
}

const defaultListenAddr = "127.0.0.1:8888"

// caFetchHost is the reserved preamble name a guest uses to fetch the egress
// CA's PUBLIC certificate. It is never resolved and never dialled: handle
// answers it directly, before routing, so it cannot collide with a real
// destination or escape the node.
const caFetchHost = "ca.egress.internal"

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
	// secrets is the credential catalog (ADR 023 6b); empty disables injection.
	secrets   []secretEntry
	brokerURL string
	// minter mints leaf certs from the egress CA for TLS termination; nil disables
	// the TLS-MITM lane (the plaintext inject lane does not need it).
	minter *caMinter
	logger *slog.Logger
	// conns bounds concurrent guest connections. A nil channel leaves the proxy
	// unlimited for tests and other direct construction sites.
	conns         chan struct{}
	rejectedConns atomic.Int64
}

func (p *proxy) acquireConn() bool {
	if p.conns == nil {
		return true
	}
	select {
	case p.conns <- struct{}{}:
		return true
	default:
		p.rejectedConns.Add(1)
		return false
	}
}

func (p *proxy) releaseConn() {
	if p.conns != nil {
		<-p.conns
	}
}

// handle services one guest connection: read the "host:port" preamble, apply the
// split-horizon guardrail (resolve + classify + pin), then inject a credential for
// a secret-bearing destination or blind-tunnel to the pinned upstream.
func (p *proxy) handle(client net.Conn) {
	defer p.releaseConn()
	defer client.Close()
	br := bufio.NewReader(client)

	if err := client.SetReadDeadline(time.Now().Add(handshakeTimeout)); err != nil {
		p.logger.Warn("egress preamble deadline failed", "err", err)
		return
	}
	line := make([]byte, 0, maxPreambleBytes)
	for len(line) < maxPreambleBytes {
		b, err := br.ReadByte()
		if err != nil {
			if netErr, ok := err.(net.Error); ok && netErr.Timeout() {
				p.logger.Warn("egress preamble timeout", "err", err)
			} else {
				p.logger.Warn("egress preamble read failed", "err", err)
			}
			return
		}
		line = append(line, b)
		if b == '\n' {
			break
		}
	}
	if len(line) == maxPreambleBytes && line[len(line)-1] != '\n' {
		p.logger.Warn("egress preamble too long", "limit", maxPreambleBytes)
		return
	}
	host, port := splitHostPort(strings.TrimSpace(string(line)))
	if host == "" || port == "" {
		p.logger.Warn("egress preamble invalid", "preamble", strings.TrimSpace(string(line)))
		return
	}
	dest := net.JoinHostPort(host, port)

	// CA bootstrap. The guest cannot verify a MITM leaf until it trusts this CA,
	// and it cannot be given the CA over TLS, so it asks for it in the clear on a
	// reserved name that never leaves the node.
	//
	// This is not a trust hole: the link is a host-local vsock with no network
	// segment on it, and the process answering IS the trust anchor the guest is
	// about to rely on for every byte of egress. There is no weaker party to
	// impersonate. Fetching rather than baking also means a CA rotation does not
	// require rebuilding the fleet's guest base.
	if host == caFetchHost {
		if p.minter == nil {
			p.logger.Warn("egress: CA fetch requested but no CA is loaded", "dest", dest)
			return
		}
		if _, err := client.Write(p.minter.caCertPEM()); err != nil {
			p.logger.Warn("egress: CA fetch write failed", "err", err)
			return
		}
		p.logger.Info("egress: served CA certificate to guest")
		return
	}

	// Resolve + classify + pin. dialAddr is the exact ip:port we will connect to,
	// so the policy decision and the connect cannot diverge (no DNS-rebind race).
	dialAddr, ok := p.route(host, port)
	if !ok {
		p.logger.Warn("egress denied", "dest", dest)
		return
	}

	// Secret-bearing destination: inject the real credential (ADR 023 6b). We only need the first byte to tell TLS from plaintext; the host already
	// came from the preamble, so no SNI/Host sniffing is required.
	if sec := p.secretFor(host); sec != nil {
		if err := sec.resolve(); err != nil {
			p.logger.Error("egress denied: credential unresolved", "dest", dest, "env", sec.Env, "brokerGrant", sec.BrokerGrant, "err", err)
			return
		}
		// FAIL CLOSED. A credentialed host whose secret has not resolved must be
		// refused, never tunnelled: the guest addresses this host in CLEARTEXT
		// (ADR 023 6b), so falling through would put the full request, prompt and
		// all, on the public internet unencrypted. Denying costs a failed turn;
		// tunnelling costs a disclosure.
		if !sec.live() {
			p.logger.Error("egress denied: credential unresolved for host", "dest", dest, "env", sec.Env)
			return
		}
		first, err := br.Peek(1)
		switch {
		case isTimeout(err):
			p.logger.Warn("egress preamble timeout", "err", err)
			return
		case err != nil:
			// Nothing to classify; fall through to the blind tunnel below.
		case first[0] != 0x16:
			// The guest speaks http:// to us over its host-local vsock, so there is no
			// TLS to terminate. Re-run the guardrail on 443 rather than rewriting the
			// port after the fact, so the zone decision and the dial cannot diverge.
			tlsAddr, ok := p.route(host, "443")
			if !ok {
				p.logger.Warn("egress denied", "dest", net.JoinHostPort(host, "443"))
				return
			}
			p.logger.Info("egress allowed (inject, plaintext)", "dest", dest, "dial", tlsAddr)
			_ = client.SetReadDeadline(time.Time{})
			p.swapPlaintext(br, client, tlsAddr, host, sec)
			return
		case p.minter != nil:
			p.logger.Info("egress allowed (inject, tls)", "dest", dest, "dial", dialAddr)
			_ = client.SetReadDeadline(time.Time{})
			p.terminateAndSwap(br, client, dialAddr, host, sec)
			return
		}
		// TLS to a secret host with no CA loaded. Expected on the plaintext lane: the
		// claude CLI redirects only its API client to http://, and still reaches
		// api.anthropic.com over https:// for telemetry and profile calls. Those
		// carry no credential, are refused at the destination, and do not affect
		// the turn. Debug, not Warn: the startup line already reports
		// tlsMitmLane=false, and warning per connection would drown the log.
		p.logger.Debug("secret-bearing TLS destination with no egress CA; credential not injected", "dest", dest)
	}
	_ = client.SetReadDeadline(time.Time{})
	p.logger.Info("egress allowed", "dest", dest, "dial", dialAddr)

	up, err := net.DialTimeout("tcp", dialAddr, dialTimeout)
	if err != nil {
		p.logger.Error("egress upstream dial failed", "dest", dest, "dial", dialAddr, "err", err)
		return
	}
	// Git's protocol is request/response in small messages. Nagle holds each
	// write until the prior segment is ACKed, while delayed ACK waits up to
	// 40ms. This measured as ~55ms per 64 KiB chunk and about 10 seconds added
	// to an 11.24 MiB clone, so disable Nagle on this socket.
	if tcpConn, ok := up.(*net.TCPConn); ok {
		_ = tcpConn.SetNoDelay(true)
	}
	defer up.Close()

	// Pump both directions. br carries the peeked-but-unconsumed client bytes, so
	// the upstream sees the exact original stream, apart from absolute-form HTTP
	// request lines. The wrapper re-enters request-line detection after each
	// request body, so keep-alive connections are normalized request by request.
	done := make(chan struct{}, 2)
	go func() { _, _ = io.Copy(up, newOriginFormReader(br)); done <- struct{}{} }()
	go func() { _, _ = io.Copy(client, up); done <- struct{}{} }()
	<-done
}

func isTimeout(err error) bool {
	netErr, ok := err.(net.Error)
	return ok && netErr.Timeout()
}

// originFormReader is deliberately only a request-line filter, not a reverse
// proxy. It consumes headers solely to find the next request boundary and
// copies every header and body byte unchanged. If the stream does not begin
// with an HTTP request line, it fails open and becomes a byte-for-byte reader,
// which keeps git and other non-HTTP protocols on the blind tunnel.
type originFormReader struct {
	src         *bufio.Reader
	pending     []byte
	state       originFormReaderState
	contentLeft int64
	rewriteHost string
	headerBuf   []byte
	hasHost     bool
}

type originFormReaderState uint8

const (
	originFormRequestLine originFormReaderState = iota
	originFormHeaders
	originFormBody
	originFormPassthrough
)

func newOriginFormReader(src *bufio.Reader) io.Reader {
	return &originFormReader{src: src, state: originFormRequestLine}
}

func (r *originFormReader) Read(p []byte) (int, error) {
	for len(r.pending) == 0 {
		switch r.state {
		case originFormRequestLine:
			line, err := r.src.ReadString('\n')
			if err != nil {
				r.state = originFormPassthrough
				r.pending = []byte(line)
				if len(line) == 0 {
					return 0, err
				}
				break
			}
			rewritten, host, absolute, ok := classifyRequestLine(line)
			if !ok {
				r.state = originFormPassthrough
				r.pending = []byte(line)
				break
			}
			r.pending = []byte(line)
			if absolute {
				r.pending = []byte(rewritten)
			}
			r.rewriteHost = host
			r.headerBuf = r.headerBuf[:0]
			r.hasHost = false
			r.state = originFormHeaders
		case originFormHeaders:
			line, err := r.src.ReadString('\n')
			if len(line) == 0 && err != nil {
				r.state = originFormPassthrough
				return 0, err
			}
			if strings.EqualFold(strings.TrimSpace(strings.SplitN(line, ":", 2)[0]), "host") {
				r.hasHost = true
			}
			r.headerBuf = append(r.headerBuf, line...)
			if line == "\r\n" || line == "\n" {
				if r.rewriteHost != "" && !r.hasHost {
					r.headerBuf = append([]byte("Host: "+r.rewriteHost+"\r\n"), r.headerBuf...)
				}
				r.contentLeft = contentLength(r.headerBuf)
				r.pending = append(r.pending, r.headerBuf...)
				r.headerBuf = r.headerBuf[:0]
				if r.contentLeft > 0 {
					r.state = originFormBody
				} else {
					r.state = originFormRequestLine
				}
			}
			if err != nil {
				if len(r.headerBuf) > 0 {
					r.pending = append(r.pending, r.headerBuf...)
					r.headerBuf = r.headerBuf[:0]
				}
				r.state = originFormPassthrough
			}
		case originFormBody:
			if r.contentLeft == 0 {
				r.state = originFormRequestLine
				continue
			}
			n := len(p)
			if int64(n) > r.contentLeft {
				n = int(r.contentLeft)
			}
			buf := make([]byte, n)
			read, err := io.ReadFull(r.src, buf)
			r.pending = append(r.pending, buf[:read]...)
			r.contentLeft -= int64(read)
			if err != nil {
				r.state = originFormPassthrough
			}
		case originFormPassthrough:
			n, err := r.src.Read(p)
			return n, err
		}
	}

	n := copy(p, r.pending)
	r.pending = r.pending[n:]
	return n, nil
}

func classifyRequestLine(line string) (string, string, bool, bool) {
	parts := strings.Fields(strings.TrimRight(line, "\r\n"))
	if len(parts) != 3 || !strings.HasPrefix(parts[2], "HTTP/") {
		return "", "", false, false
	}
	if strings.HasPrefix(parts[1], "/") {
		return line, "", false, true
	}
	u, err := url.ParseRequestURI(parts[1])
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" {
		return "", "", false, false
	}
	origin := u.RequestURI()
	if origin == "" {
		origin = "/"
	}
	ending := "\n"
	if strings.HasSuffix(line, "\r\n") {
		ending = "\r\n"
	}
	return parts[0] + " " + origin + " " + parts[2] + ending, u.Host, true, true
}

func contentLength(headers []byte) int64 {
	for _, line := range strings.Split(string(headers), "\n") {
		parts := strings.SplitN(line, ":", 2)
		if len(parts) == 2 && strings.EqualFold(strings.TrimSpace(parts[0]), "content-length") {
			n, err := strconv.ParseInt(strings.TrimSpace(parts[1]), 10, 64)
			if err == nil && n >= 0 {
				return n
			}
		}
	}
	return 0
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

func maxConnsFromEnv(logger *slog.Logger) int {
	raw := os.Getenv("EGRESS_MAX_CONNS")
	if raw == "" {
		return defaultMaxConns
	}
	maxConns, err := strconv.Atoi(raw)
	if err != nil || maxConns < 1 {
		logger.Warn("invalid EGRESS_MAX_CONNS; using default", "value", raw, "default", defaultMaxConns)
		return defaultMaxConns
	}
	return maxConns
}
