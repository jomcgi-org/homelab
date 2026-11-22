package main

import (
	"context"
	"errors"
	"log/slog"
	"os/signal"
	"syscall"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/internal/reaper"

	core "k8s.io/client-go/kubernetes/typed/core/v1"
	"k8s.io/client-go/rest"
)

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
