package store

import (
	"context"
	_ "embed"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"strings"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/r2"
)

const walksV1 = `{"schema_version":1,"fetched_at":"2026-10-20T12:20:00+00:00","walks":[{"id":"a","name":"A","url":"https://www.walkhighlands.co.uk/a.shtml","lat":57,"lon":-4,"distance_km":5,"ascent_m":200,"duration_h":2,"hours":[]}]}`

// Real build_payload bytes, checked against the producer by Python CI.
//
//go:embed testdata/camping-walks.json
var campingWalksSnapshot []byte

func TestCampingWalkProducerBytesThroughStore(t *testing.T) {
	f := &fakeR2{calls: map[string]int{}, objects: map[string]r2.Object{
		"walks.json": {Body: campingWalksSnapshot, ETag: `"camping"`},
	}}
	s := NewWalks("stars", "walks.json", time.Minute, f, slog.New(slog.NewTextHandler(io.Discard, nil)))
	if err := s.Refresh(context.Background()); err != nil {
		t.Fatal(err)
	}
	ds := s.Current()
	shore, park, outside := ds.ByID("shore"), ds.ByID("park"), ds.ByID("outside")
	if shore == nil || park == nil || outside == nil {
		t.Fatal("missing walks")
	}
	if !shore.Camping.Known() || len(shore.Camping.Zones) != 1 || shore.Camping.Zones[0] != "East Loch Lomond" {
		t.Fatal(shore.Camping)
	}
	if !park.Camping.Known() || len(park.Camping.Zones) != 0 || len(park.Camping.Parks) != 1 || park.Camping.Parks[0] != "Cairngorms" {
		t.Fatal(park.Camping)
	}
	if !outside.Camping.Known() || len(outside.Camping.Zones) != 0 || len(outside.Camping.Parks) != 0 {
		t.Fatal(outside.Camping)
	}
}

func TestWalkStoreKeepsLastGoodSnapshot(t *testing.T) {
	f := &fakeR2{calls: map[string]int{}, objects: map[string]r2.Object{
		"walks.json": {Body: []byte(walksV1), ETag: `"w1"`},
	}}
	s := NewWalks("stars", "walks.json", time.Minute, f, slog.New(slog.NewTextHandler(io.Discard, nil)))
	ctx := context.Background()
	if s.Current() != nil {
		t.Fatal("loaded before refresh")
	}
	if err := s.Refresh(ctx); err != nil {
		t.Fatal(err)
	}
	first := s.Current()
	if first == nil || len(first.Walks) != 1 || first.Version != `"w1"` {
		t.Fatalf("first = %+v", first)
	}
	// Unchanged: a 304 keeps the same dataset.
	if err := s.Refresh(ctx); err != nil || s.Current() != first {
		t.Fatalf("304 refresh err=%v swapped=%v", err, s.Current() != first)
	}
	// A broken upload is refused and the previous copy keeps serving.
	f.objects["walks.json"] = r2.Object{Body: []byte(`{"schema_version":1,"walks":[]}`), ETag: `"w2"`}
	if err := s.Refresh(ctx); err == nil || s.Current() != first {
		t.Fatalf("bad upload err=%v swapped=%v", err, s.Current() != first)
	}
	// An R2 outage keeps it too.
	f.objects["walks.json"] = r2.Object{Body: []byte(strings.Replace(walksV1, `"id":"a"`, `"id":"a","forecast_fetched_at":"invalid"`, 1)), ETag: `"w3"`}
	if err := s.Refresh(ctx); err == nil || s.Current() != first {
		t.Fatalf("bad entity timestamp err=%v swapped=%v", err, s.Current() != first)
	}
	f.fail = fmt.Errorf("%w: stars/walks.json", r2.ErrNotFound)
	if err := s.Refresh(ctx); !errors.Is(err, r2.ErrNotFound) || s.Current() != first {
		t.Fatalf("outage err=%v", err)
	}
}
