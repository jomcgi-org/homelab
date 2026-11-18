package main

import (
	"context"
	"database/sql"
	"log/slog"
	"os/signal"
	"syscall"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/internal/reaper"

	core "k8s.io/client-go/kubernetes/typed/core/v1"
	"k8s.io/client-go/rest"
)

const actorDB = "postgresql://factory_reaper@postgres.ate-system.svc:5432/atepg?sslmode=verify-full&sslrootcert=/var/run/ate/ca.pem&sslcert=/var/run/ate/client.pem&sslkey=/var/run/ate/client.pem"

func runReaper(log *slog.Logger) error {
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
	actors, err := sql.Open("postgres", actorDB)
	if err != nil {
		return err
	}
	defer actors.Close()
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()
	ticker := time.NewTicker(15 * time.Second)
	defer ticker.Stop()
	for {
		pass, cancel := context.WithTimeout(ctx, 45*time.Second)
		err := reaper.Scan(pass, actors, st.DB, client)
		cancel()
		if err != nil {
			log.Error("worker reaper scan", "error", err)
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-ticker.C:
		}
	}
}
