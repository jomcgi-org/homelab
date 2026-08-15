package guestagent

import (
	"bytes"
	"encoding/json"
	"errors"
	"net"
	"testing"
	"time"
)

// fakeClock is a table-testable Clock: SetRealtime records the last value (and
// optionally fails), GetRealtime returns a scripted read-back (and optionally
// fails). It lets the handler be exercised without root or a real syscall.
type fakeClock struct {
	set       int64
	setErr    error
	readBack  int64
	getErr    error
	setCalled bool
}

func (c *fakeClock) SetRealtime(ns int64) error {
	c.setCalled = true
	if c.setErr != nil {
		return c.setErr
	}
	c.set = ns
	return nil
}

func (c *fakeClock) GetRealtime() (int64, error) {
	if c.getErr != nil {
		return 0, c.getErr
	}
	return c.readBack, nil
}

// TestHandle is the table-driven core: every request-shape / clock-outcome pair
// maps to the expected response, with a fake clock standing in for the syscalls.
func TestHandle(t *testing.T) {
	cases := []struct {
		name        string
		body        string
		clock       *fakeClock
		wantClockNs int64
		wantErrSub  string // substring expected in response.Err ("" = no error)
		wantSet     bool   // whether SetRealtime should have been called
		wantSetVal  int64  // the epoch the handler should have passed to SetRealtime
	}{
		{
			name:        "sync_clock success sets then reads back",
			body:        `{"cmd":"sync_clock","epoch_ns":1700000000000000000}`,
			clock:       &fakeClock{readBack: 1700000000000000123},
			wantClockNs: 1700000000000000123,
			wantSet:     true,
			wantSetVal:  1700000000000000000,
		},
		{
			name:       "unknown command rejected without touching the clock",
			body:       `{"cmd":"reboot","epoch_ns":1}`,
			clock:      &fakeClock{},
			wantErrSub: `unknown command "reboot"`,
			wantSet:    false,
		},
		{
			name:       "malformed json returns a decode error",
			body:       `{not json`,
			clock:      &fakeClock{},
			wantErrSub: "decode request",
			wantSet:    false,
		},
		{
			name:       "set failure surfaces as err",
			body:       `{"cmd":"sync_clock","epoch_ns":5}`,
			clock:      &fakeClock{setErr: errors.New("EPERM")},
			wantErrSub: "set CLOCK_REALTIME",
			wantSet:    true,
			wantSetVal: 0, // set recorded nothing on failure
		},
		{
			name:       "read-back failure surfaces as err",
			body:       `{"cmd":"sync_clock","epoch_ns":9}`,
			clock:      &fakeClock{getErr: errors.New("boom")},
			wantErrSub: "read CLOCK_REALTIME",
			wantSet:    true,
			wantSetVal: 9,
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			a := New(tc.clock, nil)
			resp := a.handle([]byte(tc.body))
			if tc.wantErrSub == "" {
				if resp.Err != "" {
					t.Fatalf("unexpected err: %q", resp.Err)
				}
				if resp.ClockRealtimeNs != tc.wantClockNs {
					t.Fatalf("clock_realtime_ns = %d, want %d", resp.ClockRealtimeNs, tc.wantClockNs)
				}
			} else {
				if resp.Err == "" || !contains(resp.Err, tc.wantErrSub) {
					t.Fatalf("err = %q, want substring %q", resp.Err, tc.wantErrSub)
				}
				if resp.ClockRealtimeNs != 0 {
					t.Fatalf("error response must carry zero clock, got %d", resp.ClockRealtimeNs)
				}
			}
			if tc.clock.setCalled != tc.wantSet {
				t.Fatalf("SetRealtime called = %v, want %v", tc.clock.setCalled, tc.wantSet)
			}
			if tc.wantSet && tc.clock.setErr == nil && tc.clock.set != tc.wantSetVal {
				t.Fatalf("SetRealtime got %d, want %d", tc.clock.set, tc.wantSetVal)
			}
		})
	}
}

// TestServeRoundTrip drives the full Serve path over a loopback TCP listener (a
// stand-in for the vsock listener): a client writes a sync_clock frame and reads
// the response frame back, proving the codec + handler wire together.
func TestServeRoundTrip(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	defer ln.Close()

	clock := &fakeClock{readBack: 1234567890}
	agent := New(clock, nil)
	go func() { _ = agent.Serve(ln) }()

	conn, err := net.Dial("tcp", ln.Addr().String())
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	defer conn.Close()

	reqBody, _ := json.Marshal(request{Cmd: "sync_clock", EpochNs: 42})
	if err := writeFrame(conn, reqBody); err != nil {
		t.Fatalf("writeFrame: %v", err)
	}
	respBody, err := readFrame(conn)
	if err != nil {
		t.Fatalf("readFrame: %v", err)
	}
	var resp response
	if err := json.Unmarshal(respBody, &resp); err != nil {
		t.Fatalf("unmarshal response: %v", err)
	}
	if resp.Err != "" {
		t.Fatalf("unexpected err: %q", resp.Err)
	}
	if resp.ClockRealtimeNs != 1234567890 {
		t.Fatalf("clock_realtime_ns = %d, want 1234567890", resp.ClockRealtimeNs)
	}
	if clock.set != 42 {
		t.Fatalf("SetRealtime got %d, want 42", clock.set)
	}
}

// TestServeReturnsOnClose proves Serve returns (rather than spinning) once the
// listener is closed, so the guest-init treats it as a clean shutdown.
func TestServeReturnsOnClose(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	done := make(chan error, 1)
	agent := New(&fakeClock{}, nil)
	go func() { done <- agent.Serve(ln) }()
	_ = ln.Close()
	if err := <-done; err == nil {
		t.Fatal("expected Serve to return an error after listener close")
	}
}

func TestServeStalledPeerDoesNotBlockAnotherConnection(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	defer ln.Close()

	clock := &fakeClock{readBack: 99}
	agent := New(clock, nil)
	go func() { _ = agent.Serve(ln) }()

	stalled, err := net.Dial("tcp", ln.Addr().String())
	if err != nil {
		t.Fatalf("dial stalled peer: %v", err)
	}
	defer stalled.Close()

	active, err := net.Dial("tcp", ln.Addr().String())
	if err != nil {
		t.Fatalf("dial active peer: %v", err)
	}
	defer active.Close()
	if err := active.SetDeadline(time.Now().Add(time.Second)); err != nil {
		t.Fatalf("set active deadline: %v", err)
	}
	body, err := json.Marshal(request{Cmd: syncClockCmd, EpochNs: 42})
	if err != nil {
		t.Fatalf("marshal request: %v", err)
	}
	if err := writeFrame(active, body); err != nil {
		t.Fatalf("write active frame: %v", err)
	}
	respBody, err := readFrame(active)
	if err != nil {
		t.Fatalf("read active response while another peer is stalled: %v", err)
	}
	var resp response
	if err := json.Unmarshal(respBody, &resp); err != nil {
		t.Fatalf("unmarshal response: %v", err)
	}
	if resp.Err != "" || resp.ClockRealtimeNs != 99 {
		t.Fatalf("response = %+v, want clock 99 with no error", resp)
	}
}

func contains(s, sub string) bool { return bytes.Contains([]byte(s), []byte(sub)) }
