package grant

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/factory/broker/internal/fakes"
	"github.com/jomcgi/homelab/projects/factory/broker/metrics"
	"github.com/jomcgi/homelab/projects/shared/go/tokenbroker/provider/codexchatgpt"
	"github.com/jomcgi/homelab/projects/shared/go/tokenbroker/store"
	"github.com/prometheus/client_golang/prometheus/testutil"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/client-go/kubernetes/fake"
	k8stesting "k8s.io/client-go/testing"
)

const (
	namespace = "factory"
	prefix    = "factory-broker-grant-"
	grantName = "codex-primary"
)

type harness struct {
	auth    *fakes.OpenAIAuth
	client  *fake.Clientset
	store   *store.SecretStore
	metrics *metrics.Metrics
	mgr     *Manager
}

func newHarness(t *testing.T) *harness {
	t.Helper()
	auth := fakes.NewOpenAIAuth(t)
	client := fake.NewSimpleClientset(&corev1.Secret{ObjectMeta: metav1.ObjectMeta{Name: prefix + grantName, Namespace: namespace}})
	m := metrics.New()
	mgr := New(Options{
		Grant: grantName, Client: client, Namespace: namespace, SecretPrefix: prefix,
		Adapter:             &codexchatgpt.Adapter{Issuer: auth.URL, HTTPClient: auth.Client()},
		RefreshBeforeExpiry: 24 * time.Hour, MaxGrantAge: 7 * 24 * time.Hour, Metrics: m,
	})
	return &harness{auth: auth, client: client, store: &store.SecretStore{Client: client, Namespace: namespace, NamePrefix: prefix}, metrics: m, mgr: mgr}
}

// seed stores a grant issued by the fake, last refreshed at lastRefresh and
// expiring at expiresAt.
func (h *harness) seed(t *testing.T, lastRefresh, expiresAt time.Time) (access, refresh string) {
	t.Helper()
	_, refresh = h.auth.Issue()
	access = fakes.AccessToken("acct-1", expiresAt, "seed")
	if err := h.store.SaveGrant(store.Grant{Name: grantName, LastRefresh: lastRefresh, TokenBundle: store.TokenBundle{
		AccessToken: access, RefreshToken: refresh, ExpiresAt: expiresAt, AccountID: "acct-1",
	}}); err != nil {
		t.Fatal(err)
	}
	return access, refresh
}

func (h *harness) stored(t *testing.T) store.Grant {
	t.Helper()
	g, err := h.store.LoadGrant(grantName)
	if err != nil {
		t.Fatal(err)
	}
	return g
}

func (h *harness) state(state string) float64 {
	return testutil.ToFloat64(h.metrics.GrantState.WithLabelValues(grantName, state))
}

func TestFreshGrantIsNotRefreshed(t *testing.T) {
	h := newHarness(t)
	now := time.Now().UTC()
	access, _ := h.seed(t, now.Add(-time.Hour), now.Add(5*24*time.Hour))
	tok, err := h.mgr.AccessToken(context.Background())
	if err != nil || tok.AccessToken != access || tok.AccountID != "acct-1" {
		t.Fatalf("AccessToken() = %+v, %v", tok, err)
	}
	if h.auth.RefreshCalls() != 0 {
		t.Fatalf("refresh calls = %d, want 0", h.auth.RefreshCalls())
	}
	if h.state("ready") != 1 {
		t.Fatal("grant state is not ready")
	}
}

func TestRefreshWhenNearExpiryOrOld(t *testing.T) {
	now := time.Now().UTC()
	for name, seed := range map[string][2]time.Time{
		"under 24h from expiry": {now.Add(-time.Hour), now.Add(20 * time.Hour)},
		"7 days old":            {now.Add(-8 * 24 * time.Hour), now.Add(3 * 24 * time.Hour)},
	} {
		t.Run(name, func(t *testing.T) {
			h := newHarness(t)
			access, refresh := h.seed(t, seed[0], seed[1])
			tok, err := h.mgr.AccessToken(context.Background())
			if err != nil {
				t.Fatal(err)
			}
			if tok.AccessToken == access {
				t.Fatal("served the old access token")
			}
			// Written before use: the Secret already holds what was served.
			g := h.stored(t)
			if g.TokenBundle.AccessToken != tok.AccessToken || g.TokenBundle.RefreshToken == refresh || g.TokenBundle.RefreshToken == "" {
				t.Fatalf("stored grant not rotated before use: %+v", g.TokenBundle)
			}
			if g.TokenBundle.AccountID != "acct-1" || time.Until(g.TokenBundle.ExpiresAt) < 9*24*time.Hour {
				t.Fatalf("stored grant = %+v", g.TokenBundle)
			}
			if h.auth.RefreshCalls() != 1 {
				t.Fatalf("refresh calls = %d", h.auth.RefreshCalls())
			}
		})
	}
}

func TestRefreshTokenReusedRereadsSecret(t *testing.T) {
	h := newHarness(t)
	now := time.Now().UTC()
	_, stale := h.seed(t, now.Add(-time.Hour), now.Add(time.Hour))
	// Another writer already rotated the stale token and saved the result.
	rotated := h.auth.Rotate()
	h.auth.BeforeReuse(func(token string) {
		if token != stale {
			return
		}
		g, err := h.store.LoadGrant(grantName)
		if err != nil {
			t.Error(err)
			return
		}
		g.TokenBundle.RefreshToken = rotated
		g.LastRefresh = time.Now().UTC().Add(-time.Minute)
		if err := h.store.SaveGrant(g); err != nil {
			t.Error(err)
		}
	})
	tok, err := h.mgr.AccessToken(context.Background())
	if err != nil {
		t.Fatalf("AccessToken after reuse: %v", err)
	}
	if g := h.stored(t); g.TokenBundle.AccessToken != tok.AccessToken || g.TokenBundle.RefreshToken == rotated {
		t.Fatalf("stored grant after reuse retry = %+v", g.TokenBundle)
	}
	if got := testutil.ToFloat64(h.metrics.Refresh.ReusedRetries); got != 1 {
		t.Fatalf("reused retries = %v", got)
	}
}

func TestInvalidGrant(t *testing.T) {
	now := time.Now().UTC()
	t.Run("expired access token", func(t *testing.T) {
		h := newHarness(t)
		h.seed(t, now.Add(-9*24*time.Hour), now.Add(-time.Minute))
		h.auth.Revoke()
		if _, err := h.mgr.AccessToken(context.Background()); !errors.Is(err, ErrLoginRequired) {
			t.Fatalf("AccessToken() error = %v, want ErrLoginRequired", err)
		}
		calls := h.auth.RefreshCalls()
		if _, err := h.mgr.AccessToken(context.Background()); !errors.Is(err, ErrLoginRequired) {
			t.Fatalf("second AccessToken() error = %v", err)
		}
		if h.auth.RefreshCalls() != calls {
			t.Fatal("a dead grant kept calling the token endpoint")
		}
		if h.state("login_required") != 1 || h.state("ready") != 0 {
			t.Fatal("grant state is not login_required")
		}
	})
	t.Run("access token still valid", func(t *testing.T) {
		h := newHarness(t)
		access, _ := h.seed(t, now.Add(-time.Hour), now.Add(20*time.Hour))
		h.auth.Revoke()
		tok, err := h.mgr.AccessToken(context.Background())
		if err != nil || tok.AccessToken != access {
			t.Fatalf("AccessToken() = %+v, %v; want the still-valid stored token", tok, err)
		}
		if h.state("login_required") != 1 {
			t.Fatal("grant state does not report the dead refresh token")
		}
	})
	t.Run("no grant", func(t *testing.T) {
		h := newHarness(t)
		if _, err := h.mgr.AccessToken(context.Background()); !errors.Is(err, ErrLoginRequired) {
			t.Fatalf("empty grant error = %v", err)
		}
	})
}

func TestSingleFlightRefreshUnderConcurrency(t *testing.T) {
	h := newHarness(t)
	now := time.Now().UTC()
	h.seed(t, now.Add(-time.Hour), now.Add(time.Hour))
	h.auth.SetDelay(100 * time.Millisecond)
	var updates int
	var mu sync.Mutex
	h.client.PrependReactor("update", "secrets", func(k8stesting.Action) (bool, runtime.Object, error) {
		mu.Lock()
		updates++
		mu.Unlock()
		return false, nil, nil
	})
	const callers = 25
	tokens := make([]string, callers)
	var wg sync.WaitGroup
	for i := 0; i < callers; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			tok, err := h.mgr.AccessToken(context.Background())
			if err != nil {
				t.Errorf("caller %d: %v", i, err)
				return
			}
			tokens[i] = tok.AccessToken
		}(i)
	}
	wg.Wait()
	if got := h.auth.RefreshCalls(); got != 1 {
		t.Fatalf("refresh calls = %d, want 1", got)
	}
	for i, tok := range tokens {
		if tok != tokens[0] {
			t.Fatalf("caller %d got a different token", i)
		}
	}
	if updates != 1 {
		t.Fatalf("Secret updates = %d, want 1", updates)
	}
	if h.stored(t).TokenBundle.AccessToken != tokens[0] {
		t.Fatal("stored token differs from the served token")
	}
}

func TestDeviceLogin(t *testing.T) {
	h := newHarness(t)
	handler := h.mgr.AdminHandler()
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/admin/codex/login", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("login start = %d %s", rec.Code, rec.Body)
	}
	var start map[string]any
	_ = json.Unmarshal(rec.Body.Bytes(), &start)
	if start["user_code"] != "ABCD-1234" || start["verification_url"] == "" {
		t.Fatalf("login start body = %v", start)
	}
	deadline := time.Now().Add(10 * time.Second)
	for {
		rec = httptest.NewRecorder()
		handler.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/admin/codex/login", nil))
		var status map[string]any
		_ = json.Unmarshal(rec.Body.Bytes(), &status)
		if status["login"] == "granted" && status["grant_state"] == "present" {
			break
		}
		if status["login"] == "failed" || time.Now().After(deadline) {
			t.Fatalf("login status = %v", status)
		}
		time.Sleep(20 * time.Millisecond)
	}
	g := h.stored(t)
	if g.TokenBundle.RefreshToken == "" || g.TokenBundle.AccountID != "acct-1" || g.LastRefresh.IsZero() {
		t.Fatalf("stored grant after login = %+v", g)
	}
	tok, err := h.mgr.AccessToken(context.Background())
	if err != nil || tok.AccessToken != g.TokenBundle.AccessToken {
		t.Fatalf("AccessToken after login = %+v, %v", tok, err)
	}

	// A login after invalid_grant clears the login-required state.
	h.auth.Revoke()
	if err := h.mgr.ForceRefresh(context.Background()); !errors.Is(err, ErrLoginRequired) {
		t.Fatalf("ForceRefresh after revoke = %v", err)
	}
	rec = httptest.NewRecorder()
	handler.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/admin/codex/refresh", nil))
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("refresh endpoint with a dead grant = %d", rec.Code)
	}
}
