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
	"os"
	"os/signal"
	"strings"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/connector"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/httpapi"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/mcp"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/meter"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/places"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/r2"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/store"
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/collectors"
	"github.com/prometheus/client_golang/prometheus/promhttp"
)

type config struct {
	listenAddr, adminAddr          string
	r2Endpoint, r2Region           string
	r2Creds                        r2.Credentials
	bucket, sitesKey, historyKey   string
	refreshInterval, shutdownGrace time.Duration
	publicBaseURL, version         string
	allowedOrigins                 []string
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
		publicBaseURL:  strings.TrimRight(env("PUBLIC_BASE_URL", ""), "/"),
		version:        env("SERVICE_VERSION", "dev"),
		allowedOrigins: strings.Split(env("MCP_ALLOWED_ORIGINS", "https://claude.ai,https://claude.com,https://chatgpt.com"), ","),
	}
	var err error
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

// app is the wired service: the public and admin handlers over one store.
type app struct {
	public, admin http.Handler
	store         *store.Store
	draining      *atomic.Bool
}

func newApp(cfg config, log *slog.Logger, httpClient *http.Client) (*app, error) {
	client, err := r2.New(cfg.r2Endpoint, cfg.r2Region, cfg.r2Creds, httpClient)
	if err != nil {
		return nil, err
	}
	gaz, err := places.Seed()
	if err != nil {
		return nil, err
	}

	reg := prometheus.NewRegistry()
	reg.MustRegister(collectors.NewGoCollector(), collectors.NewProcessCollector(collectors.ProcessCollectorOpts{}))
	m := meter.New(reg)

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

	svc := &connector.Service{Data: st, Places: gaz, MethodURL: cfg.publicBaseURL + "/v1/method", Now: time.Now}
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
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		_ = json.NewEncoder(w).Encode(body)
	})
	admin.Handle("GET /metrics", promhttp.HandlerFor(reg, promhttp.HandlerOpts{}))
	return &app{public: public, admin: admin, store: st, draining: draining}, nil
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
