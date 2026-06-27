// Command egress-proxy is a tiny, dependency-free HTTP forward proxy with a
// strict destination allowlist (ADR 023, Task 6a.1). In the egress design,
// fc-agentd forwards a guest microVM's vsock-1025 stream to this co-located
// sidecar, which forwards onward only to allowlisted upstreams. fc-agentd holds
// no secrets and does no parsing of the egress bytes; this proxy is the only
// process that decides whether a destination is permitted.
//
// This binary is the plain forward proxy only: no TLS termination and no secret
// or placeholder substitution (those land in Task 6b). It speaks both the
// CONNECT tunnel form (HTTPS) and the absolute-URI form (plain HTTP).
package main

import (
	"io"
	"log/slog"
	"net"
	"net/http"
	"os"
	"strings"
	"time"
)

func main() {
	logger := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: slog.LevelInfo}))
	slog.SetDefault(logger)

	// EGRESS_LISTEN is the address the proxy listens on (default ":8888"). The
	// guest reaches it via fc-agentd's vsock forward, so localhost-only is fine.
	listen := envOr("EGRESS_LISTEN", ":8888")

	// EGRESS_ALLOWLIST is a comma-separated list of permitted destinations. An
	// empty list denies everything (fail closed).
	allowlist := parseAllowlist(os.Getenv("EGRESS_ALLOWLIST"))

	logger.Info("egress-proxy starting", "listen", listen, "allowlist", allowlist)
	if len(allowlist) == 0 {
		logger.Warn("EGRESS_ALLOWLIST is empty; all egress will be denied (fail closed)")
	}

	p := &proxy{
		allowlist: allowlist,
		logger:    logger,
		// Proxy is left nil so this Transport never chains through another
		// proxy from the environment (which could loop back into ourselves).
		transport: &http.Transport{
			Proxy: nil,
			DialContext: (&net.Dialer{
				Timeout: 30 * time.Second,
			}).DialContext,
		},
	}

	srv := &http.Server{
		Addr:    listen,
		Handler: p,
		// ReadHeaderTimeout bounds the header read only; it does not cap the
		// lifetime of a hijacked CONNECT tunnel (no ReadTimeout/WriteTimeout).
		ReadHeaderTimeout: 30 * time.Second,
	}

	if err := srv.ListenAndServe(); err != nil {
		logger.Error("egress-proxy server exited", "err", err)
		os.Exit(1)
	}
}

// proxy is the HTTP handler implementing the forward proxy.
type proxy struct {
	allowlist []string
	logger    *slog.Logger
	transport *http.Transport
}

// ServeHTTP routes CONNECT tunnels and absolute-URI forwards.
func (p *proxy) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if r.Method == http.MethodConnect {
		p.handleConnect(w, r)
		return
	}
	p.handleHTTP(w, r)
}

// handleConnect serves an HTTPS tunnel: on an allowed destination it dials the
// target, replies "200 Connection established", hijacks the client connection,
// and copies bytes in both directions until either side closes. A denied
// destination gets a 403 and no tunnel.
func (p *proxy) handleConnect(w http.ResponseWriter, r *http.Request) {
	// For CONNECT the request target is the authority form "host:port" in r.Host.
	dest := r.Host
	if !allowed(dest, p.allowlist) {
		p.logger.Warn("egress denied", "method", r.Method, "dest", dest)
		http.Error(w, "destination not allowed", http.StatusForbidden)
		return
	}
	p.logger.Info("egress allowed", "method", r.Method, "dest", dest)

	upstream, err := net.DialTimeout("tcp", dest, 30*time.Second)
	if err != nil {
		p.logger.Error("egress dial failed", "dest", dest, "err", err)
		http.Error(w, "upstream dial failed", http.StatusBadGateway)
		return
	}
	defer upstream.Close()

	hijacker, ok := w.(http.Hijacker)
	if !ok {
		p.logger.Error("egress hijack unsupported", "dest", dest)
		http.Error(w, "hijacking not supported", http.StatusInternalServerError)
		return
	}
	client, _, err := hijacker.Hijack()
	if err != nil {
		p.logger.Error("egress hijack failed", "dest", dest, "err", err)
		return
	}
	defer client.Close()

	if _, err := io.WriteString(client, "HTTP/1.1 200 Connection established\r\n\r\n"); err != nil {
		p.logger.Error("egress tunnel handshake failed", "dest", dest, "err", err)
		return
	}

	// Pump both directions. When either copy finishes, the deferred Close calls
	// unblock the other goroutine; the buffered channel keeps it from leaking.
	done := make(chan struct{}, 2)
	go func() { _, _ = io.Copy(upstream, client); done <- struct{}{} }()
	go func() { _, _ = io.Copy(client, upstream); done <- struct{}{} }()
	<-done
}

// handleHTTP serves a plain (non-tunneled) proxied request. The destination is
// taken from the absolute URI's host; an allowed destination is forwarded via
// the shared Transport and the response is streamed back, a denied one gets 403.
func (p *proxy) handleHTTP(w http.ResponseWriter, r *http.Request) {
	dest := r.URL.Host
	if dest == "" {
		// A direct (origin-form) request is not a proxy request; reject it.
		http.Error(w, "absolute-URI proxy request required", http.StatusBadRequest)
		return
	}
	if !allowed(dest, p.allowlist) {
		p.logger.Warn("egress denied", "method", r.Method, "dest", dest)
		http.Error(w, "destination not allowed", http.StatusForbidden)
		return
	}
	p.logger.Info("egress allowed", "method", r.Method, "dest", dest)

	outReq := r.Clone(r.Context())
	outReq.RequestURI = "" // must be cleared before a request is sent by a client
	removeHopByHop(outReq.Header)

	resp, err := p.transport.RoundTrip(outReq)
	if err != nil {
		p.logger.Error("egress upstream request failed", "dest", dest, "err", err)
		http.Error(w, "upstream request failed", http.StatusBadGateway)
		return
	}
	defer resp.Body.Close()

	removeHopByHop(resp.Header)
	copyHeaders(w.Header(), resp.Header)
	w.WriteHeader(resp.StatusCode)
	if _, err := io.Copy(w, resp.Body); err != nil {
		p.logger.Warn("egress response copy interrupted", "dest", dest, "err", err)
	}
}

// allowed reports whether dest is permitted by allowlist.
//
// dest is the requested destination, either "host" or "host:port". Each
// allowlist entry is either "host" (matches any port on that host) or
// "host:port" (matches only that exact host and port). Host comparison is exact
// and case-insensitive: there is deliberately NO suffix or wildcard matching, so
// "api.example.com" does not match "evil-api.example.com" or "example.com". This
// strictness is intentional for a security control. An empty allowlist (or a
// dest matched by no entry) denies, so the default posture is fail closed.
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
		// An entry without a port matches any port; an entry with a port
		// requires an exact port match.
		if entryPort == "" || entryPort == destPort {
			return true
		}
	}
	return false
}

// splitHostPort splits "host:port" into its parts. A value with no port (or one
// net.SplitHostPort cannot parse, such as a bare IPv6 literal) is returned as
// host with an empty port.
func splitHostPort(s string) (host, port string) {
	if h, p, err := net.SplitHostPort(s); err == nil {
		return h, p
	}
	return s, ""
}

// parseAllowlist splits a comma-separated allowlist string, trimming whitespace
// and dropping empty entries.
func parseAllowlist(s string) []string {
	parts := strings.Split(s, ",")
	out := make([]string, 0, len(parts))
	for _, part := range parts {
		part = strings.TrimSpace(part)
		if part != "" {
			out = append(out, part)
		}
	}
	return out
}

// envOr returns the value of the environment variable key, or fallback if it is
// unset or empty.
func envOr(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

// hopByHopHeaders are connection-specific headers a forwarding proxy must strip
// rather than pass on (RFC 7230 section 6.1).
var hopByHopHeaders = []string{
	"Connection",
	"Proxy-Connection",
	"Keep-Alive",
	"Proxy-Authenticate",
	"Proxy-Authorization",
	"Te",
	"Trailer",
	"Transfer-Encoding",
	"Upgrade",
}

// removeHopByHop deletes hop-by-hop headers from h in place.
func removeHopByHop(h http.Header) {
	for _, name := range hopByHopHeaders {
		h.Del(name)
	}
}

// copyHeaders appends all values from src into dst.
func copyHeaders(dst, src http.Header) {
	for name, values := range src {
		for _, v := range values {
			dst.Add(name, v)
		}
	}
}
