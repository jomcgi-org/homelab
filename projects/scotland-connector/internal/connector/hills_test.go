package connector_test

import (
	"encoding/json"
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/connector"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/fixture"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/verdict"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/walks"
)

type hillSource struct{ ds *walks.HillDataset }

func (h hillSource) Current() *walks.HillDataset { return h.ds }

func hillService(t *testing.T, explicit bool) *connector.Service {
	t.Helper()
	now := time.Date(2026, 6, 20, 0, 0, 0, 0, time.UTC)
	var hours [][]any
	for hour := 5; hour < 20; hour++ {
		hours = append(hours, []any{now.Add(time.Duration(hour) * time.Hour).Unix(), 12, 0, 10, 20, 0, 0})
	}
	row := map[string]any{"id": "ben", "name": "Ben Nevis via the mountain track", "url": "https://www.walkhighlands.co.uk/fortwilliam/ben-nevis.shtml", "lat": 56.8108, "lon": -5.076, "distance_km": 17, "ascent_m": 1350, "duration_h": 9, "hours": hours}
	if explicit {
		row["summit_names"] = []string{"Ben Nevis"}
	}
	row["forecast_fetched_at"] = now.Format(time.RFC3339)
	body, _ := json.Marshal(map[string]any{"schema_version": 2, "fetched_at": now.Format(time.RFC3339), "walks": []any{row}})
	wd, err := walks.Parse(body, "w1")
	if err != nil {
		t.Fatal(err)
	}
	svc := fixture.Service(true)
	svc.Now = func() time.Time { return now }
	svc.Walks = fixture.WalkSource{DS: wd}
	svc.Hills = hillSource{&walks.HillDataset{Version: "h1", Source: walks.HillSource{Name: "DoBIH", Version: "18.6", Attribution: "The Database of British and Irish Hills v18.6", License: walks.HillLicense, URL: walks.HillSourceURL, Modifications: "Filtered to Scotland; joined to walks."}, Hills: []walks.Hill{{Number: 1, Name: "Ben Nevis", Classes: []string{"Munro"}, Lat: 56.79685, Lon: -5.00351, HeightM: 1345}}}}
	return svc
}

func TestHillPlanAndCheck(t *testing.T) {
	for _, explicit := range []bool{false, true} {
		svc := hillService(t, explicit)
		filter := connector.HillFilter{HillClass: "Munro", HillName: "ben nevis"}
		pp := connector.PlanParams{Lat: f(56.82), Lon: f(-5.1), From: s("2026-06-20"), To: s("2026-06-20"), Activities: []string{"walk"}, WalkFilter: connector.WalkFilter{HillFilter: filter}}
		cp := connector.CheckParams{Items: []connector.CheckItem{{Activity: "walk", Date: "2026-06-20", WalkID: "ben"}}, HillFilter: filter}
		p, err := svc.Plan(pp)
		if err != nil {
			t.Fatal(err)
		}
		opts := p.Data.(connector.PlanResult).Days[0].Walks
		if len(opts) != 1 || opts[0].Verdict != verdict.Go {
			t.Fatalf("plan: %+v", p.Data)
		}
		svc.Hills.Current().Version = "h2"
		newPlan, err := svc.Plan(pp)
		if err != nil || newPlan.ETag == p.ETag {
			t.Fatal("hill replacement did not invalidate plan", err)
		}
		c, err := svc.Check(cp)
		if err != nil {
			t.Fatal(err)
		}
		checked := c.Data.(connector.CheckResult).Items[0]
		if checked.Walk == nil || checked.Verdict != verdict.Go {
			t.Fatalf("check: %+v", checked)
		}
		for _, env := range []*connector.Envelope{p, c} {
			body, _ := json.Marshal(env.Data)
			if !strings.Contains(string(body), `"hills":[{"name":"Ben Nevis","class":"Munro","height_m":1345}]`) {
				t.Fatal(string(body))
			}
			if strings.Contains(strings.Join(env.Meta.Notes, " "), "Hill matches inferred") == explicit {
				t.Fatal(env.Meta.Notes)
			}
			src := env.Meta.Sources[len(env.Meta.Sources)-1]
			if src.Name != "DoBIH" || src.Version != "18.6" || src.Modifications == "" || src.License != walks.HillLicense {
				t.Fatal(src)
			}
		}
		// No snapshot: filters explicitly fail, existing unfiltered answers remain.
		svc.Hills = nil
		if _, err := svc.Plan(pp); !errors.Is(err, connector.ErrUnavailable) {
			t.Fatal(err)
		}
		if _, err := svc.Check(cp); !errors.Is(err, connector.ErrUnavailable) {
			t.Fatal(err)
		}
		cp.HillFilter = connector.HillFilter{}
		old, err := svc.Check(cp)
		if err != nil {
			t.Fatal(err)
		}
		body, _ := json.Marshal(old.Data)
		if strings.Contains(string(body), `"hills"`) {
			t.Fatal(string(body))
		}
	}
}

func TestHillFiltersAndUnmatchedOmission(t *testing.T) {
	svc := hillService(t, true)
	for _, class := range walks.HillClasses {
		ds := svc.Hills.Current()
		ds.Hills[0].Classes = []string{class}
		for _, name := range []string{"", "BEN NEVIS", "unmatched"} {
			filter := connector.HillFilter{HillClass: class, HillName: name}
			cp := connector.CheckParams{Items: []connector.CheckItem{{Activity: "walk", Date: "2026-06-20", WalkName: "Ben Nevis"}}, HillFilter: filter}
			env, err := svc.Check(cp)
			if err != nil {
				t.Fatal(err)
			}
			hit := env.Data.(connector.CheckResult).Items[0]
			if (hit.Walk == nil) != (name == "unmatched") {
				t.Fatalf("%s %s %+v", class, name, hit)
			}
			p, err := svc.Plan(connector.PlanParams{Lat: f(56.82), Lon: f(-5.1), From: s("2026-06-20"), To: s("2026-06-20"), Activities: []string{"walk"}, WalkFilter: connector.WalkFilter{HillFilter: filter}})
			if err != nil {
				t.Fatal(err)
			}
			if (len(p.Data.(connector.PlanResult).Days[0].Walks) == 0) != (name == "unmatched") {
				t.Fatal(p.Data)
			}
		}
	}
	svc.Hills.Current().Hills[0].Name = "Unrelated nearby summit"
	env, err := svc.Check(connector.CheckParams{Items: []connector.CheckItem{{Activity: "walk", Date: "2026-06-20", WalkID: "ben"}}})
	if err != nil {
		t.Fatal(err)
	}
	body, _ := json.Marshal(env.Data)
	if strings.Contains(string(body), `"hills"`) || strings.Contains(strings.Join(env.Meta.Notes, " "), "Hill matches inferred") {
		t.Fatal(string(body), env.Meta.Notes)
	}
	p, err := svc.Plan(connector.PlanParams{Lat: f(56.82), Lon: f(-5.1), From: s("2026-06-20"), To: s("2026-06-20"), Activities: []string{"walk"}})
	if err != nil {
		t.Fatal(err)
	}
	body, _ = json.Marshal(p.Data)
	if strings.Contains(string(body), `"hills"`) || strings.Contains(strings.Join(p.Meta.Notes, " "), "Hill matches inferred") {
		t.Fatal(string(body), p.Meta.Notes)
	}
	for _, filter := range []connector.HillFilter{{HillClass: "Marilyn"}, {HillName: "!!!"}, {HillName: strings.Repeat("a", 101)}} {
		if _, err := svc.Check(connector.CheckParams{HillFilter: filter}); err == nil {
			t.Fatal("invalid filter accepted")
		}
	}
}
