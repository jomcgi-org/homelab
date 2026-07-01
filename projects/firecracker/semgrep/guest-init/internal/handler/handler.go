// Package handler implements the shim.Handler for semgrep scan requests.
// It bridges the fc-invoke HTTP-over-vsock substrate to the resident
// lspdriver.Driver, keeping the vsockproto.ScanRequest/ScanResult JSON schema
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

// Scanner is the seam the handler uses to run a semgrep scan. *lspdriver.Driver
// satisfies this; tests inject a fake.
type Scanner interface {
	Scan(ctx context.Context, files []vsockproto.ScanFile) ([]vsockproto.Finding, error)
}

// New returns a shim.Handler that decodes a vsockproto.ScanRequest from the
// HTTP request body, runs it through scanner, and writes a
// vsockproto.ScanResult as the response body (HTTP 200). Scan errors go into
// ScanResult.Errors rather than propagating as HTTP errors, preserving the
// partial-results semantics of the legacy scan-port RPC: a scan that ran but
// errored returns 200 with errors populated. Only an undecodable request body
// causes the handler to return a non-nil error, which the shim maps to 502.
func New(scanner Scanner) shim.Handler {
	return func(ctx context.Context, r *shim.Request) (*shim.Response, error) {
		var req vsockproto.ScanRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			return nil, fmt.Errorf("handler: decode scan request: %w", err)
		}

		var result vsockproto.ScanResult
		findings, scanErr := scanner.Scan(ctx, req.Files)
		if scanErr != nil {
			result.Errors = append(result.Errors, scanErr.Error())
		}
		result.Findings = findings

		body, err := json.Marshal(result)
		if err != nil {
			return nil, fmt.Errorf("handler: marshal result: %w", err)
		}
		return &shim.Response{Status: 200, Body: body}, nil
	}
}
