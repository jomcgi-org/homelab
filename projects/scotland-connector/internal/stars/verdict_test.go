package stars

import (
	"strings"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/verdict"
)

func TestNightVerdicts(t *testing.T) {
	// The horizon is 20:00Z (21:00 BST) on the 22nd: the nights of the 20th
	// and 21st are full, the 22nd partial and the 23rd beyond.
	ds := &Dataset{HourlyUntil: time.Date(2026, 10, 22, 20, 0, 0, 0, time.UTC)}
	night := NightDay(time.Date(2026, 10, 20, 22, 0, 0, 0, time.UTC))
	hour := Hour{Unix: time.Date(2026, 10, 20, 20, 0, 0, 0, time.UTC).Unix(), Dark: true}
	early := time.Date(2026, 10, 20, 12, 0, 0, 0, time.UTC)
	for _, c := range []struct {
		name  string
		n     NightWindow
		night int32
		want  verdict.Verdict
		why   string
	}{
		{"moonless", NightWindow{ClearDark: 4, ClearTwilight: 4, MoonlessDark: 4, MoonLit: 0.7, Hours: []Hour{hour}}, night, verdict.Go, "4 clear dark hours from 21:00, the moon below the horizon"},
		{"full moon", NightWindow{ClearDark: 5, ClearTwilight: 5, MoonLit: 0.95, Hours: []Hour{hour}}, night, verdict.Marginal, "5 clear dark hours from 21:00, but under a 95% moon"},
		{"short", NightWindow{ClearDark: 1, ClearTwilight: 1, MoonlessDark: 1, Hours: []Hour{hour}}, night, verdict.Marginal, "Only 1 clear dark hour from"},
		{"twilight", NightWindow{ClearTwilight: 3, Hours: []Hour{hour}}, night, verdict.Marginal, "only twilight-dark"},
		{"cloudy", NightWindow{}, night, verdict.No, "Cloudy all night"},
		{"partial", NightWindow{}, night + 2, verdict.Unknown, "reaches only Thu 21:00"},
		{"beyond", NightWindow{}, night + 3, verdict.Unknown, "Beyond the hourly forecast"},
	} {
		a := ds.AssessNight(&Site{ForecastFetchedAt: ds.HourlyUntil.Add(-48 * time.Hour)}, &c.n, c.night, early, &Climate{Month: 10, ClearDark: 30, Dark: 120})
		if a.Verdict != c.want || !strings.Contains(a.Why, c.why) {
			t.Errorf("%s: %s %q", c.name, a.Verdict, a.Why)
		}
	}
	if a := ds.AssessNight(&Site{ForecastFetchedAt: ds.HourlyUntil.Add(-48 * time.Hour)}, &NightWindow{}, night+3, early, &Climate{Month: 10, ClearDark: 30, Dark: 120}); !strings.Contains(a.Why, "In October about 25% of dark hours here are clear") {
		t.Errorf("beyond the forecast quotes the climate: %q", a.Why)
	}
}

func TestHourlyUntilFromSnapshot(t *testing.T) {
	ds, err := Parse([]byte(`{"fetched_at":"2026-10-20T14:00:00+00:00","sites":[{"forecast_fetched_at":"2026-10-20T14:00:00Z","id":"a","lat":57,"lon":-4,"best_hours":[
	  {"time":"2026-10-23T02:00:00+00:00","cloud_area_fraction":1,"dark":true}]}]}`), []byte(`{"sites":[]}`), "v")
	if err != nil {
		t.Fatal(err)
	}
	// The stars job keeps MET Norway's 6-hourly tail, so a listed hour past
	// fetch plus 48 h is not hourly forecast and leaves the horizon alone.
	if want := time.Date(2026, 10, 22, 14, 0, 0, 0, time.UTC); !ds.HourlyUntil.Equal(want) {
		t.Fatalf("horizon %v", ds.HourlyUntil)
	}
}

// A site whose hour list the snapshot capped cannot say a later night is
// cloudy: its hours there were dropped, not forecast cloudy.
func TestTruncatedSiteIsUnknownPastItsList(t *testing.T) {
	ds := &Dataset{HourlyUntil: time.Date(2026, 10, 22, 20, 0, 0, 0, time.UTC)}
	night := NightDay(time.Date(2026, 10, 20, 22, 0, 0, 0, time.UTC))
	last := Hour{Unix: time.Date(2026, 10, 20, 23, 0, 0, 0, time.UTC).Unix()}
	early := time.Date(2026, 10, 20, 12, 0, 0, 0, time.UTC)
	site := &Site{ForecastFetchedAt: ds.HourlyUntil.Add(-48 * time.Hour), ClearTwilight: 40, Hours: []Hour{last}}
	if a := ds.AssessNight(site, &NightWindow{}, night+1, early, nil); a.Verdict != verdict.Unknown || !strings.Contains(a.Why, "only up to Wed 01:00") {
		t.Fatalf("after the list: %s %q", a.Verdict, a.Why)
	}
	// A go stands: the dropped hours could only add to it.
	if a := ds.AssessNight(site, &NightWindow{ClearDark: 4, ClearTwilight: 4, MoonlessDark: 4}, night, early, nil); a.Verdict != verdict.Go {
		t.Fatalf("go on a capped site: %s %q", a.Verdict, a.Why)
	}
	// A whole list is believed.
	if a := ds.AssessNight(&Site{ForecastFetchedAt: ds.HourlyUntil.Add(-48 * time.Hour), ClearTwilight: 1, Hours: []Hour{last}}, &NightWindow{}, night+1, early, nil); a.Verdict != verdict.No {
		t.Fatalf("whole list: %s %q", a.Verdict, a.Why)
	}
}

// The why and the clear window name the dark hours, not the twilight
// before them, and a night in progress does not claim "all night".
func TestNightWording(t *testing.T) {
	ds := &Dataset{HourlyUntil: time.Date(2026, 10, 22, 20, 0, 0, 0, time.UTC)}
	night := NightDay(time.Date(2026, 10, 20, 22, 0, 0, 0, time.UTC))
	twilight := Hour{Unix: time.Date(2026, 10, 20, 18, 0, 0, 0, time.UTC).Unix()}
	dark := Hour{Unix: time.Date(2026, 10, 20, 22, 0, 0, 0, time.UTC).Unix(), Dark: true}
	n := NightWindow{ClearDark: 1, ClearTwilight: 2, Hours: []Hour{twilight, dark}}
	a := ds.AssessNight(&Site{ForecastFetchedAt: ds.HourlyUntil.Add(-48 * time.Hour)}, &n, night, time.Date(2026, 10, 20, 12, 0, 0, 0, time.UTC), nil)
	if !strings.Contains(a.Why, "from 23:00") {
		t.Errorf("why %q names the twilight hour", a.Why)
	}
	from, _ := n.ClearFrom()
	until, _ := n.ClearUntil()
	if !from.Equal(time.Unix(dark.Unix, 0)) || !until.Equal(time.Unix(dark.Unix+3600, 0)) {
		t.Errorf("clear window %v to %v", from, until)
	}
	tw := NightWindow{ClearTwilight: 1, Hours: []Hour{twilight}}
	if from, ok := tw.ClearFrom(); !ok || !from.Equal(time.Unix(twilight.Unix, 0)) {
		t.Errorf("twilight-only clear_from %v", from)
	}
	late := time.Date(2026, 10, 20, 23, 30, 0, 0, time.UTC)
	if a := ds.AssessNight(&Site{ForecastFetchedAt: ds.HourlyUntil.Add(-48 * time.Hour)}, &NightWindow{}, night, late, nil); a.Verdict != verdict.No || !strings.HasPrefix(a.Why, "No clear hour left tonight.") {
		t.Errorf("night in progress: %s %q", a.Verdict, a.Why)
	}
}

func TestCappedNear(t *testing.T) {
	ds, err := Parse([]byte(`{"fetched_at":"2026-10-20T14:00:00+00:00","sites":[
	  {"forecast_fetched_at":"2026-10-20T14:00:00Z","id":"capped","lat":57.0,"lon":-4.0,"clear_dark_hours":30,"clear_twilight_hours":30,"best_hours":[{"time":"2026-10-20T23:00:00+00:00","cloud_area_fraction":1,"dark":true}]},
	  {"forecast_fetched_at":"2026-10-20T14:00:00Z","id":"whole","lat":57.01,"lon":-4.0,"clear_dark_hours":1,"clear_twilight_hours":1,"best_hours":[{"time":"2026-10-20T23:00:00+00:00","cloud_area_fraction":1,"dark":true}]}]}`), []byte(`{"sites":[]}`), "v")
	if err != nil {
		t.Fatal(err)
	}
	night := NightDay(time.Date(2026, 10, 20, 22, 0, 0, 0, time.UTC))
	if s := ds.CappedNear(57.01, -4.0, 5, night+1); s == nil || s.ID != "capped" {
		t.Fatalf("capped near = %+v", s)
	}
	// The night its list covers is counted in full.
	if s := ds.CappedNear(57.01, -4.0, 5, night-1); s != nil {
		t.Fatalf("an earlier night = %+v", s.ID)
	}
	if s := ds.CappedNear(58, -4.0, 5, night+1); s != nil {
		t.Fatalf("out of range = %+v", s.ID)
	}
}

// Synthetic cloudy nights cover winter, both DST changes and summer twilight.
func TestCloudyMoonGeometryIndependentOfForecast(t *testing.T) {
	for _, date := range []string{"2026-01-03", "2026-03-28", "2026-10-24", "2026-06-29"} {
		now, err := time.Parse(time.DateOnly, date)
		if err != nil {
			t.Fatal(err)
		}
		night := NightDay(now.Add(18 * time.Hour))
		ds := &Dataset{HourlyUntil: now.Add(72 * time.Hour)}
		site := &Site{ForecastFetchedAt: now, Lat: 57, Lon: -4}
		a := ds.AssessNight(site, &NightWindow{}, night, now, nil)
		if a.Verdict != verdict.No || !strings.Contains(a.Why, "Moon illumination") || !strings.Contains(a.Why, "The moon is up") || strings.Contains(a.Why, "the moon below the horizon") {
			t.Fatalf("%s: %+v", date, a)
		}
		if date == "2026-10-24" && (!strings.Contains(a.Why, "BST") || !strings.Contains(a.Why, "GMT")) {
			t.Fatal(a.Why)
		}
		if date == "2026-01-03" && !strings.Contains(a.Why, "lunar and planetary") {
			t.Fatal(a.Why)
		}
		ds.HourlyUntil = now.Add(24 * time.Hour)
		a = ds.AssessNight(site, &NightWindow{}, night, now, nil)
		if a.Verdict != verdict.Unknown || !strings.Contains(a.Why, "Moon illumination") {
			t.Fatalf("partial: %+v", a)
		}
		ds.HourlyUntil = now
		if a = ds.AssessNight(site, &NightWindow{}, night, now, nil); a.Verdict != verdict.Unknown {
			t.Fatal(a)
		}
	}
}

func TestMoonDurationWording(t *testing.T) {
	for _, tc := range []struct {
		up, total time.Duration
		want      string
	}{
		{0, 14 * time.Hour, "below the horizon"},
		{14 * time.Hour, 14 * time.Hour, "up all night"},
		{800 * time.Minute, 860 * time.Minute, "up almost all night"},
		{6 * time.Hour, 14 * time.Hour, "up for part of the night"},
	} {
		got := moonDuration(tc.up, tc.total, "night")
		if !strings.Contains(got, tc.want) || strings.Contains(got, "samples") || len(got) > 100 {
			t.Fatal(got)
		}
	}
	now := time.Date(2026, 10, 24, 12, 0, 0, 0, time.UTC)
	night := NightDay(now.Add(6 * time.Hour))
	advice := MoonAdvice(57, -4, night, now)
	if !strings.Contains(advice, "almost all night") || strings.Contains(advice, "samples") || len(advice) > 350 {
		t.Fatal(advice)
	}
	late := now.Add(12 * time.Hour)
	if got := MoonAdvice(57, -4, night, late); !strings.Contains(got, "remaining night") {
		t.Fatal(got)
	}
}
