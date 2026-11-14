package relay

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/factory/broker/actorjwt"
	"github.com/jomcgi/homelab/projects/factory/broker/grant"
	"github.com/jomcgi/homelab/projects/factory/broker/internal/fakes"
	"github.com/jomcgi/homelab/projects/factory/broker/metrics"
	"github.com/jomcgi/homelab/projects/shared/go/tokenbroker/provider/codexchatgpt"
	"github.com/jomcgi/homelab/projects/shared/go/tokenbroker/quota"
	"github.com/jomcgi/homelab/projects/shared/go/tokenbroker/store"
	"github.com/prometheus/client_golang/prometheus/testutil"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes/fake"
)

const audience = "factory-broker"

// backend fakes chatgpt.com/backend-api/codex: it checks the grant's
// credentials and streams SSE with the x-codex quota headers.
type backend struct {
	*httptest.Server
	mu       sync.Mutex
	calls    int
	accepts  func(token string) bool
	respond  func(w http.ResponseWriter, r *http.Request)
	lastReq  *http.Request
	lastBody []byte
}

func newBackend(t *testing.T) *backend {
	t.Helper()
	b := &backend{accepts: func(string) bool { return true }}
	b.Server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		b.mu.Lock()
		b.calls++
		b.lastReq, b.lastBody = r.Clone(context.Background()), body
		accepts, respond := b.accepts, b.respond
		b.mu.Unlock()
		token := strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")
		if !accepts(token) || r.Header.Get("Chatgpt-Account-Id") != "acct-1" {
			w.WriteHeader(http.StatusUnauthorized)
			_, _ = w.Write([]byte(`{"detail":"Unauthorized"}`))
			return
		}
		if respond != nil {
			respond(w, r)
			return
		}
		sse(w, nil)
	}))
	t.Cleanup(b.Close)
	return b
}

func (b *backend) set(f func(*backend)) { b.mu.Lock(); f(b); b.mu.Unlock() }
func (b *backend) count() int           { b.mu.Lock(); defer b.mu.Unlock(); return b.calls }

func quotaHeaders(w http.ResponseWriter, used int, reset time.Time) {
	w.Header().Set("x-codex-primary-used-percent", fmt.Sprint(used))
	w.Header().Set("x-codex-primary-window-minutes", "300")
	w.Header().Set("x-codex-primary-reset-at", fmt.Sprint(reset.Unix()))
}

// sse writes three events, waiting on gate (when set) after the first.
func sse(w http.ResponseWriter, gate chan struct{}) {
	quotaHeaders(w, 12, time.Now().Add(time.Hour))
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("X-Request-Id", "req-123")
	w.Header().Set("Set-Cookie", "session=upstream")
	w.WriteHeader(http.StatusOK)
	flusher := w.(http.Flusher)
	for i, event := range []string{"response.created", "response.output_text.delta", "response.completed"} {
		fmt.Fprintf(w, "event: %s\ndata: {\"type\":%q}\n\n", event, event)
		flusher.Flush()
		if i == 0 && gate != nil {
			<-gate
		}
	}
}

type harness struct {
	t       *testing.T
	idp     *fakes.IdP
	signer  fakes.Signer
	auth    *fakes.OpenAIAuth
	backend *backend
	client  *fake.Clientset
	quota   *quota.Store
	metrics *metrics.Metrics
	logs    *bytes.Buffer
	logMu   *sync.Mutex
	relay   *httptest.Server
	handler *Relay
	access  string
}

func (h *harness) relayHandler() *Relay { return h.handler }

type lockedWriter struct {
	mu *sync.Mutex
	b  *bytes.Buffer
}

func (w lockedWriter) Write(p []byte) (int, error) {
	w.mu.Lock()
	defer w.mu.Unlock()
	return w.b.Write(p)
}

func newHarness(t *testing.T, seedGrant bool) *harness {
	t.Helper()
	h := &harness{t: t, idp: fakes.NewIdP(t), signer: fakes.NewECSigner(t, "k1"), auth: fakes.NewOpenAIAuth(t), backend: newBackend(t), logs: &bytes.Buffer{}, logMu: &sync.Mutex{}}
	h.idp.Publish(h.signer)
	h.client = fake.NewSimpleClientset(&corev1.Secret{ObjectMeta: metav1.ObjectMeta{Name: "factory-broker-grant-codex-primary", Namespace: "factory"}})
	if seedGrant {
		_, refresh := h.auth.Issue()
		h.access = fakes.AccessToken("acct-1", time.Now().Add(5*24*time.Hour), "seed")
		st := &store.SecretStore{Client: h.client, Namespace: "factory", NamePrefix: "factory-broker-grant-"}
		if err := st.SaveGrant(store.Grant{Name: "codex-primary", LastRefresh: time.Now().UTC(), TokenBundle: store.TokenBundle{
			AccessToken: h.access, RefreshToken: refresh, ExpiresAt: time.Now().Add(5 * 24 * time.Hour), AccountID: "acct-1",
		}}); err != nil {
			t.Fatal(err)
		}
	}
	logger := slog.New(slog.NewJSONHandler(lockedWriter{h.logMu, h.logs}, &slog.HandlerOptions{Level: slog.LevelDebug}))
	h.metrics = metrics.New()
	mgr := grant.New(grant.Options{
		Grant: "codex-primary", Client: h.client, Namespace: "factory", SecretPrefix: "factory-broker-grant-",
		Adapter:             &codexchatgpt.Adapter{Issuer: h.auth.URL, HTTPClient: h.auth.Client()},
		RefreshBeforeExpiry: 24 * time.Hour, MaxGrantAge: 7 * 24 * time.Hour, Metrics: h.metrics, Logger: logger,
	})
	verifier, err := actorjwt.New(actorjwt.Options{Issuer: h.idp.URL, Audience: audience, HTTPClient: h.idp.Client(), Logger: logger})
	if err != nil {
		t.Fatal(err)
	}
	h.quota = quota.NewStore()
	h.handler = &Relay{
		Verifier: verifier, Tokens: mgr, Upstream: h.backend.URL + "/backend-api/codex",
		Client: h.backend.Client(), Quota: h.quota, Metrics: h.metrics, Logger: logger,
	}
	h.relay = httptest.NewServer(h.handler)
	t.Cleanup(h.relay.Close)
	return h
}

func (h *harness) actorJWT() string {
	return h.signer.Sign(h.t, fakes.ActorClaims(h.idp.URL, audience, "factory-homelab", "session-1", time.Now()))
}

func (h *harness) post(path, jwt string) *http.Response {
	h.t.Helper()
	req, _ := http.NewRequest(http.MethodPost, h.relay.URL+path, strings.NewReader(`{"model":"gpt-5","stream":true}`))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "text/event-stream")
	req.Header.Set("Session_id", "sess-1")
	req.Header.Set("Cookie", "actor=secret")
	if jwt != "" {
		req.Header.Set("Authorization", "Bearer "+jwt)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		h.t.Fatal(err)
	}
	h.t.Cleanup(func() { resp.Body.Close() })
	return resp
}

func factoryError(t *testing.T, resp *http.Response) map[string]string {
	t.Helper()
	var body map[string]string
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		t.Fatalf("decode error body: %v", err)
	}
	return body
}

// waitQuota waits for the asynchronous quota recorder to commit.
func (h *harness) waitQuota(ok func(quota.View) bool) quota.View {
	h.t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for {
		view := h.quota.GetGrant("codex-primary")
		if ok(view) {
			return view
		}
		if time.Now().After(deadline) {
			h.t.Fatalf("quota view never matched: %+v", view)
		}
		time.Sleep(5 * time.Millisecond)
	}
}

func (h *harness) logText() string { h.logMu.Lock(); defer h.logMu.Unlock(); return h.logs.String() }

func TestRelayStreamsWithGrantCredentials(t *testing.T) {
	h := newHarness(t, true)
	gate := make(chan struct{})
	h.backend.set(func(b *backend) { b.respond = func(w http.ResponseWriter, r *http.Request) { sse(w, gate) } })
	jwt := h.actorJWT()
	resp := h.post("/v1/responses", jwt)
	if resp.StatusCode != http.StatusOK || resp.Header.Get("Content-Type") != "text/event-stream" {
		t.Fatalf("status = %d, content type %q", resp.StatusCode, resp.Header.Get("Content-Type"))
	}
	if resp.Header.Get("Set-Cookie") != "" {
		t.Fatal("upstream cookie reached the actor")
	}
	// The first event arrives while the backend is still holding the stream.
	reader := bufio.NewReader(resp.Body)
	line, err := reader.ReadString('\n')
	if err != nil || line != "event: response.created\n" {
		t.Fatalf("first line = %q, %v", line, err)
	}
	close(gate)
	rest, _ := io.ReadAll(reader)
	if !strings.Contains(string(rest), "response.completed") {
		t.Fatalf("stream tail = %q", rest)
	}

	h.backend.mu.Lock()
	req, body := h.backend.lastReq, string(h.backend.lastBody)
	h.backend.mu.Unlock()
	if req.URL.Path != "/backend-api/codex/responses" || req.Header.Get("Authorization") != "Bearer "+h.access {
		t.Fatalf("upstream path %q, authorization not the grant's access token", req.URL.Path)
	}
	if req.Header.Get("Cookie") != "" || req.Header.Get("Session_id") != "sess-1" || body != `{"model":"gpt-5","stream":true}` {
		t.Fatalf("upstream headers %v body %q", req.Header, body)
	}

	view := h.waitQuota(func(v quota.View) bool { return v.Observed })
	if len(view.Windows) != 1 || view.Windows[0].UsedPercent != 12 {
		t.Fatalf("quota view = %+v", view)
	}
	if got := testutil.ToFloat64(h.metrics.RelayRequests.WithLabelValues("ok", "2xx")); got != 1 {
		t.Fatalf("relay ok metric = %v", got)
	}
	logs := h.logText()
	if strings.Contains(logs, h.access) || strings.Contains(logs, jwt) {
		t.Fatal("a credential reached the logs")
	}
	if !strings.Contains(logs, `"actor":"actor/factory-homelab/session-1"`) || !strings.Contains(logs, `"upstream_request_id":"req-123"`) {
		t.Fatalf("relay log lacks attribution: %s", logs)
	}
}

func TestRelayRejectsActors(t *testing.T) {
	h := newHarness(t, true)
	expired := h.signer.Sign(t, fakes.ActorClaims(h.idp.URL, audience, "factory-homelab", "session-1", time.Now().Add(-time.Hour)))
	wrongAud := h.signer.Sign(t, fakes.ActorClaims(h.idp.URL, "other", "factory-homelab", "session-1", time.Now()))
	for name, jwt := range map[string]string{"missing": "", "expired": expired, "audience": wrongAud, "garbage": "x.y.z"} {
		t.Run(name, func(t *testing.T) {
			resp := h.post("/v1/responses", jwt)
			if resp.StatusCode != http.StatusUnauthorized || factoryError(t, resp)["factory_error"] != ErrActorUnauthenticated {
				t.Fatalf("status = %d", resp.StatusCode)
			}
		})
	}
	if h.backend.count() != 0 {
		t.Fatal("an unauthenticated request reached the backend")
	}
	if resp := h.post("/v1/files", h.actorJWT()); resp.StatusCode != http.StatusNotFound {
		t.Fatalf("unlisted path status = %d", resp.StatusCode)
	}
	if got := testutil.ToFloat64(h.metrics.ActorRejections.WithLabelValues(actorjwt.ReasonExpired)); got != 1 {
		t.Fatalf("expired rejections = %v", got)
	}
}

func TestRelayDeadGrant(t *testing.T) {
	h := newHarness(t, false)
	resp := h.post("/responses", h.actorJWT())
	if resp.StatusCode != http.StatusUnauthorized || factoryError(t, resp)["factory_error"] != "codex_login_required" {
		t.Fatalf("status = %d", resp.StatusCode)
	}
	if h.backend.count() != 0 {
		t.Fatal("a request without a grant reached the backend")
	}
}

func TestRelayUpstreamUnauthorizedRefreshesOnce(t *testing.T) {
	h := newHarness(t, true)
	h.backend.set(func(b *backend) { b.accepts = func(token string) bool { return token != h.access } })
	resp := h.post("/v1/responses", h.actorJWT())
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status after forced refresh = %d", resp.StatusCode)
	}
	if h.auth.RefreshCalls() != 1 || h.backend.count() != 2 {
		t.Fatalf("refresh calls %d, backend calls %d", h.auth.RefreshCalls(), h.backend.count())
	}

	// A backend 401 with a revoked refresh token: the grant is dead.
	h2 := newHarness(t, true)
	h2.backend.set(func(b *backend) { b.accepts = func(string) bool { return false } })
	h2.auth.Revoke()
	resp = h2.post("/v1/responses", h2.actorJWT())
	if resp.StatusCode != http.StatusUnauthorized || factoryError(t, resp)["factory_error"] != "codex_login_required" {
		t.Fatalf("dead grant status = %d", resp.StatusCode)
	}

	// A backend that refuses even the refreshed token: not proof of a
	// dead grant.
	h3 := newHarness(t, true)
	h3.backend.set(func(b *backend) { b.accepts = func(string) bool { return false } })
	resp = h3.post("/v1/responses", h3.actorJWT())
	if resp.StatusCode != http.StatusServiceUnavailable || factoryError(t, resp)["factory_error"] != ErrGrantUnavailable {
		t.Fatalf("refused refreshed token status = %d", resp.StatusCode)
	}
}

func TestRelayTransientRefreshFailureIsUnavailable(t *testing.T) {
	h := newHarness(t, true)
	h.backend.set(func(b *backend) { b.accepts = func(token string) bool { return token != h.access } })
	h.auth.FailTransiently(true)
	resp := h.post("/v1/responses", h.actorJWT())
	if resp.StatusCode != http.StatusServiceUnavailable || factoryError(t, resp)["factory_error"] != ErrGrantUnavailable {
		t.Fatalf("transient refresh failure status = %d", resp.StatusCode)
	}
	if testutil.ToFloat64(h.metrics.GrantState.WithLabelValues("codex-primary", "login_required")) != 0 {
		t.Fatal("a transient failure reported login_required")
	}
}

func TestRelayConcurrentUnauthorizedShareOneRefresh(t *testing.T) {
	h := newHarness(t, true)
	h.backend.set(func(b *backend) { b.accepts = func(token string) bool { return token != h.access } })
	h.auth.SetDelay(50 * time.Millisecond)
	const callers = 8
	jwt := h.actorJWT()
	codes := make([]int, callers)
	var wg sync.WaitGroup
	for i := 0; i < callers; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			req, _ := http.NewRequest(http.MethodPost, h.relay.URL+"/v1/responses", strings.NewReader(`{}`))
			req.Header.Set("Authorization", "Bearer "+jwt)
			resp, err := http.DefaultClient.Do(req)
			if err != nil {
				t.Error(err)
				return
			}
			_, _ = io.Copy(io.Discard, resp.Body)
			resp.Body.Close()
			codes[i] = resp.StatusCode
		}(i)
	}
	wg.Wait()
	for i, code := range codes {
		if code != http.StatusOK {
			t.Fatalf("caller %d status = %d", i, code)
		}
	}
	if h.auth.RefreshCalls() != 1 {
		t.Fatalf("refresh calls = %d, want 1", h.auth.RefreshCalls())
	}
}

func TestRelayLimits(t *testing.T) {
	h := newHarness(t, true)
	gate := make(chan struct{})
	h.backend.set(func(b *backend) {
		b.respond = func(w http.ResponseWriter, r *http.Request) {
			w.WriteHeader(http.StatusOK)
			w.(http.Flusher).Flush()
			<-gate // a stream that goes silent
		}
	})
	defer close(gate)
	rl := h.relayHandler()
	rl.StreamIdleTimeout = 100 * time.Millisecond
	rl.MaxConcurrent = 1
	rl.MaxRequestBytes = 1 << 10

	first := make(chan *http.Response, 1)
	go func() { first <- h.post("/v1/responses", h.actorJWT()) }()
	// The first request holds the only slot until its stream idles out.
	deadline := time.Now().Add(5 * time.Second)
	for h.backend.count() == 0 && time.Now().Before(deadline) {
		time.Sleep(5 * time.Millisecond)
	}
	busy := h.post("/v1/responses", h.actorJWT())
	if busy.StatusCode != http.StatusServiceUnavailable || factoryError(t, busy)["factory_error"] != ErrRelayBusy {
		t.Fatalf("over-capacity status = %d", busy.StatusCode)
	}
	resp := <-first
	start := time.Now()
	_, _ = io.ReadAll(resp.Body)
	if time.Since(start) > 3*time.Second {
		t.Fatal("idle stream was not cut off")
	}
	if got := testutil.ToFloat64(h.metrics.RelayRequests.WithLabelValues("upstream_stream_error", "2xx")); got != 1 {
		t.Fatalf("idle stream metric = %v", got)
	}

	req, _ := http.NewRequest(http.MethodPost, h.relay.URL+"/v1/responses", strings.NewReader(strings.Repeat("x", 2<<10)))
	req.Header.Set("Authorization", "Bearer "+h.actorJWT())
	big, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer big.Body.Close()
	if big.StatusCode != http.StatusRequestEntityTooLarge {
		t.Fatalf("oversized body status = %d", big.StatusCode)
	}
}

func TestRelayQuota(t *testing.T) {
	h := newHarness(t, true)
	reset := time.Now().Add(2 * time.Hour).Truncate(time.Second)
	h.backend.set(func(b *backend) {
		b.respond = func(w http.ResponseWriter, r *http.Request) {
			quotaHeaders(w, 100, reset)
			w.Header().Set("x-codex-rate-limit-reached-type", "primary")
			w.WriteHeader(http.StatusTooManyRequests)
			_, _ = w.Write([]byte(`{"error":{"type":"usage_limit_reached","message":"limit"}}`))
		}
	})
	resp := h.post("/v1/responses", h.actorJWT())
	body := factoryError(t, resp)
	if resp.StatusCode != http.StatusTooManyRequests || body["factory_error"] != ErrQuotaExhausted || body["resets_at"] != reset.UTC().Format(time.RFC3339) {
		t.Fatalf("quota response = %d %v", resp.StatusCode, body)
	}
	if resp.Header.Get("Retry-After") == "" {
		t.Fatal("quota response has no Retry-After")
	}
	// The observation now blocks without calling the backend.
	h.waitQuota(func(v quota.View) bool { return v.Exhausted })
	calls := h.backend.count()
	resp = h.post("/v1/responses", h.actorJWT())
	if resp.StatusCode != http.StatusTooManyRequests || factoryError(t, resp)["factory_error"] != ErrQuotaExhausted || h.backend.count() != calls {
		t.Fatalf("pre-check status = %d, backend calls %d -> %d", resp.StatusCode, calls, h.backend.count())
	}
	if got := testutil.ToFloat64(h.metrics.RelayRequests.WithLabelValues("quota_exhausted", "4xx")); got != 2 {
		t.Fatalf("quota metric = %v", got)
	}
}

func TestRelayPassesThroughTransient429(t *testing.T) {
	h := newHarness(t, true)
	h.backend.set(func(b *backend) {
		b.respond = func(w http.ResponseWriter, r *http.Request) {
			w.WriteHeader(http.StatusTooManyRequests)
			_, _ = w.Write([]byte(`{"error":{"type":"rate_limit_exceeded"}}`))
		}
	})
	resp := h.post("/v1/responses", h.actorJWT())
	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != http.StatusTooManyRequests || !strings.Contains(string(body), "rate_limit_exceeded") {
		t.Fatalf("transient 429 = %d %s", resp.StatusCode, body)
	}
}

// A server ReadTimeout bounds the request upload only: a stream that runs
// longer than it must arrive whole.
func TestRelayStreamOutlivesServerReadTimeout(t *testing.T) {
	h := newHarness(t, true)
	h.backend.set(func(b *backend) {
		b.respond = func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("Content-Type", "text/event-stream")
			w.WriteHeader(http.StatusOK)
			flusher := w.(http.Flusher)
			for i := 0; i < 8; i++ {
				fmt.Fprintf(w, "data: {\"n\":%d}\n\n", i)
				flusher.Flush()
				time.Sleep(100 * time.Millisecond)
			}
			fmt.Fprint(w, "event: response.completed\ndata: {}\n\n")
		}
	})
	srv := httptest.NewUnstartedServer(h.handler)
	srv.Config.ReadTimeout = 200 * time.Millisecond
	srv.Start()
	t.Cleanup(srv.Close)

	req, _ := http.NewRequest(http.MethodPost, srv.URL+"/v1/responses", strings.NewReader(`{"stream":true}`))
	req.Header.Set("Authorization", "Bearer "+h.actorJWT())
	start := time.Now()
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatalf("stream read: %v", err)
	}
	if elapsed := time.Since(start); elapsed < 4*srv.Config.ReadTimeout {
		t.Fatalf("stream took %v; the test needs it to outlive ReadTimeout", elapsed)
	}
	if !strings.Contains(string(body), `{"n":7}`) || !strings.Contains(string(body), "response.completed") {
		t.Fatalf("stream cut short: %q", body)
	}
	if got := testutil.ToFloat64(h.metrics.RelayRequests.WithLabelValues("ok", "2xx")); got != 1 {
		t.Fatalf("relay ok metric = %v", got)
	}
}
