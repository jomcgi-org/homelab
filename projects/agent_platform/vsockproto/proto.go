// Package vsockproto is the minimal wire contract between the in-microVM wrapper
// (fc-agent-init) and the host controller (fc-agentd). It is the one new
// in-guest/host contract ADR 022 introduces, kept deliberately small: an
// idle-signal (with the condition that should wake the thread), a wake
// notification, and a resume acknowledgement.
//
// Messages are newline-delimited JSON over any io.ReadWriteCloser, so the same
// code runs over a real vsock connection in the guest and over a unix/tcp pipe
// in tests.
package vsockproto

import (
	"bufio"
	"encoding/json"
	"fmt"
	"io"
)

// Kind enumerates the message types on the channel.
type Kind string

const (
	// KindIdle is sent guest->host when the wrapper detects a safe (quiescent)
	// idle boundary and it is safe to snapshot.
	KindIdle Kind = "idle"
	// KindWake is sent host->guest after a restore to tell the harness why it
	// woke (so it can resume the right work).
	KindWake Kind = "wake"
	// KindResumeAck is sent guest->host once the wrapper has re-established its
	// connections after a restore and the harness is live again.
	KindResumeAck Kind = "resume_ack"
	// KindHeartbeat is a liveness ping in either direction.
	KindHeartbeat Kind = "heartbeat"
)

// WakeCondition describes what should cause an idle thread to be restored.
type WakeCondition string

const (
	WakeManual       WakeCondition = "manual"
	WakeDiscordReply WakeCondition = "discord_reply"
	WakeCIEvent      WakeCondition = "ci_event"
	WakeTimer        WakeCondition = "timer"
)

// Message is one frame on the wrapper<->controller channel.
type Message struct {
	Kind Kind `json:"kind"`
	// ThreadID identifies the agent thread this message concerns.
	ThreadID string `json:"thread_id,omitempty"`
	// Wake is the condition that should wake the thread (set on KindIdle) or the
	// condition that did wake it (set on KindWake).
	Wake WakeCondition `json:"wake,omitempty"`
	// Reason is a human-readable note for logs/observability.
	Reason string `json:"reason,omitempty"`
}

// Conn is a framed message channel over an io.ReadWriteCloser.
type Conn struct {
	rw  io.ReadWriteCloser
	r   *bufio.Reader
	enc *json.Encoder
}

// NewConn wraps an io.ReadWriteCloser (a vsock conn in prod, a pipe in tests).
func NewConn(rw io.ReadWriteCloser) *Conn {
	return &Conn{rw: rw, r: bufio.NewReader(rw), enc: json.NewEncoder(rw)}
}

// Send writes one message followed by a newline.
func (c *Conn) Send(m Message) error {
	if m.Kind == "" {
		return fmt.Errorf("vsockproto: message missing kind")
	}
	if err := c.enc.Encode(m); err != nil {
		return fmt.Errorf("vsockproto: send: %w", err)
	}
	return nil
}

// Recv reads the next message. It returns io.EOF when the peer closes.
func (c *Conn) Recv() (Message, error) {
	line, err := c.r.ReadBytes('\n')
	if err != nil && len(line) == 0 {
		return Message{}, err
	}
	var m Message
	if uerr := json.Unmarshal(line, &m); uerr != nil {
		return Message{}, fmt.Errorf("vsockproto: decode %q: %w", line, uerr)
	}
	return m, nil
}

// Close closes the underlying connection.
func (c *Conn) Close() error { return c.rw.Close() }
