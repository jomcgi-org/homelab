package connector_test

import (
	"encoding/json"
	"errors"
	"strings"
	"testing"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/connector"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/fixture"
)

func marshal(t *testing.T, v any) string {
	t.Helper()
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

// Summary is the default: the answer leads with best and leaves out the
// hourly arrays, which detail=hours brings back.
func TestSummaryIsTheDefault(t *testing.T) {
	svc := fixture.Service(true)
	walks, err := svc.WalkWindows(connector.WalkWindowsParams{Lat: f(57.19), Lon: f(-3.83), Days: i(2)})
	if err != nil {
		t.Fatal(err)
	}
	wr := walks.Data.(connector.WalkWindowsResult)
	if wr.Query.Detail != "summary" || strings.Contains(marshal(t, wr), `"hours"`) {
		t.Fatalf("summary walk answer carries hours: %s", marshal(t, wr))
	}
	best := wr.Walks[0].Best
	if best == nil || best.Date != "2026-10-20" || best.Start != "2026-10-20T08:00:00Z" || best.GoodHours != 6 ||
		best.MaxWindKmh != 20 || best.MinTempC != 6.5 || best.MaxTempC != 8.5 || best.TotalPrecipMM != 0.3 {
		t.Fatalf("best = %+v", best)
	}
	if wr.Walks[1].Best != nil {
		t.Fatal("a walk with no doable day has a best")
	}

	sky, err := svc.DarkSkyWindows(connector.DarkSkyParams{Lat: f(57.19), Lon: f(-3.83), Nights: i(2)})
	if err != nil {
		t.Fatal(err)
	}
	sr := sky.Data.(connector.DarkSkyResult)
	if strings.Contains(marshal(t, sr), `"hours"`) {
		t.Fatal("summary sky answer carries hours")
	}
	top := sr.Sites[0]
	n := top.Nights[0]
	if n.ClearFrom == nil || *n.ClearFrom != "2026-10-19T20:00:00Z" || *n.ClearUntil != "2026-10-19T22:00:00Z" ||
		n.MoonIllumination <= 0 || n.MoonIllumination > 1 || n.MoonlessDark > n.ClearDark {
		t.Fatalf("night = %+v", n)
	}
	if top.BestNight == nil || top.MoonlessDark > top.ClearDark {
		t.Fatalf("site = %+v", top)
	}
	if empty := sr.Sites[2]; empty.BestNight != nil || empty.Nights[0].ClearFrom != nil {
		t.Fatalf("a site with no clear hour has a best night: %+v", empty)
	}
}

func TestNamedNightAndDate(t *testing.T) {
	svc := fixture.Service(true) // the fixture clock is 15:20Z on 19 October 2026
	env, err := svc.DarkSkyWindows(connector.DarkSkyParams{Lat: f(57.19), Lon: f(-3.83), Night: str("2026-10-20")})
	if err != nil {
		t.Fatal(err)
	}
	if r := env.Data.(connector.DarkSkyResult); strings.Join(r.Nights, ",") != "2026-10-20" || r.Query.Nights != 1 || r.Sites[0].Nights[0].ClearDark != 1 {
		t.Fatalf("night answer = %+v", r)
	}
	walks, err := svc.WalkWindows(connector.WalkWindowsParams{Lat: f(57.19), Lon: f(-3.83), Date: str("2026-10-20")})
	if err != nil {
		t.Fatal(err)
	}
	if r := walks.Data.(connector.WalkWindowsResult); strings.Join(r.Days, ",") != "2026-10-20" || r.Walks[0].Days[0].Doable == nil || !*r.Walks[0].Days[0].Doable {
		t.Fatalf("date answer = %+v", r)
	}
	// A date past the hourly forecast is accepted and says so.
	far, err := svc.WalkWindows(connector.WalkWindowsParams{Lat: f(57.19), Lon: f(-3.83), Date: str("2026-10-25")})
	if err != nil {
		t.Fatal(err)
	}
	if d := far.Data.(connector.WalkWindowsResult).Walks[0].Days[0]; d.ForecastCoverage != "none" || d.Doable != nil {
		t.Fatalf("far day = %+v", d)
	}
	for name, err := range map[string]error{
		"night in the past": func() error {
			_, e := svc.DarkSkyWindows(connector.DarkSkyParams{Lat: f(57.19), Lon: f(-3.83), Night: str("2026-10-18")})
			return e
		}(),
		"night too far": func() error {
			_, e := svc.DarkSkyWindows(connector.DarkSkyParams{Lat: f(57.19), Lon: f(-3.83), Night: str("2026-10-26")})
			return e
		}(),
		"night and nights": func() error {
			_, e := svc.DarkSkyWindows(connector.DarkSkyParams{Lat: f(57.19), Lon: f(-3.83), Night: str("2026-10-20"), Nights: i(2)})
			return e
		}(),
		"night not a date": func() error {
			_, e := svc.DarkSkyWindows(connector.DarkSkyParams{Lat: f(57.19), Lon: f(-3.83), Night: str("saturday")})
			return e
		}(),
		"date in the past": func() error {
			_, e := svc.WalkWindows(connector.WalkWindowsParams{Lat: f(57.19), Lon: f(-3.83), Date: str("2026-10-18")})
			return e
		}(),
		"date too far": func() error {
			_, e := svc.WalkWindows(connector.WalkWindowsParams{Lat: f(57.19), Lon: f(-3.83), Date: str("2026-11-02")})
			return e
		}(),
		"date and days": func() error {
			_, e := svc.WalkWindows(connector.WalkWindowsParams{Lat: f(57.19), Lon: f(-3.83), Date: str("2026-10-20"), Days: i(2)})
			return e
		}(),
		"detail unknown": func() error {
			_, e := svc.WalkWindows(connector.WalkWindowsParams{Lat: f(57.19), Lon: f(-3.83), Detail: str("everything")})
			return e
		}(),
		"sky detail unknown": func() error {
			_, e := svc.DarkSkyWindows(connector.DarkSkyParams{Lat: f(57.19), Lon: f(-3.83), Detail: str("minutes")})
			return e
		}(),
	} {
		var pe *connector.ParamError
		if !errors.As(err, &pe) {
			t.Errorf("%s: err = %v, want ParamError", name, err)
		}
	}
}

func TestWalkName(t *testing.T) {
	svc := fixture.Service(true)
	env, err := svc.WalkWindows(connector.WalkWindowsParams{WalkName: str("cairn gorm")})
	if err != nil {
		t.Fatal(err)
	}
	r := env.Data.(connector.WalkWindowsResult)
	if len(r.Walks) != 1 || r.Walks[0].WalkID != "wh-cairn-gorm" || r.Walks[0].FromPointKm != nil || r.Query.Lat != nil || r.Query.WalkName != "cairn gorm" {
		t.Fatalf("name answer = %+v", r)
	}
	// Accents and apostrophes fold.
	env, _ = svc.WalkWindows(connector.WalkWindowsParams{WalkName: str("Meall a Bhuachaille")})
	if r := env.Data.(connector.WalkWindowsResult); len(r.Walks) != 1 {
		t.Fatalf("apostrophe fold = %+v", r.Walks)
	}
	none, _ := svc.WalkWindows(connector.WalkWindowsParams{WalkName: str("Ben Lomond")})
	if len(none.Data.(connector.WalkWindowsResult).Walks) != 0 || !strings.Contains(strings.Join(none.Meta.Notes, " "), "walk_name") {
		t.Fatalf("no-match notes = %v", none.Meta.Notes)
	}
	a, _ := svc.WalkWindows(connector.WalkWindowsParams{WalkName: str("Cairn Gorm")})
	b, _ := svc.WalkWindows(connector.WalkWindowsParams{WalkName: str("cairn gorm")})
	if a.ETag == b.ETag {
		t.Fatal("the echoed name differs, so the ETags must too")
	}
	for name, p := range map[string]connector.WalkWindowsParams{
		"radius without point": {WalkName: str("cairn"), RadiusKm: f(10)},
		"name of punctuation":  {WalkName: str("!!")},
		"name too long":        {WalkName: str(strings.Repeat("a", connector.MaxNameLength+1))},
		"lat without lon":      {WalkName: str("cairn"), Lat: f(57)},
	} {
		var pe *connector.ParamError
		if _, err := svc.WalkWindows(p); !errors.As(err, &pe) {
			t.Errorf("%s: err = %v, want ParamError", name, err)
		}
	}
}
