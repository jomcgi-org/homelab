package main

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"regexp"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func codexTestConfig() config {
	return config{
		chartVersion:       "1.2.3",
		codexProbe:         true,
		codexWorkload:      "claude-runtime",
		codexModel:         "sol",
		codexProviderModel: "gpt-6.1-sol",
		codexGuestDigest:   "sha256:" + strings.Repeat("a1", 32),
		codexCLIVersion:    "0.146.0",
		budgets:            map[string]time.Duration{"S9": time.Second},
	}
}

func codexTestTurn(cfg config, nonce, artifactPath, command string) codexProbeTurn {
	encoded, _ := json.Marshal(map[string]any{
		"session_id":      "codex-thread",
		"model":           cfg.codexModel,
		"provider_model":  cfg.codexProviderModel,
		"effort":          codexProbeEffort,
		"terminal_reason": "completed",
		"usage":           map[string]int{"input_tokens": 120, "output_tokens": 20, "cache_read_tokens": 60},
		"activities":      []map[string]string{{"type": "bash", "command": command}},
		"artifact": map[string]string{
			"path":        artifactPath,
			"outcome":     "ok",
			"content_b64": base64.StdEncoding.EncodeToString([]byte(nonce + "\n" + cfg.codexGuestDigest + "\ncodex-cli " + cfg.codexCLIVersion + "\n")),
		},
	})
	var turn codexProbeTurn
	_ = json.Unmarshal(encoded, &turn)
	return turn
}

func TestCodexProbeRejectsIncompleteOrStaleEvidence(t *testing.T) {
	cfg := codexTestConfig()
	nonce, path := "fresh-nonce", "fresh-nonce.proof"
	command := "printf fresh-nonce && " + codexProofCommand("fresh-nonce.txt", path)
	tests := []struct {
		name   string
		mutate func(*codexProbeTurn)
		want   string
	}{
		{"failed turn", func(v *codexProbeTurn) { v.IsError = true }, "complete successfully"},
		{"provider error", func(v *codexProbeTurn) { v.Error = "private provider response" }, "complete successfully"},
		{"truncated turn", func(v *codexProbeTurn) { v.TerminalReason = "length" }, "complete successfully"},
		{"wrong alias", func(v *codexProbeTurn) { v.Model = "terra" }, "model or effort"},
		{"wrong provider", func(v *codexProbeTurn) { v.ProviderModel = "gpt-5.6-terra" }, "model or effort"},
		{"wrong effort", func(v *codexProbeTurn) { v.Effort = "high" }, "model or effort"},
		{"missing conversation", func(v *codexProbeTurn) { v.SessionID = "" }, "conversation"},
		{"different conversation", func(v *codexProbeTurn) { v.SessionID = "new-thread" }, "conversation"},
		{"missing input usage", func(v *codexProbeTurn) { v.Usage.InputTokens = 0 }, "token usage"},
		{"missing output usage", func(v *codexProbeTurn) { v.Usage.OutputTokens = 0 }, "token usage"},
		{"negative cache usage", func(v *codexProbeTurn) { v.Usage.CacheReadTokens = -1 }, "token usage"},
		{"cache exceeds input", func(v *codexProbeTurn) { v.Usage.CacheReadTokens = 121 }, "token usage"},
		{"text without shell", func(v *codexProbeTurn) { v.Activities = nil }, "shell command"},
		{"write instead of shell", func(v *codexProbeTurn) { v.Activities[0].Type = "write" }, "shell command"},
		{"shell without version read", func(v *codexProbeTurn) { v.Activities[0].Command = "printf fresh-nonce > " + path }, "shell command"},
		{"missing artifact", func(v *codexProbeTurn) { v.Artifact.Outcome = "missing" }, "captured independently"},
		{"wrong artifact", func(v *codexProbeTurn) { v.Artifact.Path = "old.proof" }, "captured independently"},
		{"invalid base64", func(v *codexProbeTurn) { v.Artifact.ContentB64 = "!" }, "challenge"},
		{"old nonce", func(v *codexProbeTurn) {
			v.Artifact.ContentB64 = base64.StdEncoding.EncodeToString([]byte("old\n" + cfg.codexGuestDigest + "\ncodex-cli 0.146.0\n"))
		}, "challenge"},
		{"missing stamp", func(v *codexProbeTurn) {
			v.Artifact.ContentB64 = base64.StdEncoding.EncodeToString([]byte(nonce + "\ncodex-cli 0.146.0\n"))
		}, "challenge"},
		{"stale image", func(v *codexProbeTurn) {
			v.Artifact.ContentB64 = base64.StdEncoding.EncodeToString([]byte(nonce + "\nsha256:" + strings.Repeat("b2", 32) + "\ncodex-cli 0.146.0\n"))
		}, "candidate image"},
		{"invalid image digest", func(v *codexProbeTurn) {
			v.Artifact.ContentB64 = base64.StdEncoding.EncodeToString([]byte(nonce + "\nsha256:bad\ncodex-cli 0.146.0\n"))
		}, "candidate image"},
		{"old CLI", func(v *codexProbeTurn) {
			v.Artifact.ContentB64 = base64.StdEncoding.EncodeToString([]byte(nonce + "\n" + cfg.codexGuestDigest + "\ncodex-cli 0.145.0\n"))
		}, "candidate package"},
		{"extra proof data", func(v *codexProbeTurn) {
			v.Artifact.ContentB64 = base64.StdEncoding.EncodeToString([]byte(nonce + "\n" + cfg.codexGuestDigest + "\ncodex-cli 0.146.0\nextra\n"))
		}, "challenge"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			turn := codexTestTurn(cfg, nonce, path, command)
			tc.mutate(&turn)
			err := validateCodexProbeTurn(turn, cfg, "codex-thread", path, nonce, "printf")
			if err == nil || !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("validation error = %v, want %q", err, tc.want)
			}
			if strings.Contains(err.Error(), "private provider response") {
				t.Fatal("provider response leaked into the verdict")
			}
		})
	}
}

func TestCodexProbeAcceptsConfiguredBaselineAndUpgrade(t *testing.T) {
	for _, model := range []struct{ alias, provider, version string }{
		{"sol", "gpt-6.1-sol", "0.146.0"},
		{"terra", "gpt-6.1-terra", "0.167.0"},
	} {
		t.Run(model.alias, func(t *testing.T) {
			cfg := codexTestConfig()
			cfg.codexModel, cfg.codexProviderModel, cfg.codexCLIVersion = model.alias, model.provider, model.version
			command := codexProofCommand("nonce.txt", "resumed.proof")
			turn := codexTestTurn(cfg, "nonce", "resumed.proof", command)
			if err := validateCodexProbeTurn(turn, cfg, "codex-thread", "resumed.proof", "nonce", "cat"); err != nil {
				t.Fatal(err)
			}
		})
	}
}

type codexTestServer struct {
	t              *testing.T
	cfg            config
	mode           string
	creates        atomic.Int32
	invokes        atomic.Int32
	sessionInvokes atomic.Int32
	deletes        atomic.Int32
	goneReads      atomic.Int32
	deleted        atomic.Bool
	mu             sync.Mutex
	nonce          string
	firstArtifact  string
	nonces         []string
	events         []string
}

func newCodexTestServer(t *testing.T, cfg config, mode string) (*codexTestServer, *controlPlaneClient) {
	t.Helper()
	fake := &codexTestServer{t: t, cfg: cfg, mode: mode}
	server := httptest.NewServer(http.HandlerFunc(fake.serveHTTP))
	t.Cleanup(server.Close)
	tokenFile := t.TempDir() + "/token"
	if err := os.WriteFile(tokenFile, []byte("management-token\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	return fake, &controlPlaneClient{baseURL: server.URL, tokenFile: tokenFile, http: server.Client()}
}

func (f *codexTestServer) event(value string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.events = append(f.events, value)
}

func (f *codexTestServer) authorize(w http.ResponseWriter, r *http.Request, token string) bool {
	if r.Header.Get("Authorization") != "Bearer "+token {
		f.t.Errorf("%s %s used the wrong capability", r.Method, r.URL.Path)
		http.Error(w, "wrong capability", http.StatusUnauthorized)
		return false
	}
	return true
}

func (f *codexTestServer) serveHTTP(w http.ResponseWriter, r *http.Request) {
	switch {
	case r.Method == http.MethodPost && r.URL.Path == "/v1/workloads/"+f.cfg.codexWorkload+"/sessions":
		if !f.authorize(w, r, "management-token") {
			return
		}
		f.creates.Add(1)
		switch f.mode {
		case "no_capacity":
			w.WriteHeader(http.StatusTooManyRequests)
			_, _ = w.Write([]byte(`{"error":"session create denied","reason":"no_capacity","workload":"claude-runtime","retryable":true}`))
			return
		case "create_error":
			http.Error(w, "session creation unavailable", http.StatusInternalServerError)
			return
		}
		f.sessionInvokes.Store(0)
		f.deleted.Store(false)
		f.event("create")
		w.WriteHeader(http.StatusCreated)
		state := "live"
		if strings.HasPrefix(f.mode, "readiness_") {
			state = "starting"
		}
		_ = json.NewEncoder(w).Encode(map[string]string{"session_id": "probe-session", "session_token": "session-capability", "state": state})
	case r.Method == http.MethodPost && r.URL.Path == "/v1/sessions/probe-session/invoke":
		if !f.authorize(w, r, "session-capability") {
			return
		}
		if r.Header.Get("X-Ember-Guest-Path") != "/shim/turn" {
			f.t.Error("invoke did not use the native shim turn path")
		}
		var request map[string]string
		if err := json.NewDecoder(r.Body).Decode(&request); err != nil {
			f.t.Errorf("decode invoke: %v", err)
			http.Error(w, "bad request", http.StatusBadRequest)
			return
		}
		f.invokes.Add(1)
		number := f.sessionInvokes.Add(1)
		message, artifactPath := request["message"], request["artifact_path"]
		if request["model"] != f.cfg.codexModel || request["effort"] != "xhigh" {
			f.t.Errorf("wrong model/effort: %v", request)
		}
		if strings.Contains(message, f.cfg.codexGuestDigest) || strings.Contains(message, f.cfg.codexCLIVersion) {
			f.t.Error("expected image or CLI version was supplied to the model")
		}
		challenges := regexp.MustCompile(`ember-codex-[0-9a-f]{32}`).FindAllString(message, -1)
		if len(challenges) == 0 || !strings.Contains(message, codexGuestDigestPath) || !strings.Contains(message, "/usr/local/bin/codex --version") {
			f.t.Error("missing shell challenge or runtime observations")
			http.Error(w, "missing challenge", http.StatusBadRequest)
			return
		}
		nonce := challenges[0]
		lines := strings.Split(message, "\n")
		if len(lines) != 3 {
			f.t.Error("unexpected probe message layout")
			http.Error(w, "bad command", http.StatusBadRequest)
			return
		}
		command := lines[1]
		f.mu.Lock()
		if number == 1 {
			if _, exists := request["session_id"]; exists {
				f.t.Error("first turn must omit session_id")
			}
			if artifactPath != nonce+".first.proof" || !strings.Contains(command, "printf") {
				f.t.Error("first turn did not write the fresh challenge")
			}
			f.nonce, f.firstArtifact = nonce, artifactPath
			f.nonces = append(f.nonces, nonce)
		} else if number == 2 {
			if request["session_id"] != "codex-thread" || nonce != f.nonce || artifactPath != nonce+".resume.proof" || artifactPath == f.firstArtifact {
				f.t.Error("resume did not retain conversation/nonce and capture a new proof")
			}
			if strings.Contains(command, "printf") {
				f.t.Error("resumed turn rewrote the persisted nonce")
			}
		} else {
			f.t.Errorf("unexpected turn number %d", number)
		}
		firstArtifact := f.firstArtifact
		f.mu.Unlock()
		f.event(map[bool]string{true: "first", false: "resume"}[number == 1])
		if f.mode == "provider_error" {
			http.Error(w, "provider-secret-sentinel", http.StatusBadGateway)
			return
		}
		if f.mode == "cancel_invoke" {
			<-r.Context().Done()
			return
		}
		if f.mode == "delayed_invoke" {
			timer := time.NewTimer(150 * time.Millisecond)
			defer timer.Stop()
			select {
			case <-r.Context().Done():
				return
			case <-timer.C:
			}
		}
		turn := codexTestTurn(f.cfg, nonce, artifactPath, command)
		if f.mode == "text_only" {
			turn.Activities = nil
			turn.Artifact.Outcome = "missing"
		}
		if number == 2 {
			switch f.mode {
			case "stale_resume":
				staleCfg := f.cfg
				staleCfg.codexGuestDigest = "sha256:" + strings.Repeat("b2", 32)
				turn = codexTestTurn(staleCfg, nonce, artifactPath, command)
			case "reused_artifact":
				turn.Artifact.Path = firstArtifact
			case "new_conversation":
				turn.SessionID = "new-thread"
			}
		}
		_ = json.NewEncoder(w).Encode(turn)
	case r.Method == http.MethodGet && r.URL.Path == "/v1/sessions/probe-session":
		if f.deleted.Load() {
			if !f.authorize(w, r, "management-token") {
				return
			}
			f.goneReads.Add(1)
			f.event("gone")
			if f.mode == "readiness_gone_unconfirmed" {
				http.Error(w, "session removal unconfirmed", http.StatusServiceUnavailable)
				return
			}
			w.WriteHeader(http.StatusNotFound)
			return
		}
		if !f.authorize(w, r, "session-capability") {
			return
		}
		if f.mode == "readiness_timeout" {
			_, _ = w.Write([]byte(`{"state":"starting"}`))
		} else if strings.HasPrefix(f.mode, "readiness_") {
			_, _ = w.Write([]byte(`{"state":"failed"}`))
		} else if f.mode == "never_park" {
			f.event("banked")
			_, _ = w.Write([]byte(`{"state":"banked"}`))
		} else {
			f.event("parked")
			_, _ = w.Write([]byte(`{"state":"parked"}`))
		}
	case r.Method == http.MethodDelete && r.URL.Path == "/v1/sessions/probe-session":
		if !f.authorize(w, r, "management-token") {
			return
		}
		if r.Context().Err() != nil {
			f.t.Error("cleanup inherited the expired probe context")
		}
		f.deletes.Add(1)
		f.event("destroy")
		if f.mode == "delete_failure" || f.mode == "readiness_delete_failure" {
			http.Error(w, "delete unavailable", http.StatusServiceUnavailable)
			return
		}
		f.deleted.Store(true)
		w.WriteHeader(http.StatusAccepted)
	default:
		f.t.Errorf("unexpected request %s %s", r.Method, r.URL.Path)
		http.NotFound(w, r)
	}
}

func TestS9RequiresNativeParkResumeAndIndependentProof(t *testing.T) {
	cfg := codexTestConfig()
	fake, client := newCodexTestServer(t, cfg, "")
	result := runS9(context.Background(), cfg, client, nil)
	if result.Verdict != verdictPass || !strings.Contains(result.Detail, cfg.codexGuestDigest) || !strings.Contains(result.Detail, "Codex 0.146.0") {
		t.Fatalf("S9 = %#v", result)
	}
	fake.mu.Lock()
	defer fake.mu.Unlock()
	if got := strings.Join(fake.events, ","); got != "create,first,parked,resume,destroy,gone" {
		t.Fatalf("lifecycle = %q", got)
	}
}

func TestS9InvokesUseTheScenarioDeadlineInsteadOfTheSharedClientTimeout(t *testing.T) {
	for _, tc := range []struct {
		name    string
		budget  time.Duration
		verdict string
	}{
		{"slow valid turns", time.Second, verdictPass},
		{"combined turns exceed scenario budget", 250 * time.Millisecond, verdictFail},
	} {
		t.Run(tc.name, func(t *testing.T) {
			cfg := codexTestConfig()
			cfg.budgets["S9"] = tc.budget
			fake, client := newCodexTestServer(t, cfg, "delayed_invoke")
			// Scale the production 120s client limit and a valid 150s native
			// turn to milliseconds. Both turns must outlive the shared limit;
			// their combined duration must still obey the one S9 budget.
			const sharedTimeout = 120 * time.Millisecond
			client.http.Timeout = sharedTimeout
			result := (&codexProbeGate{}).run(context.Background(), cfg, client)
			if result.Verdict != tc.verdict {
				t.Fatalf("S9 = %#v, want %s", result, tc.verdict)
			}
			if tc.verdict == verdictFail && (!strings.Contains(result.Detail, "resumed Codex shell turn") || !strings.Contains(result.Detail, "context deadline exceeded")) {
				t.Fatalf("combined scenario budget did not cancel the resumed turn: %#v", result)
			}
			if fake.invokes.Load() != 2 || fake.deletes.Load() != 1 || fake.goneReads.Load() != 1 {
				t.Fatalf("S9 lifecycle = %d invokes, %d deletes, %d terminal observations", fake.invokes.Load(), fake.deletes.Load(), fake.goneReads.Load())
			}
			if client.http.Timeout != sharedTimeout {
				t.Fatalf("S9 changed the shared client timeout to %s", client.http.Timeout)
			}
		})
	}
}

func TestS9FailureStillDestroysAndConfirmsItsSession(t *testing.T) {
	for _, mode := range []string{"provider_error", "text_only", "stale_resume", "reused_artifact", "new_conversation"} {
		t.Run(mode, func(t *testing.T) {
			cfg := codexTestConfig()
			fake, client := newCodexTestServer(t, cfg, mode)
			result := runS9(context.Background(), cfg, client, nil)
			if result.Verdict != verdictFail {
				t.Fatalf("S9 = %#v, want fail", result)
			}
			if strings.Contains(result.Detail, "provider-secret-sentinel") {
				t.Fatal("provider response leaked into the verdict")
			}
			if fake.deletes.Load() != 1 || fake.goneReads.Load() != 1 {
				t.Fatalf("cleanup = %d deletes, %d terminal observations", fake.deletes.Load(), fake.goneReads.Load())
			}
		})
	}
}

func TestS9CleanupSurvivesCancelledProbeContext(t *testing.T) {
	for _, mode := range []string{"cancel_invoke", "never_park"} {
		t.Run(mode, func(t *testing.T) {
			cfg := codexTestConfig()
			fake, client := newCodexTestServer(t, cfg, mode)
			ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
			defer cancel()
			result := runS9(ctx, cfg, client, nil)
			if result.Verdict != verdictFail || fake.invokes.Load() != 1 {
				t.Fatalf("S9 = %#v, invokes=%d; must not resume without native park", result, fake.invokes.Load())
			}
			if fake.deletes.Load() != 1 || fake.goneReads.Load() != 1 {
				t.Fatalf("expired-context cleanup = %d deletes, %d observations", fake.deletes.Load(), fake.goneReads.Load())
			}
		})
	}
}

func TestS9CannotPassWithUnconfirmedCleanup(t *testing.T) {
	cfg := codexTestConfig()
	fake, client := newCodexTestServer(t, cfg, "delete_failure")
	result := runS9(context.Background(), cfg, client, nil)
	if result.Verdict != verdictFail || !strings.Contains(result.Detail, "cleanup failed") {
		t.Fatalf("S9 = %#v", result)
	}
	if fake.deletes.Load() != 2 || fake.goneReads.Load() != 0 {
		t.Fatalf("cleanup = %d deletes, %d observations", fake.deletes.Load(), fake.goneReads.Load())
	}
}

func TestS9RetainsSuccessUntilChartOrRunnerChanges(t *testing.T) {
	cfg := codexTestConfig()
	fake, client := newCodexTestServer(t, cfg, "")
	gate := &codexProbeGate{}
	for range 3 {
		if got := gate.run(context.Background(), cfg, client); got.Verdict != verdictPass || got.ID != "S9" {
			t.Fatalf("S9 = %#v", got)
		}
	}
	if gate.attempts != 1 || fake.creates.Load() != 1 || fake.invokes.Load() != 2 {
		t.Fatalf("retained success made fresh calls: creates=%d invokes=%d", fake.creates.Load(), fake.invokes.Load())
	}
	cfg.chartVersion = "1.2.4"
	if got := gate.run(context.Background(), cfg, client); got.Verdict != verdictPass {
		t.Fatalf("new chart = %#v", got)
	}
	if got := (&codexProbeGate{}).run(context.Background(), cfg, client); got.Verdict != verdictPass {
		t.Fatalf("new runner = %#v", got)
	}
	if fake.creates.Load() != 3 || fake.invokes.Load() != 6 {
		t.Fatalf("fresh evidence = %d creates, %d invokes", fake.creates.Load(), fake.invokes.Load())
	}
	fake.mu.Lock()
	defer fake.mu.Unlock()
	if len(fake.nonces) != 3 || fake.nonces[0] == fake.nonces[1] || fake.nonces[1] == fake.nonces[2] || fake.nonces[0] == fake.nonces[2] {
		t.Fatalf("challenges not fresh per attempt: %v", fake.nonces)
	}
}

func TestS9StopsProviderTrafficAfterTwoFailedAttempts(t *testing.T) {
	cfg := codexTestConfig()
	fake, client := newCodexTestServer(t, cfg, "provider_error")
	gate := &codexProbeGate{}
	for i := 0; i < 4; i++ {
		got := gate.run(context.Background(), cfg, client)
		if got.Verdict != verdictFail || got.ID != "S9" {
			t.Fatalf("S9 = %#v", got)
		}
		if i >= 2 && !strings.Contains(got.Detail, "retained") {
			t.Fatalf("failure was not retained: %#v", got)
		}
	}
	if gate.attempts != 2 || fake.creates.Load() != 2 || fake.invokes.Load() != 2 {
		t.Fatalf("attempt limit = %d creates, %d invokes", fake.creates.Load(), fake.invokes.Load())
	}
	cfg.chartVersion = "1.2.4"
	_ = gate.run(context.Background(), cfg, client)
	if fake.creates.Load() != 3 {
		t.Fatal("new chart did not reset the attempt limit")
	}
}

func TestS9RetriesRetainedFailureAfterRetryWindow(t *testing.T) {
	cfg := codexTestConfig()
	fake, client := newCodexTestServer(t, cfg, "provider_error")
	now := time.Date(2026, 11, 12, 15, 0, 0, 0, time.UTC)
	gate := &codexProbeGate{now: func() time.Time { return now }}
	for range 3 {
		_ = gate.run(context.Background(), cfg, client)
	}
	if fake.creates.Load() != 2 {
		t.Fatalf("attempt limit = %d creates", fake.creates.Load())
	}
	now = now.Add(codexProbeFailRetryAfter - time.Second)
	if got := gate.run(context.Background(), cfg, client); !strings.Contains(got.Detail, "retained") || fake.creates.Load() != 2 {
		t.Fatalf("failure not retained inside the window: %#v, creates=%d", got, fake.creates.Load())
	}
	now = now.Add(time.Second)
	if got := gate.run(context.Background(), cfg, client); strings.Contains(got.Detail, "retained") || fake.creates.Load() != 3 {
		t.Fatalf("failure retained after the window: %#v, creates=%d", got, fake.creates.Load())
	}
	if gate.attempts != 1 {
		t.Fatalf("retry window did not reset the attempt budget: attempts=%d", gate.attempts)
	}
}

func TestS9SafePreInvokeFailuresRemainRedWithoutSpendingAttempts(t *testing.T) {
	for _, mode := range []string{"no_capacity", "readiness_failed", "readiness_timeout"} {
		t.Run(mode, func(t *testing.T) {
			cfg := codexTestConfig()
			if mode == "readiness_timeout" {
				cfg.budgets["S9"] = 30 * time.Millisecond
			}
			fake, client := newCodexTestServer(t, cfg, mode)
			gate := &codexProbeGate{}
			for range 4 {
				got := gate.run(context.Background(), cfg, client)
				if got.Verdict != verdictFail || got.ID != "S9" || strings.Contains(got.Detail, "retained") {
					t.Fatalf("pre-invoke failure = %#v", got)
				}
				if gate.attempts != 0 || fake.invokes.Load() != 0 {
					t.Fatalf("pre-invoke failure spent provider budget: attempts=%d invokes=%d", gate.attempts, fake.invokes.Load())
				}
			}
			if fake.creates.Load() != 4 {
				t.Fatalf("pre-invoke failures suppressed later readiness checks: creates=%d", fake.creates.Load())
			}
			if strings.HasPrefix(mode, "readiness_") && (fake.deletes.Load() != 4 || fake.goneReads.Load() != 4) {
				t.Fatalf("readiness cleanup = %d deletes, %d terminal observations", fake.deletes.Load(), fake.goneReads.Load())
			}
		})
	}
}

func TestS9PreInvokeFailuresPreserveEarlierCharges(t *testing.T) {
	cfg := codexTestConfig()
	provider, providerClient := newCodexTestServer(t, cfg, "provider_error")
	capacity, capacityClient := newCodexTestServer(t, cfg, "no_capacity")
	gate := &codexProbeGate{}
	if got := gate.run(context.Background(), cfg, providerClient); got.Verdict != verdictFail || gate.attempts != 1 {
		t.Fatalf("first provider failure = %#v, attempts=%d", got, gate.attempts)
	}
	for range 4 {
		if got := gate.run(context.Background(), cfg, capacityClient); got.Verdict != verdictFail || gate.attempts != 1 {
			t.Fatalf("capacity failure changed the existing charge: %#v, attempts=%d", got, gate.attempts)
		}
	}
	if got := gate.run(context.Background(), cfg, providerClient); got.Verdict != verdictFail || gate.attempts != 2 {
		t.Fatalf("second provider failure = %#v, attempts=%d", got, gate.attempts)
	}
	for range 2 {
		if got := gate.run(context.Background(), cfg, capacityClient); got.Verdict != verdictFail || !strings.Contains(got.Detail, "retained") {
			t.Fatalf("exhausted budget = %#v", got)
		}
	}
	if gate.attempts != 2 || provider.invokes.Load() != 2 || capacity.creates.Load() != 4 {
		t.Fatalf("preserved limit: attempts=%d invokes=%d capacity checks=%d", gate.attempts, provider.invokes.Load(), capacity.creates.Load())
	}
}

type codexRoundTripFunc func(*http.Request) (*http.Response, error)

func (f codexRoundTripFunc) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func TestS9UnknownCreateOutcomesConsumeAttempts(t *testing.T) {
	for _, mode := range []string{"create_error", "lost_create_response", "create_timeout"} {
		t.Run(mode, func(t *testing.T) {
			cfg := codexTestConfig()
			if mode == "create_timeout" {
				cfg.budgets["S9"] = 30 * time.Millisecond
			}
			fake, client := newCodexTestServer(t, cfg, mode)
			if mode != "create_error" {
				transport := client.http.Transport
				client.http.Transport = codexRoundTripFunc(func(r *http.Request) (*http.Response, error) {
					response, err := transport.RoundTrip(r)
					if err != nil {
						return response, err
					}
					_, _ = io.Copy(io.Discard, response.Body)
					_ = response.Body.Close()
					// The server allocated a session, but the caller never receives
					// its identity and therefore cannot confirm cleanup.
					if mode == "create_timeout" {
						<-r.Context().Done()
						return nil, r.Context().Err()
					}
					return nil, errors.New("create response lost after allocation")
				})
			}
			gate := &codexProbeGate{}
			for i := range 4 {
				got := gate.run(context.Background(), cfg, client)
				if got.Verdict != verdictFail || gate.attempts != min(i+1, 2) {
					t.Fatalf("unknown create = %#v, attempts=%d", got, gate.attempts)
				}
				if i >= 2 && !strings.Contains(got.Detail, "retained") {
					t.Fatalf("unknown create was not fenced: %#v", got)
				}
			}
			if fake.creates.Load() != 2 || fake.invokes.Load() != 0 || fake.deletes.Load() != 0 {
				t.Fatalf("unknown create bound: creates=%d invokes=%d deletes=%d", fake.creates.Load(), fake.invokes.Load(), fake.deletes.Load())
			}
		})
	}
}

func TestS9OnlyStructuredNoAllocationDenialsAvoidCharges(t *testing.T) {
	const denied = `{"error":"session create denied","reason":"no_capacity","workload":"claude-runtime","retryable":true}`
	for _, tc := range []struct {
		name   string
		status int
		body   string
		safe   bool
	}{
		{"authoritative denial", http.StatusTooManyRequests, denied, true},
		{"wrong status", http.StatusInternalServerError, denied, false},
		{"arbitrary prose", http.StatusTooManyRequests, "no_capacity retryable=true", false},
		{"wrong workload", http.StatusTooManyRequests, strings.Replace(denied, "claude-runtime", "other-workload", 1), false},
		{"unknown reason", http.StatusTooManyRequests, strings.Replace(denied, "no_capacity", "prime_failed", 1), false},
		{"not retryable", http.StatusTooManyRequests, strings.Replace(denied, "true", "false", 1), false},
		{"conflicting identity", http.StatusTooManyRequests, strings.TrimSuffix(denied, "}") + `,"session_id":"possibly-created"}`, false},
		{"trailing data", http.StatusTooManyRequests, denied + ` {}`, false},
		{"malformed body", http.StatusTooManyRequests, `{`, false},
		{"missing created identity", http.StatusCreated, `{"state":"live"}`, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			cfg := codexTestConfig()
			_, client := newCodexTestServer(t, cfg, "")
			requests := 0
			client.http.Transport = codexRoundTripFunc(func(r *http.Request) (*http.Response, error) {
				if r.Method != http.MethodPost || !strings.HasSuffix(r.URL.Path, "/sessions") {
					t.Fatalf("unexpected request %s %s", r.Method, r.URL.Path)
				}
				requests++
				return &http.Response{StatusCode: tc.status, Header: make(http.Header), Body: io.NopCloser(strings.NewReader(tc.body)), Request: r}, nil
			})
			gate := &codexProbeGate{}
			for range 4 {
				if got := gate.run(context.Background(), cfg, client); got.Verdict != verdictFail {
					t.Fatalf("create denial = %#v", got)
				}
			}
			wantAttempts, wantRequests := 2, 2
			if tc.safe {
				wantAttempts, wantRequests = 0, 4
			}
			if gate.attempts != wantAttempts || requests != wantRequests {
				t.Fatalf("denial classification: attempts=%d requests=%d, want %d/%d", gate.attempts, requests, wantAttempts, wantRequests)
			}
		})
	}
}

func TestS9UnconfirmedCleanupConsumesOneAttemptPerRun(t *testing.T) {
	for _, tc := range []struct {
		mode                   string
		invokes, deletes, gone int32
	}{
		{"readiness_delete_failure", 0, 1, 0},
		{"readiness_gone_unconfirmed", 0, 1, 1},
		{"delete_failure", 2, 2, 0},
	} {
		t.Run(tc.mode, func(t *testing.T) {
			cfg := codexTestConfig()
			fake, client := newCodexTestServer(t, cfg, tc.mode)
			gate := &codexProbeGate{}
			for i := range 4 {
				got := gate.run(context.Background(), cfg, client)
				if got.Verdict != verdictFail || !strings.Contains(got.Detail, "cleanup failed") || gate.attempts != min(i+1, 2) {
					t.Fatalf("cleanup failure = %#v, attempts=%d", got, gate.attempts)
				}
			}
			if fake.creates.Load() != 2 || fake.invokes.Load() != 2*tc.invokes || fake.deletes.Load() != 2*tc.deletes || fake.goneReads.Load() != 2*tc.gone {
				t.Fatalf("cleanup failure bound: creates=%d invokes=%d deletes=%d gone=%d", fake.creates.Load(), fake.invokes.Load(), fake.deletes.Load(), fake.goneReads.Load())
			}
		})
	}
}

func TestS9ChargesBeforeInvokeAndRetainsUnknownOutcomes(t *testing.T) {
	cfg := codexTestConfig()
	fake, client := newCodexTestServer(t, cfg, "")
	gate := &codexProbeGate{}
	transport := client.http.Transport
	requests := 0
	client.http.Transport = codexRoundTripFunc(func(r *http.Request) (*http.Response, error) {
		if r.Method == http.MethodPost && strings.HasSuffix(r.URL.Path, "/invoke") {
			requests++
			// RoundTrip runs on the gate's calling goroutine, before an unknown
			// transport outcome can be mistaken for an unused attempt.
			if gate.attempts != requests {
				t.Fatalf("invoke left before accounting: attempts=%d requests=%d", gate.attempts, requests)
			}
			return nil, errors.New("unknown invoke outcome")
		}
		return transport.RoundTrip(r)
	})
	for range 4 {
		if got := gate.run(context.Background(), cfg, client); got.Verdict != verdictFail {
			t.Fatalf("unknown outcome = %#v", got)
		}
	}
	if gate.attempts != 2 || requests != 2 || fake.creates.Load() != 2 || fake.deletes.Load() != 2 || fake.goneReads.Load() != 2 {
		t.Fatalf("unknown outcomes: attempts=%d requests=%d creates=%d deletes=%d gone=%d", gate.attempts, requests, fake.creates.Load(), fake.deletes.Load(), fake.goneReads.Load())
	}
}

func TestS9InvokeTimeoutConsumesAnAttempt(t *testing.T) {
	cfg := codexTestConfig()
	cfg.budgets["S9"] = 30 * time.Millisecond
	fake, client := newCodexTestServer(t, cfg, "cancel_invoke")
	gate := &codexProbeGate{}
	for range 4 {
		if got := gate.run(context.Background(), cfg, client); got.Verdict != verdictFail {
			t.Fatalf("timed out invoke = %#v", got)
		}
	}
	if gate.attempts != 2 || fake.invokes.Load() != 2 || fake.deletes.Load() != 2 || fake.goneReads.Load() != 2 {
		t.Fatalf("timed out invokes: attempts=%d invokes=%d deletes=%d gone=%d", gate.attempts, fake.invokes.Load(), fake.deletes.Load(), fake.goneReads.Load())
	}
}

func TestS9BudgetCannotReturnBeforePendingAccountingFinishes(t *testing.T) {
	cfg := codexTestConfig()
	cfg.budgets["S9"] = 30 * time.Millisecond
	fake, client := newCodexTestServer(t, cfg, "")
	gate := &codexProbeGate{}
	transport := client.http.Transport
	expired, release := make(chan struct{}), make(chan struct{})
	var releaseOnce sync.Once
	unblock := func() { releaseOnce.Do(func() { close(release) }) }
	defer unblock()
	client.http.Transport = codexRoundTripFunc(func(r *http.Request) (*http.Response, error) {
		response, err := transport.RoundTrip(r)
		if err == nil && r.Method == http.MethodPost && strings.HasSuffix(r.URL.Path, "/sessions") {
			body, readErr := io.ReadAll(response.Body)
			_ = response.Body.Close()
			if readErr != nil {
				return nil, readErr
			}
			// Model a transport that finishes creation but delays delivering its
			// response beyond cancellation. No accounting may escape the gate.
			<-r.Context().Done()
			close(expired)
			<-release
			response.Body = io.NopCloser(strings.NewReader(string(body)))
		}
		return response, err
	})
	finished := make(chan scenarioVerdict, 1)
	go func() { finished <- gate.run(context.Background(), cfg, client) }()
	select {
	case <-expired:
	case <-time.After(time.Second):
		t.Fatal("creation did not reach its scenario deadline")
	}
	select {
	case got := <-finished:
		t.Fatalf("gate returned while its scenario could still charge an attempt: %#v", got)
	case <-time.After(20 * time.Millisecond):
	}
	unblock()
	select {
	case got := <-finished:
		if got.Verdict != verdictFail || !strings.Contains(got.Detail, "context deadline exceeded") {
			t.Fatalf("expired creation = %#v", got)
		}
	case <-time.After(time.Second):
		t.Fatal("gate did not finish after its scenario and cleanup")
	}
	if gate.attempts != 0 || fake.invokes.Load() != 0 || fake.deletes.Load() != 1 || fake.goneReads.Load() != 1 {
		t.Fatalf("expired pre-invoke work: attempts=%d invokes=%d deletes=%d gone=%d", gate.attempts, fake.invokes.Load(), fake.deletes.Load(), fake.goneReads.Load())
	}
}

func TestLoadConfigReadsCodexProbeAndRequiresCandidateIdentity(t *testing.T) {
	t.Setenv("EMBERVM_URL", "http://embervm.test")
	t.Setenv("CHART_VERSION", "1.2.3")
	t.Setenv("S9_CODEX_PROBE", "")
	cfg, err := loadConfig()
	if err != nil || cfg.codexProbe || cfg.budgets["S9"] != 5*time.Minute {
		t.Fatalf("disabled default = %#v, %v", cfg, err)
	}
	t.Setenv("S9_CODEX_PROBE", "typo")
	if _, err := loadConfig(); err == nil {
		t.Fatal("invalid S9 gate was silently disabled")
	}
	t.Setenv("S9_CODEX_PROBE", "true")
	t.Setenv("S9_CODEX_CLI_VERSION", "0.146.0")
	for _, digest := range []string{"", "sha256:bad", "sha256:" + strings.Repeat("AA", 32), "sha512:" + strings.Repeat("a1", 32)} {
		t.Setenv("S9_CODEX_GUEST_DIGEST", digest)
		if _, err := loadConfig(); err == nil {
			t.Fatalf("accepted invalid candidate digest %q", digest)
		}
	}
	t.Setenv("S9_CODEX_GUEST_DIGEST", codexTestConfig().codexGuestDigest)
	t.Setenv("S9_CODEX_CLI_VERSION", " ")
	if _, err := loadConfig(); err == nil {
		t.Fatal("accepted an empty CLI expectation")
	}
	t.Setenv("S9_CODEX_CLI_VERSION", "0.146.0")
	t.Setenv("S9_CODEX_MODEL", "sol")
	t.Setenv("S9_CODEX_PROVIDER_MODEL", "gpt-6.1-sol")
	if cfg, err = loadConfig(); err != nil || !cfg.codexProbe || cfg.codexModel != "sol" || cfg.codexProviderModel != "gpt-6.1-sol" || cfg.codexCLIVersion != "0.146.0" {
		t.Fatalf("configured baseline = %#v, %v", cfg, err)
	}
}

func TestS9ReadinessIncludesTheCodexWorkload(t *testing.T) {
	var creates atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/healthz":
			w.WriteHeader(http.StatusOK)
		case "/v1/nodes":
			_, _ = w.Write([]byte(`{"nodes":[{"dispatchable":true,"facts":{"workloads":{"sandbox-python":{"base_state":"BASE_BUILD_STATE_READY","snapshot_ref":"task"},"pi-runtime":{"base_state":"BASE_BUILD_STATE_READY","snapshot_ref":"session"},"sandbox-elixir":{"base_state":"BASE_BUILD_STATE_READY","snapshot_ref":"elixir"}}}}]}`))
		default:
			creates.Add(1)
			http.NotFound(w, r)
		}
	}))
	defer server.Close()
	tokenFile := t.TempDir() + "/token"
	if err := os.WriteFile(tokenFile, []byte("management-token"), 0o600); err != nil {
		t.Fatal(err)
	}
	cfg := codexTestConfig()
	cfg.taskWorkload, cfg.sessionWorkload, cfg.elixirWorkload = "sandbox-python", "pi-runtime", "sandbox-elixir"
	cfg.readyWait, cfg.runInterval = 20*time.Millisecond, time.Hour
	client := &controlPlaneClient{baseURL: server.URL, tokenFile: tokenFile, http: server.Client()}
	store := newVerdictStore(cfg.chartVersion)
	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()
	runLoop(ctx, cfg, client, store)
	got := store.snapshot()
	if len(got.Scenarios) != 1 || got.Scenarios[0].ID != "S0" || !strings.Contains(got.Scenarios[0].Detail, "claude-runtime") || creates.Load() != 0 {
		t.Fatalf("readiness result = %#v, creates=%d", got, creates.Load())
	}
}

func TestS9ResumeReportOnlyPassesOnResumeFailure(t *testing.T) {
	cfg := codexTestConfig()
	cfg.codexResumeReportOnly = true
	fake, client := newCodexTestServer(t, cfg, "stale_resume")
	got := (&codexProbeGate{}).run(context.Background(), cfg, client)
	if got.Verdict != verdictPass || !strings.Contains(got.Detail, "resume report-only (#7731) failed") {
		t.Fatalf("report-only resume = %#v", got)
	}
	if fake.deletes.Load() == 0 {
		t.Fatal("report-only resume left the session alive")
	}
}

func TestS9ResumeReportOnlyStillBlocksFirstTurn(t *testing.T) {
	cfg := codexTestConfig()
	cfg.codexResumeReportOnly = true
	_, client := newCodexTestServer(t, cfg, "provider_error")
	got := (&codexProbeGate{}).run(context.Background(), cfg, client)
	if got.Verdict != verdictFail || !strings.Contains(got.Detail, "first Codex shell turn") {
		t.Fatalf("first-turn failure = %#v", got)
	}
}

func TestControlPlaneInvokeReasonOnlyEchoesRouterEnvelope(t *testing.T) {
	envelope := []byte(`{"error":"session invoke failed","reason":"{:suspect, :transport}","session_id":"s-1"}`)
	if got := controlPlaneInvokeReason(apiResponse{status: http.StatusBadGateway, body: envelope}); got != ` reason="{:suspect, :transport}"` {
		t.Fatalf("router reason = %q", got)
	}
	for name, response := range map[string]apiResponse{
		"guest mimics envelope": {status: http.StatusBadGateway, body: envelope, invokeOutcome: "guest_response"},
		"guest 500 envelope":    {status: http.StatusInternalServerError, body: envelope},
		"other error":           {status: http.StatusBadGateway, body: []byte(`{"error":"provider-secret-sentinel","reason":"leak"}`)},
		"plain body":            {status: http.StatusBadGateway, body: []byte(`provider-secret-sentinel`)},
		"no reason":             {status: http.StatusBadGateway, body: []byte(`{"error":"session invoke failed"}`)},
	} {
		if got := controlPlaneInvokeReason(response); got != "" {
			t.Fatalf("%s echoed: %q", name, got)
		}
	}
	long := []byte(`{"error":"session invoke failed","reason":"` + strings.Repeat("x", 500) + `"}`)
	if got := controlPlaneInvokeReason(apiResponse{status: http.StatusBadGateway, body: long}); len(got) > 220 {
		t.Fatalf("reason not truncated: %d", len(got))
	}
}
