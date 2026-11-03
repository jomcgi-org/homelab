package connector_test

import (
	"encoding/json"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/connector"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/fixture"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/summits"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/verdict"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/walks"
)

type summitSource struct {
	d           *summits.Snapshot
	calls       int
	panicOnRead bool
}

func (s *summitSource) Current() *summits.Snapshot {
	if s.panicOnRead {
		panic("disabled consumption")
	}
	s.calls++
	return s.d
}

func summitFixture(t *testing.T) *summits.Snapshot {
	t.Helper()
	b, e := os.ReadFile("../summits/testdata/summits.json")
	if e != nil {
		t.Fatal(e)
	}
	d, e := summits.Parse(b)
	if e != nil {
		t.Fatal(e)
	}
	return d
}

func TestUKVCheckAndDisabledCompatibility(t *testing.T) {
	params := connector.CheckParams{Items: []connector.CheckItem{{Activity: "walk", Date: "2026-10-20", WalkID: "wh-meall-a-bhuachaille"}}}
	svc := fixture.Service(true)
	svc.Now = func() time.Time { return time.Date(2026, 10, 20, 7, 0, 0, 0, time.UTC) }
	baseline, err := svc.Check(params)
	if err != nil {
		t.Fatal(err)
	}
	if baseline.Data.(connector.CheckResult).Items[0].Verdict != verdict.Go {
		t.Fatal("fixture must start go")
	}
	source := &summitSource{panicOnRead: true}
	svc.SummitData = source
	off, err := svc.Check(params)
	if err != nil {
		t.Fatal(err)
	}
	a, _ := json.Marshal(baseline)
	b, _ := json.Marshal(off)
	if string(a) != string(b) {
		t.Fatal("disabled output changed")
	}
	source.panicOnRead = false
	source.d = summitFixture(t)
	svc.UKVEnabled = true
	on, err := svc.Check(params)
	if err != nil {
		t.Fatal(err)
	}
	item := on.Data.(connector.CheckResult).Items[0]
	if item.Verdict != verdict.Marginal || item.Walk.Verdict != item.Verdict || !strings.Contains(item.Why, "summit in cloud") || !strings.Contains(item.Why, "above freezing level") {
		t.Fatalf("%+v", item)
	}
	if item.Walk.UKV == nil || !strings.Contains(item.Walk.UKV.Note, "Synthetic") {
		t.Fatal("fixture identity missing")
	}
	if len(on.Meta.Sources) != len(baseline.Meta.Sources)+10 {
		t.Fatal(on.Meta.Sources)
	}
	attributed := map[string]bool{}
	for n, src := range on.Meta.Sources {
		if n < len(baseline.Meta.Sources) {
			x, _ := json.Marshal(src)
			y, _ := json.Marshal(baseline.Meta.Sources[n])
			if string(x) != string(y) {
				t.Fatal("existing terms changed")
			}
			continue
		}
		if len(src.Fields) != 1 || src.LicenseURI != summits.License || src.Adaptation != summits.Adaptation {
			t.Fatal(src)
		}
		attributed[src.Fields[0]] = true
	}
	for _, field := range []string{"run_at", "points[].sample_lat", "points[].sample_lon", "points[].cloud_base_reference", "points[].orography_m_asl", "points[].hours[].valid_at", "points[].hours[].wind_kmh", "points[].hours[].gust_kmh", "points[].hours[].cloud_base_m", "points[].hours[].freezing_level_m_asl"} {
		if !attributed["data.items[].walk.ukv."+field] {
			t.Fatalf("missing attribution: %s", field)
		}
	}
	// Missing, stale and walk-start-only evidence preserve the baseline verdict.
	for _, kind := range []string{"missing", "stale", "walk_start", "elevation"} {
		d := summitFixture(t)
		switch kind {
		case "missing":
			d = nil
		case "stale":
			d.RunAt = d.RunAt.Add(-24 * time.Hour)
		case "walk_start":
			d.Points[0].Kind = "walk_start"
			d.Points[0].Lat = 57.17
			d.Points[0].Lon = -3.67
			d.Fixture = false
		case "elevation":
			d.Points[0].Elevation = nil
		}
		source.d = d
		env, e := svc.Check(params)
		if e != nil {
			t.Fatal(e)
		}
		got := env.Data.(connector.CheckResult).Items[0]
		if got.Verdict != verdict.Go || got.Why != baseline.Data.(connector.CheckResult).Items[0].Why {
			t.Fatalf("%s %+v", kind, got)
		}
	}
}

func TestUKVPreservesNonGo(t *testing.T) {
	for _, wind := range []float32{60, 100} {
		svc := fixture.Service(true)
		var raw map[string]any
		if e := json.Unmarshal([]byte(fixture.Walks), &raw); e != nil {
			t.Fatal(e)
		}
		for _, w := range raw["walks"].([]any) {
			for _, h := range w.(map[string]any)["hours"].([]any) {
				h.([]any)[3] = wind
			}
		}
		body, _ := json.Marshal(raw)
		wd, e := walks.Parse(body, "synthetic-wind")
		if e != nil {
			t.Fatal(e)
		}
		svc.Walks = fixture.WalkSource{DS: wd}
		svc.Now = func() time.Time { return time.Date(2026, 10, 20, 7, 0, 0, 0, time.UTC) }
		params := connector.CheckParams{Items: []connector.CheckItem{{Activity: "walk", Date: "2026-10-20", WalkID: "wh-meall-a-bhuachaille"}}}
		before, e := svc.Check(params)
		if e != nil {
			t.Fatal(e)
		}
		want := before.Data.(connector.CheckResult).Items[0]
		if want.Verdict == verdict.Go {
			t.Fatal("expected non-go")
		}
		svc.UKVEnabled = true
		svc.SummitData = &summitSource{d: summitFixture(t)}
		after, e := svc.Check(params)
		if e != nil {
			t.Fatal(e)
		}
		got := after.Data.(connector.CheckResult).Items[0]
		if got.Verdict != want.Verdict || got.Why != want.Why {
			t.Fatal("non-go changed", want, got)
		}
	}
}
