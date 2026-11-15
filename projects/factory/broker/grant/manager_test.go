package grant

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"testing/synctest"
	"time"

	"github.com/jomcgi/homelab/projects/factory/broker/internal/fakes"
	"github.com/jomcgi/homelab/projects/factory/broker/metrics"
	"github.com/jomcgi/homelab/projects/shared/go/tokenbroker/provider"
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

func TestMissingGrantSecretIsNotLoginRequired(t *testing.T) {
	h := newHarness(t)
	if err := h.client.CoreV1().Secrets(namespace).Delete(context.Background(), prefix+grantName, metav1.DeleteOptions{}); err != nil {
		t.Fatal(err)
	}
	if _, err := h.mgr.AccessToken(context.Background()); !errors.Is(err, ErrUnavailable) {
		t.Fatalf("missing Secret error = %v, want ErrUnavailable", err)
	}
	if h.state("missing") != 1 {
		t.Fatal("grant state is not missing")
	}
}

func TestRefreshRejected(t *testing.T) {
	now := time.Now().UTC()
	t.Run("transient failure is unavailable", func(t *testing.T) {
		h := newHarness(t)
		access, _ := h.seed(t, now, now.Add(5*24*time.Hour))
		h.auth.FailTransiently(true)
		if _, err := h.mgr.RefreshRejected(context.Background(), access); !errors.Is(err, ErrUnavailable) {
			t.Fatalf("RefreshRejected error = %v, want ErrUnavailable", err)
		}
		if h.mgr.broker.NeedsLogin(grantName) {
			t.Fatal("a transient failure marked the grant for login")
		}
	})
	t.Run("concurrent callers share one refresh", func(t *testing.T) {
		h := newHarness(t)
		access, _ := h.seed(t, now, now.Add(5*24*time.Hour))
		h.auth.SetDelay(50 * time.Millisecond)
		var wg sync.WaitGroup
		tokens := make([]string, 10)
		for i := range tokens {
			wg.Add(1)
			go func(i int) {
				defer wg.Done()
				tok, err := h.mgr.RefreshRejected(context.Background(), access)
				if err != nil {
					t.Errorf("caller %d: %v", i, err)
					return
				}
				tokens[i] = tok.AccessToken
			}(i)
		}
		wg.Wait()
		if h.auth.RefreshCalls() != 1 {
			t.Fatalf("refresh calls = %d, want 1", h.auth.RefreshCalls())
		}
		for i, tok := range tokens {
			if tok == access || tok != tokens[0] {
				t.Fatalf("caller %d token not the shared refreshed token", i)
			}
		}
		// The new token refused again inside the cooldown: unavailable,
		// not login required, and no second rotation.
		if _, err := h.mgr.RefreshRejected(context.Background(), tokens[0]); !errors.Is(err, ErrUnavailable) {
			t.Fatalf("repeat refusal error = %v", err)
		}
		if h.auth.RefreshCalls() != 1 {
			t.Fatal("repeat refusal rotated again inside the cooldown")
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

type loginAdapter struct {
	starts, exchanges            int
	tokens                       provider.TokenResponse
	exchanged                    chan struct{}
	afterExchange                func()
	refreshStarted, refreshAllow chan struct{}
}

func (a *loginAdapter) StartDeviceFlow(context.Context) (provider.DeviceCodeResponse, error) {
	a.starts++
	return provider.DeviceCodeResponse{UserCode: "LOGIN-CODE"}, nil
}

func (a *loginAdapter) PollForAuthorization(context.Context, provider.DeviceCodeResponse) (provider.AuthorizationCodeResponse, error) {
	return provider.AuthorizationCodeResponse{AuthorizationCode: "one-use-code"}, nil
}

func (a *loginAdapter) ExchangeCode(context.Context, provider.AuthorizationCodeResponse) (provider.TokenResponse, error) {
	a.exchanges++
	if a.afterExchange != nil {
		a.afterExchange()
	}
	if a.exchanged != nil {
		close(a.exchanged)
	}
	return a.tokens, nil
}

func (a *loginAdapter) RefreshToken(context.Context, string) (provider.TokenResponse, error) {
	if a.refreshStarted != nil {
		close(a.refreshStarted)
		<-a.refreshAllow
	}
	expiry := time.Now().Add(10 * 24 * time.Hour)
	return provider.TokenResponse{AccessToken: fakes.AccessToken("acct-1", expiry, "old-refresh"), RefreshToken: "old-refresh-result", ExpiresAt: expiry}, nil
}

func newLoginHarness(t *testing.T, a *loginAdapter) *harness {
	t.Helper()
	client := fake.NewSimpleClientset(&corev1.Secret{ObjectMeta: metav1.ObjectMeta{Name: prefix + grantName, Namespace: namespace}})
	mgr := New(Options{Grant: grantName, Client: client, Namespace: namespace, SecretPrefix: prefix, Adapter: a})
	return &harness{client: client, store: mgr.store, metrics: mgr.opts.Metrics, mgr: mgr}
}

func newLoginAdapter() *loginAdapter {
	expiry := time.Now().Add(10 * 24 * time.Hour)
	return &loginAdapter{tokens: provider.TokenResponse{
		IDToken: "exchanged-id", AccessToken: fakes.AccessToken("new-account", expiry, "login"), RefreshToken: "exchanged-refresh", ExpiresAt: expiry,
	}}
}

func TestDeviceLoginSurvivesConcurrentRefresh(t *testing.T) {
	for _, mode := range []string{"proactive", "forced", "rejected"} {
		t.Run(mode, func(t *testing.T) {
			synctest.Test(t, func(t *testing.T) {
				a := newLoginAdapter()
				a.exchanged, a.refreshStarted, a.refreshAllow = make(chan struct{}), make(chan struct{}), make(chan struct{})
				h := newLoginHarness(t, a)
				if err := h.store.SaveGrant(store.Grant{Name: grantName, LastRefresh: time.Now().Add(-time.Hour), TokenBundle: store.TokenBundle{
					AccessToken: "old-access", RefreshToken: "old-refresh", AccountID: "acct-1", ExpiresAt: time.Now().Add(time.Minute),
				}}); err != nil {
					t.Fatal(err)
				}
				refreshed := make(chan struct{})
				go func() {
					defer close(refreshed)
					switch mode {
					case "proactive":
						h.mgr.Check(context.Background())
					case "forced":
						_ = h.mgr.ForceRefresh(context.Background())
					case "rejected":
						_, _ = h.mgr.RefreshRejected(context.Background(), "old-access")
					}
				}()
				<-a.refreshStarted
				response := httptest.NewRecorder()
				h.mgr.AdminHandler().ServeHTTP(response, httptest.NewRequest(http.MethodPost, "/admin/codex/login", nil))
				if response.Code != http.StatusOK {
					t.Fatal("device login did not start")
				}
				<-a.exchanged
				synctest.Wait()
				time.Sleep(time.Second)
				close(a.refreshAllow)
				<-refreshed
				synctest.Wait()
				g := h.stored(t)
				if g.TokenBundle.AccessToken != a.tokens.AccessToken || g.TokenBundle.RefreshToken != a.tokens.RefreshToken || g.TokenBundle.AccountID != "new-account" {
					t.Fatal("concurrent refresh discarded or replaced the exchanged login")
				}
				if a.exchanges != 1 || h.mgr.login.state != "granted" {
					t.Fatal("login did not finish with exactly one code exchange")
				}
			})
		})
	}
}

func TestDeviceLoginPersistenceRetryAfterCancellation(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		a := newLoginAdapter()
		h := newLoginHarness(t, a)
		ctx, cancel := context.WithCancel(context.Background())
		defer cancel()
		h.mgr.opts.Context = ctx
		a.afterExchange = cancel
		var logs bytes.Buffer
		h.mgr.opts.Logger = slog.New(slog.NewTextHandler(&logs, nil))
		writes := 0
		h.client.PrependReactor("update", "secrets", func(k8stesting.Action) (bool, runtime.Object, error) {
			writes++
			if writes <= 5 {
				return true, nil, errors.New("storage failure containing " + a.tokens.RefreshToken)
			}
			return false, nil, nil
		})
		handler := h.mgr.AdminHandler()
		response := httptest.NewRecorder()
		handler.ServeHTTP(response, httptest.NewRequest(http.MethodPost, "/admin/codex/login", nil))
		time.Sleep(4 * time.Second)
		synctest.Wait()
		status := httptest.NewRecorder()
		handler.ServeHTTP(status, httptest.NewRequest(http.MethodGet, "/admin/codex/login", nil))
		var body map[string]any
		if err := json.Unmarshal(status.Body.Bytes(), &body); err != nil {
			t.Fatal(err)
		}
		if body["login"] != "persistence_pending" || writes != 5 {
			t.Fatal("failed writes did not expose a bounded, recoverable login state")
		}
		if strings.Contains(status.Body.String()+logs.String(), a.tokens.RefreshToken) {
			t.Fatal("login persistence exposed credential contents")
		}
		response = httptest.NewRecorder()
		handler.ServeHTTP(response, httptest.NewRequest(http.MethodPost, "/admin/codex/login", nil))
		if response.Code != http.StatusAccepted {
			t.Fatal("login POST did not accept a persistence retry")
		}
		body = nil
		if err := json.Unmarshal(response.Body.Bytes(), &body); err != nil || body["state"] != "pending" || body["persistence_retry"] != true {
			t.Fatal("persistence retry response cannot be recognized by login clients")
		}
		synctest.Wait()
		if a.starts != 1 || a.exchanges != 1 || writes != 6 || h.mgr.login.state != "granted" {
			t.Fatal("persistence recovery repeated the device flow or failed to complete")
		}
		g := h.stored(t)
		if g.TokenBundle.RefreshToken != a.tokens.RefreshToken || g.TokenBundle.IDToken != a.tokens.IDToken {
			t.Fatal("cancellation after exchange lost credentials")
		}
	})
}

func TestDeviceLoginRetriesDelayedSecretWrites(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		a := newLoginAdapter()
		h := newLoginHarness(t, a)
		writes := 0
		h.client.PrependReactor("update", "secrets", func(k8stesting.Action) (bool, runtime.Object, error) {
			writes++
			time.Sleep(time.Second)
			if writes < 3 {
				return true, nil, errors.New("temporarily unavailable")
			}
			return false, nil, nil
		})
		h.mgr.completeLogin(provider.DeviceCodeResponse{})
		if a.exchanges != 1 || writes != 3 || h.mgr.login.state != "granted" {
			t.Fatal("delayed Secret writes discarded the login or repeated the exchange")
		}
		if h.stored(t).TokenBundle.RefreshToken != a.tokens.RefreshToken {
			t.Fatal("delayed writes did not persist the exchanged credentials")
		}
	})
}

func TestDeviceLoginConfirmsSecretCommitAfterLostResponse(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		a := newLoginAdapter()
		h := newLoginHarness(t, a)
		writes := 0
		h.client.PrependReactor("update", "secrets", func(action k8stesting.Action) (bool, runtime.Object, error) {
			writes++
			secret := action.(k8stesting.UpdateAction).GetObject().(*corev1.Secret)
			if err := h.client.Tracker().Update(corev1.SchemeGroupVersion.WithResource("secrets"), secret, namespace); err != nil {
				return true, nil, err
			}
			return true, nil, errors.New("update committed but response lost")
		})
		h.mgr.completeLogin(provider.DeviceCodeResponse{})
		if a.exchanges != 1 || writes != 1 || h.mgr.login.state != "granted" {
			t.Fatal("lost update response prevented confirmation of the stored login")
		}
		if h.stored(t).TokenBundle.AccessToken != a.tokens.AccessToken {
			t.Fatal("confirmed login differs from the exchanged credentials")
		}
	})
}
