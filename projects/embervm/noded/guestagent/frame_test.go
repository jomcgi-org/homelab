package guestagent

import (
	"bytes"
	"encoding/binary"
	"errors"
	"io"
	"testing"
)

// TestFrameRoundTrip proves writeFrame then readFrame recovers the exact body
// across a range of sizes, including the empty body and the maximum.
func TestFrameRoundTrip(t *testing.T) {
	cases := []struct {
		name string
		body []byte
	}{
		{"empty", []byte{}},
		{"small", []byte(`{"cmd":"sync_clock","epoch_ns":1}`)},
		{"nul-and-newline", []byte("a\x00b\nc")},
		{"at-max", bytes.Repeat([]byte("x"), maxFrameBytes)},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			var buf bytes.Buffer
			if err := writeFrame(&buf, tc.body); err != nil {
				t.Fatalf("writeFrame: %v", err)
			}
			got, err := readFrame(&buf)
			if err != nil {
				t.Fatalf("readFrame: %v", err)
			}
			if !bytes.Equal(got, tc.body) {
				t.Fatalf("round-trip mismatch: got %q want %q", got, tc.body)
			}
		})
	}
}

// oneByteReader returns its wrapped bytes one byte per Read call, forcing
// io.ReadFull to reassemble both the 4-byte header and the body from partial
// reads (the vsock stream can split a frame arbitrarily).
type oneByteReader struct {
	data []byte
	pos  int
}

func (r *oneByteReader) Read(p []byte) (int, error) {
	if r.pos >= len(r.data) {
		return 0, io.EOF
	}
	p[0] = r.data[r.pos]
	r.pos++
	return 1, nil
}

// TestReadFramePartialReads proves readFrame reassembles a frame delivered one
// byte at a time (the header and body both arriving in fragments).
func TestReadFramePartialReads(t *testing.T) {
	body := []byte(`{"cmd":"sync_clock","epoch_ns":42}`)
	var buf bytes.Buffer
	if err := writeFrame(&buf, body); err != nil {
		t.Fatalf("writeFrame: %v", err)
	}
	got, err := readFrame(&oneByteReader{data: buf.Bytes()})
	if err != nil {
		t.Fatalf("readFrame partial: %v", err)
	}
	if !bytes.Equal(got, body) {
		t.Fatalf("partial read mismatch: got %q want %q", got, body)
	}
}

// TestReadFrameOversizedGuard proves a length prefix over maxFrameBytes is
// rejected with ErrFrameTooLarge BEFORE the (huge) body is read or allocated:
// the reader carries only the 4-byte header, so servicing the length would
// block/OOM; ErrFrameTooLarge must come back instead.
func TestReadFrameOversizedGuard(t *testing.T) {
	var hdr [4]byte
	binary.BigEndian.PutUint32(hdr[:], maxFrameBytes+1)
	_, err := readFrame(bytes.NewReader(hdr[:]))
	if !errors.Is(err, ErrFrameTooLarge) {
		t.Fatalf("expected ErrFrameTooLarge, got %v", err)
	}
}

// TestWriteFrameOversizedGuard proves writeFrame refuses a body over the max
// rather than emitting a frame the peer would reject.
func TestWriteFrameOversizedGuard(t *testing.T) {
	err := writeFrame(&bytes.Buffer{}, bytes.Repeat([]byte("x"), maxFrameBytes+1))
	if !errors.Is(err, ErrFrameTooLarge) {
		t.Fatalf("expected ErrFrameTooLarge, got %v", err)
	}
}

// TestReadFrameTruncatedBody proves a header promising N bytes but a stream
// carrying fewer surfaces io.ErrUnexpectedEOF (the codec never returns a short
// body silently).
func TestReadFrameTruncatedBody(t *testing.T) {
	var hdr [4]byte
	binary.BigEndian.PutUint32(hdr[:], 8)
	stream := append(hdr[:], []byte("abc")...) // promises 8, carries 3
	_, err := readFrame(bytes.NewReader(stream))
	if !errors.Is(err, io.ErrUnexpectedEOF) {
		t.Fatalf("expected io.ErrUnexpectedEOF, got %v", err)
	}
}
