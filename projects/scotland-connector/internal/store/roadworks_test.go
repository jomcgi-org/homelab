package store

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/fixture"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/r2"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/roadworks"
)

type roadGetter struct {
	o   r2.Object
	err error
	tag string
}

func (g *roadGetter) Get(_ context.Context, _, _, tag string, limit int64) (r2.Object, error) {
	g.tag = tag
	if limit != roadworks.MaxBytes {
		return r2.Object{}, fmt.Errorf("unbounded read")
	}
	return g.o, g.err
}

func TestRoadworksPreservesClocksAcross304AndFailures(t *testing.T) {
	g := &roadGetter{o: r2.Object{Body: fixture.RoadworksJSON, ETag: "first"}}
	s := NewRoadworks("stars", "roadworks.json", time.Minute, g, slog.New(slog.NewTextHandler(io.Discard, nil)))
	ctx := context.Background()
	if e := s.Refresh(ctx); e != nil {
		t.Fatal(e)
	}
	old := s.Current()
	g.o = r2.Object{NotModified: true}
	if e := s.Refresh(ctx); e != nil || g.tag != "first" || s.Current() != old {
		t.Fatal(e)
	}
	for _, body := range [][]byte{[]byte(`{}`), []byte(`{"schema_version":2}`)} {
		g.o = r2.Object{Body: body, ETag: "bad"}
		if e := s.Refresh(ctx); e == nil || s.Current() != old {
			t.Fatal("invalid replacement")
		}
	}
	regressive := *old
	regressive.SourceModifiedAt = old.SourceModifiedAt.Add(-time.Hour)
	b, _ := json.Marshal(regressive)
	g.o = r2.Object{Body: b}
	if s.Refresh(ctx) == nil || s.Current() != old {
		t.Fatal("regression")
	}
	future := *old
	future.SourceModifiedAt = time.Now().Add(time.Hour)
	future.CollectedAt = future.SourceModifiedAt
	b, _ = json.Marshal(future)
	g.o = r2.Object{Body: b}
	if s.Refresh(ctx) == nil || s.Current() != old {
		t.Fatal("future snapshot replaced last good")
	}
	g.err = fmt.Errorf("unavailable")
	if s.Refresh(ctx) == nil || s.Current() != old {
		t.Fatal("failed poll")
	}
	g.err = nil
	empty := *old
	empty.Activities = []roadworks.Activity{}
	b, _ = json.Marshal(empty)
	g.o = r2.Object{Body: b, ETag: "empty"}
	if e := s.Refresh(ctx); e != nil || len(s.Current().Activities) != 0 || s.Current().SourceModifiedAt != old.SourceModifiedAt {
		t.Fatal(e)
	}
}
