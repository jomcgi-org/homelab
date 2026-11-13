package store

import (
	"context"
	"crypto/sha256"
	"fmt"
	"log/slog"
	"sync/atomic"
	"time"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/roadworks"
)

// RoadworksStore is independent of readiness and the existing stars/walk stores.
// Invalid replacements keep the last good body, never reset its source clocks.
type RoadworksStore struct {
	bucket, key, tag string
	interval         time.Duration
	get              Getter
	log              *slog.Logger
	cur              atomic.Pointer[roadworks.Snapshot]
}

func NewRoadworks(bucket, key string, interval time.Duration, get Getter, log *slog.Logger) *RoadworksStore {
	return &RoadworksStore{bucket: bucket, key: key, interval: interval, get: get, log: log}
}
func (s *RoadworksStore) Current() *roadworks.Snapshot { return s.cur.Load() }

func (s *RoadworksStore) Refresh(ctx context.Context) error {
	o, err := s.get.Get(ctx, s.bucket, s.key, s.tag, roadworks.MaxBytes)
	if err != nil {
		return err
	}
	if o.NotModified {
		if s.Current() == nil {
			return fmt.Errorf("roadworks: 304 without snapshot")
		}
		return nil
	}
	d, err := roadworks.Parse(o.Body)
	if err != nil {
		return err
	}
	if d.CollectedAt.After(time.Now()) || d.SourceModifiedAt.After(time.Now()) {
		return fmt.Errorf("roadworks: future source clocks")
	}
	if old := s.Current(); old != nil && (d.SourceModifiedAt.Before(old.SourceModifiedAt) || d.CollectedAt.Before(old.CollectedAt)) {
		return fmt.Errorf("roadworks: regressive replacement")
	}
	s.cur.Store(d)
	s.tag = o.ETag
	s.log.Info("roadworks snapshot loaded", "activities", len(d.Activities), "digest", fmt.Sprintf("%x", sha256.Sum256(o.Body)))
	return nil
}

func (s *RoadworksStore) Run(ctx context.Context) {
	refresh := func() {
		c, cancel := context.WithTimeout(ctx, 30*time.Second)
		defer cancel()
		if err := s.Refresh(c); err != nil {
			s.log.Warn("roadworks refresh failed; preserving source clocks", "err", err)
		}
	}
	refresh()
	t := time.NewTicker(s.interval)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			refresh()
		}
	}
}
