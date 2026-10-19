package store

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"testing"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/r2"
)

type fakeR2 struct {
	objects map[string]r2.Object
	calls   map[string]int
	fail    error
}

func (f *fakeR2) Get(_ context.Context, _, key, ifNoneMatch string, _ int64) (r2.Object, error) {
	f.calls[key]++
	if f.fail != nil {
		return r2.Object{}, f.fail
	}
	o := f.objects[key]
	if ifNoneMatch != "" && ifNoneMatch == o.ETag {
		return r2.Object{ETag: o.ETag, NotModified: true}, nil
	}
	return o, nil
}

const history = `{"sites":[]}`

func TestRefreshSwapsOnlyOnGoodChange(t *testing.T) {
	f := &fakeR2{calls: map[string]int{}, objects: map[string]r2.Object{
		"sites.json":       {Body: []byte(`{"darkness":"astronomical","sites":[{"id":"a","lat":57,"lon":-4}]}`), ETag: `"s1"`},
		"history-map.json": {Body: []byte(history), ETag: `"h1"`},
	}}
	s := New(Config{Bucket: "stars", SitesKey: "sites.json", HistoryKey: "history-map.json"}, f, slog.New(slog.NewTextHandler(io.Discard, nil)))
	ctx := context.Background()

	if s.Current() != nil {
		t.Fatal("dataset before first load")
	}
	if err := s.Refresh(ctx); err != nil {
		t.Fatal(err)
	}
	first := s.Current()
	if first == nil || first.Version != `"s1"|"h1"` || len(first.Sites) != 1 {
		t.Fatalf("first load = %+v", first)
	}

	// Unchanged: the same pointer stays live.
	if err := s.Refresh(ctx); err != nil || s.Current() != first {
		t.Fatalf("unchanged refresh swapped dataset (err %v)", err)
	}

	// A broken upload keeps the previous dataset.
	f.objects["sites.json"] = r2.Object{Body: []byte(`{"sites":[`), ETag: `"s2"`}
	if err := s.Refresh(ctx); err == nil || s.Current() != first {
		t.Fatal("broken snapshot replaced the good one")
	}

	// A good change to one object re-parses against the cached other.
	f.objects["sites.json"] = r2.Object{Body: []byte(`{"sites":[{"id":"a","lat":57,"lon":-4},{"id":"b","lat":58,"lon":-4}]}`), ETag: `"s3"`}
	if err := s.Refresh(ctx); err != nil {
		t.Fatal(err)
	}
	if got := s.Current(); got == first || len(got.Sites) != 2 || got.Version != `"s3"|"h1"` {
		t.Fatalf("changed load = %+v", got)
	}

	// An R2 outage keeps serving.
	f.fail = errors.New("r2 down")
	if err := s.Refresh(ctx); err == nil || s.Current() == nil {
		t.Fatal("outage dropped the dataset")
	}
}
