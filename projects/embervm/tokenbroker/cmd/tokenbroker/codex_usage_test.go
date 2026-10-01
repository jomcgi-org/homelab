package main

import (
	"bytes"
	"context"
	"encoding/base64"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/embervm/tokenbroker/internal/broker"
	"github.com/jomcgi/homelab/projects/embervm/tokenbroker/internal/metrics"
	"github.com/jomcgi/homelab/projects/embervm/tokenbroker/internal/provider"
	"github.com/jomcgi/homelab/projects/embervm/tokenbroker/internal/quota"
	"github.com/jomcgi/homelab/projects/embervm/tokenbroker/internal/store"
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/testutil"
)

func usageTestToken(payload string) string {
	return "header." + base64.RawURLEncoding.EncodeToString([]byte(payload)) + ".signature"
}

const usageClaims = `{"https://api.openai.com/auth":{"chatgpt_account_id":"test-account"}}`

func usageTestPayload(now time.Time) string {
	return fmt.Sprintf(`{"rate_limit":{"allowed":true,"limit_reached":false,"primary_window":{"used_percent":23,"limit_window_seconds":18000,"reset_at":%d,"reset_after_seconds":3600},"secondary_window":{"used_percent":41,"limit_window_seconds":604800,"reset_at":%d,"reset_after_seconds":7200}}}`, now.Add(time.Hour).Unix(), now.Add(2*time.Hour).Unix())
}

func newUsageTest(t *testing.T, status int, body string) (*codexUsageRefresher, *atomic.Int32, *bytes.Buffer) {
	t.Helper()
	var requests atomic.Int32
	token := usageTestToken(usageClaims)
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		requests.Add(1)
		if req.Method != http.MethodGet || req.URL.Path != "/wham/usage" || req.URL.RawQuery != "" {
			t.Errorf("usage request = %s %s", req.Method, req.URL)
		}
		for header, want := range map[string]string{"User-Agent": "codex-cli", "Authorization": "Bearer " + token, "ChatGPT-Account-Id": "test-account"} {
			if got := req.Header.Get(header); got != want {
				t.Errorf("%s header did not match", header)
			}
		}
		w.WriteHeader(status)
		_, _ = io.WriteString(w, body)
	}))
	t.Cleanup(upstream.Close)
	logs := new(bytes.Buffer)
	logger := slog.New(slog.NewTextHandler(logs, nil))
	st := &fakeStore{grants: map[string]store.Grant{}}
	configs := map[string]grantConfig{}
	var brokerConfigs []broker.GrantConfig
	for name, kind := range map[string]string{"codex-b": "codex-chatgpt", "codex-cluster": "codex-chatgpt", "agent-mcp": "authentik", "github": "github"} {
		configs[name] = grantConfig{Name: name, ProviderName: kind}
		brokerConfigs = append(brokerConfigs, broker.GrantConfig{Name: name, ProviderName: kind})
		st.grants[name] = store.Grant{Name: name, ProviderName: kind, LastRefresh: time.Now(), TokenBundle: store.TokenBundle{AccessToken: token, ExpiresAt: time.Now().Add(time.Hour)}}
	}
	m := metrics.New()
	s := &server{store: st, configs: configs, logger: logger, quotaStore: quota.NewStore()}
	s.broker = broker.New(st, map[string]provider.Adapter{"codex-chatgpt": &fakeAdapter{startPanic: "device login forbidden"}}, nil, brokerConfigs, logger, m)
	r := newCodexUsageRefresher(s, 10*time.Minute, upstream.URL+"/", m.CodexUsage)
	now := time.Now().UTC().Truncate(time.Second)
	r.now = func() time.Time { return now }
	return r, &requests, logs
}

func seedUsage(t *testing.T, r *codexUsageRefresher, age time.Duration, status string, percent float64, reset time.Time, withWindow bool) {
	t.Helper()
	observed := r.now().Add(-age)
	obs := quota.Observation{Provider: "codex", ObservedAt: observed.Format(time.RFC3339), Status: status}
	if withWindow {
		obs.Windows = []quota.Window{{Name: "primary", UsedPercent: percent, ResetsAt: reset.UTC().Format(time.RFC3339)}}
	}
	if err := r.server.quotaStore.PutGrant("codex-b", "codex", obs, r.now()); err != nil {
		t.Fatal(err)
	}
}

func TestCodexUsageEligibility(t *testing.T) {
	for _, tc := range []struct {
		name       string
		observed   bool
		age        time.Duration
		status     string
		percent    float64
		resetAfter time.Duration
		window     bool
		want       int32
	}{
		{name: "never-observed", want: 1},
		{name: "stale", observed: true, age: 11 * time.Minute, status: "allowed", window: true, resetAfter: time.Hour, want: 1},
		{name: "fresh", observed: true, age: 9 * time.Minute, status: "allowed", window: true, resetAfter: time.Hour},
		{name: "refresh-age-boundary", observed: true, age: 10 * time.Minute, status: "allowed", window: true, resetAfter: time.Hour},
		{name: "exhausted-future", observed: true, age: 20 * time.Minute, status: "allowed", percent: 100, window: true, resetAfter: time.Hour},
		{name: "gate-threshold", observed: true, age: 20 * time.Minute, status: "allowed", percent: 97, window: true, resetAfter: time.Hour},
		{name: "below-gate-threshold", observed: true, age: 20 * time.Minute, status: "allowed", percent: 96, window: true, resetAfter: time.Hour, want: 1},
		{name: "rejected-future-low-use", observed: true, age: 20 * time.Minute, status: "rejected", percent: 1, window: true, resetAfter: time.Hour},
		{name: "expired-exhausted", observed: true, age: 20 * time.Minute, status: "allowed", percent: 100, window: true, resetAfter: -time.Second, want: 1},
		{name: "rejected-reset-passed", observed: true, age: 11 * time.Minute, status: "rejected", window: true, resetAfter: -time.Second, want: 1},
		{name: "windowless-rejection-young", observed: true, age: 11 * time.Minute, status: "rejected"},
		{name: "windowless-rejection-boundary", observed: true, age: 15 * time.Minute, status: "rejected"},
		{name: "windowless-rejection-old", observed: true, age: 15*time.Minute + time.Second, status: "rejected", want: 1},
		{name: "windowless-allowed", observed: true, age: 11 * time.Minute, status: "allowed", want: 1},
	} {
		t.Run(tc.name, func(t *testing.T) {
			r, requests, _ := newUsageTest(t, 200, usageTestPayload(time.Now()))
			if tc.observed {
				seedUsage(t, r, tc.age, tc.status, tc.percent, r.now().Add(tc.resetAfter), tc.window)
			}
			r.refresh(context.Background(), "codex-b")
			r.refresh(context.Background(), "codex-b")
			if got := requests.Load(); got != tc.want {
				t.Fatalf("requests = %d, want %d", got, tc.want)
			}
			if tc.want == 1 {
				view := r.server.quotaStore.GetGrant("codex-b")
				if !view.Observed || view.AgeSeconds < 0 || view.AgeSeconds > 5 || len(view.Windows) != 2 {
					t.Fatalf("refreshed view = %+v", view)
				}
				for i, want := range []quota.ViewWindow{{Name: "primary", UsedPercent: 23, WindowMinutes: 300}, {Name: "secondary", UsedPercent: 41, WindowMinutes: 10080}} {
					got := view.Windows[i]
					if got.Name != want.Name || got.UsedPercent != want.UsedPercent || got.WindowMinutes != want.WindowMinutes || got.ResetsAt == "" {
						t.Fatalf("window = %+v", got)
					}
				}
			}
		})
	}
}

func TestCodexUsageResumesAfterReset(t *testing.T) {
	r, requests, _ := newUsageTest(t, 200, usageTestPayload(time.Now()))
	now := r.now()
	seedUsage(t, r, 20*time.Minute, "allowed", 97, now.Add(time.Minute), true)
	r.refresh(context.Background(), "codex-b")
	if requests.Load() != 0 {
		t.Fatal("exhausted grant was requested")
	}
	r.now = func() time.Time { return now.Add(time.Minute) }
	r.refresh(context.Background(), "codex-b")
	if requests.Load() != 1 {
		t.Fatal("grant was not refreshed at reset")
	}
}

func TestCodexUsageExpiredPrimaryDoesNotBlockSecondary(t *testing.T) {
	r, requests, _ := newUsageTest(t, 200, usageTestPayload(time.Now()))
	now := r.now()
	obs := quota.Observation{Provider: "codex", ObservedAt: now.Add(-20 * time.Minute).Format(time.RFC3339), Status: "allowed", Windows: []quota.Window{
		{Name: "primary", UsedPercent: 100, ResetsAt: now.Add(time.Minute).Format(time.RFC3339)},
		{Name: "secondary", UsedPercent: 41, ResetsAt: now.Add(2 * time.Hour).Format(time.RFC3339)},
	}}
	if err := r.server.quotaStore.PutGrant("codex-b", "codex", obs, now); err != nil {
		t.Fatal(err)
	}
	r.refresh(context.Background(), "codex-b")
	if requests.Load() != 0 {
		t.Fatal("exhausted primary was requested")
	}
	r.now = func() time.Time { return now.Add(time.Minute) }
	r.refresh(context.Background(), "codex-b")
	if requests.Load() != 1 {
		t.Fatal("expired primary blocked usable secondary")
	}
}

func TestCodexUsageErrorsRecordNothing(t *testing.T) {
	for _, tc := range []struct {
		name          string
		status        int
		body, outcome string
	}{
		{"unauthorized", 401, "token must never be logged", "http_error"},
		{"forbidden", 403, "account must never be logged", "http_error"},
		{"server-error", 500, "upstream failure", "http_error"},
		{"malformed", 200, "{", "decode_error"},
		{"type-error", 200, strings.Replace(usageTestPayload(time.Now()), `"used_percent":23`, `"used_percent":"bad"`, 1), "decode_error"},
		{"allowed-no-windows", 200, `{"rate_limit":{"allowed":true,"limit_reached":false}}`, "decode_error"},
		{"missing-limit", 200, `{}`, "decode_error"},
		{"null-limit", 200, `{"rate_limit":null}`, "decode_error"},
		{"missing-allowed", 200, `{"rate_limit":{"limit_reached":true}}`, "decode_error"},
		{"missing-limit-reached", 200, `{"rate_limit":{"allowed":false}}`, "decode_error"},
		{"trailing-data", 200, usageTestPayload(time.Now()) + ` {}`, "decode_error"},
		{"oversized", 200, strings.Repeat(" ", 64<<10) + usageTestPayload(time.Now()), "decode_error"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			r, requests, logs := newUsageTest(t, tc.status, tc.body)
			now := r.now()
			r.refresh(context.Background(), "codex-b")
			r.refresh(context.Background(), "codex-b")
			if requests.Load() != 1 {
				t.Fatalf("requests = %d", requests.Load())
			}
			if r.server.quotaStore.GetGrant("codex-b").Observed || r.server.quotaStore.Get("codex").Observed || r.server.broker.NeedsLogin("codex-b") {
				t.Fatal("failure changed quota or login state")
			}
			if _, _, err := r.server.broker.GetAccessToken("codex-b", context.Background()); err != nil {
				t.Fatalf("grant no longer usable: %v", err)
			}
			if logs.Len() == 0 || strings.Contains(logs.String(), usageTestToken(usageClaims)) || strings.Contains(logs.String(), "test-account") || strings.Contains(logs.String(), tc.body) {
				t.Fatalf("unsafe or missing logs: %s", logs)
			}
			if testutil.ToFloat64(r.metrics.WithLabelValues("codex-b", tc.outcome)) != 1 {
				t.Fatal("outcome counter missing")
			}
			r.now = func() time.Time { return now.Add(10*time.Minute - time.Second) }
			r.refresh(context.Background(), "codex-b")
			if requests.Load() != 1 {
				t.Fatal("retry before refresh age")
			}
			r.now = func() time.Time { return now.Add(10 * time.Minute) }
			r.refresh(context.Background(), "codex-b")
			if requests.Load() != 2 {
				t.Fatal("retry did not resume")
			}
		})
	}
}

func TestCodexUsageRejectedAndNullWindows(t *testing.T) {
	for _, tc := range []struct {
		name, body, status string
		windows            int
	}{
		{"limit-reached", strings.Replace(usageTestPayload(time.Now()), `"limit_reached":false`, `"limit_reached":true`, 1), "rejected", 2},
		{"not-allowed", strings.Replace(usageTestPayload(time.Now()), `"allowed":true`, `"allowed":false`, 1), "rejected", 2},
		{"rejected-windowless", `{"rate_limit":{"allowed":false,"limit_reached":false,"primary_window":null,"secondary_window":null}}`, "rejected", 0},
		{"null-secondary", strings.Replace(usageTestPayload(time.Now()), `"secondary_window":`, `"unused_window":`, 1), "allowed", 1},
	} {
		t.Run(tc.name, func(t *testing.T) {
			r, _, _ := newUsageTest(t, 200, tc.body)
			r.refresh(context.Background(), "codex-b")
			view := r.server.quotaStore.GetGrant("codex-b")
			if !view.Observed || view.Status != tc.status || len(view.Windows) != tc.windows {
				t.Fatalf("view = %+v", view)
			}
			if testutil.ToFloat64(r.metrics.WithLabelValues("codex-b", "refreshed")) != 1 {
				t.Fatal("refresh counter missing")
			}
		})
	}
}

func TestCodexUsageSkipsOtherProvidersAndNeedsLogin(t *testing.T) {
	r, requests, _ := newUsageTest(t, 200, usageTestPayload(time.Now()))
	st := r.server.store.(*fakeStore)
	grant := st.grants["codex-cluster"]
	grant.TokenBundle.ExpiresAt = time.Now().Add(-time.Hour)
	grant.TokenBundle.RefreshToken = "test-refresh-token"
	st.grants["codex-cluster"] = grant
	r.server.broker = broker.New(st, map[string]provider.Adapter{"codex-chatgpt": &fakeAdapter{refreshErr: provider.ErrInvalidGrant}}, nil, []broker.GrantConfig{{Name: "codex-cluster", ProviderName: "codex-chatgpt"}, {Name: "codex-b", ProviderName: "codex-chatgpt"}, {Name: "agent-mcp", ProviderName: "authentik"}, {Name: "github", ProviderName: "github"}}, r.server.logger, nil)
	_, _, _ = r.server.broker.GetAccessToken("codex-cluster", context.Background())
	if !r.server.broker.NeedsLogin("codex-cluster") {
		t.Fatal("fixture did not require login")
	}
	r.tokens = r.server.broker
	r.tick(context.Background())
	if requests.Load() != 1 || !r.server.quotaStore.GetGrant("codex-b").Observed {
		t.Fatalf("requests = %d", requests.Load())
	}
	for _, name := range []string{"codex-cluster", "agent-mcp", "github"} {
		if r.server.quotaStore.GetGrant(name).Observed {
			t.Fatalf("unexpected observation for %s", name)
		}
	}
	if testutil.ToFloat64(r.metrics.WithLabelValues("codex-cluster", "token_error")) != 0 {
		t.Fatal("needs-login grant tried token path")
	}
}

func TestCodexUsageDisabledAndCancelled(t *testing.T) {
	for _, tc := range []struct {
		name      string
		age       time.Duration
		cancelled bool
	}{
		{"disabled", 0, false}, {"cancelled", time.Minute, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			r, requests, _ := newUsageTest(t, 200, usageTestPayload(time.Now()))
			r.age = tc.age
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			if tc.cancelled {
				cancel()
			}
			done := make(chan struct{})
			go func() { r.run(ctx); close(done) }()
			select {
			case <-done:
			case <-time.After(time.Second):
				t.Fatal("loop did not stop")
			}
			r.tick(ctx)
			if requests.Load() != 0 {
				t.Fatal("disabled or cancelled refresher requested usage")
			}
		})
	}
}

func TestCodexUsageConfiguration(t *testing.T) {
	for _, tc := range []struct {
		value   string
		want    time.Duration
		invalid bool
	}{
		{"", 0, false}, {"0", 0, false}, {"600", 10 * time.Minute, false}, {"-1", 0, true}, {"oops", 0, true}, {"9223372036854775807", 0, true},
	} {
		t.Run("seconds="+tc.value, func(t *testing.T) {
			t.Setenv("TOKENBROKER_CODEX_USAGE_REFRESH_SECONDS", tc.value)
			got, err := configuredCodexUsageRefresh()
			if (err != nil) != tc.invalid || got != tc.want {
				t.Fatalf("config = %s, %v", got, err)
			}
		})
	}
}

func TestCodexUsageStartupRejectsInvalidConfiguration(t *testing.T) {
	t.Setenv("TOKENBROKER_CODEX_USAGE_REFRESH_SECONDS", "invalid")
	err := run(slog.New(slog.NewTextHandler(io.Discard, nil)))
	if err == nil || err.Error() != "invalid TOKENBROKER_CODEX_USAGE_REFRESH_SECONDS" {
		t.Fatalf("startup error = %v", err)
	}
}

func TestCodexUsageListenerShutdown(t *testing.T) {
	for _, cancelled := range []bool{false, true} {
		t.Run(fmt.Sprintf("cancelled=%t", cancelled), func(t *testing.T) {
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			errorsCh := make(chan error, 1)
			var want error
			if cancelled {
				cancel()
			} else {
				want = errors.New("listener stopped")
				errorsCh <- want
			}
			result := make(chan error, 1)
			go func() { result <- waitForListener(ctx, errorsCh) }()
			select {
			case got := <-result:
				if !errors.Is(got, want) {
					t.Fatalf("listener error = %v, want %v", got, want)
				}
			case <-time.After(time.Second):
				t.Fatal("listener wait did not stop")
			}
		})
	}
}

func TestCodexUsageLoopCancelsInFlightRequest(t *testing.T) {
	r, _, _ := newUsageTest(t, 200, usageTestPayload(time.Now()))
	entered := make(chan struct{})
	r.client.Transport = usageRoundTripper(func(req *http.Request) (*http.Response, error) {
		close(entered)
		<-req.Context().Done()
		return nil, req.Context().Err()
	})
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan struct{})
	go func() { r.run(ctx); close(done) }()
	select {
	case <-entered:
	case <-time.After(time.Second):
		t.Fatal("enabled loop did not refresh on startup")
	}
	cancel()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("loop did not cancel its request")
	}
	if r.server.quotaStore.GetGrant("codex-b").Observed {
		t.Fatal("cancelled request recorded quota")
	}
}

func TestCodexUsageAccountClaim(t *testing.T) {
	for _, tc := range []struct {
		name, token string
		valid       bool
	}{
		{"valid", usageTestToken(usageClaims), true},
		{"no-jwt", "plain", false},
		{"bad-base64", "h.%%.s", false},
		{"bad-json", usageTestToken("{"), false},
		{"no-claim", usageTestToken(`{}`), false},
		{"empty-claim", usageTestToken(`{"https://api.openai.com/auth":{"chatgpt_account_id":""}}`), false},
		{"partial-base64", "h." + base64.RawURLEncoding.EncodeToString([]byte(usageClaims)) + "!.s", false},
		{"partial-json", usageTestToken(usageClaims + "junk"), false},
		{"partial-type-error", usageTestToken(`{"https://api.openai.com/auth":{"chatgpt_account_id":"test-account"},"https://api.openai.com/auth":{"chatgpt_account_id":123}}`), false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			_, ok := usageAccountID(tc.token)
			if ok != tc.valid {
				t.Fatalf("valid = %t", ok)
			}
		})
	}
}

type usageCountingTokens struct {
	usageTokenSource
	calls atomic.Int32
}

func (s *usageCountingTokens) GetAccessToken(name string, ctx context.Context) (string, time.Time, error) {
	s.calls.Add(1)
	return s.usageTokenSource.GetAccessToken(name, ctx)
}

func TestCodexUsageCancelledBeforeToken(t *testing.T) {
	r, _, _ := newUsageTest(t, 200, usageTestPayload(time.Now()))
	tokens := &usageCountingTokens{usageTokenSource: r.tokens}
	r.tokens = tokens
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	r.tick(ctx)
	if tokens.calls.Load() != 0 {
		t.Fatal("cancelled tick entered token path")
	}
}

type usageFailedPersistence struct{}

func (usageFailedPersistence) Load() ([]byte, string, error) { return nil, "", nil }
func (usageFailedPersistence) CompareAndSwap(string, []byte) (string, error) {
	return "", errors.New("persistence unavailable")
}

func TestCodexUsagePersistenceFailure(t *testing.T) {
	r, requests, _ := newUsageTest(t, 200, usageTestPayload(time.Now()))
	st, err := quota.NewPersistentStore(usageFailedPersistence{})
	if err != nil {
		t.Fatal(err)
	}
	r.server.quotaStore = st
	r.refresh(context.Background(), "codex-b")
	r.refresh(context.Background(), "codex-b")
	if requests.Load() != 1 || st.GetGrant("codex-b").Observed || st.Get("codex").Observed {
		t.Fatal("failed persistence exposed quota or retried early")
	}
	if testutil.ToFloat64(r.metrics.WithLabelValues("codex-b", "refreshed")) != 0 {
		t.Fatal("failed persistence counted as refreshed")
	}
}

func TestCodexUsageWindowValidation(t *testing.T) {
	for _, field := range []string{"used_percent", "limit_window_seconds", "reset_at", "reset_after_seconds"} {
		t.Run("missing-"+field, func(t *testing.T) {
			body := strings.Replace(usageTestPayload(time.Now()), `"`+field+`":`, `"ignored":`, 1)
			if _, err := decodeCodexUsage(strings.NewReader(body), time.Now().UTC()); err == nil {
				t.Fatal("incomplete window accepted")
			}
		})
	}
	t.Run("invalid-observation", func(t *testing.T) {
		body := strings.Replace(usageTestPayload(time.Now()), `"limit_window_seconds":18000`, `"limit_window_seconds":-18000`, 1)
		if _, err := decodeCodexUsage(strings.NewReader(body), time.Now().UTC()); err == nil {
			t.Fatal("invalid observation accepted")
		}
	})
}

type usageTokenFailure struct{}

func (usageTokenFailure) NeedsLogin(string) bool { return false }
func (usageTokenFailure) GetAccessToken(string, context.Context) (string, time.Time, error) {
	return usageTestToken(usageClaims), time.Time{}, errors.New("secret token error")
}

func TestCodexUsageTokenErrors(t *testing.T) {
	for _, tc := range []string{"broker-error", "invalid-claim"} {
		t.Run(tc, func(t *testing.T) {
			r, requests, logs := newUsageTest(t, 200, usageTestPayload(time.Now()))
			if tc == "broker-error" {
				r.tokens = usageTokenFailure{}
			} else {
				st := r.server.store.(*fakeStore)
				grant := st.grants["codex-b"]
				grant.TokenBundle.AccessToken = usageTestToken(`{}`)
				st.grants["codex-b"] = grant
			}
			r.refresh(context.Background(), "codex-b")
			r.refresh(context.Background(), "codex-b")
			if requests.Load() != 0 || r.server.quotaStore.GetGrant("codex-b").Observed || r.server.broker.NeedsLogin("codex-b") {
				t.Fatal("token error changed state or sent usage")
			}
			if strings.Contains(logs.String(), "secret token error") {
				t.Fatal("token error leaked")
			}
			if testutil.ToFloat64(r.metrics.WithLabelValues("codex-b", "token_error")) != 1 {
				t.Fatal("token error counter missing")
			}
		})
	}
}

func TestCodexUsageInFlightAndCompletionCooldown(t *testing.T) {
	r, _, _ := newUsageTest(t, 200, usageTestPayload(time.Now()))
	entered, release := make(chan struct{}), make(chan struct{})
	var requests atomic.Int32
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		if req.Method != "GET" || req.URL.Path != "/wham/usage" || req.Header.Get("User-Agent") != "codex-cli" || req.Header.Get("ChatGPT-Account-Id") != "test-account" || req.Header.Get("Authorization") != "Bearer "+usageTestToken(usageClaims) {
			t.Error("wrong usage request")
		}
		if requests.Add(1) == 1 {
			close(entered)
			<-release
		}
		w.WriteHeader(500)
	}))
	defer upstream.Close()
	r.base = upstream.URL
	start := r.now()
	var offset atomic.Int64
	r.now = func() time.Time { return start.Add(time.Duration(offset.Load())) }
	done := make(chan struct{})
	go func() { r.refresh(context.Background(), "codex-b"); close(done) }()
	<-entered
	r.refresh(context.Background(), "codex-b")
	if requests.Load() != 1 {
		t.Error("overlapping request")
	}
	offset.Store(int64(time.Minute))
	close(release)
	<-done
	offset.Store(int64(10 * time.Minute))
	r.refresh(context.Background(), "codex-b")
	if requests.Load() != 1 {
		t.Fatal("cooldown was measured from start instead of failure")
	}
	offset.Store(int64(11 * time.Minute))
	r.refresh(context.Background(), "codex-b")
	if requests.Load() != 2 {
		t.Fatal("cooldown did not end")
	}
}

func TestCodexUsageHTTPGuards(t *testing.T) {
	t.Run("redirect", func(t *testing.T) {
		r, requests, _ := newUsageTest(t, 200, usageTestPayload(time.Now()))
		destination := r.base + "/wham/usage"
		redirect := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
			if req.Method != "GET" || req.URL.Path != "/wham/usage" || req.Header.Get("User-Agent") != "codex-cli" || req.Header.Get("Authorization") != "Bearer "+usageTestToken(usageClaims) || req.Header.Get("ChatGPT-Account-Id") != "test-account" {
				t.Error("wrong redirect-source request")
			}
			http.Redirect(w, req, destination, 302)
		}))
		defer redirect.Close()
		r.base = redirect.URL
		r.refresh(context.Background(), "codex-b")
		if requests.Load() != 0 || r.server.quotaStore.GetGrant("codex-b").Observed {
			t.Fatal("redirect followed")
		}
	})
	t.Run("invalid-url", func(t *testing.T) {
		r, requests, _ := newUsageTest(t, 200, usageTestPayload(time.Now()))
		r.base = "://bad"
		r.refresh(context.Background(), "codex-b")
		if requests.Load() != 0 || testutil.ToFloat64(r.metrics.WithLabelValues("codex-b", "http_error")) != 1 {
			t.Fatal("invalid URL not rejected")
		}
	})
	t.Run("transport-error", func(t *testing.T) {
		r, _, _ := newUsageTest(t, 200, usageTestPayload(time.Now()))
		r.base = "http://127.0.0.1:0"
		r.refresh(context.Background(), "codex-b")
		if r.server.quotaStore.GetGrant("codex-b").Observed || testutil.ToFloat64(r.metrics.WithLabelValues("codex-b", "http_error")) != 1 {
			t.Fatal("transport error not rejected")
		}
	})
	t.Run("timeouts", func(t *testing.T) {
		r, _, _ := newUsageTest(t, 200, usageTestPayload(time.Now()))
		r.tokens = usageDeadlineTokens{usageTokenSource: r.tokens, t: t}
		if codexUsageTick != time.Minute {
			t.Fatal("usage tick changed")
		}
		if r.client.Timeout != 10*time.Second {
			t.Fatal("HTTP timeout changed")
		}
		r.client.Transport = usageRoundTripper(func(req *http.Request) (*http.Response, error) {
			deadline, ok := req.Context().Deadline()
			if !ok || time.Until(deadline) > 10*time.Second {
				t.Fatal("request timeout missing")
			}
			return nil, errors.New("timeout")
		})
		r.refresh(context.Background(), "codex-b")
	})
	// Register includes the usage counter in the broker's registry.
	t.Run("metric-registration", func(t *testing.T) {
		m := metrics.New()
		registry := prometheus.NewRegistry()
		m.Register(registry)
		m.CodexUsage.WithLabelValues("codex-b", "refreshed").Inc()
		families, err := registry.Gather()
		if err != nil {
			t.Fatal(err)
		}
		for _, family := range families {
			if family.GetName() == "tokenbroker_codex_usage_total" {
				return
			}
		}
		t.Fatal("usage counter not registered")
	})
}

type usageRoundTripper func(*http.Request) (*http.Response, error)

func (f usageRoundTripper) RoundTrip(req *http.Request) (*http.Response, error) { return f(req) }

type usageDeadlineTokens struct {
	usageTokenSource
	t *testing.T
}

func (s usageDeadlineTokens) GetAccessToken(name string, ctx context.Context) (string, time.Time, error) {
	deadline, ok := ctx.Deadline()
	if !ok || time.Until(deadline) > 10*time.Second {
		s.t.Fatal("token request context has no usage deadline")
	}
	return s.usageTokenSource.GetAccessToken(name, ctx)
}

func TestCodexUsageLatestWins(t *testing.T) {
	r, _, _ := newUsageTest(t, 200, usageTestPayload(time.Now()))
	now := r.now()
	newer := quota.Observation{Provider: "codex", ObservedAt: now.Format(time.RFC3339), Status: "allowed", Windows: []quota.Window{{Name: "primary", UsedPercent: 4}}}
	if err := r.server.recordQuota("codex", "codex-cluster", newer, now); err != nil {
		t.Fatal(err)
	}
	r.now = func() time.Time { return now.Add(-time.Minute) }
	r.refresh(context.Background(), "codex-b")
	if !r.server.quotaStore.GetGrant("codex-b").Observed || r.server.quotaStore.Get("codex").Windows[0].UsedPercent != 4 {
		t.Fatal("grant report replaced newer provider view")
	}
}
