// Package relay forwards Codex Responses and Claude Messages API calls from
// kagent actors with credentials held by the broker. The actor authenticates with
// the Substrate actor JWT the egress gateway injects; provider credentials
// never leave the broker.
package relay

import (
	"bytes"
	"compress/gzip"
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"slices"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/klauspost/compress/zstd"

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
	// DefaultQuotaProbeInterval is how long a usage-limit refusal blocks
	// before one request goes upstream to see whether the limit has lifted.
	DefaultQuotaProbeInterval = 30 * time.Minute
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
	Verifier   Verifier
	Tokens     TokenSource
	TokenUsage TokenRecorder
	// Upstream is the Codex backend prefix, such as
	// https://chatgpt.com/backend-api/codex.
	Upstream        string
	Client          *http.Client
	Quota           *quota.Store
	Metrics         *metrics.Metrics
	Logger          *slog.Logger
	MaxRequestBytes int64
	// StreamIdleTimeout cancels an upstream response that sends nothing
	// for this long. It also caps the total time spent reading each 401
	// or 429 body, even if bytes keep arriving.
	StreamIdleTimeout time.Duration
	// MaxConcurrent caps requests in flight to the backend.
	MaxConcurrent         int
	MaxConcurrentPerActor int
	AllowedModels         []string
	// ReasoningEffort, when set, replaces reasoning.effort in every
	// /responses request (withEffort).
	ReasoningEffort string
	// QuotaProbeInterval lets one request through a usage-limit block this
	// often, so a reset redeemed before the window's own reset is noticed.
	QuotaProbeInterval time.Duration
	Now                func() time.Time

	actorMu      sync.Mutex
	activeActors map[string]int
	initOnce     sync.Once
	slots        chan struct{}
	quotaRec     *quotaRecorder
	lastProbe    atomic.Int64 // unix nanoseconds of the last probe let through
}

func (rl *Relay) init() {
	rl.initOnce.Do(func() {
		n := rl.MaxConcurrent
		if n <= 0 {
			n = DefaultMaxConcurrent
		}
		rl.activeActors = make(map[string]int)
		if rl.MaxConcurrentPerActor <= 0 {
			rl.MaxConcurrentPerActor = 8
		}
		rl.slots = make(chan struct{}, n)
		if rl.Quota != nil {
			rl.quotaRec = newQuotaRecorder(rl.Quota, rl.Tokens.Grant(), rl.Logger)
		}
	})
}

func (rl *Relay) acquireActor(subject string) bool {
	rl.actorMu.Lock()
	defer rl.actorMu.Unlock()
	if rl.activeActors[subject] >= rl.MaxConcurrentPerActor {
		return false
	}
	rl.activeActors[subject]++
	return true
}

func (rl *Relay) releaseActor(subject string) {
	rl.actorMu.Lock()
	defer rl.actorMu.Unlock()
	rl.activeActors[subject]--
	if rl.activeActors[subject] == 0 {
		delete(rl.activeActors, subject)
	}
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
	if !rl.acquireActor(identity.Subject) {
		w.Header().Set("Retry-After", "5")
		return writeError(w, http.StatusTooManyRequests, "relay_actor_busy", "actor_busy", nil)
	}
	defer rl.releaseActor(identity.Subject)
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
	if rt.method == http.MethodPost && len(rl.AllowedModels) > 0 {
		decoded, err := modelBody(body, strings.Join(r.Header.Values("Content-Encoding"), ","), rl.maxRequestBytes())
		if errors.Is(err, errDecodedTooLarge) || errors.Is(err, zstd.ErrWindowSizeExceeded) || errors.Is(err, zstd.ErrDecoderSizeExceeded) {
			return writeError(w, http.StatusRequestEntityTooLarge, ErrRequestTooLarge, "bad_request", nil)
		}
		var fields map[string]json.RawMessage
		var model string
		if err != nil || json.Unmarshal(decoded, &fields) != nil || json.Unmarshal(fields["model"], &model) != nil || model == "" || !slices.Contains(rl.AllowedModels, model) {
			return writeError(w, http.StatusBadRequest, "model_not_allowed", "model_not_allowed", nil)
		}
	}
	if rt.suffix == "responses" {
		body = rl.withEffort(r, body)
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
	var observer *tokenObserver
	if rt.suffix == "responses" || rt.suffix == "responses/compact" {
		observer = rl.observeTokens(r.Context(), identity)
	}
	resp, err := rl.forward(r.Context(), r, rt, body, tok)
	if err != nil {
		return rl.upstreamFailure(w, r, err)
	}
	if resp.StatusCode == http.StatusUnauthorized {
		// The backend refused the access token. Replace it once, sharing
		// the refresh with concurrent callers, and retry.
		drain(resp)
		if err := r.Context().Err(); err != nil {
			return rl.upstreamFailure(w, r, err)
		}
		retry, err := rl.Tokens.RefreshRejected(r.Context(), tok.AccessToken)
		switch {
		case errors.Is(err, grant.ErrLoginRequired):
			return writeError(w, http.StatusUnauthorized, ErrLoginRequired, "login_required", nil)
		case err != nil:
			w.Header().Set("Retry-After", "60")
			return writeError(w, http.StatusServiceUnavailable, ErrGrantUnavailable, "grant_unavailable", nil)
		}
		if resp, err = rl.forward(r.Context(), r, rt, body, retry); err != nil {
			return rl.upstreamFailure(w, r, err)
		}
		if resp.StatusCode == http.StatusUnauthorized {
			// A freshly refreshed token refused again is not proof the
			// refresh token is dead; only invalid_grant is.
			drain(resp)
			if err := r.Context().Err(); err != nil {
				return rl.upstreamFailure(w, r, err)
			}
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
		errBody, err := io.ReadAll(io.LimitReader(resp.Body, 64<<10))
		if r.Context().Err() != nil {
			return rl.upstreamFailure(w, r, r.Context().Err())
		}
		if isUsageLimit(obs, errBody) {
			return writeQuota(w, latestReset(obs))
		}
		if err != nil {
			return rl.upstreamFailure(w, r, err)
		}
		resp.Body = io.NopCloser(bytes.NewReader(errBody))
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		observer = nil
	}
	return rl.stream(w, resp, observer)
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

var errDecodedTooLarge = errors.New("decoded request exceeds size limit")

// modelBody decodes a copy for inspection. The upstream receives the wire body.
func modelBody(body []byte, encoding string, limit int64) ([]byte, error) {
	var reader io.Reader
	switch strings.ToLower(strings.TrimSpace(encoding)) {
	case "", "identity":
		return body, nil
	case "gzip":
		decoder, err := gzip.NewReader(bytes.NewReader(body))
		if err != nil {
			return nil, err
		}
		defer decoder.Close()
		reader = decoder
	case "zstd":
		decoder, err := zstd.NewReader(bytes.NewReader(body), zstd.WithDecoderConcurrency(1), zstd.WithDecoderMaxMemory(uint64(max(limit, 1024))), zstd.WithDecodeBuffersBelow(0))
		if err != nil {
			return nil, err
		}
		defer decoder.Close()
		reader = decoder
	default:
		return nil, errors.New("unsupported content encoding")
	}
	decoded, err := io.ReadAll(io.LimitReader(reader, limit))
	if err != nil {
		return nil, err
	}
	if int64(len(decoded)) == limit {
		if n, err := io.CopyN(io.Discard, reader, 1); n != 0 {
			return nil, errDecodedTooLarge
		} else if err != io.EOF {
			return nil, err
		}
	}
	return decoded, nil
}

// withEffort sets reasoning.effort to ReasoningEffort in a /responses
// request, keeping the request's other fields and reasoning settings.
// kagent's Codex harness configures no effort, so without this every turn
// runs at the model's default. A compressed or non-object body is forwarded
// as sent.
func (rl *Relay) withEffort(r *http.Request, body []byte) []byte {
	if rl.ReasoningEffort == "" {
		return body
	}
	if enc := r.Header.Get("Content-Encoding"); enc != "" && !strings.EqualFold(enc, "identity") {
		return body
	}
	var req map[string]json.RawMessage
	if err := json.Unmarshal(body, &req); err != nil || req == nil {
		return body
	}
	reasoning := map[string]json.RawMessage{}
	if raw, ok := req["reasoning"]; ok {
		// null or a non-object reasoning is replaced.
		_ = json.Unmarshal(raw, &reasoning)
		if reasoning == nil {
			reasoning = map[string]json.RawMessage{}
		}
	}
	effort, _ := json.Marshal(rl.ReasoningEffort)
	reasoning["effort"] = effort
	encoded, err := json.Marshal(reasoning)
	if err != nil {
		return body
	}
	req["reasoning"] = encoded
	out, err := json.Marshal(req)
	if err != nil {
		return body
	}
	return out
}

func (rl *Relay) forward(ctx context.Context, r *http.Request, rt route, body []byte, tok grant.Token) (*http.Response, error) {
	// Each attempt owns its cancellation. A timed-out 401 drain must not
	// cancel token refresh or the next attempt.
	ctx, cancel := context.WithCancel(ctx)
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
		cancel()
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
	resp, err := client.Do(req)
	if err != nil {
		cancel()
		return nil, err
	}
	idle := rl.streamIdleTimeout()
	readerBody := &responseBody{
		ReadCloser: resp.Body,
		cancel:     cancel,
		timer:      time.AfterFunc(idle, cancel),
		idle:       idle,
	}
	if resp.StatusCode == http.StatusUnauthorized || resp.StatusCode == http.StatusTooManyRequests {
		// These bodies are drained or buffered with a 64 KiB limit. Give
		// them a fixed time budget too, including a trickling error body.
		readerBody.idle = 0
	}
	resp.Body = readerBody
	return resp, nil
}

// responseBody starts its watchdog at response headers, before any read.
// A zero idle keeps the fixed deadline; otherwise progress resets it.
type responseBody struct {
	io.ReadCloser
	cancel context.CancelFunc
	timer  *time.Timer
	idle   time.Duration
}

func (b *responseBody) Read(p []byte) (int, error) {
	n, err := b.ReadCloser.Read(p)
	if n > 0 && b.idle > 0 {
		b.timer.Reset(b.idle)
	}
	if err != nil {
		b.timer.Stop()
	}
	return n, err
}

func (b *responseBody) Close() error {
	b.timer.Stop()
	b.cancel()
	return b.ReadCloser.Close()
}

// stream copies the response to the actor, flushing as bytes arrive so SSE
// events are not held back. The response body owns the idle watchdog.
func (rl *Relay) stream(w http.ResponseWriter, resp *http.Response, observer *tokenObserver) outcome {
	if observer != nil {
		observer.sse = isSSE(resp.Header.Get("Content-Type"))
		defer func() {
			if !observer.recorded {
				rl.Metrics.TokenAccounting.WithLabelValues("missing").Inc()
				// Metadata only: why no completion was read, never the body.
				rl.Logger.Info("codex usage not observed",
					"content_type", resp.Header.Get("Content-Type"),
					"sse", observer.sse, "events", observer.events, "last_event", observer.lastType)
			}
		}()
	}
	return streamResponse(w, resp, rl.Logger, "codex", rl.streamIdleTimeout(), observer)
}

func streamResponse(w http.ResponseWriter, resp *http.Response, logger *slog.Logger, provider string, idle time.Duration, observer *tokenObserver) outcome {
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
			if observer != nil {
				observer.write(buf[:n])
			}
			if _, werr := w.Write(buf[:n]); werr != nil {
				return outcome{"client_canceled", resp.StatusCode}
			}
			_ = rc.Flush()
		}
		if err == io.EOF {
			if observer != nil {
				observer.finish()
			}
			break
		}
		if err != nil {
			logger.Warn("upstream stream ended early", "provider", provider, "idle_timeout", idle.String())
			return outcome{"upstream_stream_error", resp.StatusCode}
		}
	}
	name := "ok"
	if resp.StatusCode >= 300 {
		name = "upstream_error"
	}
	return outcome{name, resp.StatusCode}
}

// quotaBlock reports whether the backend's latest answer refused the grant
// for its usage limit, with a full window and a known future reset. A reading
// that shows a full window while the backend still allows requests does not
// block: the backend decides, and a redeemed reset can lift the limit before
// the window resets. A rejection without a reset is left to the backend so a
// stale reading cannot block forever. While blocked, one request per
// QuotaProbeInterval goes upstream, and its answer replaces the reading. An
// unreadable store, as after a failed restore, blocks nothing: a refused call
// costs nothing.
func (rl *Relay) quotaBlock(now time.Time) (time.Time, bool) {
	until, view := rl.quotaRefusal(now)
	if until.IsZero() || rl.claimProbe(now, view.ReceivedAt) {
		return time.Time{}, false
	}
	return until, true
}

// quotaRefusal is quotaBlock without the probe: the reset of the latest full
// window in a rejected reading, zero when the reading refuses nothing.
func (rl *Relay) quotaRefusal(now time.Time) (time.Time, quota.View) {
	if rl.Quota == nil {
		return time.Time{}, quota.View{}
	}
	view := rl.Quota.GetGrantAt(rl.Tokens.Grant(), now.UTC())
	if !view.Observed || view.Status != "rejected" {
		return time.Time{}, view
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
	return until, view
}

// QuotaStatus is the connector's quota read (HOME-127). A quota failure
// pauses the grant's work in the connector, and the connector resumes it
// once a reading received after the pause no longer refuses calls.
type QuotaStatus struct {
	// Exhausted is the relay's pre-check without its probe.
	Exhausted bool   `json:"exhausted"`
	ResetsAt  string `json:"resets_at,omitempty"`
	// ReceivedAt is when the broker received the reading, empty before
	// the first one.
	ReceivedAt string `json:"received_at,omitempty"`
}

// QuotaHandler serves QuotaStatus. It reads the stored reading only: it
// never calls the backend or claims the relay's probe.
func (rl *Relay) QuotaHandler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		until, view := rl.quotaRefusal(rl.now())
		var out QuotaStatus
		if !until.IsZero() {
			out.Exhausted, out.ResetsAt = true, until.UTC().Format(time.RFC3339)
		}
		if view.Observed && !view.ReceivedAt.IsZero() {
			out.ReceivedAt = view.ReceivedAt.UTC().Format(time.RFC3339Nano)
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(out)
	})
}

// claimProbe reports whether this request is the one probe allowed through a
// block whose reading, or last probe, is at least QuotaProbeInterval old.
func (rl *Relay) claimProbe(now, observed time.Time) bool {
	interval := rl.QuotaProbeInterval
	if interval <= 0 {
		interval = DefaultQuotaProbeInterval
	}
	last := rl.lastProbe.Load()
	since := observed
	if last != 0 && time.Unix(0, last).After(since) {
		since = time.Unix(0, last)
	}
	if now.Sub(since) < interval {
		return false
	}
	return rl.lastProbe.CompareAndSwap(last, now.UnixNano())
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

// writeQuota refuses a call while the grant is exhausted. It sends no
// Retry-After: Codex sleeps until that deadline, and a window reset is days
// away, so a turn would hold its slot until then. Without the header, Codex
// 0.161.0 does not retry the 429 and ends the turn with "exceeded retry
// limit, last status: 429 Too Many Requests", which the connector classes as
// quota and pauses. The connector's classify.go cites the Codex and kagent
// source.
func writeQuota(w http.ResponseWriter, until time.Time) outcome {
	extra := map[string]string{}
	if !until.IsZero() {
		extra["resets_at"] = until.UTC().Format(time.RFC3339)
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
