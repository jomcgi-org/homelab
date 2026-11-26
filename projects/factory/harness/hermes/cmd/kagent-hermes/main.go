// Command kagent-hermes runs Hermes Agent behind an A2A endpoint.
//
// It renders Hermes' configuration into HERMES_HOME, starts `hermes gateway`
// with only the API server on loopback, serves A2A JSON-RPC SendMessage and
// reports readiness on /readyz. Arguments after the flags replace the
// gateway command.
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/jomcgi/homelab/projects/factory/harness/hermes/adapter"
)

func main() {
	logger := slog.New(slog.NewJSONHandler(os.Stderr, nil))
	if err := run(logger, os.Args[1:]); err != nil {
		logger.Error("kagent-hermes stopped", "error", err)
		os.Exit(1)
	}
}

func run(logger *slog.Logger, args []string) error {
	fs := flag.NewFlagSet("kagent-hermes", flag.ContinueOnError)
	listen := fs.String("listen", ":80", "A2A listen address")
	ready := fs.String("ready", ":8081", "readiness listen address (/readyz)")
	home := fs.String("home", envOr("HERMES_HOME", "/data/hermes"), "Hermes home directory")
	port := fs.Int("hermes-port", 8642, "Hermes API server port on 127.0.0.1")
	startup := fs.Duration("startup-timeout", 2*time.Minute, "time to wait for the gateway to become healthy")
	turnTimeout := fs.Duration("turn-timeout", 10*time.Minute, "longest one Hermes turn may run")
	if err := fs.Parse(args); err != nil {
		return err
	}
	command := fs.Args()
	if len(command) == 0 {
		command = adapter.DefaultCommand
	}
	settings := adapter.Settings{
		Model:   os.Getenv("HERMES_MODEL"),
		BaseURL: os.Getenv("HERMES_BASE_URL"),
		APIKey:  os.Getenv("HERMES_API_KEY"),
	}
	if err := adapter.WriteHome(*home, settings); err != nil {
		return err
	}
	key, err := adapter.NewKey()
	if err != nil {
		return err
	}
	gw := &adapter.Gateway{Command: command, Home: *home, Port: *port, Key: key}
	if err := gw.Start(); err != nil {
		return err
	}
	defer gw.Stop(20 * time.Second)

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()

	readyMux := http.NewServeMux()
	readyMux.HandleFunc("/readyz", func(w http.ResponseWriter, r *http.Request) {
		if !gw.Healthy(r.Context()) {
			http.Error(w, "hermes gateway not healthy", http.StatusServiceUnavailable)
			return
		}
		fmt.Fprintln(w, "ok")
	})
	a2a := &adapter.Handler{
		Hermes: &adapter.Client{BaseURL: gw.URL(), Key: key, HTTP: &http.Client{Timeout: *turnTimeout}},
		Logger: logger,
	}
	servers := []*http.Server{
		{Addr: *ready, Handler: readyMux, ReadHeaderTimeout: 10 * time.Second},
		{Addr: *listen, Handler: a2a, ReadHeaderTimeout: 10 * time.Second},
	}
	errs := make(chan error, len(servers))
	for _, srv := range servers {
		go func() {
			if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
				errs <- err
			}
		}()
	}
	defer func() {
		shutdown, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		for _, srv := range servers {
			_ = srv.Shutdown(shutdown)
		}
	}()

	startCtx, cancel := context.WithTimeout(ctx, *startup)
	err = gw.WaitHealthy(startCtx)
	cancel()
	if err != nil {
		return err
	}
	logger.Info("hermes gateway healthy", "a2a", *listen, "ready", *ready)

	select {
	case <-ctx.Done():
		return nil
	case err := <-errs:
		return err
	case <-gw.Exited():
		return fmt.Errorf("hermes gateway exited: %v", gw.ExitErr())
	}
}

func envOr(name, fallback string) string {
	if v := os.Getenv(name); v != "" {
		return v
	}
	return fallback
}
