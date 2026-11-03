package store

import (
	"context"
	"fmt"
	"log/slog"
	"sync/atomic"
	"time"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/summits"
)

// SummitStore is optional and independent of readiness. The enable check also
// guards direct Refresh calls. Failed replacements retain their original clocks.
type SummitStore struct {
	enabled          bool
	bucket, key, tag string
	interval         time.Duration
	get              Getter
	log              *slog.Logger
	cur              atomic.Pointer[summits.Snapshot]
}

func NewSummits(enabled bool, bucket, key string, interval time.Duration, get Getter, log *slog.Logger) *SummitStore {
	return &SummitStore{enabled: enabled, bucket: bucket, key: key, interval: interval, get: get, log: log}
}

func (s *SummitStore) Current() *summits.Snapshot {
	if !s.enabled {
		return nil
	}
	return s.cur.Load()
}

func (s *SummitStore) Refresh(ctx context.Context) error {
	if !s.enabled {
		return nil
	}
	o, err := s.get.Get(ctx, s.bucket, s.key, s.tag, summits.MaxBytes)
	if err != nil {
		return err
	}
	if o.NotModified {
		if s.Current() == nil {
			return fmt.Errorf("summits: 304 without snapshot")
		}
		return nil
	}
	d, err := summits.Parse(o.Body)
	if err != nil {
		return err
	}
	if old := s.Current(); old != nil && (d.RunAt.Before(old.RunAt) || d.GeneratedAt.Before(old.GeneratedAt)) {
		return fmt.Errorf("summits: regressive replacement")
	}
	s.cur.Store(d)
	s.tag = o.ETag
	return nil
}

func (s *SummitStore) Run(ctx context.Context) {
	if !s.enabled {
		return
	}
	refresh := func() {
		c, cancel := context.WithTimeout(ctx, 30*time.Second)
		defer cancel()
		if err := s.Refresh(c); err != nil {
			s.log.Warn("UKV refresh failed; preserving source clocks", "err", err)
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
