// Per-file encryption uses a framed AES-256-GCM stream. Plaintext is split into
// 64 KiB chunks. Each frame is:
//
//	final_flag::uint8 || ciphertext_len::uint32 BE || ciphertext
//
// ciphertext includes GCM's 16-byte tag. The nonce for chunk N is the 12-byte
// base nonce with N XORed into its low 64 bits. AAD is "embervm-file-v1"
// followed by chunk_index::uint64 BE and final_flag::uint8. The writer retains
// one chunk until it knows whether that chunk is final, and emits one final
// frame even for an empty file. The reader authenticates a complete frame and,
// for the final frame, verifies that no trailing bytes exist before exposing
// its plaintext.
package store

import (
	"bufio"
	"crypto/aes"
	"crypto/cipher"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
)

const (
	fileChunkSize  = 64 << 10
	fileAADLabel   = "embervm-file-v1"
	frameHeaderLen = 5
)

type sealWriter struct {
	w      io.Writer
	aead   cipher.AEAD
	nonce  []byte
	buf    []byte
	index  uint64
	closed bool
	err    error
}

func newSealWriter(w io.Writer, key, baseNonce []byte) io.WriteCloser {
	aead, err := newFileAEAD(key, baseNonce)
	if err != nil {
		return &errorWriteCloser{err: err}
	}
	return &sealWriter{
		w:     w,
		aead:  aead,
		nonce: append([]byte(nil), baseNonce...),
		buf:   make([]byte, 0, fileChunkSize),
	}
}

func (w *sealWriter) Write(p []byte) (int, error) {
	if w.closed {
		return 0, errors.New("store: write to closed seal writer")
	}
	if w.err != nil {
		return 0, w.err
	}
	written := 0
	for len(p) > 0 {
		if len(w.buf) == fileChunkSize {
			if err := w.writeFrame(false); err != nil {
				w.err = err
				return written, err
			}
		}
		n := min(len(p), fileChunkSize-len(w.buf))
		w.buf = append(w.buf, p[:n]...)
		p = p[n:]
		written += n
	}
	return written, nil
}

func (w *sealWriter) Close() error {
	if w.closed {
		return w.err
	}
	w.closed = true
	if w.err != nil {
		return w.err
	}
	w.err = w.writeFrame(true)
	return w.err
}

func (w *sealWriter) writeFrame(final bool) error {
	flag := byte(0)
	if final {
		flag = 1
	}
	sealed := w.aead.Seal(nil, chunkNonce(w.nonce, w.index), w.buf, chunkAAD(w.index, flag))
	var header [frameHeaderLen]byte
	header[0] = flag
	binary.BigEndian.PutUint32(header[1:], uint32(len(sealed)))
	if err := writeAll(w.w, header[:]); err != nil {
		return fmt.Errorf("store: write encrypted frame header: %w", err)
	}
	if err := writeAll(w.w, sealed); err != nil {
		return fmt.Errorf("store: write encrypted frame body: %w", err)
	}
	w.buf = w.buf[:0]
	w.index++
	return nil
}

type openReader struct {
	r     *bufio.Reader
	aead  cipher.AEAD
	nonce []byte
	index uint64
	plain []byte
	final bool
	err   error
}

func newOpenReader(r io.Reader, key, baseNonce []byte) io.Reader {
	aead, err := newFileAEAD(key, baseNonce)
	if err != nil {
		return &errorReader{err: err}
	}
	return &openReader{r: bufio.NewReader(r), aead: aead, nonce: append([]byte(nil), baseNonce...)}
}

func (r *openReader) Read(p []byte) (int, error) {
	if len(p) == 0 {
		return 0, nil
	}
	for len(r.plain) == 0 && r.err == nil {
		if r.final {
			r.err = io.EOF
			break
		}
		r.readFrame()
	}
	if len(r.plain) > 0 {
		n := copy(p, r.plain)
		r.plain = r.plain[n:]
		return n, nil
	}
	return 0, r.err
}

func (r *openReader) readFrame() {
	var header [frameHeaderLen]byte
	if _, err := io.ReadFull(r.r, header[:]); err != nil {
		if errors.Is(err, io.EOF) {
			r.err = errors.New("store: encrypted stream missing final frame")
		} else {
			r.err = fmt.Errorf("store: read encrypted frame header: %w", err)
		}
		return
	}
	flag := header[0]
	if flag > 1 {
		r.err = fmt.Errorf("store: invalid encrypted frame final flag %d", flag)
		return
	}
	sealedLen := binary.BigEndian.Uint32(header[1:])
	if sealedLen < uint32(r.aead.Overhead()) || sealedLen > uint32(fileChunkSize+r.aead.Overhead()) {
		r.err = fmt.Errorf("store: invalid encrypted frame length %d", sealedLen)
		return
	}
	sealed := make([]byte, sealedLen)
	if _, err := io.ReadFull(r.r, sealed); err != nil {
		r.err = fmt.Errorf("store: read encrypted frame body: %w", err)
		return
	}
	plain, err := r.aead.Open(nil, chunkNonce(r.nonce, r.index), sealed, chunkAAD(r.index, flag))
	if err != nil {
		r.err = fmt.Errorf("store: authenticate encrypted chunk %d: %w", r.index, err)
		return
	}
	if flag == 0 && len(plain) != fileChunkSize {
		r.err = fmt.Errorf("store: non-final encrypted chunk %d has plaintext length %d", r.index, len(plain))
		return
	}
	if flag == 1 {
		if _, err := r.r.Peek(1); !errors.Is(err, io.EOF) {
			if err == nil {
				r.err = errors.New("store: trailing bytes after final encrypted frame")
			} else {
				r.err = fmt.Errorf("store: check encrypted stream trailer: %w", err)
			}
			return
		}
		r.final = true
	}
	r.index++
	r.plain = plain
}

func newFileAEAD(key, baseNonce []byte) (cipher.AEAD, error) {
	if len(key) != 32 {
		return nil, fmt.Errorf("store: AES-256-GCM key length %d, want 32", len(key))
	}
	if len(baseNonce) != 12 {
		return nil, fmt.Errorf("store: AES-256-GCM base nonce length %d, want 12", len(baseNonce))
	}
	block, err := aes.NewCipher(key)
	if err != nil {
		return nil, fmt.Errorf("store: create AES cipher: %w", err)
	}
	aead, err := cipher.NewGCM(block)
	if err != nil {
		return nil, fmt.Errorf("store: create GCM: %w", err)
	}
	return aead, nil
}

func chunkNonce(base []byte, index uint64) []byte {
	nonce := append([]byte(nil), base...)
	for i := 0; i < 8; i++ {
		nonce[len(nonce)-1-i] ^= byte(index >> (8 * i))
	}
	return nonce
}

func chunkAAD(index uint64, flag byte) []byte {
	aad := make([]byte, len(fileAADLabel)+8+1)
	copy(aad, fileAADLabel)
	binary.BigEndian.PutUint64(aad[len(fileAADLabel):], index)
	aad[len(aad)-1] = flag
	return aad
}

func writeAll(w io.Writer, p []byte) error {
	for len(p) > 0 {
		n, err := w.Write(p)
		if err != nil {
			return err
		}
		if n == 0 {
			return io.ErrShortWrite
		}
		p = p[n:]
	}
	return nil
}

type errorReader struct{ err error }

func (r *errorReader) Read([]byte) (int, error) { return 0, r.err }

type errorWriteCloser struct{ err error }

func (w *errorWriteCloser) Write([]byte) (int, error) { return 0, w.err }
func (w *errorWriteCloser) Close() error              { return w.err }
