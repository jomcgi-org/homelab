package driver

import (
	"context"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"time"
)

// RootfsProvisioner gives each thread its own writable rootfs derived from a
// read-only base image, so threads never share or corrupt one disk. The only
// production impl is a file copy ("full snapshots first"); a copy-on-write impl
// would sit behind the same interface.
type RootfsProvisioner interface {
	// Provision creates the thread's rootfs under dir and returns its host path.
	Provision(ctx context.Context, threadID, dir string) (string, error)
	// Teardown releases any out-of-dir resources the provisioner created for the
	// thread (a CoW provisioner's device and pool allocation, say), called when
	// the thread's bundle is reclaimed. It must be idempotent: reclaim may run
	// more than once and the resource may already be gone. A file-copy
	// provisioner has nothing to release (RemoveBundle's RemoveAll deletes the
	// file), so its Teardown is a no-op.
	Teardown(ctx context.Context, threadID string) error
}

// CopyProvisioner copies a base rootfs image to a per-thread file. Simple and
// correct; the cost is a full copy + full disk per thread.
type CopyProvisioner struct {
	// Base is the read-only base rootfs image (a flattened harness image).
	Base string
}

// Provision copies Base to dir/rootfs.ext4.
func (p *CopyProvisioner) Provision(_ context.Context, _, dir string) (string, error) {
	if p.Base == "" {
		return "", fmt.Errorf("driver: CopyProvisioner.Base is empty")
	}
	dst := filepath.Join(dir, "rootfs.ext4")
	if err := copyFile(p.Base, dst); err != nil {
		return "", fmt.Errorf("driver: provision rootfs: %w", err)
	}
	return dst, nil
}

// Teardown is a no-op: the per-thread rootfs file lives in the bundle dir, which
// RemoveBundle deletes wholesale.
func (p *CopyProvisioner) Teardown(_ context.Context, _ string) error { return nil }

func copyFile(src, dst string) error {
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()

	out, err := os.OpenFile(dst, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, 0o600)
	if err != nil {
		return err
	}
	if _, err := io.Copy(out, in); err != nil {
		out.Close()
		return err
	}
	return out.Close()
}

// teardownTimeout bounds a single provisioner Teardown so a wedged release
// cannot stall the reconcile loop indefinitely.
const teardownTimeout = 30 * time.Second
