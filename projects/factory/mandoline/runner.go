// Package mandoline runs the private analyzer against a pinned, clean checkout.
package mandoline

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"syscall"
	"time"
)

const (
	toolVersion = "0.25.0"
	runLimit    = 90 * time.Second
	outputLimit = 1 << 20
)

var fullSHA = regexp.MustCompile(`^[0-9a-f]{40}$`)

type Request struct {
	Command string
	Commit  string
	Base    string
}

type Envelope struct {
	Commit      string            `json:"commit"`
	ToolVersion string            `json:"tool_version"`
	PackDigests map[string]string `json:"pack_digests"`
	Status      string            `json:"status"`
	Gaps        []string          `json:"gaps"`
	Result      json.RawMessage   `json:"result"`
}

// Run takes the workspace and tool from the installed launcher. They are
// deliberately absent from the model-facing command-line arguments.
func Run(parent context.Context, workspace, cwd, tool string, audit bool, req Request) Envelope {
	result := Envelope{Commit: req.Commit, PackDigests: map[string]string{}, Gaps: []string{}}
	if !fullSHA.MatchString(req.Commit) || (!audit && !fullSHA.MatchString(req.Base)) || (audit && req.Base != "") {
		return result.fail("error", "expected a full commit and the base required by this harness")
	}
	if req.Command != "audit" && req.Command != "surface" && (req.Command != "diff" || audit) {
		return result.fail("unsupported", "command outside the harness analysis scope")
	}
	ctx, cancel := context.WithTimeout(parent, runLimit)
	defer cancel()

	resolvedWorkspace, err := filepath.EvalSymlinks(workspace)
	if err != nil || resolvedWorkspace != workspace {
		return result.fail("error", "task workspace unavailable")
	}
	cwd, err = filepath.EvalSymlinks(cwd)
	if err != nil || !beneath(workspace, cwd) {
		return result.fail("error", "working directory is outside the task workspace")
	}
	cache, err := openCache(workspace)
	if err != nil {
		return result.fail("error", "temporary cache unavailable")
	}
	home, err := os.MkdirTemp(cache.directory, "run-")
	if err != nil {
		return result.fail("error", "temporary analysis directory unavailable")
	}
	defer os.RemoveAll(home)
	env := []string{
		"PATH=/usr/bin:/bin", "LANG=C", "HOME=" + home, "TMPDIR=" + home,
		"GIT_CONFIG_NOSYSTEM=1", "GIT_CONFIG_COUNT=2",
		"GIT_CONFIG_KEY_0=core.hooksPath", "GIT_CONFIG_VALUE_0=/dev/null",
		"GIT_CONFIG_KEY_1=core.fsmonitor", "GIT_CONFIG_VALUE_1=false",
		"GIT_OPTIONAL_LOCKS=0", "GIT_NO_REPLACE_OBJECTS=1",
	}
	root, tree, err := verifyCheckout(ctx, workspace, cwd, req, env)
	if err != nil {
		return result.fail("error", err.Error())
	}
	if _, err := os.Lstat(filepath.Join(root, ".mandoline")); !errors.Is(err, os.ErrNotExist) {
		return result.fail("unsupported", "checkout-defined Mandoline configuration or packs")
	}
	digest, err := hashFile(tool)
	if errors.Is(err, os.ErrNotExist) {
		return result.fail("missing_dependency", "Mandoline executable unavailable")
	}
	if err != nil {
		return result.fail("error", "cannot verify the Mandoline executable")
	}
	version := capture(ctx, tool, root, env, []string{"--version"})
	if version.err != nil || !strings.HasPrefix(strings.TrimSpace(string(version.stdout)), "mandoline "+toolVersion+" ") {
		return result.fail("error", "expected the pinned Mandoline version")
	}
	result.ToolVersion = toolVersion
	// All active packs are bundled in these verified executable bytes. The
	// sterile HOME and absence of checkout configuration exclude local packs.
	result.PackDigests["bundled"] = digest
	key := cacheKey(tree, digest, req)
	if cached, ok := cache.read(key, result); ok &&
		classify(result, req.Command, captured{stdout: cached.Result}).Status == "complete" {
		return cached
	}

	args := []string{req.Command, "--json", "--no-cache"}
	if !audit {
		// With a verified HEAD, --base uses that exact tree without a
		// temporary checkout and its Git checkout hooks and filters.
		args = append(args, "--base", req.Base)
	}
	output := capture(ctx, tool, root, env, args)
	if output.transient() && ctx.Err() == nil {
		output = capture(ctx, tool, root, env, args)
	}
	result = classify(result, req.Command, output)
	if result.Result == nil {
		return result
	}
	// Detect checkout changes that happened while analysis read the tree.
	if _, after, err := verifyCheckout(ctx, workspace, cwd, req, env); err != nil || tree != after {
		return result.fail("error", "checkout changed or could not be verified after analysis")
	}
	if result.Status == "complete" {
		cache.store(key, result)
	}
	return result
}

func (e Envelope) fail(status, reason string) Envelope {
	e.Status, e.Gaps, e.Result = status, []string{reason}, nil
	return e
}

func verifyCheckout(ctx context.Context, workspace, cwd string, req Request, env []string) (string, string, error) {
	git := func(dir string, args ...string) ([]byte, error) {
		data := capture(ctx, "/usr/bin/git", dir, env, args)
		return data.stdout, data.err
	}
	name, err := git(cwd, "rev-parse", "--show-toplevel")
	if err != nil {
		return "", "", errors.New("expected a Git checkout in the task workspace")
	}
	root, err := filepath.EvalSymlinks(strings.TrimSpace(string(name)))
	if err != nil || !beneath(workspace, root) {
		return "", "", errors.New("checkout is outside the task workspace")
	}
	identity, err := git(root, "rev-parse", "--verify", "HEAD")
	if err != nil || strings.TrimSpace(string(identity)) != req.Commit {
		return "", "", errors.New("checkout HEAD differs from the requested commit")
	}
	if req.Base != "" {
		if _, err := git(root, "merge-base", req.Base, req.Commit); err != nil {
			return "", "", errors.New("base must be a local commit with a common ancestor")
		}
	}
	index, err := git(root, "ls-files", "-v", "-z")
	if err != nil {
		return "", "", errors.New("cannot verify checkout index")
	}
	for entry := range bytes.SplitSeq(index, []byte{0}) {
		if len(entry) > 0 && (entry[0] == 'S' || (entry[0] >= 'a' && entry[0] <= 'z')) {
			return "", "", errors.New("index flags suppress checkout verification")
		}
	}
	entries, err := git(root, "ls-files", "--stage", "-z")
	if err != nil {
		return "", "", errors.New("cannot verify checkout paths")
	}
	for entry := range bytes.SplitSeq(entries, []byte{0}) {
		if bytes.HasPrefix(entry, []byte("120000 ")) {
			_, name, _ := bytes.Cut(entry, []byte{'\t'})
			target, err := filepath.EvalSymlinks(filepath.Join(root, string(name)))
			if err != nil || !beneath(root, target) {
				return "", "", errors.New("checkout symlink does not resolve inside the pinned tree")
			}
		}
	}
	dirty, err := git(root, "status", "--porcelain=v1", "--untracked-files=all", "--ignored=matching", "--ignore-submodules=none")
	if err != nil || len(dirty) > 0 {
		return "", "", errors.New("checkout must be clean")
	}
	tree, err := git(root, "rev-parse", req.Commit+"^{tree}")
	if err != nil {
		return "", "", errors.New("cannot verify checkout tree")
	}
	return root, strings.TrimSpace(string(tree)), nil
}

func beneath(root, path string) bool {
	rel, err := filepath.Rel(root, path)
	return err == nil && rel != ".." && !strings.HasPrefix(rel, "../")
}

func hashFile(path string) (string, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer f.Close()
	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil {
		return "", err
	}
	return "sha256:" + hex.EncodeToString(h.Sum(nil)), nil
}

func classify(e Envelope, requested string, output captured) Envelope {
	switch {
	case output.truncated:
		return e.fail("truncated", "output exceeded the byte limit")
	case errors.Is(output.err, context.DeadlineExceeded):
		return e.fail("timeout", "analysis exceeded the wall-clock limit")
	case errors.Is(output.err, os.ErrNotExist):
		return e.fail("missing_dependency", "analysis executable unavailable")
	}
	var data struct {
		Command  string            `json:"command"`
		Version  int               `json:"schemaVersion"`
		Gaps     []json.RawMessage `json:"gaps"`
		Warnings []struct {
			Code string `json:"code"`
		} `json:"warnings"`
	}
	want := requested
	if requested == "diff" {
		want = "slice"
	}
	if err := json.Unmarshal(output.stdout, &data); err != nil || data.Command != want || data.Version != 1 {
		return e.fail("error", "analysis did not return its expected JSON result")
	}
	if output.err != nil {
		return e.fail("error", "analysis exited unsuccessfully")
	}
	e.Status, e.Result = "complete", json.RawMessage(output.stdout)
	for _, warning := range data.Warnings {
		e.Gaps = append(e.Gaps, "analysis warning: "+warning.Code)
	}
	if len(data.Gaps) > 0 {
		e.Gaps = append(e.Gaps, fmt.Sprintf("%d analysis coverage gaps", len(data.Gaps)))
	}
	for line := range strings.SplitSeq(string(output.stderr), "\n") {
		if strings.Contains(line, " WARN ") && !strings.Contains(line, "Slice produced 0 callees beyond entry point(s)") {
			e.Gaps = append(e.Gaps, "analysis reported a warning on stderr")
			break
		}
	}
	if len(e.Gaps) > 0 {
		e.Status = "partial"
	}
	return e
}

type captured struct {
	stdout, stderr []byte
	err            error
	truncated      bool
}

func (c captured) transient() bool {
	return errors.Is(c.err, syscall.EAGAIN) || errors.Is(c.err, syscall.ETXTBSY)
}
