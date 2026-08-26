package broker

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
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
	mu            sync.Mutex
	cond          *sync.Cond
	refreshing    bool
	lastErr       error
	lastAccess    string
	lastExpiry    time.Time
	needsLogin    bool
	authoritative *store.Grant
}

func (b *Broker) NeedsLogin(grantName string) bool {
	s, err := b.state(grantName)
	if err != nil {
		return false
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.needsLogin
}

func (b *Broker) ClearNeedsLogin(grantName string) {
	s, err := b.state(grantName)
	if err != nil {
		return
	}
	s.mu.Lock()
	s.needsLogin = false
	s.mu.Unlock()
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
	// The refresh must survive an HTTP caller disappearing after the provider has
	// rotated the refresh token. Callers still wait for this shared round-trip.
	refreshCtx, cancel := context.WithTimeout(context.Background(), time.Minute)
	access, expiry, err := b.refresh(grantName, refreshCtx)
	cancel()
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
	s, _ := b.state(grantName)
	s.mu.Lock()
	// The in-memory stash is authoritative ONLY while it is ahead of the
	// durable store (a rotation whose save is still retrying). Once the store
	// has caught up, clear it: preferring a stale stash would replay an
	// already-consumed refresh token forever and pin the reuse alarm high.
	if s.authoritative != nil {
		if s.authoritative.LastRefresh.After(grant.LastRefresh) {
			grant = *s.authoritative
		} else {
			s.authoritative = nil
		}
	}
	s.mu.Unlock()
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
	if err != nil && errors.Is(err, provider.ErrRefreshTokenReused) {
		b.metrics.ReusedRetries.Inc()
		b.logger.Warn("tokenbroker refresh_token_reused, rereading durable grant and retrying once")
		grant, err = b.store.LoadGrant(grantName)
		if err == nil {
			result, err = adapter.RefreshToken(ctx, grant.TokenBundle.RefreshToken)
		}
	}
	if err != nil {
		if errors.Is(err, provider.ErrInvalidGrant) {
			s.mu.Lock()
			s.needsLogin = true
			s.mu.Unlock()
		}
		b.metrics.RefreshFailures.Inc()
		b.logger.Error("tokenbroker refresh failed", "grant", grantName, "err", err)
		return "", time.Time{}, err
	}
	grant.Name = grantName
	grant.TokenBundle.IDToken = result.IDToken
	grant.TokenBundle.AccessToken = result.AccessToken
	grant.TokenBundle.ExpiresAt = result.ExpiresAt
	if result.RefreshToken != "" {
		grant.TokenBundle.RefreshToken = result.RefreshToken
	}
	grant.LastRefresh = time.Now().UTC()
	grant.TokenBundle.LastRefresh = grant.LastRefresh
	if err = b.saveRotatedGrant(grantName, grant); err != nil {
		if errors.Is(err, store.ErrGrantNotNewer) {
			stored, loadErr := b.store.LoadGrant(grantName)
			if loadErr != nil {
				return "", time.Time{}, loadErr
			}
			if stored.TokenBundle.AccessToken == "" {
				return "", time.Time{}, ErrNoGrant
			}
			b.ClearNeedsLogin(grantName)
			return stored.TokenBundle.AccessToken, stored.TokenBundle.ExpiresAt, nil
		}
		b.metrics.RefreshFailures.Inc()
		b.logger.Error("tokenbroker rotated grant persistence failed", "grant", grantName, "err", err)
		return "", time.Time{}, err
	}
	b.ClearNeedsLogin(grantName)
	return grant.TokenBundle.AccessToken, grant.TokenBundle.ExpiresAt, nil
}

func (b *Broker) saveRotatedGrant(name string, grant store.Grant) error {
	var err error
	for attempt := 0; attempt < 5; attempt++ {
		err = b.store.SaveGrantIfNewer(grant)
		if err == nil || errors.Is(err, store.ErrGrantNotNewer) {
			return err
		}
		if attempt < 4 {
			time.Sleep(time.Duration(1<<attempt) * 2 * time.Second)
		}
	}
	s, _ := b.state(name)
	s.mu.Lock()
	copy := grant
	s.authoritative = &copy
	s.mu.Unlock()
	go b.retryGrantPersistence(name, grant)
	return err
}

func (b *Broker) retryGrantPersistence(name string, grant store.Grant) {
	for {
		time.Sleep(30 * time.Second)
		if err := b.store.SaveGrantIfNewer(grant); err == nil || errors.Is(err, store.ErrGrantNotNewer) {
			return
		} else {
			b.logger.Error("tokenbroker rotated grant persistence retry failed", "grant", name, "err", err)
		}
	}
}

func (b *Broker) GetAccessToken(grantName string, ctx context.Context) (string, time.Time, error) {
	if _, err := b.state(grantName); err != nil {
		return "", time.Time{}, err
	}
	grant, err := b.store.LoadGrant(grantName)
	if err != nil {
		return "", time.Time{}, err
	}
	if grant.TokenBundle.AccessToken == "" || grant.TokenBundle.ExpiresAt.IsZero() {
		return "", time.Time{}, ErrNoGrant
	}
	expires := grant.TokenBundle.ExpiresAt
	// Codex access tokens are valid for roughly eight days. Refresh at seven days
	// so a broker outage does not run into the provider's staleness boundary.
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
		if grant.TokenBundle.ExpiresAt.IsZero() {
			return "", time.Time{}, ErrNoGrant
		}
		expires = grant.TokenBundle.ExpiresAt
	}
	return grant.TokenBundle.AccessToken, expires, nil
}

func (b *Broker) SaveGrant(grant store.Grant) error {
	s, err := b.state(grant.Name)
	if err != nil {
		return err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.refreshing {
		return errors.New("grant refresh in progress")
	}
	err = b.store.SaveGrantIfNewer(grant)
	if errors.Is(err, store.ErrGrantNotNewer) {
		return err
	}
	return err
}

var (
	ErrNoGrant       = errors.New("no_grant")
	ErrRefreshFailed = errors.New("refresh_failed")
)
