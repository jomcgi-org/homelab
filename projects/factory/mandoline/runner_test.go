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
if [ "$1 $2" = 'pack list' ]; then
    echo '{"command":"pack list","schema_version":1,"registries":[],"bundled":[{"name":"fixture","disabled":false}]}'
    exit 0
fi
if [ "$1 $2 $3" = 'pack taint list' ]; then
    echo '{"libraries":[{"name":"fixture","ecosystem":"go","bundled":true}]}'
    exit 0
fi
test "$#" = 5 && test "$1 $2 $3 $4" = 'diff --json --no-cache --base' || exit 2
printf '{"schemaVersion":1,"command":"slice","base":"%s"}\n' "$5"
`), 0o700); err != nil {
		t.Fatal(err)
	}
	req := Request{Command: "diff", Commit: commit, Base: commit}
	got := run(context.Background(), workspace, program, false, req, fixtureMain(commit))
	if got.Status != "complete" || got.ToolVersion != toolVersion || got.Commit != commit {
		t.Fatalf("analysis: %+v", got)
	}
	for _, catalog := range []string{"bundled_pack_catalog", "bundled_taint_catalog"} {
		if digest := got.PackDigests[catalog]; digest == "" || digest == got.ToolDigest {
			t.Fatalf("%s was not independently identified", catalog)
		}
	}
	cache, err := openCache(workspace)
	if err != nil {
		t.Fatal(err)
	}
	tree := git(t, root, "rev-parse", "HEAD^{tree}")
	key := cacheKey(tree, got.ToolDigest, got.PackDigests, req)
	cached, ok := cache.read(key, got)
	if !ok || !json.Valid(cached.Result) {
		t.Fatalf("complete analysis was not cached: %+v", cached)
	}
	req.Command = "edit-file"
	if got := run(context.Background(), workspace, program, false, req, fixtureMain(commit)); got.Status != "unsupported" {
		t.Fatalf("edit-file escaped allowlist: %+v", got)
	}
	req.Command = "audit"
	if got := run(context.Background(), workspace, program, false, req, fixtureMain(commit)); got.Status != "unsupported" {
		t.Fatalf("diff harness accepted a full audit: %+v", got)
	}
}

func TestCheckoutRejectsDirtyTreeAndHiddenChanges(t *testing.T) {
	for _, flag := range []string{"", "--assume-unchanged", "--skip-worktree"} {
		t.Run(flag, func(t *testing.T) {
			_, root, commit := checkout(t)
			req := Request{Commit: commit, Base: commit}
			if flag != "" {
				git(t, root, "update-index", flag, "tracked.py")
			}
			if err := os.WriteFile(filepath.Join(root, "tracked.py"), []byte("changed\n"), 0o600); err != nil {
				t.Fatal(err)
			}
			if _, err := verifyCheckout(context.Background(), root, commit, false, req, nil); err == nil {
				t.Fatal("dirty checkout accepted")
			}
		})
	}
}

func TestTaskCheckoutAndBaseCannotBeSubstituted(t *testing.T) {
	workspace, root, base := checkout(t)
	git(t, root, "commit", "--allow-empty", "-qm", "head")
	head := git(t, root, "rev-parse", "HEAD")
	git(t, root, "update-ref", "refs/remotes/origin/main", head)
	if _, err := verifyCheckout(context.Background(), root, base, false, Request{Commit: head, Base: head}, nil); err == nil {
		t.Fatal("accepted a substitute diff base")
	}
	if _, err := verifyCheckout(context.Background(), root, base, true, Request{Commit: head}, nil); err == nil {
		t.Fatal("accepted an audit of a non-main commit")
	}
	if _, err := verifyCheckout(context.Background(), root, base, false, Request{Commit: head, Base: base}, nil); err != nil {
		t.Fatal(err)
	}
	decoy := filepath.Join(workspace, "other")
	if err := os.Rename(root, decoy); err != nil {
		t.Fatal(err)
	}
	if _, err := selectCheckout(workspace, false); err == nil {
		t.Fatal("accepted an arbitrary checkout")
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
	scoped := run(ctx, workspace, program, false, Request{Command: "diff", Commit: head, Base: base}, fixtureMain(base))
	if scoped.Status != "complete" || scoped.ToolVersion != toolVersion || !json.Valid(scoped.Result) {
		t.Fatalf("pinned diff: status %s, gaps %v", scoped.Status, scoped.Gaps)
	}
	callers := run(ctx, workspace, program, false, Request{Command: "callers", Commit: head, Base: base}, fixtureMain(base))
	if (callers.Status != "complete" && callers.Status != "partial") || !json.Valid(callers.Result) {
		t.Fatalf("pinned callers: status %s, gaps %v", callers.Status, callers.Gaps)
	}
	main := filepath.Join(workspace, "main")
	if err := os.Rename(root, main); err != nil {
		t.Fatal(err)
	}
	audit := run(ctx, workspace, program, true, Request{Command: "audit", Commit: head}, fixtureMain(head))
	if audit.Status != "partial" || len(audit.Gaps) == 0 || !json.Valid(audit.Result) {
		t.Fatalf("pinned audit: status %s, gaps %v", audit.Status, audit.Gaps)
	}
	if git(t, main, "status", "--short", "--ignored") != "" {
		t.Fatal("analysis changed the checkout")
	}
}

func fixtureMain(commit string) func(context.Context, string, []string) (string, error) {
	return func(context.Context, string, []string) (string, error) {
		return commit, nil
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
	git(t, root, "config", "user.name", "fixture")
	git(t, root, "config", "user.email", "fixture@example.com")
	if err := os.WriteFile(filepath.Join(root, "tracked.py"), []byte("def value():\n    return 42\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	git(t, root, "add", ".")
	git(t, root, "commit", "-qm", "base")
	commit := git(t, root, "rev-parse", "HEAD")
	git(t, root, "update-ref", "refs/remotes/origin/main", commit)
	return workspace, root, commit
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
