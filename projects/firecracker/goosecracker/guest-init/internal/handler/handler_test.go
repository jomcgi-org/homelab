package handler

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"

	"github.com/jomcgi/homelab/projects/firecracker/substrate/shim"
)

// fakeRunner is a Runner test double: it records the argv/env it was invoked
// with, replays a canned set of output lines through onLine, and reports whether
// Clone was called and with which args. It runs no real goose or git.
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

	// argv reflects recipe + task + session.
	argv := strings.Join(runner.gotArgv, " ")
	for _, want := range []string{"--recipe agent", "--name sess-1", "task_description=do the thing"} {
		if !strings.Contains(argv, want) {
			t.Errorf("argv %q missing %q", argv, want)
		}
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

func TestCloneFailureIsErrorResultAt200(t *testing.T) {
	runner := &fakeRunner{cloneErr: io.ErrUnexpectedEOF}
	h := New(runner)

	req := AgentRequest{Recipe: "agent", Task: "t", GitMirror: "https://git/mirror.git"}
	body, _ := json.Marshal(req)

	resp, err := invoke(t, h, string(body))
	if err != nil {
		t.Fatalf("handler returned error: %v", err)
	}
	if resp.Status != 200 {
		t.Fatalf("status = %d, want 200", resp.Status)
	}
	res := decodeResult(t, resp)
	if res.Status != "error" || res.Error == "" {
		t.Errorf("want error result, got %+v", res)
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
	// argv resumes the named session (no --recipe).
	argv := strings.Join(runner.gotArgv, " ")
	if !strings.Contains(argv, "--name sess-9 --resume") {
		t.Errorf("argv %q missing resume", argv)
	}
	if strings.Contains(argv, "--recipe") {
		t.Errorf("argv %q should not re-pass --recipe on resume", argv)
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
