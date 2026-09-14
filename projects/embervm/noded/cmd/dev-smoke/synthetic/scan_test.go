package synthetic

import (
	"context"
	"errors"
	"path/filepath"
	"strings"
	"testing"
)

func TestDecodeRejectsUnboundedAndExecutableInputs(t *testing.T) {
	for _, body := range []string{
		`null`, `[]`, `{} {}`, `{"files":0}`, `{"files":257}`,
		`{"bytes_per_file":65537}`, `{"bytes_per_file":1023}`,
		`{"passes":0}`, `{"passes":1025}`, `{"hold_ms":-1}`, `{"hold_ms":5001}`,
		`{"command":"sh"}`, `{"path":"/etc/passwd"}`,
	} {
		t.Run(body, func(t *testing.T) {
			if _, err := Decode(strings.NewReader(body)); err == nil {
				t.Fatalf("accepted invalid request %s", body)
			}
		})
	}
	q, err := Decode(strings.NewReader(`{}`))
	if err != nil || q.Files != 64 || q.BytesPerFile != 32768 || q.Passes != 64 || q.HoldMS != 0 {
		t.Fatalf("unexpected defaults: %+v, %v", q, err)
	}
}

func TestScanIsRepeatableAndRequiresFreshDirectory(t *testing.T) {
	q := Request{Seed: "fixture", Files: 2, BytesPerFile: 1024, Passes: 2}
	root := t.TempDir()
	a, err := Run(context.Background(), filepath.Join(root, "a"), q)
	if err != nil {
		t.Fatal(err)
	}
	b, err := Run(context.Background(), filepath.Join(root, "b"), q)
	if err != nil {
		t.Fatal(err)
	}
	if !a.Synthetic || a.Bytes != 2048 || a.Findings == 0 || len(a.Checksum) != 64 || a.Checksum != b.Checksum {
		t.Fatalf("unexpected results: %+v, %+v", a, b)
	}
	if _, err := Run(context.Background(), filepath.Join(root, "a"), q); err == nil {
		t.Fatal("scan accepted a previously used directory")
	}
	q.Seed = "different"
	c, err := Run(context.Background(), filepath.Join(root, "c"), q)
	if err != nil || c.Checksum == a.Checksum {
		t.Fatalf("changed fixtures did not change checksum: %+v, %v", c, err)
	}
}

func TestScanStopsWhenCanceled(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	_, err := Run(ctx, filepath.Join(t.TempDir(), "scan"), Request{Files: 1, BytesPerFile: 1024, Passes: 1, HoldMS: 5000})
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("got %v, want context cancellation", err)
	}
}
