//go:build unix

package volume

import (
	"io/fs"
	"syscall"
)

// statBlocks extracts the POSIX st_blocks (512-byte sector count) field from a
// FileInfo's platform Sys(), used by AllocatedBytes to report real block usage
// on a sparse file rather than its logical size. Present on every unix build
// target (linux for the daemon image, darwin for local dev/tests).
func statBlocks(fi fs.FileInfo) (int64, bool) {
	st, ok := fi.Sys().(*syscall.Stat_t)
	if !ok {
		return 0, false
	}
	return int64(st.Blocks), true
}
