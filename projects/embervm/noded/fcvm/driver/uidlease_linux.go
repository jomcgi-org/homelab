//go:build linux

package driver

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"

	"golang.org/x/sys/unix"
)

// uidBlockLease is this daemon's exclusive block of jail uids. Bricks that share
// a node share its uid space, but each daemon numbers its launches from zero, so
// without a claim two bricks hand the same uid to concurrently live VMMs and the
// per-VM uid stops separating them. The lease is an flock on a lock file in the
// release's snapshot root, held by an open descriptor for the daemon's lifetime.
// Every brick of one release shares that root, so the flock keeps them apart.
// Another release on the same node (embervm-dev nests its scratch, and so its
// snapshot root, under production's) sees different lock files, so releases are
// kept apart by leasing from disjoint block ranges (ExecLauncher.UIDBlockBase). The kernel drops it when noded dies, and
// the VMs die with noded because noded is its container's PID 1 and every VMM
// runs in that PID namespace or one nested below it, so a block reclaimed by a
// sibling never has a live holder.
type uidBlockLease struct {
	file *os.File
	base int
}

// uidLeaseLockName names the lock file of block i under the lease directory.
func uidLeaseLockName(i int) string {
	return fmt.Sprintf("jail-uid-block-%02d.lock", i)
}

// leaseUIDBlock claims the first free block under dir, scanning from firstBlock
// up, so a release leasing from its own base never takes a block below it. The
// lock files are inert plain files beside the snapshot root's directories;
// every scanner there walks directories only. A full or unusable directory, or a
// base outside the node's blocks, fails closed: a jailed launch never runs with
// an unleased uid.
func leaseUIDBlock(dir string, firstBlock int) (*uidBlockLease, error) {
	if dir == "" {
		return nil, errors.New("driver: jailer uid lease directory is unset")
	}
	if firstBlock < 0 || firstBlock >= JailUIDBlocks {
		return nil, fmt.Errorf("driver: jailer uid block base %d is outside the node's %d blocks", firstBlock, JailUIDBlocks)
	}
	if err := os.MkdirAll(dir, 0o750); err != nil {
		return nil, fmt.Errorf("driver: create jailer uid lease directory: %w", err)
	}
	for i := firstBlock; i < JailUIDBlocks; i++ {
		path := filepath.Join(dir, uidLeaseLockName(i))
		f, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR, 0o600)
		if err != nil {
			return nil, fmt.Errorf("driver: open jailer uid lease %q: %w", path, err)
		}
		if err := unix.Flock(int(f.Fd()), unix.LOCK_EX|unix.LOCK_NB); err != nil {
			_ = f.Close()
			if errors.Is(err, unix.EWOULDBLOCK) || errors.Is(err, unix.EAGAIN) {
				continue
			}
			return nil, fmt.Errorf("driver: lock jailer uid lease %q: %w", path, err)
		}
		return &uidBlockLease{file: f, base: vmUIDBase + i*vmUIDBlockSize}, nil
	}
	return nil, fmt.Errorf("driver: every jailer uid block %d-%d under %q is leased (%d daemons of this release share this node)", firstBlock, JailUIDBlocks-1, dir, JailUIDBlocks-firstBlock)
}

// release drops the lease. Production never calls it: the lease lives as long
// as the daemon. Tests use it to model a sibling daemon exiting.
func (l *uidBlockLease) release() error {
	if l == nil || l.file == nil {
		return nil
	}
	return l.file.Close()
}
