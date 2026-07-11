package fullscan

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"testing"

	"github.com/jomcgi/homelab/projects/firecracker/substrate/vsockproto"
)

func TestMaterializeAndScan(t *testing.T) {
	req := vsockproto.ScanRequest{Files: []vsockproto.ScanFile{
		{Path: "pkg/a.py", Content: "a = 1\n"},
		{Path: "pkg/b.py", Content: "b = 2\n"},
	}}

	var recordedDir string
	runner := func(ctx context.Context, treeDir string) ([]byte, error) {
		recordedDir = treeDir
		if _, err := os.Stat(filepath.Join(treeDir, "pkg", "a.py")); err != nil {
			t.Fatalf("expected pkg/a.py to be materialized under %s: %v", treeDir, err)
		}
		resultPath := filepath.ToSlash(filepath.Join(treeDir, "pkg/b.py"))
		out := `{"results":[{"check_id":"rule.id","path":"` + resultPath + `","start":{"line":1,"col":1},"extra":{"message":"m","severity":"WARNING"}}],"errors":[]}
`
		return []byte(out), nil
	}

	res, err := Scan(context.Background(), req, runner)
	if err != nil {
		t.Fatalf("Scan: %v", err)
	}
	if len(res.Findings) != 1 {
		t.Fatalf("want 1 finding, got %d: %+v", len(res.Findings), res.Findings)
	}
	if got, want := res.Findings[0].Path, "pkg/b.py"; got != want {
		t.Fatalf("Path = %q, want %q", got, want)
	}

	if recordedDir == "" {
		t.Fatal("runner was never called")
	}
	if _, err := os.Stat(recordedDir); !os.IsNotExist(err) {
		t.Fatalf("expected tree dir %s to be cleaned up after Scan, stat err = %v", recordedDir, err)
	}
}

// TestPathTraversalIsContainedNotEscaping documents the actual behavior of
// the traversal guard: because a leading path separator is prepended before
// filepath.Clean, a "../evil.py"-style request path can never resolve above
// the tree dir root (Clean on an absolute path never yields a leading ".."),
// so it lands inside the tree at its cleaned, contained location rather than
// tripping the escape check. This asserts the file ends up safely inside the
// tree dir and the scan still runs, i.e. the containment-by-construction
// works even though the explicit HasPrefix guard is unreachable for this
// input shape.
func TestPathTraversalIsContainedNotEscaping(t *testing.T) {
	req := vsockproto.ScanRequest{Files: []vsockproto.ScanFile{
		{Path: "../evil.py", Content: "x = 1\n"},
	}}

	var recordedDir string
	runner := func(ctx context.Context, treeDir string) ([]byte, error) {
		recordedDir = treeDir
		if _, err := os.Stat(filepath.Join(treeDir, "evil.py")); err != nil {
			t.Fatalf("expected cleaned path to land inside tree dir at evil.py: %v", err)
		}
		return []byte(`{"results":[],"errors":[]}` + "\n"), nil
	}

	if _, err := Scan(context.Background(), req, runner); err != nil {
		t.Fatalf("Scan: %v", err)
	}
	if recordedDir == "" {
		t.Fatal("runner was never called")
	}
}

func TestRunnerErrorWithOutputStillParses(t *testing.T) {
	req := vsockproto.ScanRequest{Files: []vsockproto.ScanFile{
		{Path: "pkg/a.py", Content: "a = 1\n"},
	}}

	runErr := errors.New("exit status 1")
	runner := func(ctx context.Context, treeDir string) ([]byte, error) {
		resultPath := filepath.ToSlash(filepath.Join(treeDir, "pkg/a.py"))
		out := `{"results":[{"check_id":"rule.id","path":"` + resultPath + `","start":{"line":1,"col":1},"extra":{"message":"m","severity":"ERROR"}}],"errors":[]}
`
		return []byte(out), runErr
	}

	res, err := Scan(context.Background(), req, runner)
	if err != nil {
		t.Fatalf("Scan: %v", err)
	}
	if len(res.Findings) != 1 {
		t.Fatalf("want 1 finding, got %d", len(res.Findings))
	}
	found := false
	for _, e := range res.Errors {
		if e == runErr.Error() {
			found = true
		}
	}
	if !found {
		t.Fatalf("want runner error %q appended to Errors, got %v", runErr.Error(), res.Errors)
	}
}

func TestRunnerErrorNoOutputFails(t *testing.T) {
	req := vsockproto.ScanRequest{Files: []vsockproto.ScanFile{
		{Path: "pkg/a.py", Content: "a = 1\n"},
	}}

	runner := func(ctx context.Context, treeDir string) ([]byte, error) {
		return nil, errors.New("semgrep: command not found")
	}

	_, err := Scan(context.Background(), req, runner)
	if err == nil {
		t.Fatal("want error when runner fails with no output, got nil")
	}
}
