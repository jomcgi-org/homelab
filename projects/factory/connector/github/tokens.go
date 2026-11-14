package github

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"time"
)

// Bearer supplies the projected ServiceAccount token the broker reviews.
type Bearer interface {
	Token() (string, error)
}

// RefreshBefore is how long before expiry a cached installation token is
// replaced. The broker also hands out only tokens with more life than this.
const RefreshBefore = 15 * time.Minute

// BrokerTokens fetches the connector's installation tokens from the broker's
// GET /github/connector-token, one repository each, and caches each until
// RefreshBefore its expiry.
type BrokerTokens struct {
	// URL is the broker's base URL.
	URL    string
	Bearer Bearer
	HTTP   *http.Client
	Now    func() time.Time

	mu      sync.Mutex
	cache   map[string]cachedToken
	fetches map[string]*sync.Mutex // one fetch per repository at a time
}

type cachedToken struct {
	token     string
	expiresAt time.Time
}

// Token returns a token for repository (owner/name). Concurrent callers for
// one repository wait for a single fetch; other repositories and cached
// reads are not held up by it.
func (b *BrokerTokens) Token(ctx context.Context, repository string) (string, error) {
	key := strings.ToLower(repository)
	if t, ok := b.cached(key); ok {
		return t, nil
	}
	b.mu.Lock()
	if b.fetches == nil {
		b.fetches = map[string]*sync.Mutex{}
	}
	f := b.fetches[key]
	if f == nil {
		f = &sync.Mutex{}
		b.fetches[key] = f
	}
	b.mu.Unlock()
	f.Lock()
	defer f.Unlock()
	if t, ok := b.cached(key); ok {
		return t, nil // another caller fetched it meanwhile
	}
	t, err := b.fetch(ctx, repository)
	if err != nil {
		return "", err
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.cache == nil {
		b.cache = map[string]cachedToken{}
	}
	b.cache[key] = t
	return t.token, nil
}

func (b *BrokerTokens) cached(key string) (string, bool) {
	now := time.Now
	if b.Now != nil {
		now = b.Now
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	if t, ok := b.cache[key]; ok && t.expiresAt.Sub(now()) > RefreshBefore {
		return t.token, true
	}
	return "", false
}

// Invalidate drops a cached token GitHub refused.
func (b *BrokerTokens) Invalidate(repository string) {
	b.mu.Lock()
	defer b.mu.Unlock()
	delete(b.cache, strings.ToLower(repository))
}

func (b *BrokerTokens) fetch(ctx context.Context, repository string) (cachedToken, error) {
	bearer, err := b.Bearer.Token()
	if err != nil {
		return cachedToken{}, err
	}
	u := strings.TrimSuffix(b.URL, "/") + "/github/connector-token?" + url.Values{"repository": {repository}}.Encode()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
	if err != nil {
		return cachedToken{}, err
	}
	req.Header.Set("Authorization", "Bearer "+bearer)
	hc := b.HTTP
	if hc == nil {
		hc = &http.Client{Timeout: 30 * time.Second}
	}
	resp, err := hc.Do(req)
	if err != nil {
		return cachedToken{}, fmt.Errorf("broker: %w", err)
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err != nil {
		return cachedToken{}, err
	}
	if resp.StatusCode != http.StatusOK {
		return cachedToken{}, fmt.Errorf("broker: connector token for %s: HTTP %d: %.200s", repository, resp.StatusCode, data)
	}
	var out struct {
		Token      string    `json:"token"`
		ExpiresAt  time.Time `json:"expires_at"`
		Repository string    `json:"repository"`
	}
	if err := json.Unmarshal(data, &out); err != nil {
		return cachedToken{}, fmt.Errorf("broker: decode connector token: %w", err)
	}
	if out.Token == "" || out.ExpiresAt.IsZero() {
		return cachedToken{}, errors.New("broker: connector token without token or expiry")
	}
	if !strings.EqualFold(out.Repository, repository) {
		return cachedToken{}, fmt.Errorf("broker: asked for %s, got a token for %q", repository, out.Repository)
	}
	return cachedToken{token: out.Token, expiresAt: out.ExpiresAt}, nil
}
