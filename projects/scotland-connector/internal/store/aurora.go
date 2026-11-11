package store

import (
	"context"
	"crypto/sha256"
	"fmt"
	"log/slog"
	"sync/atomic"
	"time"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/aurora"
)

// AuroraStore is independent of readiness and the existing stars/walk stores.
// Invalid replacements keep the last good body, never reset its source clocks.
type AuroraStore struct {
	bucket, key, tag string
	interval         time.Duration
	get              Getter
	log              *slog.Logger
	cur              atomic.Pointer[aurora.Snapshot]
}

func NewAurora(bucket, key string, interval time.Duration, get Getter, log *slog.Logger) *AuroraStore {
	return &AuroraStore{bucket: bucket, key: key, interval: interval, get: get, log: log}
}
func (s *AuroraStore) Current() *aurora.Snapshot { return s.cur.Load() }

func (s *AuroraStore) Refresh(ctx context.Context) error {
	o, err := s.get.Get(ctx, s.bucket, s.key, s.tag, aurora.MaxBytes)
	if err != nil {
		return err
	}
	if o.NotModified {
		if s.Current() == nil {
			return fmt.Errorf("aurora: 304 without snapshot")
		}
		return nil
	}
	d, err := aurora.Parse(o.Body)
	if err != nil {
		return err
	}
	if old := s.Current(); old != nil && (d.CreatedAt.Before(old.CreatedAt) || d.NOAA.ObservedAt.Before(old.NOAA.ObservedAt) || d.NOAA.CollectedAt.Before(old.NOAA.CollectedAt)) {
		return fmt.Errorf("aurora: regressive replacement")
	}
	s.cur.Store(d)
	s.tag = o.ETag
	s.log.Info("aurora snapshot loaded", "sites", len(d.Sites), "digest", fmt.Sprintf("%x", sha256.Sum256(o.Body)))
	return nil
}

func (s *AuroraStore) Run(ctx context.Context) {
	refresh := func() {
		c, cancel := context.WithTimeout(ctx, 30*time.Second)
		defer cancel()
		if err := s.Refresh(c); err != nil {
			s.log.Warn("aurora refresh failed; preserving source clocks", "err", err)
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
