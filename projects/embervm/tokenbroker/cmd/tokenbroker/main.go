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
}
type server struct {
	broker   *broker.Broker
	store    *store.SecretStore
	adapters map[string]provider.Adapter
	configs  map[string]grantConfig
	logins   sync.Map
	logger   *slog.Logger
}

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
	go func() {
		if metricsErr := http.ListenAndServe(":9090", promhttp.Handler()); metricsErr != nil {
			logger.Error("metrics server stopped", "err", metricsErr)
		}
	}()
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
	valid := (len(parts) == 2 && parts[1] == "token" && r.Method == http.MethodGet) || (len(parts) == 3 && parts[1] == "login" && ((parts[2] == "start" && r.Method == http.MethodPost) || (parts[2] == "status" && r.Method == http.MethodGet)))
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
	case parts[2] == "start":
		s.loginStart(parts[0], w, r)
	case parts[2] == "status":
		s.loginStatus(parts[0], w, r)
	}
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
		state.mu.Unlock()
		writeJSON(w, http.StatusConflict, map[string]string{"reason": "login_pending"})
		return
	}
	state.state, state.detail = "pending", "approval required"
	state.mu.Unlock()
	adapter := s.adapters[s.configs[name].ProviderName]
	code, err := adapter.StartDeviceFlow(r.Context())
	if err != nil {
		s.setLogin(name, "failed", err.Error())
		writeJSON(w, http.StatusBadGateway, map[string]string{"reason": "device_code_failed"})
		return
	}
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
	s.setLogin(name, "granted", "device authorization complete")
}

func (s *server) loginStatus(name string, w http.ResponseWriter, _ *http.Request) {
	value, _ := s.logins.LoadOrStore(name, &loginState{state: "none"})
	state := value.(*loginState)
	state.mu.RLock()
	defer state.mu.RUnlock()
	writeJSON(w, http.StatusOK, map[string]string{"state": state.state, "detail": state.detail})
}

func (s *server) setLogin(name, stateValue, detail string) {
	value, _ := s.logins.LoadOrStore(name, &loginState{})
	state := value.(*loginState)
	state.mu.Lock()
	state.state, state.detail = stateValue, detail
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
