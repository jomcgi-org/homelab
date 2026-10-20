package connector_test

import (
	"errors"
	"math"
	"testing"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/connector"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/fixture"
)

func f(v float64) *float64 { return &v }
func i(v int) *int         { return &v }

func TestDarkSkyWindows(t *testing.T) {
	svc := fixture.Service(true)
	env, err := svc.DarkSkyWindows(connector.DarkSkyParams{Lat: f(57.19), Lon: f(-3.83), Nights: i(2)})
	if err != nil {
		t.Fatal(err)
	}
	res := env.Data.(connector.DarkSkyResult)
	if res.Query.Lat != 57.19 || res.Query.Lon != -3.83 || res.Query.RadiusKm != connector.DefaultRadiusKm || res.Query.Limit != connector.DefaultLimit {
		t.Fatalf("query echo = %+v", res.Query)
	}
	if len(res.Nights) != 2 || res.Nights[0] != "2026-10-19" || res.Nights[1] != "2026-10-20" {
		t.Fatalf("nights = %v", res.Nights)
	}
	if len(res.Sites) != 3 || env.Results != 3 {
		t.Fatalf("got %d sites, want the three near Aviemore", len(res.Sites))
	}
	top := res.Sites[0]
	if top.SiteID != "scotland-0001" || top.ClearDark != 3 || top.ClearTwilight != 4 {
		t.Fatalf("top = %+v", top)
	}
	if top.Nights[0].ClearDark != 2 || top.Nights[1].ClearDark != 1 || top.Nights[1].ClearTwilight != 2 || len(top.Nights[0].Hours) != 2 {
		t.Fatalf("top nights = %+v", top.Nights)
	}
	if c := top.Climatology; c == nil || c.Month != 10 || c.ClearSkyOdds == nil || *c.ClearSkyOdds != 0.25 {
		t.Fatalf("climatology = %+v", c)
	}
	if res.Sites[1].SiteID != "scotland-0002" || !res.Sites[1].HoursTruncated {
		t.Fatalf("second = %+v", res.Sites[1])
	}
	if c := res.Sites[1].Climatology; c == nil || c.ClearSkyOdds != nil {
		t.Fatalf("zero-dark month must have null odds: %+v", c)
	}
	if res.Sites[2].Climatology != nil {
		t.Fatal("site without history has climatology")
	}

	m := env.Meta
	if m.APIVersion != "v1" || m.AsOf != "2026-10-19T15:00:00Z" || m.ForecastFetchedAt == nil || *m.ForecastFetchedAt != "2026-10-19T14:05:00Z" ||
		m.DataVersion != `"s1"|"h1"` || m.MethodURL == "" || len(m.Sources) < 2 {
		t.Fatalf("meta = %+v", m)
	}
	for _, s := range m.Sources {
		if s.Name == "" || s.Attribution == "" || s.License == "" || s.URL == "" {
			t.Fatalf("incomplete source %+v", s)
		}
	}
	if len(m.Notes) == 0 {
		t.Fatal("truncated site should add a note")
	}
}

func TestDarkSkyWindowsETagIsCanonical(t *testing.T) {
	svc := fixture.Service(true)
	a, _ := svc.DarkSkyWindows(connector.DarkSkyParams{Lat: f(57.191), Lon: f(-3.829)})
	b, _ := svc.DarkSkyWindows(connector.DarkSkyParams{Lat: f(57.19), Lon: f(-3.83), RadiusKm: f(50), Nights: i(3), Limit: i(5)})
	c, _ := svc.DarkSkyWindows(connector.DarkSkyParams{Lat: f(57.19), Lon: f(-3.83), Limit: i(4)})
	if a.ETag != b.ETag {
		t.Fatal("equal questions have different ETags")
	}
	if a.ETag == c.ETag {
		t.Fatal("different questions share an ETag")
	}
}

func TestDarkSkyWindowsCaps(t *testing.T) {
	svc := fixture.Service(true)
	bad := map[string]connector.DarkSkyParams{
		"lat":     {Lon: f(-3.8)},
		"lon":     {Lat: f(57)},
		"none":    {},
		"nan":     {Lat: f(math.NaN()), Lon: f(-3.8)},
		"london":  {Lat: f(51.5), Lon: f(-0.12)},
		"radius":  {Lat: f(57), Lon: f(-3.8), RadiusKm: f(connector.MaxRadiusKm + 1)},
		"radius0": {Lat: f(57), Lon: f(-3.8), RadiusKm: f(0)},
		"nights":  {Lat: f(57), Lon: f(-3.8), Nights: i(connector.MaxNights + 1)},
		"limit":   {Lat: f(57), Lon: f(-3.8), Limit: i(connector.MaxLimit + 1)},
		"limit0":  {Lat: f(57), Lon: f(-3.8), Limit: i(0)},
	}
	for name, p := range bad {
		var pe *connector.ParamError
		if _, err := svc.DarkSkyWindows(p); !errors.As(err, &pe) {
			t.Errorf("%s: err = %v, want ParamError", name, err)
		}
	}
	env, err := svc.DarkSkyWindows(connector.DarkSkyParams{Lat: f(57.2), Lon: f(-3.8), RadiusKm: f(connector.MaxRadiusKm), Nights: i(connector.MaxNights), Limit: i(connector.MaxLimit)})
	if err != nil {
		t.Fatalf("maximum query rejected: %v", err)
	}
	if n := len(env.Data.(connector.DarkSkyResult).Sites); n > connector.MaxLimit {
		t.Fatalf("returned %d sites", n)
	}
}

func TestDarkSkyWindowsBeforeLoad(t *testing.T) {
	if _, err := fixture.Service(false).DarkSkyWindows(connector.DarkSkyParams{Lat: f(57.19), Lon: f(-3.83)}); !errors.Is(err, connector.ErrUnavailable) {
		t.Fatalf("err = %v", err)
	}
}
