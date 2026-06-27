package driver

import (
	"context"
	"fmt"
	"io"
	"os"
	"path/filepath"
)

// RootfsProvisioner gives each thread its own writable rootfs derived from a
// read-only base image, so threads never share or corrupt one disk. The default
// impl is a file copy ("full snapshots first"); a devmapper thin-COW impl is a
// future efficiency follow-up behind the same interface.
type RootfsProvisioner interface {
	// Provision creates the thread's rootfs under dir and returns its host path.
	Provision(ctx context.Context, threadID, dir string) (string, error)
}

// CopyProvisioner copies a base rootfs image to a per-thread file. Simple and
// correct; the cost is a full copy + full disk per thread, which devmapper
// thin-COW will later replace.
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
