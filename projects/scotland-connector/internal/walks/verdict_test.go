package walks

import (
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
	dark := assessDay(t, 9, 0, 1, 2, 3, 4, 5)
	if dark.Verdict != verdict.Marginal || !strings.Contains(dark.Why, "in the dark") || !dark.StartFrom.Before(base) {
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
