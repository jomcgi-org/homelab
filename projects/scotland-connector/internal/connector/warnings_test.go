package connector_test

import (
	"encoding/json"
	"strings"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/connector"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/fixture"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/verdict"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/warnings"
)

type warningSource struct{ snapshot *warnings.Snapshot }

func (w warningSource) Current() *warnings.Snapshot { return w.snapshot }
func warningSnapshot(severity string) *warnings.Snapshot {
	s := &warnings.Snapshot{Version: 1, CreatedAt: fixture.Now}
	for _, code := range warnings.Regions {
		s.Regions = append(s.Regions, warnings.Region{Code: code, Complete: true, CheckedAt: fixture.Now, Items: []warnings.Item{}})
	}
	if severity != "" {
		title := severity + " warning of wind affecting Grampian"
		s.Regions[2].Items = []warnings.Item{{Title: title, Description: title + ": Moray valid from 1600 Mon 19 Oct to 2300 Fri 23 Oct", Link: warnings.OfficialURL + "#?date=2026-10-19&id=synthetic"}}
	}
	return s
}

func TestWarningsAllSharedSurfacesAndAlternatives(t *testing.T) {
	for _, severity := range []string{"", "Yellow", "Amber", "Red", "unresolved"} {
		t.Run(severity, func(t *testing.T) {
			svc := fixture.Service(true)
			svc.WarningsData = warningSource{warningSnapshot(severity)}
			lat, lon := 57.3, -3.7
			from, to := "2026-10-19", "2026-10-20"
			e, err := svc.Plan(connector.PlanParams{Lat: &lat, Lon: &lon, From: &from, To: &to})
			if err != nil {
				t.Fatal(err)
			}
			if !e.NoStore {
				t.Fatal("cached warning response")
			}
			p := e.Data.(connector.PlanResult)
			walkCount := 0
			for _, d := range p.Days {
				for _, w := range d.Walks {
					walkCount++
					assertWarningVerdict(t, severity, w.Verdict, w.Why)
				}
				for _, n := range d.Stargazing {
					assertWarningVerdict(t, severity, n.Verdict, n.Why)
				}
			}
			if (severity == "" || severity == "Yellow") && walkCount == 0 {
				t.Fatal("no exercised plan walk")
			}
			if severity == "Amber" || severity == "Red" || severity == "unresolved" {
				for _, d := range p.Days {
					if len(d.Walks) > 0 || len(d.Stargazing) > 0 {
						t.Fatal("unsafe ranked option escaped")
					}
				}
			}
			e, err = svc.Check(connector.CheckParams{Items: []connector.CheckItem{{Activity: "walk", Date: to, WalkID: "wh-meall-a-bhuachaille"}, {Activity: "walk", Date: to, WalkID: "wh-cairn-gorm"}, {Activity: "stargaze", Date: from, Lat: &lat, Lon: &lon}}})
			if err != nil {
				t.Fatal(err)
			}
			for _, r := range e.Data.(connector.CheckResult).Items {
				if r.Error != "" {
					t.Fatal(r.Error)
				}
				assertWarningVerdict(t, severity, r.Verdict, r.Why)
				for _, w := range r.NearbyWalks {
					assertWarningVerdict(t, severity, w.Verdict, w.Why)
				}
				for _, n := range r.NearbyNights {
					assertWarningVerdict(t, severity, n.Verdict, n.Why)
				}
				for _, d := range r.OtherDays {
					assertWarningVerdict(t, severity, d.Verdict, d.Why)
				}
			}
			e, err = svc.Light(fixture.LightParams())
			if err != nil {
				t.Fatal(err)
			}
			for _, n := range e.Data.(connector.LightResult).Nights {
				if n.Night < from {
					continue
				}
				assertWarningVerdict(t, severity, n.Verdict, n.Why)
			}
			if !e.NoStore {
				t.Fatal("cached light")
			}
			found := false
			for _, s := range e.Meta.Sources {
				if s.Name == "Met Office" && s.URL == warnings.TermsURL {
					found = true
				}
			}
			if !found {
				t.Fatal(e.Meta)
			}
		})
	}
}

func assertWarningVerdict(t *testing.T, severity string, v verdict.Verdict, why string) {
	t.Helper()
	if !strings.Contains(why, warnings.CoverageNote) {
		t.Fatal("missing region limit", why)
	}
	if severity == "Yellow" && v == verdict.Go {
		t.Fatal("yellow go", why)
	}
	if (severity == "Amber" || severity == "Red" || severity == "unresolved") && (v == verdict.Go || v == verdict.Marginal) {
		t.Fatal("unsafe", v, why)
	}
	if severity == "Yellow" || severity == "Amber" || severity == "Red" {
		i := warningSnapshot(severity).Regions[2].Items[0]
		if !strings.Contains(why, i.Title+"\n"+i.Description+"\n"+i.Link) {
			t.Fatal("lost verbatim content", why)
		}
	}
}

func TestWarningAgeUsesRequestClockWithinHour(t *testing.T) {
	svc := fixture.Service(true)
	snap := warningSnapshot("")
	svc.WarningsData = warningSource{snap}
	lat, lon := 57.3, -3.7
	p := connector.CheckParams{Items: []connector.CheckItem{{Activity: "walk", Date: "2026-10-20", WalkID: "wh-meall-a-bhuachaille"}, {Activity: "stargaze", Date: "2026-10-19", Lat: &lat, Lon: &lon}}}
	a, _ := svc.Check(p)
	svc.Now = func() time.Time { return fixture.Now.Add(5 * time.Minute) }
	b, _ := svc.Check(p)
	if a.Meta.AsOf != b.Meta.AsOf {
		t.Fatal("test needs same forecast hour")
	}
	if b.Data.(connector.CheckResult).Items[0].Verdict != verdict.Unknown {
		t.Fatal("source age used top of hour")
	}
	// Warning integration must leave the astronomical windows and cloud support alone.
	light, _ := svc.Light(fixture.LightParams())
	svc.WarningsData = nil
	plain, _ := svc.Light(fixture.LightParams())
	x, _ := json.Marshal(light.Data.(connector.LightResult).Days)
	y, _ := json.Marshal(plain.Data.(connector.LightResult).Days)
	if string(x) != string(y) {
		t.Fatal("warnings changed geometry or cloud support")
	}
}
