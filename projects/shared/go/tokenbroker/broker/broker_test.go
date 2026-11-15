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
	mu     sync.Mutex
	grants map[string]store.Grant
}

func (s *testStore) LoadGrant(name string) (store.Grant, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.grants[name], nil
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
	if previous := s.grants[g.Name]; !previous.LastRefresh.IsZero() && !g.LastRefresh.After(previous.LastRefresh) {
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
	now := time.Now().UTC()
	st := &testStore{grants: map[string]store.Grant{"one": {Name: "one", ProviderName: "test", LastRefresh: now, TokenBundle: store.TokenBundle{RefreshToken: "old"}}}}
	adapter := &testAdapter{firstReused: true, onFirst: func() {
		st.mu.Lock()
		grant := st.grants["one"]
		grant.TokenBundle.RefreshToken = "durable"
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
