package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"strings"
	"sync"
	"time"

	"github.com/jomcgi/homelab/projects/embervm/tokenbroker/internal/broker"
	"github.com/jomcgi/homelab/projects/embervm/tokenbroker/internal/metrics"
	"github.com/jomcgi/homelab/projects/embervm/tokenbroker/internal/provider"
	"github.com/jomcgi/homelab/projects/embervm/tokenbroker/internal/provider/authentik"
	"github.com/jomcgi/homelab/projects/embervm/tokenbroker/internal/provider/codexchatgpt"
	"github.com/jomcgi/homelab/projects/embervm/tokenbroker/internal/quota"
	"github.com/jomcgi/homelab/projects/embervm/tokenbroker/internal/store"
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promhttp"
	"github.com/spiffe/go-spiffe/v2/spiffeid"
	"github.com/spiffe/go-spiffe/v2/spiffetls/tlsconfig"
	"github.com/spiffe/go-spiffe/v2/svid/x509svid"
	"github.com/spiffe/go-spiffe/v2/workloadapi"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"
)

type grantConfig struct {
	Name         string `json:"name"`
	ProviderName string `json:"provider"`
}
type loginState struct {
	mu            sync.RWMutex
	state, detail string
	code          *provider.DeviceCodeResponse
	ready         chan struct{}
}
type forceRefreshState struct {
	mu         sync.Mutex
	lastForced time.Time
}
type listenerConfig struct {
	listenAddr      string
	tlsListenAddr   string
	spiffeClientIDs []spiffeid.ID
}
type server struct {
	broker             *broker.Broker
	store              store.Store
	adapters           map[string]provider.Adapter
	configs            map[string]grantConfig
	logins             sync.Map
	forced             sync.Map
	logger             *slog.Logger
	startWaitTimeout   time.Duration
	quotaStore         *quota.Store
	quotaProviders     map[string]struct{}
	quotaProviderOrder []string
	tokenRequests      *prometheus.CounterVec
}

const (
	forceRefreshCooldown = 60 * time.Second
	// loginStartWaitTimeout is coupled to the 10-second client-side proxy budget in
	// projects/monolith/frontend/src/routes/private/agents/codex-login/start/+server.js.
	// Do not raise either timeout in isolation. Preserve headroom for monolith latency.
	loginStartWaitTimeout = 5 * time.Second
	// x509SourceTimeout is coupled to the chart's liveness probe, which kills the
	// pod at roughly 70 seconds (initial delay 10, period 30, three failures).
	// Raising this past that turns a clean fail-closed exit into a probe kill.
	x509SourceTimeout = 60 * time.Second
)

func main() {
	logger := slog.New(slog.NewJSONHandler(os.Stdout, nil))
	if err := run(logger); err != nil {
		logger.Error("token broker stopped", "err", err)
		os.Exit(1)
	}
}

func run(logger *slog.Logger) error {
	listeners, err := configuredListeners()
	if err != nil {
		return err
	}
	quotaProviders, err := configuredQuotaProviders()
	if err != nil {
		return fmt.Errorf("invalid TOKENBROKER_QUOTA_PROVIDERS: %w", err)
	}
	cfg, err := rest.InClusterConfig()
	if err != nil {
		return fmt.Errorf("in-cluster config failed: %w", err)
	}
	client, err := kubernetes.NewForConfig(cfg)
	if err != nil {
		return fmt.Errorf("kubernetes client failed: %w", err)
	}
	configs, err := configuredGrants()
	if err != nil {
		return fmt.Errorf("invalid TOKENBROKER_GRANTS: %w", err)
	}
	namespace := env("KUBERNETES_NAMESPACE", "embervm")
	st := &store.SecretStore{Client: client, Namespace: namespace}
	adapters := map[string]provider.Adapter{"codex-chatgpt": &codexchatgpt.Adapter{}}
	// authentik authenticates a service account, so the standing credential is
	// an app password, not an OAuth client secret.
	minters := map[string]provider.Minter{
		"authentik": &authentik.Adapter{
			TokenEndpoint: os.Getenv("AUTHENTIK_TOKEN_ENDPOINT"),
			ClientID:      os.Getenv("AUTHENTIK_CLIENT_ID"),
			Scope:         os.Getenv("AUTHENTIK_SCOPE"),
			Username:      os.Getenv("AUTHENTIK_USERNAME"),
			AppPassword:   os.Getenv("AUTHENTIK_APP_PASSWORD"),
		},
	}
	brokerConfigs := make([]broker.GrantConfig, 0, len(configs))
	configMap := make(map[string]grantConfig, len(configs))
	for _, c := range configs {
		brokerConfigs = append(brokerConfigs, broker.GrantConfig{Name: c.Name, ProviderName: c.ProviderName})
		configMap[c.Name] = c
	}
	m := metrics.New()
	m.Register(prometheus.DefaultRegisterer)
	quotaStore := quota.NewStore()
	tokenRequests := newTokenRequestsCounter()
	prometheus.MustRegister(metrics.NewQuotaCollector(quotaStore, quotaProviders), tokenRequests)
	quotaProviderSet := make(map[string]struct{}, len(quotaProviders))
	for _, provider := range quotaProviders {
		quotaProviderSet[provider] = struct{}{}
	}
	s := &server{
		store: st, adapters: adapters, configs: configMap, logger: logger,
		startWaitTimeout: loginStartWaitTimeout, quotaStore: quotaStore,
		quotaProviders: quotaProviderSet, quotaProviderOrder: quotaProviders,
		tokenRequests: tokenRequests,
	}
	s.broker = broker.New(st, adapters, minters, brokerConfigs, logger, m)
	plaintextMux := http.NewServeMux()
	plaintextMux.HandleFunc("/healthz", s.health)
	plaintextMux.Handle("/metrics", promhttp.Handler())
	plaintextMux.Handle("/grants/", s.grantsHandler(false, listeners.tlsListenAddr != ""))
	plaintextMux.HandleFunc("/quota", s.quota)
	plaintextMux.HandleFunc("/quota/", s.quota)
	plaintextServer := &http.Server{
		Addr:              listeners.listenAddr,
		Handler:           plaintextMux,
		ReadHeaderTimeout: 5 * time.Second,
	}
	if listeners.tlsListenAddr == "" {
		logger.Info("token broker listening", "addr", listeners.listenAddr)
		return plaintextServer.ListenAndServe()
	}

	serverErrors := make(chan error, 2)
	go func() {
		logger.Info("token broker plaintext listener started", "addr", listeners.listenAddr)
		serverErrors <- fmt.Errorf("plaintext listener stopped: %w", plaintextServer.ListenAndServe())
	}()

	source, err := waitForX509Source(x509SourceTimeout, newWorkloadX509Source)
	if err != nil {
		return err
	}
	defer source.Close()

	mtlsMux := http.NewServeMux()
	mtlsMux.Handle("/grants/", s.grantsHandler(true, true))
	mtlsMux.HandleFunc("/quota", s.quota)
	mtlsMux.HandleFunc("/quota/", s.quota)
	mtlsServer := &http.Server{
		Addr:              listeners.tlsListenAddr,
		Handler:           mtlsMux,
		ReadHeaderTimeout: 5 * time.Second,
		TLSConfig: tlsconfig.MTLSServerConfig(
			source,
			source,
			tlsconfig.AuthorizeOneOf(listeners.spiffeClientIDs...),
		),
	}

	go func() {
		logger.Info("token broker SPIFFE mTLS listener started", "addr", listeners.tlsListenAddr)
		serverErrors <- fmt.Errorf("SPIFFE mTLS listener stopped: %w", mtlsServer.ListenAndServeTLS("", ""))
	}()
	return <-serverErrors
}

type x509SourceFactory func(context.Context) (*workloadapi.X509Source, error)

func newWorkloadX509Source(ctx context.Context) (*workloadapi.X509Source, error) {
	return workloadapi.NewX509Source(ctx)
}

func waitForX509Source(timeout time.Duration, create x509SourceFactory) (*workloadapi.X509Source, error) {
	sourceContext, cancelSource := context.WithTimeout(context.Background(), timeout)
	defer cancelSource()
	source, err := create(sourceContext)
	if err != nil {
		return nil, fmt.Errorf("SPIFFE X509 source did not deliver an SVID within %s: %w", timeout, err)
	}
	if _, err := source.GetX509SVID(); err != nil {
		source.Close()
		return nil, fmt.Errorf("SPIFFE X509 source has no SVID: %w", err)
	}
	return source, nil
}

func configuredListeners() (listenerConfig, error) {
	config := listenerConfig{
		listenAddr:    env("BROKER_LISTEN_ADDR", ":8080"),
		tlsListenAddr: os.Getenv("BROKER_TLS_LISTEN_ADDR"),
	}
	if config.tlsListenAddr == "" {
		return config, nil
	}
	rawClientIDs := strings.TrimSpace(os.Getenv("BROKER_SPIFFE_CLIENT_IDS"))
	if rawClientIDs == "" {
		return listenerConfig{}, errors.New("BROKER_SPIFFE_CLIENT_IDS is required when BROKER_TLS_LISTEN_ADDR is set")
	}
	for _, rawClientID := range strings.Split(rawClientIDs, ",") {
		clientID, err := spiffeid.FromString(strings.TrimSpace(rawClientID))
		if err != nil {
			return listenerConfig{}, fmt.Errorf("invalid BROKER_SPIFFE_CLIENT_IDS entry %q: %w", rawClientID, err)
		}
		config.spiffeClientIDs = append(config.spiffeClientIDs, clientID)
	}
	return config, nil
}

func configuredGrants() ([]grantConfig, error) {
	raw := env("TOKENBROKER_GRANTS", `[{"name":"codex-cluster","provider":"codex-chatgpt"}]`)
	var grants []grantConfig
	if err := json.Unmarshal([]byte(raw), &grants); err != nil {
		return nil, err
	}
	if len(grants) == 0 {
		return nil, errors.New("at least one grant is required")
	}
	return grants, nil
}

func configuredQuotaProviders() ([]string, error) {
	providers, err := parseQuotaProviders(env("TOKENBROKER_QUOTA_PROVIDERS", "codex,claude"))
	if err != nil {
		return nil, err
	}
	return providers, nil
}

func parseQuotaProviders(raw string) ([]string, error) {
	seen := make(map[string]struct{})
	providers := make([]string, 0)
	for _, part := range strings.Split(raw, ",") {
		provider := strings.TrimSpace(part)
		if provider == "" {
			return nil, fmt.Errorf("provider names must not be empty")
		}
		for _, r := range provider {
			if (r < 'a' || r > 'z') && (r < '0' || r > '9') && r != '-' && r != '_' {
				return nil, fmt.Errorf("invalid provider name %q", provider)
			}
		}
		if _, duplicate := seen[provider]; duplicate {
			return nil, fmt.Errorf("duplicate provider %q", provider)
		}
		seen[provider] = struct{}{}
		providers = append(providers, provider)
	}
	if len(providers) == 0 {
		return nil, fmt.Errorf("at least one provider is required")
	}
	return providers, nil
}

// quota serves an in-memory view of subscription quota reported by egress
// proxies. POST /quota/{provider} replaces that provider's latest observation,
// GET /quota/{provider} reads one view, and GET /quota reads every allowlisted
// provider. Observations are intentionally not persisted and are lost whenever
// tokenbroker restarts.
func (s *server) quota(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path == "/quota" {
		if r.Method != http.MethodGet {
			w.Header().Set("Allow", http.MethodGet)
			writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"reason": "method_not_allowed"})
			return
		}
		providers := make(map[string]quota.View, len(s.quotaProviderOrder))
		for _, provider := range s.quotaProviderOrder {
			providers[provider] = s.quotaStore.Get(provider)
		}
		writeJSON(w, http.StatusOK, map[string]any{"providers": providers})
		return
	}

	if !strings.HasPrefix(r.URL.Path, "/quota/") {
		http.NotFound(w, r)
		return
	}
	provider := strings.TrimPrefix(r.URL.Path, "/quota/")
	if provider == "" || strings.Contains(provider, "/") {
		http.NotFound(w, r)
		return
	}
	if _, ok := s.quotaProviders[provider]; !ok {
		http.NotFound(w, r)
		return
	}
	switch r.Method {
	case http.MethodGet:
		writeJSON(w, http.StatusOK, s.quotaStore.Get(provider))
	case http.MethodPost:
		s.acceptQuota(provider, w, r)
	default:
		w.Header().Set("Allow", http.MethodGet+", "+http.MethodPost)
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"reason": "method_not_allowed"})
	}
}

func (s *server) acceptQuota(provider string, w http.ResponseWriter, r *http.Request) {
	r.Body = http.MaxBytesReader(w, r.Body, 64<<10)
	decoder := json.NewDecoder(r.Body)
	var obs quota.Observation
	if err := decoder.Decode(&obs); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"reason": "invalid_json"})
		return
	}
	if err := decoder.Decode(&struct{}{}); err != io.EOF {
		writeJSON(w, http.StatusBadRequest, map[string]string{"reason": "invalid_json"})
		return
	}
	obs.Provider = provider
	if !quota.ValidObservation(obs) {
		writeJSON(w, http.StatusBadRequest, map[string]string{"reason": "invalid_observation"})
		return
	}
	receivedAt := time.Now().UTC()
	s.quotaStore.Put(provider, obs, receivedAt)
	s.logger.Info("tokenbroker quota observation accepted", "provider", provider, "status", obs.Status, "windows", len(obs.Windows))
	w.WriteHeader(http.StatusNoContent)
}

func (s *server) grantsHandler(mtlsListener, mtlsEnabled bool) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		s.grants(w, r, mtlsListener, mtlsEnabled)
	}
}

func (s *server) grants(w http.ResponseWriter, r *http.Request, mtlsListener, mtlsEnabled bool) {
	parts := strings.Split(strings.Trim(strings.TrimPrefix(r.URL.Path, "/grants/"), "/"), "/")
	tokenRequest := len(parts) == 2 && parts[1] == "token" && r.Method == http.MethodGet
	if tokenRequest && mtlsListener {
		callerID := ""
		if r.TLS != nil && len(r.TLS.PeerCertificates) > 0 {
			if id, err := x509svid.IDFromCert(r.TLS.PeerCertificates[0]); err == nil {
				callerID = id.String()
			}
		}
		s.logger.Info("tokenbroker SPIFFE token request", "grant", parts[0], "spiffe_id", callerID)
		s.tokenRequests.WithLabelValues("mtls", "served").Inc()
	}
	valid := tokenRequest ||
		(len(parts) == 2 && parts[1] == "refresh" && r.Method == http.MethodPost) ||
		(len(parts) == 3 && parts[1] == "login" && ((parts[2] == "start" && r.Method == http.MethodPost) || (parts[2] == "status" && r.Method == http.MethodGet)))
	if !valid {
		http.NotFound(w, r)
		return
	}
	if _, ok := s.configs[parts[0]]; !ok {
		http.NotFound(w, r)
		return
	}
	if parts[1] == "token" && mtlsEnabled && !mtlsListener {
		s.logger.Info("tokenbroker plaintext token request rejected", "path", r.URL.Path, "remote_addr", r.RemoteAddr)
		s.tokenRequests.WithLabelValues("plaintext", "rejected_plaintext").Inc()
		w.Header().Set("Content-Type", "text/plain; charset=utf-8")
		w.WriteHeader(http.StatusForbidden)
		_, _ = w.Write([]byte("token endpoint requires mTLS on the SPIFFE port"))
		return
	}
	switch {
	case parts[1] == "token":
		s.token(parts[0], w, r)
	case parts[1] == "refresh":
		s.forceRefresh(parts[0], w, r)
	case parts[2] == "start":
		s.loginStart(parts[0], w, r)
	case parts[2] == "status":
		s.loginStatus(parts[0], w, r)
	}
}

func newTokenRequestsCounter() *prometheus.CounterVec {
	return prometheus.NewCounterVec(prometheus.CounterOpts{
		Name: "tokenbroker_token_requests_total",
		Help: "Token endpoint requests by listener and authorization outcome.",
	}, []string{"listener", "outcome"})
}

func (s *server) forceRefresh(name string, w http.ResponseWriter, r *http.Request) {
	value, _ := s.forced.LoadOrStore(name, &forceRefreshState{})
	state := value.(*forceRefreshState)
	state.mu.Lock()
	if !state.lastForced.IsZero() && time.Since(state.lastForced) < forceRefreshCooldown {
		state.mu.Unlock()
		writeJSON(w, http.StatusTooManyRequests, map[string]string{"reason": "cooldown"})
		return
	}
	state.lastForced = time.Now()
	state.mu.Unlock()

	_, expiry, err := s.broker.RefreshAccessToken(name, r.Context())
	if err != nil {
		needsLogin := s.broker.NeedsLogin(name)
		s.logger.Error("tokenbroker forced refresh failed", "grant", name, "needs_login", needsLogin, "err", err)
		writeJSON(w, http.StatusServiceUnavailable, map[string]any{"reason": "refresh_failed", "needs_login": needsLogin})
		return
	}
	s.logger.Info("tokenbroker forced refresh succeeded", "grant", name, "expires_at", expiry.UTC().Format(time.RFC3339))
	writeJSON(w, http.StatusOK, map[string]any{"refreshed": true, "expires_at": expiry.UTC().Format(time.RFC3339)})
}

func (s *server) health(w http.ResponseWriter, _ *http.Request) {
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write([]byte("ok\n"))
}

func (s *server) token(name string, w http.ResponseWriter, r *http.Request) {
	access, expiry, err := s.broker.GetAccessToken(name, r.Context())
	if err != nil {
		reason := "refresh_failed"
		if errors.Is(err, broker.ErrNoGrant) {
			reason = "no_grant"
		}
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"reason": reason})
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"access_token": access, "expires_at": expiry.UTC().Format(time.RFC3339)})
}

func (s *server) loginStart(name string, w http.ResponseWriter, r *http.Request) {
	value, _ := s.logins.LoadOrStore(name, &loginState{state: "none"})
	state := value.(*loginState)
	state.mu.Lock()
	if state.state == "pending" {
		if state.code != nil {
			response := map[string]any{
				"reason":           "login_pending",
				"verification_url": state.code.VerificationURL,
				"user_code":        state.code.UserCode,
				"expires_in":       state.code.ExpiresIn,
			}
			state.mu.Unlock()
			writeJSON(w, http.StatusConflict, response)
			return
		}
		readyChan := state.ready
		state.mu.Unlock()
		if readyChan != nil {
			select {
			case <-readyChan:
				state.mu.RLock()
				if state.code != nil {
					response := map[string]any{
						"reason":           "login_pending",
						"verification_url": state.code.VerificationURL,
						"user_code":        state.code.UserCode,
						"expires_in":       state.code.ExpiresIn,
					}
					state.mu.RUnlock()
					writeJSON(w, http.StatusConflict, response)
					return
				}
				state.mu.RUnlock()
			case <-r.Context().Done():
			case <-time.After(s.startWaitTimeout):
			}
		}
		writeJSON(w, http.StatusConflict, map[string]string{"reason": "login_starting"})
		return
	}
	state.state, state.detail, state.code = "pending", "approval required", nil
	state.ready = make(chan struct{})
	readyChan := state.ready
	state.mu.Unlock()
	defer func() {
		state.mu.Lock()
		close(readyChan)
		if state.ready == readyChan {
			state.ready = nil
		}
		state.mu.Unlock()
	}()
	adapter, ok := s.adapters[s.configs[name].ProviderName]
	if !ok {
		s.setLogin(name, "failed", "provider does not support device login")
		writeJSON(w, http.StatusBadRequest, map[string]string{"reason": "device_login_unsupported"})
		return
	}
	code, err := adapter.StartDeviceFlow(r.Context())
	if err != nil {
		s.setLogin(name, "failed", err.Error())
		writeJSON(w, http.StatusBadGateway, map[string]string{"reason": "device_code_failed"})
		return
	}
	state.mu.Lock()
	codeCopy := code
	state.code = &codeCopy
	state.mu.Unlock()
	go s.pollLogin(name, adapter, code)
	writeJSON(w, http.StatusOK, map[string]any{"verification_url": code.VerificationURL, "user_code": code.UserCode, "expires_in": code.ExpiresIn})
}

func (s *server) pollLogin(name string, adapter provider.Adapter, code provider.DeviceCodeResponse) {
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Minute)
	defer cancel()
	auth, err := adapter.PollForAuthorization(ctx, code)
	if err == nil {
		tok, ex := adapter.ExchangeCode(ctx, auth)
		if ex != nil {
			err = ex
		} else {
			now := time.Now().UTC()
			err = s.broker.SaveGrant(store.Grant{Name: name, ProviderName: s.configs[name].ProviderName, LastRefresh: now, TokenBundle: store.TokenBundle{IDToken: tok.IDToken, AccessToken: tok.AccessToken, RefreshToken: tok.RefreshToken, LastRefresh: now, ExpiresAt: tok.ExpiresAt}})
		}
	}
	if err != nil {
		s.logger.Error("tokenbroker login failed", "grant", name, "err", err)
		s.setLogin(name, "failed", err.Error())
		return
	}
	s.broker.ClearNeedsLogin(name)
	s.setLogin(name, "granted", "device authorization complete")
}

func (s *server) loginStatus(name string, w http.ResponseWriter, _ *http.Request) {
	value, _ := s.logins.LoadOrStore(name, &loginState{state: "none"})
	state := value.(*loginState)
	state.mu.RLock()
	stateValue, detail := state.state, state.detail
	state.mu.RUnlock()
	if stateValue == "pending" {
		writeJSON(w, http.StatusOK, map[string]string{"state": stateValue, "detail": detail})
		return
	}
	if s.broker.NeedsLogin(name) {
		writeJSON(w, http.StatusOK, map[string]string{"state": "none", "detail": "refresh failed, device login required"})
		return
	}
	grant, err := s.store.LoadGrant(name)
	if err != nil {
		s.logger.Error("tokenbroker login status grant load failed", "grant", name, "err", err)
	} else if grant.TokenBundle.RefreshToken != "" {
		writeJSON(w, http.StatusOK, map[string]string{"state": "granted", "detail": "stored grant present"})
		return
	}
	if stateValue == "failed" {
		writeJSON(w, http.StatusOK, map[string]string{"state": stateValue, "detail": detail})
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"state": "none", "detail": "no stored grant"})
}

func (s *server) setLogin(name, stateValue, detail string) {
	value, _ := s.logins.LoadOrStore(name, &loginState{})
	state := value.(*loginState)
	state.mu.Lock()
	state.state, state.detail, state.code = stateValue, detail, nil
	state.mu.Unlock()
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func env(k, d string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return d
}
