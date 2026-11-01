package walks

import (
	"fmt"
	"strings"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/astro"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/verdict"
)

func assessDay(t *testing.T, duration float64, offsets ...int) Assessment {
	t.Helper()
	ds := mustParse(t, snapshot(walkJSON("w", 57, -4, duration, 0, offsets...)))
	w := &ds.Walks[0]
	d := ds.DayFor(w, LocalDay(base), base)
	a := w.Assess(d, ds.HourlyUntil)
	if v := w.Verdict(d); v != a.Verdict {
		t.Errorf("Verdict %s, Assess %s", v, a.Verdict)
	}
	return a
}

func TestVerdicts(t *testing.T) {
	// base is 08:00 BST; the fixture wind is 18 km/h and rain 0.2 mm an hour.
	go4 := assessDay(t, 3, 0, 1, 2, 3)
	if go4.Verdict != verdict.Go || !strings.Contains(go4.Why, "4 good hours 08:00 to 12:00") || !strings.Contains(go4.Why, "0.8 mm of rain") {
		t.Fatalf("go = %+v", go4)
	}
	// A 3 h walk in a 4 h slot ending 12:00 BST: start between 08:00 and 09:00.
	if !go4.StartFrom.Equal(base) || !go4.StartBy.Equal(base.Add(time.Hour)) {
		t.Fatalf("start window %v to %v", go4.StartFrom, go4.StartBy)
	}
	if gap := assessDay(t, 3, 0, 1, 3, 4); gap.Verdict != verdict.Marginal || !strings.Contains(gap.Why, "one wet or windy hour") {
		t.Fatalf("gap = %+v", gap)
	}
	// 9 h needs 7.2 good hours; 6 plus the 2 h shoulder fits, in the dark.
	// It would start before the slot, but the slot starts at the first hour
	// that counts, so it starts then rather than in the past.
	dark := assessDay(t, 9, 0, 1, 2, 3, 4, 5)
	if dark.Verdict != verdict.Marginal || !strings.Contains(dark.Why, "in the dark") || !dark.StartFrom.Equal(base) || !dark.StartBy.Equal(base) {
		t.Fatalf("dark = %+v", dark)
	}
	// 7.5 h needs exactly 6 good hours: six fit in daylight, no dark start.
	if exact := assessDay(t, 7.5, 0, 1, 2, 3, 4, 5); exact.Verdict != verdict.Go {
		t.Fatalf("exact = %+v", exact)
	}
	if short := assessDay(t, 6, 0, 1); short.Verdict != verdict.No || !strings.Contains(short.Why, "longest good spell is 2 h") {
		t.Fatalf("short = %+v", short)
	}
	if none := assessDay(t, 2); none.Verdict != verdict.No || !strings.Contains(none.Why, "No dry, calm daylight hour") {
		t.Fatalf("none = %+v", none)
	}
}

// In November an 8 h slot can fill the day. The 80% rule tolerates hours of
// poor weather, and a 9 or 10 h walk it fits is marginal because it needs the
// dark (An Teallach and Ben Nevis on
// 2026-11-02 were go with a start before sunrise), while a 7 h walk in a
// morning slot fits in daylight by starting at sunrise.
func TestWinterDaylightBoundsTheTolerance(t *testing.T) {
	day := time.Date(2026, 11, 2, 0, 0, 0, 0, time.UTC)
	now := time.Date(2026, 11, 1, 12, 0, 0, 0, time.UTC)
	lat, lon := 57.8, -5.25
	assess := func(duration float64, from, to int) Assessment {
		t.Helper()
		var hours []string
		for h := from; h < to; h++ {
			hours = append(hours, fmt.Sprintf("[%d,6,0,10,40]", day.Add(time.Duration(h)*time.Hour).Unix()))
		}
		ds := mustParse(t, fmt.Sprintf(`{"schema_version":1,"generated_at":"2026-11-01T06:30:00+00:00","fetched_at":"2026-11-01T06:20:00+00:00","walks":[{"forecast_fetched_at":"2026-11-01T06:20:00Z","id":"w","name":"W","url":"https://www.walkhighlands.co.uk/w.shtml","lat":%g,"lon":%g,"distance_km":20,"ascent_m":1400,"duration_h":%g,"hours":[%s]}]}`,
			lat, lon, duration, strings.Join(hours, ",")))
		w := &ds.Walks[0]
		d := ds.DayFor(w, LocalDay(day), now)
		a := w.Assess(d, ds.HourlyUntil)
		if v := w.Verdict(d); v != a.Verdict {
			t.Errorf("%g h: Verdict %s, Assess %s", duration, v, a.Verdict)
		}
		return a
	}
	rise, set, ok := astro.SunTimes(day, lat, lon)
	if !ok {
		t.Fatal("no sunrise")
	}
	// Good 08:00 to 16:00Z: about 8 h 57 min of daylight in all.
	for _, h := range []float64{9, 10} {
		a := assess(h, 8, 16)
		if a.Verdict != verdict.Marginal || !strings.Contains(a.Why, "in the dark") || !a.StartFrom.Before(rise) {
			t.Errorf("%g h in a full winter day = %+v", h, a)
		}
	}
	// Good 08:00 to 14:00Z: the hour beyond the slot fits after it.
	a := assess(7, 8, 14)
	start := rise.Truncate(time.Minute).Add(time.Minute)
	if a.Verdict != verdict.Go || !a.StartFrom.Equal(start) || !a.StartBy.Equal(start) || a.StartFrom.Add(7*time.Hour).After(set) {
		t.Errorf("7 h in a morning slot = %+v, want go starting %v", a, start)
	}
	// Inside its slot nothing changes: start between 08:00 and 10:00Z.
	if a := assess(6, 8, 16); a.Verdict != verdict.Go || !a.StartFrom.Equal(day.Add(8*time.Hour)) || !a.StartBy.Equal(day.Add(10*time.Hour)) {
		t.Errorf("6 h in an 8 h slot = %+v", a)
	}
}

func TestUnknownBeyondTheForecast(t *testing.T) {
	ds := mustParse(t, snapshot(walkJSON("w", 57, -4, 3, 0, 0, 1)))
	// The horizon is 06:20Z on the 17th: the 18th has no forecast.
	w := &ds.Walks[0]
	a := w.Assess(ds.DayFor(w, LocalDay(base)+3, base), ds.HourlyUntil)
	if a.Verdict != verdict.Unknown || !strings.Contains(a.Why, "Beyond the hourly forecast, which reaches Wed 07:20") {
		t.Fatalf("unknown = %+v", a)
	}
}

// Later in the day only the hours not yet begun count, and the start window
// never lies in the past.
func TestAssessLaterToday(t *testing.T) {
	// Good 08:00 to 14:00 BST (offsets 0 to 5) for a 3 h walk.
	ds := mustParse(t, snapshot(walkJSON("w", 57, -4, 3, 0, 0, 1, 2, 3, 4, 5)))
	w := &ds.Walks[0]
	today := LocalDay(base)
	// At 12:55 BST only 13:00 to 14:00 is left: 1 h, short of the 2.4 h needed.
	late := base.Add(4*time.Hour + 55*time.Minute)
	if a := w.Assess(ds.DayFor(w, today, late), ds.HourlyUntil); a.Verdict != verdict.No || !strings.Contains(a.Why, "left today is 1 h") {
		t.Fatalf("late = %+v", a)
	}
	// At 09:10 BST, 10:00 to 14:00 is left: go, starting no earlier than 10:00.
	mid := base.Add(time.Hour + 10*time.Minute)
	a := w.Assess(ds.DayFor(w, today, mid), ds.HourlyUntil)
	if a.Verdict != verdict.Go || !a.StartFrom.Equal(base.Add(2*time.Hour)) || !a.StartBy.Equal(base.Add(3*time.Hour)) {
		t.Fatalf("mid = %+v", a)
	}
	// A long walk that would start before the slot starts at the next top
	// of the hour instead, the same answer all hour.
	long := mustParse(t, snapshot(walkJSON("l", 57, -4, 9, 0, 2, 3, 4, 5, 6, 7)))
	lw := &long.Walks[0]
	a = lw.Assess(long.DayFor(lw, today, mid), long.HourlyUntil)
	if a.Verdict != verdict.Marginal || !a.StartFrom.Equal(base.Add(2*time.Hour)) || a.StartBy.Before(a.StartFrom) {
		t.Fatalf("long = %+v", a)
	}
	if b := lw.Assess(long.DayFor(lw, today, mid.Add(40*time.Minute)), long.HourlyUntil); b != a {
		t.Fatalf("answers within the hour differ: %+v %+v", a, b)
	}
	// After the last good hour, the why says none is left today.
	if a := w.Assess(ds.DayFor(w, today, base.Add(7*time.Hour)), ds.HourlyUntil); a.Verdict != verdict.No || !strings.Contains(a.Why, "left today") {
		t.Fatalf("evening = %+v", a)
	}
}

// Limits narrow the good hours and say so in the why; without them the
// snapshot's own test stands.
func TestLimitsNarrowTheGoodHours(t *testing.T) {
	// The fixture hour has 0.2 mm of rain, 18 km/h of wind and 9.5 C.
	ds := mustParse(t, snapshot(walkJSON("w", 57, -4, 3, 0, 0, 1, 2, 3)))
	w := &ds.Walks[0]
	day := LocalDay(base)
	f := func(v float64) *float64 { return &v }
	if a := w.Assess(ds.DayWithin(w, day, base, Limits{MaxPrecipMM: f(0.2), MaxWindKmh: f(18), MinTempC: f(9.5)}), ds.HourlyUntil); a.Verdict != verdict.Go {
		t.Fatalf("at the limits = %+v", a)
	}
	for name, lim := range map[string]Limits{
		"dry":  {MaxPrecipMM: f(0)},
		"calm": {MaxWindKmh: f(15)},
		"mild": {MinTempC: f(10)},
	} {
		d := ds.DayWithin(w, day, base, lim)
		a := w.Assess(d, ds.HourlyUntil)
		if a.Verdict != verdict.No || d.GoodHours != 0 || a.Why != "No daylight hour within the limits at the start." {
			t.Errorf("%s = %+v", name, a)
		}
	}
	if d := ds.DayWithin(w, day, base, Limits{}); d.Limited || d.GoodHours != 4 {
		t.Fatalf("no limits = %+v", d)
	}
}

// tempWalk is a dry, calm walk whose hours (offsets from base, 07:00Z on
// 15 June, when sunrise is 03:22Z and sunset 21:13Z) carry the given
// temperatures.
func tempWalk(t *testing.T, duration float64, temps map[int]float64) *Dataset {
	t.Helper()
	var hours []string
	for o := -6; o < 18; o++ {
		if c, ok := temps[o]; ok {
			hours = append(hours, fmt.Sprintf("[%d,%g,0,10,20]", base.Add(time.Duration(o)*time.Hour).Unix(), c))
		}
	}
	return mustParse(t, snapshot(fmt.Sprintf(`{"forecast_fetched_at":"2026-06-15T06:20:00Z","id":"w","name":"W","url":"https://www.walkhighlands.co.uk/w.shtml","lat":57,"lon":-4,"distance_km":20,"ascent_m":900,"duration_h":%g,"hours":[%s]}`, duration, strings.Join(hours, ","))))
}

func temps(cold, warm [2]int) map[int]float64 {
	m := map[int]float64{}
	for o := cold[0]; o <= cold[1]; o++ {
		m[o] = 4
	}
	for o := warm[0]; o <= warm[1]; o++ {
		m[o] = 10
	}
	return m
}

// Under limits a long walk's dark credit counts only where the slot meets
// real darkness: the hours beside it are often daylight the person ruled
// out.
func TestLimitsAndTheDarkShoulder(t *testing.T) {
	f := func(v float64) *float64 { return &v }
	warm := Limits{MinTempC: f(5)}
	day := LocalDay(base)
	assess := func(ds *Dataset, now time.Time, lim Limits) Assessment {
		w := &ds.Walks[0]
		return w.Assess(ds.DayWithin(w, day, now, lim), ds.HourlyUntil)
	}
	// An 8 h walk (6.4 h needed) with 5 warm hours between cold daylight:
	// the 2 h credit would cover it, but neither edge is dark.
	ds := tempWalk(t, 8, temps([2]int{0, 11}, [2]int{4, 8}))
	if a := assess(ds, base, warm); a.Verdict != verdict.No || !strings.Contains(a.Why, "within the limits is 5 h") {
		t.Fatalf("both edges daylight = %+v", a)
	}
	if a := assess(ds, base, Limits{}); a.Verdict != verdict.Go {
		t.Fatalf("no limits = %+v", a)
	}
	// An hour that failed the defaults at the edge is no darker: cold from
	// 07:00Z, nothing at 10:00Z, warm 11:00Z to 15:00Z, cold after.
	gap := temps([2]int{0, 11}, [2]int{4, 8})
	delete(gap, 3)
	if a := assess(tempWalk(t, 8, gap), base, warm); a.Verdict != verdict.No {
		t.Fatalf("missing edge hour = %+v", a)
	}
	// The slot runs to 21:00Z, the last full daylight hour: credit is spent after it, so
	// the walk starts with the slot.
	ds = tempWalk(t, 8, temps([2]int{6, 8}, [2]int{9, 14}))
	slotStart := base.Add(9 * time.Hour)
	if a := assess(ds, base, warm); a.Verdict != verdict.Marginal || !strings.Contains(a.Why, "in the dark") || !a.StartFrom.Equal(slotStart) || !a.StartBy.Equal(slotStart) {
		t.Fatalf("dark after = %+v", a)
	}
	// The slot starts at 04:00Z, just after sunrise: the walk starts early,
	// in the dark.
	ds = tempWalk(t, 8, temps([2]int{2, 5}, [2]int{-3, 1}))
	early := base.Add(-5 * time.Hour)
	if a := assess(ds, early, warm); a.Verdict != verdict.Marginal || !a.StartFrom.Before(base.Add(-3*time.Hour)) {
		t.Fatalf("dark before = %+v", a)
	}
	// Later today the hour before the slot is daylight under way, not dark:
	// at 11:30Z with warm hours to 16:00Z and cold after, it does not fit.
	ds = tempWalk(t, 8, temps([2]int{10, 11}, [2]int{0, 9}))
	if a := assess(ds, base.Add(4*time.Hour+30*time.Minute), warm); a.Verdict != verdict.No {
		t.Fatalf("today at the cutoff = %+v", a)
	}
	// A walk of 7 h or less has no dark credit; its 80% rule starts it
	// before the slot as without limits.
	ds = tempWalk(t, 5, temps([2]int{0, 0}, [2]int{1, 4}))
	if a := assess(ds, base, warm); a.Verdict != verdict.Go || !a.StartFrom.Equal(base) {
		t.Fatalf("short walk = %+v", a)
	}
	// Just after dawn today the early dark is past: at 03:40Z, warm 04:00Z
	// to 08:00Z and cold after, the credit has nowhere to go.
	ds = tempWalk(t, 8, temps([2]int{2, 5}, [2]int{-3, 1}))
	if a := assess(ds, base.Add(-3*time.Hour-20*time.Minute), warm); a.Verdict != verdict.No {
		t.Fatalf("after dawn = %+v", a)
	}
	// Limits that turn no hour away change nothing, even where the stricter
	// rule would: good hours 06:00Z, 08:00Z to 12:00Z and 14:00Z.
	ds = tempWalk(t, 8, map[int]float64{-1: 10, 1: 10, 2: 10, 3: 10, 4: 10, 5: 10, 7: 10})
	none := assess(ds, base.Add(-2*time.Hour), Limits{})
	for _, lim := range []Limits{{MaxWindKmh: f(80)}, {MaxPrecipMM: f(2)}, {MinTempC: f(-30)}} {
		if a := assess(ds, base.Add(-2*time.Hour), lim); a != none {
			t.Errorf("default-valued %+v: %+v, want %+v", lim, a, none)
		}
	}
	if none.Verdict != verdict.Marginal {
		t.Fatalf("no limits = %+v", none)
	}
	// A cold hour mid-walk is the slot's one bad hour, and the why says so.
	ds = tempWalk(t, 3, map[int]float64{0: 10, 1: 10, 2: 3, 3: 10})
	if a := assess(ds, base, warm); a.Verdict != verdict.Marginal || !strings.Contains(a.Why, "one cold hour") {
		t.Fatalf("cold gap = %+v", a)
	}
}

// A bad hour turned away for wind but wet too keeps "otherwise dry".
func TestOtherwiseDryWhateverTheGapFailed(t *testing.T) {
	// Good hours dry and calm (10 km/h); 09:00Z is windy (40 km/h) and wet.
	var hours []string
	for o := 0; o < 5; o++ {
		wind, rain := 10, 0.0
		if o == 2 {
			wind, rain = 40, 1.8
		}
		hours = append(hours, fmt.Sprintf("[%d,9.5,%g,%d,40]", base.Add(time.Duration(o)*time.Hour).Unix(), rain, wind))
	}
	ds := mustParse(t, snapshot(`{"forecast_fetched_at":"2026-06-15T06:20:00Z","id":"w","name":"W","url":"https://www.walkhighlands.co.uk/w.shtml","lat":57,"lon":-4,"distance_km":9,"ascent_m":300,"duration_h":4,"hours":[`+strings.Join(hours, ",")+`]}`))
	w := &ds.Walks[0]
	f := func(v float64) *float64 { return &v }
	a := w.Assess(ds.DayWithin(w, LocalDay(base), base, Limits{MaxWindKmh: f(30)}), ds.HourlyUntil)
	if a.Verdict != verdict.Marginal || !strings.Contains(a.Why, "one windy hour") || !strings.Contains(a.Why, "otherwise dry") {
		t.Fatalf("assess = %+v", a)
	}
}

// Synthetic weather sequences, not a reproduction of an unspecified Sunday.
func TestRainFrequencyAdvice(t *testing.T) {
	for _, tc := range []struct {
		name      string
		rain      []float32
		limit     float64
		wet, good int
	}{
		{"dry", []float32{0, 0, 0, 0}, 2, 0, 4},
		{"all wet", []float32{2, 2, 2, 2}, 2, 4, 4},
		{"mixed and threshold", []float32{0, 0.099, 0.1, 0.2}, 2, 2, 4},
		{"custom boundary", []float32{0, 0.1, 0.2, 0.1}, 0.1, 2, 3},
		{"looser boundary", []float32{3, 3, 3, 3}, 3, 4, 4},
		{"tolerated wet gap", []float32{0, 0, 3, 0}, 2, 0, 3},
	} {
		t.Run(tc.name, func(t *testing.T) {
			ds := mustParse(t, snapshot(walkJSON("w", 57, -4, 2, 0, 0, 1, 2, 3)))
			w := &ds.Walks[0]
			for i := range w.Hours {
				w.Hours[i].PrecipMM = tc.rain[i]
			}
			w.good = DefaultTest.filter(w.Hours)
			d := ds.DayWithin(w, LocalDay(base), base, Limits{MaxPrecipMM: &tc.limit})
			a := w.Assess(d, ds.HourlyUntil)
			want := fmt.Sprintf("rain in %d of %d qualifying hours", tc.wet, tc.good)
			if !strings.Contains(a.Why, want) {
				t.Fatalf("want %q: %+v", want, a)
			}
		})
	}
}

func TestCloudAdviceFeasibleClearing(t *testing.T) {
	for _, tc := range []struct {
		name         string
		duration     float64
		unknown, gap bool
		now          time.Time
		prefer       bool
	}{
		{"clearer later", 2, false, false, base, true},
		{"too long to wait", 6, false, false, base, false},
		{"unknown is not clear", 2, true, false, base, false},
		{"gap not a full clear walk", 2, false, true, base, false},
		{"already cleared", 2, false, false, base.Add(3 * time.Hour), false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			ds := mustParse(t, snapshot(walkJSON("w", 57, -4, tc.duration, 0, 0, 1, 2, 3, 4, 5)))
			w := &ds.Walks[0]
			for i := range w.Hours {
				w.Hours[i].LowCloudPct = 90
				w.Hours[i].FogPct = 60
				if i >= 3 {
					w.Hours[i].LowCloudPct = 10
					w.Hours[i].FogPct = 0
				}
				if tc.unknown {
					w.Hours[i].FogPct = nan
				}
				if tc.gap && i == 3 {
					w.Hours[i].LowCloudPct = 90
					w.Hours[i].FogPct = 60
				}
				if tc.gap && i == 4 {
					w.Hours[i].PrecipMM = 3
				}
			}
			w.good = DefaultTest.filter(w.Hours)
			d := ds.DayFor(w, LocalDay(base), tc.now)
			a := w.Assess(d, ds.HourlyUntil)
			if strings.Contains(a.Why, "Prefer ") != tc.prefer || strings.Contains(a.Why, "cloud base") {
				t.Fatalf("%+v", a)
			}
			if tc.prefer && !strings.Contains(a.Why, "Prefer 11:00") {
				t.Fatal(a.Why)
			}
			if tc.unknown && strings.Contains(a.Why, "fog up to") {
				t.Fatal(a.Why)
			}
		})
	}
	// Legacy missing values must not become clear skies.
	a := assessDay(t, 2, 0, 1, 2)
	if strings.Contains(a.Why, "low cloud") || strings.Contains(a.Why, "fog") {
		t.Fatal(a.Why)
	}
}

// Synthetic route profiles: ascent is not summit elevation. Low cloud alone
// must not delay a low-relief walk; local fog can justify waiting there.
func TestCloudTimingRouteProfile(t *testing.T) {
	for _, tc := range []struct {
		name            string
		ascent          int
		length          float64
		fog, unknownLow bool
		want            bool
		reason          string
	}{
		{"urban cloud only", 50, 4, false, false, false, ""},
		{"urban fog", 50, 4, true, false, true, "less fog"},
		{"urban fog unknown cloud", 50, 4, true, true, true, "less fog"},
		{"substantial climb", 990, 12, false, false, true, "less low cloud/fog"},
		{"ascent boundary", 500, 12.5, false, false, true, "less low cloud/fog"},
		{"below ascent boundary", 499, 12, false, false, false, ""},
		{"rolling long route", 600, 20, false, false, false, ""},
		{"unknown length", 600, 0, false, false, false, ""},
		{"hill unknown cloud", 990, 12, true, true, false, ""},
	} {
		t.Run(tc.name, func(t *testing.T) {
			ds := mustParse(t, snapshot(walkJSON("w", 57, -4, 2, 0, 0, 1, 2, 3, 4, 5)))
			w := &ds.Walks[0]
			w.AscentM = tc.ascent
			w.LengthKm = tc.length
			for i := range w.Hours {
				cover := float32(90)
				if i >= 3 {
					cover = 10
				}
				w.Hours[i].LowCloudPct = cover
				w.Hours[i].FogPct = 0
				if tc.fog {
					w.Hours[i].FogPct = cover
				}
				if tc.unknownLow {
					w.Hours[i].LowCloudPct = nan
				}
			}
			w.good = DefaultTest.filter(w.Hours)
			a := w.Assess(ds.DayFor(w, LocalDay(base), base), ds.HourlyUntil)
			if strings.Contains(a.Why, "Prefer ") != tc.want || tc.want && !strings.Contains(a.Why, tc.reason) {
				t.Fatal(a.Why)
			}
			if a.Verdict != verdict.Go {
				t.Fatal(a)
			}
			if strings.Contains(a.Why, "known)") || len(a.Why) > 340 {
				t.Fatalf("verbose (%d): %s", len(a.Why), a.Why)
			}
		})
	}
}

func TestCloudKnownCountsOnlyForPartialCoverage(t *testing.T) {
	ds := mustParse(t, snapshot(walkJSON("w", 57, -4, 2, 0, 0, 1, 2, 3)))
	w := &ds.Walks[0]
	for i := range w.Hours {
		w.Hours[i].LowCloudPct = 80
		w.Hours[i].FogPct = nan
	}
	w.Hours[0].LowCloudPct = nan
	w.good = DefaultTest.filter(w.Hours)
	a := w.Assess(ds.DayFor(w, LocalDay(base), base), ds.HourlyUntil)
	if !strings.Contains(a.Why, "low cloud up to 80% (3 of 4 qualifying hours known)") || strings.Contains(a.Why, "fog up to") {
		t.Fatal(a.Why)
	}
}
