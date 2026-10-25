// Package store keeps the current stars dataset in memory and refreshes it
// from R2. Readers call Current, which is one atomic load; the poller swaps in
// a new dataset only after both snapshots parse, so a bad or half-written
// upload never replaces a good one.
package store

import (
	"context"
	"fmt"
	"log/slog"
	"sync/atomic"
	"time"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/r2"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/stars"
)

// maxSnapshotBytes bounds each object read. An uncapped full-grid snapshot can
// approach this limit even when valid. Verify size and refresh-memory headroom
// before selecting it; an oversized object leaves the last-good dataset in use.
const maxSnapshotBytes = 64 << 20

// Getter is the subset of the R2 client the store uses.
type Getter interface {
	Get(ctx context.Context, bucket, key, ifNoneMatch string, maxBytes int64) (r2.Object, error)
}

// Config names the snapshots to read.
type Config struct {
	Bucket     string
	SitesKey   string
	HistoryKey string
	Interval   time.Duration
}

// Store holds the live dataset.
type Store struct {
	cfg    Config
	get    Getter
	log    *slog.Logger
	cur    atomic.Pointer[stars.Dataset]
	loaded atomic.Int64 // unix nanos of the last successful check
	// Raw bodies and ETags of the last good pair, so a change to one object
	// re-parses against the other without refetching it.
	sitesBody, historyBody []byte
	sitesTag, historyTag   string
}

// New builds an empty store; Refresh or Run fills it.
func New(cfg Config, get Getter, log *slog.Logger) *Store {
	return &Store{cfg: cfg, get: get, log: log}
}

// Current returns the live dataset, or nil before the first load.
func (s *Store) Current() *stars.Dataset { return s.cur.Load() }

// LastChecked is when R2 last answered for both objects.
func (s *Store) LastChecked() time.Time {
	n := s.loaded.Load()
	if n == 0 {
		return time.Time{}
	}
	return time.Unix(0, n)
}

// Refresh does one conditional fetch of both objects and swaps in a new
// dataset if either changed. Not safe for concurrent use; Run serializes it.
func (s *Store) Refresh(ctx context.Context) error {
	sites, err := s.get.Get(ctx, s.cfg.Bucket, s.cfg.SitesKey, s.sitesTag, maxSnapshotBytes)
	if err != nil {
		return err
	}
	history, err := s.get.Get(ctx, s.cfg.Bucket, s.cfg.HistoryKey, s.historyTag, maxSnapshotBytes)
	if err != nil {
		return err
	}
	if sites.NotModified && history.NotModified && s.cur.Load() != nil {
		s.loaded.Store(time.Now().UnixNano())
		return nil
	}
	sitesBody, historyBody := s.sitesBody, s.historyBody
	if !sites.NotModified {
		sitesBody = sites.Body
	}
	if !history.NotModified {
		historyBody = history.Body
	}
	if sitesBody == nil || historyBody == nil {
		// A 304 for an object this process never held cannot happen with the
		// empty If-None-Match the first load sends; guard anyway.
		return fmt.Errorf("store: missing snapshot body after conditional fetch")
	}
	ds, err := stars.Parse(sitesBody, historyBody, sites.ETag+"|"+history.ETag)
	if err != nil {
		return err
	}
	s.sitesBody, s.historyBody = sitesBody, historyBody
	s.sitesTag, s.historyTag = sites.ETag, history.ETag
	s.cur.Store(ds)
	s.loaded.Store(time.Now().UnixNano())
	s.log.Info("stars snapshot loaded", "sites", len(ds.Sites), "version", ds.Version, "fetched_at", ds.FetchedAt)
	return nil
}

// Run refreshes every Interval until ctx ends. Failures keep the previous
// dataset and are logged; the readiness probe reports staleness.
func (s *Store) Run(ctx context.Context) {
	t := time.NewTicker(s.cfg.Interval)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			rctx, cancel := context.WithTimeout(ctx, s.cfg.Interval)
			if err := s.Refresh(rctx); err != nil {
				s.log.Warn("stars snapshot refresh failed; serving previous", "err", err)
			}
			cancel()
		}
	}
}
