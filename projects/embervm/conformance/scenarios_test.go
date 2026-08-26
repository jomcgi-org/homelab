package main

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func TestClassifyInvokeResponse(t *testing.T) {
	tests := []struct {
		name   string
		status int
		body   string
		pass   bool
	}{
		{name: "egress denied", status: 503, body: `{"error":"egress connection refused"}`, pass: true},
		{name: "model unreachable", status: 422, body: `{"error":"model provider network unreachable"}`, pass: true},
		{name: "pi no output", status: 422, body: `{"error":"pi turn produced no output: fetch failed"}`, pass: true},
		{name: "relight failed", status: 502, body: `{"error":"session invoke failed","reason":"relight timeout"}`, pass: false},
		{name: "stuck state", status: 409, body: `{"error":"session not ready","state":"relighting"}`, pass: false},
		{name: "successful guest response", status: 200, body: `{"error":"session invoke failed"}`, pass: true},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			got := classifyInvokeResponse(test.status, []byte(test.body))
			if got.pass != test.pass {
				t.Fatalf("pass = %v, want %v; detail=%s", got.pass, test.pass, got.detail)
			}
		})
	}
}

func TestWaitForReadyWaitsForBothDispatchableReadyBases(t *testing.T) {
	tokenFile := t.TempDir() + "/token"
	if err := os.WriteFile(tokenFile, []byte("test-token\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	var nodeRequests atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/healthz":
			w.WriteHeader(http.StatusOK)
		case "/v1/nodes":
			if r.Header.Get("Authorization") != "Bearer test-token" {
				http.Error(w, "unauthorized", http.StatusUnauthorized)
				return
			}
			if nodeRequests.Add(1) == 1 {
				_, _ = w.Write([]byte(`{"nodes":[{"dispatchable":true,"draining":false,"facts":{"workloads":{"sandbox-python":{"base_state":"BASE_BUILD_STATE_READY","snapshot_ref":"task-base"},"pi-runtime":{"base_state":"BASE_BUILD_STATE_BUILDING","snapshot_ref":""}}}}]}`))
				return
			}
			_, _ = w.Write([]byte(`{"nodes":[{"dispatchable":true,"draining":false,"facts":{"workloads":{"sandbox-python":{"base_state":"BASE_BUILD_STATE_READY","snapshot_ref":"task-base"},"pi-runtime":{"base_state":"BASE_BUILD_STATE_READY","snapshot_ref":"session-base"}}}}]}`))
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	client := &controlPlaneClient{baseURL: server.URL, tokenFile: tokenFile, http: server.Client()}
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	if err := waitForReady(ctx, client, "sandbox-python", "pi-runtime"); err != nil {
		t.Fatalf("waitForReady: %v", err)
	}
	if got := nodeRequests.Load(); got < 2 {
		t.Fatalf("node requests = %d, want at least 2", got)
	}
}

func TestWaitForReadyAllowsWorkloadsOnDifferentNodes(t *testing.T) {
	tokenFile := t.TempDir() + "/token"
	if err := os.WriteFile(tokenFile, []byte("test-token\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/healthz":
			w.WriteHeader(http.StatusOK)
		case "/v1/nodes":
			_, _ = w.Write([]byte(`{"nodes":[{"dispatchable":true,"draining":false,"facts":{"workloads":{"sandbox-python":{"base_state":"BASE_BUILD_STATE_READY","snapshot_ref":"task-base"}}}},{"dispatchable":true,"draining":false,"facts":{"workloads":{"pi-runtime":{"base_state":"BASE_BUILD_STATE_READY","snapshot_ref":"session-base"}}}}]}`))
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	client := &controlPlaneClient{baseURL: server.URL, tokenFile: tokenFile, http: server.Client()}
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	if err := waitForReady(ctx, client, "sandbox-python", "pi-runtime"); err != nil {
		t.Fatalf("waitForReady: %v", err)
	}
}

func TestRunLoopFailsS0WhenOnlyTaskWorkloadIsReady(t *testing.T) {
	tokenFile := t.TempDir() + "/token"
	if err := os.WriteFile(tokenFile, []byte("test-token\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/healthz":
			w.WriteHeader(http.StatusOK)
		case "/v1/nodes":
			_, _ = w.Write([]byte(`{"nodes":[{"dispatchable":true,"draining":false,"facts":{"workloads":{"sandbox-python":{"base_state":"BASE_BUILD_STATE_READY","snapshot_ref":"task-base"}}}}]}`))
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	cfg := config{
		chartVersion:    "test",
		readyWait:       20 * time.Millisecond,
		runInterval:     time.Hour,
		taskWorkload:    "sandbox-python",
		sessionWorkload: "pi-runtime",
	}
	client := &controlPlaneClient{baseURL: server.URL, tokenFile: tokenFile, http: server.Client()}
	store := newVerdictStore(cfg.chartVersion)
	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()
	runLoop(ctx, cfg, client, store)

	got := store.snapshot()
	if len(got.Scenarios) != 1 || got.Scenarios[0].ID != "S0" {
		t.Fatalf("scenarios = %#v, want only S0", got.Scenarios)
	}
	if !strings.Contains(got.Scenarios[0].Detail, cfg.sessionWorkload) {
		t.Fatalf("S0 detail = %q, want unready workload %q", got.Scenarios[0].Detail, cfg.sessionWorkload)
	}
}

func TestTaskAndInvariantScenariosAgainstFakeControlPlane(t *testing.T) {
	tokenFile := t.TempDir() + "/token"
	if err := os.WriteFile(tokenFile, []byte("test-token\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer test-token" {
			http.Error(w, "unauthorized", http.StatusUnauthorized)
			return
		}
		switch {
		case r.Method == http.MethodPost && r.URL.Path == "/v1/workloads/sandbox-python/tasks":
			if r.URL.Query().Get("wait") != "true" || !strings.HasPrefix(r.Header.Get("Idempotency-Key"), "1.2.3-") {
				http.Error(w, "bad request", http.StatusBadRequest)
				return
			}
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"exit_code":0,"stdout":"conformance ok\n"}`))
		case r.Method == http.MethodGet && r.URL.Path == "/v1/nodes":
			_, _ = w.Write([]byte(`{"nodes":[{"facts":{"live_vms":0,"workloads":{"sandbox-python":{}}}}]}`))
		case r.Method == http.MethodGet && r.URL.Path == "/v1/conformance":
			if r.URL.Query().Get("since_ts_ms") != "1000" {
				http.Error(w, "missing suite start", http.StatusBadRequest)
				return
			}
			_, _ = w.Write([]byte(`{"enabled":true,"verdicts":[{"invariant":"no_double_assign","verdict":"pass","coverage":1},{"invariant":"eventually_dispatched","verdict":"pass","coverage":2},{"invariant":"inventory_reconciled","verdict":"pass","coverage":3},{"invariant":"destroy_intent_precedes_record","verdict":"pass","coverage":1}]}`))
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	cfg := config{baseURL: server.URL, tokenFile: tokenFile, chartVersion: "1.2.3", taskWorkload: "sandbox-python", minPassingInvariants: 4}
	client := &controlPlaneClient{baseURL: server.URL, tokenFile: tokenFile, http: server.Client()}
	if got := runS1(context.Background(), cfg, client, time.Unix(1, 0)); got.Verdict != verdictPass {
		t.Fatalf("S1 = %#v", got)
	}
	if got := runS4(context.Background(), cfg, client, time.Unix(1, 0)); got.Verdict != verdictPass {
		t.Fatalf("S4 = %#v", got)
	}
}

func TestRunS4SendsSuiteStart(t *testing.T) {
	tokenFile := t.TempDir() + "/token"
	if err := os.WriteFile(tokenFile, []byte("test-token\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	suiteStarted := time.Date(2026, time.August, 23, 12, 34, 56, 789000000, time.UTC)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/conformance" {
			http.NotFound(w, r)
			return
		}
		if got, want := r.URL.Query().Get("since_ts_ms"), fmt.Sprintf("%d", suiteStarted.UnixMilli()); got != want {
			t.Errorf("since_ts_ms = %q, want %q", got, want)
		}
		_, _ = w.Write([]byte(`{"enabled":true,"verdicts":[{"invariant":"covered","verdict":"pass","coverage":1}]}`))
	}))
	defer server.Close()

	cfg := config{baseURL: server.URL, tokenFile: tokenFile, minPassingInvariants: 1}
	client := &controlPlaneClient{baseURL: server.URL, tokenFile: tokenFile, http: server.Client()}
	if got := runS4(context.Background(), cfg, client, suiteStarted); got.Verdict != verdictPass {
		t.Fatalf("S4 = %#v", got)
	}
}

func TestSessionSleepWakeRelightAgainstFakeControlPlane(t *testing.T) {
	tokenFile := t.TempDir() + "/token"
	if err := os.WriteFile(tokenFile, []byte("management-token\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	destroyed := false
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.Method == http.MethodPost && r.URL.Path == "/v1/workloads/pi-runtime/sessions":
			if r.Header.Get("Authorization") != "Bearer management-token" {
				http.Error(w, "unauthorized", http.StatusUnauthorized)
				return
			}
			w.WriteHeader(http.StatusCreated)
			_, _ = w.Write([]byte(`{"session_id":"session-1","session_token":"session-token","state":"running"}`))
		case r.Method == http.MethodGet && r.URL.Path == "/v1/sessions/session-1":
			if destroyed {
				if r.Header.Get("Authorization") != "Bearer management-token" {
					http.Error(w, "unauthorized", http.StatusUnauthorized)
					return
				}
				_, _ = w.Write([]byte(`{"state":"destroyed"}`))
				return
			}
			if r.Header.Get("Authorization") != "Bearer session-token" {
				http.Error(w, "unauthorized", http.StatusUnauthorized)
				return
			}
			_, _ = w.Write([]byte(`{"state":"banked"}`))
		case r.Method == http.MethodPost && r.URL.Path == "/v1/sessions/session-1/invoke":
			if r.Header.Get("Authorization") != "Bearer session-token" || r.Header.Get("X-Ember-Guest-Path") != "/shim/turn" {
				http.Error(w, "unauthorized", http.StatusUnauthorized)
				return
			}
			w.WriteHeader(http.StatusUnprocessableEntity)
			_, _ = w.Write([]byte(`{"error":"pi turn produced no output: egress connection refused"}`))
		case r.Method == http.MethodDelete && r.URL.Path == "/v1/sessions/session-1":
			if r.Header.Get("Authorization") != "Bearer management-token" {
				http.Error(w, "unauthorized", http.StatusUnauthorized)
				return
			}
			destroyed = true
			w.WriteHeader(http.StatusAccepted)
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	cfg := config{baseURL: server.URL, tokenFile: tokenFile, sessionWorkload: "pi-runtime"}
	client := &controlPlaneClient{baseURL: server.URL, tokenFile: tokenFile, http: server.Client()}
	got := runS2(context.Background(), cfg, client)
	if got.verdict.Verdict != verdictPass {
		t.Fatalf("S2 = %#v", got)
	}
	if !destroyed {
		t.Fatal("S2 did not destroy the session")
	}
}
