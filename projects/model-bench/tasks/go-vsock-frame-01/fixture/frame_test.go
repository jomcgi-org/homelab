package frame

import (
	"bytes"
	"encoding/binary"
	"io"
	"testing"
)

// chunkReader returns at most `chunk` bytes per Read, the way a streamed vsock
// connection delivers a payload in several pieces.
type chunkReader struct {
	data  []byte
	chunk int
}

func (c *chunkReader) Read(p []byte) (int, error) {
	if len(c.data) == 0 {
		return 0, io.EOF
	}
	n := c.chunk
	if n > len(p) {
		n = len(p)
	}
	if n > len(c.data) {
		n = len(c.data)
	}
	copy(p, c.data[:n])
	c.data = c.data[n:]
	return n, nil
}

func encodeFrame(payload []byte) []byte {
	hdr := make([]byte, 4)
	binary.BigEndian.PutUint32(hdr, uint32(len(payload)))
	return append(hdr, payload...)
}

func TestReadFrameSmall(t *testing.T) {
	payload := []byte("hello vsock")
	got, err := ReadFrame(bytes.NewReader(encodeFrame(payload)))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !bytes.Equal(got, payload) {
		t.Fatalf("got %q, want %q", got, payload)
	}
}

func TestReadFrameChunked(t *testing.T) {
	// A 4000-byte payload delivered 7 bytes at a time forces the payload read to
	// span many Reads; a single Read would return a truncated frame.
	payload := bytes.Repeat([]byte("abcd"), 1000)
	r := &chunkReader{data: encodeFrame(payload), chunk: 7}
	got, err := ReadFrame(r)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !bytes.Equal(got, payload) {
		t.Fatalf("payload mismatch: a short read left the frame partly unfilled "+
			"(returned %d bytes, not all equal to the %d sent)", len(got), len(payload))
	}
}

func TestReadFrameRejectsOversized(t *testing.T) {
	// A length header above MaxFrame must be rejected with an error before the
	// (bogus) payload is consumed, so a malformed peer cannot drive a huge read.
	hdr := make([]byte, 4)
	binary.BigEndian.PutUint32(hdr, uint32(MaxFrame+1))
	r := bytes.NewReader(append(hdr, 1, 2, 3))
	if _, err := ReadFrame(r); err == nil {
		t.Fatal("expected an error for a frame larger than MaxFrame, got nil")
	}
}
