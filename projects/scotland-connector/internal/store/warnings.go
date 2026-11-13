package store

import (
	"context"
	"crypto/sha256"
	"fmt"
	"log/slog"
	"sync/atomic"
	"time"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/warnings"
)

// WarningsStore is independent of readiness and the existing stars/walk stores.
// Invalid replacements keep the last good body, never reset its source clocks.
type WarningsStore struct {
	bucket, key, tag string
	interval         time.Duration
	get              Getter
	log              *slog.Logger
	cur              atomic.Pointer[warnings.Snapshot]
	clocks           map[string]time.Time
}

func NewWarnings(bucket, key string, interval time.Duration, get Getter, log *slog.Logger) *WarningsStore {
	return &WarningsStore{bucket: bucket, key: key, interval: interval, get: get, log: log, clocks: map[string]time.Time{}}
}
func (s *WarningsStore) Current() *warnings.Snapshot { return s.cur.Load() }

func (s *WarningsStore) Refresh(ctx context.Context) error {
	o, err := s.get.Get(ctx, s.bucket, s.key, s.tag, warnings.MaxBytes)
	if err != nil {
		return err
	}
	if o.NotModified {
		if s.Current() == nil {
			return fmt.Errorf("warnings: 304 without snapshot")
		}
		return nil
	}
	d, err := warnings.Parse(o.Body)
	if err != nil {
		return err
	}
	if d.CreatedAt.After(time.Now()) {
		return fmt.Errorf("warnings: future snapshot")
	}
	if old := s.Current(); old != nil && (d.CreatedAt.Before(old.CreatedAt) || regressiveWarnings(old, d)) {
		return fmt.Errorf("warnings: regressive replacement")
	}
	for _, r := range d.Regions {
		if r.Complete && r.CheckedAt.Before(s.clocks[r.Code]) {
			return fmt.Errorf("warnings: regressive source clock")
		}
	}
	for _, r := range d.Regions {
		if r.Complete {
			s.clocks[r.Code] = r.CheckedAt
		}
	}
	s.cur.Store(d)
	s.tag = o.ETag
	s.log.Info("warnings snapshot loaded", "regions", len(d.Regions), "digest", fmt.Sprintf("%x", sha256.Sum256(o.Body)))
	return nil
}

func (s *WarningsStore) Run(ctx context.Context) {
	refresh := func() {
		c, cancel := context.WithTimeout(ctx, 30*time.Second)
		defer cancel()
		if err := s.Refresh(c); err != nil {
			s.log.Warn("warnings refresh failed; preserving source clocks", "err", err)
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

// A failed source is explicitly unusable. Successful source observations never
// move backwards, even when a publisher supplies a newer packaging timestamp.
func regressiveWarnings(old, next *warnings.Snapshot) bool {
	for _, n := range next.Regions {
		if !n.Complete {
			continue
		}
		for _, o := range old.Regions {
			if o.Code == n.Code && n.CheckedAt.Before(o.CheckedAt) {
				return true
			}
		}
	}
	return false
}
