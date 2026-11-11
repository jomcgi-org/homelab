package connector_test

import (
	"encoding/json"
	"math"
	"strings"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/astro"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/connector"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/fixture"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/stars"
)

func lightDay(t *testing.T, date string, lat, lon float64) connector.LightDay {
	t.Helper()
	svc := fixture.Service(false)
	now, _ := time.Parse(time.DateOnly, date)
	svc.Now = func() time.Time { return now.Add(12 * time.Hour) }
	env, err := svc.Light(connector.LightParams{Lat: &lat, Lon: &lon, From: &date, To: &date})
	if err != nil {
		t.Fatal(err)
	}
	return env.Data.(connector.LightResult).Days[0]
}

// Independently retrieved 2026-10-28 from US Naval Observatory API v4.0.1:
// https://aa.usno.navy.mil/api/rstt/oneday?date=2026-03-20&coords=55.9533,-3.1868&tz=0
// Repeat date=2026-06-21 and 2026-12-21. WGS84 Edinburgh 55.9533 N,
// 3.1868 W, tz=0 and isdst=false: published clocks below are UTC, not BST.
// The public input rounds to 55.95,-3.19; the tolerance includes that rounding.
func TestLightPublishedEdinburghSun(t *testing.T) {
	for _, r := range []struct{ date, rise, set string }{
		{"2026-03-20", "06:15", "18:26"},
		{"2026-06-21", "03:26", "21:03"},
		{"2026-12-21", "08:42", "15:40"},
	} {
		d := lightDay(t, r.date, 55.9533, -3.1868)
		for _, c := range []struct {
			got  *string
			want string
		}{{d.Sunrise, r.rise}, {d.Sunset, r.set}} {
			if c.got == nil {
				t.Fatal(r.date, "missing solar event")
			}
			got, _ := time.Parse(time.RFC3339, *c.got)
			want, _ := time.Parse("2006-01-02 15:04", r.date+" "+c.want)
			if math.Abs(got.Sub(want).Seconds()) > 120 {
				t.Errorf("%s got %s want %s within two minutes", r.date, got, want)
			}
		}
	}
}

// USNO https://aa.usno.navy.mil/api/moon/phases/date?date=2026-07-01&nump=4
// v4.0.1, fetched 2026-10-28: full moon 2026-07-29 14:36 UTC.
func TestLightMoonPhaseDirection(t *testing.T) {
	full, _ := time.Parse(time.RFC3339, "2026-07-29T14:36:00Z")
	m := astro.MoonAt(full)
	if m.Lit < 0.995 || math.Abs(m.Phase()-0.5) > 0.01 {
		t.Fatalf("full moon lit=%g phase=%g", m.Lit, m.Phase())
	}
	for _, r := range []struct{ date, direction string }{{"2026-07-27", "waxing"}, {"2026-08-01", "waning"}} {
		d := lightDay(t, r.date, 54.95, -4.45)
		if d.Moon.Direction != r.direction {
			t.Fatalf("%s %+v", r.date, d.Moon)
		}
		if d.Moon.ReferenceAt != r.date+"T11:00:00Z" {
			t.Fatal("not local noon in BST", d.Moon)
		}
	}
}

func TestLightGallowayCore(t *testing.T) {
	// Independent PyEphem 4.2, pressure=0, WGS84 54.95,-4.45, J2000
	// fixed body RA=17:45, dec=-29, 2026-07-31 23:00 UTC. Reference
	// generation is recorded in ARCHITECTURE.md. Twilight persists in July;
	// these windows use the stars model's sun<-12, not sun<-18.
	for _, r := range []struct {
		date string
		want bool
	}{{"2026-07-31", true}, {"2026-12-21", false}} {
		d := lightDay(t, r.date, 54.95, -4.45)
		found := false
		for _, w := range d.Windows {
			if w.Kind != "milky_way" {
				continue
			}
			found = true
			from, _ := time.Parse(time.RFC3339, w.From)
			until, _ := time.Parse(time.RFC3339, w.Until)
			for at := from.Add(time.Second); at.Before(until.Add(-time.Second)); at = at.Add(5 * time.Minute) {
				if astro.SunElevation(at, 54.95, -4.45) > -11.99 || astro.GalacticCentreAltitude(at, 54.95, -4.45) < -0.01 {
					t.Fatalf("physically inconsistent window %+v at %v", w, at)
				}
			}
		}
		if found != r.want {
			t.Fatalf("%s core-visible=%t", r.date, found)
		}
	}
	d := lightDay(t, "2026-07-31", 54.95, -4.45)
	at, _ := time.Parse(time.RFC3339, "2026-07-31T23:00:00Z")
	contains := false
	for _, w := range d.Windows {
		if w.Kind == "milky_way" && w.From < at.Format(time.RFC3339) && w.Until >= at.Format(time.RFC3339) {
			contains = true
		}
	}
	if !contains || d.Astronomical.Dawn != nil || d.Astronomical.Dusk != nil {
		t.Fatal("July reference must be within core window with persistent astronomical twilight", d)
	}
}

func TestLightGrazingCoreAtCoverageBoundary(t *testing.T) {
	for _, tc := range []struct {
		name string
		date string
		lat  float64
		lon  float64
	}{
		{"north boundary in November", "2026-11-11", 61, -8.25},
		{"rounds to north boundary", "2026-11-11", 60.996, -8.25},
		{"north boundary in March", "2026-03-10", 61, -8.92},
	} {
		t.Run(tc.name, func(t *testing.T) {
			d := lightDay(t, tc.date, tc.lat, tc.lon)
			for _, w := range d.Windows {
				if w.Kind == "milky_way" {
					t.Errorf("grazing core at latitude 61 must not produce a Milky Way window: %+v", w)
				}
			}
		})
	}
}

func TestLightDatesAndMissingEvents(t *testing.T) {
	loc, err := time.LoadLocation("Europe/London")
	if err != nil {
		t.Fatal(err)
	}
	for _, date := range []string{"2026-03-29", "2026-10-25", "2026-06-21", "2026-12-21"} {
		d := lightDay(t, date, 60.15, -1.15)
		start, _ := time.ParseInLocation(time.DateOnly, date, loc)
		end := start.AddDate(0, 0, 1)
		last := ""
		for _, w := range d.Windows {
			from, _ := time.Parse(time.RFC3339, w.From)
			until, _ := time.Parse(time.RFC3339, w.Until)
			if from.Before(start) || until.After(end) || !from.Before(until) || w.From < last {
				t.Fatalf("%s unordered/unclipped %+v", date, w)
			}
			if w.Night != stars.NightName(stars.NightDay(from.Add(until.Sub(from)/2))) {
				t.Fatalf("night mismatch %+v", w)
			}
			last = w.From
		}
		if date == "2026-06-21" && (d.Nautical.Dawn != nil || d.Astronomical.Dawn != nil) {
			t.Fatal("invented summer twilight crossing")
		}
	}
	// USNO one-day moon table has no rise on this civil date at Edinburgh:
	// https://aa.usno.navy.mil/api/rstt/oneday?date=2026-07-07&coords=55.95,-3.19&tz=1
	d := lightDay(t, "2026-07-07", 55.95, -3.19)
	if d.Moon.Rise != nil {
		t.Fatal("invented moonrise", *d.Moon.Rise)
	}
}

func TestLightDatesIgnoreProcessTimezone(t *testing.T) {
	original := time.Local
	t.Cleanup(func() { time.Local = original })
	for _, date := range []string{"2026-03-29", "2026-10-25", "2026-10-19"} {
		time.Local = time.UTC
		want, err := json.Marshal(lightDay(t, date, 57.3, -3.7))
		if err != nil {
			t.Fatal(err)
		}
		for _, offset := range []int{-5 * 3600, 14 * 3600} {
			time.Local = time.FixedZone("process", offset)
			got, err := json.Marshal(lightDay(t, date, 57.3, -3.7))
			if err != nil {
				t.Fatal(err)
			}
			if string(got) != string(want) {
				t.Errorf("%s events or windows changed with process UTC offset %d", date, offset)
			}
		}
	}
}

func TestLightValidationAndAstronomyWithoutSnapshot(t *testing.T) {
	svc := fixture.Service(false)
	base := connector.LightParams{Lat: f(57.3), Lon: f(-3.7)}
	for _, mutate := range []func(*connector.LightParams){
		func(p *connector.LightParams) { p.Lat = nil }, func(p *connector.LightParams) { p.Lon = nil },
		func(p *connector.LightParams) { p.Lat = f(math.NaN()) }, func(p *connector.LightParams) { p.Lat = f(math.Inf(1)) },
		func(p *connector.LightParams) { p.Lon = f(math.Inf(-1)) }, func(p *connector.LightParams) { p.Lat = f(54) },
		func(p *connector.LightParams) { p.RadiusKm = f(math.NaN()) }, func(p *connector.LightParams) { p.RadiusKm = f(math.Inf(1)) },
		func(p *connector.LightParams) { p.RadiusKm = f(0) }, func(p *connector.LightParams) { p.RadiusKm = f(101) },
		func(p *connector.LightParams) { p.From = s("2026-10-18") }, func(p *connector.LightParams) { p.To = s("2026-10-26") },
		func(p *connector.LightParams) { p.From = s("2026-11-02") }, func(p *connector.LightParams) { p.To = s("bad") },
	} {
		p := base
		mutate(&p)
		if _, err := svc.Light(p); err == nil {
			t.Errorf("accepted %+v", p)
		}
	}
	env, err := svc.Light(base)
	if err != nil {
		t.Fatal(err)
	}
	r := env.Data.(connector.LightResult)
	if r.Site != nil || len(r.Days) != 3 || r.Days[0].Sunrise == nil || len(env.Meta.Sources) != 1 {
		t.Fatalf("astronomy depends on snapshot %+v", r)
	}
	for _, d := range r.Days {
		for _, w := range d.Windows {
			if w.Cloud.Verdict != "unknown" {
				t.Fatal(w)
			}
		}
	}
	base.From, base.To = s("2026-11-01"), s("2026-11-01")
	if _, err := svc.Light(base); err != nil {
		t.Fatal("13-day maximum rejected", err)
	}
}

func TestLightFixtureCloudAndProvenance(t *testing.T) {
	svc := fixture.Service(true)
	p := connector.LightParams{Lat: f(57.3), Lon: f(-3.7), To: s("2026-10-25")}
	env, err := svc.Light(p)
	if err != nil {
		t.Fatal(err)
	}
	r := env.Data.(connector.LightResult)
	if len(r.Days) != 7 || r.Site == nil || r.Site.ID != "scotland-0001" || r.Site.Truncated || env.Meta.DataVersion == "" || env.Meta.ForecastFetchedAt == nil || !strings.Contains(sourceNames(env.Meta), "MET Norway") {
		t.Fatal("fixture provenance", r.Site, env.Meta)
	}
	samples := 0
	for _, d := range r.Days {
		for _, w := range d.Windows {
			samples += w.Cloud.Samples
			if (w.Kind == "golden" || w.Kind == "blue" || w.Kind == "civil_twilight") && (w.Cloud.Verdict != "unknown" || w.Cloud.Samples != 0) {
				t.Fatal("daylight cloud invented", w)
			}
		}
	}
	if samples == 0 || len(r.Nights) == 0 {
		t.Fatal("fixture ignored supported samples or AssessNight")
	}
	for _, n := range r.Nights {
		if n.Night > "2026-10-21" && n.Verdict != "unknown" {
			t.Fatal("beyond forecast", n)
		}
	}
	for _, raw := range []string{strings.ReplaceAll(fixture.Sites, "2026-10-19T14:05:00", "2026-10-16T14:05:00"), strings.ReplaceAll(fixture.Sites, `"forecast_fetched_at":"2026-10-19T14:05:00Z"`, `"forecast_fetched_at":null`)} {
		ds, err := stars.Parse([]byte(raw), []byte(fixture.History), "v")
		if err != nil {
			t.Fatal(err)
		}
		svc.Data = fixture.Source{DS: ds}
		env, err = svc.Light(p)
		if err != nil {
			t.Fatal(err)
		}
		for _, d := range env.Data.(connector.LightResult).Days {
			for _, w := range d.Windows {
				if w.Cloud.Verdict != "unknown" || w.Cloud.Samples != 0 {
					t.Fatal("stale or missing forecast", w)
				}
			}
		}
	}
	svc = fixture.Service(true)
	p.Lat, p.Lon = f(55), f(-8)
	env, err = svc.Light(p)
	if err != nil || env.Data.(connector.LightResult).Site != nil {
		t.Fatal("reach cap", err)
	}
	b, err := json.Marshal(env)
	if err != nil || !strings.Contains(string(b), `"cloud":`) || !strings.Contains(string(b), `"sources":`) {
		t.Fatal("serialized response", err)
	}
}

func TestLightCoincidentSitesKeepNightIdentity(t *testing.T) {
	const a = `{"id":"a","lat":57.3,"lon":-3.7,"forecast_fetched_at":"2026-10-19T14:05:00Z","clear_dark_hours":0,"clear_twilight_hours":0,"best_hours":[]}`
	const z = `{"id":"z","lat":57.3,"lon":-3.7,"forecast_fetched_at":"2026-10-19T14:05:00Z","clear_dark_hours":1,"clear_twilight_hours":1,"best_hours":[{"time":"2026-10-19T21:00:00Z","cloud_area_fraction":0,"dark":true}]}`
	for _, rows := range []string{a + "," + z, z + "," + a} {
		ds, err := stars.Parse([]byte(`{"fetched_at":"2026-10-19T14:05:00Z","sites":[`+rows+`]}`), []byte(`{"sites":[]}`), "coincident")
		if err != nil {
			t.Fatal(err)
		}
		site, _ := ds.Nearest(57.3, -3.7, 30)
		ws := ds.Windows(stars.WindowsQuery{Lat: 57.3, Lon: -3.7, RadiusKm: 0.01, Night: stars.FirstNight(fixture.Now), Nights: 1, Limit: 1, Now: fixture.Now})
		if site == nil || site.ID != "a" || len(ws) != 1 || ws[0].Site.ID != "z" {
			t.Fatal("fixture must select a but rank z's night first", site, ws)
		}
		svc := fixture.Service(false)
		svc.Data = fixture.Source{DS: ds}
		env, err := svc.Light(fixture.LightParams())
		if err != nil {
			t.Fatal(err)
		}
		r := env.Data.(connector.LightResult)
		if r.Site == nil || r.Site.ID != "a" {
			t.Fatal("wrong selected site", r.Site)
		}
		for _, d := range r.Days {
			for _, w := range d.Windows {
				if w.Cloud.Samples != 0 || w.Cloud.Verdict != "unknown" {
					t.Fatal("cloud borrowed another site's samples", w)
				}
			}
		}
		found := false
		for _, n := range r.Nights {
			if n.Night != "2026-10-19" {
				continue
			}
			found = true
			if n.Verdict != "unknown" || n.Coverage != stars.CoverageNone || !strings.Contains(n.Why, "selected snapshot site") {
				t.Errorf("night borrowed another site's assessment: %+v", n)
			}
		}
		if !found {
			t.Fatal("missing coincident-site night")
		}
	}
}

func TestLightElapsedNightsAreUnknown(t *testing.T) {
	for _, tc := range []struct {
		name     string
		now      time.Time
		defaults bool
	}{
		{"fixture", fixture.Now, false},
		{"default dates", fixture.Now, true},
		{"before morning cutover", time.Date(2026, 10, 20, 7, 59, 0, 0, time.UTC), true},
		{"at morning cutover", time.Date(2026, 10, 20, 8, 0, 0, 0, time.UTC), true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			svc := fixture.Service(true)
			svc.Now = func() time.Time { return tc.now }
			p := fixture.LightParams()
			if tc.defaults {
				p.From, p.To = nil, nil
			}
			env, err := svc.Light(p)
			if err != nil {
				t.Fatal(err)
			}
			r := env.Data.(connector.LightResult)
			if r.Site == nil || len(r.Nights) == 0 {
				t.Fatal("fixture must exercise nearby-site night assessments", r)
			}
			ds := fixture.Dataset()
			site, _ := ds.Nearest(r.Query.Lat, r.Query.Lon, r.Query.RadiusKm)
			first := stars.FirstNight(tc.now)
			elapsed := 0
			for _, n := range r.Nights {
				if n.Night < stars.NightName(first) {
					elapsed++
					if n.Verdict != "unknown" || n.Coverage != stars.CoverageNone || !strings.Contains(n.Why, "ended") || strings.Contains(n.Why, "tonight") {
						t.Errorf("elapsed night must be unknown/none with an elapsed explanation: %+v", n)
					}
					continue
				}
				date, err := time.Parse(time.DateOnly, n.Night)
				if err != nil {
					t.Fatal(err)
				}
				night := stars.NightDay(date.Add(12 * time.Hour))
				ws := ds.Windows(stars.WindowsQuery{Lat: site.Lat, Lon: site.Lon, RadiusKm: 0.01, Night: night, Nights: 1, Limit: 1, Now: tc.now})
				if len(ws) != 1 || len(ws[0].Nights) != 1 {
					t.Fatal("missing fixture window", ws)
				}
				want := ds.AssessNight(site, &ws[0].Nights[0], night, tc.now.UTC().Truncate(time.Hour), ws[0].Climate, stars.NightAssessmentOptions{OmitMoonAdvice: true})
				if n.Verdict != want.Verdict || n.Coverage != want.Coverage || n.Why != want.Why {
					t.Errorf("current/future night assessment changed: got %+v, want %+v", n, want)
				}
			}
			wantElapsed := 1
			if tc.name == "before morning cutover" {
				wantElapsed = 0
			}
			if elapsed != wantElapsed {
				t.Errorf("elapsed nights = %d, want %d", elapsed, wantElapsed)
			}
		})
	}
}

func TestLightETagTracksExactExpiryAndQueries(t *testing.T) {
	svc := fixture.Service(true)
	ds := fixture.Dataset()
	p := fixture.LightParams()
	// Horizon expires at 14:05 inside an as-of hour; both calls must differ
	// even though the snapshot version, query and as-of hour are unchanged.
	now := time.Date(2026, 10, 21, 14, 4, 0, 0, time.UTC)
	svc.Now = func() time.Time { return now }
	svc.Data = fixture.Source{DS: ds}
	p.From, p.To = s("2026-10-21"), s("2026-10-21")
	a, err := svc.Light(p)
	if err != nil {
		t.Fatal(err)
	}
	now = now.Add(2 * time.Minute)
	b, err := svc.Light(p)
	if err != nil {
		t.Fatal(err)
	}
	if a.Meta.AsOf != b.Meta.AsOf || a.ETag == b.ETag {
		t.Fatal("expiry not in validator", a.ETag, b.ETag)
	}
	c, err := svc.Light(p)
	if err != nil || c.ETag != b.ETag {
		t.Fatal("unstable repeated answer", err)
	}
	p.Lon = f(-3.8)
	d, err := svc.Light(p)
	if err != nil || d.ETag == c.ETag {
		t.Fatal("query not in validator", err)
	}
}
