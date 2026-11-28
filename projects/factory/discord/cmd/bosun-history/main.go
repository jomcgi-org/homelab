// Command bosun-history serves Bosun's history tools over MCP: search and
// read the verbatim archive of the allowlisted Discord servers
// (projects/factory/discord/archive). It reads with a login that may only
// SELECT the archive table, and runs history_sql with one login per server
// that row security limits to that server's rows.
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
	"github.com/jomcgi/homelab/projects/factory/discord/archive"
)

func main() {
	log := slog.New(slog.NewJSONHandler(os.Stderr, nil))
	if err := run(log); err != nil {
		log.Error("bosun-history stopped", "err", err)
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
	if dsn == "" {
		return errorString("DATABASE_URL is required")
	}
	servers := map[string]bool{}
	for _, s := range list("HISTORY_SERVERS") {
		servers[s] = true
	}
	atespaces := list("ACTOR_JWT_ATESPACES")
	if len(atespaces) == 0 {
		// An empty list would admit every atespace's actors.
		return errorString("ACTOR_JWT_ATESPACES is required")
	}
	db, err := sql.Open("postgres", dsn)
	if err != nil {
		return err
	}
	defer db.Close()
	db.SetMaxOpenConns(4)
	db.SetMaxIdleConns(2)
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

	// Each server's history_sql login (HISTORY_SQL_URL_<guild>). A server
	// without one has no history_sql.
	sqlDB := map[string]*sql.DB{}
	for s := range servers {
		url := os.Getenv("HISTORY_SQL_URL_" + s)
		if url == "" {
			continue
		}
		sdb, err := sql.Open("postgres", url)
		if err != nil {
			return err
		}
		defer sdb.Close()
		sdb.SetMaxOpenConns(2)
		sdb.SetMaxIdleConns(1)
		sdb.SetConnMaxIdleTime(5 * time.Minute)
		sqlDB[s] = sdb
	}

	mux := http.NewServeMux()
	mux.Handle("/mcp", &archive.Handler{
		Reader:   &archive.Reader{DB: db, SQLDB: sqlDB, Servers: servers},
		Verifier: verifier,
		Log:      log,
	})
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
	log.Info("listening", "addr", addr, "servers", len(servers), "sql_servers", len(sqlDB), "atespaces", atespaces)
	if err := srv.ListenAndServe(); err != http.ErrServerClosed {
		return err
	}
	return nil
}
