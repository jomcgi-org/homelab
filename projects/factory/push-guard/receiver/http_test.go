package main

import (
	"bytes"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

func TestOnlyGatewayIdentity(t *testing.T) {
	gateway, err := url.Parse(gatewayID)
	if err != nil {
		t.Fatal(err)
	}
	wrong, err := url.Parse("spiffe://cluster.local/ns/factory/sa/atenet-egress")
	if err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct {
		peer    *url.URL
		allowed bool
	}{{gateway, true}, {wrong, false}, {nil, false}} {
		var state tls.ConnectionState
		if tc.peer != nil {
			state.VerifiedChains = [][]*x509.Certificate{{{URIs: []*url.URL{tc.peer}}}}
		}
		if err := authorizeGateway(state); (err == nil) != tc.allowed {
			t.Fatalf("gateway identity %v: %v", tc.peer, err)
		}
	}
}

func TestRejectsOtherRoutesAndMissingCredential(t *testing.T) {
	h := newReceiver("", "", "")
	for _, tc := range []struct {
		method, path string
		status       int
	}{
		{http.MethodGet, route + "/info/refs?service=git-upload-pack", http.StatusNotFound},
		{http.MethodPost, route + "/git-upload-pack", http.StatusNotFound},
		{http.MethodGet, "/different.git/info/refs?service=git-receive-pack", http.StatusNotFound},
		{http.MethodPost, route + "/git-receive-pack", http.StatusUnauthorized},
		{http.MethodGet, route + "/info/refs?service=git-receive-pack", http.StatusUnauthorized},
	} {
		req := httptest.NewRequest(tc.method, tc.path, nil)
		w := httptest.NewRecorder()
		h.ServeHTTP(w, req)
		if w.Code != tc.status {
			t.Errorf("%s %s got %d, want %d", tc.method, tc.path, w.Code, tc.status)
		}
	}
}

func TestOversizedRequestIsRejectedBeforeGitStarts(t *testing.T) {
	req := httptest.NewRequest(http.MethodPost, route+"/git-receive-pack", nil)
	req.SetBasicAuth("x-access-token", "fixture")
	req.Header.Set("Content-Type", "application/x-git-receive-pack-request")
	req.ContentLength = maxPack + 1
	w := httptest.NewRecorder()
	newReceiver("", "", "").ServeHTTP(w, req)
	if w.Code != http.StatusRequestEntityTooLarge {
		t.Fatalf("got %d, want 413", w.Code)
	}
}

func TestIndexMustMatchThePin(t *testing.T) {
	tmp := t.TempDir()
	dir := filepath.Join(tmp, "indices")
	if err := os.Mkdir(dir, 0o700); err != nil {
		t.Fatal(err)
	}
	raw := []byte("harmless index fixture")
	digest := fmt.Sprintf("%x", sha256.Sum256(raw))
	path := filepath.Join(dir, digest+".json")
	write(t, path, raw, 0o600)
	if got, err := verifiedIndex(tmp, digest); err != nil || got != path {
		t.Fatalf("valid pin: %s %v", got, err)
	}
	write(t, path, []byte("changed fixture"), 0o600)
	if _, err := verifiedIndex(tmp, digest); err == nil {
		t.Fatal("changed index was accepted")
	}
}

func TestGitPushIsCheckedBeforeItReachesUpstream(t *testing.T) {
	tmp := t.TempDir()
	local, staging, upstream := filepath.Join(tmp, "local"), filepath.Join(tmp, "staging.git"), filepath.Join(tmp, "upstream.git")
	git(t, tmp, "init", "-q", local)
	git(t, local, "config", "user.name", "fixture")
	git(t, local, "config", "user.email", "fixture@example.com")
	write(t, filepath.Join(local, "tracked.txt"), []byte("initial\n"), 0o600)
	git(t, local, "add", "tracked.txt")
	git(t, local, "commit", "-qm", "initial")
	git(t, tmp, "clone", "-q", "--bare", local, upstream)
	git(t, tmp, "clone", "-q", "--bare", upstream, staging)

	hook := os.Getenv("PUSH_GUARD_TEST_HOOK")
	if hook == "" {
		hook = "../pre-receive"
	} else if !filepath.IsAbs(hook) {
		hook = filepath.Join(os.Getenv("TEST_SRCDIR"), os.Getenv("TEST_WORKSPACE"), hook)
	}
	raw, err := os.ReadFile(hook)
	if err != nil {
		t.Fatal(err)
	}
	installed := filepath.Join(staging, "hooks", "pre-receive")
	write(t, installed, raw, 0o700)

	const ref = "refs/heads/factory/harmless-fixture"
	sample := bytes.Repeat([]byte("synthetic protected analysis fixture\n"), 900)
	reference := filepath.Join(tmp, "reference")
	write(t, reference, sample, 0o600)
	index := filepath.Join(tmp, "index.json")
	out, err := exec.Command(installed, "--index", reference, reference).Output()
	if err != nil {
		t.Fatal(err)
	}
	write(t, index, out, 0o600)
	git(t, staging, "config", "remote.fixture.url", upstream)
	// A local test transport needs to clear the sender's object quarantine.
	// Over HTTPS the upstream process cannot inherit this environment.
	git(t, staging, "config", "remote.fixture.receivepack",
		"env -u GIT_QUARANTINE_PATH -u GIT_OBJECT_DIRECTORY -u GIT_ALTERNATE_OBJECT_DIRECTORIES git-receive-pack")
	srv := httptest.NewServer(newReceiver(staging, index, "fixture"))
	defer srv.Close()
	dest := srv.URL + route
	push := func(branch string) (string, error) {
		cmd := exec.Command("/usr/bin/git", "-c", "http.extraHeader=Authorization: Basic eC1hY2Nlc3MtdG9rZW46Zml4dHVyZQ==",
			"push", dest, "HEAD:"+branch)
		cmd.Dir = local
		out, err := cmd.CombinedOutput()
		return string(out), err
	}
	git(t, local, "commit", "--allow-empty", "-qm", "ordinary push")
	if out, err := push(ref); err != nil {
		t.Fatalf("ordinary push: %v\n%s", err, out)
	}
	good := git(t, local, "rev-parse", "HEAD")
	if got := git(t, upstream, "rev-parse", ref); got != good {
		t.Fatalf("upstream got %s, want %s", got, good)
	}
	for _, tc := range []struct {
		name string
		data []byte
		want string
	}{
		{"oversized", make([]byte, 1024*1024+1), "blob exceeds 1 MiB"},
		{"protected", sample[80:20000], "protected content"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			git(t, local, "checkout", "-q", "--detach", good)
			write(t, filepath.Join(local, "candidate"), tc.data, 0o600)
			git(t, local, "add", "candidate")
			git(t, local, "commit", "-qm", "add "+tc.name)
			git(t, local, "rm", "-q", "candidate")
			git(t, local, "commit", "-qm", "remove candidate at tip")
			if out, err := push(ref); err == nil || !strings.Contains(out, tc.want) {
				t.Fatalf("candidate was not correctly rejected: %v\n%s", err, out)
			}
			for _, dir := range []string{upstream, staging} {
				if got := git(t, dir, "rev-parse", ref); got != good {
					t.Errorf("%s moved to %s", dir, got)
				}
			}
		})
	}

	t.Run("public base advances outside the receiver", func(t *testing.T) {
		git(t, local, "checkout", "-q", "--detach", good)
		write(t, filepath.Join(local, "public-large"), make([]byte, 1024*1024+1), 0o600)
		git(t, local, "add", "public-large")
		git(t, local, "commit", "-qm", "advance public main with a large fixture")
		mainRef := git(t, upstream, "symbolic-ref", "HEAD")
		git(t, local, "push", upstream, "HEAD:"+mainRef)

		git(t, local, "commit", "--allow-empty", "-qm", "ordinary push from public main")
		if out, err := push(ref); err != nil {
			t.Fatalf("push on the fresh public base: %v\n%s", err, out)
		}
		want := git(t, local, "rev-parse", "HEAD")
		if got := git(t, upstream, "rev-parse", ref); got != want {
			t.Fatalf("upstream got %s, want %s", got, want)
		}
	})
}

func git(t *testing.T, dir string, args ...string) string {
	t.Helper()
	cmd := exec.Command("/usr/bin/git", args...)
	cmd.Dir = dir
	data, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatal(fmt.Errorf("git %s: %w\n%s", args[0], err, data))
	}
	return strings.TrimSpace(string(data))
}

func write(t *testing.T, path string, data []byte, mode os.FileMode) {
	t.Helper()
	if err := os.WriteFile(path, data, mode); err != nil {
		t.Fatal(err)
	}
}
