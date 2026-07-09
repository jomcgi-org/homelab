// Package scandriver drives the warm offline-Pro semgrep scan-server: a child
// process (osemgrep-pro mcp --experimental --pro) that speaks newline-delimited
// JSON over its stdio, one request -> one response.
//
// It replaces the former resident `semgrep lsp` + LSP JSON-RPC client. The old
// path could only run OSS analysis (semgrep lsp ignores SEMGREP_CORE_BIN and
// runs its in-process OSS engine), so cross-function taint never fired. The
// scan-server runs the Pro engine in-process: it takes file contents inline (no
// git target discovery, no didOpen/didCreateFiles dance, no per-file diagnostic
// settle windows) and returns standard `semgrep --json` cli_output.
package scandriver

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	"sync"

	"github.com/jomcgi/homelab/projects/firecracker/substrate/vsockproto"
)

// Driver owns one warm scan-server child process. A single stdio pipe carries
// every request, so scanMu serialises Scan calls (the shim server may invoke the
// handler concurrently; interleaved writes/reads on one pipe would corrupt the
// framing).
type Driver struct {
	cmd    *exec.Cmd
	stdin  io.WriteCloser
	stdout *bufio.Reader

	scanMu sync.Mutex
}

// Start launches the scan-server and blocks until it prints exactly one
// {"ready":true} line (per-language parsers warmed, rules compiled) — the point
// at which the host may snapshot the VM. bin is the osemgrep-pro path; it is
// invoked as `osemgrep-pro mcp --experimental --pro --session-id fc`.
// --experimental is REQUIRED: without it, `mcp` falls back to the Python MCP
// server. The child's lifetime is bound to ctx (cancel -> the process is
// killed). rulesDir and settingsFile are exported to the child as
// SEMGREP_SCAN_RULES and SEMGREP_SETTINGS_FILE.
func Start(ctx context.Context, bin, rulesDir, settingsFile string) (*Driver, error) {
	cmd := exec.CommandContext(ctx, bin, "mcp", "--experimental", "--pro", "--session-id", "fc")
	cmd.Env = append(os.Environ(),
		"SEMGREP_SCAN_RULES="+rulesDir,
		"SEMGREP_SETTINGS_FILE="+settingsFile,
	)
	cmd.Stderr = os.Stderr

	stdin, err := cmd.StdinPipe()
	if err != nil {
		return nil, err
	}
	stdoutPipe, err := cmd.StdoutPipe()
	if err != nil {
		return nil, err
	}
	if err := cmd.Start(); err != nil {
		return nil, err
	}

	d := &Driver{cmd: cmd, stdin: stdin, stdout: bufio.NewReaderSize(stdoutPipe, 1<<20)}

	// The scan-server prints one {"ready":true} line after warmup; ignore any
	// pre-ready log lines it emits first. A read error here means the child
	// exited before readying (e.g. a bad settings file that trips the Pro gate).
	for {
		line, err := d.stdout.ReadBytes('\n')
		if err != nil {
			return nil, fmt.Errorf("scan-server exited before ready: %w", err)
		}
		var p struct {
			Ready bool `json:"ready"`
		}
		if json.Unmarshal(line, &p) == nil && p.Ready {
			return d, nil
		}
	}
}

// Scan sends one batch of files and returns the normalised result. One request
// -> one response. Result paths are already rewritten by the scan-server back to
// the request's file values.
func (d *Driver) Scan(req vsockproto.ScanRequest) (vsockproto.ScanResult, error) {
	d.scanMu.Lock()
	defer d.scanMu.Unlock()

	files := make([]map[string]string, 0, len(req.Files))
	for _, f := range req.Files {
		files = append(files, map[string]string{"file": f.Path, "content": f.Content})
	}
	wire := map[string]any{
		"method":   "scanFiles",
		"files":    files,
		"git_info": map[string]string{"username": "", "repo": "", "branch": ""},
	}
	if err := json.NewEncoder(d.stdin).Encode(wire); err != nil { // Encode appends '\n'
		return vsockproto.ScanResult{}, err
	}

	line, err := d.stdout.ReadBytes('\n')
	if err != nil {
		return vsockproto.ScanResult{}, err
	}

	// Standard `semgrep --json` cli_output. Severity is already the
	// ERROR/WARNING/INFO string (no LSP 1-4 mapping); start.line/col are 1-based.
	var out struct {
		Results []struct {
			CheckID string `json:"check_id"`
			Path    string `json:"path"`
			Start   struct {
				Line int `json:"line"`
				Col  int `json:"col"`
			} `json:"start"`
			Extra struct {
				Message  string `json:"message"`
				Severity string `json:"severity"`
			} `json:"extra"`
		} `json:"results"`
		Errors []struct {
			Message string `json:"message"`
		} `json:"errors"`
	}
	if err := json.Unmarshal(line, &out); err != nil {
		return vsockproto.ScanResult{}, fmt.Errorf("decode cli_output: %w", err)
	}

	var res vsockproto.ScanResult
	for _, r := range out.Results {
		res.Findings = append(res.Findings, vsockproto.Finding{
			Path:     r.Path,
			Line:     r.Start.Line,
			Col:      r.Start.Col,
			RuleID:   r.CheckID,
			Severity: r.Extra.Severity,
			Message:  r.Extra.Message,
		})
	}
	for _, e := range out.Errors {
		if e.Message != "" {
			res.Errors = append(res.Errors, e.Message)
		}
	}
	return res, nil
}

// Close shuts the child down.
func (d *Driver) Close() error { _ = d.stdin.Close(); return d.cmd.Wait() }
