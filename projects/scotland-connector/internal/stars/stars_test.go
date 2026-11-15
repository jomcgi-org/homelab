package stars

import (
	"fmt"
	"math"
	"strings"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/verdict"
)

func TestNearestSiteTieAndReach(t *testing.T) {
	for _, rows := range []string{
		`{"id":"z","lat":57,"lon":-4},{"id":"a","lat":57,"lon":-4}`,
		`{"id":"a","lat":57,"lon":-4},{"id":"z","lat":57,"lon":-4}`,
	} {
		ds, err := Parse([]byte(`{"sites":[`+rows+`]}`), []byte(`{"sites":[]}`), "v")
		if err != nil {
			t.Fatal(err)
		}
		got, km := ds.Nearest(57, -4, 30)
		if got == nil || got.ID != "a" || km != 0 {
			t.Fatal("unstable tie", got, km)
		}
		if got, _ := ds.Nearest(55, -8, 30); got != nil {
			t.Fatal("outside reach", got)
		}
	}
}

func TestEntityFreshnessFromSnapshot(t *testing.T) {
	for _, date := range []string{"2026-03-28", "2026-10-24"} {
		now, _ := time.Parse(time.DateOnly, date)
		row := func(id, field, hours string) string {
			return fmt.Sprintf(`{"id":%q,"name":null,"lat":57,"lon":-4,"altitude_m":100,"lp_zone":"rural","clear_dark_hours":0,"clear_twilight_hours":0,%s"best_hours":[%s]}`, id, field, hours)
		}
		stamp := func(at time.Time) string { return fmt.Sprintf(`"forecast_fetched_at":%q,`, at.Format(time.RFC3339)) }
		body := fmt.Sprintf(`{"fetched_at":%q,"count":4,"total_sites":4,"darkness":"none","sites":[%s]}`, now.Format(time.RFC3339), strings.Join([]string{
			row("fresh", stamp(now), ""), row("stale", stamp(now.Add(-24*time.Hour)), ""),
			row("legacy", "", ""), row("null", `"forecast_fetched_at":null,`, ""),
		}, ","))
		ds, err := Parse([]byte(body), []byte(`{"sites":[]}`), "v")
		if err != nil {
			t.Fatal(err)
		}
		night := NightDay(now.Add(18*time.Hour)) + 1
		for i, want := range []verdict.Verdict{verdict.Unknown, verdict.Unknown, verdict.Unknown, verdict.Unknown} {
			// Both DST nights have a partially known fresh forecast, while
			// the stale and absent entities have no coverage at all.
			a := ds.AssessNight(&ds.Sites[i], &NightWindow{}, night, now, nil)
			if a.Verdict != want || strings.Contains(a.Why, "0001") {
				t.Fatalf("%s %s: %+v", date, ds.Sites[i].ID, a)
			}
			if i == 0 && a.Coverage != CoveragePartial || i > 0 && a.Coverage != CoverageNone {
				t.Fatal(a)
			}
		}
		fresh := &ds.Sites[0]
		if a := ds.AssessNight(fresh, &NightWindow{}, night-1, now, nil); a.Verdict != verdict.No {
			t.Fatal(a)
		}
		if !fresh.Horizon(ds.HourlyUntil).Equal(now.Add(48 * time.Hour)) {
			t.Fatal("48h cap changed across DST")
		}
		end := localHour(night, nightEndHour)
		for _, tc := range []struct {
			until time.Time
			want  Coverage
		}{
			{end, CoverageFull},
			{end.Add(-time.Nanosecond), CoveragePartial},
			{localHour(night-1, nightStartHour), CoverageNone},
		} {
			ds.HourlyUntil = tc.until
			if got := ds.AssessNight(fresh, &NightWindow{}, night-1, now, nil).Coverage; got != tc.want {
				t.Fatalf("%s %v: %s want %s", date, tc.until, got, tc.want)
			}
		}
		bad := strings.Replace(body, stamp(now), `"forecast_fetched_at":"yesterday",`, 1)
		if _, err := Parse([]byte(bad), []byte(`{"sites":[]}`), "bad"); err == nil {
			t.Fatal("malformed entity timestamp accepted")
		}
	}
}

func TestLateHoursCannotDefeatEntityCap(t *testing.T) {
	body := `{"fetched_at":"2026-10-24T00:00:00Z","sites":[{"id":"old","lat":57,"lon":-4,"forecast_fetched_at":"2026-10-23T00:00:00Z","clear_dark_hours":4,"clear_twilight_hours":4,"best_hours":[
	{"time":"2026-10-24T23:00:00Z","cloud_area_fraction":0,"dark":true},
	{"time":"2026-10-25T00:00:00Z","cloud_area_fraction":0,"dark":true},
	{"time":"2026-10-25T01:00:00Z","cloud_area_fraction":0,"dark":true},
	{"time":"2026-10-25T02:00:00Z","cloud_area_fraction":0,"dark":true}]}]}`
	ds, err := Parse([]byte(body), []byte(`{"sites":[]}`), "v")
	if err != nil {
		t.Fatal(err)
	}
	now := time.Date(2026, 10, 24, 12, 0, 0, 0, time.UTC)
	w := ds.Windows(WindowsQuery{Lat: 57, Lon: -4, RadiusKm: 5, Nights: 1, Limit: 1, Now: now})[0]
	if w.ClearDark != 1 || w.Nights[0].ClearDark != 1 {
		t.Fatal(w)
	}
	a := ds.AssessNight(w.Site, &w.Nights[0], NightDay(now.Add(6*time.Hour)), now, nil)
	if a.Verdict != verdict.Unknown {
		t.Fatal(a)
	}
}

func history(ids ...string) string {
	var parts []string
	for _, id := range ids {
		parts = append(parts, fmt.Sprintf(`{"id":%q,"name":null,"lat":0,"lon":0,"clear":[1,2,3,4,5,6,7,8,9,40,11,12],"dark":[10,20,30,40,50,60,70,80,90,100,110,120]}`, id))
	}
	return `{"sites":[` + strings.Join(parts, ",") + `]}`
}

const fixtureSites = `{
 "count": 3, "total_sites": 3, "darkness": "astronomical", "fetched_at": "2026-10-19T11:05:00+00:00",
 "sites": [
  {"forecast_fetched_at":"2026-10-19T11:05:00Z","id":"near-cloudy","name":null,"lat":57.20,"lon":-3.80,"altitude_m":300,"lp_zone":"rural",
   "clear_dark_hours":1,"clear_twilight_hours":1,
   "best_hours":[{"time":"2026-10-19T22:00:00+00:00","cloud_area_fraction":5,"dark":true}]},
  {"forecast_fetched_at":"2026-10-19T11:05:00Z","id":"far-clear","name":null,"lat":57.60,"lon":-3.80,"altitude_m":120,"lp_zone":"pristine",
   "clear_dark_hours":3,"clear_twilight_hours":3,
   "best_hours":[
     {"time":"2026-10-20T23:00:00+00:00","cloud_area_fraction":1,"dark":true},
     {"time":"2026-10-19T21:00:00+00:00","cloud_area_fraction":2,"dark":true},
     {"time":"2026-10-20T01:00:00+00:00","cloud_area_fraction":3,"dark":true}]},
  {"forecast_fetched_at":"2026-10-19T11:05:00Z","id":"out-of-range","name":null,"lat":60.15,"lon":-1.15,"altitude_m":50,"lp_zone":"pristine",
   "clear_dark_hours":30,"clear_twilight_hours":30,"best_hours":[]}
 ]}`

func load(t *testing.T) *Dataset {
	t.Helper()
	ds, err := Parse([]byte(fixtureSites), []byte(history("near-cloudy", "far-clear")), "v1")
	if err != nil {
		t.Fatal(err)
	}
	return ds
}

func TestParse(t *testing.T) {
	ds := load(t)
	if len(ds.Sites) != 3 || ds.Darkness != "astronomical" {
		t.Fatalf("parsed %d sites, darkness %q", len(ds.Sites), ds.Darkness)
	}
	if want := time.Date(2026, 10, 19, 11, 5, 0, 0, time.UTC); !ds.FetchedAt.Equal(want) {
		t.Fatalf("FetchedAt = %v", ds.FetchedAt)
	}
	far := ds.Sites[1]
	if far.Hours[0].Unix > far.Hours[1].Unix || !far.HasClimate || far.ClimateClear[9] != 40 {
		t.Fatalf("far-clear parsed wrong: %+v", far)
	}
	if ds.Sites[2].HasClimate {
		t.Fatal("site without history has climate")
	}
	if !ds.Sites[2].Truncated() || ds.Sites[0].Truncated() {
		t.Fatal("truncation flag wrong")
	}
}

func TestParseRejectsBrokenInput(t *testing.T) {
	cases := map[string][2]string{
		"bad json":       {`{`, history()},
		"duplicate id":   {`{"sites":[{"forecast_fetched_at":"2026-10-19T11:05:00Z","id":"a","lat":1,"lon":1},{"forecast_fetched_at":"2026-10-19T11:05:00Z","id":"a","lat":1,"lon":1}]}`, history()},
		"empty id":       {`{"sites":[{"forecast_fetched_at":"2026-10-19T11:05:00Z","id":"","lat":1,"lon":1}]}`, history()},
		"bad coordinate": {`{"sites":[{"forecast_fetched_at":"2026-10-19T11:05:00Z","id":"a","lat":91,"lon":1}]}`, history()},
		"bad hour":       {`{"sites":[{"forecast_fetched_at":"2026-10-19T11:05:00Z","id":"a","lat":1,"lon":1,"best_hours":[{"time":"soon"}]}]}`, history()},
		"short history":  {`{"sites":[]}`, `{"sites":[{"forecast_fetched_at":"2026-10-19T11:05:00Z","id":"a","clear":[1],"dark":[1]}]}`},
	}
	for name, c := range cases {
		if _, err := Parse([]byte(c[0]), []byte(c[1]), "v"); err == nil {
			t.Errorf("%s: accepted", name)
		}
	}
}

func TestNightDayUsesLocalEvening(t *testing.T) {
	cases := map[string]string{
		"2026-10-19T21:00:00Z": "2026-10-19",
		"2026-10-20T01:00:00Z": "2026-10-19", // 02:00 BST still belongs to the 19th
		"2026-10-20T10:30:00Z": "2026-10-19", // 11:30 BST, before local noon
		"2026-10-20T11:30:00Z": "2026-10-20", // 12:30 BST
		"2026-12-20T11:30:00Z": "2026-12-19", // GMT
	}
	for in, want := range cases {
		ts, _ := time.Parse(time.RFC3339, in)
		if got := NightName(NightDay(ts)); got != want {
			t.Errorf("night of %s = %s, want %s", in, got, want)
		}
	}
}

func TestWindowsRanksAndBuckets(t *testing.T) {
	ds := load(t)
	q := WindowsQuery{
		Lat: 57.19, Lon: -3.83, RadiusKm: 60, Nights: 2, Limit: 5,
		Now: time.Date(2026, 10, 19, 15, 20, 0, 0, time.UTC),
	}
	got := ds.Windows(q)
	if len(got) != 2 {
		t.Fatalf("got %d windows, want 2 (Shetland is out of range)", len(got))
	}
	if got[0].Site.ID != "far-clear" || got[0].ClearDark != 3 {
		t.Fatalf("first = %s with %d clear-dark", got[0].Site.ID, got[0].ClearDark)
	}
	n := got[0].Nights
	if len(n) != 2 || n[0].Night != "2026-10-19" || n[0].ClearDark != 2 || n[1].ClearDark != 1 {
		t.Fatalf("nights = %+v", n)
	}
	if c := got[0].Climate; c == nil || c.Month != 10 || c.ClearDark != 40 || c.Dark != 100 {
		t.Fatalf("climate = %+v", c)
	}

	// One night only: the 20th's evening hour drops out of the count.
	q.Nights = 1
	if got := ds.Windows(q); got[0].ClearDark != 2 {
		t.Fatalf("one-night clear-dark = %d", got[0].ClearDark)
	}

	// Elapsed hours are dropped at the top of the current hour.
	q.Nights, q.Now = 2, time.Date(2026, 10, 20, 0, 59, 0, 0, time.UTC)
	if got := ds.Windows(q); got[0].Site.ID != "far-clear" || got[0].ClearDark != 2 {
		t.Fatalf("after midnight: %s %d", got[0].Site.ID, got[0].ClearDark)
	}

	q.Limit = 1
	if got := ds.Windows(q); len(got) != 1 {
		t.Fatalf("limit ignored: %d", len(got))
	}
}

// From the morning cutover, "tonight" is the coming evening, not the night
// that just ended.
func TestFirstNightRollsOverInTheMorning(t *testing.T) {
	cases := map[string]string{
		"2026-11-10T02:00:00Z": "2026-11-09", // still the night in progress
		"2026-11-10T08:59:00Z": "2026-11-09",
		"2026-11-10T09:00:00Z": "2026-11-10", // GMT: cutover at 09:00 local
		"2026-11-10T15:00:00Z": "2026-11-10",
		"2026-07-10T07:59:00Z": "2026-07-09", // BST: 08:59 local
		"2026-07-10T08:00:00Z": "2026-07-10",
	}
	for in, want := range cases {
		ts, _ := time.Parse(time.RFC3339, in)
		if got := NightName(FirstNight(ts)); got != want {
			t.Errorf("FirstNight(%s) = %s, want %s", in, got, want)
		}
	}

	ds, err := Parse([]byte(`{"fetched_at":"2026-11-10T09:00:00Z","sites":[{"forecast_fetched_at":"2026-11-10T09:00:00Z","id":"a","lat":57.2,"lon":-3.8,"clear_dark_hours":2,"clear_twilight_hours":2,"best_hours":[
		{"time":"2026-11-10T19:00:00Z","cloud_area_fraction":1,"dark":true},
		{"time":"2026-11-10T22:00:00Z","cloud_area_fraction":1,"dark":true}]}]}`), []byte(`{"sites":[]}`), "v")
	if err != nil {
		t.Fatal(err)
	}
	got := ds.Windows(WindowsQuery{Lat: 57.2, Lon: -3.8, RadiusKm: 10, Nights: 1, Limit: 5, Now: time.Date(2026, 11, 10, 9, 0, 0, 0, time.UTC)})
	if len(got) != 1 || got[0].ClearDark != 2 || got[0].Nights[0].Night != "2026-11-10" {
		t.Fatalf("morning query for tonight = %+v", got)
	}
}

func TestWithinMatchesBruteForce(t *testing.T) {
	var b strings.Builder
	b.WriteString(`{"sites":[`)
	n := 0
	for lat := 54.6; lat <= 60.8; lat += 0.05 {
		for lon := -7.6; lon <= -0.8; lon += 0.05 {
			if n > 0 {
				b.WriteByte(',')
			}
			fmt.Fprintf(&b, `{"forecast_fetched_at":"2026-10-19T11:05:00Z","id":"s%d","lat":%.4f,"lon":%.4f}`, n, lat, lon)
			n++
		}
	}
	b.WriteString(`]}`)
	ds, err := Parse([]byte(b.String()), []byte(`{"sites":[]}`), "v")
	if err != nil {
		t.Fatal(err)
	}
	for _, p := range [][3]float64{{57.19, -3.83, 150}, {60.15, -1.15, 80}, {54.9, -4.4, 25}, {58.2, -6.39, 1}} {
		cosQ := math.Cos(p[0] * math.Pi / 180)
		got := len(ds.Within(p[0], p[1], p[2]))
		want := 0
		for _, s := range ds.Sites {
			f := FastDistanceKm(p[0], p[1], cosQ, s.Lat, s.Lon, s.cosLat)
			if f <= p[2] {
				want++
			}
			if d := DistanceKm(p[0], p[1], s.Lat, s.Lon); d < 160 && math.Abs(d-f) > 0.05 {
				t.Fatalf("fast distance off by %.3f km at %.1f km", d-f, d)
			}
		}
		if got != want {
			t.Errorf("Within(%v) = %d, brute force %d", p, got, want)
		}
	}
}

// A site just inside a small radius, due east across a cell edge, must not be
// lost to the grid's bounding box.
func TestWithinKeepsEdgeSitesAtSmallRadius(t *testing.T) {
	cos := math.Cos(57 * math.Pi / 180)
	east := 0.9995 / (kmPerDegLat * cos) // 0.9995 km due east, in degrees
	// Sweep the query so the site steps across the -3.75 cell edge.
	for i := 0; i < 2000; i++ {
		lon := -3.75 - east - 0.0001 + float64(i)*1e-7
		siteLon := lon + east
		ds, err := Parse([]byte(fmt.Sprintf(`{"sites":[{"forecast_fetched_at":"2026-10-19T11:05:00Z","id":"a","lat":57,"lon":%.9f}]}`, siteLon)), []byte(`{"sites":[]}`), "v")
		if err != nil {
			t.Fatal(err)
		}
		if got := len(ds.Within(57, lon, 1)); got != 1 {
			t.Fatalf("query at lon %.5f dropped a site 0.9995 km east", lon)
		}
	}
}

func TestDistanceKm(t *testing.T) {
	// Edinburgh to Glasgow is about 67 km.
	if d := DistanceKm(55.9533, -3.1883, 55.8642, -4.2518); math.Abs(d-67) > 2 {
		t.Fatalf("Edinburgh-Glasgow = %.1f km", d)
	}
}

func BenchmarkWindows(b *testing.B) {
	var sb strings.Builder
	sb.WriteString(`{"sites":[`)
	n := 0
	for lat := 54.6; lat <= 60.8; lat += 0.02 {
		for lon := -7.6; lon <= -0.8; lon += 0.06 {
			if n > 0 {
				sb.WriteByte(',')
			}
			fmt.Fprintf(&sb, `{"forecast_fetched_at":"2026-10-19T11:05:00Z","id":"s%d","lat":%.4f,"lon":%.4f,"lp_zone":"rural","clear_dark_hours":6,"clear_twilight_hours":6,"best_hours":[`, n, lat, lon)
			for h := 0; h < 25; h++ {
				if h > 0 {
					sb.WriteByte(',')
				}
				fmt.Fprintf(&sb, `{"time":"2026-10-%02dT%02d:00:00Z","cloud_area_fraction":%d,"dark":%t}`, 19+h/8, 18+h%6, h%9, h%3 != 0)
			}
			sb.WriteString(`]}`)
			n++
		}
	}
	sb.WriteString(`]}`)
	ds, err := Parse([]byte(sb.String()), []byte(`{"sites":[]}`), "v")
	if err != nil {
		b.Fatal(err)
	}
	b.Logf("%d sites", len(ds.Sites))
	q := WindowsQuery{Lat: 57.19, Lon: -3.83, RadiusKm: 150, Nights: 7, Limit: 10, Now: time.Date(2026, 10, 19, 12, 0, 0, 0, time.UTC)}
	b.ResetTimer()
	for b.Loop() {
		ds.Windows(q)
	}
}

func TestMoonlessHoursFollowTheMoon(t *testing.T) {
	// Two clear-dark hours at Aviemore on the night of the 20th: at 22:00Z the
	// 71%-lit moon is 13 deg up, at 03:00Z it has set (ephem: -19 deg). Only
	// the second is moonless.
	ds, err := Parse([]byte(`{"fetched_at":"2026-10-20T11:05:00Z","sites":[{"forecast_fetched_at":"2026-10-20T11:05:00Z","id":"a","lat":57.19,"lon":-3.83,"clear_dark_hours":2,"clear_twilight_hours":2,"best_hours":[
	  {"time":"2026-10-20T22:00:00+00:00","cloud_area_fraction":1,"dark":true},
	  {"time":"2026-10-21T03:00:00+00:00","cloud_area_fraction":1,"dark":true}]}]}`), []byte(`{"sites":[]}`), "v")
	if err != nil {
		t.Fatal(err)
	}
	now := time.Date(2026, 10, 20, 15, 0, 0, 0, time.UTC)
	w := ds.Windows(WindowsQuery{Lat: 57.19, Lon: -3.83, RadiusKm: 5, Nights: 1, Limit: 1, Now: now})[0]
	n := w.Nights[0]
	if n.ClearDark != 2 || n.MoonlessDark != 1 || w.MoonlessDark != 1 {
		t.Fatalf("night %+v", n)
	}
	if n.MoonAlt[0] < 10 || n.MoonAlt[1] > -15 {
		t.Fatalf("moon altitudes %v", n.MoonAlt)
	}
	if n.MoonLit < 0.65 || n.MoonLit > 0.8 {
		t.Fatalf("moon lit %.2f", n.MoonLit)
	}
	// Asking for that night by date gives the same night.
	if got := ds.Windows(WindowsQuery{Lat: 57.19, Lon: -3.83, RadiusKm: 5, Nights: 1, Limit: 1, Night: NightDay(now.Add(6 * time.Hour)), Now: now}); got[0].Nights[0].Night != "2026-10-20" {
		t.Fatalf("night = %s", got[0].Nights[0].Night)
	}
}
