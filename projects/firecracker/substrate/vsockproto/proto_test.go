package vsockproto

import (
	"bytes"
	"io"
	"net"
	"reflect"
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

func TestScanRequestRoundTrip(t *testing.T) {
	var buf bytes.Buffer
	in := ScanRequest{Files: []ScanFile{
		{Path: "a.py", Content: "import os\n"},
		{Path: "b.py", Content: "x = 1\n"},
	}}
	if err := WriteScanRequest(&buf, in); err != nil {
		t.Fatalf("WriteScanRequest: %v", err)
	}
	out, err := ReadScanRequest(&buf)
	if err != nil {
		t.Fatalf("ReadScanRequest: %v", err)
	}
	if !reflect.DeepEqual(in, out) {
		t.Fatalf("round-trip mismatch:\n in=%+v\nout=%+v", in, out)
	}
}

func TestScanResultRoundTrip(t *testing.T) {
	var buf bytes.Buffer
	in := ScanResult{
		Findings: []Finding{{
			Path:     "a.py",
			Line:     3,
			Col:      5,
			RuleID:   "python.lang.security.insecure-hash",
			Severity: "ERROR",
			Message:  "weak hash",
		}},
		Errors: []string{"b.py: parse error"},
	}
	if err := WriteScanResult(&buf, in); err != nil {
		t.Fatalf("WriteScanResult: %v", err)
	}
	out, err := ReadScanResult(&buf)
	if err != nil {
		t.Fatalf("ReadScanResult: %v", err)
	}
	if !reflect.DeepEqual(in, out) {
		t.Fatalf("round-trip mismatch:\n in=%+v\nout=%+v", in, out)
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
