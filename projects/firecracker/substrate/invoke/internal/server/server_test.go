package server

import (
	"context"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// fakeInvoker is an injectable Invoker: it drains the request body (so the
// MaxBytesReader cap is exercised), records the session for assertions, and
// returns canned responses or errors.
type fakeInvoker struct {
	capturedSession string
	resp            *http.Response
	err             error
}

func (f *fakeInvoker) Invoke(_ context.Context, session string, body io.Reader) (*http.Response, error) {
	f.capturedSession = session
	// Drain the body so MaxBytesReader cap errors surface through the Invoke
	// call rather than being silently swallowed.
	if _, err := io.ReadAll(body); err != nil {
		return nil, err
	}
	return f.resp, f.err
}

// bodyResponse builds a minimal *http.Response with the given status and body.
func bodyResponse(code int, body string, ct string) *http.Response {
	h := make(http.Header)
	if ct != "" {
		h.Set("Content-Type", ct)
	}
	return &http.Response{
		StatusCode: code,
		Header:     h,
		Body:       io.NopCloser(strings.NewReader(body)),
	}
}

// unavailableErr implements the GuestUnavailable() bool contract the handler
// maps to 503, as opposed to a plain error which maps to 502.
type unavailableErr struct{}

func (unavailableErr) Error() string          { return "no guest" }
func (unavailableErr) GuestUnavailable() bool { return true }

func TestHealthz(t *testing.T) {
	h := New(map[string]Invoker{}, nil)
	req := httptest.NewRequest(http.MethodGet, "/healthz", nil)
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", rec.Code)
	}
}

func TestInvokeRoutesToWorkload(t *testing.T) {
	fake := &fakeInvoker{resp: bodyResponse(200, "findings", "application/json")}
	h := New(map[string]Invoker{"semgrep": fake}, nil)

	req := httptest.NewRequest(http.MethodPost, "/invoke/semgrep", strings.NewReader("req-body"))
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", rec.Code)
	}
	if got := rec.Body.String(); got != "findings" {
		t.Errorf("body = %q, want %q", got, "findings")
	}
	// Content-Type must be forwarded from the guest response.
	if got := rec.Header().Get("Content-Type"); got != "application/json" {
		t.Errorf("Content-Type = %q, want application/json", got)
	}
}

func TestInvokeParsesSession(t *testing.T) {
	fake := &fakeInvoker{resp: bodyResponse(200, "", "")}
	h := New(map[string]Invoker{"agent": fake}, nil)

	req := httptest.NewRequest(http.MethodPost, "/invoke/agent/t-abc", strings.NewReader(""))
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", rec.Code)
	}
	if fake.capturedSession != "t-abc" {
		t.Errorf("session = %q, want %q", fake.capturedSession, "t-abc")
	}
}

func TestInvokeNoSessionIsEmpty(t *testing.T) {
	fake := &fakeInvoker{resp: bodyResponse(200, "", "")}
	h := New(map[string]Invoker{"semgrep": fake}, nil)

	req := httptest.NewRequest(http.MethodPost, "/invoke/semgrep", strings.NewReader(""))
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", rec.Code)
	}
	if fake.capturedSession != "" {
		t.Errorf("session = %q, want empty string for no-session path", fake.capturedSession)
	}
}

func TestUnknownWorkload404(t *testing.T) {
	h := New(map[string]Invoker{}, nil)

	req := httptest.NewRequest(http.MethodPost, "/invoke/nope", strings.NewReader(""))
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)

	if rec.Code != http.StatusNotFound {
		t.Fatalf("status = %d, want 404 for unregistered workload", rec.Code)
	}
}

func TestGuestUnavailableIs503(t *testing.T) {
	fake := &fakeInvoker{err: unavailableErr{}}
	h := New(map[string]Invoker{"semgrep": fake}, nil)

	req := httptest.NewRequest(http.MethodPost, "/invoke/semgrep", strings.NewReader(""))
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)

	if rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("status = %d, want 503 when no guest can be obtained", rec.Code)
	}
}

func TestOtherErrorIs502(t *testing.T) {
	fake := &fakeInvoker{err: errors.New("round-trip failed")}
	h := New(map[string]Invoker{"semgrep": fake}, nil)

	req := httptest.NewRequest(http.MethodPost, "/invoke/semgrep", strings.NewReader(""))
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)

	if rec.Code != http.StatusBadGateway {
		t.Fatalf("status = %d, want 502 for non-guest-unavailable error", rec.Code)
	}
}

func TestBodyCapEnforced(t *testing.T) {
	// The fake reads the body; the MaxBytesReader cap surfaces as an error
	// returned from Invoke, which the handler maps to 413.
	fake := &fakeInvoker{resp: bodyResponse(200, "", "")}
	h := New(map[string]Invoker{"semgrep": fake}, nil, WithMaxBytes(8))

	req := httptest.NewRequest(http.MethodPost, "/invoke/semgrep", strings.NewReader("this body is definitely longer than 8 bytes"))
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)

	if rec.Code < 400 {
		t.Fatalf("status = %d, want non-2xx for oversized body", rec.Code)
	}
}

func TestGuestStatusCodeProxied(t *testing.T) {
	// A non-200 success code from the guest (e.g. 201, 204) must be forwarded
	// verbatim rather than normalised to 200.
	fake := &fakeInvoker{resp: bodyResponse(201, "created", "")}
	h := New(map[string]Invoker{"agent": fake}, nil)

	req := httptest.NewRequest(http.MethodPost, "/invoke/agent", strings.NewReader(""))
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)

	if rec.Code != 201 {
		t.Fatalf("status = %d, want 201 (guest status must pass through verbatim)", rec.Code)
	}
}

func TestUnknownPathIs404(t *testing.T) {
	h := New(map[string]Invoker{}, nil)

	req := httptest.NewRequest(http.MethodGet, "/not-a-real-path", nil)
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)

	if rec.Code != http.StatusNotFound {
		t.Fatalf("status = %d, want 404 for unmatched path", rec.Code)
	}
}
