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
	"sync"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/embervm/tokenbroker/internal/broker"
	"github.com/jomcgi/homelab/projects/embervm/tokenbroker/internal/provider"
	"github.com/jomcgi/homelab/projects/embervm/tokenbroker/internal/store"
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
	refreshErr error
	deviceCode provider.DeviceCodeResponse
	pollWait   <-chan struct{}
}

func (a *fakeAdapter) StartDeviceFlow(context.Context) (provider.DeviceCodeResponse, error) {
	return a.deviceCode, nil
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
	b := broker.New(st, adapters, []broker.GrantConfig{{Name: "codex-cluster", ProviderName: "test"}}, logger, nil)
	return &server{broker: b, store: st, adapters: adapters, configs: configs, logger: logger}
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
