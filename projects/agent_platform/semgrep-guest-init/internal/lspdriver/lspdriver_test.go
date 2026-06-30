package lspdriver

import (
	"bufio"
	"context"
	"encoding/json"
	"io"
	"path/filepath"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/agent_platform/vsockproto"
)

// pipeTransport wires the Driver to an in-memory fake LSP via two io.Pipes.
type pipeTransport struct {
	recv io.Reader      // server -> client
	send io.WriteCloser // client -> server
}

func (p *pipeTransport) Recv() io.Reader { return p.recv }
func (p *pipeTransport) Send() io.Writer { return p.send }
func (p *pipeTransport) Close() error    { return p.send.Close() }

// fakeLSP speaks the LSP framing over the pipes: it answers initialize, and on any
// didOpen/didChange it publishes the diagnostics returned by diagFor(uri). No real
// semgrep is involved.
func fakeLSP(t *testing.T, clientToServer io.Reader, serverToClient io.Writer, diagFor func(uri string) []lspDiagnostic) {
	t.Helper()
	r := bufio.NewReader(clientToServer)
	go func() {
		for {
			payload, err := readFrame(r)
			if err != nil {
				return
			}
			var msg rpcMessage
			if err := json.Unmarshal(payload, &msg); err != nil {
				continue
			}
			switch msg.Method {
			case "initialize":
				resp := rpcMessage{JSONRPC: "2.0", ID: msg.ID, Result: json.RawMessage(`{"capabilities":{}}`)}
				out, _ := json.Marshal(resp)
				_ = writeFrame(serverToClient, out)
			case "textDocument/didOpen", "textDocument/didChange":
				var p struct {
					TextDocument struct {
						URI string `json:"uri"`
					} `json:"textDocument"`
				}
				_ = json.Unmarshal(msg.Params, &p)
				note := rpcMessage{
					JSONRPC: "2.0",
					Method:  "textDocument/publishDiagnostics",
					Params:  mustRaw(publishDiagnosticsParams{URI: p.TextDocument.URI, Diagnostics: diagFor(p.TextDocument.URI)}),
				}
				out, _ := json.Marshal(note)
				_ = writeFrame(serverToClient, out)
			}
		}
	}()
}

func newTestDriver(t *testing.T, diagFor func(uri string) []lspDiagnostic) (*Driver, string) {
	t.Helper()
	c2sR, c2sW := io.Pipe()
	s2cR, s2cW := io.Pipe()
	fakeLSP(t, c2sR, s2cW, diagFor)
	ws := t.TempDir()
	d := New(&pipeTransport{recv: s2cR, send: c2sW}, ws)
	t.Cleanup(func() { _ = d.Close() })
	return d, ws
}

func TestInitializeAndWaitReady(t *testing.T) {
	d, _ := newTestDriver(t, func(uri string) []lspDiagnostic { return nil })
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	if err := d.Initialize(ctx, "/etc/semgrep/rules", 2); err != nil {
		t.Fatalf("Initialize: %v", err)
	}
	if err := d.WaitReady(ctx); err != nil {
		t.Fatalf("WaitReady: %v", err)
	}
}

func TestScanTranslatesDiagnostic(t *testing.T) {
	var ws string
	diagFor := func(uri string) []lspDiagnostic {
		// Only the scanned file gets a finding; the readiness probe stays clean.
		if uri != pathToURI(filepath.Join(ws, "a.py")) {
			return nil
		}
		return []lspDiagnostic{{
			Range:    lspRange{Start: lspPosition{Line: 2, Character: 4}},
			Severity: 1,
			Code:     json.RawMessage(`"python.lang.security.bad"`),
			Message:  "bad thing",
		}}
	}
	var d *Driver
	d, ws = newTestDriver(t, diagFor)

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	if err := d.Initialize(ctx, "/etc/semgrep/rules", 1); err != nil {
		t.Fatalf("Initialize: %v", err)
	}
	if err := d.WaitReady(ctx); err != nil {
		t.Fatalf("WaitReady: %v", err)
	}

	findings, err := d.Scan(ctx, []vsockproto.ScanFile{{Path: "a.py", Content: "import os\nx=1\nbad()\n"}})
	if err != nil {
		t.Fatalf("Scan: %v", err)
	}
	if len(findings) != 1 {
		t.Fatalf("expected 1 finding, got %d: %+v", len(findings), findings)
	}
	got := findings[0]
	want := vsockproto.Finding{
		Path:     "a.py",
		Line:     3, // 0-based 2 -> 1-based 3
		Col:      5, // 0-based 4 -> 1-based 5
		RuleID:   "python.lang.security.bad",
		Severity: "ERROR",
		Message:  "bad thing",
	}
	if got != want {
		t.Fatalf("finding mismatch:\n got=%+v\nwant=%+v", got, want)
	}
}
