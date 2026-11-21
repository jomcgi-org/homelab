package mandoline

import (
	"context"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestRunPinsAndCachesTheDiff(t *testing.T) {
	workspace, root, commit := checkout(t)
	program := filepath.Join(t.TempDir(), "mandoline")
	if err := os.WriteFile(program, []byte(`#!/bin/sh
if [ "$1" = --version ]; then
    echo 'mandoline 0.25.0 fixture'
    exit 0
fi
test "$#" = 5 && test "$1 $2 $3 $4" = 'diff --json --no-cache --base' || exit 2
printf '{"schemaVersion":1,"command":"slice","base":"%s"}\n' "$5"
`), 0o700); err != nil {
		t.Fatal(err)
	}
	req := Request{Command: "diff", Commit: commit, Base: commit}
	got := Run(context.Background(), workspace, root, program, false, req)
	if got.Status != "complete" || got.ToolVersion != toolVersion || got.Commit != commit {
		t.Fatalf("analysis: %+v", got)
	}
	cache, err := openCache(workspace)
	if err != nil {
		t.Fatal(err)
	}
	tree := git(t, root, "rev-parse", "HEAD^{tree}")
	key := cacheKey(tree, got.PackDigests["bundled"], req)
	cached, ok := cache.read(key, got)
	if !ok || !json.Valid(cached.Result) {
		t.Fatalf("complete analysis was not cached: %+v", cached)
	}
	req.Command = "edit-file"
	if got := Run(context.Background(), workspace, root, program, false, req); got.Status != "unsupported" {
		t.Fatalf("edit-file escaped allowlist: %+v", got)
	}
	req.Command, req.Base = "audit", ""
	if got := Run(context.Background(), workspace, root, program, false, req); got.Status != "error" {
		t.Fatalf("diff harness accepted a full audit: %+v", got)
	}
}

func TestCheckoutRejectsDirtyTreeAndHiddenChanges(t *testing.T) {
	for _, flag := range []string{"", "--assume-unchanged", "--skip-worktree"} {
		t.Run(flag, func(t *testing.T) {
			workspace, root, commit := checkout(t)
			req := Request{Commit: commit, Base: commit}
			if flag != "" {
				git(t, root, "update-index", flag, "tracked.py")
			}
			if err := os.WriteFile(filepath.Join(root, "tracked.py"), []byte("changed\n"), 0o600); err != nil {
				t.Fatal(err)
			}
			if _, _, err := verifyCheckout(context.Background(), workspace, root, req, nil); err == nil {
				t.Fatal("dirty checkout accepted")
			}
		})
	}
}

func TestCaptureLimitsAndSeparatesOutput(t *testing.T) {
	out := capture(context.Background(), "/bin/sh", t.TempDir(), nil, []string{"-c", "echo result; echo diagnostic >&2"})
	if out.err != nil || string(out.stdout) != "result\n" || string(out.stderr) != "diagnostic\n" {
		t.Fatalf("capture: %+v", out)
	}
	out = capture(context.Background(), "/bin/sh", t.TempDir(), nil, []string{"-c", "yes output"})
	if !out.truncated || len(out.stdout)+len(out.stderr) != outputLimit {
		t.Fatalf("size limit was not enforced")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()
	out = capture(ctx, "/bin/sh", t.TempDir(), nil, []string{"-c", "sleep 5"})
	if got := classify(Envelope{}, "audit", out); got.Status != "timeout" {
		t.Fatalf("timeout: %+v", got)
	}
}

func TestCoverageWarningIsIncomplete(t *testing.T) {
	raw := []byte(`{"schemaVersion":1,"command":"audit","warnings":[{"code":"no_entry_points_found"}]}`)
	got := classify(Envelope{}, "audit", captured{stdout: raw})
	if got.Status != "partial" || len(got.Gaps) != 1 || !json.Valid(got.Result) {
		t.Fatalf("warning was lost: %+v", got)
	}
}

// Run this regression with the private release binary when it is available.
func TestPinnedAnalyzer(t *testing.T) {
	program := os.Getenv("FACTORY_TEST_MANDOLINE")
	if program == "" {
		t.Skip("private Mandoline executable unavailable")
	}
	if !filepath.IsAbs(program) {
		program = filepath.Join(os.Getenv("TEST_SRCDIR"), os.Getenv("TEST_WORKSPACE"), program)
	}
	workspace, root, base := checkout(t)
	if err := os.WriteFile(filepath.Join(root, "tracked.py"), []byte("def value():\n    return 43\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	git(t, root, "-c", "user.name=fixture", "-c", "user.email=fixture@example.com", "commit", "-qam", "head")
	head := git(t, root, "rev-parse", "HEAD")
	ctx := context.Background()
	scoped := Run(ctx, workspace, root, program, false, Request{Command: "diff", Commit: head, Base: base})
	if scoped.Status != "complete" || scoped.ToolVersion != toolVersion || !json.Valid(scoped.Result) {
		t.Fatalf("pinned diff: status %s, gaps %v", scoped.Status, scoped.Gaps)
	}
	audit := Run(ctx, workspace, root, program, true, Request{Command: "audit", Commit: head})
	if audit.Status != "partial" || len(audit.Gaps) == 0 || !json.Valid(audit.Result) {
		t.Fatalf("pinned audit: status %s, gaps %v", audit.Status, audit.Gaps)
	}
	if git(t, root, "status", "--short", "--ignored") != "" {
		t.Fatal("analysis changed the checkout")
	}
}

func checkout(t *testing.T) (string, string, string) {
	t.Helper()
	workspace := t.TempDir()
	t.Setenv("TMPDIR", t.TempDir())
	root := filepath.Join(workspace, "pr")
	if err := os.Mkdir(root, 0o700); err != nil {
		t.Fatal(err)
	}
	git(t, root, "init", "-q")
	if err := os.WriteFile(filepath.Join(root, "tracked.py"), []byte("def value():\n    return 42\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	git(t, root, "add", ".")
	git(t, root, "-c", "user.name=fixture", "-c", "user.email=fixture@example.com", "commit", "-qm", "base")
	return workspace, root, git(t, root, "rev-parse", "HEAD")
}

func git(t *testing.T, dir string, args ...string) string {
	t.Helper()
	cmd := exec.Command("/usr/bin/git", args...)
	cmd.Dir = dir
	result, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("git %v: %v\n%s", args, err, result)
	}
	return strings.TrimSpace(string(result))
}
