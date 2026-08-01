// Package groupclock implements the HOST side of the R5 composite-group
// clock-resync handshake against a member guest's control agent.
//
// A relit member guest resumes from a memory snapshot whose wall clock is frozen at
// the bank instant, so the daemon must re-set it before the member serves. PR-1
// froze the contract and PR-2 ships the guest agent: the agent listens on a
// dedicated vsock port (vsockproto.GroupClockAgentPort, 1024) and speaks
// LENGTH-PREFIXED JSON FRAMES, a 4-byte big-endian length prefix followed by the
// JSON body. The request is {"cmd":"sync_clock","epoch_ns":<int64>} and the
// response is {"clock_realtime_ns":<int64>}: the guest sets CLOCK_REALTIME to the
// host's epoch_ns and reads it straight back so the host can verify the set landed.
//
// This is a DELIBERATELY separate lane from the task/session HTTP clock resync
// (vsockhttp.Transport.SetClock, which POSTs epoch-MILLIS to http://vsock/shim/clock):
// that path is best-effort and never fails a relight, whereas a member relight FAILS
// (FAILED_PRECONDITION) when the read-back clock is more than one second off the
// host's, because a group member with a bad clock is not safe to serve.
//
// The frame codec and the clock source are behind small interfaces so the whole
// verify path is table-tested without a real guest (a fake conn scripting the
// response frame, a fake now): the guest-side codec in PR-2 is the mirror image and
// must match this wire format exactly.
package groupclock

import (
	"context"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"time"
)

// maxClockSkew is the tolerance the read-back clock must fall within: a relight
// whose guest clock, after the resync, differs from the host's epoch-at-send by more
// than this fails (FAILED_PRECONDITION). One second is the committed R5 bound.
const maxClockSkew = time.Second

// maxFrameLen caps a response frame so a misbehaving or hostile guest cannot make
// the host allocate an unbounded buffer off a giant length prefix. The clock
// response is a few dozen bytes; 64 KiB is generous headroom while still bounded.
const maxFrameLen = 64 << 10

// syncClockRequest is the length-prefixed JSON request frame the host writes: the
// literal command name and the host's wall clock in epoch nanoseconds.
type syncClockRequest struct {
	Cmd     string `json:"cmd"`
	EpochNs int64  `json:"epoch_ns"`
}

// syncClockResponse is the length-prefixed JSON response frame the guest agent
// writes back after setting CLOCK_REALTIME: the clock it now reads, in epoch
// nanoseconds, so the host can verify the set landed within tolerance.
type syncClockResponse struct {
	ClockRealtimeNs int64 `json:"clock_realtime_ns"`
}

// syncClockCmd is the frozen command literal (the guest agent dispatches on it).
const syncClockCmd = "sync_clock"

// Dialer opens a byte stream to the member guest's clock-agent vsock port. In
// production a vsockDialer performs the Firecracker CONNECT handshake to
// GroupClockAgentPort; a test injects a fake returning a scripted conn. Keeping the
// dial behind this seam is what lets the verify path be exercised without a guest.
type Dialer interface {
	// DialClockAgent connects to the member guest's clock agent reachable via the
	// per-VM vsock UDS at udsPath and returns a raw bidirectional byte stream.
	DialClockAgent(ctx context.Context, udsPath string) (net.Conn, error)
}

// Resync performs the sync_clock handshake against the member guest's control agent
// and verifies the read-back clock is within one second of the host's epoch at send.
// It dials the clock-agent vsock port, writes the length-prefixed sync_clock request
// carrying now.UnixNano(), reads the length-prefixed response, and returns an error
// (with the observed delta) when the read-back exceeds the tolerance. Any transport,
// framing, or JSON error is returned so the caller fails the relight: unlike the old
// best-effort HTTP resync, a member relight must not proceed on an unverified clock.
func Resync(ctx context.Context, dialer Dialer, udsPath string, now func() time.Time) error {
	conn, err := dialer.DialClockAgent(ctx, udsPath)
	if err != nil {
		return fmt.Errorf("groupclock: dial clock agent: %w", err)
	}
	defer conn.Close()
	if dl, ok := ctx.Deadline(); ok {
		_ = conn.SetDeadline(dl)
	}

	hostEpochNs := now().UnixNano()
	if err := writeFrame(conn, syncClockRequest{Cmd: syncClockCmd, EpochNs: hostEpochNs}); err != nil {
		return fmt.Errorf("groupclock: write sync_clock request: %w", err)
	}

	var resp syncClockResponse
	if err := readFrame(conn, &resp); err != nil {
		return fmt.Errorf("groupclock: read sync_clock response: %w", err)
	}

	delta := time.Duration(resp.ClockRealtimeNs-hostEpochNs) * time.Nanosecond
	if delta < 0 {
		delta = -delta
	}
	if delta > maxClockSkew {
		return fmt.Errorf("groupclock: guest clock read-back %v off host (limit %v)", delta, maxClockSkew)
	}
	return nil
}

// writeFrame encodes v as JSON and writes it prefixed with a 4-byte big-endian
// length, the frozen wire format. It is exported-shaped (a small pure helper) so the
// codec is table-tested against readFrame round-trips.
func writeFrame(w io.Writer, v any) error {
	body, err := json.Marshal(v)
	if err != nil {
		return fmt.Errorf("groupclock: marshal frame: %w", err)
	}
	var lenBuf [4]byte
	binary.BigEndian.PutUint32(lenBuf[:], uint32(len(body)))
	if _, err := w.Write(lenBuf[:]); err != nil {
		return fmt.Errorf("groupclock: write frame length: %w", err)
	}
	if _, err := w.Write(body); err != nil {
		return fmt.Errorf("groupclock: write frame body: %w", err)
	}
	return nil
}

// readFrame reads a 4-byte big-endian length prefix, then that many body bytes, and
// unmarshals the JSON body into v. A length past maxFrameLen is refused so a bad
// prefix cannot force an unbounded allocation.
func readFrame(r io.Reader, v any) error {
	var lenBuf [4]byte
	if _, err := io.ReadFull(r, lenBuf[:]); err != nil {
		return fmt.Errorf("groupclock: read frame length: %w", err)
	}
	n := binary.BigEndian.Uint32(lenBuf[:])
	if n == 0 {
		return fmt.Errorf("groupclock: empty frame")
	}
	if n > maxFrameLen {
		return fmt.Errorf("groupclock: frame length %d exceeds cap %d", n, maxFrameLen)
	}
	body := make([]byte, n)
	if _, err := io.ReadFull(r, body); err != nil {
		return fmt.Errorf("groupclock: read frame body: %w", err)
	}
	if err := json.Unmarshal(body, v); err != nil {
		return fmt.Errorf("groupclock: unmarshal frame: %w", err)
	}
	return nil
}
