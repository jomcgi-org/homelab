// Package vsockhttp provides a host-side HTTP transport for communicating with
// a guest shim server over Firecracker's vsock unix-domain socket bridge.
//
// Firecracker maps vsock ports to per-VM UDS paths on the host. To open a
// host-initiated connection to a guest port the host dials the base UDS and
// writes "CONNECT <port>\n"; the device layer forwards the connection to the
// guest listener and replies "OK <port>\n". After that line the connection is
// a plain bidirectional byte stream.
//
// Transport wraps that handshake inside a custom net/http DialContext so that
// an ordinary *http.Client can speak HTTP/1.1 to the guest shim. Each
// RoundTrip opens a fresh connection (keep-alives disabled) because each
// microVM is single-use and should never share a connection with another.
package vsockhttp

import (
	"bufio"
	"context"
	"fmt"
	"net"
	"net/http"
	"strings"
	"time"

	"github.com/jomcgi/homelab/projects/firecracker/substrate/vsockproto"
)

// Transport performs HTTP request/response exchanges to a guest shim server
// over a Firecracker vsock UDS. Each RoundTrip call opens a fresh connection;
// connection reuse is disabled so separate microVMs never share state.
type Transport struct {
	directDial bool
	dialer     *net.Dialer
}

// Option configures a Transport.
type Option func(*Transport)

// WithDirectDial returns an Option that skips the Firecracker CONNECT
// handshake and dials the UDS directly as a plain HTTP server. This exists
// solely for testing: the test backs the Transport with a shim.Server bound
// to a plain unix listener that speaks HTTP immediately, without the
// Firecracker device layer in between.
func WithDirectDial() Option {
	return func(t *Transport) {
		t.directDial = true
	}
}

// WithDialer returns an Option that substitutes d for the default net.Dialer.
// Useful in tests that need to control dial timing or inject failures.
func WithDialer(d *net.Dialer) Option {
	return func(t *Transport) {
		t.dialer = d
	}
}

// NewTransport builds a Transport, applying any supplied options.
func NewTransport(opts ...Option) *Transport {
	t := &Transport{}
	for _, o := range opts {
		o(t)
	}
	return t
}

// RoundTrip sends req to the guest shim server reachable via the vsock UDS at
// udsPath and returns its HTTP response. The caller is responsible for closing
// the response body.
//
// In production mode (the default) RoundTrip performs the Firecracker
// host-initiated vsock handshake before speaking HTTP: it dials udsPath,
// writes "CONNECT <GuestHTTPPort>\n", and validates the "OK" reply. With
// WithDirectDial the handshake is skipped, which allows test code to back the
// Transport with a plain net.Listener.
//
// req.URL.Host is ignored; the dial always targets udsPath. Set the URL to
// "http://vsock/..." as a clear placeholder.
//
// If ctx carries a deadline it is applied to the underlying connection so all
// I/O is bounded.
func (t *Transport) RoundTrip(ctx context.Context, udsPath string, req *http.Request) (*http.Response, error) {
	d := t.dialer
	if d == nil {
		d = &net.Dialer{}
	}

	tr := &http.Transport{
		DialContext: func(dialCtx context.Context, _, _ string) (net.Conn, error) {
			return t.dialConn(dialCtx, d, udsPath)
		},
		DisableKeepAlives: true,
	}
	client := &http.Client{Transport: tr}

	return client.Do(req.WithContext(ctx))
}

// WaitReady polls GET readyPath over the vsock transport until the guest shim
// responds 200, or until ctx expires. readyPath defaults to "/shim/ready" when
// empty. On timeout it returns a descriptive error wrapping ctx.Err().
//
// Any error from RoundTrip (such as a connection-refused while the guest is
// still starting) is treated as "not ready yet" and causes a retry.
//
// Each attempt is bounded by its own short deadline (waitReadyAttempt), NOT the
// outer ctx. This is load-bearing for warm restores: the first post-restore
// vsock connection can wedge on Firecracker's RX-queue race, and its
// CONNECT-reply read would otherwise block for the full outer deadline before
// the retry loop ever runs. Capping per attempt abandons a wedged connection
// quickly and reconnects, so WaitReady returns the instant the guest answers
// (usually the first or second attempt) instead of hanging.
func (t *Transport) WaitReady(ctx context.Context, udsPath, readyPath string) error {
	if readyPath == "" {
		readyPath = "/shim/ready"
	}
	for {
		if err := t.readyAttempt(ctx, udsPath, readyPath); err == nil {
			return nil
		}
		// Retriable: back off briefly, stop only when the outer ctx fires.
		select {
		case <-ctx.Done():
			return fmt.Errorf("vsockhttp: timed out waiting for guest ready at %s: %w", readyPath, ctx.Err())
		case <-time.After(waitReadyBackoff):
		}
	}
}

// waitReadyAttempt bounds a single readiness probe; a wedged post-restore
// connection is abandoned after this and retried. waitReadyBackoff is the pause
// between attempts once one has returned (fast failures should not spin).
const (
	waitReadyAttempt = 150 * time.Millisecond
	waitReadyBackoff = 20 * time.Millisecond
)

// readyAttempt performs one readiness probe bounded by waitReadyAttempt (capped
// by the outer ctx). It returns nil only on a 200 response.
func (t *Transport) readyAttempt(ctx context.Context, udsPath, readyPath string) error {
	attemptCtx, cancel := context.WithTimeout(ctx, waitReadyAttempt)
	defer cancel()
	req, err := http.NewRequestWithContext(attemptCtx, http.MethodGet, "http://vsock"+readyPath, nil)
	if err != nil {
		return fmt.Errorf("vsockhttp: build ready request: %w", err)
	}
	resp, err := t.RoundTrip(attemptCtx, udsPath, req)
	if err != nil {
		return err // nosemgrep: no-bare-error-return
	}
	_ = resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("vsockhttp: guest not ready, status %d", resp.StatusCode)
	}
	return nil
}

// dialConn opens a raw connection to the guest shim at udsPath. In production
// mode it performs the Firecracker host-initiated vsock CONNECT handshake. With
// directDial (test mode) it returns the raw UDS connection directly.
func (t *Transport) dialConn(ctx context.Context, d *net.Dialer, udsPath string) (net.Conn, error) {
	conn, err := d.DialContext(ctx, "unix", udsPath)
	if err != nil {
		return nil, fmt.Errorf("vsockhttp: dial uds %s: %w", udsPath, err)
	}

	// Bound all subsequent I/O on this connection by the context deadline.
	if dl, ok := ctx.Deadline(); ok {
		_ = conn.SetDeadline(dl)
	}

	if t.directDial {
		return conn, nil
	}

	// Firecracker host-initiated vsock handshake: write "CONNECT <port>\n",
	// then read and validate the "OK <port>\n" reply. Only after the OK line
	// does the device layer route the connection to the guest listener.
	if _, err := fmt.Fprintf(conn, "CONNECT %d\n", vsockproto.GuestHTTPPort); err != nil {
		_ = conn.Close()
		return nil, fmt.Errorf("vsockhttp: write CONNECT: %w", err)
	}
	br := bufio.NewReader(conn)
	status, err := br.ReadString('\n')
	if err != nil {
		_ = conn.Close()
		return nil, fmt.Errorf("vsockhttp: read CONNECT reply: %w", err)
	}
	if !strings.HasPrefix(status, "OK") {
		_ = conn.Close()
		return nil, fmt.Errorf("vsockhttp: guest HTTP port not reachable: %q", strings.TrimSpace(status))
	}

	// Wrap conn so any bytes the bufio.Reader pre-buffered past the OK line
	// are served to net/http rather than being silently discarded.
	return &bufConnWrapper{Conn: conn, r: br}, nil
}

// bufConnWrapper combines a net.Conn with a bufio.Reader so that bytes already
// consumed into the reader's internal buffer are served first, before the
// underlying conn is read directly. This preserves any bytes the reader may
// have buffered past the CONNECT handshake "OK" line.
type bufConnWrapper struct {
	net.Conn
	r *bufio.Reader
}

// Read satisfies net.Conn by draining the bufio.Reader (which in turn reads
// from the underlying conn when its buffer is empty).
func (b *bufConnWrapper) Read(p []byte) (int, error) {
	return b.r.Read(p)
}
