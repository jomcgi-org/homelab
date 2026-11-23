package main

import (
	"context"
	"database/sql"
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
	// FACTORY_REAPER_UNBOUND_POOLS lists the pools no connector binding sends
	// to, as "namespace/name,...": the reaper publishes their pending demand
	// at 0 (the connector publishes none for them). Each must also be in
	// FACTORY_REAPER_POOLS.
	unbound, err := reaper.ParsePools(os.Getenv("FACTORY_REAPER_UNBOUND_POOLS"))
	if err != nil {
		return err
	}
	if err := reaper.CheckUnbound(unbound, pools); err != nil {
		return err
	}
	demand := reaper.NewDemand(pools).Unbound(unbound)
	poolList := make([]reaper.Pool, 0, len(pools))
	for p := range pools {
		poolList = append(poolList, p)
	}
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
	// The census runs on its own timer, so a slow scan cannot let the census
	// age past the point where the series disappear.
	go runCensus(ctx, log, actors, demand)
	ticker := time.NewTicker(15 * time.Second)
	defer ticker.Stop()
	var lease *reaper.Lease
	defer func() { lease.Close() }()
	for {
		pass, cancel := context.WithTimeout(ctx, 45*time.Second)
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
			// The connector holds a send until its pool has a free worker. It
			// reads this row, so a stale or failed census writes none and the
			// row ages out to "unknown".
			if census, fresh := demand.Latest(); lease != nil && fresh {
				if err := reaper.RecordCapacity(pass, lease, client, poolList, census.Busy); errors.Is(err, reaper.ErrNotLeader) {
					lease.Close()
					lease = nil
				} else if err != nil {
					log.Error("worker pool capacity", "error", err)
				}
				// Idle workers go first when the autoscaler lowers replicas.
				if err := reaper.ProtectBusyWorkers(pass, client, poolList, census.Pods); err != nil {
					log.Error("worker deletion cost", "error", err)
				}
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

// runCensus counts the busy workers of every pool each 15 s and publishes them.
// It only reads. A failed or unreadable census drops the published series.
func runCensus(ctx context.Context, log *slog.Logger, actors *sql.DB, demand *reaper.Demand) {
	ticker := time.NewTicker(15 * time.Second)
	defer ticker.Stop()
	for {
		pass, cancel := context.WithTimeout(ctx, 10*time.Second)
		snap, err := reaper.Take(pass, actors)
		cancel()
		if err != nil {
			demand.Invalidate()
			log.Error("worker pool census", "error", err)
		} else {
			demand.Observe(snap)
		}
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
	}
}
