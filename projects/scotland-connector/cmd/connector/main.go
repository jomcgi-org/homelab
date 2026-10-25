// Command connector serves the public Scotland connector: a read-only REST API
// under /v1 and an MCP endpoint at /mcp, both answering from an in-memory
// dataset refreshed from R2. Health, readiness and metrics are on a separate
// admin listener that the public route never reaches.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/analytics"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/connector"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/httpapi"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/mcp"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/meter"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/r2"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/store"
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/collectors"
	"github.com/prometheus/client_golang/prometheus/promhttp"
)

type config struct {
	now                            func() time.Time
	listenAddr, adminAddr          string
	r2Endpoint, r2Region           string
	r2Creds                        r2.Credentials
	bucket, sitesKey, historyKey   string
	walksKey                       string
	refreshInterval, shutdownGrace time.Duration
	publicBaseURL, version         string
	allowedOrigins                 []string
	export                         meter.ExportConfig
	analytics                      analytics.Config
	policy                         *meter.Policy
}

func env(key, def string) string {
	if v := strings.TrimSpace(os.Getenv(key)); v != "" {
		return v
	}
	return def
}

func loadConfig() (config, error) {
	c := config{
		listenAddr:     env("LISTEN_ADDR", ":8080"),
		adminAddr:      env("ADMIN_ADDR", ":9090"),
		r2Endpoint:     env("R2_ENDPOINT", ""),
		r2Region:       env("AWS_REGION", "auto"),
		r2Creds:        r2.Credentials{AccessKeyID: env("S3_ACCESS_KEY_ID", ""), SecretAccessKey: env("S3_SECRET_ACCESS_KEY", "")},
		bucket:         env("STARS_BUCKET", "stars"),
		sitesKey:       env("STARS_SITES_KEY", "sites.json"),
		historyKey:     env("STARS_HISTORY_KEY", "history-map.json"),
		walksKey:       env("STARS_WALKS_KEY", "walks.json"),
		publicBaseURL:  strings.TrimRight(env("PUBLIC_BASE_URL", ""), "/"),
		version:        env("SERVICE_VERSION", "dev"),
		allowedOrigins: strings.Split(env("MCP_ALLOWED_ORIGINS", "https://claude.ai,https://claude.com,https://chatgpt.com"), ","),
	}
	var err error
	if c.export.Enabled, err = strconv.ParseBool(env("METER_EXPORT_ENABLED", "false")); err != nil {
		return c, errors.New("invalid METER_EXPORT_ENABLED")
	}
	c.export.Endpoint, c.export.Instance = env("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", ""), env("POD_UID", "")
	if err := c.export.Validate(); err != nil {
		return c, err
	}
	if c.policy, err = meter.ParsePolicy(env("TOOL_UNIT_PRICES", meter.DefaultPrices)); err != nil {
		return c, err
	}
	if c.analytics.Enabled, err = strconv.ParseBool(env("CACHE_ANALYTICS_ENABLED", "false")); err != nil {
		return c, errors.New("invalid CACHE_ANALYTICS_ENABLED")
	}
	if c.analytics.Enabled {
		c.analytics.Zone, c.analytics.Hostname, c.analytics.PlanPath, c.analytics.Token = env("CACHE_ANALYTICS_ZONE", ""), env("CACHE_ANALYTICS_HOSTNAME", ""), env("CACHE_ANALYTICS_PLAN_PATH", ""), env("CACHE_ANALYTICS_TOKEN", "")
		if err := c.analytics.Validate(); err != nil {
			return c, err
		}
		base, err := url.Parse(c.publicBaseURL)
		if err != nil || base.Hostname() != c.analytics.Hostname || base.Path+"/v1/plan" != c.analytics.PlanPath {
			return c, errors.New("analytics population must match PUBLIC_BASE_URL")
		}
	}
	if c.refreshInterval, err = time.ParseDuration(env("REFRESH_INTERVAL", "60s")); err != nil || c.refreshInterval < 5*time.Second {
		return c, fmt.Errorf("REFRESH_INTERVAL must be a duration of at least 5s")
	}
	if c.shutdownGrace, err = time.ParseDuration(env("SHUTDOWN_GRACE", "20s")); err != nil {
		return c, fmt.Errorf("SHUTDOWN_GRACE: %w", err)
	}
	if c.r2Endpoint == "" {
		return c, errors.New("R2_ENDPOINT is required")
	}
	return c, nil
}

func main() {
	log := slog.New(slog.NewJSONHandler(os.Stdout, nil))
	if err := run(log); err != nil {
		log.Error("connector exiting", "err", err)
		os.Exit(1)
	}
}

// app is the wired service: the public and admin handlers over the stars
// store (which gates readiness) and the optional walk store.
type app struct {
	public, admin http.Handler
	store         *store.Store
	walks         *store.WalkStore
	draining      *atomic.Bool
	meter         *meter.Meter
	analytics     *analytics.Reader
}

func newApp(cfg config, log *slog.Logger, httpClient *http.Client) (*app, error) {
	client, err := r2.New(cfg.r2Endpoint, cfg.r2Region, cfg.r2Creds, httpClient)
	if err != nil {
		return nil, err
	}
	reg := prometheus.NewRegistry()
	reg.MustRegister(collectors.NewGoCollector(), collectors.NewProcessCollector(collectors.ProcessCollectorOpts{}))
	policy := cfg.policy
	if policy == nil {
		policy, _ = meter.ParsePolicy(meter.DefaultPrices)
	}
	m := meter.NewWithPolicy(reg, policy)
	cache, err := analytics.New(cfg.analytics, nil)
	if err != nil {
		return nil, err
	}
	reg.MustRegister(cache)
	m.Cache = cache

	st := store.New(store.Config{Bucket: cfg.bucket, SitesKey: cfg.sitesKey, HistoryKey: cfg.historyKey, Interval: cfg.refreshInterval}, client, log)
	reg.MustRegister(prometheus.NewGaugeFunc(prometheus.GaugeOpts{
		Name: "scotland_connector_forecast_age_seconds",
		Help: "Seconds since the newest MET Norway fetch in the live snapshot.",
	}, func() float64 {
		if ds := st.Current(); ds != nil && !ds.FetchedAt.IsZero() {
			return time.Since(ds.FetchedAt).Seconds()
		}
		return -1
	}))

	ws := store.NewWalks(cfg.bucket, cfg.walksKey, cfg.refreshInterval, client, log)
	reg.MustRegister(prometheus.NewGaugeFunc(prometheus.GaugeOpts{
		Name: "scotland_connector_walks_forecast_age_seconds",
		Help: "Seconds since the newest MET Norway fetch in the live walk snapshot.",
	}, func() float64 {
		if wd := ws.Current(); wd != nil && !wd.FetchedAt.IsZero() {
			return time.Since(wd.FetchedAt).Seconds()
		}
		return -1
	}))
	now := cfg.now
	if now == nil {
		now = time.Now
	}
	svc := &connector.Service{Data: st, Walks: ws, MethodURL: cfg.publicBaseURL + "/v1/method", Now: now}
	info := mcp.ServerInfo{Name: "scotland-connector", Title: "Scotland outdoors", Version: cfg.version}

	api, err := httpapi.New(svc, m, log, cfg.publicBaseURL)
	if err != nil {
		return nil, err
	}
	mcpHandler := mcp.New(svc, m, log, info, cfg.allowedOrigins)
	// A plain dispatch rather than a ServeMux: the mux answers "/v1" and
	// unclean paths with a 301 to a root-relative Location, which behind a
	// prefix-stripping route (jomcgi.dev/scotland) sends the client out of
	// the prefix. The API handler 404s those paths itself.
	public := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch p := r.URL.Path; {
		case p == "/mcp":
			mcpHandler.ServeHTTP(w, r)
		case p == "/v1" || strings.HasPrefix(p, "/v1/"):
			api.ServeHTTP(w, r)
		default:
			http.NotFound(w, r)
		}
	})

	draining := new(atomic.Bool)
	admin := http.NewServeMux()
	admin.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusOK) })
	admin.HandleFunc("GET /readyz", func(w http.ResponseWriter, _ *http.Request) {
		ds := st.Current()
		status := http.StatusOK
		if ds == nil || draining.Load() {
			status = http.StatusServiceUnavailable
		}
		body := map[string]any{"ready": status == http.StatusOK, "draining": draining.Load(), "last_checked": st.LastChecked()}
		if ds != nil {
			body["sites"], body["data_version"], body["forecast_fetched_at"] = len(ds.Sites), ds.Version, ds.FetchedAt
		}
		if wd := ws.Current(); wd != nil {
			body["walks"], body["walks_version"] = len(wd.Walks), wd.Version
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		_ = json.NewEncoder(w).Encode(body)
	})
	admin.Handle("GET /metrics", promhttp.HandlerFor(reg, promhttp.HandlerOpts{}))
	return &app{public: public, admin: admin, store: st, walks: ws, draining: draining, meter: m, analytics: cache}, nil
}

// loadUntilReady retries the first load with backoff, then polls forever.
func (a *app) loadUntilReady(ctx context.Context, log *slog.Logger) {
	for backoff := time.Second; ctx.Err() == nil; backoff = min(2*backoff, 30*time.Second) {
		rctx, cancel := context.WithTimeout(ctx, 30*time.Second)
		err := a.store.Refresh(rctx)
		cancel()
		if err == nil {
			break
		}
		log.Warn("initial stars snapshot load failed", "err", err, "retry_in", backoff)
		select {
		case <-ctx.Done():
			return
		case <-time.After(backoff):
		}
	}
	a.store.Run(ctx)
}

func run(log *slog.Logger) error {
	cfg, err := loadConfig()
	if err != nil {
		return err
	}
	a, err := newApp(cfg, log, &http.Client{Timeout: 30 * time.Second})
	if err != nil {
		return err
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	shutdownExport, err := a.meter.StartExport(ctx, cfg.export)
	if err != nil {
		return err
	}
	defer func() { _ = shutdownExport(context.Background()) }()

	publicSrv := &http.Server{
		Addr:              cfg.listenAddr,
		Handler:           a.public,
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       10 * time.Second,
		WriteTimeout:      15 * time.Second,
		IdleTimeout:       120 * time.Second,
		MaxHeaderBytes:    16 << 10,
	}
	adminSrv := &http.Server{Addr: cfg.adminAddr, Handler: a.admin, ReadHeaderTimeout: 5 * time.Second}
	errc := make(chan error, 2)
	for _, srv := range []*http.Server{publicSrv, adminSrv} {
		go func() {
			if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
				errc <- err
			}
		}()
	}

	go a.loadUntilReady(ctx, log)
	go a.walks.Run(ctx)
	go a.analytics.Run(ctx)
	log.Info("connector listening", "public", cfg.listenAddr, "admin", cfg.adminAddr, "version", cfg.version)

	select {
	case err := <-errc:
		return err
	case <-ctx.Done():
	}
	// Fail readiness first so endpoints drop this pod, then drain. The pod's
	// preStop sleep covers endpoint propagation before SIGTERM arrives.
	a.draining.Store(true)
	sctx, cancel := context.WithTimeout(context.Background(), cfg.shutdownGrace)
	defer cancel()
	err = publicSrv.Shutdown(sctx)
	_ = adminSrv.Shutdown(sctx)
	return err
}
