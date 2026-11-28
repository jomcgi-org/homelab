// Command bosun-notes-view serves the members' read-only viewer of Bosun's
// notes vault (HOME-920, projects/factory/discord/notesview) behind the edge
// at notes.jomcgi.dev. Its key may only read bucket bosun-notes.
package main

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/jomcgi/homelab/projects/factory/discord/artifacts"
	"github.com/jomcgi/homelab/projects/factory/discord/notesview"
)

func main() {
	log := slog.New(slog.NewJSONHandler(os.Stderr, nil))
	if err := run(log); err != nil {
		log.Error("bosun-notes-view stopped", "err", err)
		os.Exit(1)
	}
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
	for _, s := range strings.Split(os.Getenv("NOTES_SERVERS"), ",") {
		if s = strings.TrimSpace(s); s != "" {
			servers[s] = true
		}
	}
	if len(servers) == 0 {
		return errors.New("NOTES_SERVERS is required")
	}
	ttl := 30 * time.Second
	if v := os.Getenv("INDEX_TTL"); v != "" {
		if ttl, err = time.ParseDuration(v); err != nil {
			return err
		}
	}
	vault := &notesview.Vault{Bucket: s3, Servers: servers, TTL: ttl}
	addr := os.Getenv("ADDR")
	if addr == "" {
		addr = ":8080"
	}
	srv := &http.Server{
		Addr:              addr,
		Handler:           notesview.NewHandler(vault, log),
		ReadHeaderTimeout: 10 * time.Second,
		ReadTimeout:       time.Minute,
		WriteTimeout:      time.Minute,
	}
	go func() {
		<-ctx.Done()
		shutdown, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		_ = srv.Shutdown(shutdown)
	}()
	log.Info("listening", "addr", addr, "servers", len(servers), "bucket", os.Getenv("BUCKET"))
	if err := srv.ListenAndServe(); err != http.ErrServerClosed {
		return err
	}
	return nil
}
