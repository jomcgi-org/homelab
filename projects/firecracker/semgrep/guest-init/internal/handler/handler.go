// Package handler implements the shim.Handler for semgrep scan requests.
// It bridges the fc-invoke HTTP-over-vsock substrate to the resident
// scandriver.Driver, keeping the vsockproto.ScanRequest/ScanResult JSON schema
// backward-compatible with the legacy scan-port RPC.
package handler

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"regexp"

	"github.com/jomcgi/homelab/projects/firecracker/substrate/shim"
	"github.com/jomcgi/homelab/projects/firecracker/substrate/vsockproto"
)

// Scanner is the seam the handler uses to run a semgrep scan. *scandriver.Driver
// satisfies this; tests inject a fake.
type Scanner interface {
	Scan(req vsockproto.ScanRequest) (vsockproto.ScanResult, error)
}

// ScanFunc runs one scan and returns its result. Both the warm scan-server
// (Scanner.Scan) and the full-scan subprocess path satisfy this shape.
type ScanFunc func(vsockproto.ScanRequest) (vsockproto.ScanResult, error)

const maxCorrelationIDLength = 128

var correlationIDPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._:/-]*$`)

// validateCorrelationID keeps the only caller-controlled value written to the
// guest log bounded and free of control characters. The value is otherwise
// opaque so UUIDs, trace IDs, commit SHAs, and namespaced identifiers all work.
// An empty value means the optional metadata was omitted.
func validateCorrelationID(id string) error {
	if id == "" {
		return nil
	}
	if len(id) > maxCorrelationIDLength {
		return fmt.Errorf("must be at most %d bytes", maxCorrelationIDLength)
	}
	if !correlationIDPattern.MatchString(id) {
		return fmt.Errorf("must contain only letters, digits, dot, underscore, colon, slash, or hyphen")
	}
	return nil
}

// newHandler decodes a vsockproto.ScanRequest from the HTTP request body, runs
// it through scan, and writes a vsockproto.ScanResult as the response body
// (HTTP 200). Scan errors go into ScanResult.Errors rather than propagating as
// HTTP errors, preserving the partial-results semantics of the legacy scan-port
// RPC: a scan that ran but errored returns 200 with errors populated. An
// undecodable body or invalid correlation ID returns a non-nil error, which the
// shim maps to 502 before the scanner runs.
func newHandler(scan ScanFunc, logger *slog.Logger) shim.Handler {
	return func(_ context.Context, r *shim.Request) (*shim.Response, error) {
		var req vsockproto.ScanRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			return nil, fmt.Errorf("handler: decode scan request: %w", err)
		}
		if err := validateCorrelationID(req.CorrelationID); err != nil {
			return nil, fmt.Errorf("handler: invalid correlation_id: %w", err)
		}

		logAttrs := []any{"files", len(req.Files)}
		if req.CorrelationID != "" {
			logAttrs = append(logAttrs, "correlation_id", req.CorrelationID)
		}
		logger.Info("semgrep scan started", logAttrs...)

		result, scanErr := scan(req)
		// The request owns the correlation ID. Set it at the boundary even when a
		// scanner returns a partial or zero result alongside an error.
		result.CorrelationID = req.CorrelationID
		if scanErr != nil {
			result.Errors = append(result.Errors, scanErr.Error())
		}

		logAttrs = append(logAttrs, "findings", len(result.Findings), "errors", len(result.Errors))
		logger.Info("semgrep scan completed", logAttrs...)

		body, err := json.Marshal(result)
		if err != nil {
			return nil, fmt.Errorf("handler: marshal result: %w", err)
		}
		return &shim.Response{Status: 200, Body: body}, nil
	}
}

// New returns a shim.Handler backed by the warm scan-server driver (single-file
// mcp --pro). Behavior is unchanged from the original scan-port RPC.
func New(scanner Scanner) shim.Handler {
	return newHandler(scanner.Scan, slog.Default())
}

// NewFull returns a shim.Handler backed by the full-scan subprocess path
// (whole-tree semgrep scan --pro, interfile). Same request/response schema as
// New; only the scan implementation differs.
func NewFull(scan ScanFunc) shim.Handler {
	return newHandler(scan, slog.Default())
}
