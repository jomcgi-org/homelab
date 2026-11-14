// Package relay forwards Codex Responses API calls from kagent actors to the
// ChatGPT Codex backend with the broker's grant. The actor authenticates with
// the Substrate actor JWT the egress gateway injects; provider credentials
// never leave the broker.
package relay

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/jomcgi/homelab/projects/factory/broker/actorjwt"
	"github.com/jomcgi/homelab/projects/factory/broker/auth"
	"github.com/jomcgi/homelab/projects/factory/broker/grant"
	"github.com/jomcgi/homelab/projects/factory/broker/metrics"
	"github.com/jomcgi/homelab/projects/shared/go/tokenbroker/provider/codexchatgpt"
	"github.com/jomcgi/homelab/projects/shared/go/tokenbroker/quota"
)

// Error codes in the {"factory_error": ...} body. The connector classifies
// relay failures by these values.
const (
	ErrActorUnauthenticated = "actor_unauthenticated"
	ErrLoginRequired        = "codex_login_required"
	ErrGrantUnavailable     = "codex_grant_unavailable"
	ErrQuotaExhausted       = "codex_quota_exhausted"
	ErrUpstreamUnreachable  = "codex_upstream_unreachable"
	ErrRequestTooLarge      = "request_too_large"
	ErrRelayBusy            = "relay_busy"
	ErrNotFound             = "not_found"
)

// Defaults for the zero values of the Relay's limits.
const (
	DefaultMaxRequestBytes   = 32 << 20
	DefaultStreamIdleTimeout = 5 * time.Minute
	DefaultMaxConcurrent     = 64
)

// QuotaProvider is the quota store's provider key for Codex observations.
const QuotaProvider = "codex"

type Verifier interface {
	Verify(ctx context.Context, raw string) (actorjwt.Identity, error)
}

type TokenSource interface {
	Grant() string
	AccessToken(ctx context.Context) (grant.Token, error)
	RefreshRejected(ctx context.Context, rejected string) (grant.Token, error)
}

type Relay struct {
	Verifier Verifier
	Tokens   TokenSource
	// Upstream is the Codex backend prefix, such as
	// https://chatgpt.com/backend-api/codex.
	Upstream        string
	Client          *http.Client
	Quota           *quota.Store
	Metrics         *metrics.Metrics
	Logger          *slog.Logger
	MaxRequestBytes int64
	// StreamIdleTimeout cancels an upstream response that sends nothing
	// for this long.
	StreamIdleTimeout time.Duration
	// MaxConcurrent caps requests in flight to the backend.
	MaxConcurrent int
	Now           func() time.Time

	initOnce sync.Once
	slots    chan struct{}
	quotaRec *quotaRecorder
}

func (rl *Relay) init() {
	rl.initOnce.Do(func() {
		n := rl.MaxConcurrent
		if n <= 0 {
			n = DefaultMaxConcurrent
		}
		rl.slots = make(chan struct{}, n)
		if rl.Quota != nil {
			rl.quotaRec = newQuotaRecorder(rl.Quota, rl.Tokens.Grant(), rl.Logger)
		}
	})
}

// route is one relayed endpoint, named by its path under the base URL.
type route struct {
	method, suffix string
}

// Codex calls {baseUrl}/responses; the others are the endpoints it uses
// alongside it [I]. Anything else is refused, so the relay is not a general
// proxy onto the ChatGPT backend.
var routes = map[string]route{
	"responses":         {http.MethodPost, "responses"},
	"responses/compact": {http.MethodPost, "responses/compact"},
	"models":            {http.MethodGet, "models"},
}

func match(r *http.Request) (route, bool) {
	path := strings.TrimPrefix(r.URL.Path, "/v1")
	rt, ok := routes[strings.TrimPrefix(path, "/")]
	if !ok || r.Method != rt.method {
		return route{}, false
	}
	return rt, true
}

// requestHeaders are the only headers forwarded to the backend: the request
// shape and the Codex routing headers [I]. Everything else, including the
// actor's Authorization and any cookies, is dropped.
var requestHeaders = map[string]bool{
	"Accept": true, "Content-Type": true, "Content-Encoding": true, "User-Agent": true,
	"Openai-Beta": true, "Originator": true, "Session_id": true, "Conversation_id": true, "Version": true,
	"X-Codex-Beta-Features": true, "X-Codex-Turn-State": true, "X-Codex-Turn-Metadata": true,
	"X-Openai-Subagent": true,
}

func forwardRequestHeader(name string) bool { return requestHeaders[http.CanonicalHeaderKey(name)] }

// hopHeaders are never copied back to the actor.
var hopHeaders = map[string]bool{
	"Connection": true, "Keep-Alive": true, "Proxy-Authenticate": true, "Proxy-Authorization": true,
	"Te": true, "Trailer": true, "Transfer-Encoding": true, "Upgrade": true, "Set-Cookie": true, "Content-Length": true,
}

type outcome struct {
	name   string
	status int
}

func (rl *Relay) now() time.Time {
	if rl.Now != nil {
		return rl.Now()
	}
	return time.Now()
}

func (rl *Relay) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	start := rl.now()
	var id actorjwt.Identity
	var upstreamID string
	result := rl.serve(w, r, &id, &upstreamID)
	rl.Metrics.RelayRequests.WithLabelValues(result.name, metrics.StatusClass(result.status)).Inc()
	// Never log credentials: the actor JWT, access token and request body
	// stay out of this line.
	rl.Logger.Info("codex relay", "actor", id.Subject, "atespace", id.Atespace, "actor_uid", id.ActorUID,
		"path", r.URL.Path, "status", result.status, "outcome", result.name,
		"upstream_request_id", upstreamID, "duration_ms", rl.now().Sub(start).Milliseconds())
}

func (rl *Relay) serve(w http.ResponseWriter, r *http.Request, id *actorjwt.Identity, upstreamID *string) outcome {
	rl.init()
	rt, ok := match(r)
	if !ok {
		return writeError(w, http.StatusNotFound, ErrNotFound, "not_found", nil)
	}
	raw, ok := auth.BearerToken(r)
	if !ok {
		rl.Metrics.ActorRejections.WithLabelValues(actorjwt.ReasonMalformed).Inc()
		return writeError(w, http.StatusUnauthorized, ErrActorUnauthenticated, "actor_unauthenticated", nil)
	}
	identity, err := rl.Verifier.Verify(r.Context(), raw)
	if err != nil {
		reason := "error"
		var jwtErr *actorjwt.Error
		if errors.As(err, &jwtErr) {
			reason = jwtErr.Reason
		}
		rl.Metrics.ActorRejections.WithLabelValues(reason).Inc()
		return writeError(w, http.StatusUnauthorized, ErrActorUnauthenticated, "actor_unauthenticated", nil)
	}
	*id = identity

	if until, blocked := rl.quotaBlock(rl.now()); blocked {
		return writeQuota(w, until)
	}
	select {
	case rl.slots <- struct{}{}:
		defer func() { <-rl.slots }()
	default:
		w.Header().Set("Retry-After", "5")
		return writeError(w, http.StatusServiceUnavailable, ErrRelayBusy, "busy", nil)
	}

	var body []byte
	if r.Body != nil {
		body, err = io.ReadAll(http.MaxBytesReader(w, r.Body, rl.maxRequestBytes()))
		if err != nil {
			var tooLarge *http.MaxBytesError
			if errors.As(err, &tooLarge) {
				return writeError(w, http.StatusRequestEntityTooLarge, ErrRequestTooLarge, "bad_request", nil)
			}
			if r.Context().Err() != nil {
				return outcome{"client_canceled", 499}
			}
			return writeError(w, http.StatusBadRequest, "bad_request", "bad_request", nil)
		}
	}
	// The server's ReadTimeout bounds the body upload only. net/http
	// already clears the read deadline when the body reaches EOF (its
	// background read); clear it here too so a long stream never depends
	// on that, since an expired deadline would cancel r.Context().
	_ = http.NewResponseController(w).SetReadDeadline(time.Time{})

	tok, failure, ok := rl.token(w, r.Context())
	if !ok {
		return failure
	}
	// The upstream context is cancelled when the actor goes away or the
	// stream idles past StreamIdleTimeout.
	ctx, cancel := context.WithCancel(r.Context())
	defer cancel()
	resp, err := rl.forward(ctx, r, rt, body, tok)
	if err != nil {
		return rl.upstreamFailure(w, r, err)
	}
	if resp.StatusCode == http.StatusUnauthorized {
		// The backend refused the access token. Replace it once, sharing
		// the refresh with concurrent callers, and retry.
		drain(resp)
		retry, err := rl.Tokens.RefreshRejected(r.Context(), tok.AccessToken)
		switch {
		case errors.Is(err, grant.ErrLoginRequired):
			return writeError(w, http.StatusUnauthorized, ErrLoginRequired, "login_required", nil)
		case err != nil:
			w.Header().Set("Retry-After", "60")
			return writeError(w, http.StatusServiceUnavailable, ErrGrantUnavailable, "grant_unavailable", nil)
		}
		if resp, err = rl.forward(ctx, r, rt, body, retry); err != nil {
			return rl.upstreamFailure(w, r, err)
		}
		if resp.StatusCode == http.StatusUnauthorized {
			// A freshly refreshed token refused again is not proof the
			// refresh token is dead; only invalid_grant is.
			drain(resp)
			rl.Logger.Error("codex backend refused a freshly refreshed token", "grant", rl.Tokens.Grant())
			w.Header().Set("Retry-After", "60")
			return writeError(w, http.StatusServiceUnavailable, ErrGrantUnavailable, "grant_unavailable", nil)
		}
	}
	defer resp.Body.Close()
	*upstreamID = resp.Header.Get("X-Request-Id")
	obs, observed := codexchatgpt.ParseQuotaHeaders(resp.Header, resp.StatusCode, rl.now())
	if observed && rl.quotaRec != nil {
		rl.quotaRec.record(obs, rl.now().UTC())
	}
	if resp.StatusCode == http.StatusTooManyRequests {
		errBody, _ := io.ReadAll(io.LimitReader(resp.Body, 64<<10))
		if isUsageLimit(obs, errBody) {
			return writeQuota(w, latestReset(obs))
		}
		resp.Body = io.NopCloser(bytes.NewReader(errBody))
	}
	return rl.stream(w, resp, cancel)
}

func (rl *Relay) upstreamFailure(w http.ResponseWriter, r *http.Request, err error) outcome {
	if r.Context().Err() != nil {
		return outcome{"client_canceled", 499}
	}
	rl.Logger.Error("codex upstream request failed", "err", err)
	return writeError(w, http.StatusBadGateway, ErrUpstreamUnreachable, "upstream_unreachable", nil)
}

// token gets the grant's access token, answering the classified error when
// it cannot.
func (rl *Relay) token(w http.ResponseWriter, ctx context.Context) (grant.Token, outcome, bool) {
	tok, err := rl.Tokens.AccessToken(ctx)
	switch {
	case errors.Is(err, grant.ErrLoginRequired):
		return tok, writeError(w, http.StatusUnauthorized, ErrLoginRequired, "login_required", nil), false
	case err != nil:
		w.Header().Set("Retry-After", "60")
		return tok, writeError(w, http.StatusServiceUnavailable, ErrGrantUnavailable, "grant_unavailable", nil), false
	}
	return tok, outcome{}, true
}

func (rl *Relay) maxRequestBytes() int64 {
	if rl.MaxRequestBytes > 0 {
		return rl.MaxRequestBytes
	}
	return DefaultMaxRequestBytes
}

func (rl *Relay) streamIdleTimeout() time.Duration {
	if rl.StreamIdleTimeout > 0 {
		return rl.StreamIdleTimeout
	}
	return DefaultStreamIdleTimeout
}

func (rl *Relay) forward(ctx context.Context, r *http.Request, rt route, body []byte, tok grant.Token) (*http.Response, error) {
	target := strings.TrimRight(rl.Upstream, "/") + "/" + rt.suffix
	if r.URL.RawQuery != "" {
		target += "?" + r.URL.RawQuery
	}
	var reader io.Reader
	if rt.method == http.MethodPost {
		reader = bytes.NewReader(body)
	}
	req, err := http.NewRequestWithContext(ctx, rt.method, target, reader)
	if err != nil {
		return nil, err
	}
	for name, values := range r.Header {
		if forwardRequestHeader(name) {
			req.Header[http.CanonicalHeaderKey(name)] = append([]string(nil), values...)
		}
	}
	req.Header.Set("Authorization", "Bearer "+tok.AccessToken)
	req.Header.Set("Chatgpt-Account-Id", tok.AccountID)
	client := rl.Client
	if client == nil {
		client = http.DefaultClient
	}
	return client.Do(req)
}

// stream copies the response to the actor, flushing as bytes arrive so SSE
// events are not held back. cancel aborts the upstream request when it
// sends nothing for StreamIdleTimeout.
func (rl *Relay) stream(w http.ResponseWriter, resp *http.Response, cancel context.CancelFunc) outcome {
	idle := rl.streamIdleTimeout()
	timer := time.AfterFunc(idle, cancel)
	defer timer.Stop()
	for name, values := range resp.Header {
		if !hopHeaders[http.CanonicalHeaderKey(name)] {
			w.Header()[name] = append([]string(nil), values...)
		}
	}
	w.WriteHeader(resp.StatusCode)
	rc := http.NewResponseController(w)
	_ = rc.Flush()
	buf := make([]byte, 32<<10)
	for {
		n, err := resp.Body.Read(buf)
		if n > 0 {
			timer.Reset(idle)
			if _, werr := w.Write(buf[:n]); werr != nil {
				return outcome{"client_canceled", resp.StatusCode}
			}
			_ = rc.Flush()
		}
		if err == io.EOF {
			break
		}
		if err != nil {
			rl.Logger.Warn("codex upstream stream ended early", "err", err, "idle_timeout", idle.String())
			return outcome{"upstream_stream_error", resp.StatusCode}
		}
	}
	name := "ok"
	if resp.StatusCode >= 300 {
		name = "upstream_error"
	}
	return outcome{name, resp.StatusCode}
}

// quotaBlock reports whether a current observation shows an exhausted window
// with a known future reset. A rejection without one is left to the backend
// so a stale reading cannot block forever.
func (rl *Relay) quotaBlock(now time.Time) (time.Time, bool) {
	if rl.Quota == nil {
		return time.Time{}, false
	}
	view := rl.Quota.GetGrantAt(rl.Tokens.Grant(), now.UTC())
	if !view.Observed {
		return time.Time{}, false
	}
	var until time.Time
	for _, w := range view.Windows {
		if w.Expired || w.UsedPercent < 100 {
			continue
		}
		if reset, err := time.Parse(time.RFC3339, w.ResetsAt); err == nil && reset.After(until) {
			until = reset
		}
	}
	return until, !until.IsZero()
}

func isUsageLimit(obs quota.Observation, body []byte) bool {
	if obs.ReachedType != "" {
		return true
	}
	var payload struct {
		Error struct {
			Type string `json:"type"`
			Code string `json:"code"`
		} `json:"error"`
	}
	if json.Unmarshal(body, &payload) == nil {
		for _, v := range []string{payload.Error.Type, payload.Error.Code} {
			if v == "usage_limit_reached" || v == "usage_not_included" {
				return true
			}
		}
	}
	return false
}

func latestReset(obs quota.Observation) time.Time {
	var until time.Time
	for _, w := range obs.Windows {
		if reset, err := time.Parse(time.RFC3339, w.ResetsAt); err == nil && w.UsedPercent >= 100 && reset.After(until) {
			until = reset
		}
	}
	return until
}

func writeQuota(w http.ResponseWriter, until time.Time) outcome {
	extra := map[string]string{}
	if !until.IsZero() {
		extra["resets_at"] = until.UTC().Format(time.RFC3339)
		if wait := time.Until(until); wait > 0 {
			w.Header().Set("Retry-After", strconv.Itoa(int(wait.Seconds())+1))
		}
	}
	return writeError(w, http.StatusTooManyRequests, ErrQuotaExhausted, "quota_exhausted", extra)
}

func writeError(w http.ResponseWriter, status int, code, name string, extra map[string]string) outcome {
	body := map[string]string{"factory_error": code}
	for k, v := range extra {
		body[k] = v
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(body)
	return outcome{name, status}
}

func drain(resp *http.Response) {
	_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 64<<10))
	resp.Body.Close()
}

// quotaRecorder commits quota observations off the request path. A
// persistent quota store writes a ConfigMap per commit, so observations are
// coalesced: only the latest pending one is written.
type quotaRecorder struct {
	store  *quota.Store
	grant  string
	logger *slog.Logger

	mu      sync.Mutex
	pending *pendingObservation
	wake    chan struct{}
}

type pendingObservation struct {
	obs        quota.Observation
	receivedAt time.Time
}

func newQuotaRecorder(store *quota.Store, grant string, logger *slog.Logger) *quotaRecorder {
	q := &quotaRecorder{store: store, grant: grant, logger: logger, wake: make(chan struct{}, 1)}
	go q.run()
	return q
}

func (q *quotaRecorder) record(obs quota.Observation, receivedAt time.Time) {
	q.mu.Lock()
	q.pending = &pendingObservation{obs: obs, receivedAt: receivedAt}
	q.mu.Unlock()
	select {
	case q.wake <- struct{}{}:
	default:
	}
}

func (q *quotaRecorder) run() {
	for range q.wake {
		q.mu.Lock()
		next := q.pending
		q.pending = nil
		q.mu.Unlock()
		if next == nil {
			continue
		}
		if err := q.store.PutGrant(q.grant, QuotaProvider, next.obs, next.receivedAt); err != nil {
			q.logger.Warn("codex quota observation not recorded", "err", err)
		}
	}
}
