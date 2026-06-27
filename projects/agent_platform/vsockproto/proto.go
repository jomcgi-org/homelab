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
	// KindHello is sent guest->host first, right after the guest dials the
	// control channel, so the controller knows the wrapper is up and ready for
	// its task assignment.
	KindHello Kind = "hello"
	// KindAssign is sent host->guest in reply to KindHello: the work assignment
	// (Recipe + Task) the harness should run. Delivering it over vsock is what
	// lets a raw FC boot (which gives PID 1 no env) receive its task.
	KindAssign Kind = "assign"
	// KindIdle is sent guest->host when the wrapper detects a safe (quiescent)
	// idle boundary and it is safe to snapshot.
	KindIdle Kind = "idle"
	// KindWake is sent host->guest after a restore to tell the harness why it
	// woke (so it can resume the right work).
	KindWake Kind = "wake"
	// KindResumeAck is sent guest->host once the wrapper has re-established its
	// connections after a restore and the harness is live again.
	KindResumeAck Kind = "resume_ack"
	// KindDone is sent guest->host when the harness process exits, so the
	// controller can reclaim the thread.
	KindDone Kind = "done"
	// KindHeartbeat is a liveness ping in either direction.
	KindHeartbeat Kind = "heartbeat"
)

// Firecracker vsock addressing. The host is always context-id 2; guests get a
// fixed id (the controller reaches a guest by its per-thread host UDS, not by
// CID). The guest dials these ports on the host: ControlPort carries the
// message channel (this protocol); EgressPort carries one tunnelled HTTP request
// each (the egress proxy to in-cluster services).
const (
	HostCID     uint32 = 2
	GuestCID    uint32 = 3
	ControlPort uint32 = 1024
	EgressPort  uint32 = 1025
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
	// Recipe and Task carry the work assignment on KindAssign: the harness runs
	// `goose run --recipe <Recipe> --params task_description=<Task>`.
	Recipe string `json:"recipe,omitempty"`
	Task   string `json:"task,omitempty"`
	// Env carries harness environment the controller injects on KindAssign (e.g.
	// the goose provider/model and the in-cluster model base URL). A raw FC boot
	// gives PID 1 no env, and these values are cluster config that must not be
	// hardcoded in the guest binary, so they arrive over the control channel.
	Env map[string]string `json:"env,omitempty"`
	// Status is the harness exit status on KindDone.
	Status string `json:"status,omitempty"`
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
