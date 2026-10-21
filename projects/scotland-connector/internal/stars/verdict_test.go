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
	hour := Hour{Unix: time.Date(2026, 10, 20, 20, 0, 0, 0, time.UTC).Unix()}
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
		a := ds.AssessNight(&Site{}, &c.n, c.night, &Climate{Month: 10, ClearDark: 30, Dark: 120})
		if a.Verdict != c.want || !strings.Contains(a.Why, c.why) {
			t.Errorf("%s: %s %q", c.name, a.Verdict, a.Why)
		}
	}
	if a := ds.AssessNight(&Site{}, &NightWindow{}, night+3, &Climate{Month: 10, ClearDark: 30, Dark: 120}); !strings.Contains(a.Why, "In October about 25% of dark hours here are clear") {
		t.Errorf("beyond the forecast quotes the climate: %q", a.Why)
	}
}

func TestHourlyUntilFromSnapshot(t *testing.T) {
	ds, err := Parse([]byte(`{"fetched_at":"2026-10-20T14:00:00+00:00","sites":[{"id":"a","lat":57,"lon":-4,"best_hours":[
	  {"time":"2026-10-23T02:00:00+00:00","cloud_area_fraction":1,"dark":true}]}]}`), []byte(`{"sites":[]}`), "v")
	if err != nil {
		t.Fatal(err)
	}
	// A listed hour past fetch plus 48 h extends the horizon to its end.
	if want := time.Date(2026, 10, 23, 3, 0, 0, 0, time.UTC); !ds.HourlyUntil.Equal(want) {
		t.Fatalf("horizon %v", ds.HourlyUntil)
	}
}

// A site whose hour list the snapshot capped cannot say a later night is
// cloudy: its hours there were dropped, not forecast cloudy.
func TestTruncatedSiteIsUnknownPastItsList(t *testing.T) {
	ds := &Dataset{HourlyUntil: time.Date(2026, 10, 22, 20, 0, 0, 0, time.UTC)}
	night := NightDay(time.Date(2026, 10, 20, 22, 0, 0, 0, time.UTC))
	last := Hour{Unix: time.Date(2026, 10, 20, 23, 0, 0, 0, time.UTC).Unix()}
	site := &Site{ClearTwilight: 40, Hours: []Hour{last}}
	if a := ds.AssessNight(site, &NightWindow{}, night+1, nil); a.Verdict != verdict.Unknown || !strings.Contains(a.Why, "only up to Wed 01:00") {
		t.Fatalf("after the list: %s %q", a.Verdict, a.Why)
	}
	// A go stands: the dropped hours could only add to it.
	if a := ds.AssessNight(site, &NightWindow{ClearDark: 4, ClearTwilight: 4, MoonlessDark: 4}, night, nil); a.Verdict != verdict.Go {
		t.Fatalf("go on a capped site: %s %q", a.Verdict, a.Why)
	}
	// A whole list is believed.
	if a := ds.AssessNight(&Site{ClearTwilight: 1, Hours: []Hour{last}}, &NightWindow{}, night+1, nil); a.Verdict != verdict.No {
		t.Fatalf("whole list: %s %q", a.Verdict, a.Why)
	}
}
