package shim

import (
	"context"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// TestServerRoutesInvokeToHandler starts a real TCP server, POSTs to /invoke,
// and asserts the response body is what the handler returned.
func TestServerRoutesInvokeToHandler(t *testing.T) {
	h := func(_ context.Context, r *Request) (*Response, error) {
		body, _ := io.ReadAll(r.Body)
		return &Response{Status: http.StatusOK, Body: []byte("handled:" + string(body))}, nil
	}

	srv := NewServer(h)

	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	go srv.Serve(ln) //nolint:errcheck
	defer srv.Close()

	resp, err := http.Post("http://"+ln.Addr().String()+"/invoke",
		"application/octet-stream", strings.NewReader("hello"))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		t.Errorf("status %d, want 200", resp.StatusCode)
	}
	got, _ := io.ReadAll(resp.Body)
	if string(got) != "handled:hello" {
		t.Errorf("body %q, want %q", got, "handled:hello")
	}
}

// TestServerHealthzAndReady drives /shim/healthz and /shim/ready directly
// against srv.mux() using httptest, verifying 200 for both, and 503 for
// /shim/ready when the WithReady function returns false.
func TestServerHealthzAndReady(t *testing.T) {
	noop := func(_ context.Context, _ *Request) (*Response, error) {
		return &Response{Status: http.StatusOK}, nil
	}

	t.Run("healthz 200", func(t *testing.T) {
		srv := NewServer(noop)
		req := httptest.NewRequest(http.MethodGet, "/shim/healthz", nil)
		w := httptest.NewRecorder()
		srv.mux().ServeHTTP(w, req)
		if w.Code != http.StatusOK {
			t.Errorf("healthz: got %d, want 200", w.Code)
		}
	})

	t.Run("ready 200 default", func(t *testing.T) {
		srv := NewServer(noop)
		req := httptest.NewRequest(http.MethodGet, "/shim/ready", nil)
		w := httptest.NewRecorder()
		srv.mux().ServeHTTP(w, req)
		if w.Code != http.StatusOK {
			t.Errorf("ready: got %d, want 200", w.Code)
		}
	})

	t.Run("ready 503 when fn returns false", func(t *testing.T) {
		srv := NewServer(noop, WithReady(func() bool { return false }))
		req := httptest.NewRequest(http.MethodGet, "/shim/ready", nil)
		w := httptest.NewRecorder()
		srv.mux().ServeHTTP(w, req)
		if w.Code != http.StatusServiceUnavailable {
			t.Errorf("ready: got %d, want 503", w.Code)
		}
	})
}

// TestClockEndpoint verifies POST /shim/clock: it is absent (404) without a
// clock handler, calls the handler with the posted epoch on a valid body,
// rejects a non-positive epoch (400) and a malformed body (400), and surfaces a
// handler failure as 500.
func TestClockEndpoint(t *testing.T) {
	noop := func(_ context.Context, _ *Request) (*Response, error) {
		return &Response{Status: http.StatusOK}, nil
	}

	t.Run("absent without a clock handler", func(t *testing.T) {
		srv := NewServer(noop)
		req := httptest.NewRequest(http.MethodPost, "/shim/clock", strings.NewReader(`{"epoch_ms":1}`))
		w := httptest.NewRecorder()
		srv.mux().ServeHTTP(w, req)
		if w.Code != http.StatusNotFound {
			t.Errorf("got %d, want 404 (no clock handler installed)", w.Code)
		}
	})

	t.Run("calls the handler with the posted epoch", func(t *testing.T) {
		var got int64
		srv := NewServer(noop, WithClock(func(epochMs int64) error {
			got = epochMs
			return nil
		}))
		req := httptest.NewRequest(http.MethodPost, "/shim/clock", strings.NewReader(`{"epoch_ms":1700000000000}`))
		w := httptest.NewRecorder()
		srv.mux().ServeHTTP(w, req)
		if w.Code != http.StatusNoContent {
			t.Errorf("got %d, want 204", w.Code)
		}
		if got != 1700000000000 {
			t.Errorf("clock fn got %d, want 1700000000000", got)
		}
	})

	t.Run("rejects a non-positive epoch", func(t *testing.T) {
		srv := NewServer(noop, WithClock(func(int64) error { return nil }))
		req := httptest.NewRequest(http.MethodPost, "/shim/clock", strings.NewReader(`{"epoch_ms":0}`))
		w := httptest.NewRecorder()
		srv.mux().ServeHTTP(w, req)
		if w.Code != http.StatusBadRequest {
			t.Errorf("got %d, want 400", w.Code)
		}
	})

	t.Run("rejects a malformed body", func(t *testing.T) {
		srv := NewServer(noop, WithClock(func(int64) error { return nil }))
		req := httptest.NewRequest(http.MethodPost, "/shim/clock", strings.NewReader("not json"))
		w := httptest.NewRecorder()
		srv.mux().ServeHTTP(w, req)
		if w.Code != http.StatusBadRequest {
			t.Errorf("got %d, want 400", w.Code)
		}
	})

	t.Run("surfaces a set failure as 500", func(t *testing.T) {
		srv := NewServer(noop, WithClock(func(int64) error { return fmt.Errorf("nope") }))
		req := httptest.NewRequest(http.MethodPost, "/shim/clock", strings.NewReader(`{"epoch_ms":1}`))
		w := httptest.NewRecorder()
		srv.mux().ServeHTTP(w, req)
		if w.Code != http.StatusInternalServerError {
			t.Errorf("got %d, want 500", w.Code)
		}
	})
}

// TestServerUnknownPath404 verifies that requests to unregistered paths
// receive a 404 response.
func TestServerUnknownPath404(t *testing.T) {
	srv := NewServer(func(_ context.Context, _ *Request) (*Response, error) {
		return &Response{Status: http.StatusOK}, nil
	})
	req := httptest.NewRequest(http.MethodGet, "/nope", nil)
	w := httptest.NewRecorder()
	srv.mux().ServeHTTP(w, req)
	if w.Code != http.StatusNotFound {
		t.Errorf("got %d, want 404", w.Code)
	}
}

// TestServerHandlerErrorIs502 verifies that when the Handler returns an error
// the server responds with 502 Bad Gateway.
func TestServerHandlerErrorIs502(t *testing.T) {
	srv := NewServer(func(_ context.Context, _ *Request) (*Response, error) {
		return nil, fmt.Errorf("something went wrong")
	})
	req := httptest.NewRequest(http.MethodPost, "/invoke", strings.NewReader(""))
	w := httptest.NewRecorder()
	srv.mux().ServeHTTP(w, req)
	if w.Code != http.StatusBadGateway {
		t.Errorf("got %d, want 502", w.Code)
	}
}
