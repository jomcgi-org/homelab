// Package handler implements the shim.Handler for semgrep scan requests.
// It bridges the fc-invoke HTTP-over-vsock substrate to the resident
// scandriver.Driver, keeping the vsockproto.ScanRequest/ScanResult JSON schema
// byte-identical to the legacy scan-port RPC so the MCP response shape is
// unchanged.
package handler

import (
	"context"
	"encoding/json"
	"fmt"

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

// newHandler decodes a vsockproto.ScanRequest from the HTTP request body, runs
// it through scan, and writes a vsockproto.ScanResult as the response body
// (HTTP 200). Scan errors go into ScanResult.Errors rather than propagating as
// HTTP errors, preserving the partial-results semantics of the legacy scan-port
// RPC: a scan that ran but errored returns 200 with errors populated. Only an
// undecodable request body causes a non-nil error, which the shim maps to 502.
func newHandler(scan ScanFunc) shim.Handler {
	return func(_ context.Context, r *shim.Request) (*shim.Response, error) {
		var req vsockproto.ScanRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			return nil, fmt.Errorf("handler: decode scan request: %w", err)
		}

		result, scanErr := scan(req)
		if scanErr != nil {
			result.Errors = append(result.Errors, scanErr.Error())
		}

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
	return newHandler(scanner.Scan)
}

// NewFull returns a shim.Handler backed by the full-scan subprocess path
// (whole-tree semgrep scan --pro, interfile). Same request/response schema as
// New; only the scan implementation differs.
func NewFull(scan ScanFunc) shim.Handler {
	return newHandler(scan)
}
