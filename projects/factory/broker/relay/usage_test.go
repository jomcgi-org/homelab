package relay

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/factory/broker/grant"
	"github.com/jomcgi/homelab/projects/factory/broker/metrics"
	"github.com/jomcgi/homelab/projects/shared/go/tokenbroker/quota"
	"github.com/prometheus/client_golang/prometheus/testutil"
)

const usageToken = "access-secret-1"

var usageT0 = time.Date(2026, 11, 16, 0, 14, 0, 0, time.UTC)

// usageBody is a /wham/usage answer with one primary window.
func usageBody(allowed bool, used int, reset time.Time) string {
	return fmt.Sprintf(`{"rate_limit":{"allowed":%t,"limit_reached":%t,"primary_window":`+
		`{"used_percent":%d,"limit_window_seconds":18000,"reset_at":%d,"reset_after_seconds":3600}}}`,
		allowed, !allowed, used, reset.Unix())
}

// usageBackend fakes chatgpt.com/backend-api/wham/usage.
type usageBackend struct {
	*httptest.Server
	mu          sync.Mutex
	calls       int
	inFlight    int
	maxInFlight int
	status      int
	body        string
	header      http.Header
	path        string
	// gate, when set, holds each response until closed; started receives
	// one value per request.
	gate    chan struct{}
	started chan struct{}
}

func newUsageBackend(t *testing.T) *usageBackend {
	t.Helper()
	b := &usageBackend{status: http.StatusOK}
	b.Server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b.mu.Lock()
		b.calls++
		b.inFlight++
		b.maxInFlight = max(b.maxInFlight, b.inFlight)
		b.header, b.path = r.Header.Clone(), r.URL.Path
		status, body, gate, started := b.status, b.body, b.gate, b.started
		b.mu.Unlock()
		defer func() { b.mu.Lock(); b.inFlight--; b.mu.Unlock() }()
		if started != nil {
			started <- struct{}{}
		}
		if gate != nil {
			<-gate
		}
		w.WriteHeader(status)
		_, _ = io.WriteString(w, body)
	}))
	t.Cleanup(b.Close)
	return b
}

func (b *usageBackend) set(f func(*usageBackend)) { b.mu.Lock(); f(b); b.mu.Unlock() }
func (b *usageBackend) count() int                { b.mu.Lock(); defer b.mu.Unlock(); return b.calls }

type fakeUsageTokens struct{ err error }

func (f *fakeUsageTokens) Grant() string { return "codex-primary" }

func (f *fakeUsageTokens) AccessToken(context.Context) (grant.Token, error) {
	if f.err != nil {
		return grant.Token{}, f.err
	}
	return grant.Token{AccessToken: usageToken, AccountID: "acct-1"}, nil
}

type fakeClock struct {
	mu  sync.Mutex
	now time.Time
}

func (c *fakeClock) Now() time.Time  { c.mu.Lock(); defer c.mu.Unlock(); return c.now }
func (c *fakeClock) Set(t time.Time) { c.mu.Lock(); c.now = t; c.mu.Unlock() }

type usageFixture struct {
	backend   *usageBackend
	tokens    *fakeUsageTokens
	clock     *fakeClock
	quota     *quota.Store
	metrics   *metrics.Metrics
	logs      *bytes.Buffer
	logMu     *sync.Mutex
	refresher *UsageRefresher
}

func newUsageFixture(t *testing.T) *usageFixture {
	t.Helper()
	f := &usageFixture{
		backend: newUsageBackend(t), tokens: &fakeUsageTokens{}, clock: &fakeClock{now: usageT0},
		quota: quota.NewStore(), metrics: metrics.New(), logs: &bytes.Buffer{}, logMu: &sync.Mutex{},
	}
	f.refresher = &UsageRefresher{
		Tokens: f.tokens, Upstream: f.backend.URL + "/backend-api/codex", Quota: f.quota, Metrics: f.metrics,
		Logger: slog.New(slog.NewJSONHandler(lockedWriter{f.logMu, f.logs}, nil)), Now: f.clock.Now,
	}
	return f
}

// seed stores a rejected reading received at the given time whose window
// resets at reset.
func (f *usageFixture) seed(t *testing.T, at, reset time.Time) {
	t.Helper()
	obs := quota.Observation{ObservedAt: at.Format(time.RFC3339), Status: "rejected", ReachedType: "primary", Windows: []quota.Window{{
		Name: "primary", UsedPercent: 100, WindowMinutes: 300, ResetsAt: reset.Format(time.RFC3339),
	}}}
	if err := f.quota.PutGrant("codex-primary", QuotaProvider, obs, at); err != nil {
		t.Fatal(err)
	}
}

func (f *usageFixture) view() quota.View { return f.quota.GetGrantAt("codex-primary", f.clock.Now()) }

func (f *usageFixture) outcomes(outcome string) float64 {
	return testutil.ToFloat64(f.metrics.UsageRefresh.WithLabelValues(outcome))
}

func (f *usageFixture) logText() string {
	f.logMu.Lock()
	defer f.logMu.Unlock()
	return f.logs.String()
}

func TestReadingExpiry(t *testing.T) {
	received := usageT0
	resetAt := func(reset time.Time) []quota.ViewWindow {
		return []quota.ViewWindow{{Name: "primary", UsedPercent: 100, ResetsAt: reset.Format(time.RFC3339)}}
	}
	for name, tc := range map[string]struct {
		view quota.View
		want time.Time
	}{
		"no reading":    {quota.View{}, time.Time{}},
		"no windows":    {quota.View{Observed: true, ReceivedAt: received}, received.Add(UsageMaxAge)},
		"later reset":   {quota.View{Observed: true, ReceivedAt: received, Windows: resetAt(received.Add(96 * time.Hour))}, received.Add(UsageMaxAge)},
		"earlier reset": {quota.View{Observed: true, ReceivedAt: received, Windows: resetAt(received.Add(10 * time.Minute))}, received.Add(10 * time.Minute)},
		"reset already past at receipt": {
			quota.View{Observed: true, ReceivedAt: received, Windows: resetAt(received.Add(-time.Minute))}, received.Add(UsageMaxAge),
		},
	} {
		if got := readingExpiry(tc.view); !got.Equal(tc.want) {
			t.Errorf("%s: expiry = %v, want %v", name, got, tc.want)
		}
	}
}

// With no reading, the first check reads usage with the grant's credentials
// and records it for the grant and the provider.
func TestUsageRefreshReadsWithoutReading(t *testing.T) {
	f := newUsageFixture(t)
	f.backend.set(func(b *usageBackend) { b.body = usageBody(true, 40, usageT0.Add(5*time.Hour)) })
	if got := f.refresher.check(context.Background()); got != "refreshed" {
		t.Fatalf("outcome = %q", got)
	}
	f.backend.mu.Lock()
	header, path := f.backend.header, f.backend.path
	f.backend.mu.Unlock()
	if path != "/backend-api/wham/usage" {
		t.Fatalf("usage path = %q", path)
	}
	if header.Get("Authorization") != "Bearer "+usageToken || header.Get("Chatgpt-Account-Id") != "acct-1" || header.Get("User-Agent") != "codex-cli" {
		t.Fatalf("usage headers = %v", header)
	}
	view := f.view()
	if !view.Observed || view.Status != "allowed" || !view.ReceivedAt.Equal(usageT0) || len(view.Windows) != 1 || view.Windows[0].UsedPercent != 40 {
		t.Fatalf("grant view = %+v", view)
	}
	if provider := f.quota.Get(QuotaProvider); !provider.Observed || provider.Status != "allowed" {
		t.Fatalf("provider view = %+v", provider)
	}
	if got := f.outcomes("refreshed"); got != 1 {
		t.Fatalf("refreshed count = %v", got)
	}
}

// A window that resets inside the reading's 30 minutes ends the reading at
// the reset.
func TestUsageRefreshAtWindowReset(t *testing.T) {
	f := newUsageFixture(t)
	reset := usageT0.Add(10 * time.Minute)
	f.seed(t, usageT0, reset)
	f.backend.set(func(b *usageBackend) { b.body = usageBody(true, 0, usageT0.Add(5*time.Hour)) })
	ctx := context.Background()
	f.clock.Set(reset.Add(-time.Second))
	if got := f.refresher.check(ctx); got != "" || f.backend.count() != 0 {
		t.Fatalf("before reset: outcome %q, reads %d", got, f.backend.count())
	}
	f.clock.Set(reset)
	if got := f.refresher.check(ctx); got != "refreshed" || f.backend.count() != 1 {
		t.Fatalf("at reset: outcome %q, reads %d", got, f.backend.count())
	}
	if view := f.view(); view.Status != "allowed" || !view.ReceivedAt.Equal(reset) {
		t.Fatalf("view after reset = %+v", view)
	}
}

// A failed read keeps the reading, counts its outcome, logs neither token nor
// body, and waits UsageRetryBackoff before reading again.
func TestUsageRefreshFailureKeepsReadingAndBacksOff(t *testing.T) {
	const marker = "body-secret-marker"
	for _, tc := range []struct {
		name, outcome string
		tokenErr      error
		status        int
		body          string
	}{
		{name: "login required", outcome: "login_required", tokenErr: grant.ErrLoginRequired},
		{name: "token error", outcome: "token_error", tokenErr: errors.New("token-error-detail")},
		{name: "unauthorized", outcome: "authorization_error", status: http.StatusUnauthorized, body: marker},
		{name: "forbidden", outcome: "authorization_error", status: http.StatusForbidden, body: marker},
		{name: "server error", outcome: "http_error", status: http.StatusBadGateway, body: marker},
		{name: "undecodable", outcome: "decode_error", status: http.StatusOK, body: `{"detail":"` + marker + `"}`},
	} {
		t.Run(tc.name, func(t *testing.T) {
			f := newUsageFixture(t)
			f.tokens.err = tc.tokenErr
			reads := 0
			if tc.status != 0 {
				reads = 1
				f.backend.set(func(b *usageBackend) { b.status, b.body = tc.status, tc.body })
			}
			f.seed(t, usageT0, usageT0.Add(96*time.Hour))
			ctx := context.Background()
			due := usageT0.Add(UsageMaxAge)
			f.clock.Set(due)
			if got := f.refresher.check(ctx); got != tc.outcome {
				t.Fatalf("outcome = %q, want %q", got, tc.outcome)
			}
			if got := f.backend.count(); got != reads {
				t.Fatalf("reads = %d, want %d", got, reads)
			}
			if view := f.view(); view.Status != "rejected" || !view.ReceivedAt.Equal(usageT0) {
				t.Fatalf("reading replaced: %+v", view)
			}
			if got := f.outcomes(tc.outcome); got != 1 {
				t.Fatalf("%s count = %v", tc.outcome, got)
			}

			f.clock.Set(due.Add(UsageRetryBackoff - time.Second))
			if got := f.refresher.check(ctx); got != "" || f.backend.count() != reads {
				t.Fatalf("during backoff: outcome %q, reads %d", got, f.backend.count())
			}
			f.clock.Set(due.Add(UsageRetryBackoff))
			if got := f.refresher.check(ctx); got != tc.outcome {
				t.Fatalf("after backoff outcome = %q", got)
			}
			if got := f.outcomes(tc.outcome); got != 2 {
				t.Fatalf("%s count after retry = %v", tc.outcome, got)
			}

			logs := f.logText()
			if !strings.Contains(logs, `"outcome":"`+tc.outcome+`"`) {
				t.Fatalf("logs lack the outcome: %s", logs)
			}
			for _, secret := range []string{marker, usageToken, "token-error-detail"} {
				if strings.Contains(logs, secret) {
					t.Fatalf("logs contain %q: %s", secret, logs)
				}
			}
		})
	}
}

// A check while a read is in flight does not start a second one.
func TestUsageRefreshOneReadInFlight(t *testing.T) {
	f := newUsageFixture(t)
	gate, started := make(chan struct{}), make(chan struct{}, 8)
	release := sync.OnceFunc(func() { close(gate) })
	t.Cleanup(release)
	f.backend.set(func(b *usageBackend) {
		b.gate, b.started, b.body = gate, started, usageBody(true, 40, usageT0.Add(5*time.Hour))
	})
	ctx := context.Background()
	done := make(chan string, 1)
	go func() { done <- f.refresher.check(ctx) }()
	select {
	case <-started:
	case <-time.After(5 * time.Second):
		t.Fatal("usage read never started")
	}
	var wg sync.WaitGroup
	for i := 0; i < 4; i++ {
		wg.Go(func() {
			if got := f.refresher.check(ctx); got != "" {
				t.Errorf("check during a read = %q", got)
			}
		})
	}
	wg.Wait()
	release()
	if got := <-done; got != "refreshed" {
		t.Fatalf("outcome = %q", got)
	}
	f.backend.mu.Lock()
	calls, peak := f.backend.calls, f.backend.maxInFlight
	f.backend.mu.Unlock()
	if calls != 1 || peak != 1 {
		t.Fatalf("reads = %d, peak in flight = %d", calls, peak)
	}
}

// An allowed usage reading ends the block a rejected relay reading holds,
// without a relayed probe and before the window's reset.
func TestUsageRefreshEndsRelayBlock(t *testing.T) {
	h := newHarness(t, true)
	var offset atomic.Int64
	clock := func() time.Time { return time.Now().Add(time.Duration(offset.Load())) }
	h.handler.Now = clock
	// Only the usage read may lift this block.
	h.handler.QuotaProbeInterval = 24 * time.Hour
	reset := time.Now().Add(96 * time.Hour).Truncate(time.Second)
	var usageReads atomic.Int32
	h.backend.set(func(b *backend) {
		b.respond = func(w http.ResponseWriter, r *http.Request) {
			switch {
			case r.URL.Path == "/backend-api/wham/usage":
				usageReads.Add(1)
				_, _ = io.WriteString(w, usageBody(true, 40, reset))
			case usageReads.Load() > 0:
				sse(w, nil)
			default:
				quotaHeaders(w, 100, reset)
				w.Header().Set("x-codex-rate-limit-reached-type", "primary")
				w.WriteHeader(http.StatusTooManyRequests)
				_, _ = w.Write([]byte(`{"error":{"type":"usage_limit_reached"}}`))
			}
		}
	})
	status := func() int {
		resp := h.post("/v1/responses", h.actorJWT())
		_, _ = io.ReadAll(resp.Body)
		return resp.StatusCode
	}
	if got := status(); got != http.StatusTooManyRequests {
		t.Fatalf("first status = %d", got)
	}
	h.waitQuota(func(v quota.View) bool { return v.Exhausted })
	refresher := &UsageRefresher{
		Tokens: h.handler.Tokens, Upstream: h.handler.Upstream, Quota: h.quota, Metrics: h.metrics,
		Logger: slog.New(slog.NewJSONHandler(io.Discard, nil)), Now: clock,
	}
	ctx := context.Background()

	// Inside the reading's 30 minutes nothing is read and the block holds.
	offset.Store(int64(10 * time.Minute))
	if got := refresher.check(ctx); got != "" {
		t.Fatalf("early outcome = %q", got)
	}
	if got := status(); got != http.StatusTooManyRequests || h.backend.count() != 1 {
		t.Fatalf("blocked status = %d, backend calls = %d", got, h.backend.count())
	}

	// After them the usage read replaces the reading and the relay calls
	// the backend again.
	offset.Store(int64(31 * time.Minute))
	if got := refresher.check(ctx); got != "refreshed" || usageReads.Load() != 1 {
		t.Fatalf("outcome = %q, usage reads = %d", got, usageReads.Load())
	}
	if view := h.quota.GetGrantAt("codex-primary", clock().UTC()); view.Status != "allowed" || view.Exhausted {
		t.Fatalf("view after usage read = %+v", view)
	}
	if got := status(); got != http.StatusOK || h.backend.count() != 3 {
		t.Fatalf("unblocked status = %d, backend calls = %d", got, h.backend.count())
	}
}

// A relayed response's reading is fresh, so the next usage read waits until
// that reading expires.
func TestUsageRefreshDeferredByRelayedReading(t *testing.T) {
	h := newHarness(t, true)
	var offset atomic.Int64
	clock := func() time.Time { return time.Now().Add(time.Duration(offset.Load())) }
	h.handler.Now = clock
	var usageReads atomic.Int32
	h.backend.set(func(b *backend) {
		b.respond = func(w http.ResponseWriter, r *http.Request) {
			if r.URL.Path == "/backend-api/wham/usage" {
				usageReads.Add(1)
				_, _ = io.WriteString(w, usageBody(true, 10, time.Now().Add(5*time.Hour)))
				return
			}
			// x-codex headers with a window resetting in an hour.
			sse(w, nil)
		}
	})
	refresher := &UsageRefresher{
		Tokens: h.handler.Tokens, Upstream: h.handler.Upstream, Quota: h.quota, Metrics: h.metrics,
		Logger: slog.New(slog.NewJSONHandler(io.Discard, nil)), Now: clock,
	}
	ctx := context.Background()
	if got := refresher.check(ctx); got != "refreshed" {
		t.Fatalf("first outcome = %q", got)
	}
	first := h.quota.GetGrant("codex-primary").ReceivedAt

	offset.Store(int64(20 * time.Minute))
	resp := h.post("/v1/responses", h.actorJWT())
	_, _ = io.ReadAll(resp.Body)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("relay status = %d", resp.StatusCode)
	}
	h.waitQuota(func(v quota.View) bool { return v.ReceivedAt.After(first) })

	// The usage reading alone would be due now.
	offset.Store(int64(31 * time.Minute))
	if got := refresher.check(ctx); got != "" || usageReads.Load() != 1 {
		t.Fatalf("deferred outcome = %q, usage reads = %d", got, usageReads.Load())
	}
	offset.Store(int64(51 * time.Minute))
	if got := refresher.check(ctx); got != "refreshed" || usageReads.Load() != 2 {
		t.Fatalf("expired outcome = %q, usage reads = %d", got, usageReads.Load())
	}
}

// A relayed reading recorded while a usage read is in flight is newer than
// the usage answer, which describes the account from when it was sent.
func TestUsageReadingYieldsToNewerRelayedReading(t *testing.T) {
	f := newUsageFixture(t)
	gate, started := make(chan struct{}), make(chan struct{}, 1)
	release := sync.OnceFunc(func() { close(gate) })
	t.Cleanup(release)
	f.backend.set(func(b *usageBackend) {
		b.gate, b.started, b.body = gate, started, usageBody(false, 100, usageT0.Add(5*time.Hour))
	})
	done := make(chan string, 1)
	go func() { done <- f.refresher.check(context.Background()) }()
	select {
	case <-started:
	case <-time.After(5 * time.Second):
		t.Fatal("usage read never started")
	}
	relayedAt := usageT0.Add(5 * time.Second)
	f.clock.Set(relayedAt)
	relayed := quota.Observation{ObservedAt: relayedAt.Format(time.RFC3339), Status: "allowed", Windows: []quota.Window{{
		Name: "primary", UsedPercent: 40, WindowMinutes: 300, ResetsAt: usageT0.Add(5 * time.Hour).Format(time.RFC3339),
	}}}
	if err := f.quota.PutGrant("codex-primary", QuotaProvider, relayed, relayedAt); err != nil {
		t.Fatal(err)
	}
	f.clock.Set(usageT0.Add(10 * time.Second)) // The usage answer arrives later still.
	release()
	if got := <-done; got != "refreshed" {
		t.Fatalf("outcome = %q", got)
	}
	view := f.view()
	if view.Status != "allowed" || len(view.Windows) != 1 || view.Windows[0].UsedPercent != 40 {
		t.Fatalf("an older usage answer replaced the relayed reading: %+v", view)
	}
}

// flakyPersistence is a quota state ConfigMap whose writes can fail.
type flakyPersistence struct {
	mu   sync.Mutex
	data []byte
	rev  int
	fail bool
}

func (p *flakyPersistence) Load() ([]byte, string, error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.data, fmt.Sprint(p.rev), nil
}

func (p *flakyPersistence) CompareAndSwap(rev string, data []byte) (string, error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	switch {
	case p.fail:
		return "", errors.New("api unavailable")
	case rev != fmt.Sprint(p.rev):
		return "", quota.ErrPersistenceConflict
	}
	p.rev++
	p.data = append([]byte(nil), data...)
	return fmt.Sprint(p.rev), nil
}

// A failed state write makes the store unreadable until a write succeeds, so
// the refresher retries on the next tick rather than after its backoff.
func TestUsageRefreshRetriesFailedStoreWriteNextTick(t *testing.T) {
	f := newUsageFixture(t)
	persistence := &flakyPersistence{fail: true}
	st, err := quota.NewPersistentStore(persistence)
	if err != nil {
		t.Fatal(err)
	}
	f.quota, f.refresher.Quota = st, st
	f.backend.set(func(b *usageBackend) { b.body = usageBody(true, 10, usageT0.Add(5*time.Hour)) })
	ctx := context.Background()
	if got := f.refresher.check(ctx); got != "store_error" {
		t.Fatalf("first outcome = %q", got)
	}
	if f.view().Observed {
		t.Fatalf("a failed write published a reading: %+v", f.view())
	}
	persistence.mu.Lock()
	persistence.fail = false
	persistence.mu.Unlock()
	f.clock.Set(usageT0.Add(UsageTick))
	if got := f.refresher.check(ctx); got != "refreshed" {
		t.Fatalf("next tick outcome = %q, want a retry", got)
	}
	if view := f.view(); !view.Observed || view.Windows[0].UsedPercent != 10 {
		t.Fatalf("retry did not restore the reading: %+v", view)
	}
	if got := f.backend.count(); got != 2 {
		t.Fatalf("usage reads = %d, want 2", got)
	}
}
