package main

import (
	"context"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
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
		{name: "unknown success", status: 200, body: `{"result":"ok"}`, pass: false},
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
		case r.Method == http.MethodGet && r.URL.Path == "/v1/conformance":
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
	if got := runS4(context.Background(), cfg, client); got.Verdict != verdictPass {
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
