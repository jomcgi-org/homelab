// Command bosun-requests serves Bosun's private factory MCP tools
// (HOME-941): create_request and request_status; and, for the Discord
// bridge, the finished requests it answers (HOME-943). Only Substrate's egress
// gateway reaches it, with the calling session's actor JWT. The bridge says
// which Discord turn the session is answering, and only Joe's turns may file
// a request. Its database login may only insert a request and read status.
package main

import (
	"context"
	"database/sql"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	_ "github.com/lib/pq" // registers the postgres driver

	"github.com/jomcgi/homelab/projects/factory/broker/actorjwt"
	"github.com/jomcgi/homelab/projects/factory/discord/requests"
)

func main() {
	log := slog.New(slog.NewJSONHandler(os.Stderr, nil))
	if err := run(log); err != nil {
		log.Error("bosun-requests stopped", "err", err)
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

type errorString string

func (e errorString) Error() string { return string(e) }

func run(log *slog.Logger) error {
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, os.Interrupt)
	defer stop()

	dsn := os.Getenv("DATABASE_URL")
	bridge := os.Getenv("REQUESTS_BRIDGE_URL")
	if dsn == "" || bridge == "" {
		return errorString("DATABASE_URL and REQUESTS_BRIDGE_URL are required")
	}
	servers := map[string]bool{}
	for _, s := range list("REQUESTS_SERVERS") {
		servers[s] = true
	}
	atespaces := list("ACTOR_JWT_ATESPACES")
	if len(atespaces) == 0 {
		// An empty list would admit every atespace's actors.
		return errorString("ACTOR_JWT_ATESPACES is required")
	}
	// Joe's Discord user ID. Unreadable, every create_request is refused.
	operator := ""
	if b, err := os.ReadFile(os.Getenv("REQUESTS_OPERATOR_FILE")); err == nil {
		operator = strings.TrimSpace(string(b))
	} else {
		log.Error("operator user ID unreadable; create_request is refused", "err", err)
	}
	db, err := sql.Open("postgres", dsn)
	if err != nil {
		return err
	}
	defer db.Close()
	db.SetMaxOpenConns(2)
	db.SetMaxIdleConns(1)
	db.SetConnMaxIdleTime(5 * time.Minute)

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

	store := &requests.Store{DB: db}
	mux := http.NewServeMux()
	mux.Handle("/mcp", &requests.Handler{
		Store:    store,
		Turns:    &requests.BridgeTurns{URL: bridge},
		Verifier: verifier,
		Operator: operator,
		Servers:  servers,
		Log:      log,
	})
	// The bridge's side: finished requests to answer (HOME-943).
	(&requests.Replies{Store: store, Servers: list("REQUESTS_SERVERS"), Log: log}).Register(mux)
	// Ready once the database answers. It reads no rows.
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		pctx, cancel := context.WithTimeout(r.Context(), 3*time.Second)
		defer cancel()
		if err := db.PingContext(pctx); err != nil {
			w.WriteHeader(http.StatusServiceUnavailable)
			return
		}
		w.WriteHeader(http.StatusNoContent)
	})
	addr := os.Getenv("ADDR")
	if addr == "" {
		addr = ":8080"
	}
	srv := &http.Server{Addr: addr, Handler: mux, ReadHeaderTimeout: 10 * time.Second, ReadTimeout: time.Minute, WriteTimeout: time.Minute}
	go func() {
		<-ctx.Done()
		shutdown, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		_ = srv.Shutdown(shutdown)
	}()
	log.Info("listening", "addr", addr, "servers", len(servers), "atespaces", atespaces, "operator", operator != "")
	if err := srv.ListenAndServe(); err != http.ErrServerClosed {
		return err
	}
	return nil
}
