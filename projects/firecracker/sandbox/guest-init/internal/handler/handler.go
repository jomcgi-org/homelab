// Package handler executes one untrusted language snippet per request inside
// the sandbox guest (ADR agents/044). The security boundary is the microVM,
// not this process; the caps below exist to keep responses inside the
// fc-invoke 8 MiB body budget and to fail fast on runaway code.
package handler

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"github.com/jomcgi/homelab/projects/firecracker/substrate/shim"
)

const (
	stdoutCap    = 512 << 10
	stderrCap    = 128 << 10
	perFileCap   = 2 << 20
	totalFileCap = 5 << 20

	defaultTimeout = 20 * time.Second
	maxTimeout     = 25 * time.Second // below the workload requestTimeout (30s)

	// sandboxUID/sandboxGID match the sandbox apko.yaml `sandbox` account
	// (uid 65532, ADR agents/044): the executed subprocess drops to
	// this uid so untrusted code never runs as the guest-init root process.
	sandboxUID = 65532
	sandboxGID = 65532
)

// MPLConfigDir is matplotlib's config/cache directory, set as MPLCONFIGDIR on
// every python exec. It sits on the /tmp tmpfs OUTSIDE any per-invoke workdir
// so the font cache matplotlib builds is never collected as an output file.
// cmd/main.go creates it and pre-populates the font cache during warm-up,
// so the warm-base snapshot carries it and per-call plotting is both quiet and
// fast (no first-use font scan).
const MPLConfigDir = "/tmp/mplconfig"

// dropPrivileges controls whether the subprocess's Credential is set
// to sandboxUID/sandboxGID. It defaults on: production always executes
// untrusted code as uid 65532, never as the guest-init root process.
// handler_test.go flips this off for its exec-dependent tests, because a
// non-root test runner cannot set an arbitrary Credential (that requires
// CAP_SETUID) and cmd.Start would fail with "operation not permitted".
// cmd/main.go never touches this var.
var dropPrivileges = true

// ExecFile is one file in an ExecRequest.Files list or an ExecResult.Files
// list: base64-encoded content plus a path relative to the execution working
// directory.
type ExecFile struct {
	Path       string `json:"path"`
	ContentB64 string `json:"content_b64"`
}

// ExecRequest is the /invoke/sandbox request body. Code runs in a fresh,
// discarded per-invoke workdir with no state carried to the next call.
type ExecRequest struct {
	Code           string     `json:"code"`
	Files          []ExecFile `json:"files,omitempty"`
	TimeoutSeconds int        `json:"timeout_seconds,omitempty"`
}

// ExecResult is the /invoke/sandbox response body. Timeout, a nonzero exit
// code, and output truncation are all represented here rather than as
// handler errors: only a malformed request causes Handle to return a non-nil
// error, which the shim maps to HTTP 502.
type ExecResult struct {
	Stdout     string     `json:"stdout"`
	Stderr     string     `json:"stderr"`
	ExitCode   int        `json:"exit_code"`
	Files      []ExecFile `json:"files,omitempty"`
	DurationMs int64      `json:"duration_ms"`
	Truncated  bool       `json:"truncated,omitempty"`
	Error      string     `json:"error,omitempty"`
}

// New binds a language Spec to the shim handler once at guest startup.
func New(spec Spec) shim.Handler {
	return func(ctx context.Context, r *shim.Request) (*shim.Response, error) {
		return Handle(ctx, r, spec)
	}
}

// Handle decodes one ExecRequest, runs its Code using spec in a fresh per-invoke
// workdir, and return a structured ExecResult. Malformed requests (an
// undecodable body) return a non-nil error; domain-invalid requests (empty
// code, a file path escaping the workdir, bad base64) return a 400-style
// structured ExecResult instead, per the shim's Handler contract.
func Handle(ctx context.Context, r *shim.Request, spec Spec) (*shim.Response, error) {
	var req ExecRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		return nil, fmt.Errorf("handler: decode exec request: %w", err)
	}
	if strings.TrimSpace(req.Code) == "" {
		return badRequest("code is required")
	}

	workdir, err := os.MkdirTemp("/tmp", "sandbox-exec-")
	if err != nil {
		return nil, fmt.Errorf("handler: create workdir: %w", err)
	}
	defer os.RemoveAll(workdir) // nosemgrep: no-bare-error-return

	// Ownership of workdir and everything written into it is fixed up in one
	// chownTree pass after all files exist (below), so the subprocess,
	// which drops to sandboxUID/sandboxGID, owns its whole working directory.

	inputs := map[string]inputRecord{}
	for _, f := range req.Files {
		if !filepath.IsLocal(f.Path) {
			return badRequest(fmt.Sprintf("file path escapes the working directory: %s", f.Path))
		}
		data, decErr := base64.StdEncoding.DecodeString(f.ContentB64)
		if decErr != nil {
			return badRequest(fmt.Sprintf("invalid base64 content for %s: %v", f.Path, decErr))
		}
		full := filepath.Join(workdir, f.Path)
		if err := os.MkdirAll(filepath.Dir(full), 0o755); err != nil {
			return nil, fmt.Errorf("handler: create parent dir for %s: %w", f.Path, err)
		}
		if err := os.WriteFile(full, data, 0o644); err != nil {
			return nil, fmt.Errorf("handler: write input file %s: %w", f.Path, err)
		}
		inputs[filepath.ToSlash(f.Path)] = inputRecord{size: int64(len(data)), hash: sha256.Sum256(data)}
	}

	if err := os.WriteFile(filepath.Join(workdir, spec.SourceFile), []byte(req.Code), 0o644); err != nil {
		return nil, fmt.Errorf("handler: write %s: %w", spec.SourceFile, err)
	}
	if spec.Prepare != nil {
		if err := spec.Prepare(workdir); err != nil {
			return nil, fmt.Errorf("handler: prepare %s workdir: %w", spec.Name, err)
		}
	}

	// Everything under workdir was created by this (root) process, so the
	// files and any nested parent dirs written above are owned root:root and
	// the subprocess (uid 65532) sees them only as "other": read-only. A
	// script that rewrites an input in place (open(path, "w"), df.to_csv) or
	// creates a sibling in a nested input dir would then hit PermissionError.
	// Chowning workdir alone is not enough because MkdirAll and WriteFile
	// created new inodes under it; walk the tree and chown every entry so the
	// subprocess owns its whole working directory. Gated by dropPrivileges:
	// tests run as a non-root user that cannot chown to an arbitrary uid.
	if dropPrivileges {
		if err := chownTree(workdir); err != nil {
			return nil, fmt.Errorf("handler: chown workdir tree: %w", err)
		}
	}

	timeout := defaultTimeout
	if req.TimeoutSeconds > 0 {
		timeout = time.Duration(req.TimeoutSeconds) * time.Second
	}
	if timeout > maxTimeout {
		timeout = maxTimeout
	}

	result := runSnippet(ctx, spec, workdir, timeout)

	files, filesTruncated, walkErr := collectOutputFiles(workdir, inputs, spec.ExcludeOutputs)
	if walkErr != nil && result.Error == "" {
		// A walk failure loses generated-file output, but the stdout/stderr/exit
		// code the caller most likely wants are still valid; surface it as a
		// result-level note rather than discarding everything as a 502.
		result.Error = fmt.Sprintf("collecting output files: %v", walkErr)
	}
	result.Files = files
	result.Truncated = result.Truncated || filesTruncated

	body, err := json.Marshal(result)
	if err != nil {
		return nil, fmt.Errorf("handler: marshal result: %w", err)
	}
	return &shim.Response{Status: http.StatusOK, Body: body}, nil
}

// chownTree recursively chowns root and every entry beneath it to
// sandboxUID/sandboxGID, so the subprocess (which drops to that uid)
// owns its whole working directory: the workdir itself, the source and
// input files root wrote into it, and any nested parent dirs MkdirAll
// created. Without this, those inodes stay root-owned and the subprocess,
// being "other", can only read them, so an in-place rewrite of an input
// (open(path, "w")) or a new sibling in a nested input dir fails with
// PermissionError. Called only under dropPrivileges (production); a non-root
// test runner cannot chown to an arbitrary uid.
func chownTree(root string) error {
	return filepath.WalkDir(root, func(path string, _ fs.DirEntry, err error) error {
		if err != nil {
			return fmt.Errorf("handler: walk %s: %w", path, err)
		}
		if err := os.Chown(path, sandboxUID, sandboxGID); err != nil {
			return fmt.Errorf("handler: chown %s: %w", path, err)
		}
		return nil
	})
}

// badRequest returns a structured ExecResult error at HTTP 400: malformed
// domain input (empty code, a path escaping the workdir, bad base64), as
// opposed to an undecodable request body, which is a handler error mapped to
// HTTP 502 by the shim.
func badRequest(msg string) (*shim.Response, error) {
	body, err := json.Marshal(ExecResult{Error: msg, ExitCode: -1})
	if err != nil {
		return nil, fmt.Errorf("handler: marshal bad request: %w", err)
	}
	return &shim.Response{Status: http.StatusBadRequest, Body: body}, nil
}

// inputRecord remembers an input file's original size and content hash so
// the post-run walk can tell an unchanged input apart from a file the script
// regenerated with different content (which IS returned as output).
type inputRecord struct {
	size int64
	hash [32]byte
}

// runSnippet compiles when needed and then executes a snippet in workdir. Both
// stages share one hard wall-clock timeout and capped stdout/stderr buffers.
// Execution failures are results, not handler errors, per the shim contract.
func runSnippet(ctx context.Context, spec Spec, workdir string, timeout time.Duration) ExecResult {
	execCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	var stdout, stderr capBuffer
	stdout.limit = stdoutCap
	stderr.limit = stderrCap

	start := time.Now()
	var runErr error
	if len(spec.Compile) > 0 {
		runErr = runCommand(execCtx, spec, workdir, spec.Compile, &stdout, &stderr)
	}
	if runErr == nil {
		runErr = runCommand(execCtx, spec, workdir, spec.Run, &stdout, &stderr)
	}
	duration := time.Since(start)

	result := ExecResult{
		Stdout:     stdout.String(),
		Stderr:     stderr.String(),
		DurationMs: duration.Milliseconds(),
		Truncated:  stdout.truncated || stderr.truncated,
	}

	switch {
	case errors.Is(execCtx.Err(), context.DeadlineExceeded):
		result.ExitCode = -1
		result.Error = fmt.Sprintf("timed out after %ds", int(timeout.Seconds()))
	case runErr == nil:
		result.ExitCode = 0
	default:
		var exitErr *exec.ExitError
		if errors.As(runErr, &exitErr) {
			result.ExitCode = exitErr.ExitCode()
		} else {
			result.ExitCode = -1
			result.Error = runErr.Error()
		}
	}
	return result
}

// runCommand executes one argv inside the snippet process group. Setpgid and
// the custom Cancel ensure a timeout kills descendants as well as the direct
// compiler or runtime process.
func runCommand(ctx context.Context, spec Spec, workdir string, argv []string, stdout, stderr *capBuffer) error {
	if len(argv) == 0 {
		return errors.New("language spec has no command")
	}
	// Immediately before the exec that depends on it, not once at startup far
	// from here. exec.CommandContext resolves argv[0] against THIS process's
	// PATH, and a guest whose PID 1 inherits no environment resolves nothing.
	// A no-op once PATH is set, so calling it per command costs a getenv and
	// cannot be left behind by a refactor of main.
	if err := EnsureSearchPath(); err != nil {
		return fmt.Errorf("ensure sandbox search path: %w", err)
	}
	cmd := exec.CommandContext(ctx, argv[0], argv[1:]...)
	cmd.Dir = workdir
	cmd.Env = spec.Environment(workdir)
	cmd.Stdout = stdout
	cmd.Stderr = stderr
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	if dropPrivileges {
		cmd.SysProcAttr.Credential = &syscall.Credential{Uid: sandboxUID, Gid: sandboxGID}
	}
	cmd.Cancel = func() error {
		if cmd.Process == nil {
			return nil
		}
		return syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
	}
	return cmd.Run()
}

// capBuffer is an io.Writer that keeps at most limit bytes and sets
// truncated once more is written than that. It never errors the write: a
// write "failing" would abort the process's stdout/stderr entirely, whereas
// the desired behavior is to silently drop the overflow and report it via
// Truncated.
type capBuffer struct {
	limit     int
	buf       bytes.Buffer
	truncated bool
}

func (c *capBuffer) Write(p []byte) (int, error) {
	remaining := c.limit - c.buf.Len()
	if remaining <= 0 {
		c.truncated = true
		return len(p), nil
	}
	if len(p) > remaining {
		c.buf.Write(p[:remaining])
		c.truncated = true
	} else {
		c.buf.Write(p)
	}
	return len(p), nil
}

func (c *capBuffer) String() string { return c.buf.String() }

// collectOutputFiles walks workdir after execution and returns every regular
// file except spec-defined outputs and any input file whose content is unchanged
// from what Handle wrote. An input the script modified is not "unchanged" and IS
// returned, since the caller likely wants to see the edit. Per-file and
// total byte caps apply to the returned set; either one being hit sets
// truncated, and the file that tripped the cap is simply omitted (not
// partially included).
func collectOutputFiles(workdir string, inputs map[string]inputRecord, excludeOutputs []string) ([]ExecFile, bool, error) {
	var (
		files     []ExecFile
		total     int64
		truncated bool
	)
	excluded := make(map[string]struct{}, len(excludeOutputs))
	for _, path := range excludeOutputs {
		excluded[filepath.ToSlash(path)] = struct{}{}
	}
	err := filepath.WalkDir(workdir, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return fmt.Errorf("handler: walk %s: %w", path, err)
		}
		if d.IsDir() {
			return nil
		}
		rel, relErr := filepath.Rel(workdir, path)
		if relErr != nil {
			return nil // nosemgrep: no-bare-error-return
		}
		rel = filepath.ToSlash(rel)
		if _, ok := excluded[rel]; ok {
			return nil
		}
		info, infoErr := d.Info()
		if infoErr != nil || !info.Mode().IsRegular() {
			return nil
		}

		if orig, ok := inputs[rel]; ok && info.Size() == orig.size {
			if data, readErr := os.ReadFile(path); readErr == nil && sha256.Sum256(data) == orig.hash {
				return nil // unchanged input; not output
			}
		}

		if info.Size() > perFileCap {
			truncated = true
			return nil
		}
		if total+info.Size() > totalFileCap {
			truncated = true
			return nil
		}
		data, readErr := os.ReadFile(path)
		if readErr != nil {
			return nil // nosemgrep: no-bare-error-return
		}
		total += int64(len(data))
		files = append(files, ExecFile{Path: rel, ContentB64: base64.StdEncoding.EncodeToString(data)})
		return nil
	})
	return files, truncated, err
}
