package server

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"os"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/embervm/noded/config"
)

// fakeDoer records every request it sees and returns a canned status, so a test
// drives the register path without a real control plane.
type fakeDoer struct {
	mu       sync.Mutex
	status   int
	err      error
	requests []recordedReq
}

type recordedReq struct {
	url  string
	auth string
	body registration
}

func (d *fakeDoer) Do(req *http.Request) (*http.Response, error) {
	d.mu.Lock()
	defer d.mu.Unlock()
	if d.err != nil {
		return nil, d.err
	}
	var body registration
	if req.Body != nil {
		raw, _ := io.ReadAll(req.Body)
		_ = json.Unmarshal(raw, &body)
	}
	d.requests = append(d.requests, recordedReq{
		url:  req.URL.String(),
		auth: req.Header.Get("Authorization"),
		body: body,
	})
	status := d.status
	if status == 0 {
		status = http.StatusOK
	}
	return &http.Response{StatusCode: status, Body: io.NopCloser(strings.NewReader(""))}, nil
}

func (d *fakeDoer) count() int {
	d.mu.Lock()
	defer d.mu.Unlock()
	return len(d.requests)
}

func (d *fakeDoer) last() (recordedReq, bool) {
	d.mu.Lock()
	defer d.mu.Unlock()
	if len(d.requests) == 0 {
		return recordedReq{}, false
	}
	return d.requests[len(d.requests)-1], true
}

func registerTestServer(cfg config.Config) *Server {
	return &Server{cfg: cfg, logger: slog.New(slog.NewTextHandler(io.Discard, nil))}
}

// register-posts-identity: one register/3 call POSTs {node, pod_uid, address,
// boot_id} to <url>/v1/nodes/register with the bearer header derived from the
// token path, and treats a 2xx as success.
func TestRegisterPostsIdentity(t *testing.T) {
	tokenFile := writeTempFile(t, "sa-token\n")
	s := registerTestServer(config.Config{
		Node:                  "node-4",
		PodUID:                "uid-abc",
		PodIP:                 "10.1.2.3",
		ListenAddr:            ":9090",
		ControlPlaneURL:       "http://cp.embervm.svc:8080/",
		ControlPlaneTokenPath: tokenFile,
	})
	doer := &fakeDoer{status: http.StatusOK}

	if err := s.register(context.Background(), doer, "boot-xyz"); err != nil {
		t.Fatalf("register returned error: %v", err)
	}

	req, ok := doer.last()
	if !ok {
		t.Fatal("no request recorded")
	}
	if req.url != "http://cp.embervm.svc:8080/v1/nodes/register" {
		t.Fatalf("unexpected url %q", req.url)
	}
	if req.auth != "Bearer sa-token" {
		t.Fatalf("unexpected auth header %q", req.auth)
	}
	if req.body.Node != "node-4" || req.body.PodUID != "uid-abc" {
		t.Fatalf("unexpected identity %+v", req.body)
	}
	if req.body.Address != "10.1.2.3:9090" {
		t.Fatalf("unexpected address %q", req.body.Address)
	}
	if req.body.BootID != "boot-xyz" {
		t.Fatalf("unexpected boot id %q", req.body.BootID)
	}
}

// register-rejects-non-2xx: a non-2xx control-plane response is a retryable
// error, not a crash.
func TestRegisterRejectsNon2xx(t *testing.T) {
	s := registerTestServer(config.Config{
		Node:            "node-4",
		ControlPlaneURL: "http://cp:8080",
		// no token path -> no auth header, still a valid request
		ControlPlaneTokenPath: "",
	})
	doer := &fakeDoer{status: http.StatusForbidden}

	err := s.register(context.Background(), doer, "boot")
	if err == nil {
		t.Fatal("expected error on 403")
	}
	req, _ := doer.last()
	if req.auth != "" {
		t.Fatalf("expected no auth header with empty token path, got %q", req.auth)
	}
}

// register-retries-without-crash: a transport error surfaces as a retryable
// error from register/3; the loop keeps going (covered by the loop test).
func TestRegisterRetriesWithoutCrash(t *testing.T) {
	s := registerTestServer(config.Config{Node: "node-4", ControlPlaneURL: "http://cp:8080"})
	doer := &fakeDoer{err: io.ErrUnexpectedEOF}

	if err := s.register(context.Background(), doer, "boot"); err == nil {
		t.Fatal("expected transport error to surface")
	}
}

// register-stops-on-drain: once the daemon is draining, the loop stops issuing
// new POSTs (the control plane ages the instance out).
func TestRegisterStopsOnDrain(t *testing.T) {
	s := registerTestServer(config.Config{
		Node:            "node-4",
		PodUID:          "uid-abc",
		ControlPlaneURL: "http://cp:8080",
	})
	doer := &fakeDoer{status: http.StatusOK}
	s.SetDraining(time.Now().Add(time.Minute))

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	// A very short interval so, were it re-registering, many POSTs would land.
	s.runRegisterLoop(ctx, doer, "boot", time.Millisecond)
	time.Sleep(30 * time.Millisecond)

	if n := doer.count(); n != 0 {
		t.Fatalf("draining daemon issued %d registrations; want 0", n)
	}
}

// register-loop-registers-then-ticks: the loop registers once immediately and
// re-registers on the interval; ctx cancellation stops it.
func TestRegisterLoopRegistersThenTicks(t *testing.T) {
	s := registerTestServer(config.Config{
		Node:            "node-4",
		PodUID:          "uid-abc",
		ControlPlaneURL: "http://cp:8080",
	})
	var doer countingDoer
	ctx, cancel := context.WithCancel(context.Background())
	s.runRegisterLoop(ctx, &doer, "boot", time.Millisecond)

	// Wait for at least two registrations (immediate + one tick).
	deadline := time.Now().Add(time.Second)
	for doer.count() < 2 && time.Now().Before(deadline) {
		time.Sleep(2 * time.Millisecond)
	}
	if doer.count() < 2 {
		t.Fatalf("expected at least 2 registrations, got %d", doer.count())
	}

	cancel()
	stopped := doer.count()
	time.Sleep(20 * time.Millisecond)
	if after := doer.count(); after > stopped+1 {
		t.Fatalf("loop kept registering after ctx cancel: %d -> %d", stopped, after)
	}
}

type countingDoer struct{ n int64 }

func (d *countingDoer) Do(req *http.Request) (*http.Response, error) {
	atomic.AddInt64(&d.n, 1)
	return &http.Response{StatusCode: http.StatusOK, Body: io.NopCloser(strings.NewReader(""))}, nil
}

func (d *countingDoer) count() int { return int(atomic.LoadInt64(&d.n)) }

// failThenOKDoer returns a transport error for the first `failFor` calls, then
// 2xx. It models a fresh pod whose first dial-home POST races control-plane
// reachability (DNS/route not ready) before the control plane becomes dialable.
type failThenOKDoer struct {
	mu      sync.Mutex
	n       int
	failFor int
}

func (d *failThenOKDoer) Do(_ *http.Request) (*http.Response, error) {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.n++
	if d.n <= d.failFor {
		return nil, io.ErrUnexpectedEOF
	}
	return &http.Response{StatusCode: http.StatusOK, Body: io.NopCloser(strings.NewReader(""))}, nil
}

func (d *failThenOKDoer) count() int {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.n
}

// register-fast-retries-until-first-success: when the first POST(s) fail, the
// loop must re-attempt on the SHORT fast-retry backoff, never idle the full
// steady interval. With a steady interval far larger than the fast-retry base, a
// fresh instance that fails its first two POSTs still registers within a couple
// of fast-retry ticks (the co-location base-advertisement window fix). This test
// pins the behaviour with registerFastRetryBase kept small by the production
// constant and a deliberately large steady interval: if the loop waited the
// steady interval on failure, no re-attempt would land inside the window.
func TestRegisterFastRetriesUntilFirstSuccess(t *testing.T) {
	s := registerTestServer(config.Config{
		Node:            "node-4",
		PodUID:          "uid-abc",
		ControlPlaneURL: "http://cp:8080",
	})
	// Fail the first two POSTs (immediate + one fast-retry), succeed on the third.
	doer := &failThenOKDoer{failFor: 2}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	// A steady interval far larger than registerFastRetryBase (1s): were the loop
	// to wait the steady interval on a failed first POST, the third attempt would
	// not land for minutes. The fast-retry (1s, then 2s) must land it in seconds.
	s.runRegisterLoop(ctx, doer, "boot", time.Hour)

	deadline := time.Now().Add(5 * time.Second)
	for doer.count() < 3 && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	if got := doer.count(); got < 3 {
		t.Fatalf("expected the loop to fast-retry to a successful registration (>=3 POSTs) within the window, got %d", got)
	}
}

// grpc-port-parse: the advertised port is extracted from various ListenAddr
// shapes, defaulting to 9090.
func TestGrpcPortOf(t *testing.T) {
	cases := map[string]string{
		":9090":          "9090",
		"0.0.0.0:9090":   "9090",
		"127.0.0.1:7000": "7000",
		"":               "9090",
		"noport":         "9090",
	}
	for in, want := range cases {
		if got := grpcPortOf(in); got != want {
			t.Fatalf("grpcPortOf(%q) = %q, want %q", in, got, want)
		}
	}
}

func writeTempFile(t *testing.T, contents string) string {
	t.Helper()
	dir := t.TempDir()
	path := dir + "/token"
	if err := os.WriteFile(path, []byte(contents), 0o600); err != nil {
		t.Fatalf("write temp file: %v", err)
	}
	return path
}
