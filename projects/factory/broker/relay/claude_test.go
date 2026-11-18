package relay

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/factory/broker/actorjwt"
	"github.com/jomcgi/homelab/projects/factory/broker/internal/fakes"
	"github.com/jomcgi/homelab/projects/factory/broker/metrics"
)

const (
	claudeSubject = "actor/factory-intsec/audit"
	claudeModel   = "claude-test-model"
	claudeBody    = `{"model":"claude-test-model","max_tokens":16,"messages":[{"role":"user","content":"private prompt"}],"stream":true}`
	claudeToken   = "test-subscription-token"
)

type claudeHarness struct {
	*Claude
	idp    *fakes.IdP
	signer fakes.Signer
	calls  atomic.Int32
	logs   bytes.Buffer
	logMu  sync.Mutex
}

func newClaudeHarness(t *testing.T, respond http.HandlerFunc) *claudeHarness {
	t.Helper()
	h := &claudeHarness{idp: fakes.NewIdP(t), signer: fakes.NewECSigner(t, "claude-test")}
	h.idp.Publish(h.signer)
	logger := slog.New(slog.NewJSONHandler(lockedWriter{&h.logMu, &h.logs}, nil))
	verifier, err := actorjwt.New(actorjwt.Options{Issuer: h.idp.URL, Audience: audience, HTTPClient: h.idp.Client(), Logger: logger})
	if err != nil {
		t.Fatal(err)
	}
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		h.calls.Add(1)
		if respond != nil {
			respond(w, r)
		}
	}))
	t.Cleanup(upstream.Close)
	h.Claude = &Claude{
		Verifier: verifier, Upstream: upstream.URL, Client: upstream.Client(), Logger: logger, Metrics: metrics.New(),
		TokenFile: filepath.Join(t.TempDir(), "token"), ModelAllowlist: map[string][]string{claudeSubject: {claudeModel}},
	}
	h.setToken(t, claudeToken+"\n")
	return h
}

func (h *claudeHarness) setToken(t *testing.T, token string) {
	t.Helper()
	if err := os.WriteFile(h.TokenFile, []byte(token), 0o600); err != nil {
		t.Fatal(err)
	}
}

func (h *claudeHarness) request(t *testing.T, path, body string) *http.Request {
	t.Helper()
	r := httptest.NewRequest(http.MethodPost, path, strings.NewReader(body))
	r.Header.Set("X-Api-Key", h.signer.Sign(t, fakes.ActorClaims(h.idp.URL, audience, "factory-intsec", "audit", time.Now())))
	r.Header.Set("User-Agent", "claude-cli/2.1.285 (external, cli)")
	r.Header.Set("X-App", "cli")
	r.Header.Set("Content-Type", "application/json")
	return r
}

func claudeError(t *testing.T, rec *httptest.ResponseRecorder, status int, code string) {
	t.Helper()
	var body map[string]string
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil || rec.Code != status || body["factory_error"] != code {
		t.Fatalf("response = %d %s, want %d %s", rec.Code, rec.Body, status, code)
	}
}

func TestClaudeAuthenticatesAndDefaultsToDeny(t *testing.T) {
	h := newClaudeHarness(t, nil)
	for _, path := range []string{"/anthropic/v1/messages", "/anthropic/v1/messages/count_tokens"} {
		for _, tc := range []struct {
			name   string
			change func(*http.Request)
			status int
			code   string
		}{
			{"missing", func(r *http.Request) { r.Header.Del("X-Api-Key") }, 401, ErrActorUnauthenticated},
			{"provider key", func(r *http.Request) { r.Header.Set("X-Api-Key", "not-an-actor-jwt") }, 401, ErrActorUnauthenticated},
			{"ambiguous", func(r *http.Request) { r.Header.Set("Authorization", "Bearer anything") }, 401, ErrActorUnauthenticated},
			{"duplicate", func(r *http.Request) { r.Header.Add("X-Api-Key", r.Header.Get("X-Api-Key")) }, 401, ErrActorUnauthenticated},
			{"audience", func(r *http.Request) {
				r.Header.Set("X-Api-Key", h.signer.Sign(t, fakes.ActorClaims(h.idp.URL, "other", "factory-intsec", "audit", time.Now())))
			}, 401, ErrActorUnauthenticated},
			{"expired", func(r *http.Request) {
				r.Header.Set("X-Api-Key", h.signer.Sign(t, fakes.ActorClaims(h.idp.URL, audience, "factory-intsec", "audit", time.Now().Add(-time.Hour))))
			}, 401, ErrActorUnauthenticated},
			{"signature", func(r *http.Request) {
				signer := fakes.NewECSigner(t, h.signer.Kid)
				r.Header.Set("X-Api-Key", signer.Sign(t, fakes.ActorClaims(h.idp.URL, audience, "factory-intsec", "audit", time.Now())))
			}, 401, ErrActorUnauthenticated},
			{"other actor", func(r *http.Request) {
				r.Header.Set("X-Api-Key", h.signer.Sign(t, fakes.ActorClaims(h.idp.URL, audience, "factory-intsec", "other", time.Now())))
			}, 403, ErrClaudeModelDenied},
			{"other namespace", func(r *http.Request) {
				r.Header.Set("X-Api-Key", h.signer.Sign(t, fakes.ActorClaims(h.idp.URL, audience, "other", "audit", time.Now())))
			}, 403, ErrClaudeModelDenied},
			{"SDK", func(r *http.Request) { r.Header.Set("User-Agent", "anthropic-sdk") }, 403, ErrClaudeClientRequired},
			{"no app", func(r *http.Request) { r.Header.Del("X-App") }, 403, ErrClaudeClientRequired},
		} {
			t.Run(path+"/"+tc.name, func(t *testing.T) {
				r := h.request(t, path, claudeBody)
				tc.change(r)
				rec := httptest.NewRecorder()
				h.ServeHTTP(rec, r)
				claudeError(t, rec, tc.status, tc.code)
			})
		}
		h.ModelAllowlist = nil
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, h.request(t, path, claudeBody))
		claudeError(t, rec, 403, ErrClaudeModelDenied)
		h.ModelAllowlist = map[string][]string{claudeSubject: {claudeModel}}
	}
	if h.calls.Load() != 0 {
		t.Fatal("denied request reached upstream")
	}
}

func TestClaudeValidatesModelAndRoutesBeforeUpstream(t *testing.T) {
	h := newClaudeHarness(t, nil)
	h.MaxRequestBytes = 512
	for _, path := range []string{"/anthropic/v1/messages", "/anthropic/v1/messages/count_tokens"} {
		for _, tc := range []struct {
			body   string
			status int
			code   string
		}{
			{`{"model":"other"}`, 403, ErrClaudeModelDenied},
			{`{"model":"claude-test-model","model":"other"}`, 403, ErrClaudeModelDenied},
			{`{"Model":"other","model":"claude-test-model"}`, 400, "bad_request"},
			{`{"model":null}`, 400, "bad_request"},
			{`{"model":[]}`, 400, "bad_request"},
			{`{}`, 400, "bad_request"},
			{`null`, 400, "bad_request"},
			{`[]`, 400, "bad_request"},
			{`{"model":"claude-test-model"} {}`, 400, "bad_request"},
			{strings.Repeat(" ", 513), 413, ErrRequestTooLarge},
		} {
			rec := httptest.NewRecorder()
			h.ServeHTTP(rec, h.request(t, path, tc.body))
			claudeError(t, rec, tc.status, tc.code)
		}
	}
	for _, path := range []string{"/anthropic/v1/models", "/anthropic/v1/messages/", "/anthropic/v1/messages/batches"} {
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, h.request(t, path, claudeBody))
		claudeError(t, rec, 404, ErrNotFound)
	}
	r := h.request(t, "/anthropic/v1/messages", claudeBody)
	r.Method = http.MethodGet
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, r)
	claudeError(t, rec, 404, ErrNotFound)
	r = h.request(t, "/anthropic/v1/messages", claudeBody)
	r.Header.Set("Content-Encoding", "gzip")
	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, r)
	claudeError(t, rec, 415, "bad_request")
	if h.calls.Load() != 0 {
		t.Fatal("invalid request reached upstream")
	}
}

func TestClaudeForwardsOnlyProviderCredentialAndRotatesFile(t *testing.T) {
	wantToken := claudeToken
	h := newClaudeHarness(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/messages/count_tokens" || r.URL.RawQuery != "beta=true" {
			t.Errorf("target = %s", r.URL)
		}
		if r.Header.Get("Authorization") != "Bearer "+wantToken {
			t.Error("upstream did not receive the current subscription token")
		}
		for _, name := range []string{"X-Api-Key", "Cookie", "X-Forwarded-For", "Proxy-Authorization", "Chatgpt-Account-Id"} {
			if r.Header.Get(name) != "" {
				t.Errorf("forwarded forbidden header %s", name)
			}
		}
		if r.Header.Get("Anthropic-Version") != "2023-06-01" || r.Header.Get("Anthropic-Beta") != "claude-code-20250219,"+ClaudeOAuthBeta || r.Header.Get("X-App") != "cli" {
			t.Error("lost Claude Code protocol headers")
		}
		var payload map[string]any
		if json.NewDecoder(r.Body).Decode(&payload) != nil || payload["model"] != claudeModel || payload["messages"] == nil {
			t.Error("lost request payload")
		}
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("Set-Cookie", "private=upstream")
		w.Header().Set("Authorization", "must-not-return")
		_, _ = io.WriteString(w, `{"input_tokens":8}`)
	})
	for _, bearer := range []bool{false, true} {
		r := h.request(t, "/anthropic/v1/messages/count_tokens?beta=true", claudeBody)
		r.Header.Set("Anthropic-Beta", "claude-code-20250219")
		r.Header.Set("Cookie", "sandbox-cookie")
		r.Header.Set("X-Forwarded-For", "spoofed")
		jwt := r.Header.Get("X-Api-Key")
		if bearer {
			wantToken = "rotated-subscription-token"
			h.setToken(t, wantToken)
			r.Header.Del("X-Api-Key")
			r.Header.Set("Authorization", "Bearer "+jwt)
		}
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, r)
		if rec.Code != 200 || rec.Body.String() != `{"input_tokens":8}` || rec.Header().Get("Set-Cookie") != "" || rec.Header().Get("Authorization") != "" {
			t.Fatalf("count_tokens response = %d %s", rec.Code, rec.Body)
		}
		for _, secret := range []string{jwt, claudeToken, wantToken, "private prompt", "sandbox-cookie"} {
			if strings.Contains(h.logs.String(), secret) {
				t.Fatal("credential or request content logged")
			}
		}
	}
}

func TestClaudeStreamsBeforeCompletion(t *testing.T) {
	gate := make(chan struct{})
	var release sync.Once
	defer release.Do(func() { close(gate) })
	first := "event: message_start\ndata: {\"type\":\"message_start\"}\n\n"
	last := "event: message_stop\ndata: {\"type\":\"message_stop\"}\n\n"
	h := newClaudeHarness(t, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		_, _ = io.WriteString(w, first)
		w.(http.Flusher).Flush()
		select {
		case <-gate:
			_, _ = io.WriteString(w, last)
		case <-r.Context().Done():
		}
	})
	srv := httptest.NewServer(h)
	defer srv.Close()
	r := h.request(t, srv.URL+"/anthropic/v1/messages", claudeBody)
	r.RequestURI = ""
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	resp, err := srv.Client().Do(r.WithContext(ctx))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	reader := bufio.NewReader(resp.Body)
	got := make([]byte, len(first))
	if _, err := io.ReadFull(reader, got); err != nil || string(got) != first {
		t.Fatalf("first event was not streamed: %q, %v", got, err)
	}
	release.Do(func() { close(gate) })
	rest, err := io.ReadAll(reader)
	if err != nil || string(rest) != last || resp.Header.Get("Content-Type") != "text/event-stream" {
		t.Fatalf("stream completion = %q, %v", rest, err)
	}
}

func TestClaudeUpstreamErrors(t *testing.T) {
	for _, status := range []int{401, 403, 429, 500} {
		t.Run(http.StatusText(status), func(t *testing.T) {
			const body = `{"type":"error","error":{"type":"rate_limit_error","message":"retry"}}`
			h := newClaudeHarness(t, func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Content-Type", "application/json")
				w.Header().Set("Retry-After", "17")
				w.WriteHeader(status)
				_, _ = io.WriteString(w, body)
			})
			rec := httptest.NewRecorder()
			h.ServeHTTP(rec, h.request(t, "/anthropic/v1/messages", claudeBody))
			if status == 429 {
				if rec.Code != status || rec.Body.String() != body || rec.Header().Get("Retry-After") != "17" {
					t.Fatalf("429 not passed through: %d %s", rec.Code, rec.Body)
				}
			} else {
				code := ErrClaudeUpstream
				if status == 401 {
					code = ErrClaudeLoginRequired
				}
				claudeError(t, rec, status, code)
			}
			if h.calls.Load() != 1 {
				t.Fatal("upstream error was retried")
			}
		})
	}
}

func TestClaudeOAuthBetaIsAlwaysPresentOnce(t *testing.T) {
	h := newClaudeHarness(t, func(w http.ResponseWriter, r *http.Request) {
		betas := strings.Split(r.Header.Get("Anthropic-Beta"), ",")
		count := 0
		for _, beta := range betas {
			if strings.TrimSpace(beta) == ClaudeOAuthBeta {
				count++
			}
		}
		if count != 1 {
			t.Errorf("OAuth beta count = %d, want 1", count)
		}
	})
	for _, beta := range []string{"", ClaudeOAuthBeta, "claude-code-20250219"} {
		r := h.request(t, "/anthropic/v1/messages", claudeBody)
		if beta != "" {
			r.Header.Set("Anthropic-Beta", beta)
		}
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, r)
		if rec.Code != 200 {
			t.Fatalf("request with beta %q returned %d", beta, rec.Code)
		}
	}
	// Invalid upstream configuration exercises the classified error without
	// making a network request or logging the transport's raw error text.
	h.Upstream = "https://[invalid"
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, h.request(t, "/anthropic/v1/messages", claudeBody))
	claudeError(t, rec, 502, ErrClaudeUpstreamUnreachable)
	if h.calls.Load() != 3 {
		t.Fatal("failed request reached upstream")
	}
}

func TestClaudeTokenUnavailableAndNoRedirect(t *testing.T) {
	h := newClaudeHarness(t, func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, "/credential-leak", http.StatusTemporaryRedirect)
	})
	for _, token := range []string{"", "invalid\ncredential"} {
		h.setToken(t, token)
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, h.request(t, "/anthropic/v1/messages", claudeBody))
		claudeError(t, rec, 503, ErrClaudeTokenUnavailable)
	}
	if err := os.Remove(h.TokenFile); err != nil {
		t.Fatal(err)
	}
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, h.request(t, "/anthropic/v1/messages", claudeBody))
	claudeError(t, rec, 503, ErrClaudeTokenUnavailable)
	if h.calls.Load() != 0 {
		t.Fatal("missing token reached upstream")
	}
	h.setToken(t, claudeToken)
	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, h.request(t, "/anthropic/v1/messages", claudeBody))
	claudeError(t, rec, 502, ErrClaudeUpstream)
	if h.calls.Load() != 1 || rec.Header().Get("Location") != "" {
		t.Fatal("redirect followed or exposed")
	}
}

func TestClaudeIdleTimeoutCancellationAndConcurrency(t *testing.T) {
	started := make(chan struct{}, 1)
	canceled := make(chan struct{}, 1)
	h := newClaudeHarness(t, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		w.(http.Flusher).Flush()
		started <- struct{}{}
		<-r.Context().Done()
		canceled <- struct{}{}
	})
	h.MaxConcurrent = 1
	h.StreamIdleTimeout = time.Second
	for _, cancelActor := range []bool{false, true} {
		ctx, cancel := context.WithCancel(context.Background())
		done := make(chan struct{})
		r := h.request(t, "/anthropic/v1/messages", claudeBody).WithContext(ctx)
		go func() {
			h.ServeHTTP(httptest.NewRecorder(), r)
			close(done)
		}()
		select {
		case <-started:
		case <-time.After(5 * time.Second):
			cancel()
			t.Fatal("upstream never started")
		}
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, h.request(t, "/anthropic/v1/messages/count_tokens", claudeBody))
		claudeError(t, rec, 503, ErrRelayBusy)
		if cancelActor {
			cancel()
		}
		select {
		case <-done:
		case <-time.After(5 * time.Second):
			cancel()
			t.Fatal("idle or canceled stream held a slot")
		}
		cancel()
		select {
		case <-canceled:
		case <-time.After(5 * time.Second):
			t.Fatal("upstream was not canceled")
		}
	}
	if h.calls.Load() != 2 {
		t.Fatal("slot was not released")
	}
}
