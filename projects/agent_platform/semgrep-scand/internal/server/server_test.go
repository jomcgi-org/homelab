package server

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/jomcgi/homelab/projects/agent_platform/vsockproto"
)

// fakeScanner is an injectable Scanner: it returns canned results/errors so the
// handler is testable with no microVM.
type fakeScanner struct {
	res vsockproto.ScanResult
	err error
}

func (f *fakeScanner) Scan(_ context.Context, _ []vsockproto.ScanFile) (vsockproto.ScanResult, error) {
	return f.res, f.err
}

// unavailableErr implements the guestUnavailable contract the handler maps to 503.
type unavailableErr struct{}

func (unavailableErr) Error() string          { return "no guest" }
func (unavailableErr) GuestUnavailable() bool { return true }

func post(t *testing.T, h http.Handler, body string) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest(http.MethodPost, "/scan", strings.NewReader(body))
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	return rec
}

func TestScanReturnsFindings(t *testing.T) {
	h := New(&fakeScanner{res: vsockproto.ScanResult{
		Findings: []vsockproto.Finding{{Path: "a.py", Line: 3, Col: 1, RuleID: "r1", Severity: "ERROR", Message: "bad"}},
	}}, nil)

	rec := post(t, h, `{"files":[{"path":"a.py","content":"x=1"}]}`)
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", rec.Code)
	}
	var got vsockproto.ScanResult
	if err := json.Unmarshal(rec.Body.Bytes(), &got); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	if len(got.Findings) != 1 || got.Findings[0].RuleID != "r1" {
		t.Errorf("findings = %+v, want one r1 finding", got.Findings)
	}
}

func TestScanEmptyFilesIs400(t *testing.T) {
	h := New(&fakeScanner{}, nil)
	rec := post(t, h, `{"files":[]}`)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400", rec.Code)
	}
}

func TestScanOversizedIs400(t *testing.T) {
	h := New(&fakeScanner{}, nil, WithMaxBytes(8))
	rec := post(t, h, `{"files":[{"path":"a.py","content":"a very long body that exceeds the cap"}]}`)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400 for oversized input", rec.Code)
	}
}

func TestScanInvalidJSONIs400(t *testing.T) {
	h := New(&fakeScanner{}, nil)
	rec := post(t, h, `{not json`)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400", rec.Code)
	}
}

func TestScanErrorSurfacesInBody200(t *testing.T) {
	h := New(&fakeScanner{err: errors.New("rule compile blew up")}, nil)
	rec := post(t, h, `{"files":[{"path":"a.py","content":"x=1"}]}`)
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200 (a scan failure is data)", rec.Code)
	}
	var got vsockproto.ScanResult
	if err := json.Unmarshal(rec.Body.Bytes(), &got); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	if len(got.Errors) != 1 || !strings.Contains(got.Errors[0], "rule compile") {
		t.Errorf("errors = %v, want the scan error surfaced", got.Errors)
	}
}

func TestScanGuestUnavailableIs503(t *testing.T) {
	h := New(&fakeScanner{err: unavailableErr{}}, nil)
	rec := post(t, h, `{"files":[{"path":"a.py","content":"x=1"}]}`)
	if rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("status = %d, want 503 when no guest can launch", rec.Code)
	}
}

func TestHealthz(t *testing.T) {
	h := New(&fakeScanner{}, nil)
	req := httptest.NewRequest(http.MethodGet, "/healthz", nil)
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", rec.Code)
	}
}
