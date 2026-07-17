// Package guestagent is the EmberVM composite guest control agent: a tiny
// server baked into composite (k3s) guest images that listens on a dedicated
// vsock port and answers the frozen R5 group-guest-agent contract. Its only v1
// command is `sync_clock`, which the node issues immediately after a member's
// snapshot resume (standing decision 7): the node sends the host epoch, the
// agent sets CLOCK_REALTIME from it, and responds with the post-set clock so the
// node can verify the delta is within one second before accepting the resume.
//
// The wire is the D-R2.6.1 framing convention (the same the R2 sandbox guest
// uses): a 4-byte big-endian length prefix followed by a JSON body, both
// directions. It is a DELIBERATE, separate lane from the HTTP-over-vsock guest
// contract on GuestHTTPPort (1027): the clock channel is raw length-prefixed
// frames on the frozen group-agent port (1024) so a resume-time control command
// never contends with, and is never framed by, the inbound HTTP request lane.
//
// The clock syscall sits behind the Clock interface so the frame codec and the
// sync_clock handler are table-testable without root (a fake clock stands in for
// the CLOCK_REALTIME syscalls in tests); the real syscall implementation is the
// Linux-only realClock in clock_linux.go.
package guestagent

import (
	"encoding/binary"
	"errors"
	"fmt"
	"io"
)

// GroupGuestAgentVsockPort is the FROZEN R5 group-guest-agent vsock port
// (standing decision 7, Task 1 proto). It is deliberately distinct from
// vsockproto.EgressPort (1025), GuestHTTPPort (1027), and their neighbours so
// the raw length-prefixed clock lane never collides with the HTTP-over-vsock
// request lane. Do not move it: the node dials exactly this port after resume.
const GroupGuestAgentVsockPort uint32 = 1024

// maxFrameBytes caps a single frame's JSON body so a malformed or adversarial
// length prefix cannot drive an unbounded allocation. The sync_clock frames are
// tiny (two integer fields); 64 KiB is orders of magnitude of headroom while
// still refusing a hostile 4 GiB length. Mirrors the R2 guest's 16 MiB guard
// scaled to this channel's far smaller payloads.
const maxFrameBytes = 64 * 1024

// ErrFrameTooLarge is returned by readFrame when the length prefix exceeds
// maxFrameBytes. It is checked BEFORE any allocation, so an oversized prefix is
// rejected rather than serviced.
var ErrFrameTooLarge = errors.New("guestagent: frame length exceeds maximum")

// syncClockCmd is the only command verb the agent understands in v1.
const syncClockCmd = "sync_clock"

// request is the inbound frame body. The node sends {"cmd":"sync_clock",
// "epoch_ns":<int>}; epoch_ns is the host wall-clock in nanoseconds since the
// Unix epoch that the agent writes to CLOCK_REALTIME.
type request struct {
	Cmd     string `json:"cmd"`
	EpochNs int64  `json:"epoch_ns"`
}

// response is the outbound frame body. clock_realtime_ns is the guest's
// CLOCK_REALTIME read back AFTER the set, so the node verifies the applied clock
// rather than trusting the value it sent. On any handling error err carries a
// short message and clock_realtime_ns is zero.
type response struct {
	ClockRealtimeNs int64  `json:"clock_realtime_ns"`
	Err             string `json:"err,omitempty"`
}

// writeFrame writes a single length-prefixed frame: a 4-byte big-endian length
// followed by body. It is the encode half of the D-R2.6.1 codec.
func writeFrame(w io.Writer, body []byte) error {
	if len(body) > maxFrameBytes {
		return ErrFrameTooLarge
	}
	var hdr [4]byte
	binary.BigEndian.PutUint32(hdr[:], uint32(len(body)))
	if _, err := w.Write(hdr[:]); err != nil {
		return fmt.Errorf("guestagent: write frame header: %w", err)
	}
	if _, err := w.Write(body); err != nil {
		return fmt.Errorf("guestagent: write frame body: %w", err)
	}
	return nil
}

// readFrame reads a single length-prefixed frame. io.ReadFull reassembles a
// header or body split across multiple reads (the decode half of D-R2.6.1). A
// length prefix over maxFrameBytes returns ErrFrameTooLarge BEFORE allocating
// the body buffer, so an oversized prefix never drives a large allocation.
func readFrame(r io.Reader) ([]byte, error) {
	var hdr [4]byte
	if _, err := io.ReadFull(r, hdr[:]); err != nil {
		// io.EOF / io.ErrUnexpectedEOF are surfaced verbatim so the caller can
		// treat a clean connection close as end-of-stream rather than an error.
		return nil, err
	}
	n := binary.BigEndian.Uint32(hdr[:])
	if n > maxFrameBytes {
		return nil, ErrFrameTooLarge
	}
	body := make([]byte, n)
	if _, err := io.ReadFull(r, body); err != nil {
		return nil, err
	}
	return body, nil
}
