package walks

import (
	"fmt"
	"strings"
	"testing"
	"time"
)

// base is 08:00 BST on 15 June 2026, the hikes router tests' UNIT_BASE day.
var base = time.Date(2026, 6, 15, 7, 0, 0, 0, time.UTC)

func hoursAt(offsets ...int) []Hour {
	out := make([]Hour, len(offsets))
	for i, o := range offsets {
		at := base.Add(time.Duration(o) * time.Hour)
		out[i] = Hour{Unix: at.Unix(), Day: LocalDay(at)}
	}
	return out
}

// The same cases as TestLongestGoodRun in projects/monolith/hikes/router_test.py.
func TestLongestRunMatchesHikes(t *testing.T) {
	for _, c := range []struct {
		offsets []int
		want    int
	}{
		{nil, 0},
		{[]int{0}, 1},
		{[]int{0, 1, 2, 3}, 4},
		{[]int{0, 1, 3, 4}, 4},
		{[]int{0, 2, 4}, 2},
		{[]int{0, 1, 4, 5}, 2},
		{[]int{0, 1, 3, 4, 7, 8}, 4},
	} {
		if got, _, _ := longestRun(hoursAt(c.offsets...)); got != c.want {
			t.Errorf("longestRun(%v) = %d, want %d", c.offsets, got, c.want)
		}
	}
}

// The same cases as TestDoableDays in projects/monolith/hikes/router_test.py.
func TestDoableMatchesHikes(t *testing.T) {
	for _, c := range []struct {
		offsets  []int
		duration float64
		want     bool
	}{
		{[]int{0, 1, 2, 3, 4}, 5.5, true},
		{[]int{0, 1, 2, 3}, 5.5, false},
		{[]int{0, 1, 2, 3}, 5.0, true},
		{[]int{0, 1, 2, 4, 5}, 5.5, true},
		{[]int{0, 1, 2, 3, 4, 5}, 10.0, true},
		{[]int{0, 1, 2, 3, 4}, 10.0, false},
		{[]int{0, 1, 2, 3, 4}, 7.0, false},
		{nil, 3.0, false},
	} {
		ds := mustParse(t, snapshot(walkJSON("w", 57, -4, c.duration, 0, c.offsets...)))
		w := &ds.Walks[0]
		if got := w.day(LocalDay(base), w.Hours).Doable; got != c.want {
			t.Errorf("doable(%v, %.1f h) = %v, want %v", c.offsets, c.duration, got, c.want)
		}
	}
}

func TestBestRunBoundsTheSlot(t *testing.T) {
	ds := mustParse(t, snapshot(walkJSON("w", 57, -4, 3, 0, 0, 1, 4, 5, 6)))
	w := &ds.Walks[0]
	d := w.day(LocalDay(base), w.Hours)
	if d.Best == nil || d.Best.GoodHours != 3 || !d.Best.Start.Equal(base.Add(4*time.Hour)) || !d.Best.End.Equal(base.Add(7*time.Hour)) {
		t.Fatalf("best = %+v", d.Best)
	}
}

func walkJSON(id string, lat, lon, duration float64, ascent int, offsets ...int) string {
	hours := make([]string, len(offsets))
	for i, o := range offsets {
		hours[i] = fmt.Sprintf("[%d,9.5,0.2,18,40]", base.Add(time.Duration(o)*time.Hour).Unix())
	}
	return fmt.Sprintf(`{"id":%q,"name":"Walk %s","url":"https://www.walkhighlands.co.uk/x/%s.shtml","lat":%g,"lon":%g,"distance_km":9.1,"ascent_m":%d,"duration_h":%g,"hours":[%s]}`,
		id, id, id, lat, lon, ascent, duration, strings.Join(hours, ","))
}

func snapshot(walks ...string) string {
	return `{"schema_version":1,"generated_at":"2026-06-15T06:30:00+00:00","fetched_at":"2026-06-15T06:20:00+00:00","walks":[` + strings.Join(walks, ",") + `]}`
}

func mustParse(t *testing.T, body string) *Dataset {
	t.Helper()
	ds, err := Parse([]byte(body), `"w1"`)
	if err != nil {
		t.Fatal(err)
	}
	return ds
}

func TestParseRejectsBadSnapshots(t *testing.T) {
	for name, body := range map[string]string{
		"not json":       `{`,
		"future schema":  `{"schema_version":2,"walks":[` + walkJSON("a", 57, -4, 3, 0) + `]}`,
		"no walks":       `{"schema_version":1,"walks":[]}`,
		"missing url":    `{"schema_version":1,"walks":[{"id":"a","name":"A","duration_h":3}]}`,
		"bad fetched_at": `{"schema_version":1,"fetched_at":"yesterday","walks":[` + walkJSON("a", 57, -4, 3, 0) + `]}`,
	} {
		if _, err := Parse([]byte(body), "v"); err == nil {
			t.Errorf("%s: parsed", name)
		}
	}
	ds := mustParse(t, snapshot(walkJSON("a", 57, -4, 3, 0)))
	if !ds.FetchedAt.Equal(time.Date(2026, 6, 15, 6, 20, 0, 0, time.UTC)) {
		t.Fatalf("fetched_at = %v", ds.FetchedAt)
	}
}

func TestDayForDropsElapsedHours(t *testing.T) {
	// Now is 10:30 BST on day one; base is 08:00 BST. The hour in progress
	// (10:00, offset 2) is kept, as the hikes read filter does, so offsets 0
	// and 1 have elapsed.
	now := base.Add(2*time.Hour + 30*time.Minute)
	ds := mustParse(t, snapshot(
		// Doable today (5 good hours left for a 4 h walk) and tomorrow.
		walkJSON("both", 57.00, -4.00, 4, 600, 0, 1, 2, 3, 4, 5, 6, 24, 25, 26, 27),
		// Today's good hours have all elapsed: not doable.
		walkJSON("elapsed", 57.02, -4.00, 2, 100, 0, 1),
	))
	today := LocalDay(now)
	d := ds.DayFor(ds.ByID("both"), today, now)
	if d.GoodHours != 5 || !d.Doable || d.Hours[0].Unix != base.Add(2*time.Hour).Unix() {
		t.Fatalf("today = %+v", d)
	}
	if d := ds.DayFor(ds.ByID("both"), today+1, now); !d.Doable || d.GoodHours != 4 {
		t.Fatalf("tomorrow = %+v", d)
	}
	if d := ds.DayFor(ds.ByID("elapsed"), today, now); d.Doable || d.GoodHours != 0 || d.Best != nil {
		t.Fatalf("elapsed = %+v", d)
	}
	if ds.ByID("nope") != nil {
		t.Fatal("ByID found a missing walk")
	}
	if km := ds.ByID("elapsed").DistanceKm(57, -4); km < 2.1 || km > 2.3 {
		t.Fatalf("distance = %v", km)
	}
}

func TestLocalDayUsesUKCivilDate(t *testing.T) {
	// 23:30 UTC on 15 June is 00:30 BST on 16 June.
	if got := DayName(LocalDay(time.Date(2026, 6, 15, 23, 30, 0, 0, time.UTC))); got != "2026-06-16" {
		t.Fatalf("got %s", got)
	}
	// In winter UTC and UK time agree.
	if got := DayName(LocalDay(time.Date(2026, 12, 15, 23, 30, 0, 0, time.UTC))); got != "2026-12-15" {
		t.Fatalf("got %s", got)
	}
}

func TestParseSkipsBadRowsButKeepsTheRest(t *testing.T) {
	// The prod seed holds a 0 h walk; it must not take out every other walk.
	ds := mustParse(t, snapshot(walkJSON("good", 57, -4, 3, 0), walkJSON("zero", 57, -4, 0, 0)))
	if len(ds.Walks) != 1 || ds.Walks[0].ID != "good" || ds.Skipped != 1 {
		t.Fatalf("walks %d skipped %d", len(ds.Walks), ds.Skipped)
	}
}

func TestCoverageFollowsTheHourlyHorizon(t *testing.T) {
	// fetched_at 06:20Z on 15 June, so the hourly horizon is 06:20Z on the
	// 17th (07:20 BST): the 15th and 16th are full, the 17th partial, the
	// 18th has no forecast.
	ds := mustParse(t, snapshot(walkJSON("w", 57, -4, 3, 0, 0, 1)))
	if want := time.Date(2026, 6, 17, 6, 20, 0, 0, time.UTC); !ds.HourlyUntil.Equal(want) {
		t.Fatalf("horizon %v", ds.HourlyUntil)
	}
	first := LocalDay(base)
	for i, want := range []Coverage{CoverageFull, CoverageFull, CoveragePartial, CoverageNone} {
		if got := ds.CoverageOf(first + int32(i)); got != want {
			t.Errorf("day %d coverage %s, want %s", i, got, want)
		}
	}
	// A later hour in the snapshot extends the horizon past the 48 h floor.
	ds = mustParse(t, snapshot(walkJSON("w", 57, -4, 3, 0, 60)))
	if want := base.Add(61 * time.Hour); !ds.HourlyUntil.Equal(want) {
		t.Fatalf("horizon %v, want %v", ds.HourlyUntil, want)
	}
}

func TestDaysBeyondTheForecastAreUndecided(t *testing.T) {
	// A 3 h walk, good on day one only. Day three is partial with no slot
	// yet, so it is undecided rather than "not doable".
	ds := mustParse(t, snapshot(walkJSON("w", 57, -4, 3, 0, 0, 1, 2, 3)))
	days := make([]Day, 3)
	for i := range days {
		days[i] = ds.DayFor(&ds.Walks[0], LocalDay(base)+int32(i), base)
	}
	if !days[0].Decided || !days[0].Doable || days[0].Coverage != CoverageFull {
		t.Fatalf("day 1 = %+v", days[0])
	}
	if !days[1].Decided || days[1].Doable {
		t.Fatalf("day 2, fully forecast and empty, is decided not doable: %+v", days[1])
	}
	if days[2].Coverage != CoveragePartial || days[2].Decided {
		t.Fatalf("day 3 = %+v", days[2])
	}
}

func TestNameMatching(t *testing.T) {
	ds := mustParse(t, snapshot(
		strings.Replace(walkJSON("bn", 56.79, -5.0, 8, 1350, 0, 1, 2), `"Walk bn"`, `"Ben Nevis by the Mountain Path"`, 1),
		strings.Replace(walkJSON("cmd", 56.80, -4.99, 10, 1500, 0, 1, 2), `"Walk cmd"`, `"Carn Mor Dearg Arête and Ben Nevis"`, 1),
		strings.Replace(walkJSON("dd", 56.78, -5.07, 3.5, 314, 0, 1, 2, 3), `"Walk dd"`, `"Dùn Deardail fort, Glen Nevis"`, 1),
		strings.Replace(walkJSON("sc", 56.9, -5.3, 5, 800), `"Walk sc"`, `"Stob Coire a'Chearcaill"`, 1),
	))
	for _, c := range []struct {
		query string
		want  string
	}{
		{"Ben Nevis", "bn,cmd"}, // the name that starts with the query first
		{"nevis", "dd,bn,cmd"},  // a word prefix anywhere; then shorter names
		{"dun deardail", "dd"},  // accents fold
		{"Carn Mor Dearg Arete", "cmd"},
		{"stob coire a chearcaill", "sc"}, // apostrophes separate words
		{"a'chearcaill", "sc"},            // with or without a space after
		{"Dun Deardail", "dd"},
		{"ben lomond", ""},
	} {
		var ids []string
		for _, w := range ds.Named(Normalize(c.query)) {
			ids = append(ids, w.ID)
		}
		if strings.Join(ids, ",") != c.want {
			t.Errorf("%q matched %v, want %s", c.query, ids, c.want)
		}
	}
}

func TestNormalizeFoldsDecomposedAccents(t *testing.T) {
	// "Dùn" with a combining grave accent (U+0300) rather than a precomposed ù.
	if got := Normalize("Du\u0300n Deardail’s fort"); got != "dun deardail s fort" {
		t.Fatalf("got %q", got)
	}
}

func TestSlotStats(t *testing.T) {
	ds := mustParse(t, snapshot(walkJSON("w", 57, -4, 3, 0, 24, 25, 26, 27)))
	tomorrow := LocalDay(base) + 1
	best := ds.DayFor(&ds.Walks[0], tomorrow, base)
	if best.Day != tomorrow || !best.Doable || best.Best.GoodHours != 4 || best.Best.MaxWindKmh != 18 || best.Best.MinTempC != 9.5 ||
		best.Best.TotalPrecipMM < 0.79 || best.Best.TotalPrecipMM > 0.81 {
		t.Fatalf("best = %+v", best.Best)
	}
}

// The CoverageOf index agrees with the rule it caches, for horizons at every
// hour of a day.
func TestCoverageIndexMatchesTheRule(t *testing.T) {
	for h := 0; h < 48; h++ {
		ds := &Dataset{HourlyUntil: base.Add(time.Duration(h) * time.Hour)}
		ds.indexCoverage()
		for day := LocalDay(base) - 3; day <= LocalDay(base)+5; day++ {
			if got, want := ds.CoverageOf(day), ds.coverage(day); got != want {
				t.Errorf("horizon +%dh day %s: index %s, rule %s", h, DayName(day), got, want)
			}
		}
	}
}
