package server

import (
	"encoding/binary"
	"encoding/hex"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
)

const (
	ext4MagicOffset = 1024 + 0x38
	ext4UUIDOffset  = 1024 + 0x68
	ext4HeaderSize  = 0x48c
)

// ext4UUID reads the filesystem UUID from an ext4 superblock. The UUID is the
// identity used to derive the metadata checksum seed, so hashing the rootfs file
// would not provide the identity a restored guest kernel has cached.
func ext4UUID(path string) (string, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", fmt.Errorf("open ext4 rootfs %q: %w", path, err)
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil {
		return "", fmt.Errorf("stat ext4 rootfs %q: %w", path, err)
	}
	if info.Size() < ext4HeaderSize {
		return "", fmt.Errorf("read ext4 UUID from %q: file is %d bytes, need at least %d: %w", path, info.Size(), ext4HeaderSize, io.ErrUnexpectedEOF)
	}

	magic := make([]byte, 2)
	if _, err := f.ReadAt(magic, ext4MagicOffset); err != nil {
		if err == io.EOF {
			err = io.ErrUnexpectedEOF
		}
		return "", fmt.Errorf("read ext4 magic from %q: %w", path, err)
	}
	if got := binary.LittleEndian.Uint16(magic); got != 0xEF53 {
		return "", fmt.Errorf("read ext4 UUID from %q: bad ext4 magic 0x%04x", path, got)
	}

	uuid := make([]byte, 16)
	if _, err := f.ReadAt(uuid, ext4UUIDOffset); err != nil {
		if err == io.EOF {
			err = io.ErrUnexpectedEOF
		}
		return "", fmt.Errorf("read ext4 UUID from %q: %w", path, err)
	}
	hexUUID := hex.EncodeToString(uuid)
	return fmt.Sprintf("%s-%s-%s-%s-%s", hexUUID[:8], hexUUID[8:12], hexUUID[12:16], hexUUID[16:20], hexUUID[20:]), nil
}

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
	actual, err := ext4UUID(rootfsPath)
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
