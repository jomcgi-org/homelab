//go:build linux

package main

import (
	"fmt"
	"log/slog"
	"os"
	"os/exec"

	"golang.org/x/sys/unix"
)

// mountVolumeDevice formats dev with ext4 if it has no existing filesystem
// signature, then mounts it at mountPath. It shells out to blkid/mkfs.ext4
// (rather than parsing the superblock in Go) so the guest image's own
// coreutils/e2fsprogs own the filesystem-format logic exactly once, matching
// how this init already shells out to nothing else but keeping the mkfs
// decision in one well-tested external tool rather than reimplementing ext4
// probing here. A future task ensures these binaries are present in the
// stateful runtime image (blkid/mkfs.ext4/mount); an absent binary surfaces as
// a normal exec error, which this function propagates (fatal boot failure, per
// mountStatefulVolume's fail-closed contract).
func mountVolumeDevice(logger *slog.Logger, dev, mountPath string) error {
	blank, err := deviceIsBlank(dev)
	if err != nil {
		return fmt.Errorf("probe volume device %s: %w", dev, err)
	}
	if blank {
		logger.Info("stateful volume: no filesystem signature, formatting ext4", "device", dev)
		if out, err := exec.Command("mkfs.ext4", "-q", dev).CombinedOutput(); err != nil {
			return fmt.Errorf("mkfs.ext4 %s: %w: %s", dev, err, string(out))
		}
	}
	if err := os.MkdirAll(mountPath, 0o755); err != nil {
		return fmt.Errorf("mkdir volume mount path %s: %w", mountPath, err)
	}
	if err := unix.Mount(dev, mountPath, "ext4", 0, ""); err != nil {
		return fmt.Errorf("mount %s at %s: %w", dev, mountPath, err)
	}
	logger.Info("stateful volume: mounted", "device", dev, "mount", mountPath)
	return nil
}

// deviceIsBlank reports whether dev has NO existing filesystem signature
// (blkid finds nothing), meaning this is a freshly created sparse volume that
// needs formatting on first boot. blkid exits non-zero with empty output when
// a device has no recognised signature; any OTHER error (device missing,
// blkid not found) is propagated distinctly so a real problem is not
// misread as "blank, go ahead and mkfs" (which would destroy an existing
// filesystem blkid merely failed to identify for some other reason).
func deviceIsBlank(dev string) (bool, error) {
	out, err := exec.Command("blkid", "-o", "value", "-s", "TYPE", dev).CombinedOutput()
	if err == nil {
		// blkid succeeded: a non-empty TYPE line means a filesystem was found.
		return len(trimTrailingNewline(out)) == 0, nil
	}
	if exitErr, ok := err.(*exec.ExitError); ok {
		// blkid's documented exit status 2 means "no recognisable signature
		// found", which is the blank-device case this function exists to detect,
		// not an error.
		if exitErr.ExitCode() == 2 {
			return true, nil
		}
	}
	return false, fmt.Errorf("blkid %s: %w: %s", dev, err, string(out))
}

// trimTrailingNewline strips a single trailing newline blkid's output carries.
func trimTrailingNewline(b []byte) []byte {
	if n := len(b); n > 0 && b[n-1] == '\n' {
		return b[:n-1]
	}
	return b
}
