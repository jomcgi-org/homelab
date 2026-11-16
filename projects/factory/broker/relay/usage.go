package relay

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/jomcgi/homelab/projects/factory/broker/grant"
	"github.com/jomcgi/homelab/projects/factory/broker/metrics"
	"github.com/jomcgi/homelab/projects/shared/go/tokenbroker/provider/codexchatgpt"
	"github.com/jomcgi/homelab/projects/shared/go/tokenbroker/quota"
)

const (
	// UsageMaxAge is the longest a quota reading counts as current.
	UsageMaxAge = 30 * time.Minute
	// UsageTick is how often the refresher looks at the stored reading, so
	// a window reset is noticed within a minute.
	UsageTick = time.Minute
	// UsageRetryBackoff is the wait after a failed usage read.
	UsageRetryBackoff = 5 * time.Minute
	// UsageTimeout bounds one usage read.
	UsageTimeout = 10 * time.Second
)

// UsageTokens supplies the grant's access token.
type UsageTokens interface {
	Grant() string
	AccessToken(ctx context.Context) (grant.Token, error)
}

// UsageRefresher keeps the grant's quota reading current without relay
// traffic. When the stored reading expires it reads the ChatGPT backend's
// usage endpoint and records the answer as the grant's reading, as a relayed
// response's x-codex headers would be.
type UsageRefresher struct {
	Tokens UsageTokens
	// Upstream is the relay's upstream, such as
	// https://chatgpt.com/backend-api/codex; usage is read from
	// https://chatgpt.com/backend-api/wham/usage.
	Upstream string
	// Client defaults to one with UsageTimeout that follows no redirects.
	Client  *http.Client
	Quota   *quota.Store
	Metrics *metrics.Metrics
	Logger  *slog.Logger
	Now     func() time.Time

	mu       sync.Mutex
	inFlight bool
	retryAt  time.Time
}

// Run checks the stored reading every UsageTick until ctx ends.
func (u *UsageRefresher) Run(ctx context.Context) {
	ticker := time.NewTicker(UsageTick)
	defer ticker.Stop()
	for {
		u.check(ctx)
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
	}
}

// readingExpiry is when a reading stops being current: UsageMaxAge after it
// was received, or the first window reset after that moment if sooner. A
// window that had already reset when the reading arrived is ignored. With no
// reading, the zero time has already passed.
func readingExpiry(view quota.View) time.Time {
	if !view.Observed {
		return time.Time{}
	}
	expiry := view.ReceivedAt.Add(UsageMaxAge)
	for _, w := range view.Windows {
		reset, err := time.Parse(time.RFC3339, w.ResetsAt)
		if err == nil && reset.After(view.ReceivedAt) && reset.Before(expiry) {
			expiry = reset
		}
	}
	return expiry
}

// check reads usage when the stored reading has expired and no read is in
// flight or backing off. It returns the read's outcome, or "" when it did not
// read. A relayed reading resets the expiry, so busy traffic defers reads.
func (u *UsageRefresher) check(ctx context.Context) string {
	now := u.now()
	view := u.Quota.GetGrantAt(u.Tokens.Grant(), now.UTC())
	if now.Before(readingExpiry(view)) {
		return ""
	}
	u.mu.Lock()
	if u.inFlight || now.Before(u.retryAt) {
		u.mu.Unlock()
		return ""
	}
	u.inFlight = true
	u.mu.Unlock()

	outcome := u.read(ctx)

	u.mu.Lock()
	u.inFlight = false
	u.retryAt = time.Time{}
	if outcome != "refreshed" {
		u.retryAt = u.now().Add(UsageRetryBackoff)
	}
	u.mu.Unlock()
	if ctx.Err() != nil {
		// Shutdown cut the read short; it says nothing about the backend.
		return ""
	}
	u.Metrics.UsageRefresh.WithLabelValues(outcome).Inc()
	// Error values and bodies can carry credentials, so only the outcome is
	// logged.
	if outcome == "refreshed" {
		u.Logger.Info("codex usage refreshed", "grant", u.Tokens.Grant())
	} else {
		u.Logger.Warn("codex usage refresh failed", "grant", u.Tokens.Grant(), "outcome", outcome)
	}
	return outcome
}

// read fetches and records one usage reading, keeping the stored reading on
// any failure.
func (u *UsageRefresher) read(ctx context.Context) string {
	ctx, cancel := context.WithTimeout(ctx, UsageTimeout)
	defer cancel()
	tok, err := u.Tokens.AccessToken(ctx)
	switch {
	case errors.Is(err, grant.ErrLoginRequired):
		return "login_required"
	case err != nil || tok.AccessToken == "" || tok.AccountID == "":
		return "token_error"
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u.usageURL(), nil)
	if err != nil {
		return "http_error"
	}
	req.Header.Set("Authorization", "Bearer "+tok.AccessToken)
	req.Header.Set("ChatGPT-Account-Id", tok.AccountID)
	req.Header.Set("User-Agent", "codex-cli")
	resp, err := u.client().Do(req)
	if err != nil {
		return "http_error"
	}
	defer drain(resp)
	switch {
	case resp.StatusCode == http.StatusUnauthorized || resp.StatusCode == http.StatusForbidden:
		return "authorization_error"
	case resp.StatusCode != http.StatusOK:
		return "http_error"
	}
	receivedAt := u.now().UTC()
	obs, err := codexchatgpt.DecodeUsage(resp.Body, receivedAt)
	if err != nil {
		return "decode_error"
	}
	if err := u.Quota.PutGrant(u.Tokens.Grant(), QuotaProvider, obs, receivedAt); err != nil {
		return "store_error"
	}
	return "refreshed"
}

// usageURL is the usage endpoint beside the Codex backend prefix.
func (u *UsageRefresher) usageURL() string {
	return strings.TrimSuffix(strings.TrimRight(u.Upstream, "/"), "/codex") + "/wham/usage"
}

func (u *UsageRefresher) client() *http.Client {
	if u.Client != nil {
		return u.Client
	}
	return &http.Client{Timeout: UsageTimeout, CheckRedirect: func(*http.Request, []*http.Request) error {
		return http.ErrUseLastResponse
	}}
}

func (u *UsageRefresher) now() time.Time {
	if u.Now != nil {
		return u.Now()
	}
	return time.Now()
}
