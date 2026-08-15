//go:build linux

package store

import (
	"bytes"
	"context"
	"os"
	"path/filepath"
	"syscall"
	"testing"
)

func TestRestorePunchesHoles(t *testing.T) {
	const logicalSize = 8 << 20
	payload := make([]byte, logicalSize)
	payload[0] = 1
	payload[len(payload)-1] = 1

	s, _ := newTestStore(t)
	srcDir := t.TempDir()
	if err := os.WriteFile(filepath.Join(srcDir, "memfile"), payload, 0o600); err != nil {
		t.Fatal(err)
	}
	const prefix = "base/sparse-restore"
	if _, _, err := s.Export(context.Background(), prefix, srcDir, []string{"memfile"}, 1, 1, "", ""); err != nil {
		t.Fatalf("Export: %v", err)
	}

	dstDir := t.TempDir()
	if _, _, err := s.Restore(context.Background(), prefix, dstDir); err != nil {
		t.Fatalf("Restore: %v", err)
	}
	dst := filepath.Join(dstDir, "memfile")
	got, err := os.ReadFile(dst)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(got, payload) {
		t.Fatal("restored contents differ from stored contents")
	}
	info, err := os.Stat(dst)
	if err != nil {
		t.Fatal(err)
	}
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok {
		t.Fatalf("stat data has type %T, want *syscall.Stat_t", info.Sys())
	}
	allocatedBytes := stat.Blocks * 512
	if allocatedBytes >= logicalSize/2 {
		t.Fatalf("restored file allocated %d bytes for %d logical bytes, want less than half", allocatedBytes, logicalSize)
	}
}
