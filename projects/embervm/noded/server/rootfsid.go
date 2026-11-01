package server

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/jomcgi/homelab/projects/embervm/noded/fcvm/driver"
)

// RootfsMismatch describes why a base bundle's recorded rootfs identity does
// not match the filesystem currently present at its backing path.
type RootfsMismatch struct {
	Mismatch bool
	Actual   string // The actual UUID on the rootfs, or "" if unavailable.
	Reason   string // A description for failures other than a UUID mismatch.
}

// baseRootfsMatches verifies that a base bundle was captured against the ext4
// filesystem currently present at rootfsPath.
func baseRootfsMatches(dir, rootfsPath string) (bool, *RootfsMismatch) {
	recorded, err := os.ReadFile(filepath.Join(dir, "rootfsid"))
	if err != nil {
		return false, &RootfsMismatch{Reason: fmt.Sprintf("read rootfsid: %v", err)}
	}
	expected := strings.TrimSpace(string(recorded))
	if expected == "" {
		return false, &RootfsMismatch{Reason: "rootfsid is empty"}
	}
	actual, err := driver.Ext4UUID(rootfsPath)
	if err != nil {
		return false, &RootfsMismatch{Reason: err.Error()}
	}
	if expected != actual {
		return false, &RootfsMismatch{Mismatch: true, Actual: actual}
	}
	return true, nil
}

func rootfsMismatchDescription(mismatch *RootfsMismatch) string {
	if mismatch == nil {
		return "unknown rootfs identity failure"
	}
	if mismatch.Mismatch {
		return fmt.Sprintf("rootfs UUID mismatch, actual %s", mismatch.Actual)
	}
	return mismatch.Reason
}
