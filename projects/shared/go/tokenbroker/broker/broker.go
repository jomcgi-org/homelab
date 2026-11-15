package broker

import (
	"context"
	"crypto/rand"
	"errors"
	"fmt"
	"log/slog"
	"sync"
	"time"

	"github.com/jomcgi/homelab/projects/shared/go/tokenbroker/metrics"
	"github.com/jomcgi/homelab/projects/shared/go/tokenbroker/provider"
	"github.com/jomcgi/homelab/projects/shared/go/tokenbroker/store"
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
	loginMu       sync.Mutex
	mu            sync.Mutex
	cond          *sync.Cond
	refreshing    bool
	lastErr       error
	lastAccess    string
	lastExpiry    time.Time
	needsLogin    bool
	authoritative *store.Grant
	pendingLogin  *store.Grant
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
	// Two typed registries rather than one map of any: a provider advertises
	// the capability it has, and registering the wrong kind is a compile
	// error rather than a runtime "does not support" in a component whose job
	// is minting credentials. Adapter is the interactive device flow, Minter
	// is direct client_credentials minting.
	adapters       map[string]provider.Adapter
	minters        map[string]provider.Minter
	store          Store
	grants         map[string]*grantState
	grantProviders map[string]string
	mu             sync.RWMutex
	logger         *slog.Logger
	metrics        *metrics.Metrics
	refreshBefore  time.Duration
	maxGrantAge    time.Duration
}

// Default refresh policy. Codex access tokens are valid for roughly eight
// days. Refresh at seven days so a broker outage does not run into the
// provider's staleness boundary.
const (
	DefaultRefreshBeforeExpiry = 5 * time.Minute
	DefaultMaxGrantAge         = 7 * 24 * time.Hour
)

// SetRefreshPolicy makes GetAccessToken refresh a grant once it is within
// beforeExpiry of expiry or older than maxAge. Zero keeps the default. Call
// it before the broker serves requests.
func (b *Broker) SetRefreshPolicy(beforeExpiry, maxAge time.Duration) {
	if beforeExpiry > 0 {
		b.refreshBefore = beforeExpiry
	}
	if maxAge > 0 {
		b.maxGrantAge = maxAge
	}
}

func New(s Store, adapters map[string]provider.Adapter, minters map[string]provider.Minter, configs []GrantConfig, logger *slog.Logger, m *metrics.Metrics) *Broker {
	if logger == nil {
		logger = slog.Default()
	}
	if m == nil {
		m = metrics.New()
	}
	if adapters == nil {
		adapters = map[string]provider.Adapter{}
	}
	if minters == nil {
		minters = map[string]provider.Minter{}
	}
	b := &Broker{adapters: adapters, minters: minters, store: s, grants: make(map[string]*grantState), grantProviders: make(map[string]string), logger: logger, metrics: m, refreshBefore: DefaultRefreshBeforeExpiry, maxGrantAge: DefaultMaxGrantAge}
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
	// Never rotate the previous grant while an exchanged login awaits storage.
	if s.pendingLogin != nil {
		s.mu.Unlock()
		return "", time.Time{}, ErrGrantPersistencePending
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
	grant, err := b.loadGrant(ctx, grantName)
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
		if store.CompareGrantVersion(*s.authoritative, grant) > 0 {
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
	refresher, hasRefresher := b.adapters[grant.ProviderName]
	minter, hasMinter := b.minters[grant.ProviderName]
	b.mu.RUnlock()
	if !hasRefresher && !hasMinter {
		return "", time.Time{}, fmt.Errorf("provider %q is not configured", grant.ProviderName)
	}
	var result provider.TokenResponse
	if grant.TokenBundle.RefreshToken == "" {
		// A client_credentials grant never has a refresh token: it re-mints.
		if !hasMinter {
			return "", time.Time{}, errors.New("no refresh token")
		}
		result, err = minter.Mint(ctx)
	} else {
		if !hasRefresher {
			return "", time.Time{}, fmt.Errorf("provider %q does not support token refresh", grant.ProviderName)
		}
		result, err = refresher.RefreshToken(ctx, grant.TokenBundle.RefreshToken)
		if err != nil && errors.Is(err, provider.ErrRefreshTokenReused) {
			b.metrics.ReusedRetries.Inc()
			b.logger.Warn("tokenbroker refresh_token_reused, rereading durable grant and retrying once")
			grant, err = b.loadGrant(ctx, grantName)
			if err == nil {
				result, err = refresher.RefreshToken(ctx, grant.TokenBundle.RefreshToken)
			}
		}
	}
	if err != nil {
		if errors.Is(err, provider.ErrInvalidGrant) {
			// Another replica may have replaced this authorization while its
			// refresh was in flight. A refusal of the old token says nothing
			// about the newer durable grant.
			if saved, loadErr := b.loadGrant(ctx, grantName); loadErr == nil && store.CompareGrantVersion(saved, grant) > 0 && saved.TokenBundle.AccessToken != "" {
				b.ClearNeedsLogin(grantName)
				return saved.TokenBundle.AccessToken, saved.TokenBundle.ExpiresAt, nil
			}
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
	stamp := time.Now().UTC()
	if !stamp.After(grant.LastRefresh) {
		stamp = grant.LastRefresh.Add(time.Nanosecond)
	}
	grant.LastRefresh = stamp
	grant.TokenBundle.LastRefresh = grant.LastRefresh
	if err = b.saveRotatedGrant(ctx, grantName, grant); err != nil {
		if errors.Is(err, store.ErrGrantNotNewer) {
			stored, loadErr := b.loadGrant(ctx, grantName)
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

func (b *Broker) saveRotatedGrant(ctx context.Context, name string, grant store.Grant) error {
	var err error
	for attempt := 0; attempt < 5; attempt++ {
		err = b.saveGrant(ctx, grant)
		if err == nil || errors.Is(err, store.ErrGrantNotNewer) {
			return err
		}
		if attempt < 4 {
			timer := time.NewTimer(time.Duration(1<<attempt) * 2 * time.Second)
			select {
			case <-timer.C:
			case <-ctx.Done():
			}
			timer.Stop()
		}
		if ctx.Err() != nil {
			break
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
		if err := b.saveGrant(context.Background(), grant); err == nil || errors.Is(err, store.ErrGrantNotNewer) {
			return
		} else {
			b.logger.Error("tokenbroker rotated grant persistence retry failed", "grant", name, "err", err)
		}
	}
}

// CachedAccessToken is the read-only token path for usage observation. It never
// mints or rotates credentials; ordinary token serving owns OAuth refresh.
func (b *Broker) CachedAccessToken(grantName string, ctx context.Context) (string, time.Time, error) {
	if err := ctx.Err(); err != nil {
		return "", time.Time{}, err
	}
	if _, err := b.state(grantName); err != nil {
		return "", time.Time{}, err
	}
	grant, err := b.loadGrant(ctx, grantName)
	if err != nil {
		return "", time.Time{}, err
	}
	if grant.TokenBundle.AccessToken == "" || time.Until(grant.TokenBundle.ExpiresAt) <= time.Minute {
		return "", time.Time{}, ErrNoGrant
	}
	return grant.TokenBundle.AccessToken, grant.TokenBundle.ExpiresAt, nil
}

func (b *Broker) GetAccessToken(grantName string, ctx context.Context) (string, time.Time, error) {
	if _, err := b.state(grantName); err != nil {
		return "", time.Time{}, err
	}
	grant, err := b.loadGrant(ctx, grantName)
	if err != nil {
		return "", time.Time{}, err
	}
	if grant.TokenBundle.AccessToken == "" || grant.TokenBundle.ExpiresAt.IsZero() {
		providerName := grant.ProviderName
		if providerName == "" {
			b.mu.RLock()
			providerName = b.grantProviders[grantName]
			b.mu.RUnlock()
		}
		b.mu.RLock()
		_, supportsMint := b.minters[providerName]
		b.mu.RUnlock()
		if !supportsMint {
			return "", time.Time{}, ErrNoGrant
		}
		if _, _, err = b.RefreshAccessToken(grantName, ctx); err != nil {
			return "", time.Time{}, ErrRefreshFailed
		}
		grant, err = b.loadGrant(ctx, grantName)
		if err != nil {
			return "", time.Time{}, err
		}
		if grant.TokenBundle.AccessToken == "" || grant.TokenBundle.ExpiresAt.IsZero() {
			return "", time.Time{}, ErrNoGrant
		}
	}
	expires := grant.TokenBundle.ExpiresAt
	if time.Until(expires) <= b.refreshBefore || time.Since(grant.LastRefresh) > b.maxGrantAge {
		state, stateErr := b.state(grantName)
		if stateErr != nil {
			return "", time.Time{}, stateErr
		}
		state.mu.Lock()
		if state.lastAccess != "" && time.Until(state.lastExpiry) > b.refreshBefore && time.Since(grant.LastRefresh) <= b.maxGrantAge {
			access, expiry := state.lastAccess, state.lastExpiry
			state.mu.Unlock()
			return access, expiry, nil
		}
		state.mu.Unlock()
		if _, _, err = b.RefreshAccessToken(grantName, ctx); err != nil {
			return "", time.Time{}, ErrRefreshFailed
		}
		grant, err = b.loadGrant(ctx, grantName)
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

// SaveGrant installs an exchanged login after any active refresh. Once called,
// persistence is independent of the device flow's context: its code is one-use.
// Failed writes retain the bundle for RetryPendingGrant without another exchange.
func (b *Broker) SaveGrant(grant store.Grant) error {
	s, err := b.state(grant.Name)
	if err != nil {
		return err
	}
	s.loginMu.Lock()
	defer s.loginMu.Unlock()
	s.mu.Lock()
	if s.pendingLogin != nil {
		s.mu.Unlock()
		return ErrGrantPersistencePending
	}
	// Assign the persistence order only once the preceding refresh has finished.
	grant.LastRefresh = time.Time{}
	// Refresh preserves this identity, including across broker replicas. The
	// login order stays fixed even if persistence is retried much later.
	grant.LoginID, grant.LoginAt = rand.Text(), time.Now().UTC()
	// Secret timestamps round-trip through RFC3339 without monotonic data or
	// local timezone pointers. Normalize for ambiguous-write confirmation.
	grant.TokenBundle.ExpiresAt = grant.TokenBundle.ExpiresAt.UTC()
	s.pendingLogin = &grant
	for s.refreshing {
		s.cond.Wait()
	}
	s.refreshing = true
	s.mu.Unlock()
	return b.persistLogin(s, grant)
}

// RetryPendingGrant retries only persistence of the retained login. The caller
// must serialize device flows; SaveGrant refuses to replace a pending bundle.
func (b *Broker) RetryPendingGrant(name string) error {
	s, err := b.state(name)
	if err != nil {
		return err
	}
	s.loginMu.Lock()
	defer s.loginMu.Unlock()
	s.mu.Lock()
	if s.pendingLogin == nil {
		s.mu.Unlock()
		return nil
	}
	grant := *s.pendingLogin
	s.refreshing = true
	s.mu.Unlock()
	return b.persistLogin(s, grant)
}

func (b *Broker) persistLogin(s *grantState, grant store.Grant) error {
	var err error
	for attempt := 0; attempt < 5; attempt++ {
		// Secret writes have their own deadline, independent of login cancellation.
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		err = b.persistLoginAttempt(ctx, s, &grant)
		if errors.Is(err, store.ErrGrantNotNewer) {
			// A lost write response may have been followed by another replica
			// rotating this login, or a later device login superseding it. Adopt
			// that durable version without replaying a consumed refresh token.
			saved, loadErr := b.loadGrant(ctx, grant.Name)
			if loadErr == nil && saved.TokenBundle.AccessToken != "" && saved.TokenBundle.RefreshToken != "" &&
				(store.CompareGrantVersion(saved, grant) > 0 ||
					(store.CompareGrantVersion(saved, grant) == 0 && saved.TokenBundle == grant.TokenBundle)) {
				grant = saved
				err = nil
			}
		}
		cancel()
		if err == nil {
			break
		}
		if attempt < 4 {
			time.Sleep(time.Duration(1<<attempt) * 250 * time.Millisecond)
		}
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.refreshing = false
	if err != nil {
		// Storage errors may contain Secret contents. Expose only a fixed code.
		s.lastErr = ErrGrantPersistencePending
	} else {
		s.pendingLogin = nil
		s.authoritative = nil
		s.needsLogin = false
		s.lastErr = nil
		s.lastAccess, s.lastExpiry = grant.TokenBundle.AccessToken, grant.TokenBundle.ExpiresAt
	}
	s.cond.Broadcast()
	return s.lastErr
}

func (b *Broker) loadGrant(ctx context.Context, name string) (store.Grant, error) {
	ctx, cancel := context.WithTimeout(ctx, store.RequestTimeout)
	defer cancel()
	if err := ctx.Err(); err != nil {
		return store.Grant{}, err
	}
	if st, ok := b.store.(interface {
		LoadGrantContext(context.Context, string) (store.Grant, error)
	}); ok {
		return st.LoadGrantContext(ctx, name)
	}
	return b.store.LoadGrant(name)
}

func (b *Broker) saveGrant(ctx context.Context, grant store.Grant) error {
	ctx, cancel := context.WithTimeout(ctx, store.RequestTimeout)
	defer cancel()
	if err := ctx.Err(); err != nil {
		return err
	}
	if st, ok := b.store.(interface {
		SaveGrantIfNewerContext(context.Context, store.Grant) error
	}); ok {
		return st.SaveGrantIfNewerContext(ctx, grant)
	}
	return b.store.SaveGrantIfNewer(grant)
}

func (b *Broker) persistLoginAttempt(ctx context.Context, s *grantState, grant *store.Grant) error {
	if grant.LastRefresh.IsZero() {
		saved, err := b.loadGrant(ctx, grant.Name)
		if err != nil {
			return err
		}
		// Include both the durable refresh and any rotation awaiting its own
		// persistence retries. Keep this timestamp fixed across login retries.
		stamp := time.Now().UTC()
		if !stamp.After(saved.LastRefresh) {
			stamp = saved.LastRefresh.Add(time.Nanosecond)
		}
		s.mu.Lock()
		if s.authoritative != nil && !stamp.After(s.authoritative.LastRefresh) {
			stamp = s.authoritative.LastRefresh.Add(time.Nanosecond)
		}
		grant.LastRefresh, grant.TokenBundle.LastRefresh = stamp, stamp
		copy := *grant
		s.pendingLogin = &copy
		s.mu.Unlock()
	}
	return b.saveGrant(ctx, *grant)
}

var (
	ErrNoGrant                 = errors.New("no_grant")
	ErrRefreshFailed           = errors.New("refresh_failed")
	ErrGrantPersistencePending = errors.New("grant_persistence_pending")
)
