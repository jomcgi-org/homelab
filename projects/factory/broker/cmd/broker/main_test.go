package main

import (
	"context"
	"crypto/rand"
	"crypto/rsa"
	"crypto/x509"
	"encoding/json"
	"encoding/pem"
	"fmt"
	"io"
	"log/slog"
	"maps"
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

	"github.com/jomcgi/homelab/projects/factory/broker/config"
	"github.com/jomcgi/homelab/projects/factory/broker/internal/fakes"
	"github.com/jomcgi/homelab/projects/factory/broker/metrics"
	"github.com/jomcgi/homelab/projects/shared/go/tokenbroker/quota"
	authenticationv1 "k8s.io/api/authentication/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/client-go/kubernetes/fake"
	k8stesting "k8s.io/client-go/testing"
)

type routeCase struct {
	method, path string
	status       int
	code         string
}

func testDeps(t *testing.T) (*deps, config.Config) {
	t.Helper()
	idp := fakes.NewIdP(t)
	cfg := config.Defaults()
	cfg.Namespace = "factory"
	cfg.ActorJWT.Issuer = idp.URL
	cfg.TokenReview.AdminSubjects = []string{"system:serviceaccount:factory:admin"}
	d, err := build(context.Background(), cfg, fake.NewSimpleClientset(), quota.NewStore(), metrics.New(), slog.Default())
	if err != nil {
		t.Fatal(err)
	}
	return d, cfg
}

func TestHandlerAuthenticatesEveryCredentialRoute(t *testing.T) {
	d, cfg := testDeps(t)
	checkRoutes(t, d.handler(cfg, http.NotFoundHandler()), []routeCase{
		{http.MethodGet, "/healthz", http.StatusOK, ""},
		{http.MethodPost, "/admin/codex/login", http.StatusUnauthorized, "unauthenticated"},
		{http.MethodGet, "/github/connector-token?repository=o/r", http.StatusUnauthorized, "unauthenticated"},
		{http.MethodGet, "/codex/quota", http.StatusUnauthorized, "unauthenticated"},
		{http.MethodGet, "/github/reviewer-token?repository=o/r", http.StatusUnauthorized, "unauthenticated"},
		{http.MethodGet, "/github/reviewer-comments?repository=o/r&number=1&page=1", http.StatusUnauthorized, "unauthenticated"},
		{http.MethodPost, "/v1/responses", http.StatusUnauthorized, "actor_unauthenticated"},
		{http.MethodPost, "/anthropic/v1/messages", http.StatusNotFound, "not_found"},
		{http.MethodPost, "/anthropic/v1/messages/count_tokens", http.StatusNotFound, "not_found"},
		{http.MethodGet, "/v1/other", http.StatusNotFound, "not_found"},
	})
}

func TestClaudeRoutesAreOptionalAndUseOnlyRelayListener(t *testing.T) {
	d, cfg := testDeps(t)
	if d.claude != nil {
		t.Fatal("Claude relay enabled by default")
	}
	checkRoutes(t, d.relayHandler(), []routeCase{
		{http.MethodPost, "/anthropic/v1/messages", 404, "not_found"},
		{http.MethodPost, "/anthropic/v1/messages/count_tokens", 404, "not_found"},
	})
	cfg.Claude.Enabled = true
	cfg.Claude.TokenFile = filepath.Join(t.TempDir(), "token")
	d, err := build(context.Background(), cfg, fake.NewSimpleClientset(), quota.NewStore(), metrics.New(), slog.Default())
	if err != nil {
		t.Fatal(err)
	}
	for _, h := range []http.Handler{d.relayHandler(), d.handler(cfg, http.NotFoundHandler())} {
		checkRoutes(t, h, []routeCase{
			{http.MethodPost, "/anthropic/v1/messages", 401, "actor_unauthenticated"},
			{http.MethodPost, "/anthropic/v1/messages/count_tokens", 401, "actor_unauthenticated"},
			{http.MethodGet, "/anthropic/v1/messages", 404, "not_found"},
			{http.MethodPost, "/anthropic/v1/other", 404, "not_found"},
			{http.MethodPost, "/v1/responses", 401, "actor_unauthenticated"},
		})
	}
	checkRoutes(t, d.adminHandler(cfg, http.NotFoundHandler()), []routeCase{
		{http.MethodPost, "/anthropic/v1/messages", 404, ""},
		{http.MethodPost, "/anthropic/v1/messages/count_tokens", 404, ""},
	})
}

// Exercise the production config loader, builder, JWT verifier and relay mux
// with the request headers kagent v1.0.1's Claude Code harness sends after
// Substrate replaces its API-key placeholder. Only the provider network endpoint
// is replaced; the subscription token lives in a broker-side fixture file.
func TestClaudeCodeSubscriptionRelayThroughBroker(t *testing.T) {
	const (
		model = "claude-model-fixture" // No live model entitlement is assumed.
		token = "broker-only-subscription-fixture"
		first = "event: message_start\ndata: {\"type\":\"message_start\"}\n\n"
		last  = "event: message_stop\ndata: {\"type\":\"message_stop\"}\n\n"
	)
	idp := fakes.NewIdP(t)
	signer := fakes.NewECSigner(t, "claude-broker")
	idp.Publish(signer)
	write := func(name string, data []byte) string {
		t.Helper()
		path := filepath.Join(t.TempDir(), name)
		if err := os.WriteFile(path, data, 0o600); err != nil {
			t.Fatal(err)
		}
		return path
	}
	caFile := write("ca.pem", pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: idp.Certificate().Raw}))
	tokenFile := write("token", []byte(token+"\n"))
	configFile := write("broker.yaml", []byte(fmt.Sprintf(`
namespace: factory
actorJwt:
  issuer: %q
  caFile: %q
  audience: factory-broker
claude:
  enabled: true
  tokenFile: %q
  modelAllowlist:
    actor/factory-intsec/session-audit-fixture: [%s]
`, idp.URL, caFile, tokenFile, model)))
	cfg, err := config.Load(configFile, func(string) string { return "" })
	if err != nil {
		t.Fatal(err)
	}
	d, err := build(context.Background(), cfg, fake.NewSimpleClientset(), quota.NewStore(), metrics.New(), slog.New(slog.NewTextHandler(io.Discard, nil)))
	if err != nil {
		t.Fatal(err)
	}
	if d.claude == nil || d.claude.Upstream != "https://api.anthropic.com" {
		t.Fatal("enabled configuration did not build the Anthropic relay")
	}
	var calls, upstreamStatus atomic.Int32
	upstreamStatus.Store(http.StatusOK)
	gate := make(chan struct{})
	var release sync.Once
	t.Cleanup(func() { release.Do(func() { close(gate) }) })
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if r.Header.Get("Authorization") != "Bearer "+token || r.Header.Get("X-Api-Key") != "" {
			t.Error("provider must receive only the broker's subscription credential")
		}
		if r.Header.Get("Anthropic-Beta") != "claude-code-20250219,oauth-2025-04-20" ||
			r.Header.Get("Anthropic-Version") != "2023-06-01" || r.Header.Get("X-App") != "cli" ||
			r.UserAgent() != "claude-cli/2.1.285 (external, cli)" {
			t.Error("provider did not receive the Claude Code protocol headers")
		}
		var payload struct {
			Model string `json:"model"`
		}
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil || payload.Model != model || r.Method != http.MethodPost {
			t.Error("provider did not receive the allowed Messages request")
		}
		switch r.URL.Path {
		case "/v1/messages", "/v1/messages/count_tokens":
		default:
			t.Errorf("provider path = %s", r.URL.Path)
		}
		if status := int(upstreamStatus.Load()); status != http.StatusOK {
			w.Header().Set("Content-Type", "application/json")
			w.Header().Set("Retry-After", "17")
			w.WriteHeader(status)
			_, _ = io.WriteString(w, `{"type":"error","error":{"type":"rate_limit_error"}}`)
			return
		}
		if r.URL.Path == "/v1/messages/count_tokens" {
			w.Header().Set("Content-Type", "application/json")
			_, _ = io.WriteString(w, `{"input_tokens":8}`)
			return
		}
		w.Header().Set("Content-Type", "text/event-stream")
		_, _ = io.WriteString(w, first)
		w.(http.Flusher).Flush()
		select {
		case <-gate:
			_, _ = io.WriteString(w, last)
		case <-r.Context().Done():
		}
	}))
	t.Cleanup(upstream.Close)
	d.claude.Upstream, d.claude.Client = upstream.URL, upstream.Client()
	broker := httptest.NewServer(d.relayHandler())
	t.Cleanup(broker.Close)
	client := broker.Client()
	client.Timeout = 5 * time.Second
	jwt := signer.Sign(t, fakes.ActorClaims(idp.URL, "factory-broker", "factory-intsec", "session-audit-fixture", time.Now()))
	do := func(t *testing.T, path, requestedModel, credential string) *http.Response {
		t.Helper()
		body := fmt.Sprintf(`{"model":%q,"max_tokens":16,"messages":[{"role":"user","content":"fixture"}],"stream":true}`, requestedModel)
		r, err := http.NewRequest(http.MethodPost, broker.URL+"/anthropic"+path, strings.NewReader(body))
		if err != nil {
			t.Fatal(err)
		}
		if credential != "" {
			r.Header.Set("X-Api-Key", credential)
		}
		r.Header.Set("User-Agent", "claude-cli/2.1.285 (external, cli)")
		r.Header.Set("X-App", "cli")
		r.Header.Set("Anthropic-Beta", "claude-code-20250219")
		r.Header.Set("Content-Type", "application/json")
		resp, err := client.Do(r)
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { resp.Body.Close() })
		return resp
	}
	for _, path := range []string{"/v1/messages", "/v1/messages/count_tokens"} {
		t.Run(path, func(t *testing.T) {
			for _, tc := range []struct {
				name, model, credential, body string
				status                        int
				upstream                      int32
			}{
				{"missing JWT", model, "", `{"factory_error":"actor_unauthenticated"}`, 401, 0},
				{"wrong audience", model, signer.Sign(t, fakes.ActorClaims(idp.URL, "other", "factory-intsec", "session-audit-fixture", time.Now())), `{"factory_error":"actor_unauthenticated"}`, 401, 0},
				{"other actor", model, signer.Sign(t, fakes.ActorClaims(idp.URL, "factory-broker", "factory-intsec", "session-other", time.Now())), `{"factory_error":"claude_model_denied"}`, 403, 0},
				{"unlisted model", "unlisted-model", jwt, `{"factory_error":"claude_model_denied"}`, 403, 0},
				{"upstream 401", model, jwt, `{"factory_error":"claude_login_required"}`, 401, 401},
				{"upstream 429", model, jwt, `{"type":"error","error":{"type":"rate_limit_error"}}`, 429, 429},
			} {
				t.Run(tc.name, func(t *testing.T) {
					before := calls.Load()
					upstreamStatus.Store(int32(tc.status))
					resp := do(t, path, tc.model, tc.credential)
					body, err := io.ReadAll(resp.Body)
					if err != nil || resp.StatusCode != tc.status || strings.TrimSpace(string(body)) != tc.body {
						t.Fatalf("response = %d %s, %v", resp.StatusCode, body, err)
					}
					if tc.upstream == 0 && calls.Load() != before || tc.upstream != 0 && calls.Load() != before+1 {
						t.Fatal("denied request reached provider or upstream error was retried")
					}
					if tc.upstream == 429 && resp.Header.Get("Retry-After") != "17" {
						t.Fatal("lost Retry-After")
					}
				})
			}
		})
	}
	upstreamStatus.Store(http.StatusOK)
	t.Run("count tokens", func(t *testing.T) {
		resp := do(t, "/v1/messages/count_tokens", model, jwt)
		body, err := io.ReadAll(resp.Body)
		if err != nil || resp.StatusCode != 200 || string(body) != `{"input_tokens":8}` {
			t.Fatalf("count response = %d %s, %v", resp.StatusCode, body, err)
		}
	})
	t.Run("incremental SSE", func(t *testing.T) {
		defer release.Do(func() { close(gate) })
		resp := do(t, "/v1/messages", model, jwt)
		if resp.StatusCode != 200 || resp.Header.Get("Content-Type") != "text/event-stream" {
			t.Fatalf("stream response = %d, %s", resp.StatusCode, resp.Header.Get("Content-Type"))
		}
		chunk := make([]byte, len(first))
		if _, err := io.ReadFull(resp.Body, chunk); err != nil || string(chunk) != first {
			t.Fatalf("first event did not arrive before upstream completion: %q, %v", chunk, err)
		}
		release.Do(func() { close(gate) })
		rest, err := io.ReadAll(resp.Body)
		if err != nil || string(rest) != last {
			t.Fatalf("last event = %q, %v", rest, err)
		}
	})
}

// With a separate admin listener, the relay port serves no admin, connector
// or metrics route, and the admin port serves no relay route.
func TestSplitListenersSeparateRoutes(t *testing.T) {
	d, cfg := testDeps(t)
	metricsOK := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusOK) })
	checkRoutes(t, d.relayHandler(), []routeCase{
		{http.MethodGet, "/healthz", http.StatusOK, ""},
		{http.MethodPost, "/v1/responses", http.StatusUnauthorized, "actor_unauthenticated"},
		{http.MethodPost, "/admin/codex/login", http.StatusNotFound, "not_found"},
		{http.MethodGet, "/github/connector-token?repository=o/r", http.StatusNotFound, "not_found"},
		{http.MethodGet, "/codex/quota", http.StatusNotFound, "not_found"},
		{http.MethodGet, "/github/reviewer-token?repository=o/r", http.StatusNotFound, "not_found"},
		{http.MethodGet, "/github/reviewer-comments?repository=o/r&number=1&page=1", http.StatusNotFound, "not_found"},
		{http.MethodGet, "/metrics", http.StatusNotFound, "not_found"},
	})
	checkRoutes(t, d.adminHandler(cfg, metricsOK), []routeCase{
		{http.MethodGet, "/healthz", http.StatusOK, ""},
		{http.MethodGet, "/metrics", http.StatusOK, ""},
		{http.MethodPost, "/admin/codex/login", http.StatusUnauthorized, "unauthenticated"},
		{http.MethodGet, "/github/connector-token?repository=o/r", http.StatusUnauthorized, "unauthenticated"},
		{http.MethodGet, "/codex/quota", http.StatusUnauthorized, "unauthenticated"},
		{http.MethodGet, "/github/reviewer-token?repository=o/r", http.StatusUnauthorized, "unauthenticated"},
		{http.MethodGet, "/github/reviewer-comments?repository=o/r&number=1&page=1", http.StatusUnauthorized, "unauthenticated"},
		{http.MethodPost, "/v1/responses", http.StatusNotFound, ""},
		// The comment token is never served; the proxy writes with it.
		{http.MethodGet, "/github/reviewer-comment-token?repository=o/r", http.StatusNotFound, ""},
	})
}

func checkRoutes(t *testing.T, h http.Handler, cases []routeCase) {
	t.Helper()
	for _, tc := range cases {
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, httptest.NewRequest(tc.method, tc.path, nil))
		if rec.Code != tc.status {
			t.Fatalf("%s %s = %d, want %d", tc.method, tc.path, rec.Code, tc.status)
		}
		if tc.code != "" {
			var body map[string]string
			_ = json.Unmarshal(rec.Body.Bytes(), &body)
			if body["factory_error"] != tc.code {
				t.Fatalf("%s %s factory_error = %q, want %q", tc.method, tc.path, body["factory_error"], tc.code)
			}
		}
	}
}

// The reviewer token route admits only reviewer subjects: the connector,
// the author's identity, is refused, and a reviewer reaches the handler
// (503 here, as no reviewer App key is loaded). The connector's routes
// refuse the reviewer, except for the shared read-only quota endpoint.
func TestReviewerTokenAdmitsOnlyReviewerSubjects(t *testing.T) {
	const connector, reviewer = "system:serviceaccount:factory:factory-connector", "system:serviceaccount:factory:factory-reviewer"
	client := fake.NewSimpleClientset()
	client.PrependReactor("create", "tokenreviews", func(action k8stesting.Action) (bool, runtime.Object, error) {
		review := action.(k8stesting.CreateAction).GetObject().(*authenticationv1.TokenReview).DeepCopy()
		user := map[string]string{"connector-token": connector, "reviewer-token": reviewer, "actor-token": "system:serviceaccount:factory:actor"}[review.Spec.Token]
		if user != "" {
			review.Status = authenticationv1.TokenReviewStatus{Authenticated: true, Audiences: review.Spec.Audiences, User: authenticationv1.UserInfo{Username: user}}
		}
		return true, review, nil
	})
	idp := fakes.NewIdP(t)
	cfg := config.Defaults()
	cfg.Namespace = "factory"
	cfg.ActorJWT.Issuer = idp.URL
	cfg.TokenReview.ConnectorSubjects = []string{connector}
	cfg.TokenReview.ReviewerSubjects = []string{reviewer}
	d, err := build(context.Background(), cfg, client, quota.NewStore(), metrics.New(), slog.Default())
	if err != nil {
		t.Fatal(err)
	}
	h := d.adminHandler(cfg, http.NotFoundHandler())
	for _, tc := range []struct {
		path, token string
		status      int
	}{
		{"/github/reviewer-token?repository=o/r", "connector-token", http.StatusForbidden},
		{"/github/reviewer-token?repository=o/r", "reviewer-token", http.StatusServiceUnavailable},
		{"/github/reviewer-comments?repository=o/r&number=1&page=1", "connector-token", http.StatusForbidden},
		{"/github/reviewer-comments?repository=o/r&number=1&page=1", "reviewer-token", http.StatusServiceUnavailable},
		{"/github/connector-token?repository=o/r", "reviewer-token", http.StatusForbidden},
		{"/runs/reviewer", "connector-token", http.StatusForbidden},
		{"/runs/reviewer", "actor-token", http.StatusForbidden},
		{"/runs/reviewer", "reviewer-token", http.StatusMethodNotAllowed},
		{"/runs/reviewer/task", "actor-token", http.StatusForbidden},
		{"/runs/reviewer/task-pin", "connector-token", http.StatusForbidden},
		{"/runs/reviewer/task-pin", "reviewer-token", http.StatusMethodNotAllowed},
		// Both dispatchers can read quota; unrelated subjects remain refused.
		{"/codex/quota", "reviewer-token", http.StatusOK},
		{"/codex/quota", "unknown-token", http.StatusUnauthorized},
		{"/codex/quota", "actor-token", http.StatusForbidden},
		{"/codex/quota", "connector-token", http.StatusOK},
	} {
		req := httptest.NewRequest(http.MethodGet, tc.path, nil)
		req.Header.Set("Authorization", "Bearer "+tc.token)
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, req)
		if rec.Code != tc.status {
			t.Fatalf("%s with %s = %d, want %d", tc.path, tc.token, rec.Code, tc.status)
		}
	}
}

// The comment token comes from its own minter, with only the comment
// permissions, so the check token never gains issues write and an App
// without it fails only the comment. An empty map serves none.
func TestCommentMinterHoldsOnlyCommentPermissions(t *testing.T) {
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(t.TempDir(), "key.pem")
	pemKey := pem.EncodeToMemory(&pem.Block{Type: "RSA PRIVATE KEY", Bytes: x509.MarshalPKCS1PrivateKey(key)})
	if err := os.WriteFile(path, pemKey, 0o600); err != nil {
		t.Fatal(err)
	}
	idp := fakes.NewIdP(t)
	cfg := config.Defaults()
	cfg.Namespace = "factory"
	cfg.ActorJWT.Issuer = idp.URL
	cfg.GitHubReview.PrivateKeyFile, cfg.GitHubReview.AppID, cfg.GitHubReview.InstallationID = path, "9", "3"
	cfg.ReviewBindings = []config.Binding{{Name: "homelab", Repository: "o/r", Namespace: "factory-r-homelab-review"}}
	d, err := build(context.Background(), cfg, fake.NewSimpleClientset(), quota.NewStore(), metrics.New(), slog.Default())
	if err != nil {
		t.Fatal(err)
	}
	// Without the bot login there is no comment proxy and no comment token.
	if d.commentMinter != nil || d.comments != nil {
		t.Fatalf("comment minter without an author = %+v", d.commentMinter)
	}
	cfg.GitHubReview.CommentAuthor = "jomcgi-factory-review[bot]"
	d, err = build(context.Background(), cfg, fake.NewSimpleClientset(), quota.NewStore(), metrics.New(), slog.Default())
	if err != nil {
		t.Fatal(err)
	}
	if d.commentMinter == nil || !maps.Equal(d.commentMinter.ConnectorPermissions, map[string]string{"issues": "write", "pull_requests": "write"}) ||
		d.commentMinter.App != d.reviewMinter.App || d.reviewMinter.ConnectorPermissions["issues"] != "" ||
		d.reviewMinter.ConnectorPermissions["pull_requests"] != "read" ||
		d.comments == nil || d.comments.Minter != d.commentMinter || d.comments.Author != "jomcgi-factory-review[bot]" {
		t.Fatalf("comment minter = %+v, review minter permissions %v", d.commentMinter, d.reviewMinter.ConnectorPermissions)
	}
	cfg.GitHubReview.CommentPermissions = map[string]string{}
	d, err = build(context.Background(), cfg, fake.NewSimpleClientset(), quota.NewStore(), metrics.New(), slog.Default())
	if err != nil || d.commentMinter != nil {
		t.Fatalf("comment minter without permissions = %+v, %v", d.commentMinter, err)
	}
}

func TestBrowserAdmin(t *testing.T) {
	d, cfg := testDeps(t)
	idp := fakes.NewIdP(t)
	key := fakes.NewRSASigner(t, "admin")
	idp.Publish(key)
	var err error
	d.adminVerifier, err = actorjwt.New(actorjwt.Options{Issuer: idp.URL, Audience: "factory-edge", HTTPClient: idp.Client()})
	if err != nil {
		t.Fatal(err)
	}
	h := d.adminHandler(cfg, http.NotFoundHandler())
	for _, tc := range []struct {
		method, path string
		status       int
	}{
		{"GET", "/admin/codex/", 200}, {"GET", "/admin/codex/login", 200}, {"POST", "/admin/codex/login", 403}, {"GET", "/v1/models", 404},
	} {
		req := httptest.NewRequest(tc.method, tc.path, nil)
		req.Header.Set("Authorization", "Bearer "+key.Sign(t, map[string]any{"iss": idp.URL, "aud": "factory-edge", "exp": time.Now().Add(time.Minute).Unix(), "groups": []string{"authentik Admins"}, "sub": "operator"}))
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, req)
		if rec.Code != tc.status {
			t.Errorf("%s %s = %d, want %d", tc.method, tc.path, rec.Code, tc.status)
		}
	}
}
