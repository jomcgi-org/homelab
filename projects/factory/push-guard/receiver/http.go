package main

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"strings"
	"syscall"
	"time"
)

const (
	maxPack      = 128 << 20
	maxReply     = 8 << 20
	upstreamRefs = "refs/factory-push-guard/upstream/"
)

type receiver struct {
	repo, index, upstream string
	busy                  chan struct{}
}

func newReceiver(repo, index, upstream string) *receiver {
	return &receiver{repo: repo, index: index, upstream: upstream, busy: make(chan struct{}, 1)}
}

func (h *receiver) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Cache-Control", "no-store")
	discover := r.Method == http.MethodGet && r.URL.EscapedPath() == route+"/info/refs" &&
		r.URL.RawQuery == "service=git-receive-pack"
	push := r.Method == http.MethodPost && r.URL.EscapedPath() == route+"/git-receive-pack" &&
		r.URL.RawQuery == ""
	if !discover && !push {
		http.NotFound(w, r)
		return
	}
	user, token, ok := r.BasicAuth()
	if !ok || user != "x-access-token" || token == "" || len(token) > 512 ||
		strings.IndexFunc(token, func(c rune) bool { return c < 33 || c > 126 }) != -1 {
		w.Header().Set("WWW-Authenticate", `Basic realm="factory-push-guard"`)
		http.Error(w, "binding credential required", http.StatusUnauthorized)
		return
	}

	select {
	case h.busy <- struct{}{}:
		defer func() { <-h.busy }()
	default:
		http.Error(w, "receiver busy", http.StatusServiceUnavailable)
		return
	}

	var body *os.File
	if push {
		if r.Header.Get("Content-Type") != "application/x-git-receive-pack-request" {
			http.Error(w, "expected a Git receive-pack request", http.StatusUnsupportedMediaType)
			return
		}
		var err error
		body, err = savePack(w, r)
		if err != nil {
			var tooLarge *http.MaxBytesError
			if errors.As(err, &tooLarge) {
				http.Error(w, "Git pack exceeds the receiver limit", http.StatusRequestEntityTooLarge)
			} else {
				http.Error(w, "could not read the Git pack", http.StatusBadRequest)
			}
			return
		}
		defer os.Remove(body.Name())
		defer body.Close()
	}

	ctx, stop := context.WithTimeout(r.Context(), 120*time.Second)
	defer stop()
	authorization := r.Header.Get("Authorization")
	if push {
		fetch := h.gitCommand(ctx, authorization, "--git-dir="+h.repo, "fetch",
			"--no-tags", "--no-recurse-submodules", "--no-auto-maintenance", "--no-write-fetch-head",
			"--prune", "--", h.upstream, "+refs/heads/*:"+upstreamRefs+"*")
		if err := fetch.Run(); err != nil {
			http.Error(w, "could not refresh public Git refs", http.StatusBadGateway)
			return
		}
	}

	args := []string{"-c", fmt.Sprintf("receive.maxInputSize=%d", maxPack),
		"-c", "receive.hideRefs=" + upstreamRefs, "receive-pack", "--stateless-rpc"}
	if discover {
		args = append(args, "--advertise-refs")
	}
	args = append(args, h.repo)
	cmd := h.gitCommand(ctx, authorization, args...)
	if body != nil {
		cmd.Stdin = body
	}
	var reply cappedReply
	cmd.Stdout = &reply
	if err := cmd.Run(); err != nil {
		http.Error(w, "Git receiver failed", http.StatusBadGateway)
		return
	}

	if discover {
		w.Header().Set("Content-Type", "application/x-git-receive-pack-advertisement")
		banner := "# service=git-receive-pack\n"
		fmt.Fprintf(w, "%04x%s0000", len(banner)+4, banner)
	} else {
		w.Header().Set("Content-Type", "application/x-git-receive-pack-result")
	}
	_, _ = w.Write(reply.Bytes())
}

func (h *receiver) gitCommand(ctx context.Context, authorization string, args ...string) *exec.Cmd {
	cmd := exec.CommandContext(ctx, "/usr/bin/git", append([]string{"--no-replace-objects"}, args...)...)
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	cmd.Cancel = func() error {
		err := syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
		if errors.Is(err, syscall.ESRCH) {
			return os.ErrProcessDone
		}
		return err
	}
	cmd.WaitDelay = time.Second
	// Keep the injected credential out of process arguments, logs, and disk.
	// The upstream is fixed and Git will refuse redirects.
	cmd.Env = []string{
		"PATH=/usr/bin:/bin", "HOME=/nonexistent", "LANG=C",
		"GIT_CONFIG_NOSYSTEM=1", "GIT_TERMINAL_PROMPT=0", "GIT_NO_REPLACE_OBJECTS=1",
		"MANDOLINE_INDEX=" + h.index, "PUSH_UPSTREAM=" + h.upstream,
		"GIT_CONFIG_COUNT=2",
		"GIT_CONFIG_KEY_0=http.https://github.com/.extraHeader",
		"GIT_CONFIG_VALUE_0=Authorization: " + authorization,
		"GIT_CONFIG_KEY_1=http.followRedirects", "GIT_CONFIG_VALUE_1=false",
	}
	return cmd
}

type cappedReply struct{ bytes.Buffer }

func (r *cappedReply) Write(b []byte) (int, error) {
	if len(b) > maxReply-r.Len() {
		return 0, errors.New("Git response is too large")
	}
	return r.Buffer.Write(b)
}

func savePack(w http.ResponseWriter, r *http.Request) (*os.File, error) {
	if r.ContentLength > maxPack {
		return nil, &http.MaxBytesError{Limit: maxPack}
	}
	f, err := os.CreateTemp("", "factory-git-pack-")
	if err != nil {
		return nil, err
	}
	if _, err = io.Copy(f, http.MaxBytesReader(w, r.Body, maxPack)); err == nil {
		_, err = f.Seek(0, io.SeekStart)
	}
	if err == nil {
		return f, nil
	}
	f.Close()
	os.Remove(f.Name())
	return nil, err
}
