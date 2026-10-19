package stars

import (
	"fmt"
	"math"
	"strings"
	"testing"
	"time"
)

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
  {"id":"near-cloudy","name":null,"lat":57.20,"lon":-3.80,"altitude_m":300,"lp_zone":"rural",
   "clear_dark_hours":1,"clear_twilight_hours":1,
   "best_hours":[{"time":"2026-10-19T22:00:00+00:00","cloud_area_fraction":5,"dark":true}]},
  {"id":"far-clear","name":null,"lat":57.60,"lon":-3.80,"altitude_m":120,"lp_zone":"pristine",
   "clear_dark_hours":3,"clear_twilight_hours":3,
   "best_hours":[
     {"time":"2026-10-20T23:00:00+00:00","cloud_area_fraction":1,"dark":true},
     {"time":"2026-10-19T21:00:00+00:00","cloud_area_fraction":2,"dark":true},
     {"time":"2026-10-20T01:00:00+00:00","cloud_area_fraction":3,"dark":true}]},
  {"id":"out-of-range","name":null,"lat":60.15,"lon":-1.15,"altitude_m":50,"lp_zone":"pristine",
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
		"duplicate id":   {`{"sites":[{"id":"a","lat":1,"lon":1},{"id":"a","lat":1,"lon":1}]}`, history()},
		"empty id":       {`{"sites":[{"id":"","lat":1,"lon":1}]}`, history()},
		"bad coordinate": {`{"sites":[{"id":"a","lat":91,"lon":1}]}`, history()},
		"bad hour":       {`{"sites":[{"id":"a","lat":1,"lon":1,"best_hours":[{"time":"soon"}]}]}`, history()},
		"short history":  {`{"sites":[]}`, `{"sites":[{"id":"a","clear":[1],"dark":[1]}]}`},
	}
	for name, c := range cases {
		if _, err := Parse([]byte(c[0]), []byte(c[1]), "v"); err == nil {
			t.Errorf("%s: accepted", name)
		}
	}
}

func TestNightOfUsesLocalEvening(t *testing.T) {
	cases := map[string]string{
		"2026-10-19T21:00:00Z": "2026-10-19",
		"2026-10-20T01:00:00Z": "2026-10-19", // 02:00 BST still belongs to the 19th
		"2026-10-20T10:30:00Z": "2026-10-19", // 11:30 BST, before local noon
		"2026-10-20T11:30:00Z": "2026-10-20", // 12:30 BST
		"2026-12-20T11:30:00Z": "2026-12-19", // GMT
	}
	for in, want := range cases {
		ts, _ := time.Parse(time.RFC3339, in)
		if got := NightOf(ts); got != want {
			t.Errorf("NightOf(%s) = %s, want %s", in, got, want)
		}
	}
}

func TestWindowsRanksAndBuckets(t *testing.T) {
	ds := load(t)
	q := WindowsQuery{Lat: 57.19, Lon: -3.83, RadiusKm: 60, Nights: 2, Limit: 5,
		Now: time.Date(2026, 10, 19, 15, 20, 0, 0, time.UTC)}
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

func TestWithinMatchesBruteForce(t *testing.T) {
	var b strings.Builder
	b.WriteString(`{"sites":[`)
	n := 0
	for lat := 54.6; lat <= 60.8; lat += 0.05 {
		for lon := -7.6; lon <= -0.8; lon += 0.05 {
			if n > 0 {
				b.WriteByte(',')
			}
			fmt.Fprintf(&b, `{"id":"s%d","lat":%.4f,"lon":%.4f}`, n, lat, lon)
			n++
		}
	}
	b.WriteString(`]}`)
	ds, err := Parse([]byte(b.String()), []byte(`{"sites":[]}`), "v")
	if err != nil {
		t.Fatal(err)
	}
	for _, p := range [][3]float64{{57.19, -3.83, 150}, {60.15, -1.15, 80}, {54.9, -4.4, 25}, {58.2, -6.39, 1}} {
		got := len(ds.Within(p[0], p[1], p[2]))
		want := 0
		for _, s := range ds.Sites {
			if DistanceKm(p[0], p[1], s.Lat, s.Lon) <= p[2] {
				want++
			}
			if d, f := DistanceKm(p[0], p[1], s.Lat, s.Lon), FastDistanceKm(p[0], p[1], math.Cos(p[0]*math.Pi/180), s.Lat, s.Lon, s.cosLat); d < 160 && math.Abs(d-f) > 0.05 {
				t.Fatalf("fast distance off by %.3f km at %.1f km", d-f, d)
			}
		}
		// The two metrics may disagree on a site sitting on the boundary.
		if math.Abs(float64(got-want)) > 2 {
			t.Errorf("Within(%v) = %d, brute force %d", p, got, want)
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
			fmt.Fprintf(&sb, `{"id":"s%d","lat":%.4f,"lon":%.4f,"lp_zone":"rural","clear_dark_hours":6,"clear_twilight_hours":6,"best_hours":[`, n, lat, lon)
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
