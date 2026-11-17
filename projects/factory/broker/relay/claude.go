package relay

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"os"
	"slices"
	"strings"
	"sync"
	"time"

	"github.com/jomcgi/homelab/projects/factory/broker/actorjwt"
	"github.com/jomcgi/homelab/projects/factory/broker/auth"
	"github.com/jomcgi/homelab/projects/factory/broker/metrics"
)

const (
	ClaudeUpstream               = "https://api.anthropic.com"
	ClaudeOAuthBeta              = "oauth-2025-04-20"
	ErrClaudeClientRequired      = "claude_code_required"
	ErrClaudeModelDenied         = "claude_model_denied"
	ErrClaudeLoginRequired       = "claude_login_required"
	ErrClaudeTokenUnavailable    = "claude_token_unavailable"
	ErrClaudeUpstream            = "claude_upstream_error"
	ErrClaudeUpstreamUnreachable = "claude_upstream_unreachable"
)

// Claude relays only Claude Code Messages calls. ModelAllowlist keys are exact
// verified JWT subjects (actor/<atespace>/<actorName>); an absent actor or model
// is denied. TokenFile is the broker-only 1Password Secret's token projection.
// It is read for each permitted call so Secret rotation needs no restart.
type Claude struct {
	Verifier          Verifier
	TokenFile         string
	ModelAllowlist    map[string][]string
	Upstream          string
	Client            *http.Client
	Metrics           *metrics.Metrics
	Logger            *slog.Logger
	MaxRequestBytes   int64
	MaxConcurrent     int
	StreamIdleTimeout time.Duration

	once  sync.Once
	slots chan struct{}
}

func (c *Claude) init() {
	c.once.Do(func() {
		if c.Logger == nil {
			c.Logger = slog.Default()
		}
		if c.MaxConcurrent <= 0 {
			c.MaxConcurrent = DefaultMaxConcurrent
		}
		if c.MaxRequestBytes <= 0 {
			c.MaxRequestBytes = DefaultMaxRequestBytes
		}
		if c.StreamIdleTimeout <= 0 {
			c.StreamIdleTimeout = DefaultStreamIdleTimeout
		}
		c.slots = make(chan struct{}, c.MaxConcurrent)
	})
}

func (c *Claude) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	c.init()
	start := time.Now()
	var id actorjwt.Identity
	result := c.serve(w, r, &id)
	if c.Metrics != nil {
		c.Metrics.ClaudeRelayRequests.WithLabelValues(result.name, metrics.StatusClass(result.status)).Inc()
	}
	// No headers, body, token, query, or upstream error text is logged.
	c.Logger.Info("claude relay", "actor", id.Subject, "atespace", id.Atespace, "actor_uid", id.ActorUID,
		"path", r.URL.Path, "status", result.status, "outcome", result.name,
		"duration_ms", time.Since(start).Milliseconds())
}

func (c *Claude) serve(w http.ResponseWriter, r *http.Request, id *actorjwt.Identity) outcome {
	if r.Method != http.MethodPost || (r.URL.Path != "/anthropic/v1/messages" && r.URL.Path != "/anthropic/v1/messages/count_tokens") {
		return writeError(w, http.StatusNotFound, ErrNotFound, "not_found", nil)
	}
	raw, ok := claudeActorToken(r)
	var err error
	if ok {
		*id, err = c.Verifier.Verify(r.Context(), raw)
	}
	if !ok || err != nil {
		if c.Metrics != nil {
			reason := actorjwt.ReasonMalformed
			var jwtErr *actorjwt.Error
			if errors.As(err, &jwtErr) {
				reason = jwtErr.Reason
			}
			c.Metrics.ActorRejections.WithLabelValues(reason).Inc()
		}
		return writeError(w, http.StatusUnauthorized, ErrActorUnauthenticated, "actor_unauthenticated", nil)
	}
	// These markers restrict accidental use by other SDKs. Authorization comes
	// from the verified subject's allowlist, never from client-supplied markers.
	if !strings.HasPrefix(r.UserAgent(), "claude-cli/") || r.Header.Get("X-App") != "cli" {
		return writeError(w, http.StatusForbidden, ErrClaudeClientRequired, "client_denied", nil)
	}
	if len(c.ModelAllowlist[id.Subject]) == 0 {
		return writeError(w, http.StatusForbidden, ErrClaudeModelDenied, "model_denied", nil)
	}
	select {
	case c.slots <- struct{}{}:
		defer func() { <-c.slots }()
	default:
		w.Header().Set("Retry-After", "5")
		return writeError(w, http.StatusServiceUnavailable, ErrRelayBusy, "busy", nil)
	}
	// Inspect the exact model sent upstream. Compressed input cannot bypass
	// inspection; re-encoding JSON removes ambiguous duplicate object keys.
	if enc := r.Header.Get("Content-Encoding"); enc != "" && !strings.EqualFold(enc, "identity") {
		return writeError(w, http.StatusUnsupportedMediaType, "bad_request", "bad_request", nil)
	}
	body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, c.MaxRequestBytes))
	if err != nil {
		var tooLarge *http.MaxBytesError
		if errors.As(err, &tooLarge) {
			return writeError(w, http.StatusRequestEntityTooLarge, ErrRequestTooLarge, "bad_request", nil)
		}
		return writeError(w, http.StatusBadRequest, "bad_request", "bad_request", nil)
	}
	var payload map[string]json.RawMessage
	var model string
	if json.Unmarshal(body, &payload) != nil || json.Unmarshal(payload["model"], &model) != nil || model == "" {
		return writeError(w, http.StatusBadRequest, "bad_request", "bad_request", nil)
	}
	for key := range payload {
		if key != "model" && strings.EqualFold(key, "model") {
			return writeError(w, http.StatusBadRequest, "bad_request", "bad_request", nil)
		}
	}
	if !slices.Contains(c.ModelAllowlist[id.Subject], model) {
		return writeError(w, http.StatusForbidden, ErrClaudeModelDenied, "model_denied", nil)
	}
	body, _ = json.Marshal(payload)
	_ = http.NewResponseController(w).SetReadDeadline(time.Time{})

	token, err := os.ReadFile(c.TokenFile)
	credential := strings.TrimSpace(string(token))
	if err != nil || credential == "" || strings.ContainsAny(credential, " \t\r\n") {
		return writeError(w, http.StatusServiceUnavailable, ErrClaudeTokenUnavailable, "token_unavailable", nil)
	}
	resp, err := c.forward(r, body, credential)
	if err != nil {
		if r.Context().Err() != nil {
			return outcome{"client_canceled", 499}
		}
		return writeError(w, http.StatusBadGateway, ErrClaudeUpstreamUnreachable, "upstream_unreachable", nil)
	}
	defer resp.Body.Close()
	if resp.StatusCode == http.StatusUnauthorized {
		// A setup-token has no broker refresh flow. Never retry or expose the
		// upstream error body; the operator replaces the 1Password token.
		return writeError(w, http.StatusUnauthorized, ErrClaudeLoginRequired, "login_required", nil)
	}
	if resp.StatusCode >= 300 && resp.StatusCode != http.StatusTooManyRequests {
		status := resp.StatusCode
		if status < 400 {
			status = http.StatusBadGateway
		}
		return writeError(w, status, ErrClaudeUpstream, "upstream_error", nil)
	}
	// A 429 retains its status, body and Retry-After. No quota reads or retry
	// policy run here. Copy only response metadata needed by Claude Code.
	for name := range resp.Header {
		switch http.CanonicalHeaderKey(name) {
		case "Content-Type", "Retry-After", "Request-Id", "X-Request-Id":
		default:
			delete(resp.Header, name)
		}
	}
	return streamResponse(w, resp, c.Logger, "claude", c.StreamIdleTimeout, nil)
}

// kagent v1.0.1 injects Anthropic actorJwt into x-api-key without a prefix.
// Also accept the broker's Bearer form, but never two credential headers.
// Neither form accepts provider keys: both go through the actor JWT verifier.
func claudeActorToken(r *http.Request) (string, bool) {
	keys, bearer := r.Header.Values("X-Api-Key"), r.Header.Values("Authorization")
	if len(keys) == 1 && len(bearer) == 0 {
		return keys[0], keys[0] != ""
	}
	if len(keys) == 0 && len(bearer) == 1 {
		return auth.BearerToken(r)
	}
	return "", false
}

func (c *Claude) forward(r *http.Request, body []byte, token string) (*http.Response, error) {
	ctx, cancel := context.WithCancel(r.Context())
	target := strings.TrimRight(c.Upstream, "/") + strings.TrimPrefix(r.URL.Path, "/anthropic")
	if r.URL.RawQuery != "" {
		target += "?" + r.URL.RawQuery
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, target, bytes.NewReader(body))
	if err != nil {
		cancel()
		return nil, err
	}
	for _, name := range []string{"Accept", "User-Agent", "X-App", "Anthropic-Version", "Anthropic-Beta"} {
		if values := r.Header.Values(name); len(values) > 0 {
			req.Header[name] = append([]string(nil), values...)
		}
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+token)
	if req.Header.Get("Anthropic-Version") == "" {
		req.Header.Set("Anthropic-Version", "2023-06-01")
	}
	betas := strings.Join(req.Header.Values("Anthropic-Beta"), ",")
	if !slices.Contains(strings.Split(strings.ReplaceAll(betas, " ", ""), ","), ClaudeOAuthBeta) {
		betas = strings.Trim(betas+","+ClaudeOAuthBeta, ",")
	}
	req.Header.Set("Anthropic-Beta", betas)
	client := http.DefaultClient
	if c.Client != nil {
		client = c.Client
	}
	// Never redirect the broker's credential, including to a subdomain.
	copyClient := *client
	copyClient.CheckRedirect = func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }
	resp, err := copyClient.Do(req)
	if err != nil {
		cancel()
		return nil, err
	}
	resp.Body = &responseBody{
		ReadCloser: resp.Body, cancel: cancel,
		timer: time.AfterFunc(c.StreamIdleTimeout, cancel), idle: c.StreamIdleTimeout,
	}
	return resp, nil
}
