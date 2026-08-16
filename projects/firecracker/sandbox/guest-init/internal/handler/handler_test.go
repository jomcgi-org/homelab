package handler

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/firecracker/substrate/shim"
)

// Tests run as a normal, non-root user (including in CI). Setting Credential
// to an arbitrary uid requires CAP_SETUID, which a non-root test process
// does not have, so exec-dependent tests below disable the production
// uid-drop for the duration of this package's test run via this init.
// cmd/main.go never touches dropPrivileges; production always runs Handle as
// the guest-init root PID 1, which is exactly the case this flag exists for.
func init() {
	dropPrivileges = false
}

// requirePython skips the calling test when python3 is not on PATH, so
// exec-dependent tests degrade gracefully on a runner without it instead of
// failing.
func requirePython(t *testing.T) {
	t.Helper()
	if _, err := exec.LookPath("python3"); err != nil {
		t.Skip("python3 not found on PATH; skipping exec-dependent test")
	}
}

func call(t *testing.T, req ExecRequest) (*shim.Response, error) {
	t.Helper()
	body, err := json.Marshal(req)
	if err != nil {
		t.Fatalf("marshal request: %v", err)
	}
	return Handle(context.Background(), &shim.Request{Path: "/invoke/sandbox", Body: bytes.NewReader(body)}, languageSpecs["python"])
}

func decodeResult(t *testing.T, resp *shim.Response) ExecResult {
	t.Helper()
	var result ExecResult
	if err := json.Unmarshal(resp.Body, &result); err != nil {
		t.Fatalf("unmarshal result: %v", err)
	}
	return result
}

func TestHandleHappyPath(t *testing.T) {
	requirePython(t)
	resp, err := call(t, ExecRequest{Code: "print('hello sandbox')"})
	if err != nil {
		t.Fatalf("Handle returned unexpected error: %v", err)
	}
	if resp.Status != http.StatusOK {
		t.Fatalf("status = %d, want %d", resp.Status, http.StatusOK)
	}
	result := decodeResult(t, resp)
	if result.ExitCode != 0 {
		t.Errorf("exit code = %d, want 0", result.ExitCode)
	}
	if strings.TrimSpace(result.Stdout) != "hello sandbox" {
		t.Errorf("stdout = %q, want %q", result.Stdout, "hello sandbox")
	}
	if result.Error != "" {
		t.Errorf("error = %q, want empty", result.Error)
	}
}

func TestHandleNonzeroExit(t *testing.T) {
	requirePython(t)
	resp, err := call(t, ExecRequest{Code: "import sys; sys.exit(7)"})
	if err != nil {
		t.Fatalf("Handle returned unexpected error: %v", err)
	}
	result := decodeResult(t, resp)
	if result.ExitCode != 7 {
		t.Errorf("exit code = %d, want 7", result.ExitCode)
	}
}

func TestHandleStdoutTruncation(t *testing.T) {
	requirePython(t)
	code := fmt.Sprintf("import sys; sys.stdout.write('x' * %d)", stdoutCap+1024)
	resp, err := call(t, ExecRequest{Code: code})
	if err != nil {
		t.Fatalf("Handle returned unexpected error: %v", err)
	}
	result := decodeResult(t, resp)
	if !result.Truncated {
		t.Error("Truncated = false, want true")
	}
	if len(result.Stdout) != stdoutCap {
		t.Errorf("stdout len = %d, want %d", len(result.Stdout), stdoutCap)
	}
}

func TestHandleInputFileRoundTrip(t *testing.T) {
	requirePython(t)
	content := []byte("42\n")
	resp, err := call(t, ExecRequest{
		Code: "print(open('input.txt').read().strip())",
		Files: []ExecFile{
			{Path: "input.txt", ContentB64: base64.StdEncoding.EncodeToString(content)},
		},
	})
	if err != nil {
		t.Fatalf("Handle returned unexpected error: %v", err)
	}
	result := decodeResult(t, resp)
	if strings.TrimSpace(result.Stdout) != "42" {
		t.Errorf("stdout = %q, want %q", result.Stdout, "42")
	}
	for _, f := range result.Files {
		if f.Path == "input.txt" {
			t.Errorf("unchanged input file %q was echoed back as output", f.Path)
		}
	}
}

func TestHandleInputFileOverwriteRoundTrip(t *testing.T) {
	requirePython(t)
	// Regression guard (Opus PR B review): a script that rewrites an input
	// file in place must succeed and the modified content must come back in
	// result.Files. The plain round-trip test only READS an input, so it
	// never exercised the write path collectOutputFiles's doc promises ("An
	// input the script modified ... IS returned").
	//
	// Caveat: dropPrivileges is off in this package's tests (a non-root
	// runner cannot chown to uid 65532), so this proves the round-trip and
	// change-detection logic but NOT the chownTree fix itself. Under real
	// uid-drop in the guest the overwrite would fail with PermissionError
	// without chownTree; that path only exists when running as root, which CI
	// does not. The chownTree call is covered by code reading, not this test.
	original := []byte("original\n")
	resp, err := call(t, ExecRequest{
		Code: "open('data.txt', 'w').write('rewritten')",
		Files: []ExecFile{
			{Path: "data.txt", ContentB64: base64.StdEncoding.EncodeToString(original)},
		},
	})
	if err != nil {
		t.Fatalf("Handle returned unexpected error: %v", err)
	}
	result := decodeResult(t, resp)
	if result.ExitCode != 0 {
		t.Fatalf("exit code = %d (error %q), want 0", result.ExitCode, result.Error)
	}
	var found bool
	for _, f := range result.Files {
		if f.Path != "data.txt" {
			continue
		}
		found = true
		data, decErr := base64.StdEncoding.DecodeString(f.ContentB64)
		if decErr != nil {
			t.Fatalf("decode data.txt content: %v", decErr)
		}
		if string(data) != "rewritten" {
			t.Errorf("data.txt content = %q, want %q", data, "rewritten")
		}
	}
	if !found {
		t.Error("overwritten input data.txt not present in result.Files (change-detection missed it)")
	}
}

func TestHandleGeneratedFilePickup(t *testing.T) {
	requirePython(t)
	resp, err := call(t, ExecRequest{Code: "open('out.txt', 'w').write('generated')"})
	if err != nil {
		t.Fatalf("Handle returned unexpected error: %v", err)
	}
	result := decodeResult(t, resp)
	var found bool
	for _, f := range result.Files {
		if f.Path != "out.txt" {
			continue
		}
		found = true
		data, decErr := base64.StdEncoding.DecodeString(f.ContentB64)
		if decErr != nil {
			t.Fatalf("decode out.txt content: %v", decErr)
		}
		if string(data) != "generated" {
			t.Errorf("out.txt content = %q, want %q", data, "generated")
		}
	}
	if !found {
		t.Error("generated file out.txt not present in result.Files")
	}
}

func TestHandlePathEscapeRejected(t *testing.T) {
	resp, err := call(t, ExecRequest{
		Code: "print('should not run')",
		Files: []ExecFile{
			{Path: "../escape.txt", ContentB64: base64.StdEncoding.EncodeToString([]byte("x"))},
		},
	})
	if err != nil {
		t.Fatalf("Handle returned unexpected error: %v", err)
	}
	if resp.Status != http.StatusBadRequest {
		t.Errorf("status = %d, want %d", resp.Status, http.StatusBadRequest)
	}
	result := decodeResult(t, resp)
	if result.Error == "" {
		t.Error("expected a non-empty Error for a path-escape request")
	}
}

func TestHandleEmptyCodeRejected(t *testing.T) {
	resp, err := call(t, ExecRequest{Code: "   "})
	if err != nil {
		t.Fatalf("Handle returned unexpected error: %v", err)
	}
	if resp.Status != http.StatusBadRequest {
		t.Errorf("status = %d, want %d", resp.Status, http.StatusBadRequest)
	}
}

func TestHandleDecodeErrorReturnsHandlerError(t *testing.T) {
	_, err := Handle(context.Background(), &shim.Request{Path: "/invoke/sandbox", Body: strings.NewReader("not json {{")}, languageSpecs["python"])
	if err == nil {
		t.Fatal("expected a non-nil error for an undecodable body")
	}
}

func TestHandleTimeoutKillsProcess(t *testing.T) {
	requirePython(t)
	start := time.Now()
	resp, err := call(t, ExecRequest{
		Code:           "import time; time.sleep(30)",
		TimeoutSeconds: 1,
	})
	elapsed := time.Since(start)
	if err != nil {
		t.Fatalf("Handle returned unexpected error: %v", err)
	}
	result := decodeResult(t, resp)
	if result.ExitCode != -1 {
		t.Errorf("exit code = %d, want -1", result.ExitCode)
	}
	if !strings.Contains(result.Error, "timed out") {
		t.Errorf("error = %q, want it to mention a timeout", result.Error)
	}
	// The script sleeps 30s; a 1s TimeoutSeconds should kill it well short of
	// that, proving the process (and not just the request) was terminated.
	if elapsed > 10*time.Second {
		t.Errorf("Handle took %s, want well under the 30s sleep", elapsed)
	}
}

func TestRunSnippetCompileFailureShortCircuits(t *testing.T) {
	requirePython(t)
	dir := t.TempDir()
	spec := Spec{
		Name:       "compiled-test",
		Compile:    []string{"python3", "-c", "import sys; print('compile out'); print('compile err', file=sys.stderr); sys.exit(7)"},
		Run:        []string{"python3", "-c", "open('ran.txt', 'w').write('ran')"},
		SourceFile: "main.test",
	}

	result := runSnippet(context.Background(), spec, dir, 5*time.Second)
	if result.ExitCode != 7 {
		t.Errorf("exit code = %d, want compiler exit code 7", result.ExitCode)
	}
	if !strings.Contains(result.Stdout, "compile out") || !strings.Contains(result.Stderr, "compile err") {
		t.Errorf("compiler output missing: stdout=%q stderr=%q", result.Stdout, result.Stderr)
	}
	if _, err := os.Stat(filepath.Join(dir, "ran.txt")); !os.IsNotExist(err) {
		t.Errorf("run command executed after compile failure, stat err = %v", err)
	}
}

// --- pure-function coverage: caps, path validation, output walking, all
// without needing python3 ---

func TestCapBufferTruncates(t *testing.T) {
	var c capBuffer
	c.limit = 4
	if _, err := c.Write([]byte("hello")); err != nil {
		t.Fatalf("Write: %v", err)
	}
	if c.String() != "hell" {
		t.Errorf("buf = %q, want %q", c.String(), "hell")
	}
	if !c.truncated {
		t.Error("truncated = false, want true")
	}
}

func TestCapBufferUnderLimitNotTruncated(t *testing.T) {
	var c capBuffer
	c.limit = 100
	if _, err := c.Write([]byte("hi")); err != nil {
		t.Fatalf("Write: %v", err)
	}
	if c.truncated {
		t.Error("truncated = true, want false")
	}
}

func TestMaxTimeoutBelowWorkloadRequestTimeout(t *testing.T) {
	// The workload's requestTimeout is 30s (substrate chart values, Task B3);
	// maxTimeout must stay under it so a caller's own timeout error always
	// beats the daemon killing the request out from under the guest.
	if maxTimeout >= 30*time.Second {
		t.Errorf("maxTimeout = %s, must stay below the 30s workload requestTimeout", maxTimeout)
	}
}

func TestCollectOutputFilesSkipsMainAndUnchangedInputIncludesChangedAndGenerated(t *testing.T) {
	dir := t.TempDir()
	unchanged := []byte("same")
	changedOriginal := []byte("was different")

	writeFile(t, dir, "unchanged.txt", unchanged)
	writeFile(t, dir, "changed.txt", []byte("now different"))
	writeFile(t, dir, "generated.txt", []byte("new"))
	writeFile(t, dir, "main.py", []byte("print(1)"))

	inputs := map[string]inputRecord{
		"unchanged.txt": {size: int64(len(unchanged)), hash: sha256.Sum256(unchanged)},
		"changed.txt":   {size: int64(len(changedOriginal)), hash: sha256.Sum256(changedOriginal)},
	}

	files, truncated, err := collectOutputFiles(dir, inputs, []string{"main.py"})
	if err != nil {
		t.Fatalf("collectOutputFiles: %v", err)
	}
	if truncated {
		t.Error("truncated = true, want false")
	}

	got := map[string]bool{}
	for _, f := range files {
		got[f.Path] = true
	}
	if got["unchanged.txt"] {
		t.Error("unchanged.txt should be excluded (unchanged input)")
	}
	if got["main.py"] {
		t.Error("main.py should always be excluded")
	}
	if !got["changed.txt"] {
		t.Error("changed.txt (modified input) should be included")
	}
	if !got["generated.txt"] {
		t.Error("generated.txt should be included")
	}
}

func TestCollectOutputFilesPerFileCap(t *testing.T) {
	dir := t.TempDir()
	writeFile(t, dir, "big.bin", bytes.Repeat([]byte("a"), perFileCap+1))

	files, truncated, err := collectOutputFiles(dir, nil, nil)
	if err != nil {
		t.Fatalf("collectOutputFiles: %v", err)
	}
	if !truncated {
		t.Error("truncated = false, want true (file exceeds perFileCap)")
	}
	for _, f := range files {
		if f.Path == "big.bin" {
			t.Error("big.bin exceeds perFileCap and should have been omitted")
		}
	}
}

func TestCollectOutputFilesTotalCap(t *testing.T) {
	dir := t.TempDir()
	// Two files each under perFileCap but together over totalFileCap.
	half := perFileCap
	writeFile(t, dir, "a.bin", bytes.Repeat([]byte("a"), half))
	writeFile(t, dir, "b.bin", bytes.Repeat([]byte("b"), half))
	writeFile(t, dir, "c.bin", bytes.Repeat([]byte("c"), half))

	_, truncated, err := collectOutputFiles(dir, nil, nil)
	if err != nil {
		t.Fatalf("collectOutputFiles: %v", err)
	}
	if !truncated {
		t.Error("truncated = false, want true (files exceed totalFileCap combined)")
	}
}

func writeFile(t *testing.T, dir, name string, data []byte) {
	t.Helper()
	if err := os.WriteFile(filepath.Join(dir, name), data, 0o644); err != nil {
		t.Fatalf("write %s: %v", name, err)
	}
}
