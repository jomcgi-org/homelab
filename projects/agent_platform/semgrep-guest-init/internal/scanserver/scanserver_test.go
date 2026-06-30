package scanserver

import (
	"context"
	"io"
	"net"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/agent_platform/vsockproto"
)

// fakeScanner returns canned findings (or an error) for any request.
type fakeScanner struct {
	findings []vsockproto.Finding
	err      error
}

func (f *fakeScanner) Scan(context.Context, []vsockproto.ScanFile) ([]vsockproto.Finding, error) {
	return f.findings, f.err
}

// fakeListener yields a fixed set of connections then blocks (Accept never returns
// again) until closed, mimicking a real listener.
type fakeListener struct {
	conns  chan io.ReadWriteCloser
	closed chan struct{}
}

func newFakeListener(conns ...io.ReadWriteCloser) *fakeListener {
	ch := make(chan io.ReadWriteCloser, len(conns))
	for _, c := range conns {
		ch <- c
	}
	return &fakeListener{conns: ch, closed: make(chan struct{})}
}

func (l *fakeListener) Accept() (io.ReadWriteCloser, error) {
	select {
	case c := <-l.conns:
		return c, nil
	case <-l.closed:
		return nil, io.EOF
	}
}

func (l *fakeListener) Close() error {
	select {
	case <-l.closed:
	default:
		close(l.closed)
	}
	return nil
}

func TestHandleReturnsScanResult(t *testing.T) {
	client, server := net.Pipe()
	scanner := &fakeScanner{findings: []vsockproto.Finding{{
		Path:     "a.py",
		Line:     3,
		Col:      5,
		RuleID:   "rule.x",
		Severity: "ERROR",
		Message:  "bad",
	}}}
	s := &Server{Scanner: scanner}

	go s.handle(context.Background(), server)

	// Client side: send the request, read the result.
	go func() {
		_ = vsockproto.WriteScanRequest(client, vsockproto.ScanRequest{
			Files: []vsockproto.ScanFile{{Path: "a.py", Content: "bad()\n"}},
		})
	}()

	done := make(chan vsockproto.ScanResult, 1)
	go func() {
		res, err := vsockproto.ReadScanResult(client)
		if err != nil {
			t.Errorf("ReadScanResult: %v", err)
		}
		done <- res
	}()

	select {
	case res := <-done:
		if len(res.Findings) != 1 || res.Findings[0] != scanner.findings[0] {
			t.Fatalf("unexpected findings: %+v", res.Findings)
		}
		if len(res.Errors) != 0 {
			t.Fatalf("unexpected errors: %+v", res.Errors)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("timed out waiting for scan result")
	}
}

func TestHandleSurfacesScanError(t *testing.T) {
	client, server := net.Pipe()
	s := &Server{Scanner: &fakeScanner{err: errBoom}}

	go s.handle(context.Background(), server)
	go func() {
		_ = vsockproto.WriteScanRequest(client, vsockproto.ScanRequest{})
	}()

	res, err := vsockproto.ReadScanResult(client)
	if err != nil {
		t.Fatalf("ReadScanResult: %v", err)
	}
	if len(res.Errors) != 1 || res.Errors[0] != "boom" {
		t.Fatalf("expected scan error surfaced, got %+v", res.Errors)
	}
}

func TestServeAcceptsAndStopsOnCancel(t *testing.T) {
	client, server := net.Pipe()
	ln := newFakeListener(server)
	s := &Server{Scanner: &fakeScanner{}}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	serveErr := make(chan error, 1)
	go func() { serveErr <- s.Serve(ctx, ln) }()

	go func() {
		_ = vsockproto.WriteScanRequest(client, vsockproto.ScanRequest{
			Files: []vsockproto.ScanFile{{Path: "a.py", Content: "x\n"}},
		})
	}()
	if _, err := vsockproto.ReadScanResult(client); err != nil {
		t.Fatalf("ReadScanResult: %v", err)
	}

	cancel()
	select {
	case err := <-serveErr:
		if err != nil {
			t.Fatalf("Serve returned error on cancel: %v", err)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("Serve did not stop after cancel")
	}
}

type boomErr struct{}

func (boomErr) Error() string { return "boom" }

var errBoom = boomErr{}
