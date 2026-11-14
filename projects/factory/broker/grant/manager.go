// Package grant manages the broker's one Codex grant: its device login,
// refresh ahead of expiry, and the access token the relay uses. Refresh,
// single-flight and durable rotation come from the shared token broker.
package grant

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"sync"
	"time"

	"github.com/jomcgi/homelab/projects/factory/broker/auth"
	"github.com/jomcgi/homelab/projects/factory/broker/metrics"
	"github.com/jomcgi/homelab/projects/shared/go/tokenbroker/broker"
	"github.com/jomcgi/homelab/projects/shared/go/tokenbroker/provider"
	"github.com/jomcgi/homelab/projects/shared/go/tokenbroker/provider/codexchatgpt"
	"github.com/jomcgi/homelab/projects/shared/go/tokenbroker/store"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"
)

// ProviderName is the shared broker's provider key for Codex.
const ProviderName = "codex-chatgpt"

var (
	// ErrLoginRequired means the grant is missing or its refresh token is
	// dead: only a new device login recovers it.
	ErrLoginRequired = errors.New("codex_login_required")
	// ErrUnavailable is a transient refresh or storage failure.
	ErrUnavailable = errors.New("codex_grant_unavailable")
)

// Token is a usable Codex access token and its ChatGPT account.
type Token struct {
	AccessToken string
	AccountID   string
	ExpiresAt   time.Time
}

type Options struct {
	Grant        string
	Client       kubernetes.Interface
	Namespace    string
	SecretPrefix string
	Adapter      provider.Adapter
	// RefreshBeforeExpiry and MaxGrantAge set when a grant is refreshed.
	RefreshBeforeExpiry time.Duration
	MaxGrantAge         time.Duration
	// CheckInterval is how often Run checks whether a refresh is due.
	CheckInterval time.Duration
	// RetryAfter is the least time between refresh attempts after a
	// transient failure, so relay traffic does not hammer the provider.
	RetryAfter time.Duration
	// ForceCooldown is the least time between forced refreshes.
	ForceCooldown time.Duration
	Metrics       *metrics.Metrics
	Logger        *slog.Logger
	// Context bounds background work such as a pending device login; it
	// is the process's shutdown context. Defaults to context.Background.
	Context context.Context
}

type Manager struct {
	opts   Options
	store  *store.SecretStore
	broker *broker.Broker

	mu          sync.Mutex
	lastFailure time.Time
	lastForced  time.Time
	// rejectMu serializes RefreshRejected, so concurrent callers holding
	// the same refused token wait for the one in-flight refresh.
	rejectMu sync.Mutex
	login    loginState
}

type loginState struct {
	state, detail string
	code          *provider.DeviceCodeResponse
}

func New(opts Options) *Manager {
	if opts.Logger == nil {
		opts.Logger = slog.Default()
	}
	if opts.Metrics == nil {
		opts.Metrics = metrics.New()
	}
	if opts.CheckInterval <= 0 {
		opts.CheckInterval = 10 * time.Minute
	}
	if opts.RetryAfter <= 0 {
		opts.RetryAfter = time.Minute
	}
	if opts.Context == nil {
		opts.Context = context.Background()
	}
	if opts.ForceCooldown <= 0 {
		opts.ForceCooldown = time.Minute
	}
	st := &store.SecretStore{Client: opts.Client, Namespace: opts.Namespace, NamePrefix: opts.SecretPrefix}
	b := broker.New(st, map[string]provider.Adapter{ProviderName: opts.Adapter}, nil,
		[]broker.GrantConfig{{Name: opts.Grant, ProviderName: ProviderName}}, opts.Logger, opts.Metrics.Refresh)
	b.SetRefreshPolicy(opts.RefreshBeforeExpiry, opts.MaxGrantAge)
	return &Manager{opts: opts, store: st, broker: b, login: loginState{state: "none"}}
}

// Grant is the managed grant's name.
func (m *Manager) Grant() string { return m.opts.Grant }

// AccessToken returns a token the relay can use now. A grant due for refresh
// is refreshed first, single-flight, and the rotated grant is written to its
// Secret before the token is returned. When a refresh fails while the stored
// access token is still valid, the stored token is served and the state
// metric reports the failure.
func (m *Manager) AccessToken(ctx context.Context) (Token, error) {
	grant := m.opts.Grant
	if m.broker.NeedsLogin(grant) {
		return m.cached(ctx, ErrLoginRequired)
	}
	m.mu.Lock()
	backoff := !m.lastFailure.IsZero() && time.Since(m.lastFailure) < m.opts.RetryAfter
	m.mu.Unlock()
	if backoff {
		return m.cached(ctx, ErrUnavailable)
	}
	access, expiry, err := m.broker.GetAccessToken(grant, ctx)
	if err == nil {
		m.recordSuccess(expiry)
		return m.token(access, expiry)
	}
	switch {
	case errors.Is(err, broker.ErrNoGrant):
		m.opts.Metrics.SetGrantState(grant, "login_required")
		return Token{}, ErrLoginRequired
	case m.broker.NeedsLogin(grant):
		m.opts.Metrics.SetGrantState(grant, "login_required")
		m.opts.Logger.Error("codex grant needs a new device login", "grant", grant)
		return m.cached(ctx, ErrLoginRequired)
	}
	m.mu.Lock()
	m.lastFailure = time.Now()
	m.mu.Unlock()
	// A missing grant Secret is a deployment fault, not a dead grant: the
	// chart creates it and a device login cannot (the store only updates
	// it), so it stays a transient failure with its own state.
	state := "error"
	if _, getErr := m.opts.Client.CoreV1().Secrets(m.opts.Namespace).Get(ctx, m.store.SecretName(grant), metav1.GetOptions{}); apierrors.IsNotFound(getErr) {
		state = "missing"
	}
	m.opts.Metrics.SetGrantState(grant, state)
	m.opts.Logger.Error("codex grant refresh failed", "grant", grant, "state", state, "err", err)
	return m.cached(ctx, ErrUnavailable)
}

// cached serves the stored access token while it is valid, else fallback.
func (m *Manager) cached(ctx context.Context, fallback error) (Token, error) {
	access, expiry, err := m.broker.CachedAccessToken(m.opts.Grant, ctx)
	if err != nil {
		return Token{}, fallback
	}
	return m.token(access, expiry)
}

func (m *Manager) token(access string, expiry time.Time) (Token, error) {
	account, ok := codexchatgpt.AccountID(access)
	if !ok {
		grant, err := m.store.LoadGrant(m.opts.Grant)
		if err != nil || grant.TokenBundle.AccountID == "" {
			m.opts.Logger.Error("codex access token has no ChatGPT account", "grant", m.opts.Grant)
			return Token{}, ErrLoginRequired
		}
		account = grant.TokenBundle.AccountID
	}
	return Token{AccessToken: access, AccountID: account, ExpiresAt: expiry}, nil
}

func (m *Manager) recordSuccess(expiry time.Time) {
	m.mu.Lock()
	m.lastFailure = time.Time{}
	m.mu.Unlock()
	m.opts.Metrics.SetGrantState(m.opts.Grant, "ready")
	m.opts.Metrics.GrantExpiry.WithLabelValues(m.opts.Grant).Set(float64(expiry.Unix()))
}

// RefreshRejected replaces an access token the backend refused. Callers
// holding the same refused token share one refresh: later callers wait for
// it and then get the token it stored. It answers ErrLoginRequired only when
// the refresh token is confirmed dead; anything else is ErrUnavailable.
func (m *Manager) RefreshRejected(ctx context.Context, rejected string) (Token, error) {
	grant := m.opts.Grant
	if m.broker.NeedsLogin(grant) {
		return Token{}, ErrLoginRequired
	}
	m.rejectMu.Lock()
	defer m.rejectMu.Unlock()
	if access, expiry, err := m.broker.CachedAccessToken(grant, ctx); err == nil && access != rejected {
		return m.token(access, expiry)
	}
	m.mu.Lock()
	recent := !m.lastForced.IsZero() && time.Since(m.lastForced) < m.opts.ForceCooldown
	if !recent {
		m.lastForced = time.Now()
	}
	m.mu.Unlock()
	if recent {
		// A refresh just ran and the backend still refuses: do not rotate
		// again in a loop, and do not claim the grant is dead.
		return Token{}, ErrUnavailable
	}
	access, expiry, err := m.broker.RefreshAccessToken(grant, ctx)
	if err != nil {
		if m.broker.NeedsLogin(grant) {
			m.opts.Metrics.SetGrantState(grant, "login_required")
			return Token{}, ErrLoginRequired
		}
		m.opts.Metrics.SetGrantState(grant, "error")
		m.opts.Logger.Error("codex grant refresh after backend 401 failed", "grant", grant, "err", err)
		return Token{}, ErrUnavailable
	}
	m.recordSuccess(expiry)
	return m.token(access, expiry)
}

// ForceRefresh rotates the grant now, at most once per ForceCooldown, for
// the admin endpoint.
func (m *Manager) ForceRefresh(ctx context.Context) error {
	grant := m.opts.Grant
	if m.broker.NeedsLogin(grant) {
		return ErrLoginRequired
	}
	m.mu.Lock()
	if !m.lastForced.IsZero() && time.Since(m.lastForced) < m.opts.ForceCooldown {
		m.mu.Unlock()
		return nil
	}
	m.lastForced = time.Now()
	m.mu.Unlock()
	_, expiry, err := m.broker.RefreshAccessToken(grant, ctx)
	if err != nil {
		if m.broker.NeedsLogin(grant) {
			m.opts.Metrics.SetGrantState(grant, "login_required")
			return ErrLoginRequired
		}
		m.opts.Metrics.SetGrantState(grant, "error")
		return ErrUnavailable
	}
	m.recordSuccess(expiry)
	return nil
}

// Run checks the grant every CheckInterval so it is refreshed ahead of
// expiry even when no relay traffic arrives.
func (m *Manager) Run(ctx context.Context) {
	t := time.NewTicker(m.opts.CheckInterval)
	defer t.Stop()
	for {
		m.Check(ctx)
		select {
		case <-ctx.Done():
			return
		case <-t.C:
		}
	}
}

// Check runs one refresh check and records its outcome.
func (m *Manager) Check(ctx context.Context) {
	outcome := "ok"
	_, err := m.AccessToken(ctx)
	switch {
	case errors.Is(err, ErrLoginRequired):
		outcome = "login_required"
	case err != nil:
		outcome = "error"
	}
	m.opts.Metrics.RefreshOutcomes.WithLabelValues(m.opts.Grant, outcome).Inc()
}

// AdminHandler serves the device login and forced refresh. Callers wrap it
// with TokenReview authorization.
//
//	POST /admin/codex/login    start a device login (returns the user code)
//	GET  /admin/codex/login    login and grant state
//	POST /admin/codex/refresh  refresh now
func (m *Manager) AdminHandler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("POST /admin/codex/login", m.startLogin)
	mux.HandleFunc("GET /admin/codex/login", m.loginStatus)
	mux.HandleFunc("POST /admin/codex/refresh", m.refreshNow)
	return mux
}

func (m *Manager) startLogin(w http.ResponseWriter, r *http.Request) {
	m.mu.Lock()
	if m.login.state == "pending" {
		resp := map[string]any{"state": "pending"}
		if c := m.login.code; c != nil {
			resp["verification_url"], resp["user_code"], resp["expires_in"] = c.VerificationURL, c.UserCode, c.ExpiresIn
		}
		m.mu.Unlock()
		writeJSON(w, http.StatusConflict, resp)
		return
	}
	m.login = loginState{state: "pending", detail: "starting"}
	m.mu.Unlock()

	ctx, cancel := context.WithTimeout(r.Context(), 30*time.Second)
	defer cancel()
	code, err := m.opts.Adapter.StartDeviceFlow(ctx)
	if err != nil {
		m.opts.Logger.Error("codex device login start failed", "err", err)
		m.setLogin(loginState{state: "failed", detail: "device code request failed"})
		writeJSON(w, http.StatusBadGateway, map[string]string{"state": "failed", "reason": "device_code_failed"})
		return
	}
	m.setLogin(loginState{state: "pending", detail: "approval required", code: &code})
	m.opts.Logger.Info("codex device login started", "grant", m.opts.Grant, "by", auth.User(r.Context()))
	go m.completeLogin(code)
	writeJSON(w, http.StatusOK, map[string]any{"state": "pending", "verification_url": code.VerificationURL, "user_code": code.UserCode, "expires_in": code.ExpiresIn})
}

func (m *Manager) completeLogin(code provider.DeviceCodeResponse) {
	ctx, cancel := context.WithTimeout(m.opts.Context, 16*time.Minute)
	defer cancel()
	err := m.exchange(ctx, code)
	if err != nil {
		m.opts.Logger.Error("codex device login failed", "grant", m.opts.Grant, "err", err)
		m.setLogin(loginState{state: "failed", detail: "device login failed"})
		return
	}
	m.broker.ClearNeedsLogin(m.opts.Grant)
	m.mu.Lock()
	m.lastFailure = time.Time{}
	m.mu.Unlock()
	m.setLogin(loginState{state: "granted", detail: "device authorization complete"})
	m.opts.Logger.Info("codex device login complete", "grant", m.opts.Grant)
	m.Check(ctx)
}

func (m *Manager) exchange(ctx context.Context, code provider.DeviceCodeResponse) error {
	auth, err := m.opts.Adapter.PollForAuthorization(ctx, code)
	if err != nil {
		return err
	}
	tok, err := m.opts.Adapter.ExchangeCode(ctx, auth)
	if err != nil {
		return err
	}
	if tok.AccessToken == "" || tok.RefreshToken == "" {
		return errors.New("token exchange returned no access or refresh token")
	}
	account, _ := codexchatgpt.AccountID(tok.AccessToken)
	now := time.Now().UTC()
	return m.broker.SaveGrant(store.Grant{
		Name: m.opts.Grant, ProviderName: ProviderName, LastRefresh: now,
		TokenBundle: store.TokenBundle{
			IDToken: tok.IDToken, AccessToken: tok.AccessToken, RefreshToken: tok.RefreshToken,
			AccountID: account, LastRefresh: now, ExpiresAt: tok.ExpiresAt,
		},
	})
}

func (m *Manager) setLogin(s loginState) {
	m.mu.Lock()
	m.login = s
	m.mu.Unlock()
}

func (m *Manager) loginStatus(w http.ResponseWriter, r *http.Request) {
	m.mu.Lock()
	login := m.login
	m.mu.Unlock()
	resp := map[string]any{"grant": m.opts.Grant, "login": login.state, "detail": login.detail, "grant_state": "missing"}
	switch g, err := m.store.LoadGrant(m.opts.Grant); {
	case err != nil:
		resp["grant_state"] = "error"
	case m.broker.NeedsLogin(m.opts.Grant):
		resp["grant_state"] = "login_required"
	case g.TokenBundle.RefreshToken != "":
		resp["grant_state"] = "present"
		resp["expires_at"] = g.TokenBundle.ExpiresAt.UTC().Format(time.RFC3339)
		resp["last_refresh"] = g.LastRefresh.UTC().Format(time.RFC3339)
	}
	writeJSON(w, http.StatusOK, resp)
}

func (m *Manager) refreshNow(w http.ResponseWriter, r *http.Request) {
	switch err := m.ForceRefresh(r.Context()); {
	case errors.Is(err, ErrLoginRequired):
		writeJSON(w, http.StatusUnauthorized, map[string]string{"factory_error": ErrLoginRequired.Error()})
	case err != nil:
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"factory_error": ErrUnavailable.Error()})
	default:
		writeJSON(w, http.StatusOK, map[string]bool{"refreshed": true})
	}
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}
