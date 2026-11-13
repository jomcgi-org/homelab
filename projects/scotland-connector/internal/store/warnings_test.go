package store

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/r2"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/verdict"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/warnings"
)

type warningGetter struct {
	object r2.Object
	tag    string
	max    int64
}

func (g *warningGetter) Get(_ context.Context, _, _, tag string, max int64) (r2.Object, error) {
	g.tag = tag
	g.max = max
	return g.object, nil
}

func TestWarningsStoreRevalidationAndRestart(t *testing.T) {
	now := time.Now().UTC().Add(-time.Second)
	snap := &warnings.Snapshot{Version: 1, CreatedAt: now}
	for _, code := range warnings.Regions {
		snap.Regions = append(snap.Regions, warnings.Region{Code: code, Complete: true, CheckedAt: now, Items: []warnings.Item{}})
	}
	body := func() []byte {
		b, e := json.Marshal(snap)
		if e != nil {
			t.Fatal(e)
		}
		return b
	}
	get := &warningGetter{object: r2.Object{Body: body(), ETag: "first"}}
	newStore := func() *WarningsStore {
		return NewWarnings("stars", "warnings.json", 60*time.Second, get, slog.New(slog.NewTextHandler(io.Discard, nil)))
	}
	s := newStore()
	ctx := context.Background()
	if e := s.Refresh(ctx); e != nil {
		t.Fatal(e)
	}
	original := s.Current()
	if get.max != warnings.MaxBytes {
		t.Fatal(get.max)
	}
	get.object = r2.Object{NotModified: true}
	if e := s.Refresh(ctx); e != nil || s.Current() != original || get.tag != "first" {
		t.Fatal(e)
	}
	v, _ := warnings.Apply(s.Current(), now.Add(warnings.MaxAge), now, now.Add(time.Hour), verdict.Go, "")
	if v != verdict.Unknown {
		t.Fatal("304 rejuvenated source", v)
	}
	get.object = r2.Object{Body: []byte("{"), ETag: "bad"}
	if e := s.Refresh(ctx); e == nil || s.Current() != original {
		t.Fatal("malformed replacement", e)
	}
	// New object timestamp cannot change source age, including a process restart.
	snap.CreatedAt = now.Add(time.Millisecond)
	get.object = r2.Object{Body: body(), ETag: "new-package"}
	restarted := newStore()
	if e := restarted.Refresh(ctx); e != nil {
		t.Fatal(e)
	}
	v, _ = warnings.Apply(restarted.Current(), now.Add(warnings.MaxAge), now, now.Add(time.Hour), verdict.Go, "")
	if v != verdict.Unknown {
		t.Fatal("restart/package rejuvenated source")
	}
	snap.Regions[0].CheckedAt = now.Add(-time.Second)
	get.object = r2.Object{Body: body(), ETag: "regressive"}
	if e := s.Refresh(ctx); e == nil || s.Current() != original {
		t.Fatal("regressive accepted", e)
	}
	snap.Regions[0].CheckedAt = time.Time{}
	snap.Regions[0].Complete = false
	snap.Regions[0].Items = nil
	get.object = r2.Object{Body: body(), ETag: "failed"}
	if e := s.Refresh(ctx); e != nil {
		t.Fatal(e)
	}
	snap.Regions[0].Complete = true
	snap.Regions[0].Items = []warnings.Item{}
	snap.Regions[0].CheckedAt = now.Add(-time.Second)
	get.object = r2.Object{Body: body(), ETag: "regressive-after-failure"}
	if e := s.Refresh(ctx); e == nil {
		t.Fatal("failure erased high-water source clock")
	}
	snap.CreatedAt = now.Add(time.Hour)
	get.object = r2.Object{Body: body(), ETag: "future"}
	if e := s.Refresh(ctx); e == nil {
		t.Fatal("future accepted")
	}
}
