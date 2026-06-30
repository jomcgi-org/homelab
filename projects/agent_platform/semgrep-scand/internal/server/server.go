// Package server is the HTTP front of semgrep-scand. It exposes POST /scan
// (decode a batch of in-memory files, run them through a Scanner, encode the
// findings) and GET /healthz. The handler depends on the Scanner interface, not
// the Firecracker scanner, so the request/response path is unit-testable with a
// fake (no microVM).
//
// Status policy lives here, not in the Scanner: a scan that ran but produced
// per-file errors is data, returned 200 with an `errors` field; only an inability
// to launch a guest at all is an infra failure, returned 503. The Scanner signals
// that latter case with an error implementing GuestUnavailable() bool.
package server

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"

	"github.com/jomcgi/homelab/projects/firecracker/substrate/vsockproto"
)

// defaultMaxBytes caps the decoded request body. A scan batch is source files,
// not blobs; a multi-MiB request is almost certainly a mistake, and capping it
// keeps a single request from ballooning the daemon's memory.
const defaultMaxBytes int64 = 8 << 20 // 8 MiB

// Scanner runs one scan batch and returns its findings. The Firecracker scanner
// satisfies this; tests inject a fake. A returned error whose chain implements
// GuestUnavailable() bool means no guest could be launched (mapped to 503); any
// other returned error is folded into the response's errors field (200).
type Scanner interface {
	Scan(ctx context.Context, files []vsockproto.ScanFile) (vsockproto.ScanResult, error)
}

// Handler is the semgrep-scand http.Handler.
type Handler struct {
	scanner  Scanner
	logger   *slog.Logger
	maxBytes int64
}

// Option configures a Handler.
type Option func(*Handler)

// WithMaxBytes overrides the request-body cap (tests use a small value to drive
// the oversized-input path).
func WithMaxBytes(n int64) Option {
	return func(h *Handler) { h.maxBytes = n }
}

// New builds a Handler over the given Scanner.
func New(scanner Scanner, logger *slog.Logger, opts ...Option) *Handler {
	if logger == nil {
		logger = slog.Default()
	}
	h := &Handler{scanner: scanner, logger: logger, maxBytes: defaultMaxBytes}
	for _, opt := range opts {
		opt(h)
	}
	return h
}

// ServeHTTP routes the daemon's two endpoints.
func (h *Handler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	switch {
	case r.Method == http.MethodGet && r.URL.Path == "/healthz":
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok\n"))
	case r.Method == http.MethodPost && r.URL.Path == "/scan":
		h.handleScan(w, r)
	default:
		http.NotFound(w, r)
	}
}

func (h *Handler) handleScan(w http.ResponseWriter, r *http.Request) {
	// Cap the body before decoding so an oversized request is rejected without
	// reading it all into memory.
	r.Body = http.MaxBytesReader(w, r.Body, h.maxBytes)
	var req vsockproto.ScanRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		var maxErr *http.MaxBytesError
		if errors.As(err, &maxErr) {
			http.Error(w, "request body too large", http.StatusBadRequest)
			return
		}
		http.Error(w, "invalid scan request: "+err.Error(), http.StatusBadRequest)
		return
	}
	if len(req.Files) == 0 {
		http.Error(w, "scan request has no files", http.StatusBadRequest)
		return
	}

	res, err := h.scanner.Scan(r.Context(), req.Files)
	if err != nil {
		if isGuestUnavailable(err) {
			h.logger.Error("scan: no guest available", "err", err)
			http.Error(w, "scanner unavailable: "+err.Error(), http.StatusServiceUnavailable)
			return
		}
		// A scan that failed mid-flight is data, not a 5xx: surface it in the body.
		res.Errors = append(res.Errors, err.Error())
	}

	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	if err := json.NewEncoder(w).Encode(res); err != nil {
		h.logger.Warn("scan: encode response", "err", err)
	}
}

// guestUnavailable is implemented by a Scanner error that means no guest could
// be launched at all (an infra failure -> 503), as opposed to a scan that ran
// and produced errors (data -> 200). Keeping the check structural (an interface,
// not a concrete type) avoids a server->scanner import.
type guestUnavailable interface {
	GuestUnavailable() bool
}

func isGuestUnavailable(err error) bool {
	var u guestUnavailable
	return errors.As(err, &u) && u.GuestUnavailable()
}
