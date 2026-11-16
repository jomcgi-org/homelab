package broker

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"sync"
	"testing"
	"testing/synctest"
	"time"

	"github.com/jomcgi/homelab/projects/shared/go/tokenbroker/provider"
	"github.com/jomcgi/homelab/projects/shared/go/tokenbroker/store"
)

type testStore struct {
	mu      sync.Mutex
	grants  map[string]store.Grant
	loadErr error
}

func (s *testStore) LoadGrant(name string) (store.Grant, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.grants[name], s.loadErr
}

func (s *testStore) SaveGrant(g store.Grant) error {
	s.mu.Lock()
	s.grants[g.Name] = g
	s.mu.Unlock()
	return nil
}

func (s *testStore) SaveGrantIfNewer(g store.Grant) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if previous := s.grants[g.Name]; !previous.LastRefresh.IsZero() && store.CompareGrantVersion(g, previous) <= 0 {
		return store.ErrGrantNotNewer
	}
	s.grants[g.Name] = g
	return nil
}

type testAdapter struct {
	mu           sync.Mutex
	calls        int
	refreshInput []string
	delay        time.Duration
	firstReused  bool
	refreshErr   error
	onFirst      func()
}

type testMinter struct {
	mu    sync.Mutex
	calls int
	token provider.TokenResponse
}

func (m *testMinter) Mint(context.Context) (provider.TokenResponse, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.calls++
	return m.token, nil
}

type testDualAdapter struct {
	*testAdapter
	mintCalls int
}

func (a *testDualAdapter) Mint(context.Context) (provider.TokenResponse, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.mintCalls++
	return provider.TokenResponse{}, errors.New("unexpected mint")
}

func (a *testAdapter) StartDeviceFlow(context.Context) (provider.DeviceCodeResponse, error) {
	return provider.DeviceCodeResponse{}, nil
}

func (a *testAdapter) PollForAuthorization(context.Context, provider.DeviceCodeResponse) (provider.AuthorizationCodeResponse, error) {
	return provider.AuthorizationCodeResponse{}, nil
}

func (a *testAdapter) ExchangeCode(context.Context, provider.AuthorizationCodeResponse) (provider.TokenResponse, error) {
	return provider.TokenResponse{}, nil
}

func (a *testAdapter) RefreshToken(_ context.Context, refreshToken string) (provider.TokenResponse, error) {
	return a.refreshToken(refreshToken)
}

func (a *testAdapter) refreshToken(refreshToken string) (provider.TokenResponse, error) {
	a.mu.Lock()
	a.calls++
	a.refreshInput = append(a.refreshInput, refreshToken)
	first := a.calls == 1 && a.firstReused
	refreshErr := a.refreshErr
	a.mu.Unlock()
	if first {
		if a.onFirst != nil {
			a.onFirst()
		}
		return provider.TokenResponse{}, provider.ErrRefreshTokenReused
	}
	if refreshErr != nil {
		return provider.TokenResponse{}, refreshErr
	}
	time.Sleep(a.delay)
	return provider.TokenResponse{AccessToken: jwt(time.Now().Add(time.Hour)), RefreshToken: "new", ExpiresAt: time.Now().Add(time.Hour)}, nil
}

func TestInvalidGrantMarksNeedsLoginUntilSuccessfulRefresh(t *testing.T) {
	now := time.Now().UTC()
	st := &testStore{grants: map[string]store.Grant{"one": {Name: "one", ProviderName: "test", LastRefresh: now, TokenBundle: store.TokenBundle{RefreshToken: "old"}}}}
	adapter := &testAdapter{refreshErr: fmt.Errorf("provider rejected grant: %w", provider.ErrInvalidGrant)}
	b := New(st, map[string]provider.Adapter{"test": adapter}, nil, []GrantConfig{{Name: "one", ProviderName: "test"}}, nil, nil)
	if _, _, err := b.RefreshAccessToken("one", context.Background()); !errors.Is(err, provider.ErrInvalidGrant) {
		t.Fatalf("refresh error = %v, want ErrInvalidGrant", err)
	}
	if !b.NeedsLogin("one") {
		t.Fatal("NeedsLogin = false after invalid grant")
	}
	adapter.mu.Lock()
	adapter.refreshErr = nil
	adapter.mu.Unlock()
	if _, _, err := b.RefreshAccessToken("one", context.Background()); err != nil {
		t.Fatal(err)
	}
	if b.NeedsLogin("one") {
		t.Fatal("NeedsLogin = true after successful refresh")
	}
}

func jwt(exp time.Time) string {
	payload, _ := json.Marshal(map[string]int64{"exp": exp.Unix()})
	return "e." + base64.RawURLEncoding.EncodeToString(payload) + ".s"
}

func TestGetAccessTokenSingleFlightPerGrant(t *testing.T) {
	adapter := &testAdapter{delay: 100 * time.Millisecond}
	now := time.Now().UTC()
	// ExpiresAt one minute out is inside the refresh margin, so every caller
	// wants a refresh and the single-flight is what keeps it to one per grant.
	st := &testStore{grants: map[string]store.Grant{"one": {Name: "one", ProviderName: "test", LastRefresh: now, TokenBundle: store.TokenBundle{AccessToken: jwt(now.Add(time.Minute)), RefreshToken: "old", ExpiresAt: now.Add(time.Minute)}}, "two": {Name: "two", ProviderName: "test", LastRefresh: now, TokenBundle: store.TokenBundle{AccessToken: jwt(now.Add(time.Minute)), RefreshToken: "old", ExpiresAt: now.Add(time.Minute)}}}}
	b := New(st, map[string]provider.Adapter{"test": adapter}, nil, []GrantConfig{{Name: "one", ProviderName: "test"}, {Name: "two", ProviderName: "test"}}, nil, nil)
	var wg sync.WaitGroup
	for i := 0; i < 5; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if token, _, err := b.GetAccessToken("one", context.Background()); err != nil || token == "" {
				t.Errorf("one = %q, %v", token, err)
			}
		}()
	}
	wg.Add(1)
	go func() {
		defer wg.Done()
		if token, _, err := b.GetAccessToken("two", context.Background()); err != nil || token == "" {
			t.Errorf("two = %q, %v", token, err)
		}
	}()
	wg.Wait()
	adapter.mu.Lock()
	calls := adapter.calls
	adapter.mu.Unlock()
	if calls != 2 {
		t.Fatalf("refresh calls = %d, want one per grant", calls)
	}
}

func TestRefreshTokenReusedRereadsStoreAndRetriesOnce(t *testing.T) {
	for _, newLogin := range []bool{false, true} {
		t.Run(fmt.Sprintf("new-login-%t", newLogin), func(t *testing.T) {
			now := time.Now().UTC()
			st := &testStore{grants: map[string]store.Grant{"one": {Name: "one", ProviderName: "test", LastRefresh: now, TokenBundle: store.TokenBundle{RefreshToken: "old"}}}}
			adapter := &testAdapter{firstReused: true, onFirst: func() {
				st.mu.Lock()
				grant := st.grants["one"]
				grant.TokenBundle.RefreshToken = "durable"
				grant.LastRefresh = now.Add(time.Nanosecond)
				if newLogin {
					// A new login wins even when its replica's clock is behind.
					grant.LoginGeneration++
					grant.LoginID = "new-login"
					grant.LastRefresh = now.Add(-time.Hour)
				}
				st.grants["one"] = grant
				st.mu.Unlock()
			}}
			b := New(st, map[string]provider.Adapter{"test": adapter}, nil, []GrantConfig{{Name: "one", ProviderName: "test"}}, nil, nil)
			if _, _, err := b.RefreshAccessToken("one", context.Background()); err != nil {
				t.Fatal(err)
			}
			adapter.mu.Lock()
			defer adapter.mu.Unlock()
			if adapter.calls != 2 || len(adapter.refreshInput) != 2 || adapter.refreshInput[0] != "old" || adapter.refreshInput[1] != "durable" {
				t.Fatalf("refresh calls = %d, inputs = %v, want exactly one retry with durable token", adapter.calls, adapter.refreshInput)
			}
		})
	}
}

func TestConsumedGrantReplacementUsesLoginGeneration(t *testing.T) {
	for _, newer := range []bool{false, true} {
		t.Run(fmt.Sprintf("newer-login-%t", newer), func(t *testing.T) {
			now := time.Now().UTC()
			original := store.Grant{
				Name: "one", ProviderName: "test", LoginGeneration: 2, LoginID: "consumed-login", LastRefresh: now,
				TokenBundle: store.TokenBundle{RefreshToken: "consumed"},
			}
			st := &testStore{grants: map[string]store.Grant{"one": original}}
			adapter := &testAdapter{refreshErr: provider.ErrRefreshTokenReused}
			b := New(st, map[string]provider.Adapter{"test": adapter}, nil, []GrantConfig{{Name: "one", ProviderName: "test"}}, nil, nil)
			if _, _, err := b.RefreshAccessToken("one", context.Background()); !errors.Is(err, provider.ErrRefreshTokenReused) {
				t.Fatal("expected a consumed-token block")
			}
			replacement := original
			replacement.LoginGeneration, replacement.LoginID = 1, "older-login"
			replacement.LastRefresh = now.Add(time.Hour)
			replacement.TokenBundle.RefreshToken = "replacement"
			if newer {
				replacement.LoginGeneration, replacement.LoginID = 3, "newer-login"
				replacement.LastRefresh = now.Add(-time.Hour)
			}
			if err := st.SaveGrant(replacement); err != nil {
				t.Fatal(err)
			}
			if b.RefreshBlocked("one") == newer || b.NeedsLogin("one") || adapter.calls != 2 {
				t.Fatal("replacement detection ignored login generation or retried the provider")
			}
		})
	}
}

func TestRepeatedReuseBlocksProviderCallsUntilReplacement(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		now := time.Now().UTC()
		original := store.Grant{Name: "one", ProviderName: "test", LastRefresh: now.Add(-time.Hour), TokenBundle: store.TokenBundle{
			AccessToken: "expired", RefreshToken: "consumed", ExpiresAt: now.Add(-time.Minute),
		}}
		st := &testStore{grants: map[string]store.Grant{"one": original}}
		adapter := &testAdapter{refreshErr: fmt.Errorf("consumed: %w", provider.ErrRefreshTokenReused)}
		b := New(st, map[string]provider.Adapter{"test": adapter}, nil, []GrantConfig{{Name: "one", ProviderName: "test"}}, nil, nil)
		for i := 0; i < 3; i++ {
			if _, _, err := b.RefreshAccessToken("one", context.Background()); !errors.Is(err, provider.ErrRefreshTokenReused) {
				t.Fatalf("refresh error = %v, want ErrRefreshTokenReused", err)
			}
			if b.NeedsLogin("one") || !b.RefreshBlocked("one") {
				t.Fatal("consumed token must expose a recoverable refresh block")
			}
			if _, _, err := b.GetAccessToken("one", context.Background()); !errors.Is(err, ErrRefreshFailed) {
				t.Fatalf("token error = %v, want ErrRefreshFailed", err)
			}
			// A lost replacement stays explicitly blocked without sending
			// the consumed token again or assuming no writer can recover it.
			time.Sleep(24 * time.Hour)
		}
		if adapter.calls != 2 {
			t.Fatalf("provider calls = %d, want one attempt and one retry", adapter.calls)
		}
		if stored, _ := st.LoadGrant("one"); stored != original {
			t.Fatal("failed refresh changed the stored grant")
		}
		st.loadErr = errors.New("storage unavailable")
		_, _, _ = b.RefreshAccessToken("one", context.Background())
		if b.NeedsLogin("one") || !b.RefreshBlocked("one") {
			t.Fatal("storage failure changed the recovery classification")
		}
		st.loadErr = nil
		// A timestamp-only update must not unblock the consumed token.
		replacement := original
		replacement.LastRefresh = time.Now().UTC()
		_ = st.SaveGrant(replacement)
		if !b.RefreshBlocked("one") {
			t.Fatal("unchanged refresh token cleared the block")
		}
		replacement.TokenBundle.RefreshToken = "replacement"
		_ = st.SaveGrant(replacement)
		adapter.refreshErr = nil
		if _, _, err := b.RefreshAccessToken("one", context.Background()); err != nil {
			t.Fatal(err)
		}
		if b.NeedsLogin("one") || b.RefreshBlocked("one") || adapter.calls != 3 || adapter.refreshInput[2] != "replacement" {
			t.Fatal("refresh did not recover using the durable replacement")
		}
	})
}

func TestReuseRecoveryTransientFailurePreservesGrant(t *testing.T) {
	for _, storageFailure := range []bool{false, true} {
		t.Run(fmt.Sprintf("storage-failure-%t", storageFailure), func(t *testing.T) {
			original := store.Grant{Name: "one", ProviderName: "test", LastRefresh: time.Now().UTC(), TokenBundle: store.TokenBundle{
				AccessToken: "cached", RefreshToken: "old", ExpiresAt: time.Now().Add(time.Hour),
			}}
			st := &testStore{grants: map[string]store.Grant{"one": original}}
			transient := errors.New("temporarily unavailable")
			adapter := &testAdapter{firstReused: true, refreshErr: transient}
			if storageFailure {
				adapter.onFirst = func() { st.loadErr = transient }
			}
			b := New(st, map[string]provider.Adapter{"test": adapter}, nil, []GrantConfig{{Name: "one", ProviderName: "test"}}, nil, nil)
			if _, _, err := b.RefreshAccessToken("one", context.Background()); !errors.Is(err, transient) {
				t.Fatalf("refresh error = %v, want transient failure", err)
			}
			if b.NeedsLogin("one") {
				t.Fatal("transient recovery failure required login")
			}
			st.loadErr = nil
			if stored, _ := st.LoadGrant("one"); stored != original {
				t.Fatal("transient failure changed the stored grant")
			}
			if access, _, err := b.CachedAccessToken("one", context.Background()); err != nil || access != "cached" {
				t.Fatal("transient failure lost the cached access token")
			}
			adapter.refreshErr = nil
			if _, _, err := b.RefreshAccessToken("one", context.Background()); err != nil {
				t.Fatal(err)
			}
		})
	}
}

func TestReuseRecoveryDoesNotRetryOlderDurableGrant(t *testing.T) {
	now := time.Now().UTC()
	st := &testStore{grants: map[string]store.Grant{"one": {Name: "one", ProviderName: "test", LoginGeneration: 1, LastRefresh: now.Add(time.Hour), TokenBundle: store.TokenBundle{RefreshToken: "old"}}}}
	adapter := &testAdapter{refreshErr: provider.ErrRefreshTokenReused}
	b := New(st, map[string]provider.Adapter{"test": adapter}, nil, []GrantConfig{{Name: "one", ProviderName: "test"}}, nil, nil)
	s, _ := b.state("one")
	s.authoritative = &store.Grant{Name: "one", ProviderName: "test", LoginGeneration: 2, LastRefresh: now, TokenBundle: store.TokenBundle{RefreshToken: "rotated"}}
	for i := 0; i < 3; i++ {
		if _, _, err := b.RefreshAccessToken("one", context.Background()); !errors.Is(err, provider.ErrRefreshTokenReused) {
			t.Fatalf("refresh error = %v", err)
		}
		if b.NeedsLogin("one") {
			t.Fatal("lagging durable storage was classified as a lost rotation")
		}
	}
	if adapter.calls != 2 || adapter.refreshInput[0] != "rotated" || adapter.refreshInput[1] != "rotated" {
		t.Fatal("reuse recovery retried an older token or exceeded its retry bound")
	}
}

type delayedStore struct {
	*testStore
	mu       sync.Mutex
	pending  store.Grant
	writable bool
	attempts int
}

func (s *delayedStore) SaveGrantIfNewer(g store.Grant) error {
	s.mu.Lock()
	s.pending = g
	s.attempts++
	writable := s.writable
	s.mu.Unlock()
	if !writable {
		return errors.New("Secret updates unavailable")
	}
	return s.testStore.SaveGrantIfNewer(g)
}

func (s *delayedStore) allowWrites() {
	s.mu.Lock()
	s.writable = true
	s.mu.Unlock()
}

func TestLoginClearsConsumedBlockOnlyAfterPersistence(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		now := time.Now().UTC()
		st := &testStore{grants: map[string]store.Grant{"one": {
			Name: "one", ProviderName: "test", LastRefresh: now,
			TokenBundle: store.TokenBundle{RefreshToken: "consumed"},
		}}}
		delayed := &delayedStore{testStore: st}
		adapter := &testAdapter{refreshErr: provider.ErrRefreshTokenReused}
		b := New(delayed, map[string]provider.Adapter{"test": adapter}, nil, []GrantConfig{{Name: "one", ProviderName: "test"}}, nil, nil)
		if _, _, err := b.RefreshAccessToken("one", context.Background()); !errors.Is(err, provider.ErrRefreshTokenReused) {
			t.Fatal("expected a consumed-token block")
		}
		login := store.Grant{Name: "one", ProviderName: "test", TokenBundle: store.TokenBundle{
			AccessToken: "login-access", RefreshToken: "login-refresh", ExpiresAt: now.Add(time.Hour),
		}}
		if err := b.SaveGrant(login); !errors.Is(err, ErrGrantPersistencePending) || !b.RefreshBlocked("one") {
			t.Fatal("an unsaved login must leave the consumed-token block intact")
		}
		delayed.allowWrites()
		if err := b.RetryPendingGrant("one"); err != nil {
			t.Fatal(err)
		}
		// Successful persistence must clear the marker itself, without needing
		// a later status read to rediscover the saved login.
		st.loadErr = errors.New("storage reads unavailable")
		if b.RefreshBlocked("one") || b.NeedsLogin("one") || adapter.calls != 2 {
			t.Fatal("saving a new login did not clear the block or retried the provider")
		}
	})
}

type rotatingAdapter struct {
	testAdapter
	current string
}

func (a *rotatingAdapter) RefreshToken(_ context.Context, token string) (provider.TokenResponse, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.calls++
	if token != a.current {
		return provider.TokenResponse{}, provider.ErrRefreshTokenReused
	}
	a.current += "-next"
	return provider.TokenResponse{AccessToken: jwt(time.Now().Add(time.Hour)), RefreshToken: a.current, ExpiresAt: time.Now().Add(time.Hour)}, nil
}

func TestOverlappingRotationDelayedWriteNeverRequiresLogin(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		now := time.Now().UTC()
		st := &testStore{grants: map[string]store.Grant{"one": {
			Name: "one", ProviderName: "test", LastRefresh: now.Add(-time.Hour),
			TokenBundle: store.TokenBundle{AccessToken: "expired", RefreshToken: "old", ExpiresAt: now.Add(-time.Minute)},
		}}}
		delayed := &delayedStore{testStore: st}
		// Let the background persistence goroutine finish even on failure.
		defer func() {
			delayed.allowWrites()
			time.Sleep(30 * time.Second)
			synctest.Wait()
		}()
		adapter := &rotatingAdapter{current: "old"}
		adapters := map[string]provider.Adapter{"test": adapter}
		configs := []GrantConfig{{Name: "one", ProviderName: "test"}}
		winner := New(delayed, adapters, nil, configs, nil, nil)
		other := New(st, adapters, nil, configs, nil, nil)
		if _, _, err := winner.RefreshAccessToken("one", context.Background()); err == nil {
			t.Fatal("rotation should await persistence while updates fail")
		}
		// Reads work while every foreground and background Secret update
		// fails. Cross the former five-minute deadline with no saved token.
		for i := 0; i < 3; i++ {
			if _, _, err := other.GetAccessToken("one", context.Background()); !errors.Is(err, ErrRefreshFailed) || other.NeedsLogin("one") || !other.RefreshBlocked("one") {
				t.Fatalf("overlapping refresh became a permanent credential block: %v", err)
			}
			time.Sleep(5 * time.Minute)
		}
		delayed.mu.Lock()
		pending, attempts := delayed.pending, delayed.attempts
		delayed.mu.Unlock()
		if attempts <= 5 {
			t.Fatal("test did not exercise background persistence retries")
		}
		delayed.allowWrites()
		time.Sleep(30 * time.Second)
		synctest.Wait()
		if access, _, err := other.GetAccessToken("one", context.Background()); err != nil || access != pending.TokenBundle.AccessToken || other.NeedsLogin("one") || other.RefreshBlocked("one") {
			t.Fatalf("delayed durable replacement did not recover: %v", err)
		}
		adapter.mu.Lock()
		defer adapter.mu.Unlock()
		if adapter.calls != 3 {
			t.Fatalf("provider calls = %d, want one rotation and two reuse responses", adapter.calls)
		}
	})
}

func TestRefreshAccessTokenUsesRefreshTokenWhenAvailable(t *testing.T) {
	st := &testStore{grants: map[string]store.Grant{"one": {Name: "one", ProviderName: "test", TokenBundle: store.TokenBundle{RefreshToken: "old"}}}}
	adapter := &testDualAdapter{testAdapter: &testAdapter{}}
	b := New(st, map[string]provider.Adapter{"test": adapter}, nil, []GrantConfig{{Name: "one", ProviderName: "test"}}, nil, nil)

	if _, _, err := b.RefreshAccessToken("one", context.Background()); err != nil {
		t.Fatal(err)
	}
	adapter.mu.Lock()
	defer adapter.mu.Unlock()
	if adapter.calls != 1 || adapter.mintCalls != 0 {
		t.Fatalf("RefreshToken calls = %d, Mint calls = %d, want 1 and 0", adapter.calls, adapter.mintCalls)
	}
}

func TestGetAccessTokenProactiveSevenDayRefresh(t *testing.T) {
	now := time.Now().UTC()
	st := &testStore{grants: map[string]store.Grant{"one": {Name: "one", ProviderName: "test", LastRefresh: now.Add(-8 * 24 * time.Hour), TokenBundle: store.TokenBundle{AccessToken: jwt(now.Add(2 * time.Hour)), RefreshToken: "old", ExpiresAt: now.Add(2 * time.Hour)}}}}
	adapter := &testAdapter{}
	b := New(st, map[string]provider.Adapter{"test": adapter}, nil, []GrantConfig{{Name: "one", ProviderName: "test"}}, nil, nil)
	if _, _, err := b.GetAccessToken("one", context.Background()); err != nil {
		t.Fatal(err)
	}
	adapter.mu.Lock()
	defer adapter.mu.Unlock()
	if adapter.calls != 1 {
		t.Fatalf("refresh calls = %d, want proactive refresh after seven days", adapter.calls)
	}
}

func TestGetAccessTokenEmptyStore(t *testing.T) {
	st := &testStore{grants: map[string]store.Grant{"one": {Name: "one"}}}
	b := New(st, map[string]provider.Adapter{}, nil, []GrantConfig{{Name: "one", ProviderName: "test"}}, nil, nil)
	if _, _, err := b.GetAccessToken("one", context.Background()); !errors.Is(err, ErrNoGrant) {
		t.Fatalf("error = %v, want ErrNoGrant", err)
	}
}

func TestRefreshAccessTokenMintsWithoutRefreshToken(t *testing.T) {
	now := time.Now().UTC()
	st := &testStore{grants: map[string]store.Grant{"one": {Name: "one", ProviderName: "minter"}}}
	minter := &testMinter{token: provider.TokenResponse{AccessToken: "minted", ExpiresAt: now.Add(time.Hour)}}
	b := New(st, nil, map[string]provider.Minter{"minter": minter}, []GrantConfig{{Name: "one", ProviderName: "minter"}}, nil, nil)

	access, expiry, err := b.RefreshAccessToken("one", context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if access != "minted" {
		t.Fatal("refresh access token does not match minted token")
	}
	if !expiry.Equal(minter.token.ExpiresAt) {
		t.Fatalf("refresh expiry = %v, want %v", expiry, minter.token.ExpiresAt)
	}
	minter.mu.Lock()
	defer minter.mu.Unlock()
	if minter.calls != 1 {
		t.Fatalf("Mint calls = %d, want 1", minter.calls)
	}
}

func TestGetAccessTokenCachesMintedToken(t *testing.T) {
	now := time.Now().UTC()
	st := &testStore{grants: map[string]store.Grant{"one": {Name: "one", ProviderName: "minter"}}}
	minter := &testMinter{token: provider.TokenResponse{AccessToken: "minted", ExpiresAt: now.Add(time.Hour)}}
	b := New(st, nil, map[string]provider.Minter{"minter": minter}, []GrantConfig{{Name: "one", ProviderName: "minter"}}, nil, nil)

	for i := 0; i < 2; i++ {
		access, expiry, err := b.GetAccessToken("one", context.Background())
		if err != nil {
			t.Fatal(err)
		}
		if access != "minted" {
			t.Fatal("GetAccessToken result does not match minted token")
		}
		if !expiry.Equal(minter.token.ExpiresAt) {
			t.Fatalf("GetAccessToken expiry = %v, want %v", expiry, minter.token.ExpiresAt)
		}
	}
	minter.mu.Lock()
	defer minter.mu.Unlock()
	if minter.calls != 1 {
		t.Fatalf("Mint calls = %d, want 1", minter.calls)
	}
}

func TestCachedAccessTokenNeverRefreshesOrMints(t *testing.T) {
	for _, tc := range []struct {
		name    string
		token   string
		expires time.Duration
		wantOK  bool
	}{
		{"valid", "cached", time.Hour, true},
		{"near-expiry", "cached", 30 * time.Second, false},
		{"expired", "cached", -time.Hour, false},
		{"missing", "", time.Hour, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			now := time.Now()
			st := &testStore{grants: map[string]store.Grant{"one": {Name: "one", ProviderName: "test", LastRefresh: now.Add(-8 * 24 * time.Hour), TokenBundle: store.TokenBundle{AccessToken: tc.token, RefreshToken: "existing", ExpiresAt: now.Add(tc.expires)}}}}
			adapter := &testDualAdapter{testAdapter: &testAdapter{}}
			b := New(st, map[string]provider.Adapter{"test": adapter}, map[string]provider.Minter{"test": adapter}, []GrantConfig{{Name: "one", ProviderName: "test"}}, nil, nil)
			token, _, err := b.CachedAccessToken("one", context.Background())
			if (err == nil) != tc.wantOK || (tc.wantOK && token != tc.token) {
				t.Fatalf("cached token: success=%t want=%t", err == nil, tc.wantOK)
			}
			if adapter.calls != 0 || adapter.mintCalls != 0 {
				t.Fatal("cached token path performed OAuth refresh or mint")
			}
		})
	}
}

func TestCacheAvailabilityUsesServingPolicyWithoutRefresh(t *testing.T) {
	for _, tc := range []struct {
		name          string
		token         string
		expires       time.Duration
		age           time.Duration
		refreshBefore time.Duration
		maxAge        time.Duration
		want          bool
	}{
		{name: "usable", token: "cached", expires: time.Hour, age: time.Hour, want: true},
		{name: "missing", expires: time.Hour, age: time.Hour},
		{name: "expired", token: "cached", expires: -time.Minute, age: time.Hour},
		{name: "refresh-window", token: "cached", expires: 4 * time.Minute, age: time.Hour},
		{name: "aged", token: "cached", expires: time.Hour, age: 8 * 24 * time.Hour},
		{name: "custom-window", token: "cached", expires: time.Hour, age: time.Hour, refreshBefore: 2 * time.Hour},
		{name: "custom-age", token: "cached", expires: time.Hour, age: time.Hour, maxAge: 30 * time.Minute},
	} {
		t.Run(tc.name, func(t *testing.T) {
			now := time.Now().UTC()
			original := store.Grant{Name: "one", ProviderName: "test", LastRefresh: now.Add(-tc.age), TokenBundle: store.TokenBundle{
				AccessToken: tc.token, RefreshToken: "existing", ExpiresAt: now.Add(tc.expires),
			}}
			st := &testStore{grants: map[string]store.Grant{"one": original}}
			adapter := &testDualAdapter{testAdapter: &testAdapter{}}
			b := New(st, map[string]provider.Adapter{"test": adapter}, map[string]provider.Minter{"test": adapter}, []GrantConfig{{Name: "one", ProviderName: "test"}}, nil, nil)
			b.SetRefreshPolicy(tc.refreshBefore, tc.maxAge)
			if got := b.CanServeCachedAccessToken("one"); got != tc.want {
				t.Fatalf("cache availability = %t, want %t", got, tc.want)
			}
			if adapter.calls != 0 || adapter.mintCalls != 0 || st.grants["one"] != original {
				t.Fatal("cache availability changed credentials or called the provider")
			}
			st.loadErr = errors.New("storage unavailable")
			if b.CanServeCachedAccessToken("one") || b.CanServeCachedAccessToken("unknown") {
				t.Fatal("unreadable or unknown grant reported a usable cache")
			}
		})
	}
}

func TestRefreshPolicy(t *testing.T) {
	now := time.Now().UTC()
	grant := func() *testStore {
		return &testStore{grants: map[string]store.Grant{"one": {Name: "one", ProviderName: "test", LastRefresh: now.Add(-time.Hour), TokenBundle: store.TokenBundle{AccessToken: "current", RefreshToken: "old", ExpiresAt: now.Add(20 * time.Hour)}}}}
	}
	adapter := &testAdapter{}
	b := New(grant(), map[string]provider.Adapter{"test": adapter}, nil, []GrantConfig{{Name: "one", ProviderName: "test"}}, nil, nil)
	if token, _, err := b.GetAccessToken("one", context.Background()); err != nil || token != "current" {
		t.Fatalf("default policy GetAccessToken = %q, %v; want the stored token", token, err)
	}
	b = New(grant(), map[string]provider.Adapter{"test": adapter}, nil, []GrantConfig{{Name: "one", ProviderName: "test"}}, nil, nil)
	b.SetRefreshPolicy(24*time.Hour, 0)
	if token, _, err := b.GetAccessToken("one", context.Background()); err != nil || token == "current" {
		t.Fatalf("24h policy GetAccessToken = %q, %v; want a refreshed token", token, err)
	}
	st := grant()
	g := st.grants["one"]
	g.TokenBundle.ExpiresAt = now.Add(10 * 24 * time.Hour)
	g.LastRefresh = now.Add(-25 * time.Hour)
	st.grants["one"] = g
	b = New(st, map[string]provider.Adapter{"test": adapter}, nil, []GrantConfig{{Name: "one", ProviderName: "test"}}, nil, nil)
	b.SetRefreshPolicy(0, 24*time.Hour)
	if token, _, err := b.GetAccessToken("one", context.Background()); err != nil || token == "current" {
		t.Fatalf("max-age policy GetAccessToken = %q, %v; want a refreshed token", token, err)
	}
}

type blockedRefreshAdapter struct {
	*testAdapter
	started, release chan struct{}
	err              error
}

func (a *blockedRefreshAdapter) RefreshToken(context.Context, string) (provider.TokenResponse, error) {
	close(a.started)
	<-a.release
	return provider.TokenResponse{AccessToken: "older-refresh-access", RefreshToken: "older-refresh-token", ExpiresAt: time.Now().Add(time.Hour)}, a.err
}

func loginGrant() store.Grant {
	now := time.Now().UTC()
	return store.Grant{Name: "one", ProviderName: "test", LastRefresh: now, TokenBundle: store.TokenBundle{
		IDToken: "login-id", AccessToken: "login-access", RefreshToken: "login-refresh", AccountID: "login-account",
		LastRefresh: now, ExpiresAt: now.Add(24 * time.Hour),
	}}
}

func TestLoginWaitsForRefreshAndReplacesItsResult(t *testing.T) {
	for _, refreshErr := range []error{nil, provider.ErrInvalidGrant} {
		for _, proactive := range []bool{false, true} {
			t.Run(fmt.Sprintf("proactive=%t/invalid=%t", proactive, refreshErr != nil), func(t *testing.T) {
				synctest.Test(t, func(t *testing.T) {
					old := loginGrant()
					old.LastRefresh = time.Now().Add(-time.Hour)
					old.TokenBundle.ExpiresAt = time.Now().Add(time.Minute)
					st := &testStore{grants: map[string]store.Grant{"one": old}}
					a := &blockedRefreshAdapter{testAdapter: &testAdapter{}, started: make(chan struct{}), release: make(chan struct{}), err: refreshErr}
					b := New(st, map[string]provider.Adapter{"test": a}, nil, []GrantConfig{{Name: "one", ProviderName: "test"}}, nil, nil)
					refreshed := make(chan struct{})
					go func() {
						defer close(refreshed)
						if proactive {
							_, _, _ = b.GetAccessToken("one", context.Background())
						} else {
							_, _, _ = b.RefreshAccessToken("one", context.Background())
						}
					}()
					<-a.started
					login := loginGrant()
					saved := make(chan error, 1)
					go func() { saved <- b.SaveGrant(login) }()
					synctest.Wait()
					select {
					case <-saved:
						t.Fatal("login did not wait for the refresh")
					default:
					}
					// The refresh completes after the exchange, so its timestamp
					// would reject the login if the exchange time were retained.
					time.Sleep(time.Second)
					close(a.release)
					<-refreshed
					if err := <-saved; err != nil {
						t.Fatal(err)
					}
					g, _ := st.LoadGrant("one")
					if g.TokenBundle.AccessToken != login.TokenBundle.AccessToken || g.TokenBundle.RefreshToken != login.TokenBundle.RefreshToken || !g.LastRefresh.After(login.LastRefresh) || b.NeedsLogin("one") {
						t.Fatal("refresh result or login-required state survived the login")
					}
					if access, _, err := b.GetAccessToken("one", context.Background()); err != nil || access != login.TokenBundle.AccessToken {
						t.Fatal("token serving did not adopt the login")
					}
				})
			})
		}
	}
}

type failingLoginStore struct {
	*testStore
	failures  int
	timeout   bool
	ambiguous bool
	attempts  []store.Grant
}

func (s *failingLoginStore) SaveGrantIfNewerContext(ctx context.Context, g store.Grant) error {
	s.attempts = append(s.attempts, g)
	if s.failures > 0 {
		s.failures--
		if s.timeout {
			<-ctx.Done()
		}
		// Deliberately include sensitive data to verify it never escapes.
		return fmt.Errorf("Secret write failed: %s", g.TokenBundle.RefreshToken)
	}
	err := s.testStore.SaveGrantIfNewer(g)
	if s.ambiguous {
		s.ambiguous = false
		return errors.New("update response lost")
	}
	return err
}

func TestLoginPersistenceIsBoundedAndRecoverable(t *testing.T) {
	for _, timeout := range []bool{false, true} {
		t.Run(fmt.Sprintf("timeout=%t", timeout), func(t *testing.T) {
			synctest.Test(t, func(t *testing.T) {
				st := &failingLoginStore{testStore: &testStore{grants: map[string]store.Grant{}}, failures: 5, timeout: timeout}
				a := &testAdapter{}
				b := New(st, map[string]provider.Adapter{"test": a}, nil, []GrantConfig{{Name: "one", ProviderName: "test"}}, nil, nil)
				login := loginGrant()
				start := time.Now()
				if err := b.SaveGrant(login); err != ErrGrantPersistencePending {
					t.Fatal("failed persistence did not return the fixed pending error")
				}
				budget := 3750 * time.Millisecond
				if timeout {
					budget += 50 * time.Second
				}
				if len(st.attempts) != 5 || time.Since(start) != budget {
					t.Fatal("persistence exceeded its attempt or time budget")
				}
				if _, _, err := b.RefreshAccessToken("one", context.Background()); err != ErrGrantPersistencePending || a.calls != 0 {
					t.Fatal("refresh used the old grant while login persistence was pending")
				}
				if err := b.SaveGrant(loginGrant()); err != ErrGrantPersistencePending {
					t.Fatal("another login replaced the retained bundle")
				}
				if err := b.RetryPendingGrant("one"); err != nil {
					t.Fatal(err)
				}
				if len(st.attempts) != 6 {
					t.Fatal("retry did not make exactly one successful write")
				}
				for _, attempt := range st.attempts {
					if attempt != st.attempts[0] {
						t.Fatal("persistence changed the retained bundle or its ordering")
					}
				}
				g, _ := st.LoadGrant("one")
				if g.TokenBundle.RefreshToken != login.TokenBundle.RefreshToken {
					t.Fatal("recovered grant differs from the exchanged login")
				}
			})
		})
	}
}

func TestLoginConfirmsAmbiguousWrite(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		st := &failingLoginStore{testStore: &testStore{grants: map[string]store.Grant{}}, ambiguous: true}
		b := New(st, nil, nil, []GrantConfig{{Name: "one"}}, nil, nil)
		if err := b.SaveGrant(loginGrant()); err != nil {
			t.Fatal(err)
		}
		if len(st.attempts) != 2 {
			t.Fatal("ambiguous commit was not confirmed on retry")
		}
	})
}

type delayedRotationStore struct {
	*testStore
	rotationWrites int
}

func (s *delayedRotationStore) SaveGrantIfNewer(g store.Grant) error {
	if g.TokenBundle.RefreshToken == "new" {
		s.rotationWrites++
		if s.rotationWrites <= 5 {
			return errors.New("temporarily unavailable")
		}
	}
	return s.testStore.SaveGrantIfNewer(g)
}

func TestLoginFencesDetachedRotationRetry(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		st := &delayedRotationStore{testStore: &testStore{grants: map[string]store.Grant{"one": loginGrant()}}}
		b := New(st, map[string]provider.Adapter{"test": &testAdapter{}}, nil, []GrantConfig{{Name: "one", ProviderName: "test"}}, nil, nil)
		if _, _, err := b.RefreshAccessToken("one", context.Background()); err == nil {
			t.Fatal("rotation unexpectedly persisted")
		}
		login := loginGrant()
		if err := b.SaveGrant(login); err != nil {
			t.Fatal(err)
		}
		time.Sleep(30 * time.Second)
		synctest.Wait()
		g, _ := st.LoadGrant("one")
		if st.rotationWrites != 6 || g.TokenBundle.RefreshToken != login.TokenBundle.RefreshToken {
			t.Fatal("detached older rotation overwrote the login")
		}
	})
}

type competingLoginStore struct {
	*testStore
	scenario string
	wrote    bool
}

func (s *competingLoginStore) SaveGrantIfNewerContext(_ context.Context, g store.Grant) error {
	if !s.wrote {
		s.wrote = true
		competing := g
		competing.LastRefresh = g.LastRefresh.Add(time.Hour)
		competing.TokenBundle.LastRefresh = competing.LastRefresh
		competing.TokenBundle.AccessToken = "competing-access"
		competing.TokenBundle.RefreshToken = "competing-refresh"
		switch s.scenario {
		case "older login refresh":
			competing.LoginID, competing.LoginAt = "previous-login", g.LoginAt.Add(time.Hour)
			competing.LoginGeneration--
		case "later login":
			competing.LoginID, competing.LoginAt = "later-login", g.LoginAt.Add(-time.Hour)
			competing.LoginGeneration++
		case "concurrent login":
			competing.LoginID = "concurrent-login"
		}
		// For "rotated login", simulate a committed write whose response was
		// lost, followed by another replica refreshing that same authorization.
		if err := s.testStore.SaveGrant(competing); err != nil {
			return err
		}
		return errors.New("write response lost")
	}
	return s.testStore.SaveGrantIfNewer(g)
}

func TestLoginReconcilesCompetingWrites(t *testing.T) {
	for _, scenario := range []string{"older login refresh", "rotated login", "later login", "concurrent login"} {
		t.Run(scenario, func(t *testing.T) {
			synctest.Test(t, func(t *testing.T) {
				st := &competingLoginStore{testStore: &testStore{grants: map[string]store.Grant{}}, scenario: scenario}
				a := &testAdapter{}
				b := New(st, map[string]provider.Adapter{"test": a}, nil, []GrantConfig{{Name: "one", ProviderName: "test"}}, nil, nil)
				login := loginGrant()
				if err := b.SaveGrant(login); err != nil {
					t.Fatal("competing write stranded login persistence")
				}
				g, _ := st.LoadGrant("one")
				want := "competing-refresh"
				if scenario == "older login refresh" {
					want = login.TokenBundle.RefreshToken
				}
				if g.TokenBundle.RefreshToken != want || g.LoginID == "" {
					t.Fatal("reconciliation selected the wrong authorization")
				}
				if access, _, err := b.GetAccessToken("one", context.Background()); err != nil || access != g.TokenBundle.AccessToken {
					t.Fatal("token serving did not adopt the reconciled grant")
				}
				if _, _, err := b.RefreshAccessToken("one", context.Background()); err != nil || len(a.refreshInput) != 1 || a.refreshInput[0] != want {
					t.Fatal("recovery left refresh blocked or replayed an older credential")
				}
			})
		})
	}
}

func TestLoginGenerationIgnoresReplicaClockSkew(t *testing.T) {
	for _, generation := range []uint64{0, 17} {
		t.Run(fmt.Sprintf("stored-generation=%d", generation), func(t *testing.T) {
			synctest.Test(t, func(t *testing.T) {
				old := loginGrant()
				old.LoginID, old.LoginGeneration = "ahead-replica", generation
				old.LoginAt = time.Now().UTC().Add(24 * time.Hour)
				old.LastRefresh = old.LoginAt
				st := &failingLoginStore{testStore: &testStore{grants: map[string]store.Grant{"one": old}}, failures: 5}
				b := New(st, nil, nil, []GrantConfig{{Name: "one", ProviderName: "test"}}, nil, nil)
				login := loginGrant()
				login.TokenBundle.RefreshToken = "approved-on-behind-replica"
				if err := b.SaveGrant(login); !errors.Is(err, ErrGrantPersistencePending) {
					t.Fatal("failed writes did not retain the approved login")
				}
				if err := b.RetryPendingGrant("one"); err != nil {
					t.Fatal("retry did not install the approved login")
				}
				saved, _ := st.LoadGrant("one")
				if saved.LoginGeneration != generation+1 || !saved.LoginAt.Before(old.LoginAt) || saved.TokenBundle.RefreshToken != login.TokenBundle.RefreshToken {
					t.Fatal("replica clock skew discarded the newly approved login")
				}
				old.LastRefresh = saved.LastRefresh.Add(time.Hour)
				if err := st.SaveGrantIfNewer(old); !errors.Is(err, store.ErrGrantNotNewer) {
					t.Fatal("ahead replica's older refresh replaced the new login")
				}
			})
		})
	}
}

func TestLoginFencesAnotherReplicaRefresh(t *testing.T) {
	for _, refreshErr := range []error{nil, provider.ErrInvalidGrant} {
		t.Run(fmt.Sprintf("invalid=%t", refreshErr != nil), func(t *testing.T) {
			synctest.Test(t, func(t *testing.T) {
				st := &testStore{grants: map[string]store.Grant{"one": loginGrant()}}
				a := &blockedRefreshAdapter{testAdapter: &testAdapter{}, started: make(chan struct{}), release: make(chan struct{}), err: refreshErr}
				remote := New(st, map[string]provider.Adapter{"test": a}, nil, []GrantConfig{{Name: "one", ProviderName: "test"}}, nil, nil)
				local := New(st, nil, nil, []GrantConfig{{Name: "one", ProviderName: "test"}}, nil, nil)
				refreshed := make(chan error, 1)
				go func() {
					_, _, err := remote.RefreshAccessToken("one", context.Background())
					refreshed <- err
				}()
				<-a.started
				login := loginGrant()
				if err := local.SaveGrant(login); err != nil {
					t.Fatal(err)
				}
				time.Sleep(time.Second)
				close(a.release)
				if err := <-refreshed; err != nil || remote.NeedsLogin("one") {
					t.Fatal("old refresh did not adopt the replacement login")
				}
				g, _ := st.LoadGrant("one")
				if g.TokenBundle.RefreshToken != login.TokenBundle.RefreshToken {
					t.Fatal("late refresh from another replica overwrote the new login")
				}
			})
		})
	}
}

type stuckRefreshStore struct {
	*testStore
	operation string
	started   chan struct{}
	fenced    chan struct{}
	once      sync.Once
	released  bool
}

func (s *stuckRefreshStore) stall(ctx context.Context) {
	s.once.Do(func() { close(s.started) })
	<-ctx.Done()
	s.released = true
}

func (s *stuckRefreshStore) LoadGrantContext(ctx context.Context, name string) (store.Grant, error) {
	if s.operation == "load" && !s.released {
		s.stall(ctx)
		return store.Grant{}, ctx.Err()
	}
	return s.testStore.LoadGrant(name)
}

func (s *stuckRefreshStore) SaveGrantIfNewerContext(ctx context.Context, g store.Grant) error {
	if s.operation == "save" && g.LoginID == "" {
		current, _ := s.testStore.LoadGrant(g.Name)
		if store.CompareGrantVersion(g, current) <= 0 {
			close(s.fenced)
			return store.ErrGrantNotNewer
		}
		s.stall(ctx)
		return ctx.Err()
	}
	return s.testStore.SaveGrantIfNewer(g)
}

func TestStuckRefreshStorageReleasesWaitingLogin(t *testing.T) {
	for _, operation := range []string{"load", "save"} {
		t.Run(operation, func(t *testing.T) {
			synctest.Test(t, func(t *testing.T) {
				st := &stuckRefreshStore{testStore: &testStore{grants: map[string]store.Grant{"one": loginGrant()}}, operation: operation, started: make(chan struct{}), fenced: make(chan struct{})}
				b := New(st, map[string]provider.Adapter{"test": &testAdapter{}}, nil, []GrantConfig{{Name: "one", ProviderName: "test"}}, nil, nil)
				refreshed := make(chan struct{})
				go func() {
					defer close(refreshed)
					_, _, _ = b.RefreshAccessToken("one", context.Background())
				}()
				<-st.started
				start := time.Now()
				if err := b.SaveGrant(loginGrant()); err != nil {
					t.Fatal("stuck refresh prevented the exchanged login from being installed")
				}
				<-refreshed
				if time.Since(start) > time.Minute || !st.released {
					t.Fatal("refresh Secret request was not bounded")
				}
				if operation == "save" {
					// The timed-out rotation retries in the background. Keep the
					// bubble alive until it observes the new login and exits;
					// returning with its sleep pending deadlocks synctest teardown.
					select {
					case <-st.fenced:
					case <-time.After(time.Minute):
						t.Fatal("detached rotation did not observe the replacement login")
					}
					synctest.Wait()
				}
			})
		})
	}
}
