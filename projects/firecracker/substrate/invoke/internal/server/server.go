// Package server is the HTTP front of the fc-invoke daemon. It exposes
// POST /invoke/{workload}[/{session}] (route the request to the Invoker
// registered for that workload and proxy the guest response through verbatim)
// and GET /healthz. The handler depends on the Invoker interface, not the
// concrete *invoker.Invoker, so the request/response path is unit-testable
// with a fake (no microVM).
//
// Status policy lives here, not in the Invoker: a failure to boot or obtain a
// guest at all is an infra failure returned 503 (the Invoker signals this with
// an error implementing GuestUnavailable() bool); any other error from Invoke
// means the VM ran but the HTTP round-trip failed and is returned 502. A
// request body that exceeds the cap is returned 413 before the Invoker is
// called.
package server

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"strings"
)

// defaultMaxBytes caps the request body forwarded to the guest. Workload
// bodies are not expected to be large; capping keeps a single request from
// ballooning the daemon's memory before the body is streamed to the guest.
const defaultMaxBytes int64 = 8 << 20 // 8 MiB

// Invoker runs one invocation for a workload. The real *invoker.Invoker
// satisfies it; tests inject a fake. A returned error whose chain implements
// GuestUnavailable() bool means no guest could be obtained (mapped to 503);
// any other error means the HTTP round-trip itself failed (mapped to 502).
type Invoker interface {
	Invoke(ctx context.Context, session string, body io.Reader) (*http.Response, error)
}

// Handler routes /invoke/{workload}[/{session}] to the Invoker registered for
// that workload and proxies the guest HTTP response to the caller verbatim.
type Handler struct {
	invokers map[string]Invoker
	logger   *slog.Logger
	maxBytes int64
}

// Option configures a Handler.
type Option func(*Handler)

// WithMaxBytes overrides the request-body cap. Tests use a small value to
// drive the oversized-input rejection path.
func WithMaxBytes(n int64) Option {
	return func(h *Handler) { h.maxBytes = n }
}

// New builds a Handler over the given workload-to-Invoker map.
func New(invokers map[string]Invoker, logger *slog.Logger, opts ...Option) *Handler {
	if logger == nil {
		logger = slog.Default()
	}
	h := &Handler{invokers: invokers, logger: logger, maxBytes: defaultMaxBytes}
	for _, opt := range opts {
		opt(h)
	}
	return h
}

// ServeHTTP routes the daemon's endpoints.
func (h *Handler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	switch {
	case r.Method == http.MethodGet && r.URL.Path == "/healthz":
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok\n"))
	case r.Method == http.MethodPost && strings.HasPrefix(r.URL.Path, "/invoke/"):
		h.handleInvoke(w, r)
	default:
		http.NotFound(w, r)
	}
}

func (h *Handler) handleInvoke(w http.ResponseWriter, r *http.Request) {
	// Parse workload and optional session from the path.
	// /invoke/{workload}           -> workload set, session ""
	// /invoke/{workload}/{session} -> both set
	rest := strings.TrimPrefix(r.URL.Path, "/invoke/")
	parts := strings.SplitN(rest, "/", 2)
	workload := parts[0]
	var session string
	if len(parts) == 2 {
		session = parts[1]
	}

	if workload == "" {
		http.Error(w, "missing workload in path", http.StatusNotFound)
		return
	}

	inv, ok := h.invokers[workload]
	if !ok {
		http.Error(w, "unknown workload: "+workload, http.StatusNotFound)
		return
	}

	// Cap the body before the invoker reads it so an oversized request is
	// rejected without consuming unbounded memory.
	r.Body = http.MaxBytesReader(w, r.Body, h.maxBytes)

	resp, err := inv.Invoke(r.Context(), session, r.Body)
	if err != nil {
		var maxErr *http.MaxBytesError
		if errors.As(err, &maxErr) {
			http.Error(w, "request body too large", http.StatusRequestEntityTooLarge)
			return
		}
		if isGuestUnavailable(err) {
			h.logger.Error("invoke: no guest available", "workload", workload, "session", session, "err", err)
			http.Error(w, "guest unavailable: "+err.Error(), http.StatusServiceUnavailable)
			return
		}
		h.logger.Error("invoke: round-trip failed", "workload", workload, "session", session, "err", err)
		http.Error(w, "invoke failed: "+err.Error(), http.StatusBadGateway)
		return
	}
	defer resp.Body.Close()

	// Copy the guest response through verbatim: status code, content-type
	// header (when present), and body stream.
	if ct := resp.Header.Get("Content-Type"); ct != "" {
		w.Header().Set("Content-Type", ct)
	}
	w.WriteHeader(resp.StatusCode)
	if _, err := io.Copy(w, resp.Body); err != nil {
		h.logger.Warn("invoke: stream response body", "workload", workload, "session", session, "err", err)
	}
}

// guestUnavailable is the structural interface the server uses to map a
// failed-boot or slot-unavailable error to 503, without importing the concrete
// invoker package. The real *invoker.GuestUnavailableError satisfies it.
type guestUnavailable interface {
	GuestUnavailable() bool
}

func isGuestUnavailable(err error) bool {
	var u guestUnavailable
	return errors.As(err, &u) && u.GuestUnavailable()
}
