package guestagent

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
)

// Clock abstracts the two CLOCK_REALTIME syscalls the sync_clock handler needs,
// so the handler is table-testable without root (a fake stands in for the real
// syscalls). realClock in clock_linux.go is the production implementation.
type Clock interface {
	// SetRealtime sets CLOCK_REALTIME to epochNs (nanoseconds since the Unix
	// epoch). It requires CAP_SYS_TIME (root inside the guest, which the k3s
	// guest already runs as, see the image README).
	SetRealtime(epochNs int64) error
	// GetRealtime reads CLOCK_REALTIME back as nanoseconds since the Unix epoch.
	GetRealtime() (int64, error)
}

// Agent serves the frozen group-guest-agent contract over a net.Listener. It is
// transport-agnostic (the vsock listener is supplied by the caller) so tests
// drive it over an in-memory or loopback listener without a microVM.
type Agent struct {
	clock  Clock
	logger *slog.Logger
}

// New returns an Agent backed by clock. A nil logger is replaced with the
// default so the guest-init caller need not thread one through.
func New(clock Clock, logger *slog.Logger) *Agent {
	if logger == nil {
		logger = slog.Default()
	}
	return &Agent{clock: clock, logger: logger}
}

// Serve accepts connections on ln until ln is closed, handling each in its own
// goroutine. The node opens one connection per sync_clock exchange (one
// in-flight command per the contract), but serving each connection in a
// goroutine keeps a slow or stalled peer from blocking the accept loop. Serve
// returns the Accept error when ln is closed (the guest-init treats a closed
// listener as a clean shutdown).
func (a *Agent) Serve(ln net.Listener) error {
	for {
		conn, err := ln.Accept()
		if err != nil {
			return err
		}
		go a.handleConn(conn)
	}
}

// handleConn services one connection: it reads frames and answers each until the
// peer closes (io.EOF) or a framing error occurs. The node's contract is one
// command per connection, but the loop tolerates a peer that pipelines.
func (a *Agent) handleConn(conn net.Conn) {
	defer conn.Close() //nolint:errcheck // best-effort close on a control channel
	for {
		body, err := readFrame(conn)
		if err != nil {
			// A clean close (io.EOF / io.ErrUnexpectedEOF) is the expected end
			// of a one-command exchange; anything else is logged but still ends
			// the connection.
			if !errors.Is(err, io.EOF) && !errors.Is(err, io.ErrUnexpectedEOF) {
				a.logger.Warn("guestagent: read frame", "err", err)
			}
			return
		}
		resp := a.handle(body)
		out, err := json.Marshal(resp)
		if err != nil {
			a.logger.Error("guestagent: marshal response", "err", err)
			return
		}
		if err := writeFrame(conn, out); err != nil {
			a.logger.Warn("guestagent: write frame", "err", err)
			return
		}
	}
}

// handle decodes one request frame body and produces the response. It is the
// pure core of the agent (no I/O), so it is directly table-testable with a fake
// clock. An unparseable body, an unknown command, or a clock syscall failure all
// return a response carrying err (never a panic): a control-channel command that
// fails must report the failure so the node fails the resume, not hang.
func (a *Agent) handle(body []byte) response {
	var req request
	if err := json.Unmarshal(body, &req); err != nil {
		return response{Err: fmt.Sprintf("decode request: %v", err)}
	}
	if req.Cmd != syncClockCmd {
		return response{Err: fmt.Sprintf("unknown command %q", req.Cmd)}
	}
	if err := a.clock.SetRealtime(req.EpochNs); err != nil {
		return response{Err: fmt.Sprintf("set CLOCK_REALTIME: %v", err)}
	}
	now, err := a.clock.GetRealtime()
	if err != nil {
		return response{Err: fmt.Sprintf("read CLOCK_REALTIME: %v", err)}
	}
	return response{ClockRealtimeNs: now}
}
