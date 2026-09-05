package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
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

type fakeStore struct {
	mu     sync.Mutex
	grants map[string]store.Grant
	err    error
}

func (s *fakeStore) LoadGrant(name string) (store.Grant, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.grants[name], s.err
}

func (s *fakeStore) SaveGrant(grant store.Grant) error {
	s.mu.Lock()
	s.grants[grant.Name] = grant
	s.mu.Unlock()
	return nil
}

func (s *fakeStore) SaveGrantIfNewer(grant store.Grant) error {
	return s.SaveGrant(grant)
}

type fakeAdapter struct {
	refreshErr  error
	deviceCode  provider.DeviceCodeResponse
	deviceErr   error
	startPanic  any
	startWait   <-chan struct{}
	startCalled chan struct{}
	startOnce   sync.Once
	pollWait    <-chan struct{}
}

func (a *fakeAdapter) StartDeviceFlow(ctx context.Context) (provider.DeviceCodeResponse, error) {
	if a.startPanic != nil {
		panic(a.startPanic)
	}
	if a.startCalled != nil {
		a.startOnce.Do(func() { close(a.startCalled) })
	}
	if a.startWait != nil {
		select {
		case <-a.startWait:
		case <-ctx.Done():
			return provider.DeviceCodeResponse{}, ctx.Err()
		}
	}
	return a.deviceCode, a.deviceErr
}

func (a *fakeAdapter) PollForAuthorization(ctx context.Context, _ provider.DeviceCodeResponse) (provider.AuthorizationCodeResponse, error) {
	if a.pollWait == nil {
		return provider.AuthorizationCodeResponse{}, errors.New("poll stopped")
	}
	select {
	case <-a.pollWait:
		return provider.AuthorizationCodeResponse{}, errors.New("poll stopped")
	case <-ctx.Done():
		return provider.AuthorizationCodeResponse{}, ctx.Err()
	}
}

func (a *fakeAdapter) ExchangeCode(context.Context, provider.AuthorizationCodeResponse) (provider.TokenResponse, error) {
	return provider.TokenResponse{}, errors.New("unexpected exchange")
}

func (a *fakeAdapter) RefreshToken(context.Context, string) (provider.TokenResponse, error) {
	if a.refreshErr != nil {
		return provider.TokenResponse{}, a.refreshErr
	}
	return provider.TokenResponse{AccessToken: "secret-access-token", RefreshToken: "rotated-refresh-token", ExpiresAt: time.Now().Add(time.Hour)}, nil
}

func newTestServer(st *fakeStore, adapter *fakeAdapter) *server {
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	adapters := map[string]provider.Adapter{"test": adapter}
	configs := map[string]grantConfig{"codex-cluster": {Name: "codex-cluster", ProviderName: "test"}}
	b := broker.New(st, adapters, nil, []broker.GrantConfig{{Name: "codex-cluster", ProviderName: "test"}}, logger, nil)
	return &server{
		broker: b, store: st, adapters: adapters, configs: configs, logger: logger,
		startWaitTimeout: loginStartWaitTimeout, quotaStore: quota.NewStore(),
		quotaProviders:     map[string]struct{}{"codex": {}, "claude": {}},
		quotaProviderOrder: []string{"codex", "claude"},
	}
}

func requestGrant(t *testing.T, s *server, method, path string) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest(method, path, nil)
	recorder := httptest.NewRecorder()
	s.grants(recorder, req)
	return recorder
}

func decodeBody(t *testing.T, recorder *httptest.ResponseRecorder) map[string]any {
	t.Helper()
	var body map[string]any
	if err := json.Unmarshal(recorder.Body.Bytes(), &body); err != nil {
		t.Fatal(err)
	}
	return body
}

type signalingContext struct {
	context.Context
	entered chan time.Time
	once    sync.Once
}

func (c *signalingContext) Done() <-chan struct{} {
	c.once.Do(func() {
		c.entered <- time.Now()
		close(c.entered)
	})
	return c.Context.Done()
}

func requestGrantAsync(s *server, ctx context.Context) <-chan *httptest.ResponseRecorder {
	result := make(chan *httptest.ResponseRecorder, 1)
	go func() {
		req := httptest.NewRequest(http.MethodPost, "/grants/codex-cluster/login/start", nil).WithContext(ctx)
		recorder := httptest.NewRecorder()
		s.grants(recorder, req)
		result <- recorder
	}()
	return result
}

func receiveWithin[T any](t *testing.T, ch <-chan T, description string) T {
	t.Helper()
	select {
	case value := <-ch:
		return value
	case <-time.After(time.Second):
		t.Fatalf("timed out waiting for %s", description)
		var zero T
		return zero
	}
}

func quotaTestServer(providers ...string) (*server, *prometheus.Registry) {
	providerSet := make(map[string]struct{}, len(providers))
	for _, provider := range providers {
		providerSet[provider] = struct{}{}
	}
	quotaStore := quota.NewStore()
	registry := prometheus.NewRegistry()
	registry.MustRegister(metrics.NewQuotaCollector(quotaStore, providers))
	return &server{
		logger: slog.New(slog.NewTextHandler(io.Discard, nil)), quotaStore: quotaStore,
		quotaProviders: providerSet, quotaProviderOrder: providers,
	}, registry
}

func requestQuota(t *testing.T, s *server, method, path, body string) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest(method, path, strings.NewReader(body))
	recorder := httptest.NewRecorder()
	s.quota(recorder, req)
	return recorder
}

func TestQuotaPostThenGetRoundTrip(t *testing.T) {
	s, _ := quotaTestServer("codex", "claude")
	body := `{"provider":"wrong-body-value","observed_at":"2026-09-05T18:10:00Z","status":"allowed","reached_type":"","windows":[{"name":"primary","used_percent":24,"window_minutes":10080,"resets_at":"2026-09-12T10:07:08Z"}]}`
	posted := requestQuota(t, s, http.MethodPost, "/quota/codex", body)
	if posted.Code != http.StatusNoContent || posted.Body.Len() != 0 {
		t.Fatalf("POST = %d %s", posted.Code, posted.Body.String())
	}
	got := requestQuota(t, s, http.MethodGet, "/quota/codex", "")
	if got.Code != http.StatusOK {
		t.Fatalf("GET = %d %s", got.Code, got.Body.String())
	}
	decoded := decodeBody(t, got)
	if decoded["provider"] != "codex" || decoded["observed"] != true || decoded["status"] != "allowed" || decoded["exhausted"] != false {
		t.Fatalf("view = %#v", decoded)
	}
}

func TestQuotaHandlerRejectsUnknownProviderBadJSONAndEmptyObservation(t *testing.T) {
	s, _ := quotaTestServer("codex", "claude")
	tests := []struct {
		name, path, body string
		want             int
	}{
		{name: "unknown provider", path: "/quota/gemini", body: `{}`, want: http.StatusNotFound},
		{name: "bad JSON", path: "/quota/codex", body: `{`, want: http.StatusBadRequest},
		{name: "empty observation", path: "/quota/codex", body: `{"observed_at":"2026-09-05T18:10:00Z","status":"unknown","windows":[]}`, want: http.StatusBadRequest},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := requestQuota(t, s, http.MethodPost, tt.path, tt.body)
			if got.Code != tt.want {
				t.Fatalf("status = %d, want %d; body = %s", got.Code, tt.want, got.Body.String())
			}
		})
	}
}

func TestQuotaGetListsAllowedProvidersIncludingUnobserved(t *testing.T) {
	s, _ := quotaTestServer("codex", "claude")
	body := `{"observed_at":"2026-09-05T18:10:00Z","status":"allowed","reached_type":"","windows":[{"name":"primary","used_percent":24}]}`
	if got := requestQuota(t, s, http.MethodPost, "/quota/codex", body); got.Code != http.StatusNoContent {
		t.Fatalf("POST = %d %s", got.Code, got.Body.String())
	}
	response := requestQuota(t, s, http.MethodGet, "/quota", "")
	decoded := decodeBody(t, response)
	providers, ok := decoded["providers"].(map[string]any)
	if !ok || len(providers) != 2 {
		t.Fatalf("providers = %#v", decoded["providers"])
	}
	claude, ok := providers["claude"].(map[string]any)
	if !ok || claude["provider"] != "claude" || claude["observed"] != false || len(claude) != 2 {
		t.Fatalf("unobserved claude view = %#v", providers["claude"])
	}
}

func TestConfiguredQuotaProvidersOverride(t *testing.T) {
	t.Setenv("TOKENBROKER_QUOTA_PROVIDERS", "claude")
	providers := configuredQuotaProviders(slog.New(slog.NewTextHandler(io.Discard, nil)))
	if len(providers) != 1 || providers[0] != "claude" {
		t.Fatalf("providers = %v", providers)
	}
	s, _ := quotaTestServer(providers...)
	if got := requestQuota(t, s, http.MethodGet, "/quota/codex", ""); got.Code != http.StatusNotFound {
		t.Fatalf("codex status = %d, want 404", got.Code)
	}
}

func TestQuotaMetricsAfterPost(t *testing.T) {
	s, registry := quotaTestServer("codex")
	observedAt := time.Now().UTC().Add(-time.Minute).Truncate(time.Second)
	resetAt := time.Now().UTC().Add(time.Hour).Truncate(time.Second)
	body, err := json.Marshal(quota.Observation{
		ObservedAt: observedAt.Format(time.RFC3339), Status: "allowed",
		Windows: []quota.Window{{Name: "primary", UsedPercent: 100, ResetsAt: resetAt.Format(time.RFC3339)}},
	})
	if err != nil {
		t.Fatal(err)
	}
	got := requestQuota(t, s, http.MethodPost, "/quota/codex", string(body))
	if got.Code != http.StatusNoContent {
		t.Fatalf("POST = %d %s", got.Code, got.Body.String())
	}
	expected := fmt.Sprintf(`# HELP tokenbroker_quota_exhausted Whether the latest provider quota observation is exhausted.
# TYPE tokenbroker_quota_exhausted gauge
tokenbroker_quota_exhausted{provider="codex"} 1
# HELP tokenbroker_quota_observed_at_seconds Latest quota observation time as Unix seconds.
# TYPE tokenbroker_quota_observed_at_seconds gauge
tokenbroker_quota_observed_at_seconds{provider="codex"} %d
# HELP tokenbroker_quota_resets_at_seconds Latest observed quota reset time as Unix seconds.
# TYPE tokenbroker_quota_resets_at_seconds gauge
tokenbroker_quota_resets_at_seconds{provider="codex",window="primary"} %d
# HELP tokenbroker_quota_used_percent Latest observed quota utilization percentage.
# TYPE tokenbroker_quota_used_percent gauge
tokenbroker_quota_used_percent{provider="codex",window="primary"} 100
`, observedAt.Unix(), resetAt.Unix())
	if err := testutil.GatherAndCompare(
		registry, strings.NewReader(expected),
		"tokenbroker_quota_exhausted",
		"tokenbroker_quota_observed_at_seconds",
		"tokenbroker_quota_resets_at_seconds",
		"tokenbroker_quota_used_percent",
	); err != nil {
		t.Fatal(err)
	}

	pastResetBody, err := json.Marshal(quota.Observation{
		ObservedAt: observedAt.Format(time.RFC3339), Status: "allowed",
		Windows: []quota.Window{{Name: "primary", UsedPercent: 100, ResetsAt: time.Now().UTC().Add(-time.Second).Format(time.RFC3339)}},
	})
	if err != nil {
		t.Fatal(err)
	}
	if got := requestQuota(t, s, http.MethodPost, "/quota/codex", string(pastResetBody)); got.Code != http.StatusNoContent {
		t.Fatalf("POST expired window = %d %s", got.Code, got.Body.String())
	}
	if err := testutil.GatherAndCompare(
		registry,
		strings.NewReader(`# HELP tokenbroker_quota_exhausted Whether the latest provider quota observation is exhausted.
# TYPE tokenbroker_quota_exhausted gauge
tokenbroker_quota_exhausted{provider="codex"} 0
`),
		"tokenbroker_quota_exhausted",
	); err != nil {
		t.Fatalf("expired window scrape: %v", err)
	}
}

func quotaObservationBodyOfSize(t *testing.T, size int) string {
	t.Helper()
	obs := quota.Observation{
		ObservedAt: "2026-09-05T18:10:00Z", Status: "allowed",
		Windows: []quota.Window{{Name: "padding", UsedPercent: 1}},
	}
	encoded, err := json.Marshal(obs)
	if err != nil {
		t.Fatal(err)
	}
	fixedSize := len(encoded) - len(obs.Windows[0].Name)
	if size <= fixedSize {
		t.Fatalf("requested body size %d is not larger than fixed JSON size %d", size, fixedSize)
	}
	obs.Windows[0].Name = strings.Repeat("x", size-fixedSize)
	encoded, err = json.Marshal(obs)
	if err != nil {
		t.Fatal(err)
	}
	if len(encoded) != size {
		t.Fatalf("body size = %d, want %d", len(encoded), size)
	}
	return string(encoded)
}

func TestQuotaBodyLimit(t *testing.T) {
	s, _ := quotaTestServer("codex")
	tests := []struct {
		name string
		size int
		want int
	}{
		{name: "under limit", size: 63 << 10, want: http.StatusNoContent},
		{name: "over limit", size: (64 << 10) + 1, want: http.StatusBadRequest},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			body := quotaObservationBodyOfSize(t, tt.size)
			got := requestQuota(t, s, http.MethodPost, "/quota/codex", body)
			if got.Code != tt.want {
				t.Fatalf("status = %d, want %d; body = %s", got.Code, tt.want, got.Body.String())
			}
		})
	}
}

func TestForceRefreshReturnsMetadataWithoutCredentialAndEnforcesCooldown(t *testing.T) {
	now := time.Now().UTC()
	st := &fakeStore{grants: map[string]store.Grant{"codex-cluster": {Name: "codex-cluster", ProviderName: "test", LastRefresh: now, TokenBundle: store.TokenBundle{RefreshToken: "refresh-token"}}}}
	s := newTestServer(st, &fakeAdapter{})

	first := requestGrant(t, s, http.MethodPost, "/grants/codex-cluster/refresh")
	if first.Code != http.StatusOK {
		t.Fatalf("status = %d, body = %s", first.Code, first.Body.String())
	}
	body := decodeBody(t, first)
	expiresAt, hasExpiry := body["expires_at"].(string)
	if body["refreshed"] != true || !hasExpiry || expiresAt == "" {
		t.Fatalf("body = %#v, want refresh metadata", body)
	}
	if _, exists := body["access_token"]; exists {
		t.Fatalf("body leaked access_token: %#v", body)
	}

	second := requestGrant(t, s, http.MethodPost, "/grants/codex-cluster/refresh")
	if second.Code != http.StatusTooManyRequests || decodeBody(t, second)["reason"] != "cooldown" {
		t.Fatalf("second response = %d %s", second.Code, second.Body.String())
	}
}

func TestForceRefreshFailureReturnsNeedsLogin(t *testing.T) {
	now := time.Now().UTC()
	st := &fakeStore{grants: map[string]store.Grant{"codex-cluster": {Name: "codex-cluster", ProviderName: "test", LastRefresh: now, TokenBundle: store.TokenBundle{RefreshToken: "dead"}}}}
	s := newTestServer(st, &fakeAdapter{refreshErr: fmt.Errorf("oauth rejected grant: %w", provider.ErrInvalidGrant)})

	response := requestGrant(t, s, http.MethodPost, "/grants/codex-cluster/refresh")
	body := decodeBody(t, response)
	if response.Code != http.StatusServiceUnavailable || body["reason"] != "refresh_failed" || body["needs_login"] != true {
		t.Fatalf("response = %d %#v", response.Code, body)
	}
}

func TestLoginStatusUsesDurableGrantAndNeedsLoginMarker(t *testing.T) {
	now := time.Now().UTC()
	st := &fakeStore{grants: map[string]store.Grant{"codex-cluster": {Name: "codex-cluster", ProviderName: "test", LastRefresh: now, TokenBundle: store.TokenBundle{RefreshToken: "dead"}}}}
	adapter := &fakeAdapter{refreshErr: fmt.Errorf("oauth rejected grant: %w", provider.ErrInvalidGrant)}
	s := newTestServer(st, adapter)

	granted := requestGrant(t, s, http.MethodGet, "/grants/codex-cluster/login/status")
	grantedBody := decodeBody(t, granted)
	if grantedBody["state"] != "granted" || grantedBody["detail"] != "stored grant present" {
		t.Fatalf("stored status = %#v", grantedBody)
	}

	if _, _, err := s.broker.RefreshAccessToken("codex-cluster", context.Background()); !errors.Is(err, provider.ErrInvalidGrant) {
		t.Fatalf("refresh error = %v, want ErrInvalidGrant", err)
	}
	dead := requestGrant(t, s, http.MethodGet, "/grants/codex-cluster/login/status")
	deadBody := decodeBody(t, dead)
	if deadBody["state"] != "none" || deadBody["detail"] != "refresh failed, device login required" {
		t.Fatalf("dead status = %#v", deadBody)
	}
}

func TestPendingLoginStatusAndConflictRepeatLiveDeviceCode(t *testing.T) {
	release := make(chan struct{})
	defer close(release)
	st := &fakeStore{grants: make(map[string]store.Grant)}
	adapter := &fakeAdapter{
		deviceCode: provider.DeviceCodeResponse{VerificationURL: "https://auth.example/device", UserCode: "ABCD-EFGH", ExpiresIn: 900},
		pollWait:   release,
	}
	s := newTestServer(st, adapter)

	started := requestGrant(t, s, http.MethodPost, "/grants/codex-cluster/login/start")
	if started.Code != http.StatusOK {
		t.Fatalf("start = %d %s", started.Code, started.Body.String())
	}
	status := decodeBody(t, requestGrant(t, s, http.MethodGet, "/grants/codex-cluster/login/status"))
	if status["state"] != "pending" || status["detail"] != "approval required" {
		t.Fatalf("pending status = %#v", status)
	}

	conflict := requestGrant(t, s, http.MethodPost, "/grants/codex-cluster/login/start")
	conflictBody := decodeBody(t, conflict)
	if conflict.Code != http.StatusConflict || conflictBody["reason"] != "login_pending" || conflictBody["verification_url"] != "https://auth.example/device" || conflictBody["user_code"] != "ABCD-EFGH" || conflictBody["expires_in"] != float64(900) {
		t.Fatalf("conflict = %d %#v", conflict.Code, conflictBody)
	}
}

func TestConcurrentLoginStartWaitsForDeviceCode(t *testing.T) {
	startWait := make(chan struct{})
	startCalled := make(chan struct{})
	pollWait := make(chan struct{})
	defer close(pollWait)
	adapter := &fakeAdapter{
		deviceCode:  provider.DeviceCodeResponse{VerificationURL: "https://auth.example/device", UserCode: "ABCD-EFGH", ExpiresIn: 900},
		startWait:   startWait,
		startCalled: startCalled,
		pollWait:    pollWait,
	}
	s := newTestServer(&fakeStore{grants: make(map[string]store.Grant)}, adapter)

	firstResult := requestGrantAsync(s, context.Background())
	receiveWithin(t, startCalled, "first caller to start the device flow")
	waiting := make(chan time.Time, 1)
	secondResult := requestGrantAsync(s, &signalingContext{Context: context.Background(), entered: waiting})
	receiveWithin(t, waiting, "second caller to enter the wait")
	close(startWait)

	first := receiveWithin(t, firstResult, "first login response")
	firstBody := decodeBody(t, first)
	if first.Code != http.StatusOK || firstBody["verification_url"] != "https://auth.example/device" || firstBody["user_code"] != "ABCD-EFGH" || firstBody["expires_in"] != float64(900) {
		t.Fatalf("first = %d %#v", first.Code, firstBody)
	}
	second := receiveWithin(t, secondResult, "second login response")
	secondBody := decodeBody(t, second)
	if second.Code != http.StatusConflict || secondBody["reason"] != "login_pending" || secondBody["verification_url"] != "https://auth.example/device" || secondBody["user_code"] != "ABCD-EFGH" || secondBody["expires_in"] != float64(900) {
		t.Fatalf("second = %d %#v", second.Code, secondBody)
	}
}

func TestConcurrentLoginStartReportsStartingWhenDeviceCodeFails(t *testing.T) {
	startWait := make(chan struct{})
	startCalled := make(chan struct{})
	adapter := &fakeAdapter{
		deviceErr:   errors.New("device flow unavailable"),
		startWait:   startWait,
		startCalled: startCalled,
	}
	s := newTestServer(&fakeStore{grants: make(map[string]store.Grant)}, adapter)

	firstResult := requestGrantAsync(s, context.Background())
	receiveWithin(t, startCalled, "first caller to start the device flow")
	waiting := make(chan time.Time, 1)
	secondResult := requestGrantAsync(s, &signalingContext{Context: context.Background(), entered: waiting})
	receiveWithin(t, waiting, "second caller to enter the wait")
	close(startWait)

	first := receiveWithin(t, firstResult, "first login response")
	if first.Code != http.StatusBadGateway || decodeBody(t, first)["reason"] != "device_code_failed" {
		t.Fatalf("first = %d %s", first.Code, first.Body.String())
	}
	second := receiveWithin(t, secondResult, "second login response")
	secondBody := decodeBody(t, second)
	if second.Code != http.StatusConflict || len(secondBody) != 1 || secondBody["reason"] != "login_starting" {
		t.Fatalf("second = %d %#v", second.Code, secondBody)
	}
}

func TestConcurrentLoginStartTimesOutWaitingForDeviceCode(t *testing.T) {
	startWait := make(chan struct{})
	startCalled := make(chan struct{})
	adapter := &fakeAdapter{
		deviceCode:  provider.DeviceCodeResponse{VerificationURL: "https://auth.example/device", UserCode: "ABCD-EFGH", ExpiresIn: 900},
		startWait:   startWait,
		startCalled: startCalled,
	}
	s := newTestServer(&fakeStore{grants: make(map[string]store.Grant)}, adapter)
	s.startWaitTimeout = time.Millisecond

	firstResult := requestGrantAsync(s, context.Background())
	receiveWithin(t, startCalled, "first caller to start the device flow")
	waiting := make(chan time.Time, 1)
	secondResult := requestGrantAsync(s, &signalingContext{Context: context.Background(), entered: waiting})
	startedAt := receiveWithin(t, waiting, "second caller to enter the wait")
	second := receiveWithin(t, secondResult, "second login response")
	elapsed := time.Since(startedAt)
	if elapsed < s.startWaitTimeout || elapsed > s.startWaitTimeout+time.Second {
		t.Fatalf("wait duration = %s, want about %s", elapsed, s.startWaitTimeout)
	}
	secondBody := decodeBody(t, second)
	if second.Code != http.StatusConflict || len(secondBody) != 1 || secondBody["reason"] != "login_starting" {
		t.Fatalf("second = %d %#v", second.Code, secondBody)
	}

	close(startWait)
	receiveWithin(t, firstResult, "first login response")
}

func TestLoginStartClosesReadyChannelWhenDeviceFlowPanics(t *testing.T) {
	s := newTestServer(
		&fakeStore{grants: make(map[string]store.Grant)},
		&fakeAdapter{startPanic: "device flow panic"},
	)
	s.startWaitTimeout = 500 * time.Millisecond

	panicked := false
	func() {
		defer func() {
			panicked = recover() != nil
		}()
		requestGrant(t, s, http.MethodPost, "/grants/codex-cluster/login/start")
	}()
	if !panicked {
		t.Fatal("StartDeviceFlow did not panic")
	}

	startedAt := time.Now()
	response := requestGrant(t, s, http.MethodPost, "/grants/codex-cluster/login/start")
	if elapsed := time.Since(startedAt); elapsed >= s.startWaitTimeout {
		t.Fatalf("response after panic waited %s", elapsed)
	}
	body := decodeBody(t, response)
	if response.Code != http.StatusConflict || len(body) != 1 || body["reason"] != "login_starting" {
		t.Fatalf("response after panic = %d %#v", response.Code, body)
	}
}
