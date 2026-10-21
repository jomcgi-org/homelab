package astro

import (
	"math"
	"testing"
	"time"
)

// Python is projects/monolith/hikes/forecast.py sun_times on the same
// inputs; ephem is PyEphem with the -0:50 horizon.
var sunReference = []struct {
	place               string
	lat, lon            float64
	day                 string
	pyRise, pySet       string
	ephemRise, ephemSet string
}{
	{"fortwilliam", 56.82, -5.11, "2026-10-20", "07:04:17", "17:08:14", "07:01", "17:07"},
	{"fortwilliam", 56.82, -5.11, "2026-12-21", "08:56:56", "15:42:09", "08:53", "15:43"},
	{"fortwilliam", 56.82, -5.11, "2026-06-21", "03:28:08", "21:18:26", "03:24", "21:20"},
	{"fortwilliam", 56.82, -5.11, "2026-03-29", "05:59:47", "18:52:49", "05:56", "18:55"},
	{"lerwick", 60.15, -1.15, "2026-10-20", "06:56:59", "16:43:52", "06:54", "16:43"},
	{"lerwick", 60.15, -1.15, "2026-12-21", "09:09:06", "14:58:18", "09:04", "15:00"},
	{"lerwick", 60.15, -1.15, "2026-06-21", "02:39:42", "21:35:12", "02:34", "21:37"},
	{"lerwick", 60.15, -1.15, "2026-03-29", "05:40:31", "18:40:25", "05:36", "18:43"},
	{"dumfries", 55.07, -3.61, "2026-10-20", "06:54:23", "17:06:08", "06:51", "17:05"},
	{"dumfries", 55.07, -3.61, "2026-12-21", "08:39:02", "15:48:03", "08:35", "15:49"},
	{"dumfries", 55.07, -3.61, "2026-06-21", "03:35:35", "20:58:59", "03:31", "21:00"},
	{"dumfries", 55.07, -3.61, "2026-03-29", "05:55:23", "18:45:14", "05:52", "18:47"},
}

func clock(t *testing.T, day, hms, layout string) time.Time {
	t.Helper()
	at, err := time.Parse("2006-01-02 "+layout, day+" "+hms)
	if err != nil {
		t.Fatal(err)
	}
	return at
}

func TestSunTimesMatchHikesAndEphem(t *testing.T) {
	for _, r := range sunReference {
		day := clock(t, r.day, "00:00", "15:04")
		rise, set, ok := SunTimes(day, r.lat, r.lon)
		if !ok {
			t.Fatalf("%s %s: no sunrise", r.place, r.day)
		}
		for _, c := range []struct {
			name      string
			got, want time.Time
			tol       time.Duration
		}{
			{"rise vs hikes", rise, clock(t, r.day, r.pyRise, "15:04:05"), 2 * time.Second},
			{"set vs hikes", set, clock(t, r.day, r.pySet, "15:04:05"), 2 * time.Second},
			{"rise vs ephem", rise, clock(t, r.day, r.ephemRise, "15:04"), 6 * time.Minute},
			{"set vs ephem", set, clock(t, r.day, r.ephemSet, "15:04"), 6 * time.Minute},
		} {
			if d := c.got.Sub(c.want); math.Abs(float64(d)) > float64(c.tol) {
				t.Errorf("%s %s %s: %s, want %s", r.place, r.day, c.name, c.got.Format("15:04:05"), c.want.Format("15:04:05"))
			}
		}
	}
}
