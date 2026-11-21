// Command broker is the factory credential broker: it relays model calls
// from kagent actors with the broker's own credentials, and mints GitHub App
// installation tokens per repository binding.
package main

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracegrpc"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"

	"github.com/jomcgi/homelab/projects/factory/broker/actorjwt"
	"github.com/jomcgi/homelab/projects/factory/broker/auth"
	"github.com/jomcgi/homelab/projects/factory/broker/config"
	"github.com/jomcgi/homelab/projects/factory/broker/github"
	"github.com/jomcgi/homelab/projects/factory/broker/grant"
	"github.com/jomcgi/homelab/projects/factory/broker/metrics"
	"github.com/jomcgi/homelab/projects/factory/broker/relay"
	"github.com/jomcgi/homelab/projects/factory/broker/usage"
	"github.com/jomcgi/homelab/projects/factory/connector/provenance"
	"github.com/jomcgi/homelab/projects/factory/taskpin"
	sharedmetrics "github.com/jomcgi/homelab/projects/shared/go/tokenbroker/metrics"
	"github.com/jomcgi/homelab/projects/shared/go/tokenbroker/provider/codexchatgpt"
	"github.com/jomcgi/homelab/projects/shared/go/tokenbroker/quota"
	"github.com/jomcgi/homelab/projects/shared/go/tokenbroker/store"
	_ "github.com/lib/pq"
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
	if os.Getenv("OTEL_EXPORTER_OTLP_ENDPOINT") != "" || os.Getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") != "" {
		exporter, err := otlptracegrpc.New(ctx)
		if err != nil {
			return fmt.Errorf("initialize trace exporter: %w", err)
		}
		provider := sdktrace.NewTracerProvider(sdktrace.WithResource(resource.Default()), sdktrace.WithBatcher(exporter))
		otel.SetTracerProvider(provider)
		defer func() {
			flush, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer cancel()
			if err := provider.Shutdown(flush); err != nil {
				logger.Error("trace shutdown failed", "err", err)
			}
		}()
	}
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
		// The backend enforces the limit itself, so a failed state write
		// keeps the last committed reading rather than none.
		persistent, err := quota.NewPersistentStore(&store.QuotaStateStore{Client: client, Namespace: cfg.Namespace, Name: cfg.Codex.QuotaConfigMap}, quota.KeepCommittedOnWriteFailure())
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
	if dsn := os.Getenv("BROKER_USAGE_DATABASE_URL"); dsn != "" {
		db, err := sql.Open("postgres", dsn)
		if err != nil {
			return errors.New("open token accounting database")
		}
		defer db.Close()
		db.SetMaxOpenConns(8)
		db.SetMaxIdleConns(2)
		d.runStore = &usage.Store{DB: db}
		d.relay.TokenUsage = d.runStore
	}
	go d.adminVerifier.Run(ctx)
	go d.verifier.Run(ctx)
	go d.grants.Run(ctx)
	go d.usage.Run(ctx)
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
	runStore      *usage.Store
	adminVerifier *actorjwt.Verifier
	verifier      *actorjwt.Verifier
	grants        *grant.Manager
	relay         *relay.Relay
	claude        *relay.Claude
	taskPin       http.Handler
	// usage keeps the grant's quota reading current between relayed calls.
	usage  *relay.UsageRefresher
	minter *github.Minter
	// reviewMinter mints the reviewer App's tokens; commentMinter its PR
	// comment tokens, apart so a permission the App lacks fails only those.
	// The comment token never leaves the broker: comments writes the
	// reviewer's PR comment with it.
	reviewMinter  *github.Minter
	commentMinter *github.Minter
	comments      *github.CommentProxy
	reviewer      *auth.Reviewer
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
	adminVerifier, err := actorjwt.New(actorjwt.Options{Issuer: "https://factory-auth.jomcgi.dev/application/o/factory-edge/", Audience: "factory-edge"})
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
		verifier:      verifier,
		adminVerifier: adminVerifier,
		grants:        grants,
		relay: &relay.Relay{
			Verifier: verifier, Tokens: grants, Upstream: cfg.Codex.UpstreamURL,
			// No overall timeout: responses stream for as long as a turn runs.
			Client: &http.Client{Transport: transport},
			Quota:  quotaStore, Metrics: m, Logger: logger, MaxRequestBytes: cfg.Codex.MaxRequestBytes,
			StreamIdleTimeout: cfg.Codex.StreamIdleTimeout.Duration, MaxConcurrent: cfg.Codex.MaxConcurrentRequests,
			MaxConcurrentPerActor: cfg.Codex.MaxConcurrentRequestsPerActor,
			TraceModels:           cfg.Codex.TraceModels,
			AllowedModels:         cfg.Codex.AllowedModels,
			ReasoningEffort:       cfg.Codex.ReasoningEffort,
		},
		usage: &relay.UsageRefresher{
			Tokens: grants, Upstream: cfg.Codex.UpstreamURL, Quota: quotaStore, Metrics: m, Logger: logger,
		},
		reviewer: &auth.Reviewer{Client: client, Audience: cfg.TokenReview.Audience, Logger: logger},
	}
	owners := map[string]string{}
	for _, role := range []struct {
		origin   string
		bindings []config.Binding
	}{{cfg.TaskPinOwnerURL, cfg.Bindings}, {cfg.ReviewTaskPinOwnerURL, cfg.ReviewBindings}} {
		if role.origin == "" {
			continue
		}
		for _, binding := range role.bindings {
			if owners[binding.Namespace] != "" {
				return nil, fmt.Errorf("ambiguous task pin owner in %s", binding.Namespace)
			}
			owners[binding.Namespace] = role.origin + taskpin.Path
		}
	}
	if len(owners) > 0 {
		d.taskPin = taskpin.Broker(verifier, owners)
	}
	if cfg.Claude.Enabled {
		d.claude = &relay.Claude{
			Verifier: verifier, TokenFile: cfg.Claude.TokenFile, ModelAllowlist: cfg.Claude.ModelAllowlist,
			Upstream: relay.ClaudeUpstream, Client: &http.Client{Transport: transport},
			Metrics: m, Logger: logger, MaxRequestBytes: cfg.Claude.MaxRequestBytes,
			MaxConcurrent: cfg.Claude.MaxConcurrentRequests, StreamIdleTimeout: cfg.Claude.StreamIdleTimeout.Duration,
		}
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
		app := &github.App{APIURL: r.APIURL, AppID: r.AppID, InstallationID: r.InstallationID, Key: key}
		d.reviewMinter = &github.Minter{
			App:    app,
			Client: client, Bindings: cfg.ReviewBindings, SecretName: r.AgentSecretName,
			AgentPermissions: r.AgentPermissions, ConnectorPermissions: r.ConnectorPermissions,
			Interval: r.MintInterval.Duration, RetryInterval: time.Minute, ConnectorMinLife: 15 * time.Minute,
			AgentKind: "review_agent", TokenKind: "reviewer", Metrics: m, Logger: logger,
		}
		if len(r.CommentPermissions) > 0 && r.CommentAuthor != "" {
			// Mints for the comment proxy only; it never runs, so it writes
			// no agent Secret.
			d.commentMinter = &github.Minter{
				App: app, Bindings: cfg.ReviewBindings, ConnectorPermissions: r.CommentPermissions,
				ConnectorMinLife: 15 * time.Minute, TokenKind: "reviewer_comment", Metrics: m, Logger: logger,
			}
			d.comments = &github.CommentProxy{Minter: d.commentMinter, Author: r.CommentAuthor, Logger: logger}
		}
	}
	return d, nil
}

// handler routes every endpoint on one listener. Everything except health
// and metrics authenticates: the relay by actor JWT, the rest by TokenReview.
func (d *deps) handler(cfg config.Config, metricsHandler http.Handler) http.Handler {
	mux := d.adminMux(cfg, metricsHandler)
	d.registerRelays(mux)
	return mux
}

// relayHandler serves only health and the relay, for actors.
func (d *deps) relayHandler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", healthz)
	d.registerRelays(mux)
	return mux
}

func (d *deps) registerRelays(mux *http.ServeMux) {
	if d.taskPin != nil {
		mux.Handle(taskpin.Path, d.taskPin)
	}
	if d.claude != nil {
		mux.Handle("/anthropic/", d.claude)
	}
	mux.Handle("/", d.relay)
}

// adminHandler serves health, metrics, the admin endpoints, connector
// tokens and the shared quota read, and nothing else.
func (d *deps) adminHandler(cfg config.Config, metricsHandler http.Handler) http.Handler {
	mux := d.adminMux(cfg, metricsHandler)
	mux.Handle("/", http.NotFoundHandler())
	return mux
}

func (d *deps) adminMux(cfg config.Config, metricsHandler http.Handler) *http.ServeMux {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", healthz)
	mux.Handle("GET /metrics", metricsHandler)
	admin := d.grants.AdminHandler()
	breakGlass := d.reviewer.Require(cfg.TokenReview.AdminSubjects, admin)
	mux.Handle("/admin/", breakGlass)
	browser := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Cache-Control", "no-store")
		if token, ok := auth.BearerToken(r); ok && d.adminVerifier != nil {
			if subject, err := d.adminVerifier.AdminSubject(r.Context(), token); err == nil {
				if r.Method == "POST" && r.Header.Get("X-Codex-Login") != "start" {
					http.Error(w, "forbidden", 403)
					return
				}
				admin.ServeHTTP(w, r.WithContext(auth.WithUser(r.Context(), subject)))
				return
			}
		}
		breakGlass.ServeHTTP(w, r)
	})
	mux.Handle("/admin/codex/{$}", browser)
	mux.Handle("/admin/codex/login", browser)
	connector := http.Handler(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusServiceUnavailable)
		_, _ = w.Write([]byte(`{"factory_error":"github_not_configured"}` + "\n"))
	}))
	if d.minter != nil {
		connector = d.minter.ConnectorHandler()
	}
	mux.Handle("/github/connector-token", d.reviewer.Require(cfg.TokenReview.ConnectorSubjects, connector))
	// Both dispatchers pause work until the shared grant reads usable.
	quotaSubjects := append(append([]string(nil), cfg.TokenReview.ConnectorSubjects...), cfg.TokenReview.ReviewerSubjects...)
	mux.Handle("GET /codex/quota", d.reviewer.Require(quotaSubjects, d.relay.QuotaHandler()))
	reviewer := http.Handler(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusServiceUnavailable)
		_, _ = w.Write([]byte(`{"factory_error":"github_review_not_configured"}` + "\n"))
	}))
	if d.reviewMinter != nil {
		reviewer = d.reviewMinter.ConnectorHandler()
	}
	mux.Handle("/github/reviewer-token", d.reviewer.Require(cfg.TokenReview.ReviewerSubjects, reviewer))
	comments := http.Handler(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusServiceUnavailable)
		_, _ = w.Write([]byte(`{"factory_error":"github_review_comments_not_configured"}` + "\n"))
	}))
	if d.comments != nil {
		comments = d.comments
	}
	// The reviewer's PR comment, written here so the comment token, which
	// could approve, never reaches the reviewer.
	mux.Handle("/github/reviewer-comments", d.reviewer.Require(cfg.TokenReview.ReviewerSubjects, comments))
	mux.Handle("/runs/reviewer", d.reviewer.Require(cfg.TokenReview.ReviewerSubjects, d.runStore.ReviewerHandler(cfg.ReviewBindings)))
	mux.Handle(provenance.BindReviewerTaskPath, d.reviewer.Require(cfg.TokenReview.ReviewerSubjects, d.runStore.ReviewerTaskHandler(cfg.ReviewBindings, true)))
	mux.Handle(provenance.ReadReviewerPinPath, d.reviewer.Require(cfg.TokenReview.ReviewerSubjects, d.runStore.ReviewerTaskHandler(cfg.ReviewBindings, false)))
	return mux
}

func healthz(w http.ResponseWriter, _ *http.Request) { _, _ = w.Write([]byte("ok\n")) }
