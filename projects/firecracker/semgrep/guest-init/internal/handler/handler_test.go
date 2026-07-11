package handler

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"testing"

	"github.com/jomcgi/homelab/projects/firecracker/substrate/shim"
	"github.com/jomcgi/homelab/projects/firecracker/substrate/vsockproto"
)

// fakeScanner returns canned findings or a canned error for any scan request.
type fakeScanner struct {
	findings []vsockproto.Finding
	err      error
}

func (f *fakeScanner) Scan(_ vsockproto.ScanRequest) (vsockproto.ScanResult, error) {
	return vsockproto.ScanResult{Findings: f.findings}, f.err
}

// call is a test helper that invokes h with a string body.
func call(t *testing.T, h shim.Handler, body string) (*shim.Response, error) {
	t.Helper()
	return h(context.Background(), &shim.Request{Path: "/invoke", Body: strings.NewReader(body)})
}

// TestHandlerDecodeAndRoundTrip verifies that a valid ScanRequest body is
// decoded, the scanner is called, and the ScanResult is returned as JSON with
// the correct findings shape.
func TestHandlerDecodeAndRoundTrip(t *testing.T) {
	want := vsockproto.Finding{
		Path:     "foo.py",
		Line:     3,
		Col:      1,
		RuleID:   "rule.x",
		Severity: "ERROR",
		Message:  "bad code",
	}
	h := New(&fakeScanner{findings: []vsockproto.Finding{want}})

	resp, err := call(t, h, `{"files":[{"path":"foo.py","content":"x=1\n"}]}`)
	if err != nil {
		t.Fatalf("handler returned unexpected error: %v", err)
	}
	if resp.Status != 200 {
		t.Errorf("status %d, want 200", resp.Status)
	}

	var got vsockproto.ScanResult
	if err := json.Unmarshal(resp.Body, &got); err != nil {
		t.Fatalf("unmarshal response body: %v", err)
	}
	if len(got.Findings) != 1 || got.Findings[0] != want {
		t.Errorf("findings %+v, want [%+v]", got.Findings, want)
	}
	if len(got.Errors) != 0 {
		t.Errorf("errors %v, want empty", got.Errors)
	}
}

// TestHandlerBadBodyReturnsError verifies that an undecodable request body
// causes the handler to return a non-nil error (which the shim maps to 502).
func TestHandlerBadBodyReturnsError(t *testing.T) {
	h := New(&fakeScanner{})
	_, err := call(t, h, "not valid json {{")
	if err == nil {
		t.Fatal("expected non-nil error for undecodable body, got nil")
	}
}

// TestNewFullRoundTrip verifies NewFull uses the same decode/scan/encode path as
// New: a valid body is decoded, the full-scan func is called, and its result is
// returned as JSON. NewFull takes a ScanFunc directly (the subprocess path is a
// closure, not a Scanner).
func TestNewFullRoundTrip(t *testing.T) {
	want := vsockproto.Finding{Path: "b.py", Line: 5, Col: 5, RuleID: "taint.x", Severity: "ERROR", Message: "cross-file"}
	h := NewFull(func(_ vsockproto.ScanRequest) (vsockproto.ScanResult, error) {
		return vsockproto.ScanResult{Findings: []vsockproto.Finding{want}}, nil
	})

	resp, err := call(t, h, `{"files":[{"path":"b.py","content":"x=1\n"}]}`)
	if err != nil {
		t.Fatalf("handler returned unexpected error: %v", err)
	}
	if resp.Status != 200 {
		t.Errorf("status %d, want 200", resp.Status)
	}

	var got vsockproto.ScanResult
	if err := json.Unmarshal(resp.Body, &got); err != nil {
		t.Fatalf("unmarshal response body: %v", err)
	}
	if len(got.Findings) != 1 || got.Findings[0] != want {
		t.Errorf("findings %+v, want [%+v]", got.Findings, want)
	}
}

// TestHandlerScanErrorLandsInErrors verifies that a scanner error goes into
// ScanResult.Errors at HTTP 200 rather than propagating as a handler error,
// matching the partial-results semantics of the legacy scan-port RPC.
func TestHandlerScanErrorLandsInErrors(t *testing.T) {
	boom := errors.New("scan exploded")
	h := New(&fakeScanner{err: boom})

	resp, err := call(t, h, `{"files":[]}`)
	if err != nil {
		t.Fatalf("handler returned unexpected error: %v", err)
	}
	if resp.Status != 200 {
		t.Errorf("status %d, want 200", resp.Status)
	}

	var got vsockproto.ScanResult
	if err := json.Unmarshal(resp.Body, &got); err != nil {
		t.Fatalf("unmarshal response body: %v", err)
	}
	if len(got.Errors) != 1 || got.Errors[0] != boom.Error() {
		t.Errorf("errors %v, want [%q]", got.Errors, boom.Error())
	}
	if len(got.Findings) != 0 {
		t.Errorf("findings %+v, want empty", got.Findings)
	}
}
