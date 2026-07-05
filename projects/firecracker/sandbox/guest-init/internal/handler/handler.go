// Package handler executes one untrusted Python snippet per request inside
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
	// (uid 65532, ADR agents/044): the executed python subprocess drops to
	// this uid so untrusted code never runs as the guest-init root process.
	sandboxUID = 65532
	sandboxGID = 65532
)

// dropPrivileges controls whether the python subprocess's Credential is set
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

// ExecRequest is the /invoke/sandbox request body.
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

// Handle is the shim.Handler for the sandbox workload: decode one
// ExecRequest, run its Code as main.py under python3 in a fresh per-invoke
// workdir, and return a structured ExecResult. Malformed requests (an
// undecodable body) return a non-nil error; domain-invalid requests (empty
// code, a file path escaping the workdir, bad base64) return a 400-style
// structured ExecResult instead, per the shim's Handler contract.
func Handle(ctx context.Context, r *shim.Request) (*shim.Response, error) {
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

	// The workdir is created by this process (root, since PID 1 in a raw
	// Firecracker boot never drops privilege). The python subprocess drops to
	// sandboxUID/sandboxGID below, so it needs ownership here to read inputs
	// and write generated files.
	if dropPrivileges {
		if err := os.Chown(workdir, sandboxUID, sandboxGID); err != nil {
			return nil, fmt.Errorf("handler: chown workdir: %w", err)
		}
	}

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

	if err := os.WriteFile(filepath.Join(workdir, "main.py"), []byte(req.Code), 0o644); err != nil {
		return nil, fmt.Errorf("handler: write main.py: %w", err)
	}

	timeout := defaultTimeout
	if req.TimeoutSeconds > 0 {
		timeout = time.Duration(req.TimeoutSeconds) * time.Second
	}
	if timeout > maxTimeout {
		timeout = maxTimeout
	}

	result := runPython(ctx, workdir, timeout)

	files, filesTruncated, walkErr := collectOutputFiles(workdir, inputs)
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

// runPython execs `python3 main.py` in workdir with a hard wall-clock
// timeout, capped stdout/stderr, and dropped privileges (unless disabled for
// tests). It always returns a populated ExecResult; execution failures land
// in ExecResult.Error/ExitCode rather than as a Go error, per the shim
// contract that user-code failures are results, not handler errors.
func runPython(ctx context.Context, workdir string, timeout time.Duration) ExecResult {
	execCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	var stdout, stderr capBuffer
	stdout.limit = stdoutCap
	stderr.limit = stderrCap

	cmd := exec.CommandContext(execCtx, "python3", "main.py")
	cmd.Dir = workdir
	// A raw Firecracker boot hands PID 1 no environment (the kernel ignores
	// the OCI image config), and this handler runs as a descendant of that
	// PID 1, so nothing useful is inherited. Set everything the python
	// subprocess needs explicitly. HOME/TMPDIR point at workdir because it
	// must be writable (the rootfs HOME baked in apko.yaml is read-only), and
	// pinning TMPDIR there keeps any tempfile use inside user code within the
	// caps-enforced, cleaned-up-on-return working directory.
	cmd.Env = []string{
		"PATH=/usr/bin:/bin:/usr/local/bin",
		"HOME=" + workdir,
		"MPLBACKEND=Agg",
		"PYTHONUNBUFFERED=1",
		"TMPDIR=" + workdir,
	}
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	if dropPrivileges {
		cmd.SysProcAttr.Credential = &syscall.Credential{Uid: sandboxUID, Gid: sandboxGID}
	}
	// Setpgid above puts the child in its own process group. On a timeout,
	// exec.CommandContext's default Cancel only kills the direct python3
	// process; overriding it to kill the negative pid takes any children
	// python3 forked (a runaway script's subprocesses) down with it too.
	cmd.Cancel = func() error {
		if cmd.Process == nil {
			return nil
		}
		return syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
	}

	start := time.Now()
	runErr := cmd.Run()
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
// file except main.py and any input file whose content is unchanged from
// what Handle wrote. An input the script modified is not "unchanged" and IS
// returned, since the caller likely wants to see the edit. Per-file and
// total byte caps apply to the returned set; either one being hit sets
// truncated, and the file that tripped the cap is simply omitted (not
// partially included).
func collectOutputFiles(workdir string, inputs map[string]inputRecord) ([]ExecFile, bool, error) {
	var (
		files     []ExecFile
		total     int64
		truncated bool
	)
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
		if rel == "main.py" {
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
