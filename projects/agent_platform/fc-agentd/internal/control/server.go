// Package control is the host side of the ADR 022 vsock control channel. The
// guest (fc-agent-init) dials the host over vsock; Firecracker bridges that to a
// per-thread unix socket at "<uds>_<port>", so the host side is a plain unix
// listener. The server hands the guest its task (Assign) in reply to Hello, then
// dispatches the guest's lifecycle signals (Idle, Done) to the caller.
package control

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"os"

	"github.com/jomcgi/homelab/projects/agent_platform/vsockproto"
)

// Assignment is the work handed to a guest in the Assign reply.
type Assignment struct {
	ThreadID string
	Recipe   string
	Task     string
	// Env is harness environment injected into the guest (goose provider/model,
	// the in-cluster model base URL): cluster config the guest cannot hardcode.
	Env map[string]string
}

// Handlers are invoked for the guest's lifecycle signals. They run on the
// server goroutine, so they must not block for long.
type Handlers struct {
	// OnIdle fires when the guest reports a quiescent idle boundary (safe to
	// snapshot). May be nil.
	OnIdle func(threadID string, wake vsockproto.WakeCondition)
	// OnDone fires when the guest's harness exits. May be nil.
	OnDone func(threadID, status string)
}

// listenPath is the host unix socket Firecracker bridges the guest's
// control-port connection to.
func listenPath(udsPath string) string {
	return fmt.Sprintf("%s_%d", udsPath, vsockproto.ControlPort)
}

// Serve listens on the thread's control socket, accepts the single guest
// connection, sends the Assign, and dispatches inbound messages to handlers
// until the connection closes or ctx is cancelled. It returns nil on a clean
// guest disconnect or ctx cancellation.
func Serve(ctx context.Context, logger *slog.Logger, udsPath string, a Assignment, h Handlers) error {
	path := listenPath(udsPath)
	_ = os.Remove(path)
	ln, err := net.Listen("unix", path)
	if err != nil {
		return fmt.Errorf("control: listen %s: %w", path, err)
	}
	defer func() {
		_ = ln.Close()
		_ = os.Remove(path)
	}()

	// Unblock Accept on cancellation by closing the listener.
	go func() {
		<-ctx.Done()
		_ = ln.Close()
	}()

	raw, err := ln.Accept()
	if err != nil {
		if ctx.Err() != nil {
			return nil
		}
		return fmt.Errorf("control: accept: %w", err)
	}
	defer raw.Close()

	conn := vsockproto.NewConn(raw)
	hello, err := conn.Recv()
	if err != nil {
		return fmt.Errorf("control: read hello: %w", err)
	}
	if hello.Kind != vsockproto.KindHello {
		return fmt.Errorf("control: expected hello, got %q", hello.Kind)
	}
	if err := conn.Send(vsockproto.Message{
		Kind:     vsockproto.KindAssign,
		ThreadID: a.ThreadID,
		Recipe:   a.Recipe,
		Task:     a.Task,
		Env:      a.Env,
	}); err != nil {
		return fmt.Errorf("control: send assign: %w", err)
	}
	logger.Info("control: assigned task to guest", "thread", a.ThreadID, "recipe", a.Recipe)

	for {
		msg, err := conn.Recv()
		if err != nil {
			if ctx.Err() != nil || errors.Is(err, net.ErrClosed) {
				return nil
			}
			// A clean guest disconnect (EOF) ends the session without error.
			return nil
		}
		switch msg.Kind {
		case vsockproto.KindIdle:
			if h.OnIdle != nil {
				h.OnIdle(a.ThreadID, msg.Wake)
			}
		case vsockproto.KindDone:
			if h.OnDone != nil {
				h.OnDone(a.ThreadID, msg.Status)
			}
			return nil
		case vsockproto.KindHeartbeat:
			_ = conn.Send(vsockproto.Message{Kind: vsockproto.KindHeartbeat, ThreadID: a.ThreadID})
		default:
			logger.Debug("control: ignoring message", "thread", a.ThreadID, "kind", string(msg.Kind))
		}
	}
}
