package egress

import (
	"context"
	"io"
	"log/slog"
	"net"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/embervm/noded/vsockproto"
)

// startEchoSidecar starts a fake egress-proxy sidecar: a TCP listener that
// echoes every byte back on each accepted connection. It returns the listen
// address and a cleanup func. This stands in for the real sidecar so the tunnel
// can be exercised over plain in-process sockets (no Firecracker, no vsock).
func startEchoSidecar(t *testing.T) (addr string, stop func()) {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("echo sidecar listen: %v", err)
	}
	go func() {
		for {
			conn, err := ln.Accept()
			if err != nil {
				return
			}
			go func(c net.Conn) {
				defer c.Close()
				_, _ = io.Copy(c, c)
			}(conn)
		}
	}()
	return ln.Addr().String(), func() { _ = ln.Close() }
}

// TestServeEgressTunnelsBidirectionally asserts that a guest connection to
// <uds>_<EgressPort> is tunnelled bidirectionally to the sidecar: bytes written
// by the guest reach the (echo) sidecar and its reply is copied back to the
// guest. The udsPath is a plain unix socket path standing in for the FC vsock
// UDS; ServeEgress listens on the derived egress path exactly as it would in
// production.
func TestServeEgressTunnelsBidirectionally(t *testing.T) {
	sidecarAddr, stopSidecar := startEchoSidecar(t)
	defer stopSidecar()

	// The base UDS path the driver would return for a thread; ServeEgress
	// listens on "<uds>_<EgressPort>".
	udsPath := filepath.Join(t.TempDir(), "vsock.sock")

	ctx, cancel := context.WithCancel(context.Background())
	serveErr := make(chan error, 1)
	go func() {
		serveErr <- ServeEgress(ctx, slog.Default(), udsPath, sidecarAddr)
	}()

	listenPath := egressListenPath(udsPath)
	guest := dialWithRetry(t, listenPath)
	defer guest.Close()

	want := []byte("hello sidecar")
	if _, err := guest.Write(want); err != nil {
		t.Fatalf("guest write: %v", err)
	}
	if err := guest.SetReadDeadline(time.Now().Add(2 * time.Second)); err != nil {
		t.Fatalf("set read deadline: %v", err)
	}
	got := make([]byte, len(want))
	if _, err := io.ReadFull(guest, got); err != nil {
		t.Fatalf("guest read echo: %v", err)
	}
	if string(got) != string(want) {
		t.Fatalf("echo mismatch: got %q want %q", got, want)
	}

	// Cancelling the context stops the accept loop and returns nil.
	cancel()
	select {
	case err := <-serveErr:
		if err != nil {
			t.Fatalf("ServeEgress returned error on ctx cancel: %v", err)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("ServeEgress did not return after ctx cancel")
	}
}

// TestEgressListenPath pins the derived listen path so the guest-side funnel and
// the host forwarder agree on the same socket name.
func TestEgressListenPath(t *testing.T) {
	got := egressListenPath("/run/vsock.sock")
	want := "/run/vsock.sock_" + itoa(int(vsockproto.EgressPort))
	if got != want {
		t.Fatalf("egressListenPath = %q, want %q", got, want)
	}
}

func TestServeEgressReturnsListenFailure(t *testing.T) {
	udsPath := filepath.Join(t.TempDir(), "missing", "vsock.sock")
	err := ServeEgress(context.Background(), slog.Default(), udsPath, "127.0.0.1:1")
	if err == nil {
		t.Fatal("ServeEgress should fail when the socket parent does not exist")
	}
	if !strings.Contains(err.Error(), "listen") {
		t.Fatalf("ServeEgress error = %v, want listen context", err)
	}
}

// dialWithRetry dials the unix socket, retrying briefly so the test does not
// race ServeEgress's Listen call.
func dialWithRetry(t *testing.T, path string) net.Conn {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for {
		conn, err := net.Dial("unix", path)
		if err == nil {
			return conn
		}
		if time.Now().After(deadline) {
			t.Fatalf("dial %s: %v", path, err)
		}
		time.Sleep(10 * time.Millisecond)
	}
}

// itoa is a tiny local int-to-string to avoid importing strconv just for the
// path assertion.
func itoa(n int) string {
	if n == 0 {
		return "0"
	}
	var b [20]byte
	i := len(b)
	for n > 0 {
		i--
		b[i] = byte('0' + n%10)
		n /= 10
	}
	return string(b[i:])
}
