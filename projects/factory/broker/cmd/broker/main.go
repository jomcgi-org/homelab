// Command broker is the factory credential broker: it relays Codex model
// calls from kagent actors with the broker's own grant, and mints GitHub App
// installation tokens per repository binding.
package main

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/jomcgi/homelab/projects/factory/broker/actorjwt"
	"github.com/jomcgi/homelab/projects/factory/broker/auth"
	"github.com/jomcgi/homelab/projects/factory/broker/config"
	"github.com/jomcgi/homelab/projects/factory/broker/github"
	"github.com/jomcgi/homelab/projects/factory/broker/grant"
	"github.com/jomcgi/homelab/projects/factory/broker/metrics"
	"github.com/jomcgi/homelab/projects/factory/broker/relay"
	sharedmetrics "github.com/jomcgi/homelab/projects/shared/go/tokenbroker/metrics"
	"github.com/jomcgi/homelab/projects/shared/go/tokenbroker/provider/codexchatgpt"
	"github.com/jomcgi/homelab/projects/shared/go/tokenbroker/quota"
	"github.com/jomcgi/homelab/projects/shared/go/tokenbroker/store"
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/collectors"
	"github.com/prometheus/client_golang/prometheus/promhttp"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"
)

func main() {
	logger := slog.New(slog.NewJSONHandler(os.Stdout, nil))
	if err := run(logger); err != nil {
		logger.Error("broker stopped", "err", err)
		os.Exit(1)
	}
}

func run(logger *slog.Logger) error {
	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancel()
	cfg, err := config.Load(os.Getenv("BROKER_CONFIG"), os.Getenv)
	if err != nil {
		return err
	}
	restConfig, err := rest.InClusterConfig()
	if err != nil {
		return fmt.Errorf("in-cluster config: %w", err)
	}
	client, err := kubernetes.NewForConfig(restConfig)
	if err != nil {
		return fmt.Errorf("kubernetes client: %w", err)
	}

	registry := prometheus.NewRegistry()
	registry.MustRegister(collectors.NewGoCollector(), collectors.NewProcessCollector(collectors.ProcessCollectorOpts{}))
	m := metrics.New()
	m.Register(registry)

	quotaStore := quota.NewStore()
	if cfg.Codex.QuotaConfigMap != "" {
		persistent, err := quota.NewPersistentStore(&store.QuotaStateStore{Client: client, Namespace: cfg.Namespace, Name: cfg.Codex.QuotaConfigMap})
		if err != nil {
			// Usable but empty: quota reads stay unobserved until a fresh
			// observation commits.
			logger.Error("quota state unavailable", "err", err)
		}
		quotaStore = persistent
	}
	registry.MustRegister(sharedmetrics.NewQuotaCollectorWithPrefix(metrics.Prefix, quotaStore, []string{relay.QuotaProvider}))

	d, err := build(cfg, client, quotaStore, m, logger)
	if err != nil {
		return err
	}
	go d.verifier.Run(ctx)
	go d.grants.Run(ctx)
	if d.minter != nil {
		go d.minter.Run(ctx)
	} else {
		logger.Warn("GitHub App not configured; no tokens will be minted")
	}

	server := &http.Server{
		Addr:              cfg.ListenAddr,
		Handler:           d.handler(cfg, promhttp.HandlerFor(registry, promhttp.HandlerOpts{})),
		ReadHeaderTimeout: 10 * time.Second,
	}
	errc := make(chan error, 1)
	go func() {
		logger.Info("broker listening", "addr", cfg.ListenAddr)
		errc <- server.ListenAndServe()
	}()
	select {
	case err := <-errc:
		return err
	case <-ctx.Done():
	}
	shutdown, stop := context.WithTimeout(context.Background(), 30*time.Second)
	defer stop()
	if err := server.Shutdown(shutdown); err != nil && !errors.Is(err, http.ErrServerClosed) {
		return err
	}
	return nil
}

type deps struct {
	verifier *actorjwt.Verifier
	grants   *grant.Manager
	relay    *relay.Relay
	minter   *github.Minter
	reviewer *auth.Reviewer
}

func build(cfg config.Config, client kubernetes.Interface, quotaStore *quota.Store, m *metrics.Metrics, logger *slog.Logger) (*deps, error) {
	verifier, err := actorjwt.New(actorjwt.Options{
		Issuer: cfg.ActorJWT.Issuer, Audience: cfg.ActorJWT.Audience, CAFile: cfg.ActorJWT.CAFile,
		RefreshInterval: cfg.ActorJWT.KeyRefreshInterval.Duration, MinRefetch: cfg.ActorJWT.MinKeyRefetch.Duration,
		AllowedAtespaces: cfg.ActorJWT.AllowedAtespaces, Logger: logger,
	})
	if err != nil {
		return nil, err
	}
	grants := grant.New(grant.Options{
		Grant: cfg.Codex.Grant, Client: client, Namespace: cfg.Namespace, SecretPrefix: cfg.Codex.SecretPrefix,
		Adapter:             &codexchatgpt.Adapter{Issuer: cfg.Codex.AuthIssuer, HTTPClient: &http.Client{Timeout: 30 * time.Second}},
		RefreshBeforeExpiry: cfg.Codex.RefreshBeforeExpiry.Duration, MaxGrantAge: cfg.Codex.MaxGrantAge.Duration,
		CheckInterval: cfg.Codex.RefreshCheckInterval.Duration, Metrics: m, Logger: logger,
	})
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.ResponseHeaderTimeout = 2 * time.Minute
	d := &deps{
		verifier: verifier,
		grants:   grants,
		relay: &relay.Relay{
			Verifier: verifier, Tokens: grants, Upstream: cfg.Codex.UpstreamURL,
			// No overall timeout: responses stream for as long as a turn runs.
			Client: &http.Client{Transport: transport},
			Quota:  quotaStore, Metrics: m, Logger: logger, MaxRequestBytes: cfg.Codex.MaxRequestBytes,
		},
		reviewer: &auth.Reviewer{Client: client, Audience: cfg.TokenReview.Audience, Logger: logger},
	}
	if cfg.GitHubEnabled() {
		key, err := github.LoadPrivateKey(cfg.GitHub.PrivateKeyFile)
		if err != nil {
			return nil, err
		}
		d.minter = &github.Minter{
			App:    &github.App{APIURL: cfg.GitHub.APIURL, AppID: cfg.GitHub.AppID, InstallationID: cfg.GitHub.InstallationID, Key: key},
			Client: client, Bindings: cfg.Bindings, SecretName: cfg.GitHub.AgentSecretName,
			AgentPermissions: cfg.GitHub.AgentPermissions, ConnectorPermissions: cfg.GitHub.ConnectorPermissions,
			Interval: cfg.GitHub.MintInterval.Duration, RetryInterval: time.Minute, ConnectorMinLife: 15 * time.Minute,
			Metrics: m, Logger: logger,
		}
	}
	return d, nil
}

// handler routes the broker's endpoints. Everything except health and
// metrics authenticates: the relay by actor JWT, the rest by TokenReview.
func (d *deps) handler(cfg config.Config, metricsHandler http.Handler) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) { _, _ = w.Write([]byte("ok\n")) })
	mux.Handle("GET /metrics", metricsHandler)
	mux.Handle("/admin/", d.reviewer.Require(cfg.TokenReview.AdminSubjects, d.grants.AdminHandler()))
	connector := http.Handler(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusServiceUnavailable)
		_, _ = w.Write([]byte(`{"factory_error":"github_not_configured"}` + "\n"))
	}))
	if d.minter != nil {
		connector = d.minter.ConnectorHandler()
	}
	mux.Handle("/github/connector-token", d.reviewer.Require(cfg.TokenReview.ConnectorSubjects, connector))
	mux.Handle("/", d.relay)
	return mux
}
