package store

import (
	"bytes"
	"encoding/binary"
	"io"
	"testing"
)

func sealForTest(t *testing.T, plaintext, key, nonce []byte) []byte {
	t.Helper()
	var sealed bytes.Buffer
	w := newSealWriter(&sealed, key, nonce)
	if _, err := w.Write(plaintext); err != nil {
		t.Fatalf("seal Write: %v", err)
	}
	if err := w.Close(); err != nil {
		t.Fatalf("seal Close: %v", err)
	}
	return sealed.Bytes()
}

func TestAEADRoundTrip(t *testing.T) {
	key := bytes.Repeat([]byte{0x23}, 32)
	nonce := bytes.Repeat([]byte{0x45}, 12)
	tests := []struct {
		name string
		size int
	}{
		{name: "empty", size: 0},
		{name: "less_than_one_chunk", size: fileChunkSize - 17},
		{name: "exactly_one_chunk", size: fileChunkSize},
		{name: "three_and_a_half_chunks", size: 3*fileChunkSize + fileChunkSize/2},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			plaintext := makePattern(tt.size)
			sealed := sealForTest(t, plaintext, key, nonce)
			opened, err := io.ReadAll(newOpenReader(bytes.NewReader(sealed), key, nonce))
			if err != nil {
				t.Fatalf("open: %v", err)
			}
			if !bytes.Equal(opened, plaintext) {
				t.Fatalf("round trip changed plaintext: got %d bytes, want %d", len(opened), len(plaintext))
			}
		})
	}
}

func TestAEADDamageFailsAtAuthenticatedBoundary(t *testing.T) {
	key := bytes.Repeat([]byte{0x67}, 32)
	wrongKey := bytes.Repeat([]byte{0x68}, 32)
	nonce := bytes.Repeat([]byte{0x89}, 12)
	plaintext := makePattern(3*fileChunkSize + fileChunkSize/2)
	sealed := sealForTest(t, plaintext, key, nonce)
	frames := splitFrames(t, sealed)

	truncated := append([]byte(nil), sealed[:len(sealed)-9]...)
	bitFlipped := append([]byte(nil), sealed...)
	secondBody := len(frames[0]) + frameHeaderLen
	bitFlipped[secondBody+11] ^= 0x80
	reordered := joinFrames(frames[1], frames[0], frames[2], frames[3])

	tests := []struct {
		name       string
		ciphertext []byte
		openKey    []byte
		wantMax    int
	}{
		{name: "truncation", ciphertext: truncated, openKey: key, wantMax: 3 * fileChunkSize},
		{name: "bit_flip", ciphertext: bitFlipped, openKey: key, wantMax: fileChunkSize},
		{name: "chunk_reorder", ciphertext: reordered, openKey: key, wantMax: 0},
		{name: "wrong_key", ciphertext: sealed, openKey: wrongKey, wantMax: 0},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			opened, err := io.ReadAll(newOpenReader(bytes.NewReader(tt.ciphertext), tt.openKey, nonce))
			if err == nil {
				t.Fatal("damaged ciphertext reached EOF without an authentication error")
			}
			if len(opened) > tt.wantMax {
				t.Fatalf("reader emitted %d bytes, want no plaintext beyond boundary %d", len(opened), tt.wantMax)
			}
			if !bytes.Equal(opened, plaintext[:len(opened)]) {
				t.Fatal("reader emitted unauthenticated plaintext")
			}
		})
	}
}

func makePattern(size int) []byte {
	b := make([]byte, size)
	for i := range b {
		b[i] = byte((i*31 + 7) % 251)
	}
	return b
}

func splitFrames(t *testing.T, sealed []byte) [][]byte {
	t.Helper()
	var frames [][]byte
	for len(sealed) > 0 {
		if len(sealed) < frameHeaderLen {
			t.Fatalf("short frame header: %d bytes", len(sealed))
		}
		n := frameHeaderLen + int(binary.BigEndian.Uint32(sealed[1:5]))
		if n > len(sealed) {
			t.Fatalf("short frame body: frame wants %d, have %d", n, len(sealed))
		}
		frames = append(frames, append([]byte(nil), sealed[:n]...))
		sealed = sealed[n:]
	}
	return frames
}

func joinFrames(frames ...[]byte) []byte {
	return bytes.Join(frames, nil)
}
