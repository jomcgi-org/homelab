package astro

import (
	"math"
	"testing"
	"time"
)

// Reference values from PyEphem 4.x (topocentric, no refraction). Over 300
// random Scottish times and places the worst error was 0.52 deg in altitude
// and 0.003 in illumination.
var reference = []struct {
	place    string
	lat, lon float64
	at       string
	alt      float64
	lit      float64
}{
	{"aviemore", 57.19, -3.83, "2026-10-20T22:00:00Z", 13.04, 0.712},
	{"aviemore", 57.19, -3.83, "2026-10-21T03:00:00Z", -19.37, 0.730},
	{"aviemore", 57.19, -3.83, "2026-10-26T00:00:00Z", 47.79, 0.998},
	{"aviemore", 57.19, -3.83, "2024-01-25T18:00:00Z", 14.52, 0.998},
	{"aviemore", 57.19, -3.83, "2024-01-11T12:00:00Z", 4.86, 0.002},
	{"aviemore", 57.19, -3.83, "2026-06-21T23:30:00Z", 2.81, 0.510},
	{"aviemore", 57.19, -3.83, "2026-03-03T19:15:00Z", 7.44, 0.999},
	{"lerwick", 60.15, -1.15, "2026-10-20T22:00:00Z", 9.78, 0.712},
	{"lerwick", 60.15, -1.15, "2026-10-21T03:00:00Z", -20.47, 0.730},
	{"lerwick", 60.15, -1.15, "2026-10-26T00:00:00Z", 44.63, 0.998},
	{"lerwick", 60.15, -1.15, "2024-01-25T18:00:00Z", 16.78, 0.998},
	{"lerwick", 60.15, -1.15, "2024-01-11T12:00:00Z", 2.02, 0.002},
	{"lerwick", 60.15, -1.15, "2026-06-21T23:30:00Z", 0.92, 0.510},
	{"lerwick", 60.15, -1.15, "2026-03-03T19:15:00Z", 8.51, 0.999},
	{"stornoway", 58.21, -6.39, "2026-10-20T22:00:00Z", 12.82, 0.712},
	{"stornoway", 58.21, -6.39, "2026-10-21T03:00:00Z", -17.96, 0.730},
	{"stornoway", 58.21, -6.39, "2026-10-26T00:00:00Z", 46.85, 0.998},
	{"stornoway", 58.21, -6.39, "2024-01-25T18:00:00Z", 13.60, 0.998},
	{"stornoway", 58.21, -6.39, "2024-01-11T12:00:00Z", 3.67, 0.002},
	{"stornoway", 58.21, -6.39, "2026-06-21T23:30:00Z", 3.93, 0.510},
	{"stornoway", 58.21, -6.39, "2026-03-03T19:15:00Z", 6.01, 0.999},
}

func TestMoonMatchesEphem(t *testing.T) {
	for _, r := range reference {
		at, err := time.Parse(time.RFC3339, r.at)
		if err != nil {
			t.Fatal(err)
		}
		if got := MoonAltitude(at, r.lat, r.lon); math.Abs(got-r.alt) > 0.6 {
			t.Errorf("%s %s altitude %.2f, ephem %.2f", r.place, r.at, got, r.alt)
		}
		if got := MoonIllumination(at); math.Abs(got-r.lit) > 0.01 {
			t.Errorf("%s illumination %.3f, ephem %.3f", r.at, got, r.lit)
		}
	}
}
