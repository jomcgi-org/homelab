package broker

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"strings"
	"sync"
	"time"

	"github.com/jomcgi/homelab/projects/embervm/tokenbroker/internal/metrics"
	"github.com/jomcgi/homelab/projects/embervm/tokenbroker/internal/provider"
	"github.com/jomcgi/homelab/projects/embervm/tokenbroker/internal/store"
)

type (
	GrantConfig struct{ Name, ProviderName string }
	Store       interface {
		LoadGrant(string) (store.Grant, error)
		SaveGrant(store.Grant) error
		SaveGrantIfNewer(store.Grant) error
	}
)

type grantState struct {
	mu         sync.Mutex
	cond       *sync.Cond
	refreshing bool
	lastErr    error
	lastAccess string
	lastExpiry time.Time
}
type Broker struct {
	adapters       map[string]provider.Adapter
	store          Store
	grants         map[string]*grantState
	grantProviders map[string]string
	mu             sync.RWMutex
	logger         *slog.Logger
	metrics        *metrics.Metrics
}

func New(s Store, adapters map[string]provider.Adapter, configs []GrantConfig, logger *slog.Logger, m *metrics.Metrics) *Broker {
	if logger == nil {
		logger = slog.Default()
	}
	if m == nil {
		m = metrics.New()
	}
	b := &Broker{adapters: adapters, store: s, grants: make(map[string]*grantState), grantProviders: make(map[string]string), logger: logger, metrics: m}
	for _, config := range configs {
		s := &grantState{}
		s.cond = sync.NewCond(&s.mu)
		b.grants[config.Name] = s
		b.grantProviders[config.Name] = config.ProviderName
	}
	return b
}

func (b *Broker) state(name string) (*grantState, error) {
	b.mu.RLock()
	s, ok := b.grants[name]
	b.mu.RUnlock()
	if !ok {
		return nil, ErrNoGrant
	}
	return s, nil
}

func (b *Broker) RefreshAccessToken(grantName string, ctx context.Context) (string, time.Time, error) {
	s, err := b.state(grantName)
	if err != nil {
		return "", time.Time{}, err
	}
	s.mu.Lock()
	waited := false
	for s.refreshing {
		waited = true
		s.cond.Wait()
	}
	if waited {
		access, expiry, err := s.lastAccess, s.lastExpiry, s.lastErr
		s.mu.Unlock()
		return access, expiry, err
	}
	s.refreshing = true
	s.lastErr = nil
	s.mu.Unlock()
	access, expiry, err := b.refresh(grantName, ctx)
	s.mu.Lock()
	s.refreshing = false
	s.lastErr = err
	s.lastAccess, s.lastExpiry = access, expiry
	s.cond.Broadcast()
	s.mu.Unlock()
	return access, expiry, err
}

func (b *Broker) refresh(grantName string, ctx context.Context) (string, time.Time, error) {
	b.metrics.Refreshes.Inc()
	grant, err := b.store.LoadGrant(grantName)
	if err != nil {
		return "", time.Time{}, err
	}
	if grant.ProviderName == "" {
		b.mu.RLock()
		grant.ProviderName = b.grantProviders[grantName]
		b.mu.RUnlock()
	}
	b.mu.RLock()
	adapter, ok := b.adapters[grant.ProviderName]
	b.mu.RUnlock()
	if !ok {
		return "", time.Time{}, fmt.Errorf("provider %q is not configured", grant.ProviderName)
	}
	if grant.TokenBundle.RefreshToken == "" {
		return "", time.Time{}, errors.New("no refresh token")
	}
	result, err := adapter.RefreshToken(ctx, grant.TokenBundle.RefreshToken)
	if err != nil && strings.Contains(err.Error(), "refresh_token_reused") {
		b.metrics.ReusedRetries.Inc()
		b.logger.Warn("tokenbroker refresh_token_reused, rereading durable grant and retrying once")
		grant, err = b.store.LoadGrant(grantName)
		if err == nil {
			result, err = adapter.RefreshToken(ctx, grant.TokenBundle.RefreshToken)
		}
	}
	if err != nil {
		b.metrics.RefreshFailures.Inc()
		b.logger.Error("tokenbroker refresh failed", "grant", grantName, "err", err)
		return "", time.Time{}, err
	}
	grant.Name = grantName
	grant.TokenBundle.IDToken = result.IDToken
	grant.TokenBundle.AccessToken = result.AccessToken
	if result.RefreshToken != "" {
		grant.TokenBundle.RefreshToken = result.RefreshToken
	}
	grant.LastRefresh = time.Now().UTC()
	grant.TokenBundle.LastRefresh = grant.LastRefresh
	if err = b.store.SaveGrantIfNewer(grant); err != nil {
		b.metrics.RefreshFailures.Inc()
		return "", time.Time{}, err
	}
	expires, err := TokenExpiry(grant.TokenBundle.AccessToken)
	if err != nil {
		return "", time.Time{}, err
	}
	return grant.TokenBundle.AccessToken, expires, nil
}

func (b *Broker) GetAccessToken(grantName string, ctx context.Context) (string, time.Time, error) {
	if _, err := b.state(grantName); err != nil {
		return "", time.Time{}, err
	}
	grant, err := b.store.LoadGrant(grantName)
	if err != nil {
		return "", time.Time{}, err
	}
	if grant.TokenBundle.AccessToken == "" {
		return "", time.Time{}, ErrNoGrant
	}
	expires, err := TokenExpiry(grant.TokenBundle.AccessToken)
	if err != nil {
		return "", time.Time{}, fmt.Errorf("parse access token: %w", err)
	}
	if time.Until(expires) <= 5*time.Minute || time.Since(grant.LastRefresh) > 7*24*time.Hour {
		state, stateErr := b.state(grantName)
		if stateErr != nil {
			return "", time.Time{}, stateErr
		}
		state.mu.Lock()
		if state.lastAccess != "" && time.Until(state.lastExpiry) > 5*time.Minute && time.Since(grant.LastRefresh) <= 7*24*time.Hour {
			access, expiry := state.lastAccess, state.lastExpiry
			state.mu.Unlock()
			return access, expiry, nil
		}
		state.mu.Unlock()
		if _, _, err = b.RefreshAccessToken(grantName, ctx); err != nil {
			return "", time.Time{}, ErrRefreshFailed
		}
		grant, err = b.store.LoadGrant(grantName)
		if err != nil {
			return "", time.Time{}, err
		}
		expires, err = TokenExpiry(grant.TokenBundle.AccessToken)
		if err != nil {
			return "", time.Time{}, err
		}
	}
	return grant.TokenBundle.AccessToken, expires, nil
}

var (
	ErrNoGrant       = errors.New("no_grant")
	ErrRefreshFailed = errors.New("refresh_failed")
)

func TokenExpiry(token string) (time.Time, error) {
	parts := strings.Split(token, ".")
	if len(parts) < 2 {
		return time.Time{}, errors.New("not a JWT")
	}
	raw, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil {
		return time.Time{}, err
	}
	var claims struct {
		Exp int64 `json:"exp"`
	}
	if err = json.Unmarshal(raw, &claims); err != nil {
		return time.Time{}, err
	}
	if claims.Exp == 0 {
		return time.Time{}, errors.New("JWT has no exp")
	}
	return time.Unix(claims.Exp, 0), nil
}
