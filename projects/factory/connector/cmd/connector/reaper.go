package main

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/internal/reaper"
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/collectors"
	"github.com/prometheus/client_golang/prometheus/promhttp"

	core "k8s.io/client-go/kubernetes/typed/core/v1"
	"k8s.io/client-go/rest"
)

func runReaper(log *slog.Logger) error {
	// FACTORY_REAPER_POOLS lists the pools whose demand the reaper publishes
	// even while they are idle, as "namespace/name=spare,..." (chart
	// templates/reaper.yaml). Pools with actors are published regardless.
	pools, err := reaper.ParsePools(os.Getenv("FACTORY_REAPER_POOLS"))
	if err != nil {
		return err
	}
	demand := reaper.NewDemand(pools)
	reg := prometheus.NewRegistry()
	reg.MustRegister(collectors.NewGoCollector(), collectors.NewProcessCollector(collectors.ProcessCollectorOpts{}), demand)
	addr := os.Getenv("FACTORY_REAPER_METRICS_ADDR")
	if addr == "" {
		addr = ":9090"
	}
	mux := http.NewServeMux()
	mux.Handle("/metrics", promhttp.HandlerFor(reg, promhttp.HandlerOpts{}))
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusOK) })
	srv := &http.Server{Addr: addr, Handler: mux, ReadHeaderTimeout: 5 * time.Second}
	go func() {
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Error("reaper metrics server", "error", err)
		}
	}()
	defer srv.Close()

	cfg, err := rest.InClusterConfig()
	if err != nil {
		return err
	}
	cfg.Timeout = 5 * time.Second
	client, err := core.NewForConfig(cfg)
	if err != nil {
		return err
	}
	st, err := operatorStore()
	if err != nil {
		return err
	}
	defer st.DB.Close()
	actors := openActorDB()
	defer actors.Close()
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()
	ticker := time.NewTicker(15 * time.Second)
	defer ticker.Stop()
	var lease *reaper.Lease
	defer func() { lease.Close() }()
	for {
		pass, cancel := context.WithTimeout(ctx, 45*time.Second)
		// The census only reads, so it runs without the lease. A failed one
		// leaves the last census to age out and publish nothing.
		if busy, cerr := reaper.Census(pass, actors); cerr != nil {
			log.Error("worker pool census", "error", cerr)
		} else {
			demand.Observe(busy)
		}
		if lease == nil {
			lease, err = reaper.TryLease(pass, st.DB)
		}
		switch {
		case err != nil:
			log.Error("worker reaper lease", "error", err)
		case lease == nil:
			log.Info("worker reaper lease held elsewhere")
		default:
			err = reaper.Scan(pass, actors, st.DB, client, lease)
			if errors.Is(err, reaper.ErrNotLeader) {
				lease.Close()
				lease = nil // Take a new lease, and a new epoch, next pass.
			}
			if err != nil {
				log.Error("worker reaper scan", "error", err)
			}
		}
		cancel()
		err = nil
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-ticker.C:
		}
	}
}
