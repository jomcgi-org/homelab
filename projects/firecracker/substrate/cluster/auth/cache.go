package auth

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"sync"
	"time"

	"golang.org/x/sync/singleflight"
)

// defaultReviewTTL bounds how long a successful TokenReview is trusted before it
// is re-checked with the API server. The callers are a small, fixed set of
// ServiceAccounts whose projected tokens rotate hourly, so a short TTL yields a
// near-100% cache hit rate under a saturating invoke load while keeping the
// window in which a just-revoked token still passes small.
const defaultReviewTTL = 60 * time.Second

// maxCacheEntries bounds memory if many distinct tokens are seen (token
// rotation, or a spray of distinct bad tokens). Only SUCCESSFUL reviews are
// cached, so a rejected-token flood cannot grow the map.
const maxCacheEntries = 4096

type cacheEntry struct {
	username string
	expires  time.Time
}

// cachingReviewer memoizes successful TokenReviews keyed by a hash of the token.
// Without it, a saturating invoke rate becomes an equal rate of TokenReview
// calls against the API server, which client-go rate-limits (the default 5 QPS
// bucket was the fc-invoke throughput ceiling) and which needlessly loads the
// control plane. Failures are never cached: a transient API error or a genuine
// rejection must re-check on the next request.
type cachingReviewer struct {
	inner Reviewer
	ttl   time.Duration
	now   func() time.Time // injectable for tests
	group singleflight.Group

	mu      sync.Mutex
	entries map[string]cacheEntry
}

func newCachingReviewer(inner Reviewer, ttl time.Duration) *cachingReviewer {
	return &cachingReviewer{
		inner:   inner,
		ttl:     ttl,
		now:     time.Now,
		entries: make(map[string]cacheEntry),
	}
}

// hashToken avoids holding raw bearer tokens as map keys; the sha256 is
// sufficient to key the cache and never leaves the process.
func hashToken(token string) string {
	sum := sha256.Sum256([]byte(token))
	return hex.EncodeToString(sum[:])
}

func (c *cachingReviewer) lookup(key string, now time.Time) (string, bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	e, ok := c.entries[key]
	if !ok {
		return "", false
	}
	if now.After(e.expires) {
		delete(c.entries, key)
		return "", false
	}
	return e.username, true
}

func (c *cachingReviewer) store(key, username string, now time.Time) {
	c.mu.Lock()
	defer c.mu.Unlock()
	// Bound memory: when at capacity and inserting a new key, sweep expired
	// entries first; if still full, skip the insert (correctness is unaffected,
	// the token is simply re-reviewed next time).
	if _, exists := c.entries[key]; !exists && len(c.entries) >= maxCacheEntries {
		for k, e := range c.entries {
			if now.After(e.expires) {
				delete(c.entries, k)
			}
		}
		if len(c.entries) >= maxCacheEntries {
			return
		}
	}
	c.entries[key] = cacheEntry{username: username, expires: now.Add(c.ttl)}
}

// Review returns a cached username when the token was recently validated,
// otherwise performs one TokenReview via the inner reviewer. A burst of
// concurrent first-time requests for the same token is collapsed into a single
// inner call by singleflight, so a cold cache under load does not stampede the
// API server.
func (c *cachingReviewer) Review(ctx context.Context, token string) (string, error) {
	key := hashToken(token)
	if username, ok := c.lookup(key, c.now()); ok {
		return username, nil
	}
	v, err, _ := c.group.Do(key, func() (any, error) {
		// A concurrent caller may have populated the cache while this call queued
		// on the singleflight lock; re-check before hitting the API server.
		if username, ok := c.lookup(key, c.now()); ok {
			return username, nil
		}
		username, reviewErr := c.inner.Review(ctx, token)
		if reviewErr != nil {
			return "", reviewErr
		}
		c.store(key, username, c.now())
		return username, nil
	})
	if err != nil {
		return "", err
	}
	return v.(string), nil
}
