//go:build linux

package driver

import (
	"bytes"
	"os"
	"path/filepath"
	"testing"

	"golang.org/x/sys/unix"
)

func TestDigHolesPreservesMixedFile(t *testing.T) {
	const block = 4096
	data := make([]byte, 3*1024*1024+block/2)
	copy(data[block/2:], bytes.Repeat([]byte{0x7f}, block+block/2))
	copy(data[2*1024*1024+block/2:], bytes.Repeat([]byte{0x23}, 2*block+123))
	path := filepath.Join(t.TempDir(), "memfile")
	if err := os.WriteFile(path, data, 0o600); err != nil {
		t.Fatal(err)
	}
	before, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	beforeSize := fileSize(t, path)
	freed, err := digHoles(path)
	if err != nil {
		t.Fatalf("digHoles: %v", err)
	}
	if freed == 0 {
		t.Fatal("digHoles freed zero bytes")
	}
	after, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(before, after) {
		t.Fatal("file contents changed after punching holes")
	}
	if got := fileSize(t, path); got != beforeSize {
		t.Fatalf("logical size changed: before %d, after %d", beforeSize, got)
	}
}

func TestDigHolesDropsBlocks(t *testing.T) {
	path := filepath.Join(t.TempDir(), "memfile")
	data := make([]byte, 2*1024*1024)
	data[0] = 1
	data[len(data)-1] = 1
	if err := os.WriteFile(path, data, 0o600); err != nil {
		t.Fatal(err)
	}
	before := fileBlocks(t, path)
	if _, err := digHoles(path); err != nil {
		t.Fatal(err)
	}
	if after := fileBlocks(t, path); after >= before {
		t.Fatalf("allocated blocks did not decrease: before %d, after %d", before, after)
	}
}

func TestDigHolesEdgeCases(t *testing.T) {
	tests := []struct {
		name string
		data []byte
	}{
		{name: "all zero", data: make([]byte, 2*4096)},
		{name: "all non-zero", data: bytes.Repeat([]byte{1}, 2*4096)},
		{name: "smaller than block", data: []byte{1, 2, 3}},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "memfile")
			if err := os.WriteFile(path, tt.data, 0o600); err != nil {
				t.Fatal(err)
			}
			before, err := os.ReadFile(path)
			if err != nil {
				t.Fatal(err)
			}
			beforeSize := fileSize(t, path)
			freed, err := digHoles(path)
			if err != nil {
				t.Fatal(err)
			}
			if tt.name == "all non-zero" && freed != 0 {
				t.Fatalf("freed %d bytes from non-zero file", freed)
			}
			after, err := os.ReadFile(path)
			if err != nil {
				t.Fatal(err)
			}
			if !bytes.Equal(before, after) || fileSize(t, path) != beforeSize {
				t.Fatal("edge-case file changed")
			}
		})
	}
}

func fileSize(t *testing.T, path string) int64 {
	t.Helper()
	st, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	return st.Size()
}

func fileBlocks(t *testing.T, path string) int64 {
	t.Helper()
	var st unix.Stat_t
	if err := unix.Stat(path, &st); err != nil {
		t.Fatal(err)
	}
	return st.Blocks
}
