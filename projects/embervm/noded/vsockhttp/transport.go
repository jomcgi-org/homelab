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
	"bytes"
	"context"
	"fmt"
	"io"
	"net"
	"net/http"
	"strings"
	"time"

	"github.com/jomcgi/homelab/projects/embervm/noded/vsockproto"
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

// primeAttempt bounds a single prime probe. It is deliberately far tighter than
// waitReadyAttempt: the whole point of priming is to abandon a wedged
// post-restore connection FAST and re-roll, so the RX-queue race is shaken out
// in tens of milliseconds rather than the ~150ms a readiness attempt would burn
// on the same wedge. primeBackoff is the pause between probes.
const (
	primeAttempt = 30 * time.Millisecond
	primeBackoff = 10 * time.Millisecond
)

// Prime forces the post-restore vsock path open BEFORE the readiness poll runs,
// absorbing Firecracker's post-restore RX-queue race off the readiness path.
//
// A freshly restored guest can wedge its first host-initiated vsock connection
// (see WaitReady for the mechanism). Prime repeatedly issues a cheap liveness
// probe against the shim's /shim/healthz, each bounded by the tight primeAttempt
// deadline, until one exchange completes or ctx fires. /shim/healthz answers 200
// the moment the shim serves, so ANY completed HTTP exchange -- whatever its
// status -- proves the vsock path drained. The connection is thrown away; Prime
// exists only so the subsequent WaitReady and exec dials land on an already
// drained queue and answer on their first attempt.
//
// Prime is best-effort: on ctx expiry it returns an error the caller may log and
// ignore, since WaitReady still retries past the race as before. Callers should
// bound ctx by the same short restore budget used for the readiness wait.
func (t *Transport) Prime(ctx context.Context, udsPath string) error {
	for {
		if err := t.primeProbe(ctx, udsPath); err == nil {
			return nil
		}
		// Retriable: back off briefly, stop only when the outer ctx fires.
		select {
		case <-ctx.Done():
			return fmt.Errorf("vsockhttp: timed out priming vsock at %s: %w", udsPath, ctx.Err())
		case <-time.After(primeBackoff):
		}
	}
}

// primeProbe performs one liveness probe bounded by primeAttempt (capped by the
// outer ctx). It returns nil once an HTTP exchange completes, regardless of the
// response status: a completed exchange means the vsock path is open.
func (t *Transport) primeProbe(ctx context.Context, udsPath string) error {
	attemptCtx, cancel := context.WithTimeout(ctx, primeAttempt)
	defer cancel()
	req, err := http.NewRequestWithContext(attemptCtx, http.MethodGet, "http://vsock/shim/healthz", nil)
	if err != nil {
		return fmt.Errorf("vsockhttp: build prime request: %w", err)
	}
	resp, err := t.RoundTrip(attemptCtx, udsPath, req)
	if err != nil {
		return err // nosemgrep: no-bare-error-return
	}
	_ = resp.Body.Close()
	return nil
}

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

// hydratePath is the build-only guest endpoint that receives the zip archive
// bytes. The shim unpacks them to tmpfs, imports the handler, and only then
// flips /shim/ready to 200, so a Hydrate MUST land between Prime and WaitReady.
const hydratePath = "/shim/hydrate"

// hydrateAttempt bounds the single hydrate POST. It is far more generous than the
// readiness/prime probes: the body is the whole archive and the shim unpacks and
// imports the handler synchronously before replying, which is real work, not a
// liveness ping. The caller passes a ctx bound by the build's ready budget.
const hydrateAttempt = 60 * time.Second

// Hydrate POSTs the archive bytes to the guest shim's build-only /shim/hydrate
// endpoint over the vsock transport, so the shim can unpack + import the handler
// before it goes ready. It is the zip lane's replacement for attaching the
// archive as a block device: the bytes cross as a clean HTTP body, never a padded
// block file, so the resulting snapshot has no archive backing dependency.
//
// The request carries an explicit Content-Length (bytes.Reader gives net/http a
// known length, so the body is fixed-length framed, never chunked; the transport
// on the guest side rejects chunked framing). A non-2xx response is an error
// whose message carries the guest's response body (the shim writes the unpack /
// import traceback there), so a bad archive fails the build legibly.
//
// Hydrate is bounded by hydrateAttempt (capped by the caller's ctx): a single
// attempt, not a retry loop, because Prime has already drained the vsock path and
// a hydrate is not idempotent to re-drive (a re-hydrate after ready is a 409).
func (t *Transport) Hydrate(ctx context.Context, udsPath string, archive []byte) error {
	attemptCtx, cancel := context.WithTimeout(ctx, hydrateAttempt)
	defer cancel()

	req, err := http.NewRequestWithContext(attemptCtx, http.MethodPost, "http://vsock"+hydratePath, bytes.NewReader(archive))
	if err != nil {
		return fmt.Errorf("vsockhttp: build hydrate request: %w", err)
	}
	// Explicit length so net/http fixed-length frames the body (the guest rejects
	// chunked framing). bytes.Reader already sets ContentLength, but set the header
	// too so it is unambiguous on the wire.
	req.ContentLength = int64(len(archive))
	req.Header.Set("Content-Type", "application/zip")

	resp, err := t.RoundTrip(attemptCtx, udsPath, req)
	if err != nil {
		return fmt.Errorf("vsockhttp: hydrate round-trip: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		// Bound the error body read so a runaway guest cannot pin memory on the
		// failure path; the shim writes a compact traceback here.
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 64<<10))
		return fmt.Errorf("vsockhttp: hydrate rejected, status %d: %s", resp.StatusCode, strings.TrimSpace(string(body)))
	}
	return nil
}

// SetClock POSTs the current epoch-ms to the guest shim's /shim/clock endpoint
// over the vsock transport, so a restored guest runs with the correct wall
// clock instead of stale time from before the restore. A 404 is treated as a
// guest without the endpoint and is not an error. SetClock retries on transient
// failures until ctx expires, similar to WaitReady.
func (t *Transport) SetClock(ctx context.Context, udsPath string, epochMs int64) error {
	for {
		if err := t.setClockAttempt(ctx, udsPath, epochMs); err == nil {
			return nil
		}
		// Retriable: back off briefly, stop only when the outer ctx fires.
		select {
		case <-ctx.Done():
			return fmt.Errorf("vsockhttp: timed out setting guest clock: %w", ctx.Err())
		case <-time.After(waitReadyBackoff):
		}
	}
}

// setClockAttempt performs one clock-sync POST bounded by waitReadyAttempt (capped
// by the outer ctx). It returns nil only on a 2xx response or 404 (guest without
// the endpoint).
func (t *Transport) setClockAttempt(ctx context.Context, udsPath string, epochMs int64) error {
	attemptCtx, cancel := context.WithTimeout(ctx, waitReadyAttempt)
	defer cancel()

	body := []byte(fmt.Sprintf(`{"epoch_ms":%d}`, epochMs))
	req, err := http.NewRequestWithContext(attemptCtx, http.MethodPost, "http://vsock/shim/clock", bytes.NewReader(body))
	if err != nil {
		return fmt.Errorf("vsockhttp: build clock request: %w", err)
	}
	req.ContentLength = int64(len(body))
	req.Header.Set("Content-Type", "application/json")

	resp, err := t.RoundTrip(attemptCtx, udsPath, req)
	if err != nil {
		return err // nosemgrep: no-bare-error-return
	}
	defer resp.Body.Close()
	_, _ = io.ReadAll(io.LimitReader(resp.Body, 4<<10))

	if resp.StatusCode == http.StatusNotFound {
		return nil
	}
	if resp.StatusCode >= 200 && resp.StatusCode < 300 {
		return nil
	}
	return fmt.Errorf("vsockhttp: clock rejected, status %d", resp.StatusCode)
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
