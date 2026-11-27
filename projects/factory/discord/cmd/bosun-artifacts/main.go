// Command bosun-artifacts serves Bosun's artifact tools over MCP
// (projects/factory/discord/artifacts).
package main

import (
	"context"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/jomcgi/homelab/projects/factory/broker/actorjwt"
	"github.com/jomcgi/homelab/projects/factory/discord/artifacts"
)

func main() {
	log := slog.New(slog.NewJSONHandler(os.Stderr, nil))
	if err := run(log); err != nil {
		log.Error("bosun-artifacts stopped", "err", err)
		os.Exit(1)
	}
}

func list(name string) []string {
	var out []string
	for _, v := range strings.Split(os.Getenv(name), ",") {
		if v = strings.TrimSpace(v); v != "" {
			out = append(out, v)
		}
	}
	return out
}

func run(log *slog.Logger) error {
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, os.Interrupt)
	defer stop()

	s3, err := artifacts.NewS3(os.Getenv("AWS_ENDPOINT_URL"), os.Getenv("AWS_REGION"), os.Getenv("BUCKET"),
		os.Getenv("AWS_ACCESS_KEY_ID"), os.Getenv("AWS_SECRET_ACCESS_KEY"))
	if err != nil {
		return err
	}
	servers := map[string]bool{}
	for _, s := range list("ARTIFACT_SERVERS") {
		servers[s] = true
	}
	atespaces := list("ACTOR_JWT_ATESPACES")
	if len(atespaces) == 0 {
		// An empty list would admit every atespace's actors.
		return errorString("ACTOR_JWT_ATESPACES is required")
	}
	verifier, err := actorjwt.New(actorjwt.Options{
		Issuer:           os.Getenv("ACTOR_JWT_ISSUER"),
		Audience:         os.Getenv("ACTOR_JWT_AUDIENCE"),
		CAFile:           os.Getenv("ACTOR_JWT_CA_FILE"),
		AllowedAtespaces: atespaces,
		Logger:           log,
	})
	if err != nil {
		return err
	}
	go verifier.Run(ctx)

	mux := http.NewServeMux()
	mux.Handle("/mcp", &artifacts.Handler{
		Store:    &artifacts.Store{S3: s3, BaseURL: strings.TrimRight(os.Getenv("ARTIFACT_BASE_URL"), "/"), Servers: servers},
		Verifier: verifier,
		Log:      log,
	})
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusNoContent) })
	addr := os.Getenv("ADDR")
	if addr == "" {
		addr = ":8080"
	}
	srv := &http.Server{Addr: addr, Handler: mux, ReadHeaderTimeout: 10 * time.Second, ReadTimeout: time.Minute}
	go func() {
		<-ctx.Done()
		shutdown, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		_ = srv.Shutdown(shutdown)
	}()
	log.Info("listening", "addr", addr, "servers", len(servers), "atespaces", atespaces)
	if err := srv.ListenAndServe(); err != http.ErrServerClosed {
		return err
	}
	return nil
}

type errorString string

func (e errorString) Error() string { return string(e) }
