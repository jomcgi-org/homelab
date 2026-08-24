package refresh

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func git(t *testing.T, dir string, args ...string) string {
	t.Helper()
	path, err := exec.LookPath("git")
	if err != nil {
		t.Fatalf("git not on PATH: %v", err)
	}
	cmd := exec.Command(path, args...)
	cmd.Dir = dir
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("git %v in %s: %v: %s", args, dir, err, out)
	}
	return string(out)
}

func commitFile(t *testing.T, work, name, content, message string) {
	t.Helper()
	if err := os.WriteFile(filepath.Join(work, name), []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
	git(t, work, "add", name)
	git(t, work, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-m", message)
}

// fixtureUpstream creates an upstream "remote" shaped exactly as the refresher
// addresses GitHub: <base>/<owner>/<name>.git holding one branch main.
func fixtureUpstream(t *testing.T) (base string, work string) {
	t.Helper()
	tmp := t.TempDir()
	work = filepath.Join(tmp, "work")
	if err := os.MkdirAll(work, 0o755); err != nil {
		t.Fatal(err)
	}
	git(t, work, "init", "-b", "main", ".")
	commitFile(t, work, "hello.txt", "first\n", "first")
	base = filepath.Join(tmp, "upstream") + string(filepath.Separator)
	if err := os.MkdirAll(base, 0o755); err != nil {
		t.Fatal(err)
	}
	git(t, work, "clone", "--bare", ".", filepath.Join(base, "owner", "name.git"))
	return base, work
}

func newTestRefresher(t *testing.T, base string) *Refresher {
	t.Helper()
	path, err := exec.LookPath("git")
	if err != nil {
		t.Fatalf("git not on PATH: %v", err)
	}
	return New(Config{
		Root:         filepath.Join(t.TempDir(), "mirrors"),
		GitBin:       path,
		Repos:        []string{"owner/name"},
		Interval:     time.Hour,
		UpstreamBase: base,
	}, nil)
}

func TestSyncRepoInitialCloneAndRefresh(t *testing.T) {
	base, work := fixtureUpstream(t)
	r := newTestRefresher(t, base)

	dir := r.RepoDir("owner/name")
	if _, err := os.Stat(dir); !os.IsNotExist(err) {
		t.Fatalf("mirror exists before first sync: %v", err)
	}

	r.syncRepo(t.Context(), "owner/name")

	out := git(t, dir, "rev-parse", "--is-bare-repository")
	if strings.TrimSpace(out) != "true" {
		t.Errorf("mirror is not a bare repository: %s", out)
	}
	out = git(t, dir, "rev-list", "--count", "refs/heads/main")
	if strings.TrimSpace(out) != "1" {
		t.Errorf("main commit count = %s, want 1", out)
	}

	// The filter config is load-bearing: without it guests cannot hydrate
	// with --filter=blob:none (#4473).
	out = git(t, dir, "config", "--get", "uploadpack.allowFilter")
	if strings.TrimSpace(out) != "true" {
		t.Errorf("uploadpack.allowFilter = %q, want true", out)
	}
	// HEAD points at the upstream default branch so a plain clone resolves.
	out = git(t, dir, "symbolic-ref", "HEAD")
	if strings.TrimSpace(out) != "refs/heads/main" {
		t.Errorf("HEAD = %q, want refs/heads/main", out)
	}

	// A second upstream commit appears after the next pass: the mirror moves
	// with upstream within one interval. The work repo pushes into the bare
	// stand-in remote, exactly what a real GitHub push does.
	commitFile(t, work, "hello.txt", "second\n", "second")
	git(t, work, "push", filepath.Join(base, "owner", "name.git"), "main")
	r.syncRepo(t.Context(), "owner/name")

	out = strings.TrimSpace(git(t, dir, "rev-parse", "refs/heads/main"))
	want := strings.TrimSpace(git(t, work, "rev-parse", "refs/heads/main"))
	if out != want {
		t.Errorf("mirror main = %s, want upstream %s", out, want)
	}
}

func TestSyncRepoRejectsInvalidNames(t *testing.T) {
	base, _ := fixtureUpstream(t)
	r := newTestRefresher(t, base)
	for _, repo := range []string{"norepo", "../escape", "", "a/b/c"} {
		if err := r.syncRepo(t.Context(), repo); err == nil {
			t.Errorf("syncRepo(%q) succeeded, want rejection", repo)
		}
	}
}

func TestAuthArgsEmptyTokenIsNil(t *testing.T) {
	if got := authArgs(""); got != nil {
		t.Errorf("authArgs(\"\") = %v, want nil", got)
	}
	if got := authArgs("tok"); len(got) != 2 || !strings.Contains(got[1], "Basic ") {
		t.Errorf("authArgs(tok) = %v, want one -c pair carrying Basic auth", got)
	}
}
