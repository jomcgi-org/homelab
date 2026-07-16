//go:build !unix

package volume

import "io/fs"

// statBlocks has no portable equivalent off unix; AllocatedBytes falls back to
// the file's logical size on such a build.
func statBlocks(fs.FileInfo) (int64, bool) {
	return 0, false
}
