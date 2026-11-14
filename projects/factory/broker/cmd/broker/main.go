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

	d, err := build(ctx, cfg, client, quotaStore, m, logger)
	if err != nil {
		return err
	}
	go d.verifier.Run(ctx)
	go d.grants.Run(ctx)
	if d.reviewMinter != nil {
		go d.reviewMinter.Run(ctx)
	}
	if d.minter != nil {
		go d.minter.Run(ctx)
	} else {
		logger.Warn("GitHub App not configured; no tokens will be minted")
	}

	metricsHandler := promhttp.HandlerFor(registry, promhttp.HandlerOpts{})
	listeners := map[string]http.Handler{cfg.ListenAddr: d.handler(cfg, metricsHandler)}
	if cfg.AdminListenAddr != "" {
		listeners = map[string]http.Handler{
			cfg.ListenAddr:      d.relayHandler(),
			cfg.AdminListenAddr: d.adminHandler(cfg, metricsHandler),
		}
	}
	servers := make([]*http.Server, 0, len(listeners))
	errc := make(chan error, len(listeners))
	for addr, handler := range listeners {
		server := &http.Server{
			Addr:              addr,
			Handler:           handler,
			ReadHeaderTimeout: 10 * time.Second,
			// Bounds the request body upload; responses stream without a
			// write timeout, ended instead by the relay's idle timeout.
			ReadTimeout: cfg.Codex.RequestReadTimeout.Duration,
			IdleTimeout: 2 * time.Minute,
		}
		servers = append(servers, server)
		go func() {
			logger.Info("broker listening", "addr", addr)
			errc <- server.ListenAndServe()
		}()
	}
	select {
	case err := <-errc:
		return err
	case <-ctx.Done():
	}
	shutdown, stop := context.WithTimeout(context.Background(), 30*time.Second)
	defer stop()
	for _, server := range servers {
		if err := server.Shutdown(shutdown); err != nil && !errors.Is(err, http.ErrServerClosed) {
			return err
		}
	}
	return nil
}

type deps struct {
	verifier *actorjwt.Verifier
	grants   *grant.Manager
	relay    *relay.Relay
	minter   *github.Minter
	// reviewMinter mints the reviewer App's tokens.
	reviewMinter *github.Minter
	reviewer     *auth.Reviewer
}

func build(ctx context.Context, cfg config.Config, client kubernetes.Interface, quotaStore *quota.Store, m *metrics.Metrics, logger *slog.Logger) (*deps, error) {
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
		CheckInterval: cfg.Codex.RefreshCheckInterval.Duration, Metrics: m, Logger: logger, Context: ctx,
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
			StreamIdleTimeout: cfg.Codex.StreamIdleTimeout.Duration, MaxConcurrent: cfg.Codex.MaxConcurrentRequests,
			ReasoningEffort: cfg.Codex.ReasoningEffort,
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
	if cfg.GitHubReviewEnabled() {
		r := cfg.GitHubReview
		key, err := github.LoadPrivateKey(r.PrivateKeyFile)
		if err != nil {
			return nil, err
		}
		d.reviewMinter = &github.Minter{
			App:    &github.App{APIURL: r.APIURL, AppID: r.AppID, InstallationID: r.InstallationID, Key: key},
			Client: client, Bindings: cfg.ReviewBindings, SecretName: r.AgentSecretName,
			AgentPermissions: r.AgentPermissions, ConnectorPermissions: r.ConnectorPermissions,
			Interval: r.MintInterval.Duration, RetryInterval: time.Minute, ConnectorMinLife: 15 * time.Minute,
			AgentKind: "review_agent", TokenKind: "reviewer", Metrics: m, Logger: logger,
		}
	}
	return d, nil
}

// handler routes every endpoint on one listener. Everything except health
// and metrics authenticates: the relay by actor JWT, the rest by TokenReview.
func (d *deps) handler(cfg config.Config, metricsHandler http.Handler) http.Handler {
	mux := d.adminMux(cfg, metricsHandler)
	mux.Handle("/", d.relay)
	return mux
}

// relayHandler serves only health and the relay, for actors.
func (d *deps) relayHandler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", healthz)
	mux.Handle("/", d.relay)
	return mux
}

// adminHandler serves health, metrics, the admin endpoints and connector
// tokens, and nothing else.
func (d *deps) adminHandler(cfg config.Config, metricsHandler http.Handler) http.Handler {
	mux := d.adminMux(cfg, metricsHandler)
	mux.Handle("/", http.NotFoundHandler())
	return mux
}

func (d *deps) adminMux(cfg config.Config, metricsHandler http.Handler) *http.ServeMux {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", healthz)
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
	reviewer := http.Handler(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusServiceUnavailable)
		_, _ = w.Write([]byte(`{"factory_error":"github_review_not_configured"}` + "\n"))
	}))
	if d.reviewMinter != nil {
		reviewer = d.reviewMinter.ConnectorHandler()
	}
	mux.Handle("/github/reviewer-token", d.reviewer.Require(cfg.TokenReview.ReviewerSubjects, reviewer))
	return mux
}

func healthz(w http.ResponseWriter, _ *http.Request) { _, _ = w.Write([]byte("ok\n")) }
