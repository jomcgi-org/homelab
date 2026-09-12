package handler

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"strings"
	"sync"
	"testing"

	"github.com/jomcgi/homelab/projects/firecracker/substrate/shim"
	"github.com/jomcgi/homelab/projects/firecracker/substrate/vsockproto"
)

// fakeScanner returns canned findings or a canned error for any scan request.
type fakeScanner struct {
	findings []vsockproto.Finding
	err      error
	requests []vsockproto.ScanRequest
}

func (f *fakeScanner) Scan(req vsockproto.ScanRequest) (vsockproto.ScanResult, error) {
	f.requests = append(f.requests, req)
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
	scanner := &fakeScanner{findings: []vsockproto.Finding{want}}
	h := New(scanner)

	resp, err := call(t, h, `{"files":[{"path":"foo.py","content":"x=1\n"}],"correlation_id":"scan-123"}`)
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
	if got.CorrelationID != "scan-123" {
		t.Errorf("correlation_id %q, want scan-123", got.CorrelationID)
	}
	if len(scanner.requests) != 1 || scanner.requests[0].CorrelationID != "scan-123" {
		t.Errorf("scanner requests %+v, want correlation_id scan-123", scanner.requests)
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
	var scanned vsockproto.ScanRequest
	h := NewFull(func(req vsockproto.ScanRequest) (vsockproto.ScanResult, error) {
		scanned = req
		return vsockproto.ScanResult{Findings: []vsockproto.Finding{want}}, nil
	})

	resp, err := call(t, h, `{"files":[{"path":"b.py","content":"x=1\n"}],"correlation_id":"full-456"}`)
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
	if got.CorrelationID != "full-456" || scanned.CorrelationID != "full-456" {
		t.Errorf("correlation ids response=%q request=%q, want full-456", got.CorrelationID, scanned.CorrelationID)
	}
}

// TestHandlerScanErrorLandsInErrors verifies that a scanner error goes into
// ScanResult.Errors at HTTP 200 rather than propagating as a handler error,
// matching the partial-results semantics of the legacy scan-port RPC.
func TestHandlerScanErrorLandsInErrors(t *testing.T) {
	boom := errors.New("scan exploded")
	h := New(&fakeScanner{err: boom})

	resp, err := call(t, h, `{"files":[],"correlation_id":"failed-789"}`)
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
	if got.CorrelationID != "failed-789" {
		t.Errorf("correlation_id %q, want failed-789", got.CorrelationID)
	}
}

func TestHandlerSuccessiveMetadataDoesNotLeak(t *testing.T) {
	h := newHandler(func(req vsockproto.ScanRequest) (vsockproto.ScanResult, error) {
		return vsockproto.ScanResult{}, nil
	}, slog.New(slog.NewTextHandler(io.Discard, nil)))

	for _, id := range []string{"first-id", "second-id", ""} {
		body := `{"files":[]}`
		if id != "" {
			body = fmt.Sprintf(`{"files":[],"correlation_id":%q}`, id)
		}
		resp, err := call(t, h, body)
		if err != nil {
			t.Fatalf("correlation_id %q: %v", id, err)
		}
		var got vsockproto.ScanResult
		if err := json.Unmarshal(resp.Body, &got); err != nil {
			t.Fatalf("correlation_id %q: unmarshal: %v", id, err)
		}
		if got.CorrelationID != id {
			t.Errorf("correlation_id %q, want %q", got.CorrelationID, id)
		}
		if id == "" && bytes.Contains(resp.Body, []byte("correlation_id")) {
			t.Errorf("omitted correlation_id leaked from prior request: %s", resp.Body)
		}
	}
}

func TestHandlerLogsOnlyValidatedCorrelationID(t *testing.T) {
	var logs bytes.Buffer
	logger := slog.New(slog.NewJSONHandler(&logs, nil))
	h := newHandler(func(req vsockproto.ScanRequest) (vsockproto.ScanResult, error) {
		return vsockproto.ScanResult{}, nil
	}, logger)

	if _, err := call(t, h, `{"files":[],"correlation_id":"trace/abc-123"}`); err != nil {
		t.Fatalf("valid request: %v", err)
	}
	if !strings.Contains(logs.String(), `"correlation_id":"trace/abc-123"`) {
		t.Fatalf("validated correlation id missing from logs: %s", logs.String())
	}

	logs.Reset()
	called := false
	invalid := newHandler(func(req vsockproto.ScanRequest) (vsockproto.ScanResult, error) {
		called = true
		return vsockproto.ScanResult{}, nil
	}, logger)
	secret := "do not log this secret"
	_, err := call(t, invalid, fmt.Sprintf(`{"files":[],"correlation_id":%q}`, secret))
	if err == nil || !strings.Contains(err.Error(), "invalid correlation_id") {
		t.Fatalf("invalid correlation id error = %v", err)
	}
	if called {
		t.Fatal("scanner called for invalid correlation id")
	}
	if strings.Contains(logs.String(), secret) || strings.Contains(err.Error(), secret) {
		t.Fatalf("invalid correlation id was exposed: error=%q logs=%q", err, logs.String())
	}
}

func TestHandlerRejectsOversizedCorrelationID(t *testing.T) {
	h := newHandler(func(req vsockproto.ScanRequest) (vsockproto.ScanResult, error) {
		t.Fatal("scanner called for oversized correlation id")
		return vsockproto.ScanResult{}, nil
	}, slog.New(slog.NewTextHandler(io.Discard, nil)))

	body := fmt.Sprintf(`{"files":[],"correlation_id":%q}`, strings.Repeat("a", maxCorrelationIDLength+1))
	if _, err := call(t, h, body); err == nil || !strings.Contains(err.Error(), "at most 128 bytes") {
		t.Fatalf("oversized correlation id error = %v", err)
	}
}

func TestHandlerOverlappingScansKeepMetadataRequestLocal(t *testing.T) {
	entered := make(chan string, 2)
	release := make(chan struct{})
	h := newHandler(func(req vsockproto.ScanRequest) (vsockproto.ScanResult, error) {
		entered <- req.CorrelationID
		<-release
		return vsockproto.ScanResult{}, nil
	}, slog.New(slog.NewTextHandler(io.Discard, nil)))

	type outcome struct {
		id   string
		resp *shim.Response
		err  error
	}
	outcomes := make(chan outcome, 2)
	var wg sync.WaitGroup
	for _, id := range []string{"overlap-one", "overlap-two"} {
		id := id
		wg.Add(1)
		go func() {
			defer wg.Done()
			resp, err := call(t, h, fmt.Sprintf(`{"files":[],"correlation_id":%q}`, id))
			outcomes <- outcome{id: id, resp: resp, err: err}
		}()
	}

	seen := map[string]bool{<-entered: true, <-entered: true}
	if !seen["overlap-one"] || !seen["overlap-two"] {
		t.Fatalf("overlapping scanner calls saw ids %v", seen)
	}
	close(release)
	wg.Wait()
	close(outcomes)

	for out := range outcomes {
		if out.err != nil {
			t.Fatalf("correlation_id %q: %v", out.id, out.err)
		}
		var got vsockproto.ScanResult
		if err := json.Unmarshal(out.resp.Body, &got); err != nil {
			t.Fatalf("correlation_id %q: unmarshal: %v", out.id, err)
		}
		if got.CorrelationID != out.id {
			t.Errorf("overlapping result correlation_id %q, want %q", got.CorrelationID, out.id)
		}
	}
}
