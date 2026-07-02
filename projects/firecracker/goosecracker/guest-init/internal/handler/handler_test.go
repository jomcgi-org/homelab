package handler

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"

	"github.com/jomcgi/homelab/projects/firecracker/substrate/shim"
)

// fakeRunner is a Runner test double: it records the argv/env it was invoked
// with, replays a canned set of output lines through onLine, and records Clone
// and RecordScratch calls. It runs no real goose or git.
type fakeRunner struct {
	lines    []string
	out      string
	runErr   error
	cloneErr error

	gotArgv   []string
	gotEnv    map[string]string
	cloneCall struct {
		called            bool
		mirror, ref, dest string
	}

	// RecordScratch seam: configure the returned ref and error.
	recordScratchRef  string
	recordScratchErr  error
	recordScratchCall struct {
		called                        bool
		workspace, mirrorURL, session string
	}
}

func (f *fakeRunner) Run(_ context.Context, argv []string, env map[string]string, onLine func(string)) (string, error) {
	f.gotArgv = argv
	f.gotEnv = env
	for _, l := range f.lines {
		onLine(l)
	}
	return f.out, f.runErr
}

func (f *fakeRunner) Clone(_ context.Context, mirror, ref, dest string) error {
	f.cloneCall.called = true
	f.cloneCall.mirror, f.cloneCall.ref, f.cloneCall.dest = mirror, ref, dest
	return f.cloneErr // nosemgrep: no-bare-error-return
}

func (f *fakeRunner) RecordScratch(_ context.Context, workspace, mirrorURL, session string) (string, error) {
	f.recordScratchCall.called = true
	f.recordScratchCall.workspace = workspace
	f.recordScratchCall.mirrorURL = mirrorURL
	f.recordScratchCall.session = session
	return f.recordScratchRef, f.recordScratchErr // nosemgrep: no-bare-error-return
}

// progressSink is an httptest server that records the "chunk" of every progress
// POST it receives, so a test can assert lines were forwarded.
type progressSink struct {
	srv    *httptest.Server
	mu     sync.Mutex
	chunks []string
}

func newProgressSink() *progressSink {
	s := &progressSink{}
	s.srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		var m map[string]string
		if json.Unmarshal(body, &m) == nil {
			s.mu.Lock()
			s.chunks = append(s.chunks, m["chunk"])
			s.mu.Unlock()
		}
		w.WriteHeader(http.StatusOK)
	}))
	return s
}

func (s *progressSink) got() []string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]string(nil), s.chunks...)
}

// fakeSessionStore is a SessionStore double: it records the bytes handed to
// Hydrate and returns canned bytes/errors from Hydrate/Export.
type fakeSessionStore struct {
	hydrated   []byte
	hydrateErr error

	exportData []byte
	exportErr  error
}

func (f *fakeSessionStore) Hydrate(_ context.Context, data []byte) error {
	f.hydrated = data
	return f.hydrateErr // nosemgrep: no-bare-error-return
}

func (f *fakeSessionStore) Export(_ context.Context) ([]byte, error) {
	return f.exportData, f.exportErr
}

// TestMain points the task/context file paths at a temp dir so the handler's
// per-run file write does not touch the real /tmp during tests.
func TestMain(m *testing.M) {
	dir, err := os.MkdirTemp("", "goose-handler-test")
	if err != nil {
		panic(err)
	}
	taskFilePath = filepath.Join(dir, "task.md")
	contextFilePath = filepath.Join(dir, "context.md")
	code := m.Run()
	_ = os.RemoveAll(dir)
	os.Exit(code)
}

func invoke(t *testing.T, h shim.Handler, body string) (*shim.Response, error) {
	t.Helper()
	return h(context.Background(), &shim.Request{Path: "/invoke", Body: strings.NewReader(body)})
}

func decodeResult(t *testing.T, resp *shim.Response) AgentResult {
	t.Helper()
	var res AgentResult
	if err := json.Unmarshal(resp.Body, &res); err != nil {
		t.Fatalf("decode AgentResult: %v (body=%q)", err, resp.Body)
	}
	return res
}

func TestColdRunBuildsArgvStreamsAndReturnsResult(t *testing.T) {
	sink := newProgressSink()
	defer sink.srv.Close()

	runner := &fakeRunner{lines: []string{"step one", "step two"}, out: "final answer"}
	h := New(runner)

	req := AgentRequest{
		Recipe:      "agent",
		Task:        "do the thing",
		Session:     "sess-1",
		Env:         map[string]string{"GOOSE_PROVIDER": "openai", "GOOSE_MODEL": "qwen"},
		ProgressURL: sink.srv.URL,
	}
	body, _ := json.Marshal(req)

	resp, err := invoke(t, h, string(body))
	if err != nil {
		t.Fatalf("handler returned error: %v", err)
	}
	if resp.Status != 200 {
		t.Fatalf("status = %d, want 200", resp.Status)
	}

	res := decodeResult(t, resp)
	if res.Status != "ok" {
		t.Errorf("result status = %q, want ok (err=%q)", res.Status, res.Error)
	}
	if res.Result != "final answer" {
		t.Errorf("result = %q, want %q", res.Result, "final answer")
	}

	// argv reflects recipe + session + the task-file param (the task itself is
	// written to that file, not templated into the recipe).
	argv := strings.Join(runner.gotArgv, " ")
	for _, want := range []string{"--recipe agent", "--name sess-1", "task_file=" + taskFilePath} {
		if !strings.Contains(argv, want) {
			t.Errorf("argv %q missing %q", argv, want)
		}
	}
	// the task text landed in the task file
	if b, err := os.ReadFile(taskFilePath); err != nil || string(b) != "do the thing" {
		t.Errorf("task file = %q (err %v), want %q", b, err, "do the thing")
	}

	// env forwarded verbatim.
	if runner.gotEnv["GOOSE_PROVIDER"] != "openai" || runner.gotEnv["GOOSE_MODEL"] != "qwen" {
		t.Errorf("env not forwarded: %v", runner.gotEnv)
	}

	// progress lines streamed to the URL.
	if got := sink.got(); !equalStrings(got, []string{"step one", "step two"}) {
		t.Errorf("progress lines = %v, want [step one step two]", got)
	}
}

func TestGitMirrorTriggersCloneIntoWorkspace(t *testing.T) {
	runner := &fakeRunner{out: "ok"}
	h := New(runner)

	req := AgentRequest{Recipe: "agent", Task: "t", GitMirror: "https://git/mirror.git", GitRef: "abc123"}
	body, _ := json.Marshal(req)

	if _, err := invoke(t, h, string(body)); err != nil {
		t.Fatalf("handler returned error: %v", err)
	}
	if !runner.cloneCall.called {
		t.Fatal("clone was not called for a request with GitMirror")
	}
	if runner.cloneCall.mirror != "https://git/mirror.git" || runner.cloneCall.ref != "abc123" {
		t.Errorf("clone args = %+v", runner.cloneCall)
	}
	if runner.cloneCall.dest != Workspace {
		t.Errorf("clone dest = %q, want %q", runner.cloneCall.dest, Workspace)
	}
}

func TestCloneFailureContinuesWithEmptyWorkspace(t *testing.T) {
	// Clone failure is soft (best-effort per ADR 026): the run continues with an
	// empty workspace rather than aborting. The result is "ok" when goose itself
	// succeeds, even though the mirror clone failed.
	runner := &fakeRunner{cloneErr: io.ErrUnexpectedEOF, out: "ok despite no workspace"}
	h := New(runner)

	req := AgentRequest{Recipe: "agent", Task: "t", GitMirror: "git://mirror:9418/repo", GitRef: "main"}
	body, _ := json.Marshal(req)

	resp, err := invoke(t, h, string(body))
	if err != nil {
		t.Fatalf("handler returned error: %v", err)
	}
	if resp.Status != 200 {
		t.Fatalf("status = %d, want 200", resp.Status)
	}
	res := decodeResult(t, resp)
	// clone failure is soft: the run must not be reported as an error
	if res.Status != "ok" {
		t.Errorf("clone failure should not fail the run (soft-fail); got status %q error %q", res.Status, res.Error)
	}
}

// TestHandlerRecordsScratchRefOnSuccess verifies that after a successful goose
// run the handler calls RecordScratch and populates RecordedRef in the result.
func TestHandlerRecordsScratchRefOnSuccess(t *testing.T) {
	r := &fakeRunner{out: "done", recordScratchRef: "refs/agents/sess-5"}
	h := New(r)

	req := AgentRequest{
		Recipe:    "agent",
		Task:      "t",
		Session:   "sess-5",
		GitMirror: "git://mirror:9418/repo",
		GitRef:    "main",
	}
	body, _ := json.Marshal(req)

	resp, err := invoke(t, h, string(body))
	if err != nil {
		t.Fatalf("handler returned error: %v", err)
	}
	res := decodeResult(t, resp)
	if res.Status != "ok" {
		t.Fatalf("status = %q, want ok", res.Status)
	}

	// RecordScratch must have been called with the right args.
	if !r.recordScratchCall.called {
		t.Fatal("RecordScratch was not called")
	}
	if r.recordScratchCall.workspace != Workspace {
		t.Errorf("RecordScratch workspace = %q, want %q", r.recordScratchCall.workspace, Workspace)
	}
	if r.recordScratchCall.mirrorURL != "git://mirror:9418/repo" {
		t.Errorf("RecordScratch mirrorURL = %q, want git://mirror:9418/repo", r.recordScratchCall.mirrorURL)
	}
	if r.recordScratchCall.session != "sess-5" {
		t.Errorf("RecordScratch session = %q, want sess-5", r.recordScratchCall.session)
	}

	// RecordedRef set in the result.
	if res.RecordedRef != "refs/agents/sess-5" {
		t.Errorf("RecordedRef = %q, want refs/agents/sess-5", res.RecordedRef)
	}
}

// TestHandlerSkipsScratchWhenNoChanges verifies that when RecordScratch returns
// ("", nil) (workspace clean), RecordedRef is empty in the result.
func TestHandlerSkipsScratchWhenNoChanges(t *testing.T) {
	r := &fakeRunner{out: "done", recordScratchRef: ""} // empty ref = no changes
	h := New(r)

	req := AgentRequest{
		Recipe:    "agent",
		Task:      "t",
		Session:   "sess-6",
		GitMirror: "git://mirror:9418/repo",
		GitRef:    "main",
	}
	body, _ := json.Marshal(req)

	resp, err := invoke(t, h, string(body))
	if err != nil {
		t.Fatalf("handler returned error: %v", err)
	}
	res := decodeResult(t, resp)
	if res.Status != "ok" {
		t.Fatalf("status = %q, want ok", res.Status)
	}
	if res.RecordedRef != "" {
		t.Errorf("RecordedRef = %q, want empty when no changes", res.RecordedRef)
	}
}

// TestHandlerScratchFailureDoesNotFailRun verifies that a RecordScratch error
// does not fail a run that already succeeded (best-effort, non-fatal).
func TestHandlerScratchFailureDoesNotFailRun(t *testing.T) {
	r := &fakeRunner{out: "done", recordScratchErr: io.ErrUnexpectedEOF}
	h := New(r)

	req := AgentRequest{
		Recipe:    "agent",
		Task:      "t",
		Session:   "sess-7",
		GitMirror: "git://mirror:9418/repo",
		GitRef:    "main",
	}
	body, _ := json.Marshal(req)

	resp, err := invoke(t, h, string(body))
	if err != nil {
		t.Fatalf("handler returned error: %v", err)
	}
	res := decodeResult(t, resp)
	// A RecordScratch failure must not change the run status from "ok".
	if res.Status != "ok" {
		t.Errorf("RecordScratch failure should not fail the run; got status %q", res.Status)
	}
	if res.RecordedRef != "" {
		t.Errorf("RecordedRef = %q, want empty on failure", res.RecordedRef)
	}
}

// TestHandlerSkipsScratchWhenNoMirrorSet verifies that when no GitMirror is
// provided, RecordScratch is not called at all.
func TestHandlerSkipsScratchWhenNoMirrorSet(t *testing.T) {
	r := &fakeRunner{out: "done"}
	h := New(r)

	req := AgentRequest{Recipe: "agent", Task: "t", Session: "sess-8"}
	body, _ := json.Marshal(req)

	resp, err := invoke(t, h, string(body))
	if err != nil {
		t.Fatalf("handler returned error: %v", err)
	}
	res := decodeResult(t, resp)
	if res.Status != "ok" {
		t.Fatalf("status = %q, want ok", res.Status)
	}
	if r.recordScratchCall.called {
		t.Error("RecordScratch must not be called when no GitMirror is set")
	}
}

func TestGooseRunErrorIsErrorResultAt200(t *testing.T) {
	runner := &fakeRunner{out: "partial", runErr: io.ErrClosedPipe}
	h := New(runner)

	req := AgentRequest{Recipe: "agent", Task: "t"}
	body, _ := json.Marshal(req)

	resp, err := invoke(t, h, string(body))
	if err != nil {
		t.Fatalf("handler returned error: %v", err)
	}
	res := decodeResult(t, resp)
	if res.Status != "error" || res.Error == "" {
		t.Errorf("want error result, got %+v", res)
	}
	// goose's captured output is preserved on the error path so the failure is
	// self-diagnosing (a bare exit code is useless).
	if res.Result != "partial" {
		t.Errorf("error result should keep goose output for diagnosis, got Result %q", res.Result)
	}
}

func TestUndecodableBodyReturnsHandlerError(t *testing.T) {
	h := New(&fakeRunner{})
	resp, err := invoke(t, h, "{ this is not json")
	if err == nil {
		t.Fatal("want a handler error for an undecodable body, got nil")
	}
	if resp != nil {
		t.Errorf("want nil response on error, got %+v", resp)
	}
}

func TestResumeHydratesSessionAndExportsUpdatedDb(t *testing.T) {
	runner := &fakeRunner{out: "resumed answer"}
	store := &fakeSessionStore{exportData: []byte("updated-db")}
	h := New(runner, WithSessionStore(store))

	req := AgentRequest{
		Recipe:    "agent",
		Task:      "make it bigger",
		Session:   "sess-9",
		Resume:    true,
		SessionDb: base64.StdEncoding.EncodeToString([]byte("prior-db")),
	}
	body, _ := json.Marshal(req)

	resp, err := invoke(t, h, string(body))
	if err != nil {
		t.Fatalf("handler returned error: %v", err)
	}
	res := decodeResult(t, resp)
	if res.Status != "ok" {
		t.Fatalf("status = %q, want ok (err=%q)", res.Status, res.Error)
	}

	// prior db hydrated into the guest.
	if string(store.hydrated) != "prior-db" {
		t.Errorf("hydrated = %q, want %q", store.hydrated, "prior-db")
	}
	// argv resumes the named session AND re-passes --recipe so goose re-applies
	// the recipe's response schema + settings to the follow-up turn; the task goes
	// via --params (-t conflicts with --recipe in goose's CLI).
	argv := strings.Join(runner.gotArgv, " ")
	if !strings.Contains(argv, "--name sess-9 --resume") {
		t.Errorf("argv %q missing resume", argv)
	}
	if !strings.Contains(argv, "--recipe agent") {
		t.Errorf("argv %q should re-pass --recipe on resume", argv)
	}
	if !strings.Contains(argv, "task_file="+taskFilePath) {
		t.Errorf("argv %q should pass the task file via --params on resume", argv)
	}
	if b, err := os.ReadFile(taskFilePath); err != nil || string(b) != "make it bigger" {
		t.Errorf("resume task file = %q (err %v), want %q", b, err, "make it bigger")
	}
	// updated db exported back.
	wantDb := base64.StdEncoding.EncodeToString([]byte("updated-db"))
	if res.SessionDb != wantDb {
		t.Errorf("result SessionDb = %q, want %q", res.SessionDb, wantDb)
	}
}

func TestHydrateFailureFallsBackToColdRun(t *testing.T) {
	runner := &fakeRunner{out: "cold answer"}
	store := &fakeSessionStore{hydrateErr: io.ErrUnexpectedEOF}
	h := New(runner, WithSessionStore(store))

	req := AgentRequest{
		Recipe:    "agent",
		Task:      "t",
		Session:   "sess-10",
		Resume:    true,
		SessionDb: base64.StdEncoding.EncodeToString([]byte("corrupt")),
	}
	body, _ := json.Marshal(req)

	resp, err := invoke(t, h, string(body))
	if err != nil {
		t.Fatalf("handler returned error: %v", err)
	}
	if res := decodeResult(t, resp); res.Status != "ok" {
		t.Fatalf("status = %q, want ok (a hydrate failure must fall back, not fail)", res.Status)
	}
	// falls back to a cold recipe run, not a resume.
	argv := strings.Join(runner.gotArgv, " ")
	if strings.Contains(argv, "--resume") {
		t.Errorf("argv %q should fall back to cold (no --resume) on hydrate failure", argv)
	}
	if !strings.Contains(argv, "--recipe agent") {
		t.Errorf("argv %q missing cold recipe run", argv)
	}
}

func TestColdRunExportsFirstSession(t *testing.T) {
	runner := &fakeRunner{out: "first answer"}
	store := &fakeSessionStore{exportData: []byte("first-db")}
	h := New(runner, WithSessionStore(store))

	req := AgentRequest{Recipe: "agent", Task: "first", Session: "sess-11"}
	body, _ := json.Marshal(req)

	resp, err := invoke(t, h, string(body))
	if err != nil {
		t.Fatalf("handler returned error: %v", err)
	}
	res := decodeResult(t, resp)
	if store.hydrated != nil {
		t.Errorf("cold run should not hydrate, got %q", store.hydrated)
	}
	wantDb := base64.StdEncoding.EncodeToString([]byte("first-db"))
	if res.SessionDb != wantDb {
		t.Errorf("result SessionDb = %q, want %q (first run must persist its session)", res.SessionDb, wantDb)
	}
}

func TestFailedRunDoesNotExportSession(t *testing.T) {
	runner := &fakeRunner{out: "partial", runErr: io.ErrClosedPipe}
	store := &fakeSessionStore{exportData: []byte("should-not-be-read")}
	h := New(runner, WithSessionStore(store))

	req := AgentRequest{Recipe: "agent", Task: "t", Session: "sess-12"}
	body, _ := json.Marshal(req)

	resp, err := invoke(t, h, string(body))
	if err != nil {
		t.Fatalf("handler returned error: %v", err)
	}
	res := decodeResult(t, resp)
	if res.Status != "error" {
		t.Fatalf("status = %q, want error", res.Status)
	}
	if res.SessionDb != "" {
		t.Errorf("a failed run should not export a session, got %q", res.SessionDb)
	}
}

func TestArtifactHTMLReturnedWhenRecipeWroteIt(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "artifact.html")
	if err := os.WriteFile(path, []byte("<html>built</html>"), 0o644); err != nil {
		t.Fatalf("write artifact: %v", err)
	}
	old := artifactPath
	artifactPath = path
	defer func() { artifactPath = old }()

	runner := &fakeRunner{out: "done"}
	h := New(runner)
	body, _ := json.Marshal(AgentRequest{Recipe: "artifact", Task: "build", Session: "sess-a"})

	resp, err := invoke(t, h, string(body))
	if err != nil {
		t.Fatalf("handler returned error: %v", err)
	}
	res := decodeResult(t, resp)
	if res.Status != "ok" {
		t.Fatalf("status = %q, want ok", res.Status)
	}
	if res.ArtifactHTML != "<html>built</html>" {
		t.Errorf("ArtifactHTML = %q, want the built HTML", res.ArtifactHTML)
	}
}

func TestNoArtifactHTMLWhenAbsent(t *testing.T) {
	old := artifactPath
	artifactPath = filepath.Join(t.TempDir(), "missing.html")
	defer func() { artifactPath = old }()

	runner := &fakeRunner{out: "done"}
	h := New(runner)
	body, _ := json.Marshal(AgentRequest{Recipe: "agent", Task: "t"})

	resp, err := invoke(t, h, string(body))
	if err != nil {
		t.Fatalf("handler returned error: %v", err)
	}
	if res := decodeResult(t, resp); res.ArtifactHTML != "" {
		t.Errorf("ArtifactHTML = %q, want empty (no artifact written)", res.ArtifactHTML)
	}
}

func equalStrings(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}
