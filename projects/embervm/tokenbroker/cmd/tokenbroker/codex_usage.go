package main

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/jomcgi/homelab/projects/embervm/tokenbroker/internal/quota"
	"github.com/prometheus/client_golang/prometheus"
)

const (
	codexUsageTick             = time.Minute
	codexUsageTimeout          = 10 * time.Second
	codexRejectionStaleAfter   = 15 * time.Minute
	codexUsageExhaustedPercent = 97
)

type usageTokenSource interface {
	NeedsLogin(string) bool
	GetAccessToken(string, context.Context) (string, time.Time, error)
}

type usageAttempt struct {
	inFlight bool
	next     time.Time
}

type codexUsageRefresher struct {
	server   *server
	tokens   usageTokenSource
	age      time.Duration
	base     string
	client   *http.Client
	now      func() time.Time
	metrics  *prometheus.CounterVec
	mu       sync.Mutex
	attempts map[string]usageAttempt
}

func configuredCodexUsageRefresh() (time.Duration, error) {
	seconds, err := strconv.ParseInt(env("TOKENBROKER_CODEX_USAGE_REFRESH_SECONDS", "0"), 10, 64)
	if err != nil || seconds < 0 || seconds > int64((1<<63-1)/time.Second) {
		return 0, errors.New("invalid TOKENBROKER_CODEX_USAGE_REFRESH_SECONDS")
	}
	return time.Duration(seconds) * time.Second, nil
}

func newCodexUsageRefresher(s *server, age time.Duration, base string, counter *prometheus.CounterVec) *codexUsageRefresher {
	return &codexUsageRefresher{
		server: s, tokens: s.broker, age: age, base: strings.TrimRight(base, "/"),
		client: &http.Client{Timeout: codexUsageTimeout, CheckRedirect: func(*http.Request, []*http.Request) error {
			return http.ErrUseLastResponse
		}},
		now: time.Now, metrics: counter, attempts: make(map[string]usageAttempt),
	}
}

func (r *codexUsageRefresher) run(ctx context.Context) {
	if r.age <= 0 {
		return
	}
	ticker := time.NewTicker(codexUsageTick)
	defer ticker.Stop()
	r.tick(ctx)
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			r.tick(ctx)
		}
	}
}

func (r *codexUsageRefresher) tick(ctx context.Context) {
	if r.age <= 0 {
		return
	}
	names := make([]string, 0, len(r.server.configs))
	for name, config := range r.server.configs {
		if config.ProviderName == "codex-chatgpt" {
			names = append(names, name)
		}
	}
	sort.Strings(names)
	for _, name := range names {
		if ctx.Err() != nil {
			return
		}
		r.refresh(ctx, name)
	}
}

func usageExhausted(view quota.View, now time.Time) bool {
	for _, window := range view.Windows {
		reset, err := time.Parse(time.RFC3339, window.ResetsAt)
		if err == nil && reset.After(now) && (view.Exhausted || window.UsedPercent >= codexUsageExhaustedPercent) {
			return true
		}
	}
	return len(view.Windows) == 0 && view.Status == "rejected" && view.AgeSeconds <= codexRejectionStaleAfter.Seconds()
}

func (r *codexUsageRefresher) refresh(ctx context.Context, name string) {
	if r.tokens.NeedsLogin(name) {
		return
	}
	now := r.now().UTC()
	view := r.server.quotaStore.GetGrantAt(name, now)
	if usageExhausted(view, now) {
		r.outcome(name, "skipped_exhausted")
		return
	}
	if view.Observed && view.AgeSeconds <= r.age.Seconds() {
		return
	}
	r.mu.Lock()
	attempt := r.attempts[name]
	if attempt.inFlight || now.Before(attempt.next) {
		r.mu.Unlock()
		return
	}
	r.attempts[name] = usageAttempt{inFlight: true}
	r.mu.Unlock()
	defer func() {
		r.mu.Lock()
		r.attempts[name] = usageAttempt{next: r.now().Add(r.age)}
		r.mu.Unlock()
	}()

	requestCtx, cancel := context.WithTimeout(ctx, codexUsageTimeout)
	defer cancel()
	token, _, err := r.tokens.GetAccessToken(name, requestCtx)
	accountID, ok := usageAccountID(token)
	if err != nil || !ok {
		r.outcome(name, "token_error")
		return
	}
	req, err := http.NewRequestWithContext(requestCtx, http.MethodGet, r.base+"/wham/usage", nil)
	if err != nil {
		r.outcome(name, "http_error")
		return
	}
	req.Header.Set("User-Agent", "codex-cli")
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("ChatGPT-Account-Id", accountID)
	resp, err := r.client.Do(req)
	if err != nil {
		r.outcome(name, "http_error")
		return
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		r.outcome(name, "http_error")
		return
	}
	receivedAt := r.now().UTC()
	obs, err := decodeCodexUsage(resp.Body, receivedAt)
	if err != nil {
		r.outcome(name, "decode_error")
		return
	}
	if err := r.server.recordQuota("codex", name, obs, receivedAt); err == nil {
		r.outcome(name, "refreshed")
	}
}

func (r *codexUsageRefresher) outcome(grant, outcome string) {
	r.metrics.WithLabelValues(grant, outcome).Inc()
	if outcome != "refreshed" && outcome != "skipped_exhausted" {
		// HTTP errors and bodies can contain credentials, so only log the outcome.
		r.server.logger.Warn("tokenbroker Codex usage refresh failed", "grant", grant, "outcome", outcome)
	}
}

func usageAccountID(token string) (string, bool) {
	parts := strings.Split(token, ".")
	if len(parts) != 3 {
		return "", false
	}
	payload, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil {
		return "", false
	}
	var claims struct {
		Auth struct {
			AccountID string `json:"chatgpt_account_id"`
		} `json:"https://api.openai.com/auth"`
	}
	if json.Unmarshal(payload, &claims) != nil || claims.Auth.AccountID == "" {
		return "", false
	}
	return claims.Auth.AccountID, true
}

type codexUsageWindow struct {
	UsedPercent       *int   `json:"used_percent"`
	WindowSeconds     *int   `json:"limit_window_seconds"`
	ResetAt           *int64 `json:"reset_at"`
	ResetAfterSeconds *int   `json:"reset_after_seconds"`
}

func decodeCodexUsage(body io.Reader, now time.Time) (quota.Observation, error) {
	var payload struct {
		RateLimit *struct {
			Allowed      *bool             `json:"allowed"`
			LimitReached *bool             `json:"limit_reached"`
			Primary      *codexUsageWindow `json:"primary_window"`
			Secondary    *codexUsageWindow `json:"secondary_window"`
		} `json:"rate_limit"`
	}
	decoder := json.NewDecoder(io.LimitReader(body, 64<<10))
	if err := decoder.Decode(&payload); err != nil {
		return quota.Observation{}, err
	}
	if err := decoder.Decode(&struct{}{}); err != io.EOF {
		return quota.Observation{}, errors.New("trailing usage data")
	}
	limit := payload.RateLimit
	if limit == nil || limit.Allowed == nil || limit.LimitReached == nil {
		return quota.Observation{}, errors.New("missing rate limit details")
	}
	obs := quota.Observation{Provider: "codex", ObservedAt: now.UTC().Format(time.RFC3339), Status: "allowed"}
	if *limit.LimitReached || !*limit.Allowed {
		obs.Status = "rejected"
	}
	for _, entry := range []struct {
		name   string
		window *codexUsageWindow
	}{{"primary", limit.Primary}, {"secondary", limit.Secondary}} {
		window := entry.window
		if window == nil {
			continue
		}
		if window.UsedPercent == nil || window.WindowSeconds == nil || window.ResetAt == nil || window.ResetAfterSeconds == nil {
			return quota.Observation{}, fmt.Errorf("incomplete %s usage window", entry.name)
		}
		obs.Windows = append(obs.Windows, quota.Window{
			Name: entry.name, UsedPercent: float64(*window.UsedPercent), WindowMinutes: *window.WindowSeconds / 60,
			ResetsAt: time.Unix(*window.ResetAt, 0).UTC().Format(time.RFC3339),
		})
	}
	if obs.Status == "allowed" && len(obs.Windows) == 0 {
		return quota.Observation{}, errors.New("allowed usage has no windows")
	}
	if !quota.ValidObservation(obs, now.UTC()) {
		return quota.Observation{}, errors.New("invalid usage observation")
	}
	return obs, nil
}
