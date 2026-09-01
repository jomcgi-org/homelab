//go:build linux

package driver

import (
	"os"
	"path/filepath"
	"testing"
)

func TestReadOOMKillCount(t *testing.T) {
	path := filepath.Join(t.TempDir(), "memory.events")
	data := []byte("low 0\nhigh 0\nmax 3\noom 2\noom_kill 1\noom_group_kill 1\n")
	if err := os.WriteFile(path, data, 0o600); err != nil {
		t.Fatal(err)
	}
	got, err := readOOMKillCount(path)
	if err != nil {
		t.Fatalf("readOOMKillCount: %v", err)
	}
	if got != 1 {
		t.Fatalf("oom_kill = %d, want 1", got)
	}
}
