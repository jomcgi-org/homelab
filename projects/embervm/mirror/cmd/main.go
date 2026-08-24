// Command embervm-git-mirror serves a node-local git mirror over the smart
// HTTP protocol (#4473). It runs as a sidecar container in every noded pod,
// bound to the pod's loopback so the ONLY way in is through the co-located
// egress-proxy's reserved mirror lane; nothing else in the cluster or on the
// node can dial it.
//
// Environment:
//
//	EMBERVM_MIRROR_LISTEN          listen addr (default 127.0.0.1:9419)
//	EMBERVM_MIRROR_ROOT            bare-clone root (default /var/lib/git-mirror/mirrors)
//	EMBERVM_MIRROR_REPOS           comma-separated owner/name repos to mirror
//	EMBERVM_MIRROR_REFRESH_INTERVAL  Go duration between fetch passes (default 60s)
//	EMBERVM_MIRROR_GITHUB_TOKEN    optional GitHub read token (private repos)
//	EMBERVM_MIRROR_GIT_BIN         git executable override (default "git")
package main

import (
	"context"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/jomcgi/homelab/projects/embervm/mirror/refresh"
	"github.com/jomcgi/homelab/projects/embervm/mirror/server"
)

const (
	defaultListen   = "127.0.0.1:9419"
	defaultRoot     = "/var/lib/git-mirror/mirrors"
	defaultInterval = 60 * time.Second
)

func main() {
	logger := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: slog.LevelInfo}))
	slog.SetDefault(logger)

	listen := envOr("EMBERVM_MIRROR_LISTEN", defaultListen)
	root := envOr("EMBERVM_MIRROR_ROOT", defaultRoot)
	repos := splitRepos(os.Getenv("EMBERVM_MIRROR_REPOS"))
	interval, err := parseDuration(os.Getenv("EMBERVM_MIRROR_REFRESH_INTERVAL"), defaultInterval)
	if err != nil {
		logger.Error("mirror: invalid refresh interval", "err", err)
		os.Exit(1)
	}

	if len(repos) == 0 {
		// Serving an empty mirror is a misconfiguration, not a degraded state:
		// every hydration would silently fall back to GitHub and nobody would
		// ever notice the sidecar is dead weight.
		logger.Error("mirror: EMBERVM_MIRROR_REPOS is empty; refusing to start")
		os.Exit(1)
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()

	refresher := refresh.New(refresh.Config{
		Root:     root,
		GitBin:   os.Getenv("EMBERVM_MIRROR_GIT_BIN"),
		Repos:    repos,
		Interval: interval,
		Token:    os.Getenv("EMBERVM_MIRROR_GITHUB_TOKEN"),
	}, logger)
	go refresher.Run(ctx)

	mux := server.New(root, logger).Handler()
	httpServer := &http.Server{
		Addr:              listen,
		Handler:           mux,
		ReadHeaderTimeout: 30 * time.Second,
	}
	errCh := make(chan error, 1)
	go func() {
		errCh <- httpServer.ListenAndServe()
	}()
	logger.Info("mirror: serving",
		"listen", listen,
		"root", root,
		"repos", repos,
		"refresh_interval", interval.String(),
	)

	select {
	case <-ctx.Done():
	case err := <-errCh:
		if err != nil && err != http.ErrServerClosed {
			logger.Error("mirror: listener failed", "err", err)
			os.Exit(1)
		}
	}
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_ = httpServer.Shutdown(shutdownCtx)
	stop()
}

func envOr(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

func splitRepos(raw string) []string {
	var out []string
	for _, part := range strings.Split(raw, ",") {
		if part = strings.TrimSpace(part); part != "" {
			out = append(out, part)
		}
	}
	return out
}

func parseDuration(raw string, fallback time.Duration) (time.Duration, error) {
	if raw == "" {
		return fallback, nil
	}
	if seconds, err := strconv.Atoi(raw); err == nil {
		return time.Duration(seconds) * time.Second, nil
	}
	d, err := time.ParseDuration(raw)
	if err != nil {
		return 0, err
	}
	return d, nil
}
