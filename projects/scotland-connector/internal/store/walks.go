package store

import (
	"context"
	"errors"
	"log/slog"
	"sync/atomic"
	"time"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/r2"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/walks"
)

// WalkStore keeps the walk snapshot in memory. It is optional: readiness
// depends only on the stars Store, so a missing or broken walks.json takes
// out walk_windows alone, and the last good copy keeps serving.
type WalkStore struct {
	bucket, key string
	interval    time.Duration
	get         Getter
	log         *slog.Logger
	cur         atomic.Pointer[walks.Dataset]
	tag         string
}

// NewWalks builds an empty walk store.
func NewWalks(bucket, key string, interval time.Duration, get Getter, log *slog.Logger) *WalkStore {
	return &WalkStore{bucket: bucket, key: key, interval: interval, get: get, log: log}
}

// Current returns the live walk dataset, or nil before the first load.
func (s *WalkStore) Current() *walks.Dataset { return s.cur.Load() }

// Refresh does one conditional fetch and swaps in a new dataset if it parses.
func (s *WalkStore) Refresh(ctx context.Context) error {
	obj, err := s.get.Get(ctx, s.bucket, s.key, s.tag, maxSnapshotBytes)
	if err != nil {
		return err
	}
	if obj.NotModified && s.cur.Load() != nil {
		return nil
	}
	ds, err := walks.Parse(obj.Body, obj.ETag)
	if err != nil {
		return err
	}
	s.tag = obj.ETag
	s.cur.Store(ds)
	s.log.Info("walk snapshot loaded", "walks", len(ds.Walks), "version", ds.Version, "fetched_at", ds.FetchedAt)
	return nil
}

// Run loads once, then refreshes every interval until ctx ends.
func (s *WalkStore) Run(ctx context.Context) {
	s.refreshLogged(ctx)
	t := time.NewTicker(s.interval)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			s.refreshLogged(ctx)
		}
	}
}

func (s *WalkStore) refreshLogged(ctx context.Context) {
	rctx, cancel := context.WithTimeout(ctx, s.interval)
	defer cancel()
	err := s.Refresh(rctx)
	switch {
	case err == nil:
	case errors.Is(err, r2.ErrNotFound) && s.cur.Load() == nil:
		// Expected until the monolith's hikes job first writes walks.json.
		s.log.Info("walk snapshot not published yet", "key", s.key)
	default:
		s.log.Warn("walk snapshot refresh failed; serving previous", "err", err)
	}
}
