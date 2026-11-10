package store

import (
	"context"
	"log/slog"
	"sync/atomic"
	"time"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/walks"
)

// HillStore is optional and never participates in readiness. Nil until a
// fully validated snapshot loads; errors and 304s preserve the working copy.
type HillStore struct {
	bucket, key, tag string
	interval         time.Duration
	get              Getter
	log              *slog.Logger
	cur              atomic.Pointer[walks.HillDataset]
}

func NewHills(bucket, key string, interval time.Duration, get Getter, log *slog.Logger) *HillStore {
	return &HillStore{bucket: bucket, key: key, interval: interval, get: get, log: log}
}
func (s *HillStore) Current() *walks.HillDataset { return s.cur.Load() }
func (s *HillStore) Refresh(ctx context.Context) error {
	obj, err := s.get.Get(ctx, s.bucket, s.key, s.tag, maxSnapshotBytes)
	if err != nil {
		return err
	}
	if obj.NotModified && s.Current() != nil {
		return nil
	}
	ds, err := walks.ParseHills(obj.Body, obj.ETag)
	if err != nil {
		return err
	}
	s.tag = obj.ETag
	s.cur.Store(ds)
	return nil
}

func (s *HillStore) Run(ctx context.Context) {
	refresh := func() {
		rctx, cancel := context.WithTimeout(ctx, s.interval)
		defer cancel()
		if err := s.Refresh(rctx); err != nil {
			s.log.Warn("hill snapshot unavailable; keeping previous", "err", err)
		}
	}
	refresh()
	ticker := time.NewTicker(s.interval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			refresh()
		}
	}
}
