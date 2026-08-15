//go:build linux

package sparse

import (
	"fmt"
	"io"
	"os"

	"golang.org/x/sys/unix"
)

const (
	holeBlockSize = int64(4096)
	holeChunkSize = int64(1 << 20)
)

// DigHoles deallocates all-zero regions of the file at path, leaving its
// logical size and its contents-as-read unchanged. It returns the number of
// bytes requested for deallocation.
func DigHoles(path string) (freed int64, err error) {
	f, err := os.OpenFile(path, os.O_RDWR, 0)
	if err != nil {
		return 0, err
	}
	defer func() {
		if closeErr := f.Close(); err == nil && closeErr != nil {
			err = closeErr
		}
	}()

	st, err := f.Stat()
	if err != nil {
		return 0, err
	}
	var runStart, runLength int64
	punchRun := func() error {
		if runLength == 0 {
			return nil
		}
		if err := unix.Fallocate(int(f.Fd()), unix.FALLOC_FL_PUNCH_HOLE|unix.FALLOC_FL_KEEP_SIZE, runStart, runLength); err != nil {
			return err
		}
		freed += runLength
		runStart, runLength = 0, 0
		return nil
	}

	buf := make([]byte, holeChunkSize)
	for offset := int64(0); offset+holeBlockSize <= st.Size(); {
		readLength := holeChunkSize
		if remaining := st.Size() - offset; remaining < readLength {
			readLength = remaining
		}
		n, readErr := f.ReadAt(buf[:readLength], offset)
		if readErr != nil && readErr != io.EOF {
			return freed, readErr
		}
		for blockOffset := int64(0); blockOffset+holeBlockSize <= int64(n); blockOffset += holeBlockSize {
			block := buf[blockOffset : blockOffset+holeBlockSize]
			zero := true
			for _, b := range block {
				if b != 0 {
					zero = false
					break
				}
			}
			if zero {
				if runLength == 0 {
					runStart = offset + blockOffset
				}
				runLength += holeBlockSize
			} else if err := punchRun(); err != nil {
				return freed, fmt.Errorf("punch zero run at offset %d: %w", runStart, err)
			}
		}
		offset += int64(n)
		if n == 0 {
			break
		}
	}
	if err := punchRun(); err != nil {
		return freed, fmt.Errorf("punch zero run at offset %d: %w", runStart, err)
	}
	return freed, nil
}
