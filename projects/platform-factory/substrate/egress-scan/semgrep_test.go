package main

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	authv3 "github.com/envoyproxy/go-control-plane/envoy/service/auth/v3"
	typev3 "github.com/envoyproxy/go-control-plane/envoy/type/v3"
	"github.com/prometheus/client_golang/prometheus"
	"google.golang.org/grpc/codes"
)

// A synthetic example of a recognized credential format.
var secretFixture = "gh" + "p_" + "8Jdm9u1Qa7bWdF2p7XZ9sWy7Qnf2gXMbRFqa"

func testConfig() semgrepConfig {
	return semgrepConfig{
		Endpoint:        "http://engine.invalid:8080/",
		Deadline:        300 * time.Millisecond,
		FailureMode:     failureDeny,
		MaxBody:         maxBody,
		Concurrency:     4,
		MaxResponse:     1 << 20,
		BreakerFailures: 0,
		BreakerCooldown: time.Second,
	}
}

type engineFunc func(ctx context.Context, body []byte) (Verdict, error)

func (f engineFunc) Scan(ctx context.Context, body []byte) (Verdict, error) { return f(ctx, body) }

func newTierScanner(t *testing.T, cfg semgrepConfig, engine Engine) (*scanner, *prometheus.Registry) {
	t.Helper()
	tier, err := newSemgrepTier(cfg, engine)
	if err != nil {
		t.Fatal(err)
	}
	s := newTestScanner(t)
	s.semgrep = tier
	registry := prometheus.NewRegistry()
	newMeteredScanner(s, registry)
	return s, registry
}

func cleanRequest(body string) *authv3.CheckRequest {
	return request(&authv3.AttributeContext_HttpRequest{
		Method: "POST", Host: "hindsight.example", Path: "/mcp/", RawBody: []byte(body), Size: int64(len(body)),
		Headers: map[string]string{"content-type": "text/plain"},
	})
}

func captureLog(t *testing.T) *bytes.Buffer {
	t.Helper()
	var buf bytes.Buffer
	log.SetOutput(&buf)
	t.Cleanup(func() { log.SetOutput(io.Discard) })
	return &buf
}

func counter(t *testing.T, registry *prometheus.Registry, name string, labels map[string]string) float64 {
	t.Helper()
	families, err := registry.Gather()
	if err != nil {
		t.Fatal(err)
	}
	for _, family := range families {
		if family.GetName() != name {
			continue
		}
		for _, metric := range family.GetMetric() {
			got := map[string]string{}
			for _, l := range metric.GetLabel() {
				got[l.GetName()] = l.GetValue()
			}
			match := true
			for k, v := range labels {
				match = match && got[k] == v
			}
			if match {
				if metric.GetCounter() != nil {
					return metric.GetCounter().GetValue()
				}
				return metric.GetGauge().GetValue()
			}
		}
	}
	t.Fatalf("metric %s %v not found", name, labels)
	return 0
}

// ---- HTTP engine client ----

const engineFindingJSON = ".\n" + `{"version":"1.186.0","results":[` +
	`{"check_id":"aws-key-0","path":"/scan/target.json","start":{"line":1,"col":5,"offset":4},"end":{"line":1,"col":25,"offset":24},` +
	`"extra":{"lines":"SECRET-MATCHED-TEXT","message":"aws-key","severity":"ERROR","validation_state":"NO_VALIDATOR","is_ignored":false}},` +
	`{"check_id":"generic-token-3","path":"/scan/target.json","start":{"line":2,"col":1,"offset":40},"end":{"line":2,"col":9,"offset":48},` +
	`"extra":{"is_ignored":false,"validation_state":"CONFIRMED_INVALID"}}],"errors":[]}`

func newFakeEngine(t *testing.T, handler http.HandlerFunc) (*httpEngine, *httptest.Server) {
	t.Helper()
	srv := httptest.NewServer(handler)
	t.Cleanup(srv.Close)
	engine, err := newHTTPEngine(srv.URL+"/", 1<<20)
	if err != nil {
		t.Fatal(err)
	}
	return engine, srv
}

func TestHTTPEngineVerdicts(t *testing.T) {
	cases := []struct {
		name     string
		handler  http.HandlerFunc
		findings []Finding
		err      error
	}{
		{name: "clean", handler: func(w http.ResponseWriter, r *http.Request) {
			io.WriteString(w, ".\n{\"results\":[],\"errors\":[]}")
		}},
		{name: "findings with progress line", handler: func(w http.ResponseWriter, r *http.Request) {
			io.WriteString(w, engineFindingJSON)
		}, findings: []Finding{{"aws-key-0", 4, 24}, {"generic-token-3", 40, 48}}},
		{name: "ignored result is still a finding", handler: func(w http.ResponseWriter, r *http.Request) {
			// An actor controls the text, so it can add a nosemgrep marker.
			io.WriteString(w, `{"results":[{"check_id":"a","start":{"offset":1},"end":{"offset":2},"extra":{"is_ignored":true}}],"errors":[]}`)
		}, findings: []Finding{{"a", 1, 2}}},
		{name: "unsafe rule id is replaced", handler: func(w http.ResponseWriter, r *http.Request) {
			io.WriteString(w, `{"results":[{"check_id":"x y\n`+secretFixture+`","start":{"offset":-3},"end":{"offset":2},"extra":{}}]}`)
		}, findings: []Finding{{"invalid-rule-id", 0, 2}}},
		{name: "warning level error without findings is incomplete", handler: func(w http.ResponseWriter, r *http.Request) {
			// The CLI reports a rule timeout or out-of-memory at level warn.
			io.WriteString(w, `{"results":[],"errors":[{"level":"warn","type":"Timeout"}]}`)
		}, err: errEngineIncomplete},
		{name: "info level error without findings is incomplete", handler: func(w http.ResponseWriter, r *http.Request) {
			io.WriteString(w, `{"results":[],"errors":[{"level":"info"}]}`)
		}, err: errEngineIncomplete},
		{name: "error without a level is incomplete", handler: func(w http.ResponseWriter, r *http.Request) {
			io.WriteString(w, `{"results":[],"errors":[{}]}`)
		}, err: errEngineIncomplete},
		{name: "scan error without findings is incomplete", handler: func(w http.ResponseWriter, r *http.Request) {
			io.WriteString(w, `{"results":[],"errors":[{"level":"error"}]}`)
		}, err: errEngineIncomplete},
		{name: "skipped path without findings is incomplete", handler: func(w http.ResponseWriter, r *http.Request) {
			// Text over the default target size limit is skipped.
			io.WriteString(w, `{"results":[],"errors":[],"paths":{"scanned":[],"skipped":[{"path":"/scan/target.json","reason":"file_too_large"}]}}`)
		}, err: errEngineIncomplete},
		{name: "empty skipped list is clean", handler: func(w http.ResponseWriter, r *http.Request) {
			io.WriteString(w, `{"results":[],"errors":[],"paths":{"scanned":["/scan/target.json"],"skipped":[]}}`)
		}},
		{name: "scanned paths only is clean", handler: func(w http.ResponseWriter, r *http.Request) {
			io.WriteString(w, `{"results":[],"errors":[],"paths":{"scanned":["/scan/target.json"]}}`)
		}},
		{name: "skipped path with a finding still finds", handler: func(w http.ResponseWriter, r *http.Request) {
			io.WriteString(w, `{"results":[{"check_id":"a","start":{"offset":1},"end":{"offset":2}}],"paths":{"skipped":[{"path":"x"}]}}`)
		}, findings: []Finding{{"a", 1, 2}}},
		{name: "scan error with a finding still finds", handler: func(w http.ResponseWriter, r *http.Request) {
			io.WriteString(w, `{"results":[{"check_id":"a","start":{"offset":1},"end":{"offset":2},"extra":{}}],"errors":[{"level":"error"}]}`)
		}, findings: []Finding{{"a", 1, 2}}},
		{name: "server error", handler: func(w http.ResponseWriter, r *http.Request) {
			http.Error(w, "boom "+secretFixture, http.StatusInternalServerError)
		}, err: errEngineStatus},
		{name: "garbage", handler: func(w http.ResponseWriter, r *http.Request) {
			io.WriteString(w, "<html>not json</html>")
		}, err: errEngineGarbage},
		{name: "text before the JSON", handler: func(w http.ResponseWriter, r *http.Request) {
			io.WriteString(w, `x{"results":[]}`)
		}, err: errEngineGarbage},
		{name: "text after the progress line", handler: func(w http.ResponseWriter, r *http.Request) {
			io.WriteString(w, ".\nwarning: something\n{\"results\":[]}")
		}, err: errEngineGarbage},
		{name: "dots and blank lines before the JSON", handler: func(w http.ResponseWriter, r *http.Request) {
			io.WriteString(w, "..\r\n\n .\n{\"results\":[]}")
		}},
		{name: "broken json", handler: func(w http.ResponseWriter, r *http.Request) {
			io.WriteString(w, `.\n{"results":[{"check_id":`)
		}, err: errEngineGarbage},
		{name: "json without results", handler: func(w http.ResponseWriter, r *http.Request) {
			io.WriteString(w, `{"errors":[]}`)
		}, err: errEngineGarbage},
		{name: "empty body", handler: func(w http.ResponseWriter, r *http.Request) {}, err: errEngineGarbage},
		{name: "response over the limit", handler: func(w http.ResponseWriter, r *http.Request) {
			io.WriteString(w, `{"results":[],"pad":"`+strings.Repeat("a", 2<<20)+`"}`)
		}, err: errEngineTooLarge},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			engine, _ := newFakeEngine(t, tc.handler)
			verdict, err := engine.Scan(context.Background(), []byte("body"))
			if tc.err != nil {
				if !errors.Is(err, tc.err) {
					t.Fatalf("got error %v, want %v", err, tc.err)
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			if fmt.Sprint(verdict.Findings) != fmt.Sprint(tc.findings) {
				t.Fatalf("findings %v, want %v", verdict.Findings, tc.findings)
			}
		})
	}
}

// An engine that never ends its response must not hold the scanner. The
// response has no Content-Length and no end. io.LimitReader stops the read.
func TestHTTPEngineStopsAnEndlessResponse(t *testing.T) {
	stopped := make(chan struct{})
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		defer close(stopped)
		flusher := w.(http.Flusher)
		chunk := []byte(strings.Repeat("a", 1024))
		io.WriteString(w, `{"results":[],"pad":"`)
		for {
			select {
			case <-r.Context().Done():
				return
			default:
			}
			if _, err := w.Write(chunk); err != nil {
				return
			}
			flusher.Flush()
		}
	}))
	defer srv.Close()
	engine, err := newHTTPEngine(srv.URL+"/", 4096)
	if err != nil {
		t.Fatal(err)
	}
	// No deadline on the context: only the size limit can end the call.
	done := make(chan error, 1)
	go func() {
		_, err := engine.Scan(context.Background(), []byte("body"))
		done <- err
	}()
	select {
	case err := <-done:
		if !errors.Is(err, errEngineTooLarge) {
			t.Fatalf("got %v, want the size limit error", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("an endless response was not stopped by the size limit")
	}
	select {
	case <-stopped:
	case <-time.After(5 * time.Second):
		t.Fatal("the connection to the endless engine stayed open")
	}
}

func TestHTTPEngineSendsOnePostWithTheText(t *testing.T) {
	var got struct {
		method, contentType, path string
		length                    int64
		body                      []byte
	}
	engine, _ := newFakeEngine(t, func(w http.ResponseWriter, r *http.Request) {
		got.method, got.contentType, got.path, got.length = r.Method, r.Header.Get("Content-Type"), r.URL.Path, r.ContentLength
		got.body, _ = io.ReadAll(r.Body)
		io.WriteString(w, `{"results":[]}`)
	})
	if _, err := engine.Scan(context.Background(), []byte("hello")); err != nil {
		t.Fatal(err)
	}
	if got.method != "POST" || got.length != 5 || string(got.body) != "hello" || got.path != "/" || !strings.HasPrefix(got.contentType, "text/plain") {
		t.Fatalf("unexpected request %+v", got)
	}
}

func TestHTTPEngineDoesNotFollowRedirects(t *testing.T) {
	var elsewhere atomic.Int32
	other := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		elsewhere.Add(1)
		io.WriteString(w, `{"results":[]}`)
	}))
	defer other.Close()
	engine, _ := newFakeEngine(t, func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, other.URL, http.StatusTemporaryRedirect)
	})
	if _, err := engine.Scan(context.Background(), []byte("body")); !errors.Is(err, errEngineStatus) {
		t.Fatalf("got %v, want a status error", err)
	}
	if elsewhere.Load() != 0 {
		t.Fatal("the body was sent to the redirect target")
	}
}

func TestHTTPEngineConnectionRefused(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	addr := listener.Addr().String()
	listener.Close()
	engine, err := newHTTPEngine("http://"+addr+"/", 1<<20)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := engine.Scan(context.Background(), []byte("body")); err == nil {
		t.Fatal("expected an error")
	}
}

func TestHTTPEngineRejectsBadEndpoints(t *testing.T) {
	for _, endpoint := range []string{"", "engine:8080", "ftp://engine/", "http://", "http://user:pass@engine:8080/", "http://engine:8080/#x"} {
		if _, err := newHTTPEngine(endpoint, 1<<20); err == nil {
			t.Errorf("endpoint %q accepted", endpoint)
		}
	}
	if _, err := newHTTPEngine("https://engine.ate-system.svc:8080/scan", 1<<20); err != nil {
		t.Fatal(err)
	}
}

func TestSemgrepConfigBounds(t *testing.T) {
	bad := []func(*semgrepConfig){
		func(c *semgrepConfig) { c.Deadline = time.Millisecond },
		func(c *semgrepConfig) { c.Deadline = time.Minute },
		func(c *semgrepConfig) { c.FailureMode = "maybe" },
		func(c *semgrepConfig) { c.MaxBody = 0 },
		func(c *semgrepConfig) { c.MaxBody = maxBody + 1 },
		func(c *semgrepConfig) { c.Concurrency = 0 },
		func(c *semgrepConfig) { c.MaxResponse = 10 },
		func(c *semgrepConfig) { c.BreakerFailures = -1 },
		func(c *semgrepConfig) { c.BreakerFailures = 3; c.BreakerCooldown = 0 },
	}
	for i, mutate := range bad {
		cfg := testConfig()
		mutate(&cfg)
		if _, err := newSemgrepTier(cfg, engineFunc(nil)); err == nil {
			t.Errorf("case %d accepted", i)
		}
	}
	cfg := testConfig()
	cfg.Deadline = 75 * time.Millisecond // a less memory constrained deployment
	if _, err := newSemgrepTier(cfg, engineFunc(nil)); err != nil {
		t.Fatal(err)
	}
}

// ---- Check with the second tier ----

func TestTierCleanAndFinding(t *testing.T) {
	logs := captureLog(t)
	var seen []byte
	var mu sync.Mutex
	engine := engineFunc(func(ctx context.Context, body []byte) (Verdict, error) {
		mu.Lock()
		seen = body
		mu.Unlock()
		if bytes.Contains(body, []byte("engine-should-flag")) {
			return Verdict{Findings: []Finding{{"aws-key-0", 3, 9}}}, nil
		}
		return Verdict{}, nil
	})
	s, registry := newTierScanner(t, testConfig(), engine)

	reply, err := s.Check(context.Background(), cleanRequest("all fine"))
	if err != nil || reply.GetStatus().GetCode() != int32(codes.OK) {
		t.Fatalf("clean request: %v %v", reply, err)
	}
	mu.Lock()
	text := string(seen)
	mu.Unlock()
	if !strings.Contains(text, "hindsight.example") || !strings.Contains(text, "/mcp/") || !strings.Contains(text, "all fine") {
		t.Fatalf("engine text lacks the scanned fields: %q", text)
	}

	reply, err = s.Check(context.Background(), cleanRequest("engine-should-flag"))
	if err != nil {
		t.Fatal(err)
	}
	if reply.GetDeniedResponse().GetStatus().GetCode() != typev3.StatusCode_Forbidden ||
		reply.GetDeniedResponse().GetBody() != "request contains a credential pattern" {
		t.Fatalf("finding not denied: %v", reply)
	}
	if strings.Contains(reply.String(), "aws-key-0") {
		t.Error("the denial names the rule to the actor")
	}
	if !strings.Contains(logs.String(), "aws-key-0@3-9") || strings.Contains(logs.String(), "engine-should-flag") {
		t.Fatalf("log must hold rule and position, not text: %q", logs.String())
	}
	if got := counter(t, registry, "factory_egress_scan_semgrep_agreement_total", map[string]string{"gitleaks": "clean", "semgrep": "found"}); got != 1 {
		t.Errorf("semgrep-only agreement = %v", got)
	}
	if got := counter(t, registry, "factory_egress_scan_semgrep_agreement_total", map[string]string{"gitleaks": "clean", "semgrep": "clean"}); got != 1 {
		t.Errorf("both-clean agreement = %v", got)
	}
}

func TestTierMeteredOutcomes(t *testing.T) {
	engine := engineFunc(func(ctx context.Context, body []byte) (Verdict, error) {
		if bytes.Contains(body, []byte("flag-me")) {
			return Verdict{Findings: []Finding{{"r", 0, 1}}}, nil
		}
		return Verdict{}, nil
	})
	tier, err := newSemgrepTier(testConfig(), engine)
	if err != nil {
		t.Fatal(err)
	}
	base := newTestScanner(t)
	base.semgrep = tier
	registry := prometheus.NewRegistry()
	metered := newMeteredScanner(base, registry)
	if _, err := metered.Check(context.Background(), cleanRequest("flag-me")); err != nil {
		t.Fatal(err)
	}
	if _, err := metered.Check(context.Background(), cleanRequest("fine")); err != nil {
		t.Fatal(err)
	}
	if got := counter(t, registry, "factory_egress_scan_checks_total", map[string]string{"outcome": "credential_blocked"}); got != 1 {
		t.Errorf("credential_blocked = %v", got)
	}
	if got := counter(t, registry, "factory_egress_scan_checks_total", map[string]string{"outcome": "allowed"}); got != 1 {
		t.Errorf("allowed = %v", got)
	}
	if got := counter(t, registry, "factory_egress_scan_semgrep_requests_total", map[string]string{"outcome": "found"}); got != 1 {
		t.Errorf("found = %v", got)
	}
	if got := counter(t, registry, "factory_egress_scan_semgrep_deadline_seconds", nil); got != 0.3 {
		t.Errorf("deadline gauge = %v", got)
	}
}

func TestGitleaksDenyWinsWithoutWaitingForTheEngine(t *testing.T) {
	started := make(chan struct{})
	released := make(chan struct{})
	engine := engineFunc(func(ctx context.Context, body []byte) (Verdict, error) {
		close(started)
		<-ctx.Done()
		close(released)
		return Verdict{}, ctx.Err()
	})
	cfg := testConfig()
	cfg.Deadline = 5 * time.Second
	s, registry := newTierScanner(t, cfg, engine)

	begin := time.Now()
	reply, err := s.Check(context.Background(), cleanRequest("token "+secretFixture))
	if err != nil {
		t.Fatal(err)
	}
	if elapsed := time.Since(begin); elapsed > time.Second {
		t.Fatalf("Gitleaks deny waited %v for the engine", elapsed)
	}
	if reply.GetDeniedResponse().GetStatus().GetCode() != typev3.StatusCode_Forbidden || strings.Contains(reply.String(), secretFixture) {
		t.Fatalf("unexpected reply %v", reply)
	}
	select {
	case <-released:
	case <-time.After(2 * time.Second):
		t.Fatal("the engine call was not cancelled")
	}
	<-started
	waitFor(t, func() bool {
		return counter(t, registry, "factory_egress_scan_semgrep_requests_total", map[string]string{"outcome": "cancelled"}) == 1
	})
	// A cancelled call does not apply the failure mode.
	for _, action := range []string{"deny", "allow"} {
		if got := counter(t, registry, "factory_egress_scan_semgrep_failure_mode_total", map[string]string{"action": action}); got != 0 {
			t.Errorf("failure mode %s counted %v", action, got)
		}
	}
}

func TestGitleaksDenyWinsWhenTheEngineFails(t *testing.T) {
	engine := engineFunc(func(ctx context.Context, body []byte) (Verdict, error) {
		return Verdict{}, errors.New("engine down")
	})
	s, _ := newTierScanner(t, testConfig(), engine)
	reply, err := s.Check(context.Background(), cleanRequest("token "+secretFixture))
	if err != nil {
		t.Fatal(err)
	}
	if reply.GetStatus().GetCode() != int32(codes.PermissionDenied) || reply.GetDeniedResponse().GetBody() != "request contains a credential pattern" {
		t.Fatalf("Gitleaks verdict lost: %v", reply)
	}
}

func TestAgreementCountsOnlyVerdicts(t *testing.T) {
	registry := prometheus.NewRegistry()
	tier, err := newSemgrepTier(testConfig(), engineFunc(nil))
	if err != nil {
		t.Fatal(err)
	}
	tier.metrics.register(registry)
	tier.metrics.agree(true, engineResult{outcome: engineFound})
	tier.metrics.agree(true, engineResult{outcome: engineClean})
	for _, outcome := range []string{engineCancelled, engineTimeout, engineError, engineShed} {
		tier.metrics.agree(true, engineResult{outcome: outcome})
		tier.metrics.agree(false, engineResult{outcome: outcome})
	}
	want := map[[2]string]float64{{"found", "found"}: 1, {"found", "clean"}: 1, {"clean", "found"}: 0, {"clean", "clean"}: 0}
	for k, v := range want {
		if got := counter(t, registry, "factory_egress_scan_semgrep_agreement_total", map[string]string{"gitleaks": k[0], "semgrep": k[1]}); got != v {
			t.Errorf("%v = %v, want %v", k, got, v)
		}
	}
}

func TestSlowEngineAppliesDeadlineAndFailureMode(t *testing.T) {
	logs := captureLog(t)
	for _, mode := range []string{failureDeny, failureAllow} {
		t.Run(mode, func(t *testing.T) {
			engine := engineFunc(func(ctx context.Context, body []byte) (Verdict, error) {
				<-ctx.Done()
				return Verdict{}, ctx.Err()
			})
			cfg := testConfig()
			cfg.Deadline = 60 * time.Millisecond
			cfg.FailureMode = mode
			s, registry := newTierScanner(t, cfg, engine)
			begin := time.Now()
			reply, err := s.Check(context.Background(), cleanRequest("fine"))
			elapsed := time.Since(begin)
			if err != nil {
				t.Fatal(err)
			}
			if elapsed < 55*time.Millisecond || elapsed > time.Second {
				t.Fatalf("returned after %v, want about the 60ms deadline", elapsed)
			}
			if mode == failureDeny {
				if reply.GetDeniedResponse().GetStatus().GetCode() != typev3.StatusCode_ServiceUnavailable || reply.GetDeniedResponse().GetBody() != engineUnavailableReason {
					t.Fatalf("not denied as unavailable: %v", reply)
				}
				if checkOutcome(reply, nil) != engineUnavailableOutcome {
					t.Errorf("metric outcome %q", checkOutcome(reply, nil))
				}
			} else if reply.GetStatus().GetCode() != int32(codes.OK) {
				t.Fatalf("allow mode denied: %v", reply)
			}
			if got := counter(t, registry, "factory_egress_scan_semgrep_requests_total", map[string]string{"outcome": "timeout"}); got != 1 {
				t.Errorf("timeout = %v", got)
			}
			if got := counter(t, registry, "factory_egress_scan_semgrep_failure_mode_total", map[string]string{"action": mode}); got != 1 {
				t.Errorf("failure mode %s = %v", mode, got)
			}
		})
	}
	if !strings.Contains(logs.String(), "semgrep tier timeout, failure mode") {
		t.Errorf("a late engine must be visible in the log: %q", logs.String())
	}
}

func TestEngineFailuresApplyTheFailureMode(t *testing.T) {
	logs := captureLog(t)
	cases := []struct {
		name    string
		handler http.HandlerFunc
	}{
		{"500", func(w http.ResponseWriter, r *http.Request) { http.Error(w, secretFixture, 500) }},
		{"garbage", func(w http.ResponseWriter, r *http.Request) { io.WriteString(w, "nope "+secretFixture) }},
		{"huge", func(w http.ResponseWriter, r *http.Request) {
			io.WriteString(w, `{"results":[],"pad":"`+strings.Repeat("a", 2<<20)+`"}`)
		}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			engine, _ := newFakeEngine(t, tc.handler)
			s, registry := newTierScanner(t, testConfig(), engine)
			reply, err := s.Check(context.Background(), cleanRequest("fine"))
			if err != nil {
				t.Fatal(err)
			}
			if reply.GetDeniedResponse().GetStatus().GetCode() != typev3.StatusCode_ServiceUnavailable {
				t.Fatalf("not denied: %v", reply)
			}
			if got := counter(t, registry, "factory_egress_scan_semgrep_requests_total", map[string]string{"outcome": "error"}); got != 1 {
				t.Errorf("error = %v", got)
			}
		})
	}
	if strings.Contains(logs.String(), secretFixture) {
		t.Error("engine output reached the log")
	}
}

func TestEnginePanicIsAnEngineErrorNotACrash(t *testing.T) {
	logs := captureLog(t)
	engine := engineFunc(func(ctx context.Context, body []byte) (Verdict, error) {
		panic("boom " + string(body))
	})
	for _, mode := range []string{failureDeny, failureAllow} {
		cfg := testConfig()
		cfg.FailureMode = mode
		cfg.BreakerFailures = 2
		s, registry := newTierScanner(t, cfg, engine)
		for i := 0; i < 3; i++ {
			reply, err := s.Check(context.Background(), cleanRequest("fine SENSITIVE-BODY-TEXT"))
			if err != nil {
				t.Fatal(err)
			}
			if allowed := reply.GetStatus().GetCode() == int32(codes.OK); allowed != (mode == failureAllow) {
				t.Fatalf("mode %s allowed=%v", mode, allowed)
			}
		}
		// Two panics are two failures: the breaker opened for the third call.
		if got := counter(t, registry, "factory_egress_scan_semgrep_requests_total", map[string]string{"outcome": "error"}); got != 2 {
			t.Errorf("error = %v", got)
		}
		if got := counter(t, registry, "factory_egress_scan_semgrep_inflight", nil); got != 0 {
			t.Errorf("inflight = %v after a panic", got)
		}
	}
	if !strings.Contains(logs.String(), "engine panicked") || strings.Contains(logs.String(), "SENSITIVE-BODY-TEXT") {
		t.Fatalf("log must name the panic and hold no body: %q", logs.String())
	}
}

// The start goroutine has its own recover for a panic outside Engine.Scan.
func TestStartRecoversAPanicOutsideTheEngine(t *testing.T) {
	tier, err := newSemgrepTier(testConfig(), engineFunc(nil))
	if err != nil {
		t.Fatal(err)
	}
	tier.metrics = nil // run dereferences the metrics, so it panics
	select {
	case res := <-tier.start(context.Background(), []byte("x"), 1):
		if res.outcome != engineError || !errors.Is(res.err, errEnginePanic) {
			t.Fatalf("got %+v", res)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("no result after a panic")
	}
}

func TestLogsAreRateLimited(t *testing.T) {
	logs := captureLog(t)
	tier, err := newSemgrepTier(testConfig(), engineFunc(nil))
	if err != nil {
		t.Fatal(err)
	}
	clock := time.Now()
	tier.now = func() time.Time { return clock }
	found := engineResult{outcome: engineFound, verdict: Verdict{Findings: []Finding{{"aws-key-0", 4, 24}}}}
	timeout := engineResult{outcome: engineTimeout}
	for i := 0; i < 100; i++ {
		tier.decide(found)
		tier.decide(timeout)
	}
	if got := strings.Count(logs.String(), "semgrep tier blocked a request"); got != 1 {
		t.Fatalf("%d finding lines for 100 findings: %q", got, logs.String())
	}
	if got := strings.Count(logs.String(), "semgrep tier timeout"); got != 1 {
		t.Fatalf("%d timeout lines for 100 timeouts", got)
	}
	// The next interval writes one more line that counts what it held back.
	clock = clock.Add(logInterval + time.Second)
	tier.decide(found)
	tier.decide(timeout)
	if !strings.Contains(logs.String(), "aws-key-0@4-24 (+99 suppressed)") || !strings.Contains(logs.String(), "failure mode deny (+99 suppressed)") {
		t.Fatalf("second interval lines lack the suppressed count: %q", logs.String())
	}
	// Findings and failures are limited separately.
	if got := strings.Count(logs.String(), "\n"); got != 4 {
		t.Fatalf("%d lines, want 4: %q", got, logs.String())
	}
}

func TestConnectionRefusedDeniesOrAllows(t *testing.T) {
	listener, _ := net.Listen("tcp", "127.0.0.1:0")
	addr := listener.Addr().String()
	listener.Close()
	engine, err := newHTTPEngine("http://"+addr+"/", 1<<20)
	if err != nil {
		t.Fatal(err)
	}
	for _, mode := range []string{failureDeny, failureAllow} {
		cfg := testConfig()
		cfg.FailureMode = mode
		s, _ := newTierScanner(t, cfg, engine)
		reply, err := s.Check(context.Background(), cleanRequest("fine"))
		if err != nil {
			t.Fatal(err)
		}
		if allowed := reply.GetStatus().GetCode() == int32(codes.OK); allowed != (mode == failureAllow) {
			t.Errorf("mode %s allowed=%v", mode, allowed)
		}
	}
}

func TestShedsAtTheConcurrencyLimit(t *testing.T) {
	hold := make(chan struct{})
	entered := make(chan struct{}, 4)
	engine := engineFunc(func(ctx context.Context, body []byte) (Verdict, error) {
		entered <- struct{}{}
		select {
		case <-hold:
		case <-ctx.Done():
		}
		return Verdict{}, nil
	})
	cfg := testConfig()
	cfg.Concurrency = 1
	cfg.Deadline = 5 * time.Second
	s, registry := newTierScanner(t, cfg, engine)

	first := make(chan *authv3.CheckResponse, 1)
	go func() {
		reply, _ := s.Check(context.Background(), cleanRequest("first"))
		first <- reply
	}()
	<-entered
	reply, err := s.Check(context.Background(), cleanRequest("second"))
	if err != nil {
		t.Fatal(err)
	}
	if reply.GetDeniedResponse().GetBody() != engineUnavailableReason {
		t.Fatalf("shed request not denied: %v", reply)
	}
	if got := counter(t, registry, "factory_egress_scan_semgrep_requests_total", map[string]string{"outcome": "shed"}); got != 1 {
		t.Errorf("shed = %v", got)
	}
	close(hold)
	if r := <-first; r.GetStatus().GetCode() != int32(codes.OK) {
		t.Fatalf("first request: %v", r)
	}
	// The slot is free again.
	if r, _ := s.Check(context.Background(), cleanRequest("third")); r.GetStatus().GetCode() != int32(codes.OK) {
		t.Fatalf("third request: %v", r)
	}
}

func TestSlowEngineDoesNotHoldGitleaksSlots(t *testing.T) {
	hold := make(chan struct{})
	engine := engineFunc(func(ctx context.Context, body []byte) (Verdict, error) {
		select {
		case <-hold:
		case <-ctx.Done():
		}
		return Verdict{}, nil
	})
	cfg := testConfig()
	cfg.Deadline = 5 * time.Second
	cfg.Concurrency = 8
	s, registry := newTierScanner(t, cfg, engine)
	s.slots = make(chan struct{}, 1) // one Gitleaks slot
	done := make(chan struct{})
	go func() {
		s.Check(context.Background(), cleanRequest("waits for the engine"))
		close(done)
	}()
	// A second request must still reach Gitleaks while the first waits.
	waitFor(t, func() bool {
		return counter(t, registry, "factory_egress_scan_semgrep_inflight", nil) == 1
	})
	reply, err := s.Check(context.Background(), cleanRequest("token "+secretFixture))
	if err != nil || reply.GetStatus().GetCode() != int32(codes.PermissionDenied) {
		t.Fatalf("Gitleaks slot was held by the engine wait: %v %v", reply, err)
	}
	close(hold)
	<-done
}

func TestOversizeBodyAppliesTheFailureMode(t *testing.T) {
	var calls atomic.Int32
	engine := engineFunc(func(ctx context.Context, body []byte) (Verdict, error) {
		calls.Add(1)
		return Verdict{}, nil
	})
	cfg := testConfig()
	cfg.MaxBody = 16
	s, registry := newTierScanner(t, cfg, engine)
	reply, err := s.Check(context.Background(), cleanRequest(strings.Repeat("x", 17)))
	if err != nil || reply.GetDeniedResponse().GetBody() != engineUnavailableReason {
		t.Fatalf("oversize not denied: %v %v", reply, err)
	}
	if calls.Load() != 0 {
		t.Error("an oversize body was sent to the engine")
	}
	if got := counter(t, registry, "factory_egress_scan_semgrep_requests_total", map[string]string{"outcome": "oversize"}); got != 1 {
		t.Errorf("oversize = %v", got)
	}
	if r, _ := s.Check(context.Background(), cleanRequest(strings.Repeat("x", 16))); r.GetStatus().GetCode() != int32(codes.OK) {
		t.Fatalf("a body at the limit was denied: %v", r)
	}
}

func TestBreakerOpensAndProbes(t *testing.T) {
	var calls atomic.Int32
	var fail atomic.Bool
	fail.Store(true)
	engine := engineFunc(func(ctx context.Context, body []byte) (Verdict, error) {
		calls.Add(1)
		if fail.Load() {
			return Verdict{}, errors.New("down")
		}
		return Verdict{}, nil
	})
	cfg := testConfig()
	cfg.BreakerFailures = 2
	cfg.BreakerCooldown = time.Minute
	s, registry := newTierScanner(t, cfg, engine)
	clock := time.Now()
	s.semgrep.breaker.now = func() time.Time { return clock }

	for i := 0; i < 2; i++ {
		s.Check(context.Background(), cleanRequest("fine"))
	}
	if calls.Load() != 2 || counter(t, registry, "factory_egress_scan_semgrep_breaker_open", nil) != 1 {
		t.Fatalf("breaker did not open: calls=%d", calls.Load())
	}
	reply, _ := s.Check(context.Background(), cleanRequest("fine"))
	if calls.Load() != 2 || reply.GetDeniedResponse().GetBody() != engineUnavailableReason {
		t.Fatalf("open breaker called the engine: calls=%d reply=%v", calls.Load(), reply)
	}
	if got := counter(t, registry, "factory_egress_scan_semgrep_requests_total", map[string]string{"outcome": "breaker_open"}); got != 1 {
		t.Errorf("breaker_open = %v", got)
	}

	// After the cooldown one probe goes through. A failed probe re-opens.
	clock = clock.Add(2 * time.Minute)
	s.Check(context.Background(), cleanRequest("fine"))
	if calls.Load() != 3 {
		t.Fatalf("probe was not sent: calls=%d", calls.Load())
	}
	s.Check(context.Background(), cleanRequest("fine"))
	if calls.Load() != 3 {
		t.Fatal("breaker did not re-open after a failed probe")
	}
	// A good probe closes it.
	clock = clock.Add(2 * time.Minute)
	fail.Store(false)
	if r, _ := s.Check(context.Background(), cleanRequest("fine")); r.GetStatus().GetCode() != int32(codes.OK) {
		t.Fatalf("probe request: %v", r)
	}
	if counter(t, registry, "factory_egress_scan_semgrep_breaker_open", nil) != 0 {
		t.Error("breaker stayed open after a good probe")
	}
}

// When the caller ends the check, the tier result is "cancelled" and the select
// in checkTwoTiers can see the result before ctx.Done. The cancelled guard in
// checkTwoTiers is defence in depth: it keeps such a result from reaching the
// allow path. This test cancels while the engine returns at once and requires
// that no run allows.
func TestCancelledResultNeverAllows(t *testing.T) {
	engine := engineFunc(func(ctx context.Context, body []byte) (Verdict, error) {
		<-ctx.Done()
		return Verdict{}, ctx.Err()
	})
	cfg := testConfig()
	cfg.Deadline = 5 * time.Second
	s, _ := newTierScanner(t, cfg, engine)
	for i := 0; i < 200; i++ {
		ctx, cancel := context.WithCancel(context.Background())
		go func() {
			time.Sleep(time.Duration(i%5) * time.Millisecond)
			cancel()
		}()
		reply, err := s.Check(ctx, cleanRequest("fine"))
		cancel()
		if err == nil || reply != nil {
			t.Fatalf("run %d: a cancelled check returned a verdict: %v %v", i, reply, err)
		}
	}
}

// The guard itself, called with a cancelled result and a live context.
func TestCancelledGuardDeniesWithoutACancelledContext(t *testing.T) {
	s, _ := newTierScanner(t, testConfig(), engineFunc(func(ctx context.Context, body []byte) (Verdict, error) {
		return Verdict{}, nil
	}))
	reply, err := s.finishTwoTiers(context.Background(), engineResult{outcome: engineCancelled})
	if err != nil || reply.GetStatus().GetCode() == int32(codes.OK) {
		t.Fatalf("a cancelled result with a live context was allowed: %v %v", reply, err)
	}
}

func TestCallerCancelNeverAllows(t *testing.T) {
	engine := engineFunc(func(ctx context.Context, body []byte) (Verdict, error) {
		<-ctx.Done()
		return Verdict{}, ctx.Err()
	})
	cfg := testConfig()
	cfg.Deadline = 5 * time.Second
	s, registry := newTierScanner(t, cfg, engine)
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Millisecond)
	defer cancel()
	reply, err := s.Check(ctx, cleanRequest("fine"))
	if err == nil || reply != nil {
		t.Fatalf("a cancelled check returned a verdict: %v %v", reply, err)
	}
	waitFor(t, func() bool {
		return counter(t, registry, "factory_egress_scan_semgrep_requests_total", map[string]string{"outcome": "cancelled"}) == 1
	})
}

// ---- Tier off ----

// With the tier off, the metric families, label values and buckets are the
// ones that the scanner exposed before the tier existed.
func TestTierOffExposesTheOldMetrics(t *testing.T) {
	registry := prometheus.NewRegistry()
	metered := newMeteredScanner(newTestScanner(t), registry)
	if _, err := metered.Check(context.Background(), cleanRequest("fine")); err != nil {
		t.Fatal(err)
	}
	families, err := registry.Gather()
	if err != nil {
		t.Fatal(err)
	}
	var names []string
	for _, family := range families {
		names = append(names, family.GetName())
		var outcomes []string
		for _, m := range family.GetMetric() {
			outcomes = append(outcomes, m.GetLabel()[0].GetValue())
			if h := m.GetHistogram(); h != nil {
				var bounds []float64
				for _, b := range h.GetBucket() {
					bounds = append(bounds, b.GetUpperBound())
				}
				if fmt.Sprint(bounds) != "[0.001 0.0025 0.005 0.01 0.025 0.05 0.075 0.08 0.1 0.25]" {
					t.Errorf("buckets changed: %v", bounds)
				}
			}
		}
		if fmt.Sprint(outcomes) != "[allowed cancelled capacity_limited credential_blocked error invalid_request work_limit]" {
			t.Errorf("%s outcomes changed: %v", family.GetName(), outcomes)
		}
	}
	if fmt.Sprint(names) != "[factory_egress_scan_check_duration_seconds factory_egress_scan_checks_total]" {
		t.Fatalf("metric families changed: %v", names)
	}
}

// With the tier off, a request takes the same path and gives the same reply as
// before the tier existed, whatever an engine would say.
func TestTierOffNeverCallsAnEngine(t *testing.T) {
	s := newTestScanner(t)
	if s.semgrep != nil {
		t.Fatal("the tier must be off by default")
	}
	reply, err := s.Check(context.Background(), cleanRequest("fine"))
	if err != nil || reply.GetStatus().GetCode() != int32(codes.OK) {
		t.Fatalf("clean: %v %v", reply, err)
	}
	reply, err = s.Check(context.Background(), cleanRequest(secretFixture))
	if err != nil || reply.GetDeniedResponse().GetBody() != "request contains a credential pattern" {
		t.Fatalf("secret: %v %v", reply, err)
	}
}

func waitFor(t *testing.T, ok func() bool) {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for !ok() {
		if time.Now().After(deadline) {
			t.Fatal("condition not met in time")
		}
		time.Sleep(5 * time.Millisecond)
	}
}
