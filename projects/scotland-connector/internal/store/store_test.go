package store

import (
	"context"
	_ "embed"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/connector"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/r2"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/verdict"
)

type fakeR2 struct {
	objects map[string]r2.Object
	calls   map[string]int
	fail    error
	tags    map[string]string
}

func (f *fakeR2) Get(_ context.Context, _, key, ifNoneMatch string, limit int64) (r2.Object, error) {
	f.calls[key]++
	if limit != 64<<20 {
		return r2.Object{}, fmt.Errorf("unexpected read limit %d", limit)
	}
	if f.tags != nil {
		f.tags[key] = ifNoneMatch
	}
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

// Regenerated and byte-checked by stars/router_test.py using the real producer.
//
//go:embed testdata/connector-sites.json
var producerSnapshot []byte

func snapshotStore(body []byte) (*Store, *fakeR2) {
	f := &fakeR2{calls: map[string]int{}, tags: map[string]string{}, objects: map[string]r2.Object{
		"connector-sites.json": {Body: body, ETag: `"s1"`},
		"history-map.json":     {Body: []byte(history), ETag: `"h1"`},
	}}
	return New(Config{Bucket: "stars", SitesKey: "connector-sites.json", HistoryKey: "history-map.json"}, f, slog.New(slog.NewTextHandler(io.Discard, nil))), f
}

func TestProducerBytesThroughStoreAndSevenDayPlanCheck(t *testing.T) {
	s, f := snapshotStore(producerSnapshot)
	if err := s.Refresh(context.Background()); err != nil {
		t.Fatal(err)
	}
	if f.calls["sites.json"] != 0 || f.calls["connector-sites.json"] != 1 {
		t.Fatal(f.calls)
	}
	ds := s.Current()
	if len(ds.Sites) != 4 || len(ds.Sites[0].Hours) <= 25 || ds.Sites[0].Truncated() {
		t.Fatalf("uncapped site: %+v", ds.Sites)
	}
	lat, lon := 57.3, -3.7
	from, to := "2026-10-19", "2026-10-25"
	now := time.Date(2026, 10, 19, 10, 0, 0, 0, time.UTC)
	svc := &connector.Service{Data: s, Now: func() time.Time { return now }}
	env, err := svc.Plan(connector.PlanParams{Lat: &lat, Lon: &lon, From: &from, To: &to, Activities: []string{"stargaze"}})
	if err != nil {
		t.Fatal(err)
	}
	days := env.Data.(connector.PlanResult).Days
	if len(days) != 7 {
		t.Fatal(days)
	}
	// Night two lies beyond the old 25-entry cap. Both dark and twilight
	// counts survive parsing, while coverage is still only the first 48h.
	if len(days[1].Stargazing) == 0 || days[1].Stargazing[0].ClearDark != 14 {
		t.Fatalf("later night: %+v", days[1])
	}
	for i, day := range days {
		date := day.Date
		checked, err := svc.Check(connector.CheckParams{Items: []connector.CheckItem{{Activity: "stargaze", Date: date, Lat: &lat, Lon: &lon}}})
		if err != nil {
			t.Fatal(err)
		}
		item := checked.Data.(connector.CheckResult).Items[0]
		if i == 1 && (item.Night == nil || item.Night.ClearDark != 14) {
			t.Fatalf("later check: %+v", item)
		}
		if i >= 2 {
			if item.Verdict != verdict.Unknown || len(day.Stargazing) != 0 || day.StarNote == "" {
				t.Fatalf("beyond entity horizon: %+v / %+v", item, day)
			}
		}
	}
	// The same producer shape also contains successful-empty, stale and
	// missing entity metadata. Exercise each in isolation, through the store.
	var doc map[string]json.RawMessage
	if err := json.Unmarshal(producerSnapshot, &doc); err != nil {
		t.Fatal(err)
	}
	var sites []json.RawMessage
	if err := json.Unmarshal(doc["sites"], &sites); err != nil {
		t.Fatal(err)
	}
	for i := 1; i < len(sites); i++ {
		doc["sites"], _ = json.Marshal([]json.RawMessage{sites[i]})
		body, _ := json.Marshal(doc)
		single, _ := snapshotStore(body)
		if err := single.Refresh(context.Background()); err != nil {
			t.Fatal(err)
		}
		svc.Data = single
		for offset := 0; offset < 7; offset++ {
			date := now.AddDate(0, 0, offset).Format("2006-01-02")
			e, err := svc.Check(connector.CheckParams{Items: []connector.CheckItem{{Activity: "stargaze", Date: date, Lat: &lat, Lon: &lon}}})
			if err != nil {
				t.Fatal(err)
			}
			got := e.Data.(connector.CheckResult).Items[0]
			want := verdict.Unknown
			if i == 1 && offset < 2 {
				want = verdict.No
			}
			if got.Verdict != want {
				t.Fatalf("entity %d day %d: %+v", i, offset, got)
			}
		}
		e, err := svc.Plan(connector.PlanParams{Lat: &lat, Lon: &lon, From: &from, To: &to, Activities: []string{"stargaze"}})
		if err != nil {
			t.Fatal(err)
		}
		for offset, day := range e.Data.(connector.PlanResult).Days {
			if offset >= 2 || i > 1 {
				if len(day.Stargazing) != 0 || day.StarNote == "" {
					t.Fatalf("empty/stale plan %d: %+v", i, day)
				}
			}
		}
	}
}

func TestSelectedSnapshotFailuresRetainBodiesTagsVersionAndRecover(t *testing.T) {
	for _, bad := range []string{`{"sites":[`, `{"sites":[{"id":""}]}`, `{"sites":[{"id":"a","lat":91}]}`, `{"sites":[{"id":"a","forecast_fetched_at":"bad"}]}`} {
		t.Run(bad, func(t *testing.T) {
			s, f := snapshotStore([]byte(bad))
			ctx := context.Background()
			if err := s.Refresh(ctx); err == nil || s.Current() != nil || s.sitesTag != "" || s.sitesBody != nil {
				t.Fatal("bad initial load accepted")
			}
			f.fail = errors.New("selected object unavailable")
			if err := s.Refresh(ctx); err == nil || s.Current() != nil {
				t.Fatal("unavailable initial load")
			}
			f.fail = nil
			f.objects["connector-sites.json"] = r2.Object{Body: producerSnapshot, ETag: `"s1"`}
			if err := s.Refresh(ctx); err != nil {
				t.Fatal(err)
			}
			first := s.Current()
			body := string(s.sitesBody)
			f.objects["connector-sites.json"] = r2.Object{Body: []byte(bad), ETag: `"bad"`}
			if err := s.Refresh(ctx); err == nil || s.Current() != first || s.sitesTag != `"s1"` || string(s.sitesBody) != body || s.historyTag != `"h1"` {
				t.Fatal("failed refresh changed cached pair")
			}
			if f.tags["connector-sites.json"] != `"s1"` {
				t.Fatal(f.tags)
			}
			f.fail = errors.New("outage")
			if err := s.Refresh(ctx); err == nil || s.Current() != first {
				t.Fatal("outage discarded last good")
			}
			f.fail = nil
			f.objects["connector-sites.json"] = r2.Object{Body: producerSnapshot, ETag: `"s2"`}
			if err := s.Refresh(ctx); err != nil || s.Current().Version != `"s2"|"h1"` {
				t.Fatalf("recovery %v", err)
			}
			recovered := s.Current()
			if err := s.Refresh(ctx); err != nil || s.Current() != recovered || f.tags["connector-sites.json"] != `"s2"` {
				t.Fatal("conditional recovery", err)
			}
		})
	}
}

func TestAllEmptySelectedRefreshIsSuccessfulAndRecovers(t *testing.T) {
	s, f := snapshotStore(producerSnapshot)
	ctx := context.Background()
	if err := s.Refresh(ctx); err != nil {
		t.Fatal(err)
	}
	f.objects["connector-sites.json"] = r2.Object{Body: []byte(`{"sites":[],"count":0,"total_sites":0,"darkness":"none","fetched_at":"2026-10-19T09:00:00Z"}`), ETag: `"empty"`}
	if err := s.Refresh(ctx); err != nil {
		t.Fatal(err)
	}
	if got := s.Current(); len(got.Sites) != 0 || got.Version != `"empty"|"h1"` || got.FetchedAt.Hour() != 9 {
		t.Fatalf("empty success: %+v", got)
	}
	f.objects["connector-sites.json"] = r2.Object{Body: producerSnapshot, ETag: `"recovered"`}
	if err := s.Refresh(ctx); err != nil || len(s.Current().Sites) != 4 {
		t.Fatal("recovery", err)
	}
}

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
	f.objects["sites.json"] = r2.Object{Body: []byte(`{"sites":[{"id":"a","lat":57,"lon":-4,"forecast_fetched_at":"invalid"}]}`), ETag: `"bad-time"`}
	if err := s.Refresh(ctx); err == nil || s.Current() != first {
		t.Fatal("malformed entity timestamp replaced the good snapshot")
	}
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
