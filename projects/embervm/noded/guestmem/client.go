// Package guestmem observes guest memory without making lifecycle or policy decisions.
package guestmem

import (
	"bufio"
	"context"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/jomcgi/homelab/projects/embervm/noded/guestagent"
	"github.com/jomcgi/homelab/projects/embervm/noded/vsockproto"
)

const (
	maxFrameBytes  = 64 * 1024
	requestTimeout = time.Second
)

var ErrUnsupported = errors.New("guest memory agent unsupported")

// An absent listener can become available later in the same activation. Only
// an explicit unknown-command response confirms unsupported agent capability.
var ErrAgentUnavailable = fmt.Errorf("%w: listener unavailable", ErrUnsupported)

type Dialer func(context.Context, string) (net.Conn, error)

// Client uses a fresh connection and a hard budget for each request. The dialer
// returns the raw Firecracker UDS stream, before the CONNECT handshake.
type Client struct{ Dial Dialer }

func (c Client) Fetch(ctx context.Context, uds, nonce string) (guestagent.MemoryStatus, error) {
	var empty guestagent.MemoryStatus
	if nonce == "" || len(nonce) > 128 {
		return empty, errors.New("invalid request nonce")
	}
	ctx, cancel := context.WithTimeout(ctx, requestTimeout)
	defer cancel()
	dial := c.Dial
	if dial == nil {
		dial = func(ctx context.Context, uds string) (net.Conn, error) {
			return (&net.Dialer{}).DialContext(ctx, "unix", uds)
		}
	}
	conn, err := dial(ctx, uds)
	if err != nil {
		if errors.Is(err, syscall.ECONNREFUSED) {
			return empty, ErrAgentUnavailable
		}
		return empty, fmt.Errorf("dial memory agent: %w", err)
	}
	defer conn.Close()
	stop := context.AfterFunc(ctx, func() { _ = conn.Close() })
	defer stop()
	deadline, _ := ctx.Deadline()
	if err := conn.SetDeadline(deadline); err != nil {
		return empty, err
	}
	if _, err := fmt.Fprintf(conn, "CONNECT %d\n", vsockproto.GroupClockAgentPort); err != nil {
		return empty, fmt.Errorf("CONNECT write: %w", err)
	}
	// ReadSlice bounds the handshake line before any guest-controlled allocation.
	reader := bufio.NewReaderSize(conn, 64)
	line, err := reader.ReadSlice('\n')
	if err != nil {
		// Timeout/cancellation is unavailable evidence, not proof of no agent.
		if ctx.Err() != nil {
			return empty, ctx.Err()
		}
		if ne, ok := err.(net.Error); ok && ne.Timeout() {
			return empty, err
		}
		return empty, fmt.Errorf("%w: CONNECT reply: %v", ErrAgentUnavailable, err)
	}
	fields := strings.Fields(string(line))
	if len(fields) != 2 || fields[0] != "OK" {
		return empty, fmt.Errorf("%w: CONNECT rejected", ErrAgentUnavailable)
	}
	// Firecracker returns its assigned source port, not necessarily port 1024.
	port, err := strconv.ParseUint(fields[1], 10, 32)
	if err != nil || port == 0 {
		return empty, fmt.Errorf("%w: invalid CONNECT port", ErrAgentUnavailable)
	}
	body, err := json.Marshal(struct {
		Cmd   string `json:"cmd"`
		Nonce string `json:"nonce"`
	}{"memory_status", nonce})
	if err != nil {
		return empty, err
	}
	var hdr [4]byte
	binary.BigEndian.PutUint32(hdr[:], uint32(len(body)))
	if _, err := io.Copy(conn, strings.NewReader(string(hdr[:])+string(body))); err != nil {
		return empty, err
	}
	if _, err := io.ReadFull(reader, hdr[:]); err != nil {
		return empty, err
	}
	n := binary.BigEndian.Uint32(hdr[:])
	if n == 0 || n > maxFrameBytes {
		return empty, errors.New("invalid memory response frame size")
	}
	body = make([]byte, n)
	if _, err := io.ReadFull(reader, body); err != nil {
		return empty, err
	}
	var response struct {
		ClockRealtimeNs int64                    `json:"clock_realtime_ns"`
		Err             string                   `json:"err"`
		Memory          *guestagent.MemoryStatus `json:"memory_status"`
	}
	decoder := json.NewDecoder(strings.NewReader(string(body)))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&response); err != nil {
		return empty, fmt.Errorf("decode memory status: %w", err)
	}
	if err := decoder.Decode(new(any)); err != io.EOF {
		return empty, errors.New("trailing memory response data")
	}
	if response.Err != "" {
		if response.Err == `unknown command "memory_status"` && response.Memory == nil {
			return empty, ErrUnsupported
		}
		return empty, errors.New("guest memory command failed")
	}
	if response.Memory == nil || response.ClockRealtimeNs != 0 {
		return empty, errors.New("missing or unknown memory response")
	}
	m := *response.Memory
	if err := m.Validate(); err != nil {
		return empty, err
	}
	if m.Nonce != nonce {
		return empty, errors.New("memory response nonce mismatch")
	}
	if err := ctx.Err(); err != nil {
		return empty, err
	}
	return m, nil
}
