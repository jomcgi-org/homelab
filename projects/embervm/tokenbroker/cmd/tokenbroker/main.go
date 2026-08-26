package main

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"os"
	"strings"
	"sync"
	"time"

	"github.com/jomcgi/homelab/projects/embervm/tokenbroker/internal/broker"
	"github.com/jomcgi/homelab/projects/embervm/tokenbroker/internal/metrics"
	"github.com/jomcgi/homelab/projects/embervm/tokenbroker/internal/provider"
	"github.com/jomcgi/homelab/projects/embervm/tokenbroker/internal/provider/codexchatgpt"
	"github.com/jomcgi/homelab/projects/embervm/tokenbroker/internal/store"
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promhttp"
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
}
type forceRefreshState struct {
	mu         sync.Mutex
	lastForced time.Time
}
type server struct {
	broker   *broker.Broker
	store    store.Store
	adapters map[string]provider.Adapter
	configs  map[string]grantConfig
	logins   sync.Map
	forced   sync.Map
	logger   *slog.Logger
}

const forceRefreshCooldown = 60 * time.Second

func main() {
	logger := slog.New(slog.NewJSONHandler(os.Stdout, nil))
	cfg, err := rest.InClusterConfig()
	if err != nil {
		logger.Error("in-cluster config failed", "err", err)
		os.Exit(1)
	}
	client, err := kubernetes.NewForConfig(cfg)
	if err != nil {
		logger.Error("kubernetes client failed", "err", err)
		os.Exit(1)
	}
	configs := configuredGrants(logger)
	namespace := env("KUBERNETES_NAMESPACE", "embervm")
	st := &store.SecretStore{Client: client, Namespace: namespace}
	adapters := map[string]provider.Adapter{"codex-chatgpt": &codexchatgpt.Adapter{}}
	brokerConfigs := make([]broker.GrantConfig, 0, len(configs))
	configMap := make(map[string]grantConfig, len(configs))
	for _, c := range configs {
		brokerConfigs = append(brokerConfigs, broker.GrantConfig{Name: c.Name, ProviderName: c.ProviderName})
		configMap[c.Name] = c
	}
	m := metrics.New()
	m.Register(prometheus.DefaultRegisterer)
	s := &server{store: st, adapters: adapters, configs: configMap, logger: logger}
	s.broker = broker.New(st, adapters, brokerConfigs, logger, m)
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", s.health)
	mux.Handle("/metrics", promhttp.Handler())
	mux.HandleFunc("/grants/", s.grants)
	addr := env("BROKER_LISTEN_ADDR", ":8080")
	logger.Info("token broker listening", "addr", addr)
	if err = http.ListenAndServe(addr, mux); err != nil {
		logger.Error("server stopped", "err", err)
		os.Exit(1)
	}
}

func configuredGrants(logger *slog.Logger) []grantConfig {
	raw := env("TOKENBROKER_GRANTS", `[{"name":"codex-cluster","provider":"codex-chatgpt"}]`)
	var grants []grantConfig
	if err := json.Unmarshal([]byte(raw), &grants); err != nil || len(grants) == 0 {
		logger.Error("invalid TOKENBROKER_GRANTS", "err", err)
		os.Exit(1)
	}
	return grants
}

func (s *server) grants(w http.ResponseWriter, r *http.Request) {
	parts := strings.Split(strings.Trim(strings.TrimPrefix(r.URL.Path, "/grants/"), "/"), "/")
	valid := (len(parts) == 2 && parts[1] == "token" && r.Method == http.MethodGet) ||
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
		response := map[string]any{"reason": "login_pending", "verification_url": "", "user_code": "", "expires_in": provider.FlexInt(0)}
		if state.code != nil {
			response["verification_url"] = state.code.VerificationURL
			response["user_code"] = state.code.UserCode
			response["expires_in"] = state.code.ExpiresIn
		}
		state.mu.Unlock()
		writeJSON(w, http.StatusConflict, response)
		return
	}
	state.state, state.detail, state.code = "pending", "approval required", nil
	state.mu.Unlock()
	adapter := s.adapters[s.configs[name].ProviderName]
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
