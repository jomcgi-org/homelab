package auth

import (
	"context"
	"errors"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// countingReviewer records how many times the inner Review is invoked and can
// be told to fail or to block until released (for the singleflight test).
type countingReviewer struct {
	calls   atomic.Int64
	user    string
	err     error
	release chan struct{} // if non-nil, Review blocks until closed
}

func (c *countingReviewer) Review(_ context.Context, _ string) (string, error) {
	c.calls.Add(1)
	if c.release != nil {
		<-c.release
	}
	return c.user, c.err
}

func TestCachingReviewer_HitAvoidsSecondCall(t *testing.T) {
	inner := &countingReviewer{user: "system:serviceaccount:monolith:app"}
	c := newCachingReviewer(inner, time.Minute)

	for i := 0; i < 5; i++ {
		got, err := c.Review(context.Background(), "tok")
		if err != nil {
			t.Fatalf("Review: %v", err)
		}
		if got != inner.user {
			t.Fatalf("username = %q, want %q", got, inner.user)
		}
	}
	if n := inner.calls.Load(); n != 1 {
		t.Fatalf("inner called %d times, want 1 (cache should serve the rest)", n)
	}
}

func TestCachingReviewer_TTLExpiryRereviews(t *testing.T) {
	inner := &countingReviewer{user: "u"}
	c := newCachingReviewer(inner, time.Minute)
	base := time.Unix(1_000_000, 0)
	c.now = func() time.Time { return base }

	if _, err := c.Review(context.Background(), "tok"); err != nil {
		t.Fatal(err)
	}
	// Still within TTL: served from cache.
	c.now = func() time.Time { return base.Add(59 * time.Second) }
	if _, err := c.Review(context.Background(), "tok"); err != nil {
		t.Fatal(err)
	}
	if n := inner.calls.Load(); n != 1 {
		t.Fatalf("within TTL inner called %d times, want 1", n)
	}
	// Past TTL: re-reviews.
	c.now = func() time.Time { return base.Add(61 * time.Second) }
	if _, err := c.Review(context.Background(), "tok"); err != nil {
		t.Fatal(err)
	}
	if n := inner.calls.Load(); n != 2 {
		t.Fatalf("after TTL inner called %d times, want 2", n)
	}
}

func TestCachingReviewer_FailuresNotCached(t *testing.T) {
	inner := &countingReviewer{err: errors.New("token not authenticated")}
	c := newCachingReviewer(inner, time.Minute)

	for i := 0; i < 3; i++ {
		if _, err := c.Review(context.Background(), "bad"); err == nil {
			t.Fatal("expected error")
		}
	}
	if n := inner.calls.Load(); n != 3 {
		t.Fatalf("inner called %d times, want 3 (failures must not be cached)", n)
	}
}

func TestCachingReviewer_SingleflightCollapsesConcurrent(t *testing.T) {
	inner := &countingReviewer{user: "u", release: make(chan struct{})}
	c := newCachingReviewer(inner, time.Minute)

	const n = 20
	var wg sync.WaitGroup
	wg.Add(n)
	for i := 0; i < n; i++ {
		go func() {
			defer wg.Done()
			_, _ = c.Review(context.Background(), "tok")
		}()
	}
	// Give the goroutines time to funnel into singleflight, then release.
	time.Sleep(20 * time.Millisecond)
	close(inner.release)
	wg.Wait()

	if got := inner.calls.Load(); got != 1 {
		t.Fatalf("inner called %d times for %d concurrent same-token requests, want 1", got, n)
	}
}
