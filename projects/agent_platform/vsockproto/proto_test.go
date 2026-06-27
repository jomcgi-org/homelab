package vsockproto

import (
	"io"
	"net"
	"testing"
	"time"
)

func TestSendRecvRoundTrip(t *testing.T) {
	guest, host := net.Pipe()
	gc := NewConn(guest)
	hc := NewConn(host)

	go func() {
		_ = gc.Send(Message{Kind: KindIdle, ThreadID: "t1", Wake: WakeDiscordReply, Reason: "between turns"})
	}()

	done := make(chan Message, 1)
	go func() {
		m, err := hc.Recv()
		if err != nil {
			t.Errorf("Recv: %v", err)
		}
		done <- m
	}()

	select {
	case m := <-done:
		if m.Kind != KindIdle || m.ThreadID != "t1" || m.Wake != WakeDiscordReply {
			t.Fatalf("unexpected message: %+v", m)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("timed out waiting for message")
	}
}

func TestSendRejectsEmptyKind(t *testing.T) {
	guest, _ := net.Pipe()
	if err := NewConn(guest).Send(Message{ThreadID: "t1"}); err == nil {
		t.Fatal("Send should reject a message with no kind")
	}
}

func TestRecvEOFOnClose(t *testing.T) {
	guest, host := net.Pipe()
	hc := NewConn(host)
	_ = guest.Close()
	if _, err := hc.Recv(); err != io.EOF && err != io.ErrClosedPipe {
		t.Fatalf("expected EOF/closed after peer close, got %v", err)
	}
}
