// Command egress-proxy is a tiny, dependency-free transparent egress proxy that
// mediates every Firecracker guest's outbound traffic (ADR 023). The guest is
// vsock-only: fc-agent-init answers every DNS query with 127.0.0.1 and funnels
// each loopback connection over vsock to fc-agentd, which forwards it here. This
// sidecar is the only process that reaches the network on the guest's behalf.
//
// Each connection arrives as: a one-line preamble carrying the original
// destination port (the guest funnel knows it from the listening port), then the
// raw client stream. The proxy recovers the destination HOST by peeking the
// stream, the TLS SNI for an HTTPS ClientHello, or the HTTP Host header for
// plaintext, without consuming the bytes, so the exact stream still forwards to
// the upstream. It then applies policy and pipes to the real destination (the pod
// resolves cluster and public DNS).
//
// Egress posture is set by EGRESS_POLICY:
//   - "allow" (default): every destination is permitted. Secrets still only
//     materialise at their bound destination (Task 6b), so the open path cannot
//     leak a credential; it can only browse. This lets the agent read arbitrary
//     docs.
//   - "allowlist": only EGRESS_ALLOWLIST destinations are permitted; everything
//     else is denied (fail closed). The dormant lockdown knob.
//
// This binary does plain transparent routing only: TLS termination and secret /
// placeholder substitution for destinations that carry a credential land in Task
// 6b (the proxy already sees the destination, so that is an additive branch).
package main

import (
	"bufio"
	"bytes"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"os"
	"strconv"
	"strings"
	"time"
)

// Egress policies (EGRESS_POLICY).
const (
	policyAllow     = "allow"
	policyAllowlist = "allowlist"
)

// dialTimeout bounds the upstream connect; it does not cap a tunnel's lifetime.
const dialTimeout = 30 * time.Second

func main() {
	logger := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: slog.LevelInfo}))
	slog.SetDefault(logger)

	// EGRESS_LISTEN is where fc-agentd forwards guest egress (pod-local).
	listen := envOr("EGRESS_LISTEN", ":8888")

	// EGRESS_POLICY: "allow" (default) permits every destination; "allowlist"
	// permits only EGRESS_ALLOWLIST and denies the rest.
	policy := envOr("EGRESS_POLICY", policyAllow)

	// EGRESS_ALLOWLIST is consulted only under policy=allowlist (comma-separated
	// host[:port]). Empty there denies everything.
	allowlist := parseAllowlist(os.Getenv("EGRESS_ALLOWLIST"))

	logger.Info("egress-proxy starting", "listen", listen, "policy", policy, "allowlist", allowlist)
	if policy == policyAllowlist && len(allowlist) == 0 {
		logger.Warn("policy=allowlist with empty EGRESS_ALLOWLIST; all egress will be denied (fail closed)")
	}

	// Secret placeholder-swap (ADR 023 6b): load the catalog and the CA the sidecar
	// uses to TLS-terminate secret-bearing destinations. Absent CA paths or an empty
	// catalog leave the proxy a plain transparent router (6a).
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

	p := &proxy{policy: policy, allowlist: allowlist, secrets: secrets, minter: minter, logger: logger}

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

// proxy holds the egress posture, the secret catalog, and the CA minter.
type proxy struct {
	// policy is the egress posture: policyAllow (default) permits every
	// destination, policyAllowlist permits only allowlist entries.
	policy string
	// allowlist is consulted only under policyAllowlist.
	allowlist []string
	// secrets is the placeholder-swap catalog (ADR 023 6b); empty disables swap.
	secrets []secretEntry
	// minter mints leaf certs from the egress CA for TLS termination; nil disables
	// the swap path (the proxy stays a plain transparent router).
	minter *caMinter
	logger *slog.Logger
}

// permits reports whether dest may be reached under the current policy.
func (p *proxy) permits(dest string) bool {
	if p.policy != policyAllowlist {
		return true
	}
	return allowed(dest, p.allowlist)
}

// handle services one guest connection: read the port preamble, recover the host
// from the stream (SNI or Host header), apply policy, then pipe to the upstream.
func (p *proxy) handle(client net.Conn) {
	defer client.Close()
	// Size the buffer to maxHeadPeek so the head scan (and the SNI parse) can peek
	// up to the cap without hitting ErrBufferFull below the cap.
	br := bufio.NewReaderSize(client, maxHeadPeek)

	portLine, err := br.ReadString('\n')
	if err != nil {
		p.logger.Warn("egress preamble read failed", "err", err)
		return
	}
	port := strings.TrimSpace(portLine)
	if _, err := strconv.Atoi(port); err != nil {
		p.logger.Warn("egress preamble invalid port", "preamble", port)
		return
	}

	host, isTLS, err := hostFromStream(br)
	if err != nil {
		p.logger.Warn("egress host detection failed", "port", port, "err", err)
		return
	}
	dest := net.JoinHostPort(host, port)

	if !p.permits(dest) {
		p.logger.Warn("egress denied", "dest", dest)
		return
	}

	// Secret-bearing TLS destination: terminate, swap the placeholder, re-originate
	// (ADR 023 6b). Everything else is blind-tunnelled (6a). The swap fires only for
	// a host in the secret's egressTo, so the real value is unreachable elsewhere.
	if p.minter != nil && isTLS {
		if sec := p.secretFor(host); sec != nil {
			p.logger.Info("egress allowed (swap)", "dest", dest)
			p.terminateAndSwap(br, client, dest, host, sec)
			return
		}
	}
	p.logger.Info("egress allowed", "dest", dest)

	up, err := net.DialTimeout("tcp", dest, dialTimeout)
	if err != nil {
		p.logger.Error("egress upstream dial failed", "dest", dest, "err", err)
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

// hostFromStream peeks the buffered client stream and returns the destination
// host and whether the stream is TLS: the TLS SNI for a handshake record,
// otherwise the HTTP Host header. It never consumes bytes, so the stream forwards
// to the upstream verbatim.
func hostFromStream(br *bufio.Reader) (host string, isTLS bool, err error) {
	first, err := br.Peek(1)
	if err != nil {
		return "", false, err
	}
	if first[0] == 0x16 { // TLS handshake record
		host, err = sniFromClientHello(br)
		return host, true, err
	}
	host, err = hostFromHTTP(br)
	return host, false, err
}

// maxHeadPeek bounds how far we peek looking for the SNI or Host header.
const maxHeadPeek = 8192

// hostFromHTTP scans the buffered request head for the Host header without
// consuming it (the raw bytes are blind-tunnelled to the upstream afterwards).
//
// It peeks only what is already buffered and forces just ONE more byte into the
// buffer per iteration when the head is not yet complete. A fixed-size Peek(n)
// would block until n bytes arrive, which deadlocks on a request whose entire
// head is shorter than n: a bodyless GET (or any small request) sends its full
// head, then waits for the response, so the missing bytes never come and Peek
// hangs until the client's own timeout. (Large POSTs and TLS ClientHellos hide
// this by exceeding n on the first read.) The full head normally lands in the
// first read, so the per-byte growth loop iterates at most a couple of times.
func hostFromHTTP(br *bufio.Reader) (string, error) {
	for {
		if buffered := br.Buffered(); buffered > 0 {
			buf, _ := br.Peek(buffered) // already in the buffer: never blocks
			if host, ok := scanHostHeader(buf); ok {
				return host, nil
			}
			if bytes.Contains(buf, []byte("\r\n\r\n")) {
				return "", errors.New("no Host header in request")
			}
			if buffered >= maxHeadPeek {
				return "", errors.New("HTTP head exceeds cap without a Host header")
			}
		}
		// Force one more byte (and whatever else arrives with it) into the buffer.
		// Blocks only for genuinely-in-flight head bytes, never for a head that is
		// already complete.
		if _, err := br.Peek(br.Buffered() + 1); err != nil {
			return "", fmt.Errorf("incomplete HTTP head (buffered %d): %w", br.Buffered(), err)
		}
	}
}

// scanHostHeader returns the value of the Host header (port stripped) if present
// in buf. It returns false if the header is absent in the bytes scanned or the
// blank-line header terminator is reached without one.
func scanHostHeader(buf []byte) (string, bool) {
	for len(buf) > 0 {
		var line []byte
		if nl := bytes.IndexByte(buf, '\n'); nl >= 0 {
			line, buf = buf[:nl], buf[nl+1:]
		} else {
			line, buf = buf, nil
		}
		line = bytes.TrimRight(line, "\r")
		if len(line) == 0 {
			return "", false // end of headers, no Host
		}
		const key = "host:"
		if len(line) >= len(key) && bytes.EqualFold(line[:len(key)], []byte(key)) {
			v := strings.TrimSpace(string(line[len(key):]))
			if h, _, err := net.SplitHostPort(v); err == nil {
				return h, true
			}
			return v, true
		}
	}
	return "", false
}

// sniFromClientHello peeks a TLS ClientHello and returns its SNI host_name.
func sniFromClientHello(br *bufio.Reader) (string, error) {
	hdr, err := br.Peek(5)
	if err != nil {
		return "", err
	}
	total := 5 + (int(hdr[3])<<8 | int(hdr[4]))
	if total > maxHeadPeek {
		total = maxHeadPeek
	}
	buf, _ := br.Peek(total) // best effort: parse whatever is buffered
	return parseSNI(buf)
}

var errClientHelloShort = errors.New("clienthello truncated")

// parseSNI extracts the server_name (host_name) from a TLS ClientHello record.
func parseSNI(b []byte) (string, error) {
	// record header (5) + handshake header (4) + client_version (2) + random (32).
	pos := 5 + 4 + 2 + 32
	if len(b) < 6 || b[5] != 0x01 {
		return "", errors.New("not a ClientHello")
	}
	if pos+1 > len(b) {
		return "", errClientHelloShort
	}
	pos += 1 + int(b[pos]) // session_id
	if pos+2 > len(b) {
		return "", errClientHelloShort
	}
	pos += 2 + (int(b[pos])<<8 | int(b[pos+1])) // cipher_suites
	if pos+1 > len(b) {
		return "", errClientHelloShort
	}
	pos += 1 + int(b[pos]) // compression_methods
	if pos+2 > len(b) {
		return "", errClientHelloShort
	}
	end := pos + 2 + (int(b[pos])<<8 | int(b[pos+1])) // extensions block
	pos += 2
	if end > len(b) {
		end = len(b)
	}
	for pos+4 <= end {
		extType := int(b[pos])<<8 | int(b[pos+1])
		extLen := int(b[pos+2])<<8 | int(b[pos+3])
		pos += 4
		if pos+extLen > len(b) {
			break
		}
		if extType == 0x0000 { // server_name
			if host, ok := parseServerName(b[pos : pos+extLen]); ok {
				return host, nil
			}
		}
		pos += extLen
	}
	return "", errors.New("no SNI in ClientHello")
}

// parseServerName extracts the first host_name from a server_name extension body.
func parseServerName(sn []byte) (string, bool) {
	if len(sn) < 2 {
		return "", false
	}
	listEnd := 2 + (int(sn[0])<<8 | int(sn[1]))
	if listEnd > len(sn) {
		listEnd = len(sn)
	}
	for p := 2; p+3 <= listEnd; {
		nameType := sn[p]
		nameLen := int(sn[p+1])<<8 | int(sn[p+2])
		p += 3
		if p+nameLen > len(sn) {
			break
		}
		if nameType == 0 { // host_name
			return string(sn[p : p+nameLen]), true
		}
		p += nameLen
	}
	return "", false
}

// allowed reports whether dest is permitted by allowlist (consulted only under
// policyAllowlist).
//
// dest is "host" or "host:port". Each allowlist entry is "host" (any port) or
// "host:port" (that exact port). Host comparison is exact and case-insensitive:
// there is deliberately NO suffix or wildcard matching, so "api.example.com" does
// not match "evil-api.example.com". An empty allowlist (or no match) denies, so
// the locked-down posture is fail closed.
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
