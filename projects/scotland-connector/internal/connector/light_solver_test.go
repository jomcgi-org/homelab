package connector

import (
	"math"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/astro"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/stars"
)

// Independent test oracle: one-minute full scan, pure bisection of each
// transition, no production knots, extremum fit or secant method.
func slowLightAbove(from, until time.Time, at func(time.Time) float64, level float64) []lightInterval {
	var out []lightInterval
	var start time.Time
	if at(from) >= level {
		start = from
	}
	for left := from; left.Before(until); {
		right := left.Add(time.Minute)
		if right.After(until) {
			right = until
		}
		if (at(left) >= level) != (at(right) >= level) {
			lo, hi := left, right
			for hi.Sub(lo) > 100*time.Millisecond {
				mid := lo.Add(hi.Sub(lo) / 2)
				if (at(mid) >= level) == (at(lo) >= level) {
					lo = mid
				} else {
					hi = mid
				}
			}
			root := lo.Add(hi.Sub(lo) / 2)
			if at(right) >= level {
				start = root
			} else {
				out = append(out, lightInterval{start, root})
				start = time.Time{}
			}
		}
		left = right
	}
	if !start.IsZero() {
		out = append(out, lightInterval{start, until})
	}
	return out
}

func TestLightSolverAgainstSlowOracle(t *testing.T) {
	for month := 1; month <= 12; month++ {
		for _, lat := range []float64{54.5, 57.3, 61} {
			start := time.Date(2026, time.Month(month), 21, 0, 0, 0, 0, lightLondon)
			end := start.AddDate(0, 0, 1)
			for _, kind := range []string{"sun", "moon", "core"} {
				at := func(t time.Time) float64 { return astro.SunElevation(t, lat, -4.45) }
				levels := []float64{-18, -12, -10, -6, -4, -0.833, 6}
				if kind == "moon" {
					at = func(t time.Time) float64 { return astro.MoonAltitude(t, lat, -4.45) }
					levels = []float64{-0.833}
				}
				if kind == "core" {
					at = func(t time.Time) float64 { return astro.GalacticCentreAltitude(t, lat, -4.45) }
					levels = []float64{0}
				}
				c := newLightCurve(start, end, at)
				for _, level := range levels {
					got, want := c.above(level), slowLightAbove(start, end, at, level)
					if len(got) != len(want) {
						t.Fatalf("%d lat %g %s level %g: %+v want %+v", month, lat, kind, level, got, want)
					}
					for i := range got {
						if math.Abs(got[i].from.Sub(want[i].from).Seconds()) > 2 || math.Abs(got[i].until.Sub(want[i].until).Seconds()) > 2 {
							t.Fatalf("%d lat %g %s level %g: %+v want %+v", month, lat, kind, level, got, want)
						}
					}
				}
			}
		}
	}
}

func TestLightSolverGrazingAndAllDay(t *testing.T) {
	start := time.Date(2026, 6, 21, 0, 0, 0, 0, time.UTC)
	end := start.AddDate(0, 0, 1)
	for _, offset := range []time.Duration{10 * time.Minute, 12 * time.Hour, 23*time.Hour + 55*time.Minute} {
		peak := start.Add(offset)
		at := func(t time.Time) float64 { x := t.Sub(peak).Seconds(); return 1 - x*x/100 }
		got := newLightCurve(start, end, at).above(0)
		if len(got) != 1 || math.Abs(got[0].from.Sub(peak.Add(-10*time.Second)).Seconds()) > 2 || math.Abs(got[0].until.Sub(peak.Add(10*time.Second)).Seconds()) > 2 {
			t.Fatalf("lost grazing crossing %v: %+v", offset, got)
		}
	}
	for _, v := range []float64{-1, 1} {
		got := newLightCurve(start, end, func(time.Time) float64 { return v }).above(0)
		if v < 0 && len(got) != 0 || v > 0 && (len(got) != 1 || !got[0].from.Equal(start) || !got[0].until.Equal(end)) {
			t.Fatal(got)
		}
	}
}

func TestLightAboveSubsecondGrazingDoesNotExtendToDayEnd(t *testing.T) {
	start := time.Date(2026, 6, 21, 0, 0, 0, 0, time.UTC)
	end := start.AddDate(0, 0, 1)
	peak := start.Add(12*time.Hour + 500*time.Millisecond)
	at := func(t time.Time) float64 {
		x := t.Sub(peak).Seconds()
		return 0.04 - x*x
	}
	c := lightCurve{at: at}
	for _, t := range []time.Time{start, peak.Add(-400 * time.Millisecond), peak, peak.Add(400 * time.Millisecond), end} {
		c.samples = append(c.samples, lightSample{t, at(t)})
	}
	// Both crossings truncate to the same second. Dropping that empty
	// interval must also clear its start, with no later rise to replace it.
	if got := c.above(0); len(got) != 0 {
		t.Fatalf("sub-second grazing peak produced above intervals: %+v", got)
	}
}

func TestLightIntervalCloudScope(t *testing.T) {
	start := time.Date(2026, 10, 19, 20, 0, 0, 0, time.UTC)
	now := start.Add(-time.Hour)
	x := lightInterval{start, start.Add(time.Hour)}
	site := &stars.Site{ForecastFetchedAt: now, Hours: []stars.Hour{{Unix: start.Unix(), Cloud: 2, Dark: true}}}
	ds := &stars.Dataset{HourlyUntil: now.Add(48 * time.Hour)}
	if c := lightCloud(x, []lightInterval{x}, ds, site, now); c.Verdict != "clear" || c.SupportedSeconds != 3600 {
		t.Fatal(c)
	}
	if c := lightCloud(x, nil, ds, site, now); c.Verdict != "unknown" || c.Samples != 0 {
		t.Fatal("daylight", c)
	}
	longer := lightInterval{start, start.Add(2 * time.Hour)}
	if c := lightCloud(longer, []lightInterval{longer}, ds, site, now); c.Verdict != "unknown" || c.Coverage != stars.CoveragePartial {
		t.Fatal("missing rows prove no cloud", c)
	}
	site.ClearTwilight = 100
	if c := lightCloud(longer, []lightInterval{longer}, ds, site, now); c.Verdict != "unknown" {
		t.Fatal("truncated", c)
	}
	ds.HourlyUntil = start.Add(30 * time.Minute)
	if c := lightCloud(x, []lightInterval{x}, ds, site, now); c.Samples != 0 {
		t.Fatal("partial horizon", c)
	}
}

func TestLightNightOptionPreservesAssessment(t *testing.T) {
	start := time.Date(2026, 10, 19, 14, 0, 0, 0, time.UTC)
	site := &stars.Site{ForecastFetchedAt: start}
	ds := &stars.Dataset{HourlyUntil: start.Add(48 * time.Hour)}
	for _, n := range []stars.NightWindow{{}, {ClearDark: 4, ClearTwilight: 4, MoonlessDark: 4}, {ClearDark: 1, ClearTwilight: 2, MoonLit: 0.9}} {
		for _, night := range []int32{stars.NightDay(start), stars.NightDay(start) + 5} {
			a := ds.AssessNight(site, &n, night, start, nil)
			b := ds.AssessNight(site, &n, night, start, nil, stars.NightAssessmentOptions{OmitMoonAdvice: true})
			if a.Verdict != b.Verdict || a.Coverage != b.Coverage {
				t.Fatal(a, b)
			}
		}
	}
}
