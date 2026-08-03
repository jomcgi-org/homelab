package broker

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"sync"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/embervm/tokenbroker/internal/provider"
	"github.com/jomcgi/homelab/projects/embervm/tokenbroker/internal/store"
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
func (s *testStore) SaveGrantIfNewer(g store.Grant) error { return s.SaveGrant(g) }

type testAdapter struct {
	mu           sync.Mutex
	calls        int
	refreshInput []string
	delay        time.Duration
	firstReused  bool
	onFirst      func()
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
	a.mu.Unlock()
	if first {
		if a.onFirst != nil {
			a.onFirst()
		}
		return provider.TokenResponse{}, provider.ErrRefreshTokenReused
	}
	time.Sleep(a.delay)
	return provider.TokenResponse{AccessToken: jwt(time.Now().Add(time.Hour)), RefreshToken: "new", ExpiresAt: time.Now().Add(time.Hour)}, nil
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
	b := New(st, map[string]provider.Adapter{"test": adapter}, []GrantConfig{{Name: "one", ProviderName: "test"}, {Name: "two", ProviderName: "test"}}, nil, nil)
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
	b := New(st, map[string]provider.Adapter{"test": adapter}, []GrantConfig{{Name: "one", ProviderName: "test"}}, nil, nil)
	if _, _, err := b.RefreshAccessToken("one", context.Background()); err != nil {
		t.Fatal(err)
	}
	adapter.mu.Lock()
	defer adapter.mu.Unlock()
	if adapter.calls != 2 || len(adapter.refreshInput) != 2 || adapter.refreshInput[0] != "old" || adapter.refreshInput[1] != "durable" {
		t.Fatalf("refresh calls = %d, inputs = %v, want exactly one retry with durable token", adapter.calls, adapter.refreshInput)
	}
}

func TestGetAccessTokenProactiveSevenDayRefresh(t *testing.T) {
	now := time.Now().UTC()
	st := &testStore{grants: map[string]store.Grant{"one": {Name: "one", ProviderName: "test", LastRefresh: now.Add(-8 * 24 * time.Hour), TokenBundle: store.TokenBundle{AccessToken: jwt(now.Add(2 * time.Hour)), RefreshToken: "old", ExpiresAt: now.Add(2 * time.Hour)}}}}
	adapter := &testAdapter{}
	b := New(st, map[string]provider.Adapter{"test": adapter}, []GrantConfig{{Name: "one", ProviderName: "test"}}, nil, nil)
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
	b := New(st, map[string]provider.Adapter{}, []GrantConfig{{Name: "one", ProviderName: "test"}}, nil, nil)
	if _, _, err := b.GetAccessToken("one", context.Background()); !errors.Is(err, ErrNoGrant) {
		t.Fatalf("error = %v, want ErrNoGrant", err)
	}
}
