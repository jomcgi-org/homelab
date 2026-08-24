package server

import (
	"bytes"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

// git runs a git command in dir and fails t on error.
func git(t *testing.T, dir string, args ...string) string {
	t.Helper()
	cmd := exec.Command(gitBin(t), args...)
	cmd.Dir = dir
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("git %v in %s: %v: %s", args, dir, err, out)
	}
	return string(out)
}

func gitBin(t *testing.T) string {
	t.Helper()
	path, err := exec.LookPath("git")
	if err != nil {
		t.Fatalf("git not on PATH: %v", err)
	}
	return path
}

// fixtureMirror builds one bare mirror of a two-commit repo under
// root/owner/repo.git and returns the file content the second commit carries,
// so the clone smoke can assert real data crossed the wire.
func fixtureMirror(t *testing.T, root string) string {
	t.Helper()
	work := t.TempDir()
	git(t, work, "init", "-b", "main", ".")
	write := func(name, content, message string) {
		if err := os.WriteFile(filepath.Join(work, name), []byte(content), 0o644); err != nil {
			t.Fatal(err)
		}
		git(t, work, "add", name)
		git(t, work, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-m", message)
	}
	write("hello.txt", "hi from the fixture\n", "first")
	write("hello.txt", "second revision\n", "second")
	mirrorDir := filepath.Join(root, "owner", "repo.git")
	if err := os.MkdirAll(filepath.Dir(mirrorDir), 0o755); err != nil {
		t.Fatal(err)
	}
	git(t, work, "clone", "--bare", ".", mirrorDir)
	return "second revision\n"
}

func newTestServer(t *testing.T) (*Server, http.Handler) {
	t.Helper()
	root := t.TempDir()
	s := New(root, nil)
	s.gitBin = gitBin(t)
	return s, s.Handler()
}

func TestResolveRepoPath(t *testing.T) {
	cases := []struct {
		name       string
		path       string
		wantRepo   string
		wantAction string
		wantOK     bool
	}{
		{"happy path", "/owner/repo.git/info/refs", filepath.Join("/r", "owner", "repo.git"), "info/refs", true},
		{"upload-pack action", "/owner/repo.git/git-upload-pack", filepath.Join("/r", "owner", "repo.git"), "git-upload-pack", true},
		{"trailing slash", "/owner/repo.git/info/refs/", filepath.Join("/r", "owner", "repo.git"), "info/refs", true},
		{"missing action", "/owner/repo.git", "", "", false},
		{"single segment", "/repo.git", "", "", false},
		{"no dot git", "/owner/repo/info/refs", "", "", false},
		{"dotdot owner", "/../repo.git/info/refs", "", "", false},
		{"dotdot name", "/owner/../info/refs", "", "", false},
		{"hidden segment", "/.hidden/repo.git/info/refs", "", "", false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			repo, action, ok := resolveRepoPath("/r", tc.path)
			if ok != tc.wantOK || repo != tc.wantRepo || action != tc.wantAction {
				t.Errorf("resolveRepoPath(%q) = (%q, %q, %v), want (%q, %q, %v)",
					tc.path, repo, action, ok, tc.wantRepo, tc.wantAction, tc.wantOK)
			}
		})
	}
}

func TestInfoRefsAdvertisement(t *testing.T) {
	root := t.TempDir()
	fixtureMirror(t, root)
	s := New(root, nil).WithGitBin(gitBin(t))
	srv := httptest.NewServer(s.Handler())
	defer srv.Close()

	resp, err := http.Get(srv.URL + "/owner/repo.git/info/refs?service=git-upload-pack")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	body := new(bytes.Buffer)
	if _, err := body.ReadFrom(resp.Body); err != nil {
		t.Fatal(err)
	}

	const wantHeader = "001e# service=git-upload-pack\n0000"
	if got := body.String(); !strings.HasPrefix(got, wantHeader) {
		t.Errorf("advertisement prefix = %q, want %q", got[:minInt(len(got), len(wantHeader))], wantHeader)
	}
	if ct := resp.Header.Get("Content-Type"); ct != contentTypeAdvertisement {
		t.Errorf("content type = %q, want %q", ct, contentTypeAdvertisement)
	}
	if cc := resp.Header.Get("Cache-Control"); cc != "no-cache" {
		t.Errorf("cache-control = %q, want no-cache", cc)
	}
	// The filter capability is what makes guest --filter=blob:none hydration
	// possible at all (#4473): the server must advertise it on every mirror.
	if !strings.Contains(body.String(), "filter") {
		t.Errorf("advertisement does not advertise the filter capability:\n%s", body.String())
	}
	if !strings.Contains(body.String(), "HEAD") {
		t.Errorf("advertisement missing refs:\n%s", body.String())
	}
}

func minInt(a, b int) int {
	if b < a {
		return b
	}
	return a
}

func TestInfoRefsRejectsOtherServices(t *testing.T) {
	_, handler := newTestServer(t)
	srv := httptest.NewServer(handler)
	defer srv.Close()

	resp, err := http.Get(srv.URL + "/owner/repo.git/info/refs?service=git-receive-pack")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusNotFound {
		t.Errorf("receive-pack discovery status = %d, want 404 (mirror must not advertise a write side)", resp.StatusCode)
	}
}

func TestUploadPackRejectsWrongContentType(t *testing.T) {
	_, handler := newTestServer(t)
	srv := httptest.NewServer(handler)
	defer srv.Close()

	resp, err := http.Post(srv.URL+"/owner/repo.git/git-upload-pack", "application/json", strings.NewReader("{}"))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusNotFound {
		t.Errorf("wrong content-type status = %d, want 404", resp.StatusCode)
	}
}

func TestUnknownRepoIs404(t *testing.T) {
	_, handler := newTestServer(t)
	srv := httptest.NewServer(handler)
	defer srv.Close()

	resp, err := http.Get(srv.URL + "/ghost/nothing.git/info/refs?service=git-upload-pack")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusNotFound {
		t.Errorf("unknown repo status = %d, want 404", resp.StatusCode)
	}
}

// TestRealGitCloneSmoke is the end-to-end guard: an actual `git clone` (the
// exact shape hydration uses, --single-branch --filter=blob:none) succeeds
// against the served fixture and checks out the fixture's content.
func TestRealGitCloneSmoke(t *testing.T) {
	root := t.TempDir()
	wantContent := fixtureMirror(t, root)
	s := New(root, nil).WithGitBin(gitBin(t))
	srv := httptest.NewServer(s.Handler())
	defer srv.Close()

	dst := filepath.Join(t.TempDir(), "checkout")
	git(t, t.TempDir(), "clone",
		"--single-branch",
		"--filter=blob:none",
		srv.URL+"/owner/repo.git",
		dst,
	)
	content, err := os.ReadFile(filepath.Join(dst, "hello.txt"))
	if err != nil {
		t.Fatalf("cloned checkout missing hello.txt: %v", err)
	}
	if string(content) != wantContent {
		t.Errorf("cloned hello.txt = %q, want %q", content, wantContent)
	}
	// Full history survived the partial clone: both commits present.
	logOut := git(t, dst, "rev-list", "--count", "HEAD")
	if strings.TrimSpace(logOut) != "2" {
		t.Errorf("commit count = %s, want 2 (blob:none keeps history)", logOut)
	}
}

func TestHealthz(t *testing.T) {
	_, handler := newTestServer(t)
	srv := httptest.NewServer(handler)
	defer srv.Close()

	resp, err := http.Get(srv.URL + "/healthz")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Errorf("healthz status = %d, want 200", resp.StatusCode)
	}
}
