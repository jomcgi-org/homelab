package main

import (
	"context"
	"database/sql"
	"log/slog"
	"os"
	"strings"
	"time"

	_ "github.com/lib/pq" // registers the postgres driver

	"github.com/prometheus/client_golang/prometheus"

	"github.com/jomcgi/homelab/projects/factory/discord"
	"github.com/jomcgi/homelab/projects/factory/discord/archive"
)

// newArchive builds the verbatim message archive (HOME-909) from the
// environment, or returns nil when ARCHIVE_DATABASE_URL is not set.
// ARCHIVE_READER_ROLE is the login bosun-history reads with; the schema
// grants it SELECT. ARCHIVE_PURGE lists servers whose rows are deleted at
// start: a server removed from the allowlist.
func newArchive(log *slog.Logger, policy discord.Config, places *discord.Places, reg prometheus.Registerer) (*discord.Archiver, *sql.DB, error) {
	dsn := os.Getenv("ARCHIVE_DATABASE_URL")
	if dsn == "" {
		log.Info("archive off: ARCHIVE_DATABASE_URL is not set")
		return nil, nil, nil
	}
	db, err := sql.Open("postgres", dsn)
	if err != nil {
		return nil, nil, err
	}
	// One writer for live events and one for the backfill.
	db.SetMaxOpenConns(2)
	db.SetMaxIdleConns(2)
	db.SetConnMaxIdleTime(5 * time.Minute)
	return &discord.Archiver{
		Store:       &archive.Store{DB: db},
		Places:      places,
		Config:      policy,
		IncludeBots: os.Getenv("BACKFILL_INCLUDE_BOTS") == "true",
		Log:         log,
		Metrics:     discord.NewArchiveMetrics(reg),
	}, db, nil
}

// runArchive applies the schema, trying again each minute until the
// database answers, purges the listed servers and then writes live events
// until ctx ends. Events wait in the archiver's bounded queue meanwhile.
func runArchive(ctx context.Context, log *slog.Logger, a *discord.Archiver, db *sql.DB, policy discord.Config) {
	for {
		mctx, cancel := context.WithTimeout(ctx, time.Minute)
		err := archive.Migrate(mctx, db, os.Getenv("ARCHIVE_READER_ROLE"))
		cancel()
		if err == nil {
			break
		}
		log.Warn("archive schema not applied; trying again in a minute", "error", err)
		select {
		case <-ctx.Done():
			return
		case <-time.After(time.Minute):
		}
	}
	store := &archive.Store{DB: db}
	for _, g := range strings.Split(os.Getenv("ARCHIVE_PURGE"), ",") {
		if g = strings.TrimSpace(g); g == "" {
			continue
		}
		if _, listed := policy.Guilds[g]; listed {
			log.Error("archive purge skipped: the server is still allowlisted", "guild", g)
			continue
		}
		n, err := store.Purge(ctx, g)
		if err != nil {
			log.Error("archive purge failed", "guild", g, "error", err)
			continue
		}
		log.Info("archive purged a server", "guild", g, "rows", n)
	}
	log.Info("archive on")
	a.Run(ctx)
}
