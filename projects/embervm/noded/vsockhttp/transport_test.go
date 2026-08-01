package vsockhttp

import (
	"context"
	"io"
	"net"
	"net/http"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

// echoHandler echoes "ok:" prepended to the request body as a 200 response.
// It is the workload handler used across these tests.
func echoHandler(w http.ResponseWriter, r *http.Request) {
	body, _ := io.ReadAll(r.Body)
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write([]byte("ok:" + string(body)))
}

// newShimMux builds a stdlib http.Handler that reproduces the guest shim
// server's behaviour these tests rely on: POST /invoke echoes the body,
// GET /shim/healthz always answers 200 (Prime probes this), and GET
// /shim/ready answers 200 once ready reports true (nil ready means always
// ready).
func newShimMux(ready func() bool) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/invoke", echoHandler)
	mux.HandleFunc("/shim/healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	})
	mux.HandleFunc("/shim/ready", func(w http.ResponseWriter, _ *http.Request) {
		if ready == nil || ready() {
			w.WriteHeader(http.StatusOK)
			return
		}
		w.WriteHeader(http.StatusServiceUnavailable)
	})
	return mux
}

// startShimOnUDS starts a stdlib http.Server bound to a Unix-domain socket in
// t's temp directory, backed by newShimMux(ready). The socket is created with
// net.Listen so the server speaks plain HTTP with no Firecracker CONNECT
// layer, making it suitable for use with WithDirectDial. The server is
// stopped when t.Cleanup runs. It returns the UDS path. A nil ready means the
// shim always reports ready.
func startShimOnUDS(t *testing.T, ready func() bool) string {
	t.Helper()
	udsPath := t.TempDir() + "/shim.sock"
	ln, err := net.Listen("unix", udsPath)
	if err != nil {
		t.Fatalf("listen unix %s: %v", udsPath, err)
	}
	srv := &http.Server{Handler: newShimMux(ready)}
	go func() { _ = srv.Serve(ln) }()
	t.Cleanup(func() { _ = srv.Close() })
	return udsPath
}

// TestRoundTripOverUDS verifies that Transport.RoundTrip sends an HTTP POST to
// a shim server over a Unix-domain socket and receives the echoed response
// body.
func TestRoundTripOverUDS(t *testing.T) {
	udsPath := startShimOnUDS(t, nil)
	tr := NewTransport(WithDirectDial())

	req, err := http.NewRequestWithContext(
		context.Background(), http.MethodPost, "http://vsock/invoke", strings.NewReader("hi"),
	)
	if err != nil {
		t.Fatalf("build request: %v", err)
	}

	resp, err := tr.RoundTrip(context.Background(), udsPath, req)
	if err != nil {
		t.Fatalf("RoundTrip: %v", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		t.Errorf("status = %d, want 200", resp.StatusCode)
	}
	got, _ := io.ReadAll(resp.Body)
	if string(got) != "ok:hi" {
		t.Errorf("body = %q, want %q", string(got), "ok:hi")
	}
}

// TestWaitReadyBecomesReady starts a shim server whose readiness flips to true
// after 100 ms and asserts that WaitReady returns nil once the server is ready.
func TestWaitReadyBecomesReady(t *testing.T) {
	var ready atomic.Bool
	time.AfterFunc(100*time.Millisecond, func() { ready.Store(true) })

	udsPath := startShimOnUDS(t, func() bool { return ready.Load() })
	tr := NewTransport(WithDirectDial())

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()

	if err := tr.WaitReady(ctx, udsPath, "/shim/ready"); err != nil {
		t.Errorf("WaitReady: %v", err)
	}
}

// TestWaitReadyTimeout asserts that WaitReady returns a non-nil error when the
// context expires before the server ever reports ready.
func TestWaitReadyTimeout(t *testing.T) {
	// Server is permanently not ready.
	udsPath := startShimOnUDS(t, func() bool { return false })
	tr := NewTransport(WithDirectDial())

	ctx, cancel := context.WithTimeout(context.Background(), 150*time.Millisecond)
	defer cancel()

	if err := tr.WaitReady(ctx, udsPath, "/shim/ready"); err == nil {
		t.Error("WaitReady: want non-nil error on timeout, got nil")
	}
}

func TestSetClockSucceeds(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/shim/clock", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			w.WriteHeader(http.StatusMethodNotAllowed)
			return
		}
		w.WriteHeader(http.StatusOK)
	})
	udsPath := t.TempDir() + "/shim.sock"
	ln, err := net.Listen("unix", udsPath)
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	srv := &http.Server{Handler: mux}
	go func() { _ = srv.Serve(ln) }()
	t.Cleanup(func() { _ = srv.Close() })

	tr := NewTransport(WithDirectDial())
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	if err := tr.SetClock(ctx, udsPath, 1600000000000); err != nil {
		t.Errorf("SetClock: %v", err)
	}
}

func TestSetClockTreats404AsSuccess(t *testing.T) {
	udsPath := startShimOnUDS(t, nil)
	tr := NewTransport(WithDirectDial())
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	if err := tr.SetClock(ctx, udsPath, 1600000000000); err != nil {
		t.Errorf("SetClock on 404: want nil, got %v", err)
	}
}

func TestSetClockErrorOnConnectionFailure(t *testing.T) {
	tr := NewTransport(WithDirectDial())
	ctx, cancel := context.WithTimeout(context.Background(), 500*time.Millisecond)
	defer cancel()
	if err := tr.SetClock(ctx, "/nonexistent/shim.sock", 1600000000000); err == nil {
		t.Error("SetClock on bad path: want error, got nil")
	}
}

func TestSetClockReturnsErrorOn500(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/shim/clock", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			w.WriteHeader(http.StatusMethodNotAllowed)
			return
		}
		w.WriteHeader(http.StatusInternalServerError)
	})

	udsPath := t.TempDir() + "/shim.sock"
	ln, err := net.Listen("unix", udsPath)
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	srv := &http.Server{Handler: mux}
	go func() { _ = srv.Serve(ln) }()
	t.Cleanup(func() { _ = srv.Close() })

	tr := NewTransport(WithDirectDial())
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()

	if err := tr.SetClock(ctx, udsPath, 1600000000000); err == nil {
		t.Error("SetClock on 500: want error, got nil")
	}
}

// hangFirstListener makes the FIRST accepted connection wedge (never serviced),
// simulating the Firecracker post-restore vsock RX-queue race; every subsequent
// connection is passed through to the real server.
type hangFirstListener struct {
	net.Listener
	tripped atomic.Bool
	stop    chan struct{}
}

func (l *hangFirstListener) Accept() (net.Conn, error) {
	c, err := l.Listener.Accept()
	if err != nil {
		return nil, err // nosemgrep: no-bare-error-return
	}
	if l.tripped.CompareAndSwap(false, true) {
		return &hangConn{Conn: c, stop: l.stop}, nil
	}
	return c, nil
}

// hangConn blocks on Read until stop is closed, so the server never reads the
// request on the wedged first connection and the client's per-attempt deadline
// must fire for it to make progress.
type hangConn struct {
	net.Conn
	stop chan struct{}
}

func (c *hangConn) Read(_ []byte) (int, error) {
	<-c.stop
	return 0, io.EOF
}

// TestWaitReadyRetriesPastWedgedFirstConnection is the regression test for the
// warm-restore latency bug: the first post-restore connection wedges, and
// WaitReady must abandon it at the per-attempt deadline and reconnect, returning
// well under the (generous) outer context rather than blocking on it.
func TestWaitReadyRetriesPastWedgedFirstConnection(t *testing.T) {
	udsPath := t.TempDir() + "/shim.sock"
	base, err := net.Listen("unix", udsPath)
	if err != nil {
		t.Fatalf("listen unix: %v", err)
	}
	stop := make(chan struct{})
	defer close(stop)
	ln := &hangFirstListener{Listener: base, stop: stop}
	srv := &http.Server{Handler: newShimMux(func() bool { return true })}
	go func() { _ = srv.Serve(ln) }()
	t.Cleanup(func() { _ = srv.Close() })

	tr := NewTransport(WithDirectDial())
	// Outer budget is deliberately generous: without the per-attempt cap the
	// wedged first connection would block WaitReady for the full 5s.
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	start := time.Now()
	if err := tr.WaitReady(ctx, udsPath, "/shim/ready"); err != nil {
		t.Fatalf("WaitReady: %v", err)
	}
	if elapsed := time.Since(start); elapsed > 2*time.Second {
		t.Errorf("WaitReady took %v; the per-attempt cap should abandon the wedged connection and retry well under the 5s outer ctx", elapsed)
	}
}

// TestPrimeShedsWedgedFirstConnection verifies that Prime absorbs the
// post-restore RX-queue race off the readiness path: the first vsock connection
// wedges, and Prime's tight per-attempt deadline abandons it and re-rolls,
// completing well under the generous outer budget so the subsequent WaitReady
// lands clean.
func TestPrimeShedsWedgedFirstConnection(t *testing.T) {
	udsPath := t.TempDir() + "/shim.sock"
	base, err := net.Listen("unix", udsPath)
	if err != nil {
		t.Fatalf("listen unix: %v", err)
	}
	stop := make(chan struct{})
	defer close(stop)
	ln := &hangFirstListener{Listener: base, stop: stop}
	srv := &http.Server{Handler: newShimMux(func() bool { return true })}
	go func() { _ = srv.Serve(ln) }()
	t.Cleanup(func() { _ = srv.Close() })

	tr := NewTransport(WithDirectDial())
	// Outer budget is deliberately generous: the value comes from Prime shedding
	// the wedged first connection at its tight per-attempt cap, not from the ctx.
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	start := time.Now()
	if err := tr.Prime(ctx, udsPath); err != nil {
		t.Fatalf("Prime: %v", err)
	}
	if elapsed := time.Since(start); elapsed > time.Second {
		t.Errorf("Prime took %v; the tight per-attempt cap should shed the wedged connection and re-roll fast", elapsed)
	}
}

// TestPrimeTimesOutWhenNeverReachable asserts Prime returns a non-nil error when
// the vsock path never opens before ctx expires, so the caller can log it and
// fall through to WaitReady.
func TestPrimeTimesOutWhenNeverReachable(t *testing.T) {
	// A path with no listener: every dial fails, so no probe ever completes.
	udsPath := t.TempDir() + "/absent.sock"
	tr := NewTransport(WithDirectDial())

	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()

	if err := tr.Prime(ctx, udsPath); err == nil {
		t.Error("Prime: want non-nil error when the vsock path never opens, got nil")
	}
}

// TestRoundTripContextCancel verifies that a context cancelled before the call
// causes RoundTrip to return promptly with an error.
func TestRoundTripContextCancel(t *testing.T) {
	udsPath := startShimOnUDS(t, nil)
	tr := NewTransport(WithDirectDial())

	ctx, cancel := context.WithCancel(context.Background())
	cancel() // cancel before the request is made

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, "http://vsock/invoke", nil)
	if err != nil {
		t.Fatalf("build request: %v", err)
	}

	_, err = tr.RoundTrip(ctx, udsPath, req)
	if err == nil {
		t.Error("RoundTrip: want non-nil error on cancelled context, got nil")
	}
}
