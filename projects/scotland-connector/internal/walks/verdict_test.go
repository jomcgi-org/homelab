package walks

import (
	"fmt"
	"strings"
	"testing"
	"time"

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

// tempWalk is a dry, calm walk whose hours (offsets from base) carry the
// given temperatures.
func tempWalk(t *testing.T, duration float64, temps map[int]float64) *Dataset {
	t.Helper()
	var hours []string
	for o := 0; o < 24; o++ {
		if c, ok := temps[o]; ok {
			hours = append(hours, fmt.Sprintf("[%d,%g,0,10,20]", base.Add(time.Duration(o)*time.Hour).Unix(), c))
		}
	}
	return mustParse(t, snapshot(fmt.Sprintf(`{"id":"w","name":"W","url":"https://www.walkhighlands.co.uk/w.shtml","lat":57,"lon":-4,"distance_km":20,"ascent_m":900,"duration_h":%g,"hours":[%s]}`, duration, strings.Join(hours, ","))))
}

// Hours the limits turned away are daylight the person ruled out, so a long
// walk cannot borrow them as "dark" hours.
func TestLimitsAndTheDarkShoulder(t *testing.T) {
	f := func(v float64) *float64 { return &v }
	warm := Limits{MinTempC: f(5)}
	day := LocalDay(base)
	// An 8 h walk (6.4 h needed) with 5 warm hours between cold ones: the
	// 2 h dark credit would cover it, but both edges are cold daylight.
	temps := map[int]float64{}
	for o := 0; o <= 11; o++ {
		temps[o] = 4
	}
	for o := 4; o <= 8; o++ {
		temps[o] = 10
	}
	ds := tempWalk(t, 8, temps)
	w := &ds.Walks[0]
	if a := w.Assess(ds.DayWithin(w, day, base, warm), ds.HourlyUntil); a.Verdict != verdict.No || !strings.Contains(a.Why, "within the limits is 5 h") {
		t.Fatalf("both edges cold = %+v", a)
	}
	if a := w.Assess(ds.DayFor(w, day, base), ds.HourlyUntil); a.Verdict != verdict.Go {
		t.Fatalf("no limits = %+v", a)
	}
	// Cold before, nothing after: the credit stands on the far side, so the
	// walk starts with the slot and finishes late.
	late := map[int]float64{0: 4, 1: 4, 2: 4, 3: 4, 4: 10, 5: 10, 6: 10, 7: 10, 8: 10}
	ds = tempWalk(t, 8, late)
	w = &ds.Walks[0]
	a := w.Assess(ds.DayWithin(w, day, base, warm), ds.HourlyUntil)
	slotStart := base.Add(4 * time.Hour)
	if a.Verdict != verdict.Marginal || !strings.Contains(a.Why, "in the dark") || !a.StartFrom.Equal(slotStart) || !a.StartBy.Equal(slotStart) {
		t.Fatalf("cold before = %+v", a)
	}
	// A cold hour mid-walk is the slot's one bad hour, and the why says so.
	ds = tempWalk(t, 3, map[int]float64{0: 10, 1: 10, 2: 3, 3: 10})
	w = &ds.Walks[0]
	if a := w.Assess(ds.DayWithin(w, day, base, warm), ds.HourlyUntil); a.Verdict != verdict.Marginal || !strings.Contains(a.Why, "one hour outside the limits") {
		t.Fatalf("cold gap = %+v", a)
	}
}
