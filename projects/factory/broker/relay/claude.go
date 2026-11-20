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

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"

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

	bodyMu   sync.Mutex
	reserved int64
	once     sync.Once
	slots    chan struct{}
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
		c.slots = make(chan struct{}, min(c.MaxConcurrent, DefaultMaxConcurrent))
	})
}

func (c *Claude) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	c.init()
	start := time.Now()
	ctx, span := startRelaySpan(r.Context(), "claude.relay")
	defer span.End()
	span.SetAttributes(attribute.String("quota.state", "unmanaged"))
	var id actorjwt.Identity
	result := c.serve(w, r.WithContext(ctx), &id)
	finishSpan(span, "claude", id, result)
	if c.Metrics != nil {
		c.Metrics.ClaudeRelayRequests.WithLabelValues(result.name, metrics.StatusClass(result.status)).Inc()
	}
	// No headers, body, token, query, or upstream error text is logged.
	logUntraced(span, c.Logger, slog.LevelInfo, "claude relay", "actor", id.Subject, "atespace", id.Atespace, "actor_uid", id.ActorUID,
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
	// inspection; ambiguous model keys are rejected.
	if enc := r.Header.Get("Content-Encoding"); enc != "" && !strings.EqualFold(enc, "identity") {
		return writeError(w, http.StatusUnsupportedMediaType, "bad_request", "bad_request", nil)
	}
	// invariant: FACT-14
	// Reserve before reading. Unknown lengths reserve a full request, and
	// ReadFull below enforces the reservation even for inconsistent lengths.
	size := r.ContentLength
	limit := min(c.MaxRequestBytes, int64(DefaultMaxRequestBytes))
	if size < 0 {
		size = limit
	}
	if size > limit {
		return writeError(w, http.StatusRequestEntityTooLarge, ErrRequestTooLarge, "bad_request", nil)
	}
	c.bodyMu.Lock()
	if size > DefaultMaxRequestBytes-c.reserved {
		c.bodyMu.Unlock()
		w.Header().Set("Retry-After", "5")
		return writeError(w, http.StatusServiceUnavailable, ErrRelayBusy, "busy", nil)
	}
	c.reserved += size
	c.bodyMu.Unlock()
	defer func() { c.bodyMu.Lock(); c.reserved -= size; c.bodyMu.Unlock() }()
	body := make([]byte, size+1)
	n, err := io.ReadFull(r.Body, body)
	if err == nil {
		return writeError(w, http.StatusRequestEntityTooLarge, ErrRequestTooLarge, "bad_request", nil)
	}
	if err != io.EOF && err != io.ErrUnexpectedEOF {
		return writeError(w, http.StatusBadRequest, "bad_request", "bad_request", nil)
	}
	body = body[:n]
	model, denied := claudeRequestModel(body, c.ModelAllowlist[id.Subject])
	if denied {
		return writeError(w, http.StatusForbidden, ErrClaudeModelDenied, "model_denied", nil)
	}
	if model == "" {
		return writeError(w, http.StatusBadRequest, "bad_request", "bad_request", nil)
	}
	if !slices.Contains(c.ModelAllowlist[id.Subject], model) {
		return writeError(w, http.StatusForbidden, ErrClaudeModelDenied, "model_denied", nil)
	}
	trace.SpanFromContext(r.Context()).SetAttributes(attribute.String("gen_ai.request.model", model))
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
	var observer *tokenObserver
	if span := trace.SpanFromContext(r.Context()); span.IsRecording() && resp.StatusCode < 300 {
		observer = &tokenObserver{claudeSpan: span, sse: isSSE(resp.Header.Get("Content-Type"))}
	}
	return streamResponse(w, resp, c.Logger, "claude", c.StreamIdleTimeout, observer)
}

// Walk validated JSON in place so large prompts and wide objects do not
// require a decoded object graph or a second serialized body.
func claudeRequestModel(body []byte, allowed []string) (string, bool) {
	if !json.Valid(body) {
		return "", false
	}
	body = bytes.TrimSpace(body)
	if body[0] != '{' {
		return "", false
	}
	depth := 0
	key, wantModel := false, false
	model := ""
	for i := 0; i < len(body); i++ {
		switch body[i] {
		case '{', '[':
			depth++
			key = depth == 1
		case '}', ']':
			depth--
		case ',':
			key = depth == 1
		case '"':
			start := i
			for i++; body[i] != '"'; i++ {
				if body[i] == '\\' {
					i++
				}
			}
			if depth != 1 {
				continue
			}
			raw := body[start : i+1]
			if key {
				key = false
				// Five JSON-escaped ASCII letters need at most 30 bytes.
				if len(raw) > 32 {
					continue
				}
				var field string
				if json.Unmarshal(raw, &field) == nil && strings.EqualFold(field, "model") {
					if field != "model" {
						return "", false
					}
					if model != "" {
						return "", true
					}
					wantModel = true
				}
			} else if wantModel {
				// Each byte of an allowed UTF-8 name needs at most six
				// bytes as a JSON escape, plus the surrounding quotes.
				longest := 0
				for _, name := range allowed {
					longest = max(longest, len(name))
				}
				if (len(raw)-2+5)/6 > longest {
					return "", true
				}
				if json.Unmarshal(raw, &model) != nil || model == "" {
					return "", false
				}
				wantModel = false
			}
		case ':':
			if wantModel && bytes.TrimLeft(body[i+1:], " \t\r\n")[0] != '"' {
				return "", false
			}
		}
	}
	return model, false
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
