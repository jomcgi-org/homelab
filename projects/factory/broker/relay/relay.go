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
	ErrNotFound             = "not_found"
)

// QuotaProvider is the quota store's provider key for Codex observations.
const QuotaProvider = "codex"

type Verifier interface {
	Verify(ctx context.Context, raw string) (actorjwt.Identity, error)
}

type TokenSource interface {
	Grant() string
	AccessToken(ctx context.Context) (grant.Token, error)
	ForceRefresh(ctx context.Context) error
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
	Now             func() time.Time
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

// requestHeaders are forwarded to the backend; everything else, including
// the actor's Authorization and any cookies, is dropped.
var requestHeaders = map[string]bool{
	"Accept": true, "Content-Type": true, "Content-Encoding": true, "User-Agent": true,
	"Openai-Beta": true, "Originator": true, "Session_id": true, "Conversation_id": true, "Version": true,
}

func forwardRequestHeader(name string) bool {
	name = http.CanonicalHeaderKey(name)
	return requestHeaders[name] || strings.HasPrefix(name, "X-Codex-") || strings.HasPrefix(name, "X-Openai-")
}

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

	var body []byte
	if r.Body != nil {
		body, err = io.ReadAll(http.MaxBytesReader(w, r.Body, rl.maxRequestBytes()))
		if err != nil {
			var tooLarge *http.MaxBytesError
			if errors.As(err, &tooLarge) {
				return writeError(w, http.StatusRequestEntityTooLarge, ErrRequestTooLarge, "bad_request", nil)
			}
			return writeError(w, http.StatusBadRequest, "bad_request", "bad_request", nil)
		}
	}

	tok, failure, ok := rl.token(w, r.Context())
	if !ok {
		return failure
	}
	resp, err := rl.forward(r, rt, body, tok)
	if err != nil {
		if r.Context().Err() != nil {
			return outcome{"client_canceled", 499}
		}
		rl.Logger.Error("codex upstream request failed", "err", err)
		return writeError(w, http.StatusBadGateway, ErrUpstreamUnreachable, "upstream_unreachable", nil)
	}
	if resp.StatusCode == http.StatusUnauthorized {
		// The backend refused the access token: rotate once and retry. A
		// refusal after that means the grant is dead.
		drain(resp)
		if err := rl.Tokens.ForceRefresh(r.Context()); errors.Is(err, grant.ErrLoginRequired) {
			return writeError(w, http.StatusUnauthorized, ErrLoginRequired, "login_required", nil)
		}
		retry, failure, ok := rl.token(w, r.Context())
		if !ok {
			return failure
		}
		if retry.AccessToken == tok.AccessToken {
			return writeError(w, http.StatusUnauthorized, ErrLoginRequired, "login_required", nil)
		}
		if resp, err = rl.forward(r, rt, body, retry); err != nil {
			return writeError(w, http.StatusBadGateway, ErrUpstreamUnreachable, "upstream_unreachable", nil)
		}
		if resp.StatusCode == http.StatusUnauthorized {
			drain(resp)
			return writeError(w, http.StatusUnauthorized, ErrLoginRequired, "login_required", nil)
		}
	}
	defer resp.Body.Close()
	*upstreamID = resp.Header.Get("X-Request-Id")
	obs, observed := codexchatgpt.ParseQuotaHeaders(resp.Header, resp.StatusCode, rl.now())
	if observed && rl.Quota != nil {
		if err := rl.Quota.PutGrant(rl.Tokens.Grant(), QuotaProvider, obs, rl.now().UTC()); err != nil {
			rl.Logger.Warn("codex quota observation not recorded", "err", err)
		}
	}
	if resp.StatusCode == http.StatusTooManyRequests {
		errBody, _ := io.ReadAll(io.LimitReader(resp.Body, 64<<10))
		if isUsageLimit(obs, errBody) {
			return writeQuota(w, latestReset(obs))
		}
		resp.Body = io.NopCloser(bytes.NewReader(errBody))
	}
	return rl.stream(w, resp)
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
	return 64 << 20
}

func (rl *Relay) forward(r *http.Request, rt route, body []byte, tok grant.Token) (*http.Response, error) {
	target := strings.TrimRight(rl.Upstream, "/") + "/" + rt.suffix
	if r.URL.RawQuery != "" {
		target += "?" + r.URL.RawQuery
	}
	var reader io.Reader
	if rt.method == http.MethodPost {
		reader = bytes.NewReader(body)
	}
	req, err := http.NewRequestWithContext(r.Context(), rt.method, target, reader)
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
// events are not held back.
func (rl *Relay) stream(w http.ResponseWriter, resp *http.Response) outcome {
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
			if _, werr := w.Write(buf[:n]); werr != nil {
				return outcome{"client_canceled", resp.StatusCode}
			}
			_ = rc.Flush()
		}
		if err == io.EOF {
			break
		}
		if err != nil {
			rl.Logger.Warn("codex upstream stream ended early", "err", err)
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
