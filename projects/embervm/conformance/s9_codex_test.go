package main

import (
	"context"
	"encoding/base64"
	"encoding/json"
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
		f.sessionInvokes.Store(0)
		f.deleted.Store(false)
		f.event("create")
		w.WriteHeader(http.StatusCreated)
		_, _ = w.Write([]byte(`{"session_id":"probe-session","session_token":"session-capability","state":"live"}`))
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
			w.WriteHeader(http.StatusNotFound)
			return
		}
		if !f.authorize(w, r, "session-capability") {
			return
		}
		if f.mode == "never_park" {
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
		if f.mode == "delete_failure" {
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
	result := runS9(context.Background(), cfg, client)
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
			result := runS9(context.Background(), cfg, client)
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
			result := runS9(ctx, cfg, client)
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
	result := runS9(context.Background(), cfg, client)
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
	if fake.creates.Load() != 1 || fake.invokes.Load() != 2 {
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
	if fake.creates.Load() != 2 || fake.invokes.Load() != 2 {
		t.Fatalf("attempt limit = %d creates, %d invokes", fake.creates.Load(), fake.invokes.Load())
	}
	cfg.chartVersion = "1.2.4"
	_ = gate.run(context.Background(), cfg, client)
	if fake.creates.Load() != 3 {
		t.Fatal("new chart did not reset the attempt limit")
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
