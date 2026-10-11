package driver

import (
	"bufio"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
)

// orphanJailDirName is the per-bundle directory prepareJail roots every jail
// under (<bundle>/jailer/<exec>/<vm>/root).
const orphanJailDirName = "jailer"

// procSelfMountinfoPath lists the mounts the sweep must detach before removing
// a jail tree. A package var so tests can point it at a fixture.
var procSelfMountinfoPath = "/proc/self/mountinfo"

// unmountJailPath detaches one bind-mount fallback left inside a jail tree.
var unmountJailPath = unmountFile

// SweepOrphanJails removes the jail trees a crashed daemon left under its own
// warmth root. A jail holds hard links to kernels, rootfs images, base and
// session snapshot files and a copy of the Firecracker binary, so an orphaned
// one pins those inodes after the named files are evicted. Only
// <warmthRoot>/<bundle>/jailer is removed, never the bundle itself: a thread
// dir can also hold a durable snapshot. The sweep reads one level and never
// recurses, so a flat legacy root never reaches a sibling brick's warmth below
// i/. It must run before this daemon launches anything: correctness rests on
// noded being its container's PID 1, so no VMM survives the daemon that owned
// it and no jail under this root has a live owner at startup. A symlinked
// jailer entry is never followed. removeErr reports each tree that could not be
// removed; the removed trees are returned.
func SweepOrphanJails(warmthRoot string, removeErr func(path string, err error)) []string {
	if warmthRoot == "" {
		return nil
	}
	entries, err := os.ReadDir(warmthRoot)
	if err != nil {
		if !os.IsNotExist(err) && removeErr != nil {
			removeErr(warmthRoot, err)
		}
		return nil
	}
	var removed []string
	for _, entry := range entries {
		if !entry.IsDir() {
			continue
		}
		tree := filepath.Join(warmthRoot, entry.Name(), orphanJailDirName)
		info, err := os.Lstat(tree)
		if err != nil || !info.IsDir() {
			continue
		}
		if err := removeJailTree(tree); err != nil {
			if removeErr != nil {
				removeErr(tree, err)
			}
			continue
		}
		removed = append(removed, tree)
	}
	return removed
}

// removeJailTree detaches every mount at or below root, deepest first, then
// removes root. Bind mounts are the jail's fallback when a hard link fails;
// RemoveAll would otherwise stop at them with EBUSY or descend into the host
// file a mount exposes.
func removeJailTree(root string) error {
	mounts, err := mountsBelow(root)
	if err != nil && !os.IsNotExist(err) {
		return fmt.Errorf("read mount table: %w", err)
	}
	var unmountErr error
	for _, target := range mounts {
		if err := unmountJailPath(target); err != nil {
			unmountErr = errors.Join(unmountErr, fmt.Errorf("unmount %q: %w", target, err))
		}
	}
	if unmountErr != nil {
		return unmountErr
	}
	return os.RemoveAll(root)
}

// mountsBelow returns the mount points at or below root, deepest first.
func mountsBelow(root string) ([]string, error) {
	f, err := os.Open(procSelfMountinfoPath)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	root = filepath.Clean(root)
	var mounts []string
	s := bufio.NewScanner(f)
	for s.Scan() {
		fields := strings.Fields(s.Text())
		if len(fields) < 5 {
			continue
		}
		target := filepath.Clean(unescapeMountPath(fields[4]))
		if target == root || strings.HasPrefix(target, root+string(filepath.Separator)) {
			mounts = append(mounts, target)
		}
	}
	if err := s.Err(); err != nil {
		return nil, err
	}
	sort.SliceStable(mounts, func(i, j int) bool { return len(mounts[i]) > len(mounts[j]) })
	return mounts, nil
}

// unescapeMountPath decodes the octal escapes (\040 for a space and so on) the
// kernel writes into /proc/self/mountinfo paths.
func unescapeMountPath(field string) string {
	if !strings.Contains(field, `\`) {
		return field
	}
	var b strings.Builder
	for i := 0; i < len(field); i++ {
		if field[i] == '\\' && i+3 < len(field) {
			if v, err := strconv.ParseUint(field[i+1:i+4], 8, 8); err == nil {
				b.WriteByte(byte(v))
				i += 3
				continue
			}
		}
		b.WriteByte(field[i])
	}
	return b.String()
}
