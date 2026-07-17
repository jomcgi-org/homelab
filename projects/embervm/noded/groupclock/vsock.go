package groupclock

import (
	"bufio"
	"context"
	"fmt"
	"net"
	"strings"

	"github.com/jomcgi/homelab/projects/embervm/noded/vsockproto"
)

// VsockDialer performs the Firecracker host-initiated vsock CONNECT handshake to a
// member guest's clock-agent port (vsockproto.GroupClockAgentPort) and returns the
// raw byte stream, exactly the handshake vsockhttp.Transport uses for the guest HTTP
// port but targeting port 1024 and yielding a plain stream (not an HTTP transport,
// because the clock agent speaks length-prefixed JSON frames, not HTTP). Firecracker
// maps vsock ports to per-VM UDS paths on the host: the host dials the base UDS,
// writes "CONNECT <port>\n", and the device layer replies "OK <port>\n" before it
// routes the connection to the guest listener.
type VsockDialer struct{}

var _ Dialer = VsockDialer{}

// DialClockAgent dials the per-VM vsock UDS at udsPath and performs the CONNECT
// handshake for GroupClockAgentPort, returning the connection ready to carry the
// length-prefixed clock frames. Any bytes the handshake reader pre-buffered past the
// OK line are preserved by wrapping the conn (a clock response can, in principle,
// arrive coalesced with the OK line on the same read).
func (VsockDialer) DialClockAgent(ctx context.Context, udsPath string) (net.Conn, error) {
	d := &net.Dialer{}
	conn, err := d.DialContext(ctx, "unix", udsPath)
	if err != nil {
		return nil, fmt.Errorf("groupclock: dial uds %s: %w", udsPath, err)
	}
	if dl, ok := ctx.Deadline(); ok {
		_ = conn.SetDeadline(dl)
	}
	if _, err := fmt.Fprintf(conn, "CONNECT %d\n", vsockproto.GroupClockAgentPort); err != nil {
		_ = conn.Close()
		return nil, fmt.Errorf("groupclock: write CONNECT: %w", err)
	}
	br := bufio.NewReader(conn)
	status, err := br.ReadString('\n')
	if err != nil {
		_ = conn.Close()
		return nil, fmt.Errorf("groupclock: read CONNECT reply: %w", err)
	}
	if !strings.HasPrefix(status, "OK") {
		_ = conn.Close()
		return nil, fmt.Errorf("groupclock: clock agent port not reachable: %q", strings.TrimSpace(status))
	}
	return &bufConn{Conn: conn, r: br}, nil
}

// bufConn serves bytes the CONNECT-handshake reader buffered past the OK line before
// falling through to the underlying conn, so a coalesced response frame is not lost.
type bufConn struct {
	net.Conn
	r *bufio.Reader
}

func (b *bufConn) Read(p []byte) (int, error) { return b.r.Read(p) }
