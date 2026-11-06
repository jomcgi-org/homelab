package connector_test

import (
	"encoding/json"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/connector"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/fixture"
)

func TestLightKindsCanonicalAndIsolated(t *testing.T) {
	svc := fixture.Service(true)
	p := fixture.LightParams()
	all, err := svc.Light(p)
	if err != nil {
		t.Fatal(err)
	}
	for _, kinds := range [][]string{nil, {}, {"milky_way", "darkness", "astronomical", "nautical", "civil", "blue", "golden", "golden"}} {
		p.Kinds = kinds
		env, err := svc.Light(p)
		if err != nil || env.ETag != all.ETag || !reflect.DeepEqual(env.Data, all.Data) {
			t.Fatalf("all kinds %v: %v", kinds, err)
		}
	}
	p.Kinds = []string{"blue", "golden", "blue"}
	a, err := svc.Light(p)
	if err != nil {
		t.Fatal(err)
	}
	p.Kinds = []string{" golden ", "blue"}
	b, err := svc.Light(p)
	if err != nil || a.ETag != b.ETag || !reflect.DeepEqual(a.Data, b.Data) || a.ETag == all.ETag {
		t.Fatal("filter identity", err)
	}
	for _, kind := range []string{"golden", "blue", "civil", "nautical", "astronomical", "darkness", "milky_way"} {
		p.Kinds = []string{kind}
		env, err := svc.Light(p)
		if err != nil {
			t.Fatal(err)
		}
		if env.ETag == a.ETag || env.ETag == all.ETag {
			t.Fatal("distinct filter shares validator")
		}
		want := kind
		if kind == "civil" || kind == "nautical" || kind == "astronomical" {
			want += "_twilight"
		}
		if kind == "darkness" {
			want = "astronomical_darkness"
		}
		for _, d := range env.Data.(connector.LightResult).Days {
			for _, w := range d.Windows {
				if w.Kind != want {
					t.Fatalf("%s: %+v", kind, w)
				}
			}
		}
	}
	for _, kinds := range [][]string{{""}, {"golden", ""}, {"GOLDEN"}, {"cloud"}, {" "}} {
		p.Kinds = kinds
		if _, err := svc.Light(p); err == nil {
			t.Fatal("accepted", kinds)
		}
	}
}

func TestLightCompactCloudPreservesEvidence(t *testing.T) {
	for _, loaded := range []bool{false, true} {
		svc := fixture.Service(loaded)
		p := fixture.LightParams()
		if !loaded {
			p.Lat, p.Lon = f(55), f(-8)
		}
		env, err := svc.Light(p)
		if err != nil {
			t.Fatal(err)
		}
		b, err := json.Marshal(env)
		if err != nil {
			t.Fatal(err)
		}
		var wire struct {
			Data struct {
				Days []struct {
					CloudWhy string                       `json:"cloud_why"`
					Windows  []map[string]json.RawMessage `json:"windows"`
				} `json:"days"`
			} `json:"data"`
		}
		if err := json.Unmarshal(b, &wire); err != nil {
			t.Fatal(err)
		}
		for i, day := range env.Data.(connector.LightResult).Days {
			wd := wire.Data.Days[i]
			if wd.CloudWhy == "" || strings.Count(string(b), `"cloud_why"`) != len(wire.Data.Days) {
				t.Fatal("missing day explanation")
			}
			for j, w := range day.Windows {
				raw, present := wd.Windows[j]["cloud"]
				if w.Cloud.Samples == 0 {
					if present || w.Cloud.Verdict != "unknown" || w.Cloud.Coverage != "none" || w.Cloud.SupportedSeconds != 0 {
						t.Fatalf("unsupported window changed: %+v", w)
					}
				} else {
					var c connector.LightCloud
					if !present || json.Unmarshal(raw, &c) != nil {
						t.Fatal("lost known support")
					}
					want := w.Cloud
					want.Why = ""
					if c != want {
						t.Fatal("support changed", c, want)
					}
				}
			}
		}
	}
}

func TestPlanDatasetHorizonRuns(t *testing.T) {
	svc := fixture.Service(true)
	env, err := svc.Plan(connector.PlanParams{Lat: f(57.19), Lon: f(-3.83), To: s("2026-10-25")})
	if err != nil {
		t.Fatal(err)
	}
	r := env.Data.(connector.PlanResult)
	for activity, want := range map[string]time.Time{"walk": fixture.WalkDataset().HourlyUntil, "stargaze": fixture.Dataset().HourlyUntil} {
		c := r.Datasets[activity]
		if c.HourlyUntil == nil || *c.HourlyUntil != want.Format(time.RFC3339) || len(c.Notes) != 1 {
			t.Fatalf("%s coverage %+v", activity, c)
		}
		first := "2026-10-22"
		if activity == "stargaze" {
			first = "2026-10-21"
		}
		if c.Notes[0].From != first || c.Notes[0].To != "2026-10-25" {
			t.Fatal(c.Notes)
		}
	}
	if !strings.Contains(r.Days[2].WalkNote, "reaches only") || r.Days[0].WalkNote == "" {
		t.Fatal("partial or unrelated note lost", r.Days)
	}
	for _, d := range r.Days {
		if strings.Contains(d.WalkNote+d.StarNote, "Beyond the hourly forecast") {
			t.Fatal("duplicate horizon note", d)
		}
		if d.Date >= "2026-10-21" && !strings.Contains(d.StarNote, "Moon illumination") {
			t.Fatal("lost daily lunar advice", d)
		}
	}
	// A range starting beyond the horizon reports exactly the requested run.
	env, err = svc.Plan(connector.PlanParams{Lat: f(57.19), Lon: f(-3.83), From: s("2026-10-24"), To: s("2026-10-25")})
	if err != nil {
		t.Fatal(err)
	}
	for _, c := range env.Data.(connector.PlanResult).Datasets {
		if len(c.Notes) != 1 || c.Notes[0].From != "2026-10-24" {
			t.Fatal(c)
		}
	}
	// An absent walk snapshot is null coverage, with its loading explanations
	// left on each day; the sky dataset retains its independent horizon.
	svc.Walks = fixture.WalkSource{}
	env, err = svc.Plan(connector.PlanParams{Lat: f(57.19), Lon: f(-3.83)})
	if err != nil {
		t.Fatal(err)
	}
	r = env.Data.(connector.PlanResult)
	if r.Datasets["walk"].HourlyUntil != nil || len(r.Datasets["walk"].Notes) != 0 || r.Datasets["stargaze"].HourlyUntil == nil {
		t.Fatal(r.Datasets)
	}
	for _, d := range r.Days {
		if d.WalkNote == "" {
			t.Fatal("lost loading explanation")
		}
	}
}

func TestPlanHorizonBoundaryAndAbsentTimestamp(t *testing.T) {
	// The walk coverage model starts its possible daylight envelope at 04:00
	// Europe/London. Exactly at that boundary has no support; one second
	// later is partial coverage, even though no whole hour has been published.
	boundary := time.Date(2026, 10, 20, 3, 0, 0, 0, time.UTC)
	for _, delta := range []time.Duration{-time.Second, 0, time.Second} {
		svc := fixture.Service(true)
		wk := fixture.WalkDataset()
		wk.HourlyUntil = boundary.Add(delta)
		svc.Walks = fixture.WalkSource{DS: wk}
		env, err := svc.Plan(connector.PlanParams{Lat: f(57.19), Lon: f(-3.83), From: s("2026-10-20"), To: s("2026-10-20"), Activities: []string{"walk"}})
		if err != nil {
			t.Fatal(err)
		}
		r := env.Data.(connector.PlanResult)
		c := r.Datasets["walk"]
		if c.HourlyUntil == nil || *c.HourlyUntil != wk.HourlyUntil.Format(time.RFC3339) || len(r.Datasets) != 1 {
			t.Fatal(c)
		}
		if delta <= 0 {
			if len(c.Notes) != 1 || c.Notes[0].From != "2026-10-20" || c.Notes[0].To != "2026-10-20" || r.Days[0].WalkNote != "" {
				t.Fatal("boundary coverage overstated", r)
			}
		} else if len(c.Notes) != 0 || !strings.Contains(r.Days[0].WalkNote, "reaches only") {
			t.Fatal("partial explanation collapsed", r)
		}
	}
	svc := fixture.Service(true)
	wk := fixture.WalkDataset()
	wk.HourlyUntil = time.Time{}
	svc.Walks = fixture.WalkSource{DS: wk}
	env, err := svc.Plan(connector.PlanParams{Lat: f(57.19), Lon: f(-3.83), From: s("2026-10-20"), To: s("2026-10-21"), Activities: []string{"walk"}})
	if err != nil {
		t.Fatal(err)
	}
	r := env.Data.(connector.PlanResult)
	if r.Datasets["walk"].HourlyUntil != nil || len(r.Datasets["walk"].Notes) != 0 {
		t.Fatal("invented horizon", r)
	}
	for _, d := range r.Days {
		if !strings.Contains(d.WalkNote, "No successful forecast fetch") {
			t.Fatal("missing-data explanation changed", d)
		}
	}
}
