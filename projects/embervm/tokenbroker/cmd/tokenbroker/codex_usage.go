package main

import (
	"context"
	"errors"
	"io"
	"net/http"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/jomcgi/homelab/projects/shared/go/tokenbroker/provider/codexchatgpt"
	"github.com/jomcgi/homelab/projects/shared/go/tokenbroker/quota"
	"github.com/prometheus/client_golang/prometheus"
)

const (
	codexUsageTick    = time.Minute
	codexUsageTimeout = 10 * time.Second
)

type usageTokenSource interface {
	NeedsLogin(string) bool
	CachedAccessToken(string, context.Context) (string, time.Time, error)
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

func (r *codexUsageRefresher) refresh(ctx context.Context, name string) {
	if r.tokens.NeedsLogin(name) {
		return
	}
	now := r.now().UTC()
	view := r.server.quotaStore.GetGrantAt(name, now)
	// Exhaustion is quota evidence, not a reason to stop observing. A manual
	// reset can restore capacity before the previously reported reset time.
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
	token, _, err := r.tokens.CachedAccessToken(name, requestCtx)
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
	if resp.StatusCode == http.StatusUnauthorized || resp.StatusCode == http.StatusForbidden {
		r.outcome(name, "authorization_error")
		return
	}
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
	if outcome != "refreshed" {
		// HTTP errors and bodies can contain credentials, so only log the outcome.
		r.server.logger.Warn("tokenbroker Codex usage refresh failed", "grant", grant, "outcome", outcome)
	}
}

func usageAccountID(token string) (string, bool) {
	return codexchatgpt.AccountID(token)
}

func decodeCodexUsage(body io.Reader, now time.Time) (quota.Observation, error) {
	return codexchatgpt.DecodeUsage(body, now)
}
