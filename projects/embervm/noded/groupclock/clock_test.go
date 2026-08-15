package groupclock

import (
	"bytes"
	"context"
	"encoding/binary"
	"encoding/json"
	"errors"
	"io"
	"net"
	"testing"
	"time"
)

// fakeConn is a net.Conn whose reads drain a scripted buffer and whose writes are
// captured, so a test can assert the request frame the host sent and script the
// response frame the "guest" returns. Deadlines are recorded but not enforced.
type fakeConn struct {
	readBuf  *bytes.Reader
	writeBuf bytes.Buffer
	closed   bool
	readErr  error
}

func (c *fakeConn) Read(p []byte) (int, error) {
	if c.readErr != nil {
		return 0, c.readErr
	}
	return c.readBuf.Read(p)
}
func (c *fakeConn) Write(p []byte) (int, error)      { return c.writeBuf.Write(p) }
func (c *fakeConn) Close() error                     { c.closed = true; return nil }
func (c *fakeConn) LocalAddr() net.Addr              { return nil }
func (c *fakeConn) RemoteAddr() net.Addr             { return nil }
func (c *fakeConn) SetDeadline(time.Time) error      { return nil }
func (c *fakeConn) SetReadDeadline(time.Time) error  { return nil }
func (c *fakeConn) SetWriteDeadline(time.Time) error { return nil }

// fakeDialer returns a scripted fakeConn (or a dial error).
type fakeDialer struct {
	conn    net.Conn
	dialErr error
}

func (d *fakeDialer) DialClockAgent(context.Context, string) (net.Conn, error) {
	if d.dialErr != nil {
		return nil, d.dialErr
	}
	return d.conn, nil
}

// frameOf encodes v as the length-prefixed JSON wire frame the guest agent would
// return, so a test can script a response.
func frameOf(t *testing.T, v any) []byte {
	t.Helper()
	body, err := json.Marshal(v)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	var buf bytes.Buffer
	var lenBuf [4]byte
	binary.BigEndian.PutUint32(lenBuf[:], uint32(len(body)))
	buf.Write(lenBuf[:])
	buf.Write(body)
	return buf.Bytes()
}

// TestFrameRoundTrip proves writeFrame and readFrame agree on the wire format (a
// 4-byte big-endian length prefix + JSON body), the same format the guest mirrors.
func TestFrameRoundTrip(t *testing.T) {
	var buf bytes.Buffer
	req := syncClockRequest{Cmd: syncClockCmd, EpochNs: 1234567890}
	if err := writeFrame(&buf, req); err != nil {
		t.Fatalf("writeFrame: %v", err)
	}
	// The first four bytes are the big-endian length of the JSON body.
	raw := buf.Bytes()
	n := binary.BigEndian.Uint32(raw[:4])
	if int(n) != len(raw)-4 {
		t.Errorf("length prefix %d does not match body length %d", n, len(raw)-4)
	}
	var got syncClockRequest
	if err := readFrame(bytes.NewReader(raw), &got); err != nil {
		t.Fatalf("readFrame: %v", err)
	}
	if got.Cmd != "sync_clock" || got.EpochNs != 1234567890 {
		t.Errorf("round-trip mismatch: %+v", got)
	}
}

// TestResyncSuccess proves a guest that echoes a clock within one second verifies,
// and that the host sent the frozen sync_clock request carrying its epoch_ns.
func TestResyncSuccess(t *testing.T) {
	now := time.Unix(1_700_000_000, 500)
	// Guest returns a clock 200ms ahead: within the one-second tolerance.
	resp := syncClockResponse{ClockRealtimeNs: now.UnixNano() + int64(200*time.Millisecond)}
	conn := &fakeConn{readBuf: bytes.NewReader(frameOf(t, resp))}
	dialer := &fakeDialer{conn: conn}

	if err := Resync(context.Background(), dialer, "/uds", func() time.Time { return now }); err != nil {
		t.Fatalf("Resync should succeed within tolerance: %v", err)
	}
	// Assert the request frame the host wrote: length prefix + {"cmd":"sync_clock",...}.
	written := conn.writeBuf.Bytes()
	var sent syncClockRequest
	if err := readFrame(bytes.NewReader(written), &sent); err != nil {
		t.Fatalf("decode host request frame: %v", err)
	}
	if sent.Cmd != "sync_clock" {
		t.Errorf("host sent cmd %q want sync_clock", sent.Cmd)
	}
	if sent.EpochNs != now.UnixNano() {
		t.Errorf("host sent epoch_ns %d want %d (epoch NANOS, not millis)", sent.EpochNs, now.UnixNano())
	}
	if !conn.closed {
		t.Error("Resync should close the conn")
	}
}

// TestResyncFailsWhenClockTooFarOff proves a read-back more than one second off the
// host's epoch fails the call, with the delta surfaced in the error.
func TestResyncFailsWhenClockTooFarOff(t *testing.T) {
	now := time.Unix(1_700_000_000, 0)
	// Guest returns a clock 3 seconds behind: outside the one-second tolerance.
	resp := syncClockResponse{ClockRealtimeNs: now.UnixNano() - int64(3*time.Second)}
	conn := &fakeConn{readBuf: bytes.NewReader(frameOf(t, resp))}
	dialer := &fakeDialer{conn: conn}

	err := Resync(context.Background(), dialer, "/uds", func() time.Time { return now })
	if err == nil {
		t.Fatal("Resync should fail when the read-back clock is more than 1s off")
	}
}

// TestResyncDialError proves a dial failure surfaces as an error (fails the relight).
func TestResyncDialError(t *testing.T) {
	dialer := &fakeDialer{dialErr: errors.New("connection refused")}
	err := Resync(context.Background(), dialer, "/uds", time.Now)
	if err == nil {
		t.Fatal("a dial error must fail Resync")
	}
}

// TestResyncReadError proves a transport/framing read error (the timeout / no-agent
// case) surfaces as an error rather than silently passing.
func TestResyncReadError(t *testing.T) {
	conn := &fakeConn{readBuf: bytes.NewReader(nil), readErr: io.ErrUnexpectedEOF}
	dialer := &fakeDialer{conn: conn}
	err := Resync(context.Background(), dialer, "/uds", time.Now)
	if err == nil {
		t.Fatal("a read error (no response frame) must fail Resync")
	}
}

func TestResyncSilentGuestHonorsDeadline(t *testing.T) {
	host, guest := net.Pipe()
	defer guest.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()
	requestRead := make(chan struct{})
	go func() {
		var req syncClockRequest
		_ = readFrame(guest, &req)
		close(requestRead)
		<-ctx.Done()
	}()

	start := time.Now()
	err := Resync(ctx, &fakeDialer{conn: host}, "/uds", time.Now)
	if err == nil {
		t.Fatal("a guest that never answers must fail Resync")
	}
	if elapsed := time.Since(start); elapsed > time.Second {
		t.Fatalf("Resync returned after %v, want the context deadline to bound it", elapsed)
	}
	select {
	case <-requestRead:
	default:
		t.Fatal("silent-guest test never delivered the sync request")
	}
}

// TestReadFrameRejectsOversizeLength proves a bogus giant length prefix is refused
// rather than triggering an unbounded allocation.
func TestReadFrameRejectsOversizeLength(t *testing.T) {
	var lenBuf [4]byte
	binary.BigEndian.PutUint32(lenBuf[:], maxFrameLen+1)
	var got syncClockResponse
	if err := readFrame(bytes.NewReader(lenBuf[:]), &got); err == nil {
		t.Fatal("readFrame should reject a length past the cap")
	}
}
