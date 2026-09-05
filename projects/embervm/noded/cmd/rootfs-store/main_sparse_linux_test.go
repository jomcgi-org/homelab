//go:build linux

package main

import (
	"bytes"
	"context"
	"os"
	"path/filepath"
	"syscall"
	"testing"
)

func TestGetPunchesLargeZeroRunsSparse(t *testing.T) {
	server, fake := newFakeObjectStore(t)
	contents := make([]byte, 8<<20)
	copy(contents, []byte("ext4"))
	copy(contents[len(contents)-4:], []byte("tail"))
	payloadKey, marker := testCompletenessMarker(t, testDigest, contents)
	checksumKey := checksumObjectKey(testDigest)
	fake.objects["/embervm/"+payloadKey] = contents
	fake.objects["/embervm/"+checksumKey] = marker
	out := filepath.Join(t.TempDir(), "rootfs.ext4")

	var stdout, stderr bytes.Buffer
	code := run(context.Background(), []string{"get", "--digest", testDigest, "--out", out}, storeEnv(server.URL), &stdout, &stderr)
	if code != 0 {
		t.Fatalf("get exit = %d, stderr = %q", code, stderr.String())
	}
	info, err := os.Stat(out)
	if err != nil {
		t.Fatal(err)
	}
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok {
		t.Skip("filesystem stat does not report allocated blocks")
	}
	allocated := int64(stat.Blocks) * 512
	if allocated >= info.Size() {
		t.Fatalf("download allocated %d bytes for %d-byte sparse fixture", allocated, info.Size())
	}
}
