package handler

// kernel.go implements the session (kernel) execution mode for the sandbox
// guest (EmberVM R2, ADR embervm/001). Where the one-shot path (handler.go)
// runs each snippet as a fresh, discarded python3 process, session mode keeps
// ONE persistent python3 child alive and executes every snippet in its shared
// module namespace under a stable /tmp/session workdir, so variables, imports,
// and files accrete across an agent's turns.
//
// Protocol: the Go parent and the python child speak a length-prefixed frame
// protocol over the child's stdin/stdout. Each frame is a 4-byte big-endian
// uint32 length followed by exactly that many JSON bytes. The parent writes a
// request frame ({"code": ...}); the child executes it and writes back a
// response frame ({stdout, stderr, exit_code, files, ...}). stderr on the
// child is left connected to the parent's stderr for crash diagnostics and is
// NOT part of the protocol stream. Length-prefix framing (not newline
// delimiting) is what makes the reader robust to partial reads and to snippet
// output that itself contains newlines or NULs.
//
// Timeout: the PARENT owns the per-snippet wall-clock. It cannot be enforced
// inside the child, because a mid-exec interrupt could leave the shared
// namespace half-mutated; so on timeout the parent KILLS the child (its whole
// process group) and lazily restarts it on the next request. The namespace is
// lost, which is reported as SessionReset on that response. This is exactly
// the "a snippet timeout kills and restarts the child" rule in the plan, and
// it keeps the loop unwedgeable: one runaway snippet costs its own state, not
// the server.

import (
	"bufio"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"sync"
	"syscall"
	"time"

	"github.com/jomcgi/homelab/projects/firecracker/substrate/shim"
)

const (
	// sessionWorkdir is the stable working directory the persistent child cds
	// into. Unlike the one-shot per-invoke workdir it is NEVER removed between
	// snippets: files written in one snippet are visible to the next and are
	// returned as changed-file output. It lives on the /tmp tmpfs (RAM), so it
	// is captured in a session bank snapshot along with the child's memory.
	sessionWorkdir = "/tmp/session"

	// maxFrameBytes caps a single protocol frame. Both directions honor it: the
	// child truncates its captured stdout/stderr and drops oversized files (the
	// same caps as one-shot) so its response frame stays well under this, and
	// the parent refuses to read a frame claiming to be larger (a corrupt or
	// hostile child must not be able to make the parent allocate unbounded).
	maxFrameBytes = 16 << 20
)

// sessionKernel is the single persistent python child for this guest. One
// microVM serves exactly one session (the control plane binds a session to a
// VM and serializes its invokes), so a package-level singleton guarded by mu
// is the whole concurrency model: mu also serializes any two session requests
// that race, which the daemon guard already prevents but which must never
// corrupt the frame stream if it did not.
var sessionKernel = &kernel{}

type kernel struct {
	mu    sync.Mutex
	cmd   *exec.Cmd
	stdin io.WriteCloser
	// r wraps the child stdout; buffered so a frame that arrives in several
	// vsock/pipe reads is reassembled correctly (partial-read robustness).
	r *bufio.Reader
}

// kernelRequest is one request frame written to the child.
type kernelRequest struct {
	Code string `json:"code"`
}

// kernelResponse is one response frame read from the child. It mirrors the
// one-shot ExecResult fields the caller consumes; DurationMs and SessionReset
// are filled in by the parent, not the child.
type kernelResponse struct {
	Stdout    string     `json:"stdout"`
	Stderr    string     `json:"stderr"`
	ExitCode  int        `json:"exit_code"`
	Files     []ExecFile `json:"files"`
	Truncated bool       `json:"truncated"`
	Error     string     `json:"error"`
}

// handleSession serves one session-mode request against the persistent kernel.
// It returns the same ExecResult shape as the one-shot path plus SessionReset
// when the shared namespace was lost. Only a transport-level failure it cannot
// recover from returns a non-nil error (mapped to 502 by the shim); a python
// error, a nonzero exit, or a timeout are results, exactly as one-shot.
func handleSession(req ExecRequest) (*shim.Response, error) {
	timeout := defaultTimeout
	if req.TimeoutSeconds > 0 {
		timeout = time.Duration(req.TimeoutSeconds) * time.Second
	}
	if timeout > maxTimeout {
		timeout = maxTimeout
	}

	result, err := sessionKernel.run(req.Code, timeout)
	if err != nil {
		return nil, err // nosemgrep: no-bare-error-return
	}
	body, marshalErr := json.Marshal(result)
	if marshalErr != nil {
		return nil, fmt.Errorf("handler: marshal session result: %w", marshalErr)
	}
	return &shim.Response{Status: http.StatusOK, Body: body}, nil
}

// run executes one snippet. It lazily (re)starts the child, sends the code
// frame, and waits up to timeout for the response frame. On timeout, or on any
// framing/transport error, it kills the child so the next call starts fresh
// and reports SessionReset. A started boolean tracks whether THIS call had to
// start the child (the very first session request, or the request right after
// a reset): that is also reported as SessionReset because a fresh child has an
// empty namespace, so the model never assumes carried state it does not have.
func (k *kernel) run(code string, timeout time.Duration) (ExecResult, error) {
	k.mu.Lock()
	defer k.mu.Unlock()

	started, err := k.ensureChild()
	if err != nil {
		return ExecResult{}, fmt.Errorf("handler: start session child: %w", err)
	}

	start := time.Now()
	resp, runErr := k.exchange(code, timeout)
	duration := time.Since(start)
	if runErr != nil {
		// Kill so the next request starts a clean child; the namespace is gone.
		k.kill()
		if isTimeout(runErr) {
			return ExecResult{
				ExitCode:     -1,
				Error:        fmt.Sprintf("timed out after %ds", int(timeout.Seconds())),
				DurationMs:   duration.Milliseconds(),
				SessionReset: true,
			}, nil
		}
		// A framing or transport failure: report it, reset next time.
		return ExecResult{
			ExitCode:     -1,
			Error:        fmt.Sprintf("session child failed: %v", runErr),
			DurationMs:   duration.Milliseconds(),
			SessionReset: true,
		}, nil
	}

	return ExecResult{
		Stdout:       resp.Stdout,
		Stderr:       resp.Stderr,
		ExitCode:     resp.ExitCode,
		Files:        resp.Files,
		Truncated:    resp.Truncated,
		Error:        resp.Error,
		DurationMs:   duration.Milliseconds(),
		SessionReset: started,
	}, nil
}

// ensureChild starts the persistent python child if it is not already running.
// It returns whether it started a NEW child on this call (reported to the
// caller as SessionReset: a new child has an empty namespace). Caller holds mu.
func (k *kernel) ensureChild() (bool, error) {
	if k.cmd != nil && k.cmd.Process != nil {
		return false, nil
	}

	if err := os.MkdirAll(sessionWorkdir, 0o755); err != nil {
		return false, fmt.Errorf("mkdir %s: %w", sessionWorkdir, err)
	}

	cmd := exec.Command("python3", "-u", "-c", kernelScript)
	cmd.Dir = sessionWorkdir
	// Same explicit environment the one-shot path sets (a raw Firecracker boot
	// hands PID 1 nothing), with HOME/TMPDIR pinned INSIDE the session workdir
	// so tempfile use and any $HOME writes accrete under the banked tmpfs.
	cmd.Env = []string{
		"PATH=/usr/bin:/bin:/usr/local/bin",
		"HOME=" + sessionWorkdir,
		"MPLBACKEND=Agg",
		"PYTHONUNBUFFERED=1",
		"TMPDIR=" + sessionWorkdir,
		"MPLCONFIGDIR=" + MPLConfigDir,
		"PYTHONPATH=/opt/sandbox",
	}
	cmd.Stderr = os.Stderr // crash diagnostics only; NOT the protocol stream.
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	if dropPrivileges {
		cmd.SysProcAttr.Credential = &syscall.Credential{Uid: sandboxUID, Gid: sandboxGID}
		// The child owns its whole workdir so uid 65532 can write into it.
		if err := chownTree(sessionWorkdir); err != nil {
			return false, fmt.Errorf("chown %s: %w", sessionWorkdir, err)
		}
	}

	stdin, err := cmd.StdinPipe()
	if err != nil {
		return false, fmt.Errorf("stdin pipe: %w", err)
	}
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return false, fmt.Errorf("stdout pipe: %w", err)
	}
	if err := cmd.Start(); err != nil {
		return false, fmt.Errorf("start python child: %w", err)
	}

	k.cmd = cmd
	k.stdin = stdin
	k.r = bufio.NewReaderSize(stdout, 64<<10)
	return true, nil
}

// exchange writes one request frame and reads one response frame, enforcing
// timeout on the READ (the snippet's wall clock). A timeout returns a sentinel
// the caller distinguishes with isTimeout. Caller holds mu.
func (k *kernel) exchange(code string, timeout time.Duration) (kernelResponse, error) {
	reqBody, err := json.Marshal(kernelRequest{Code: code})
	if err != nil {
		return kernelResponse{}, fmt.Errorf("marshal request: %w", err)
	}
	if err := writeFrame(k.stdin, reqBody); err != nil {
		return kernelResponse{}, fmt.Errorf("write request frame: %w", err)
	}

	type frameResult struct {
		body []byte
		err  error
	}
	done := make(chan frameResult, 1)
	go func() {
		body, readErr := readFrame(k.r)
		done <- frameResult{body: body, err: readErr}
	}()

	select {
	case <-time.After(timeout):
		return kernelResponse{}, errTimeout
	case fr := <-done:
		if fr.err != nil {
			return kernelResponse{}, fmt.Errorf("read response frame: %w", fr.err)
		}
		var resp kernelResponse
		if err := json.Unmarshal(fr.body, &resp); err != nil {
			return kernelResponse{}, fmt.Errorf("unmarshal response frame: %w", err)
		}
		return resp, nil
	}
}

// kill terminates the child and its process group and clears kernel state so
// the next request starts a fresh child. Caller holds mu. Safe to call when no
// child is running.
func (k *kernel) kill() {
	if k.cmd != nil && k.cmd.Process != nil {
		_ = syscall.Kill(-k.cmd.Process.Pid, syscall.SIGKILL)
		_, _ = k.cmd.Process.Wait()
	}
	if k.stdin != nil {
		_ = k.stdin.Close()
	}
	k.cmd = nil
	k.stdin = nil
	k.r = nil
}

// errTimeout is the sentinel returned by exchange on a read timeout.
var errTimeout = fmt.Errorf("session snippet timed out")

func isTimeout(err error) bool { return err == errTimeout } //nolint:errorlint // sentinel identity

// writeFrame writes a length-prefixed frame: a 4-byte big-endian length
// followed by body.
func writeFrame(w io.Writer, body []byte) error {
	var hdr [4]byte
	binary.BigEndian.PutUint32(hdr[:], uint32(len(body)))
	if _, err := w.Write(hdr[:]); err != nil {
		return err // nosemgrep: no-bare-error-return
	}
	if _, err := w.Write(body); err != nil {
		return err // nosemgrep: no-bare-error-return
	}
	return nil
}

// readFrame reads one length-prefixed frame. io.ReadFull reassembles a frame
// that arrives across several underlying reads (the partial-read robustness
// the vsock/pipe transport requires); a frame larger than maxFrameBytes is
// rejected rather than allocated.
func readFrame(r io.Reader) ([]byte, error) {
	var hdr [4]byte
	if _, err := io.ReadFull(r, hdr[:]); err != nil {
		return nil, err // nosemgrep: no-bare-error-return
	}
	n := binary.BigEndian.Uint32(hdr[:])
	if n > maxFrameBytes {
		return nil, fmt.Errorf("frame length %d exceeds cap %d", n, maxFrameBytes)
	}
	body := make([]byte, n)
	if _, err := io.ReadFull(r, body); err != nil {
		return nil, err // nosemgrep: no-bare-error-return
	}
	return body, nil
}

// kernelScript is the python exec-loop that runs as the persistent child. It
// reads length-prefixed request frames on stdin, executes each snippet's code
// in a SHARED module namespace (a single dict reused across snippets, so
// assignments and imports persist) with cwd already at the session workdir,
// captures stdout/stderr under the same caps as the one-shot handler, snapshots
// files that changed since the previous snippet, and writes a response frame on
// stdout. Its own stderr is the process's real stderr (crash diagnostics), so
// captured snippet stderr is redirected in-process and never pollutes the frame
// stream. A single bad snippet is caught and returned as a result; the loop
// never dies on user code.
const kernelScript = `
import base64, io, json, os, struct, sys, traceback, contextlib

STDOUT_CAP = 512 * 1024
STDERR_CAP = 128 * 1024
PER_FILE_CAP = 2 * 1024 * 1024
TOTAL_FILE_CAP = 5 * 1024 * 1024

# The real fds: framing is on these, snippet output is captured separately.
_in = sys.stdin.buffer
_out = sys.stdout.buffer

def read_frame():
    hdr = _read_exact(4)
    if hdr is None:
        return None
    (n,) = struct.unpack(">I", hdr)
    return _read_exact(n)

def _read_exact(n):
    buf = bytearray()
    while len(buf) < n:
        chunk = _in.read(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)

def write_frame(obj):
    body = json.dumps(obj).encode("utf-8")
    _out.write(struct.pack(">I", len(body)))
    _out.write(body)
    _out.flush()

def snapshot_files():
    # Path -> (size, mtime_ns) for every regular file under cwd, so the next
    # snapshot can tell what a snippet changed. main.py has no meaning here.
    seen = {}
    for root, _dirs, names in os.walk("."):
        for name in names:
            p = os.path.join(root, name)
            try:
                st = os.stat(p)
            except OSError:
                continue
            if os.path.isfile(p):
                seen[os.path.relpath(p, ".")] = (st.st_size, st.st_mtime_ns)
    return seen

def collect_changed(before):
    after = snapshot_files()
    files = []
    total = 0
    truncated = False
    for rel, meta in after.items():
        if before.get(rel) == meta:
            continue
        size = meta[0]
        if size > PER_FILE_CAP:
            truncated = True
            continue
        if total + size > TOTAL_FILE_CAP:
            truncated = True
            continue
        try:
            with open(rel, "rb") as f:
                data = f.read()
        except OSError:
            continue
        total += len(data)
        files.append({"path": rel.replace(os.sep, "/"),
                      "content_b64": base64.b64encode(data).decode("ascii")})
    return files, truncated

# The one shared namespace reused for every snippet: this is what makes state
# persist. __name__ is __main__ so scripts behave like a normal top-level run.
NS = {"__name__": "__main__", "__builtins__": __builtins__}

def run_snippet(code):
    out = io.StringIO()
    err = io.StringIO()
    exit_code = 0
    error = ""
    before = snapshot_files()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            compiled = compile(code, "<session>", "exec")
            exec(compiled, NS)
        except SystemExit as e:
            try:
                exit_code = int(e.code) if e.code is not None else 0
            except (TypeError, ValueError):
                exit_code = 1
        except BaseException:
            exit_code = 1
            traceback.print_exc()
    so = out.getvalue()
    se = err.getvalue()
    truncated = False
    if len(so) > STDOUT_CAP:
        so = so[:STDOUT_CAP]
        truncated = True
    if len(se) > STDERR_CAP:
        se = se[:STDERR_CAP]
        truncated = True
    files, files_truncated = collect_changed(before)
    return {"stdout": so, "stderr": se, "exit_code": exit_code,
            "files": files, "truncated": truncated or files_truncated,
            "error": error}

def main():
    while True:
        frame = read_frame()
        if frame is None:
            return
        try:
            req = json.loads(frame.decode("utf-8"))
            resp = run_snippet(req.get("code", ""))
        except Exception as e:  # framing/decoding fault: report, keep looping
            resp = {"stdout": "", "stderr": "", "exit_code": -1,
                    "files": [], "truncated": False,
                    "error": "kernel: %s" % e}
        write_frame(resp)

main()
`
