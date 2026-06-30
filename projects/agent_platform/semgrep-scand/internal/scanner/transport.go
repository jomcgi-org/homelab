package scanner

import (
	"bufio"
	"context"
	"fmt"
	"net"
	"os"
	"strings"

	"github.com/jomcgi/homelab/projects/firecracker/substrate/vsockproto"
)

// vsockTransport is the production guestTransport. It speaks to a guest over the
// per-VM vsock unix-domain socket that Firecracker bridges: the guest dials OUT
// to the host control port (so the host *listens* on "<uds>_<ControlPort>" for
// the readiness Hello), and the host dials IN to the guest's scan port (so the
// host *connects* to the base "<uds>" and issues Firecracker's host-initiated
// "CONNECT <port>" handshake). Both legs are plain unix-domain sockets, never
// AF_VSOCK, so this builds and runs unchanged on the darwin CI path, exactly like
// fc-agentd's control/egress host-side code.
type vsockTransport struct{}

// NewVsockTransport returns the production transport.
func NewVsockTransport() guestTransport { return vsockTransport{} }

// controlPath is the host unix socket Firecracker bridges the guest's
// control-port connection (its readiness Hello) to.
func controlPath(udsPath string) string {
	return fmt.Sprintf("%s_%d", udsPath, vsockproto.ControlPort)
}

// WaitReady listens on the guest's control socket and reads the single KindHello
// the guest sends once its semgrep lsp is warm. It returns when the Hello
// arrives, or an error if ctx (BootReadyTimeout) fires first.
func (vsockTransport) WaitReady(ctx context.Context, udsPath string) error {
	path := controlPath(udsPath)
	_ = os.Remove(path)
	ln, err := net.Listen("unix", path)
	if err != nil {
		return fmt.Errorf("transport: listen control %s: %w", path, err)
	}
	defer func() {
		_ = ln.Close()
		_ = os.Remove(path)
	}()

	// Unblock Accept when ctx fires (timeout or cancellation) by closing the
	// listener; the Accept below then returns a net.ErrClosed we translate.
	go func() {
		<-ctx.Done()
		_ = ln.Close()
	}()

	raw, err := ln.Accept()
	if err != nil {
		if ctx.Err() != nil {
			return fmt.Errorf("transport: timed out waiting for guest hello: %w", ctx.Err())
		}
		return fmt.Errorf("transport: accept control: %w", err)
	}
	defer raw.Close()

	conn := vsockproto.NewConn(raw)
	hello, err := conn.Recv()
	if err != nil {
		return fmt.Errorf("transport: read hello: %w", err)
	}
	if hello.Kind != vsockproto.KindHello {
		return fmt.Errorf("transport: expected hello, got %q", hello.Kind)
	}
	return nil
}

// Scan dials the guest's scan port (host-initiated, via Firecracker's CONNECT
// handshake on the base UDS), writes one ScanRequest, and reads one ScanResult.
func (vsockTransport) Scan(ctx context.Context, udsPath string, req vsockproto.ScanRequest) (vsockproto.ScanResult, error) {
	var d net.Dialer
	raw, err := d.DialContext(ctx, "unix", udsPath)
	if err != nil {
		return vsockproto.ScanResult{}, fmt.Errorf("transport: dial vsock uds %s: %w", udsPath, err)
	}
	defer raw.Close()

	// Bound every read/write on the connection by the scan deadline.
	if dl, ok := ctx.Deadline(); ok {
		_ = raw.SetDeadline(dl)
	}

	// Firecracker host-initiated vsock: write "CONNECT <port>\n", expect "OK ...".
	br := bufio.NewReader(raw)
	if _, err := fmt.Fprintf(raw, "CONNECT %d\n", vsockproto.ScanPort); err != nil {
		return vsockproto.ScanResult{}, fmt.Errorf("transport: write CONNECT: %w", err)
	}
	status, err := br.ReadString('\n')
	if err != nil {
		return vsockproto.ScanResult{}, fmt.Errorf("transport: read CONNECT reply: %w", err)
	}
	if !strings.HasPrefix(status, "OK") {
		return vsockproto.ScanResult{}, fmt.Errorf("transport: guest scan port not reachable: %q", strings.TrimSpace(status))
	}

	if err := vsockproto.WriteScanRequest(raw, req); err != nil {
		return vsockproto.ScanResult{}, fmt.Errorf("transport: write scan request: %w", err)
	}
	// Decode the reply from the buffered reader so any bytes read past the CONNECT
	// status line are not lost.
	res, err := vsockproto.ReadScanResult(br)
	if err != nil {
		return vsockproto.ScanResult{}, fmt.Errorf("transport: read scan result: %w", err)
	}
	return res, nil
}
