package broker

import (
	"context"
	"encoding/base64"
	"encoding/json"
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
	mu    sync.Mutex
	calls int
	delay time.Duration
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

func (a *testAdapter) RefreshToken(context.Context, string) (provider.TokenResponse, error) {
	a.mu.Lock()
	a.calls++
	a.mu.Unlock()
	time.Sleep(a.delay)
	return provider.TokenResponse{AccessToken: jwt(time.Now().Add(time.Hour)), RefreshToken: "new"}, nil
}

func jwt(exp time.Time) string {
	payload, _ := json.Marshal(map[string]int64{"exp": exp.Unix()})
	return "e." + base64.RawURLEncoding.EncodeToString(payload) + ".s"
}

func TestGetAccessTokenSingleFlightPerGrant(t *testing.T) {
	adapter := &testAdapter{delay: 100 * time.Millisecond}
	now := time.Now().UTC()
	st := &testStore{grants: map[string]store.Grant{"one": {Name: "one", ProviderName: "test", LastRefresh: now, TokenBundle: store.TokenBundle{AccessToken: jwt(now.Add(time.Minute)), RefreshToken: "old"}}, "two": {Name: "two", ProviderName: "test", LastRefresh: now, TokenBundle: store.TokenBundle{AccessToken: jwt(now.Add(time.Minute)), RefreshToken: "old"}}}}
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
