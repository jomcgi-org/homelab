package connector_test

import (
	"encoding/json"
	"errors"
	"strings"
	"testing"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/connector"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/fixture"
)

func TestWalkWindows(t *testing.T) {
	env, err := fixture.Service(true).WalkWindows(connector.WalkWindowsParams{Lat: f(57.19), Lon: f(-3.83), Days: i(2)})
	if err != nil {
		t.Fatal(err)
	}
	res := env.Data.(connector.WalkWindowsResult)
	if strings.Join(res.Days, ",") != "2026-10-19,2026-10-20" || res.Query.RadiusKm != connector.DefaultWalkRadiusKm {
		t.Fatalf("days %v query %+v", res.Days, res.Query)
	}
	if len(res.Walks) != 2 || env.Results != 2 {
		t.Fatalf("walks = %+v", res.Walks)
	}
	top := res.Walks[0]
	if top.WalkID != "wh-meall-a-bhuachaille" || top.DoableDays != 1 || !strings.HasPrefix(top.URL, "https://www.walkhighlands.co.uk/") {
		t.Fatalf("top = %+v", top)
	}
	day := top.Days[1]
	if day.Date != "2026-10-20" || !day.Doable || day.GoodHours != 6 || len(day.Hours) != 6 ||
		day.BestSlot == nil || day.BestSlot.Start != "2026-10-20T08:00:00Z" || day.BestSlot.End != "2026-10-20T14:00:00Z" {
		t.Fatalf("day = %+v", day)
	}
	if long := res.Walks[1]; long.DoableDays != 0 || long.Days[1].GoodHours != 2 || long.Days[1].Doable {
		t.Fatalf("a 9 h walk with 2 good hours is not doable: %+v", long)
	}

	m := env.Meta
	if m.ForecastFetchedAt == nil || *m.ForecastFetchedAt != "2026-10-19T14:20:00Z" || m.DataVersion != `"w1"` || len(m.Sources) != 2 {
		t.Fatalf("meta = %+v", m)
	}
	if !strings.Contains(strings.Join(m.Notes, " "), "start point") {
		t.Fatalf("notes must say the forecast is at the start: %v", m.Notes)
	}
	body, _ := json.Marshal(env)
	if strings.Contains(string(body), "summary") {
		t.Fatal("walk prose leaked into the answer")
	}
}

func TestWalkWindowsFiltersAndETag(t *testing.T) {
	svc := fixture.Service(true)
	env, err := svc.WalkWindows(connector.WalkWindowsParams{Lat: f(57.19), Lon: f(-3.83), MaxAscentM: i(600)})
	if err != nil {
		t.Fatal(err)
	}
	if ws := env.Data.(connector.WalkWindowsResult).Walks; len(ws) != 1 || ws[0].WalkID != "wh-meall-a-bhuachaille" {
		t.Fatalf("ascent filter = %+v", ws)
	}
	d4 := 4.0
	short, _ := svc.WalkWindows(connector.WalkWindowsParams{Lat: f(57.19), Lon: f(-3.83), MaxDurationH: &d4})
	if short.ETag == env.ETag {
		t.Fatal("different filters share an ETag")
	}
	a, _ := svc.WalkWindows(connector.WalkWindowsParams{Lat: f(57.191), Lon: f(-3.829)})
	b, _ := svc.WalkWindows(connector.WalkWindowsParams{Lat: f(57.19), Lon: f(-3.83), RadiusKm: f(30), Days: i(3), Limit: i(5)})
	if a.ETag != b.ETag {
		t.Fatal("equal questions have different ETags")
	}
	none, _ := svc.WalkWindows(connector.WalkWindowsParams{Lat: f(60.3), Lon: f(-1.3), RadiusKm: f(5)})
	if len(none.Data.(connector.WalkWindowsResult).Walks) != 0 || len(none.Meta.Notes) <= 3 {
		t.Fatalf("empty answer should say why: %+v", none.Meta.Notes)
	}
}

func TestWalkWindowsCaps(t *testing.T) {
	svc := fixture.Service(true)
	for name, p := range map[string]connector.WalkWindowsParams{
		"no coords": {},
		"london":    {Lat: f(51.5), Lon: f(-0.12)},
		"radius":    {Lat: f(57), Lon: f(-3.8), RadiusKm: f(connector.MaxWalkRadiusKm + 1)},
		"days":      {Lat: f(57), Lon: f(-3.8), Days: i(connector.MaxWalkDays + 1)},
		"days0":     {Lat: f(57), Lon: f(-3.8), Days: i(0)},
		"limit":     {Lat: f(57), Lon: f(-3.8), Limit: i(connector.MaxLimit + 1)},
		"duration":  {Lat: f(57), Lon: f(-3.8), MaxDurationH: f(0.1)},
		"ascent":    {Lat: f(57), Lon: f(-3.8), MaxAscentM: i(0)},
	} {
		var pe *connector.ParamError
		if _, err := svc.WalkWindows(p); !errors.As(err, &pe) {
			t.Errorf("%s: err = %v, want ParamError", name, err)
		}
	}
}

func TestWalkWindowsBeforePublish(t *testing.T) {
	svc := fixture.Service(true)
	svc.Walks = fixture.WalkSource{}
	if _, err := svc.WalkWindows(connector.WalkWindowsParams{Lat: f(57.19), Lon: f(-3.83)}); !errors.Is(err, connector.ErrUnavailable) {
		t.Fatalf("err = %v", err)
	}
	// Stars keep answering without walks.
	if _, err := svc.DarkSkyWindows(connector.DarkSkyParams{Lat: f(57.19), Lon: f(-3.83)}); err != nil {
		t.Fatal(err)
	}
}
