package handler

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"testing"

	"github.com/jomcgi/homelab/projects/firecracker/substrate/shim"
)

// callSession drives one session-mode request through Handle. Each test resets
// the package singleton first so kernels never leak between tests.
func callSession(t *testing.T, code string, timeoutSeconds int) ExecResult {
	t.Helper()
	req := ExecRequest{Code: code, Mode: ModeSession, TimeoutSeconds: timeoutSeconds}
	body, err := json.Marshal(req)
	if err != nil {
		t.Fatalf("marshal request: %v", err)
	}
	resp, err := Handle(context.Background(), &shim.Request{Path: "/invoke/sandbox", Body: bytes.NewReader(body)})
	if err != nil {
		t.Fatalf("Handle returned unexpected error: %v", err)
	}
	if resp.Status != http.StatusOK {
		t.Fatalf("status = %d, want %d", resp.Status, http.StatusOK)
	}
	return decodeResult(t, resp)
}

// resetKernel kills any running child and clears the singleton so a test starts
// from a known-empty namespace.
func resetKernel(t *testing.T) {
	t.Helper()
	sessionKernel.mu.Lock()
	sessionKernel.kill()
	sessionKernel.mu.Unlock()
	t.Cleanup(func() {
		sessionKernel.mu.Lock()
		sessionKernel.kill()
		sessionKernel.mu.Unlock()
	})
}

// TestOneShotUnaffectedByModeField proves the byte-identical one-shot guarantee:
// with Mode absent, Handle runs the classic per-invoke path. This is the same
// assertion the task-class and deprecated fc-invoke callers rely on.
func TestOneShotUnaffectedByModeField(t *testing.T) {
	requirePython(t)
	// No Mode field at all: classic one-shot.
	resp, err := call(t, ExecRequest{Code: "print('one shot')"})
	if err != nil {
		t.Fatalf("Handle error: %v", err)
	}
	result := decodeResult(t, resp)
	if strings.TrimSpace(result.Stdout) != "one shot" {
		t.Errorf("stdout = %q, want %q", result.Stdout, "one shot")
	}
	if result.SessionReset {
		t.Error("one-shot result must never set session_reset")
	}
}

// TestOneShotResultOmitsSessionReset guards the wire shape: a one-shot result
// JSON must not contain the session_reset key (omitempty), so a one-shot
// caller's response is byte-for-byte what it was before session mode existed.
func TestOneShotResultOmitsSessionReset(t *testing.T) {
	body, err := json.Marshal(ExecResult{Stdout: "x", ExitCode: 0})
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	if strings.Contains(string(body), "session_reset") {
		t.Errorf("one-shot ExecResult JSON must omit session_reset; got %s", body)
	}
}

// TestSessionStatePersistsAcrossSnippets is the headline behavior: a variable
// defined in one snippet is readable in the next through the shared namespace.
func TestSessionStatePersistsAcrossSnippets(t *testing.T) {
	requirePython(t)
	resetKernel(t)

	first := callSession(t, "x = 41", 5)
	if first.ExitCode != 0 {
		t.Fatalf("first snippet exit = %d (err %q)", first.ExitCode, first.Error)
	}
	if !first.SessionReset {
		t.Error("the first snippet starts a fresh child; session_reset should be true")
	}

	second := callSession(t, "print(x + 1)", 5)
	if second.ExitCode != 0 {
		t.Fatalf("second snippet exit = %d (err %q)", second.ExitCode, second.Error)
	}
	if strings.TrimSpace(second.Stdout) != "42" {
		t.Errorf("stdout = %q, want %q (state did not persist)", second.Stdout, "42")
	}
	if second.SessionReset {
		t.Error("second snippet reused the live child; session_reset should be false")
	}
}

// TestSessionFilesPersistAcrossSnippets proves the /tmp/session workdir persists
// and changed files are returned.
func TestSessionFilesPersistAcrossSnippets(t *testing.T) {
	requirePython(t)
	resetKernel(t)

	callSession(t, "open('note.txt', 'w').write('hello')", 5)
	read := callSession(t, "print(open('note.txt').read())", 5)
	if strings.TrimSpace(read.Stdout) != "hello" {
		t.Errorf("stdout = %q, want %q (file did not persist)", read.Stdout, "hello")
	}
}

// TestSessionChangedFileReturned proves a file written in a snippet comes back
// in that snippet's Files manifest.
func TestSessionChangedFileReturned(t *testing.T) {
	requirePython(t)
	resetKernel(t)

	res := callSession(t, "open('out.txt', 'w').write('generated')", 5)
	var found bool
	for _, f := range res.Files {
		if f.Path != "out.txt" {
			continue
		}
		found = true
		data, decErr := base64.StdEncoding.DecodeString(f.ContentB64)
		if decErr != nil {
			t.Fatalf("decode: %v", decErr)
		}
		if string(data) != "generated" {
			t.Errorf("out.txt = %q, want %q", data, "generated")
		}
	}
	if !found {
		t.Error("generated file out.txt not returned in session mode")
	}
}

// TestSessionTimeoutResetsChild proves a snippet timeout kills the child (so the
// loop is not wedged) and reports session_reset, and that the NEXT snippet sees
// an empty namespace (the reset actually happened).
func TestSessionTimeoutResetsChild(t *testing.T) {
	requirePython(t)
	resetKernel(t)

	// Seed state, then time a snippet out.
	callSession(t, "marker = 'kept'", 5)
	timedOut := callSession(t, "import time; time.sleep(30)", 1)
	if timedOut.ExitCode != -1 {
		t.Errorf("timed-out exit = %d, want -1", timedOut.ExitCode)
	}
	if !strings.Contains(timedOut.Error, "timed out") {
		t.Errorf("error = %q, want a timeout", timedOut.Error)
	}
	if !timedOut.SessionReset {
		t.Error("a snippet timeout must report session_reset (child was killed)")
	}

	// The next snippet runs against a fresh child: marker is gone (NameError),
	// and this snippet itself is reported as a reset (it started the new child).
	afterReset := callSession(t, "print('marker' in dir())", 5)
	if !afterReset.SessionReset {
		t.Error("the snippet after a reset starts a new child; session_reset should be true")
	}
	if strings.TrimSpace(afterReset.Stdout) != "False" {
		t.Errorf("stdout = %q, want False (state should be gone after reset)", afterReset.Stdout)
	}
}

// TestSessionSnippetErrorKeepsChild proves a snippet that raises does NOT kill
// the child (only a timeout/transport fault does): state survives a caught
// exception, so an agent's typo does not wipe its whole session.
func TestSessionSnippetErrorKeepsChild(t *testing.T) {
	requirePython(t)
	resetKernel(t)

	callSession(t, "y = 7", 5)
	errored := callSession(t, "raise ValueError('boom')", 5)
	if errored.ExitCode != 1 {
		t.Errorf("errored exit = %d, want 1", errored.ExitCode)
	}
	if !strings.Contains(errored.Stderr, "ValueError") {
		t.Errorf("stderr = %q, want a ValueError traceback", errored.Stderr)
	}
	if errored.SessionReset {
		t.Error("a snippet exception must NOT reset the session; the child lives on")
	}

	survived := callSession(t, "print(y)", 5)
	if strings.TrimSpace(survived.Stdout) != "7" {
		t.Errorf("stdout = %q, want 7 (state should survive a snippet exception)", survived.Stdout)
	}
}

// TestFrameRoundTrip exercises the length-prefixed framing directly, including a
// PARTIAL read (the header and body split across reads) to prove readFrame
// reassembles from an io.Reader that returns short reads.
func TestFrameRoundTrip(t *testing.T) {
	payload := []byte(`{"code":"print(1)"}`)
	var buf bytes.Buffer
	if err := writeFrame(&buf, payload); err != nil {
		t.Fatalf("writeFrame: %v", err)
	}

	// A reader that yields one byte at a time is the worst-case partial read.
	got, err := readFrame(&oneByteReader{data: buf.Bytes()})
	if err != nil {
		t.Fatalf("readFrame: %v", err)
	}
	if !bytes.Equal(got, payload) {
		t.Errorf("frame = %q, want %q", got, payload)
	}
}

// TestReadFrameRejectsOversized proves a frame claiming more than maxFrameBytes
// is refused rather than allocated, so a corrupt child cannot exhaust memory.
func TestReadFrameRejectsOversized(t *testing.T) {
	var hdr [4]byte
	binary.BigEndian.PutUint32(hdr[:], maxFrameBytes+1)
	_, err := readFrame(bytes.NewReader(hdr[:]))
	if err == nil {
		t.Fatal("expected an error for an oversized frame length")
	}
	if !strings.Contains(err.Error(), "exceeds cap") {
		t.Errorf("error = %q, want it to mention the cap", err)
	}
}

// oneByteReader returns at most one byte per Read, forcing io.ReadFull to loop.
type oneByteReader struct {
	data []byte
	pos  int
}

func (r *oneByteReader) Read(p []byte) (int, error) {
	if r.pos >= len(r.data) {
		return 0, io.EOF
	}
	if len(p) == 0 {
		return 0, nil
	}
	p[0] = r.data[r.pos]
	r.pos++
	return 1, nil
}
