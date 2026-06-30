// Package lspdriver is a small JSON-RPC-over-stdio client that drives a resident
// `semgrep lsp` process. semgrep lsp loads and compiles the rule set once at
// startup and then scans opened documents in-process, so keeping it warm turns a
// per-scan multi-second cold start into a sub-second incremental scan. The driver
// owns that process, performs the initialize handshake, detects when the rules are
// compiled (WaitReady), and runs whole-file scans (Scan), translating each LSP
// diagnostic into a vsockproto.Finding.
//
// The byte-level seam is the Transport interface: production wraps the semgrep
// subprocess's stdin/stdout (Spawn), and tests wrap in-memory pipes driven by a
// fake LSP goroutine, so the unit tests never spawn real semgrep.
package lspdriver

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"github.com/jomcgi/homelab/projects/agent_platform/vsockproto"
)

// Transport is the stdio byte seam to the LSP server. Recv carries server->client
// bytes (the LSP stdout: responses and notifications); Send carries client->server
// bytes (the LSP stdin: requests and notifications); Close terminates it (killing
// the subprocess in production).
type Transport interface {
	Recv() io.Reader
	Send() io.Writer
	Close() error
}

// Driver is a resident JSON-RPC client for one `semgrep lsp` process.
type Driver struct {
	t         Transport
	workspace string

	writeMu sync.Mutex // serialises framed writes (request sender + read-loop replies)
	w       io.Writer

	mu      sync.Mutex // guards nextID + pending
	nextID  int
	pending map[int]chan rpcResponse

	docMu  sync.Mutex // guards opened
	opened map[string]int

	scanMu sync.Mutex // serialises Scan calls (one warm process, deterministic versions)

	diagMu sync.Mutex
	// latestDiag holds the most recent publishDiagnostics per URI (latest wins).
	// semgrep lsp publishes an EMPTY set on didOpen (clearing prior state) and the
	// real findings only after its async scan completes, so Scan must read the
	// latest publish for the URI, not the first one.
	latestDiag map[string][]lspDiagnostic
	// diagWaiters holds first-publish waiters keyed by URI: each registered channel
	// is closed on the next publishDiagnostics for that URI. WaitReady uses this to
	// detect that scanning works (any publish proves rules are compiled).
	diagWaiters map[string][]chan struct{}

	progMu sync.Mutex
	// progGen increments once per completed scan, detected via the semgrep lsp
	// work-done-progress cycle ($/progress notification with value.kind == "end").
	progGen uint64
	// progCh is closed when progGen advances and is then replaced; a waiter reads
	// (gen, progCh) atomically under progMu so it never misses a completion signal.
	progCh chan struct{}
}

// New builds a Driver over an arbitrary Transport and a workspace directory (the
// LSP rootUri; scanned files are written under it). It starts the read loop. Use
// Spawn for the production semgrep subprocess; New is the seam tests use directly.
func New(t Transport, workspace string) *Driver {
	d := &Driver{
		t:           t,
		workspace:   workspace,
		w:           t.Send(),
		pending:     make(map[int]chan rpcResponse),
		opened:      make(map[string]int),
		latestDiag:  make(map[string][]lspDiagnostic),
		diagWaiters: make(map[string][]chan struct{}),
		progCh:      make(chan struct{}),
	}
	go d.readLoop(bufio.NewReader(t.Recv()))
	return d
}

// Spawn starts `bin lsp` as a subprocess and returns a Driver over its stdio. The
// process inherits the current environment (main sets the OFFLINE semgrep env
// before calling Spawn), so the rule engine never reaches the Semgrep cloud.
func Spawn(ctx context.Context, bin, workspace string) (*Driver, error) {
	cmd := exec.CommandContext(ctx, bin, "lsp")
	cmd.Env = os.Environ()
	cmd.Stderr = os.Stderr
	stdin, err := cmd.StdinPipe()
	if err != nil {
		return nil, fmt.Errorf("lspdriver: stdin pipe: %w", err)
	}
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return nil, fmt.Errorf("lspdriver: stdout pipe: %w", err)
	}
	if err := cmd.Start(); err != nil {
		return nil, fmt.Errorf("lspdriver: start %s lsp: %w", bin, err)
	}
	return New(&procTransport{cmd: cmd, in: stdin, out: stdout}, workspace), nil
}

// Close shuts the transport (and the subprocess) down.
func (d *Driver) Close() error { return d.t.Close() }

// procTransport wraps a semgrep subprocess as a Transport.
type procTransport struct {
	cmd *exec.Cmd
	in  io.WriteCloser
	out io.ReadCloser
}

func (p *procTransport) Recv() io.Reader { return p.out }
func (p *procTransport) Send() io.Writer { return p.in }
func (p *procTransport) Close() error {
	_ = p.in.Close()
	if p.cmd.Process != nil {
		_ = p.cmd.Process.Kill()
	}
	return p.cmd.Wait()
}

// --- JSON-RPC plumbing ---

type rpcResponse struct {
	result json.RawMessage
	err    json.RawMessage
}

// rpcMessage is the wire shape of any JSON-RPC frame (request, notification, or
// response). A response has id + result/error; a notification has method, no id;
// a server->client request has both method and id.
type rpcMessage struct {
	JSONRPC string           `json:"jsonrpc"`
	ID      *json.RawMessage `json:"id,omitempty"`
	Method  string           `json:"method,omitempty"`
	Params  json.RawMessage  `json:"params,omitempty"`
	Result  json.RawMessage  `json:"result,omitempty"`
	Error   json.RawMessage  `json:"error,omitempty"`
}

// readLoop reads framed messages and dispatches them: responses go to the waiting
// caller; publishDiagnostics notifications update the diagnostics hub; $/progress
// notifications drive scan-completion detection; any other server->client request
// (one with an id) gets a null reply so the server never blocks waiting on us.
func (d *Driver) readLoop(r *bufio.Reader) {
	for {
		payload, err := readFrame(r)
		if err != nil {
			d.failPending(err)
			return
		}
		var msg rpcMessage
		if err := json.Unmarshal(payload, &msg); err != nil {
			continue // skip an unparseable frame rather than killing the loop
		}
		if msg.Method != "" {
			switch msg.Method {
			case "textDocument/publishDiagnostics":
				d.handleDiagnostics(msg.Params)
			case "$/progress":
				d.handleProgress(msg.Params)
			}
			if msg.ID != nil {
				// Server->client request: acknowledge with null so it proceeds.
				d.replyNull(*msg.ID)
			}
			continue
		}
		if msg.ID != nil {
			d.deliverResponse(*msg.ID, rpcResponse{result: msg.Result, err: msg.Error})
		}
	}
}

func (d *Driver) deliverResponse(rawID json.RawMessage, resp rpcResponse) {
	id, ok := parseID(rawID)
	if !ok {
		return
	}
	d.mu.Lock()
	ch := d.pending[id]
	delete(d.pending, id)
	d.mu.Unlock()
	if ch != nil {
		ch <- resp
	}
}

func (d *Driver) failPending(err error) {
	d.mu.Lock()
	for id, ch := range d.pending {
		ch <- rpcResponse{err: json.RawMessage(fmt.Sprintf("%q", err.Error()))}
		delete(d.pending, id)
	}
	d.mu.Unlock()
}

// call sends a request and waits for its response (or ctx cancellation).
func (d *Driver) call(ctx context.Context, method string, params any) (json.RawMessage, error) {
	d.mu.Lock()
	d.nextID++
	id := d.nextID
	ch := make(chan rpcResponse, 1)
	d.pending[id] = ch
	d.mu.Unlock()

	if err := d.writeFrame(rpcMessage{JSONRPC: "2.0", ID: rawID(id), Method: method, Params: mustRaw(params)}); err != nil {
		d.mu.Lock()
		delete(d.pending, id)
		d.mu.Unlock()
		return nil, err
	}
	select {
	case <-ctx.Done():
		return nil, ctx.Err()
	case resp := <-ch:
		if len(resp.err) > 0 {
			return nil, fmt.Errorf("lspdriver: %s error: %s", method, string(resp.err))
		}
		return resp.result, nil
	}
}

// notify sends a notification (no id, no reply expected).
func (d *Driver) notify(method string, params any) error {
	return d.writeFrame(rpcMessage{JSONRPC: "2.0", Method: method, Params: mustRaw(params)})
}

func (d *Driver) replyNull(rawID json.RawMessage) {
	id := rawID
	_ = d.writeFrame(rpcMessage{JSONRPC: "2.0", ID: &id, Result: json.RawMessage("null")})
}

func (d *Driver) writeFrame(msg rpcMessage) error {
	payload, err := json.Marshal(msg)
	if err != nil {
		return fmt.Errorf("lspdriver: marshal %s: %w", msg.Method, err)
	}
	d.writeMu.Lock()
	defer d.writeMu.Unlock()
	return writeFrame(d.w, payload)
}

// --- diagnostics hub ---

type lspPosition struct {
	Line      int `json:"line"`
	Character int `json:"character"`
}

type lspRange struct {
	Start lspPosition `json:"start"`
	End   lspPosition `json:"end"`
}

type lspDiagnostic struct {
	Range    lspRange        `json:"range"`
	Severity int             `json:"severity"`
	Code     json.RawMessage `json:"code"`
	Message  string          `json:"message"`
	Source   string          `json:"source"`
}

type publishDiagnosticsParams struct {
	URI         string          `json:"uri"`
	Diagnostics []lspDiagnostic `json:"diagnostics"`
}

// handleDiagnostics records the latest diagnostics for a URI (latest wins, so a
// later real-findings publish overwrites the premature empty publish semgrep emits
// on didOpen) and wakes any first-publish waiters registered for that URI.
func (d *Driver) handleDiagnostics(raw json.RawMessage) {
	var p publishDiagnosticsParams
	if err := json.Unmarshal(raw, &p); err != nil {
		return
	}
	d.diagMu.Lock()
	d.latestDiag[p.URI] = p.Diagnostics
	waiters := d.diagWaiters[p.URI]
	delete(d.diagWaiters, p.URI)
	d.diagMu.Unlock()
	for _, ch := range waiters {
		close(ch)
	}
}

// subscribeDiag registers a first-publish waiter for uri: the returned channel is
// closed by handleDiagnostics on the next publishDiagnostics for uri. WaitReady
// subscribes BEFORE opening the probe document so a fast server reply is never
// missed. This only signals THAT a publish happened, not its contents.
func (d *Driver) subscribeDiag(uri string) <-chan struct{} {
	ch := make(chan struct{})
	d.diagMu.Lock()
	d.diagWaiters[uri] = append(d.diagWaiters[uri], ch)
	d.diagMu.Unlock()
	return ch
}

// lspProgressParams is the partial wire shape of a $/progress notification. semgrep
// lsp wraps each scan in a work-done-progress cycle whose value.kind goes
// "begin" -> ("report" ...) -> "end"; only the "end" marks a completed scan.
type lspProgressParams struct {
	Value struct {
		Kind string `json:"kind"`
	} `json:"value"`
}

// handleProgress advances the scan-completion generation on each $/progress "end".
// It captures the moment an async scan finishes; because readLoop processes frames
// in order, any publishDiagnostics for that scan have already updated latestDiag by
// the time the matching "end" arrives.
func (d *Driver) handleProgress(raw json.RawMessage) {
	var p lspProgressParams
	if err := json.Unmarshal(raw, &p); err != nil {
		return
	}
	if p.Value.Kind != "end" {
		return
	}
	d.progMu.Lock()
	d.progGen++
	close(d.progCh)
	d.progCh = make(chan struct{})
	d.progMu.Unlock()
}

// scanProgressGrace bounds how long Scan waits for a $/progress "end" before
// falling back to best-effort collection of whatever diagnostics have arrived. It
// is deliberately well under any sane ctx scan timeout so a semgrep build that does
// not emit progress degrades to best-effort rather than hanging the request.
const scanProgressGrace = 3 * time.Second

// waitScanComplete blocks until progGen advances past startGen (a scan finished
// after our document opens), ctx is cancelled, or the grace window elapses. It
// returns ctx.Err() only on real cancellation; a grace-window timeout returns nil
// so the caller proceeds with whatever was collected (best-effort fallback).
func (d *Driver) waitScanComplete(ctx context.Context, startGen uint64) error {
	grace := time.NewTimer(scanProgressGrace)
	defer grace.Stop()
	for {
		d.progMu.Lock()
		gen := d.progGen
		ch := d.progCh
		d.progMu.Unlock()
		if gen > startGen {
			return nil
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-grace.C:
			return nil // best-effort fallback: no progress emitted in time
		case <-ch:
			// progGen advanced; loop to re-read and confirm it passed startGen.
		}
	}
}

// --- public LSP operations ---

// Initialize performs the initialize/initialized handshake, pointing semgrep at
// rulesDir and disabling cloud metrics. jobs sets the scan parallelism.
func (d *Driver) Initialize(ctx context.Context, rulesDir string, jobs int) error {
	params := map[string]any{
		"processId":    nil,
		"rootUri":      pathToURI(d.workspace),
		"capabilities": map[string]any{},
		"initializationOptions": map[string]any{
			"scan": map[string]any{
				"configuration": []string{rulesDir},
				"onlyGitDirty":  false,
				"jobs":          jobs,
			},
			"metrics": map[string]any{"enabled": false},
			"doHover": false,
		},
	}
	if _, err := d.call(ctx, "initialize", params); err != nil {
		return err
	}
	return d.notify("initialized", map[string]any{})
}

// WaitReady blocks until the rule set is compiled and scanning works. semgrep lsp
// compiles rules asynchronously after initialize (the ~2s warm-up), so readiness
// is detected behaviourally: open a trivial probe document and wait for its first
// publishDiagnostics, which can only arrive once rules are loaded and the document
// has been scanned.
func (d *Driver) WaitReady(ctx context.Context) error {
	const probe = "__semgrep_probe__.py"
	uri := pathToURI(filepath.Join(d.workspace, probe))
	if err := d.writeWorkspaceFile(probe, "x = 1\n"); err != nil {
		return err
	}
	ch := d.subscribeDiag(uri)
	if err := d.openDoc(uri, languageID(probe), 1, "x = 1\n"); err != nil {
		return err
	}
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-ch:
		return nil
	}
}

// Scan runs the warm rule set over a batch of files (whole-file scanning) and
// returns the findings in request order. Each file is written into the workspace
// and (re)opened with a unique, increasing version so semgrep re-scans it even if
// it was scanned in a prior request.
func (d *Driver) Scan(ctx context.Context, files []vsockproto.ScanFile) ([]vsockproto.Finding, error) {
	d.scanMu.Lock()
	defer d.scanMu.Unlock()

	// Snapshot the scan-completion generation before opening any documents, so we
	// can detect a $/progress "end" that lands strictly after our opens.
	d.progMu.Lock()
	startGen := d.progGen
	d.progMu.Unlock()

	type target struct {
		path string
		uri  string
	}
	targets := make([]target, 0, len(files))
	for _, f := range files {
		rel := cleanRel(f.Path)
		uri := pathToURI(filepath.Join(d.workspace, rel))
		if err := d.writeWorkspaceFile(rel, f.Content); err != nil {
			return nil, fmt.Errorf("lspdriver: write %s: %w", f.Path, err)
		}
		// Drop any stale diagnostics for this uri (e.g. a prior scan, or the empty
		// publish semgrep emits on didOpen) so we only read this scan's output.
		d.diagMu.Lock()
		delete(d.latestDiag, uri)
		d.diagMu.Unlock()
		targets = append(targets, target{path: f.Path, uri: uri})
		if err := d.openOrChange(uri, rel, f.Content); err != nil {
			return nil, err
		}
	}

	// Wait for semgrep's async scan to complete (its $/progress "end") rather than
	// grabbing the premature empty publish. waitScanComplete returns an error only
	// on real ctx cancellation; a grace-window timeout falls through to whatever
	// latest diagnostics were collected (best-effort), so a build that emits no
	// progress degrades rather than hangs.
	if err := d.waitScanComplete(ctx, startGen); err != nil {
		return nil, err
	}

	var findings []vsockproto.Finding
	d.diagMu.Lock()
	for _, tgt := range targets {
		for _, diag := range d.latestDiag[tgt.uri] {
			findings = append(findings, translate(tgt.path, diag))
		}
	}
	d.diagMu.Unlock()
	return findings, nil
}

// openOrChange opens a document the first time it is seen, and sends a didChange
// with a bumped version on subsequent scans (forcing a re-scan).
func (d *Driver) openOrChange(uri, rel, content string) error {
	d.docMu.Lock()
	version, seen := d.opened[uri]
	version++
	d.opened[uri] = version
	d.docMu.Unlock()
	if !seen {
		return d.openDoc(uri, languageID(rel), version, content)
	}
	return d.changeDoc(uri, version, content)
}

func (d *Driver) openDoc(uri, lang string, version int, text string) error {
	return d.notify("textDocument/didOpen", map[string]any{
		"textDocument": map[string]any{
			"uri":        uri,
			"languageId": lang,
			"version":    version,
			"text":       text,
		},
	})
}

func (d *Driver) changeDoc(uri string, version int, text string) error {
	return d.notify("textDocument/didChange", map[string]any{
		"textDocument":   map[string]any{"uri": uri, "version": version},
		"contentChanges": []map[string]any{{"text": text}},
	})
}

func (d *Driver) writeWorkspaceFile(rel, content string) error {
	dst := filepath.Join(d.workspace, rel)
	if err := os.MkdirAll(filepath.Dir(dst), 0o755); err != nil {
		return err
	}
	return os.WriteFile(dst, []byte(content), 0o644)
}

// translate maps one LSP diagnostic to a vsockproto.Finding, carrying the caller's
// original path (not the workspace uri) and converting 0-based LSP positions to
// 1-based line/column.
func translate(origPath string, diag lspDiagnostic) vsockproto.Finding {
	return vsockproto.Finding{
		Path:     origPath,
		Line:     diag.Range.Start.Line + 1,
		Col:      diag.Range.Start.Character + 1,
		RuleID:   codeString(diag.Code),
		Severity: severityName(diag.Severity),
		Message:  diag.Message,
	}
}

// --- small helpers ---

func severityName(s int) string {
	switch s {
	case 1:
		return "ERROR"
	case 2:
		return "WARNING"
	case 3:
		return "INFO"
	case 4:
		return "HINT"
	default:
		return "UNKNOWN"
	}
}

func codeString(raw json.RawMessage) string {
	if len(raw) == 0 {
		return ""
	}
	var s string
	if err := json.Unmarshal(raw, &s); err == nil {
		return s
	}
	return strings.Trim(string(raw), `"`)
}

// cleanRel neutralises absolute paths and ".." escapes so a file always lands
// inside the workspace.
func cleanRel(p string) string {
	clean := filepath.Clean("/" + p)
	return strings.TrimPrefix(clean, "/")
}

func pathToURI(p string) string { return "file://" + p }

func languageID(path string) string {
	switch strings.ToLower(filepath.Ext(path)) {
	case ".py":
		return "python"
	case ".js", ".jsx":
		return "javascript"
	case ".ts", ".tsx":
		return "typescript"
	case ".go":
		return "go"
	case ".java":
		return "java"
	case ".rb":
		return "ruby"
	case ".php":
		return "php"
	case ".c", ".h":
		return "c"
	case ".cpp", ".cc", ".cxx", ".hpp":
		return "cpp"
	case ".rs":
		return "rust"
	case ".json":
		return "json"
	case ".yaml", ".yml":
		return "yaml"
	default:
		return "plaintext"
	}
}

func rawID(id int) *json.RawMessage {
	r := json.RawMessage(fmt.Sprintf("%d", id))
	return &r
}

func parseID(raw json.RawMessage) (int, bool) {
	var id int
	if err := json.Unmarshal(raw, &id); err != nil {
		return 0, false
	}
	return id, true
}

func mustRaw(v any) json.RawMessage {
	b, err := json.Marshal(v)
	if err != nil {
		return json.RawMessage("null")
	}
	return b
}

// writeFrame writes one LSP message with its Content-Length header.
func writeFrame(w io.Writer, payload []byte) error {
	if _, err := fmt.Fprintf(w, "Content-Length: %d\r\n\r\n", len(payload)); err != nil {
		return err
	}
	_, err := w.Write(payload)
	return err
}

// readFrame reads one LSP message: a header block terminated by a blank line, then
// the Content-Length body.
func readFrame(r *bufio.Reader) ([]byte, error) {
	var length int
	for {
		line, err := r.ReadString('\n')
		if err != nil {
			return nil, err
		}
		line = strings.TrimRight(line, "\r\n")
		if line == "" {
			break
		}
		if name, value, ok := strings.Cut(line, ":"); ok && strings.EqualFold(strings.TrimSpace(name), "Content-Length") {
			if _, err := fmt.Sscanf(strings.TrimSpace(value), "%d", &length); err != nil {
				return nil, fmt.Errorf("lspdriver: bad Content-Length %q: %w", value, err)
			}
		}
	}
	if length <= 0 {
		return nil, fmt.Errorf("lspdriver: missing/zero Content-Length")
	}
	body := make([]byte, length)
	if _, err := io.ReadFull(r, body); err != nil {
		return nil, err
	}
	return body, nil
}
