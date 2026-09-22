//go:build !linux

package driver

import (
	"fmt"
	"io"
	"os"
)

// cloneOrCopyFile keeps non-Linux builds correct without claiming reflink
// support. Production noded runs on Linux and uses reflink_linux.go.
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
	if _, err = io.Copy(out, in); err != nil {
		return fmt.Errorf("copy file: %w", err)
	}
	return nil
}
