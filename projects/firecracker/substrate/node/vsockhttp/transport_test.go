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

	"github.com/jomcgi/homelab/projects/firecracker/substrate/shim"
)

// echoHandler echoes "ok:" prepended to the request body as a 200 response.
// It is the workload handler used across these tests.
func echoHandler(_ context.Context, r *shim.Request) (*shim.Response, error) {
	body, _ := io.ReadAll(r.Body)
	return &shim.Response{
		Status: http.StatusOK,
		Body:   []byte("ok:" + string(body)),
	}, nil
}

// startShimOnUDS starts a shim.Server bound to a Unix-domain socket in
// t's temp directory. The socket is created with net.Listen so the server
// speaks plain HTTP with no Firecracker CONNECT layer, making it suitable for
// use with WithDirectDial. The server is stopped when t.Cleanup runs.
// It returns the UDS path.
func startShimOnUDS(t *testing.T, opts ...shim.Option) string {
	t.Helper()
	udsPath := t.TempDir() + "/shim.sock"
	ln, err := net.Listen("unix", udsPath)
	if err != nil {
		t.Fatalf("listen unix %s: %v", udsPath, err)
	}
	srv := shim.NewServer(echoHandler, opts...)
	go srv.Serve(ln) //nolint:errcheck
	t.Cleanup(func() { _ = srv.Close() })
	return udsPath
}

// TestRoundTripOverUDS verifies that Transport.RoundTrip sends an HTTP POST to
// a shim.Server over a Unix-domain socket and receives the echoed response
// body.
func TestRoundTripOverUDS(t *testing.T) {
	udsPath := startShimOnUDS(t)
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

	udsPath := startShimOnUDS(t, shim.WithReady(func() bool { return ready.Load() }))
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
	udsPath := startShimOnUDS(t, shim.WithReady(func() bool { return false }))
	tr := NewTransport(WithDirectDial())

	ctx, cancel := context.WithTimeout(context.Background(), 150*time.Millisecond)
	defer cancel()

	if err := tr.WaitReady(ctx, udsPath, "/shim/ready"); err == nil {
		t.Error("WaitReady: want non-nil error on timeout, got nil")
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
	srv := shim.NewServer(echoHandler, shim.WithReady(func() bool { return true }))
	go srv.Serve(ln) //nolint:errcheck
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

// TestRoundTripContextCancel verifies that a context cancelled before the call
// causes RoundTrip to return promptly with an error.
func TestRoundTripContextCancel(t *testing.T) {
	udsPath := startShimOnUDS(t)
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
