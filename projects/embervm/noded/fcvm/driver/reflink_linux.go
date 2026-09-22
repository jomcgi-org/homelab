package driver

import (
	"errors"
	"fmt"
	"io"
	"os"

	"golang.org/x/sys/unix"
)

// reflinkFile is a seam for hermetic tests. Production issues FICLONE with the
// destination descriptor first, as required by ioctl_ficlonerange(2).
var reflinkFile = func(dst, src *os.File) error {
	return unix.IoctlFileClone(int(dst.Fd()), int(src.Fd()))
}

// cloneOrCopyFile creates dst from src. On reflink-capable Linux filesystems it
// is a metadata-only clone. EOPNOTSUPP means the filesystem lacks clone support,
// while EXDEV means the paths cannot share extents across filesystems; those two
// capability errors fall back to a byte copy. All other ioctl errors are real
// failures and are returned without hiding permission or I/O faults.
//
// dst must not exist. Requiring an absent destination prevents truncating an
// existing file, including src itself. Any failed attempt removes only the new
// destination and never changes src.
func cloneOrCopyFile(src, dst string) (err error) {
	created := false
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer func() {
		if closeErr := in.Close(); err == nil && closeErr != nil {
			err = closeErr
		}
		if err != nil && created {
			_ = os.Remove(dst)
		}
	}()

	srcInfo, err := in.Stat()
	if err != nil {
		return err
	}
	if dstInfo, statErr := os.Stat(dst); statErr == nil {
		if os.SameFile(srcInfo, dstInfo) {
			return fmt.Errorf("source and destination are the same file: %w", os.ErrExist)
		}
		return fmt.Errorf("destination already exists: %w", os.ErrExist)
	} else if !errors.Is(statErr, os.ErrNotExist) {
		return statErr
	}

	out, err := os.OpenFile(dst, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o600)
	if err != nil {
		return err
	}
	created = true
	defer func() {
		if closeErr := out.Close(); err == nil && closeErr != nil {
			err = closeErr
		}
	}()

	err = reflinkFile(out, in)
	if err == nil {
		return nil
	}
	if !errors.Is(err, unix.EOPNOTSUPP) && !errors.Is(err, unix.EXDEV) {
		return fmt.Errorf("FICLONE %q from %q: %w", dst, src, err)
	}

	// Do not rely on a failed ioctl leaving offsets or destination length
	// unchanged. Reset all three before the fallback byte copy.
	if _, err = in.Seek(0, io.SeekStart); err != nil {
		return fmt.Errorf("rewind source after unsupported FICLONE: %w", err)
	}
	if err = out.Truncate(0); err != nil {
		return fmt.Errorf("truncate destination after unsupported FICLONE: %w", err)
	}
	if _, err = out.Seek(0, io.SeekStart); err != nil {
		return fmt.Errorf("rewind destination after unsupported FICLONE: %w", err)
	}
	if _, err = io.Copy(out, in); err != nil {
		return fmt.Errorf("copy after unsupported FICLONE: %w", err)
	}
	return nil
}
