package main

import (
	"context"
	"database/sql"
	"errors"
	"log/slog"
	"os"
	"strings"
	"sync/atomic"
	"time"

	_ "github.com/lib/pq" // registers the postgres driver

	"github.com/prometheus/client_golang/prometheus"

	"github.com/jomcgi/homelab/projects/factory/discord"
	"github.com/jomcgi/homelab/projects/factory/discord/archive"
)

// errArchiveNotReady is a write before the database is connected and its
// schema applied. The live queue and the next backfill pass hold the work.
var errArchiveNotReady = errors.New("archive: database not ready")

// lazyStore is the archive store once runArchive connects it.
type lazyStore struct{ s atomic.Pointer[archive.Store] }

func (l *lazyStore) store() (*archive.Store, error) {
	if s := l.s.Load(); s != nil {
		return s, nil
	}
	return nil, errArchiveNotReady
}

func (l *lazyStore) Upsert(ctx context.Context, rows []archive.Row) error {
	s, err := l.store()
	if err != nil {
		return err
	}
	return s.Upsert(ctx, rows)
}

func (l *lazyStore) Edit(ctx context.Context, e archive.Edit) error {
	s, err := l.store()
	if err != nil {
		return err
	}
	return s.Edit(ctx, e)
}

func (l *lazyStore) Delete(ctx context.Context, guildID, channelID string, ids []string) error {
	s, err := l.store()
	if err != nil {
		return err
	}
	return s.Delete(ctx, guildID, channelID, ids)
}

func (l *lazyStore) Reconcile(ctx context.Context, guildID, channelID, oldest, newest string, present []string) (int64, error) {
	s, err := l.store()
	if err != nil {
		return 0, err
	}
	return s.Reconcile(ctx, guildID, channelID, oldest, newest, present)
}

// newArchive builds the verbatim message archive (HOME-909), or returns nil
// when ARCHIVE_DATABASE_URL_FILE is not set. The file is a mounted Secret
// key that factory-db-setup fills; runArchive waits for it.
// ARCHIVE_READER_ROLE is the login bosun-history reads with; the schema
// grants it SELECT. ARCHIVE_SQL_ROLE is the group of the per-server
// history_sql logins; row security shows each its own server. ARCHIVE_PURGE lists servers whose rows are deleted once
// connected: a server removed from the allowlist.
func newArchive(log *slog.Logger, policy discord.Config, places *discord.Places, reg prometheus.Registerer) (*discord.Archiver, *lazyStore) {
	if os.Getenv("ARCHIVE_DATABASE_URL_FILE") == "" {
		log.Info("archive off: ARCHIVE_DATABASE_URL_FILE is not set")
		return nil, nil
	}
	store := &lazyStore{}
	return &discord.Archiver{
		Store:       store,
		Places:      places,
		Config:      policy,
		IncludeBots: os.Getenv("BACKFILL_INCLUDE_BOTS") == "true",
		Log:         log,
		Metrics:     discord.NewArchiveMetrics(reg),
	}, store
}

// connectArchive reads the URL file, opens the database and applies the
// schema.
func connectArchive(ctx context.Context) (*sql.DB, error) {
	b, err := os.ReadFile(os.Getenv("ARCHIVE_DATABASE_URL_FILE"))
	if err != nil {
		return nil, err
	}
	dsn := strings.TrimSpace(string(b))
	if dsn == "" {
		return nil, errors.New("the database URL file is empty")
	}
	db, err := sql.Open("postgres", dsn)
	if err != nil {
		return nil, err
	}
	// One writer for live events and one for the backfill.
	db.SetMaxOpenConns(2)
	db.SetMaxIdleConns(2)
	db.SetConnMaxIdleTime(5 * time.Minute)
	mctx, cancel := context.WithTimeout(ctx, time.Minute)
	defer cancel()
	if err := archive.Migrate(mctx, db, os.Getenv("ARCHIVE_READER_ROLE"), os.Getenv("ARCHIVE_SQL_ROLE")); err != nil {
		db.Close()
		return nil, err
	}
	return db, nil
}

// runArchive connects, trying again each minute until the URL file is
// filled and the database answers, purges the listed servers, and then
// writes live events until ctx ends. Events wait in the archiver's bounded
// queue meanwhile; backfill pages before then fail and the next pass
// repeats them.
func runArchive(ctx context.Context, log *slog.Logger, a *discord.Archiver, lazy *lazyStore, policy discord.Config) {
	var db *sql.DB
	for {
		var err error
		if db, err = connectArchive(ctx); err == nil {
			break
		}
		log.Warn("archive not connected; trying again in a minute", "error", err)
		select {
		case <-ctx.Done():
			return
		case <-time.After(time.Minute):
		}
	}
	defer db.Close()
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
	lazy.s.Store(store)
	log.Info("archive on")
	a.Run(ctx)
}
