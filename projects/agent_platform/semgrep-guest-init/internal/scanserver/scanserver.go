// Package scanserver serves semgrep scan requests over a connection-oriented
// listener. In production the listener is an AF_VSOCK socket bound to
// vsockproto.ScanPort (the host dials the guest); the server reads one
// vsockproto.ScanRequest per connection, runs it through the resident semgrep lsp
// (via the Scanner seam), and writes back one vsockproto.ScanResult.
//
// Both seams are interfaces so the request/response path is unit-testable without
// real vsock or real semgrep: tests inject a fake Listener yielding pipe
// connections and a fake Scanner returning canned findings.
package scanserver

import (
	"context"
	"io"
	"log/slog"

	"github.com/jomcgi/homelab/projects/agent_platform/vsockproto"
)

// Scanner runs a scan over a batch of files and returns the findings. The resident
// lspdriver.Driver satisfies this; tests inject a fake.
type Scanner interface {
	Scan(ctx context.Context, files []vsockproto.ScanFile) ([]vsockproto.Finding, error)
}

// Listener yields connections. It is deliberately narrower than net.Listener (a
// connection is just an io.ReadWriteCloser), so the AF_VSOCK listener can hand
// back raw os.File-backed vsock connections without a net.Conn wrapper, and tests
// can hand back net.Pipe ends.
type Listener interface {
	Accept() (io.ReadWriteCloser, error)
	Close() error
}

// Server answers scan requests using Scanner.
type Server struct {
	Scanner Scanner
	Logger  *slog.Logger
}

// Serve accepts connections until ctx is cancelled or the listener fails. Each
// connection is handled in its own goroutine (one request/response per conn).
func (s *Server) Serve(ctx context.Context, ln Listener) error {
	go func() {
		<-ctx.Done()
		_ = ln.Close()
	}()
	for {
		conn, err := ln.Accept()
		if err != nil {
			if ctx.Err() != nil {
				return nil
			}
			return err
		}
		go s.handle(ctx, conn)
	}
}

// handle reads one ScanRequest, runs the scan, and writes one ScanResult. A scan
// error is returned to the caller in ScanResult.Errors rather than dropping the
// connection, so a transient rule failure is observable host-side.
func (s *Server) handle(ctx context.Context, conn io.ReadWriteCloser) {
	defer conn.Close()
	req, err := vsockproto.ReadScanRequest(conn)
	if err != nil {
		s.logWarn("scanserver: read request failed", "err", err)
		return
	}
	var result vsockproto.ScanResult
	findings, serr := s.Scanner.Scan(ctx, req.Files)
	if serr != nil {
		result.Errors = append(result.Errors, serr.Error())
	}
	result.Findings = findings
	if err := vsockproto.WriteScanResult(conn, result); err != nil {
		s.logWarn("scanserver: write result failed", "err", err)
	}
}

func (s *Server) logWarn(msg string, args ...any) {
	if s.Logger != nil {
		s.Logger.Warn(msg, args...)
	}
}
