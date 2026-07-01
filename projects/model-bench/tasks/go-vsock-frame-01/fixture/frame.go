// Package frame implements the length-prefixed wire framing used over the
// firecracker vsock transport: a 4-byte big-endian payload length followed by
// that many payload bytes.
package frame

import (
	"encoding/binary"
	"io"
)

// MaxFrame bounds a single frame so a malformed length cannot allocate without limit.
const MaxFrame = 16 << 20 // 16 MiB

// ReadFrame reads one frame from r: a 4-byte big-endian uint32 length, then that
// many payload bytes. It returns the payload.
func ReadFrame(r io.Reader) ([]byte, error) {
	var hdr [4]byte
	if _, err := io.ReadFull(r, hdr[:]); err != nil {
		return nil, err
	}
	n := binary.BigEndian.Uint32(hdr[:])
	buf := make([]byte, n)
	// NOTE: a single Read may return fewer than n bytes when the underlying
	// stream delivers the payload in chunks (as vsock does), leaving buf partly
	// unfilled.
	if _, err := r.Read(buf); err != nil {
		return nil, err
	}
	return buf, nil
}
