package walks

import (
	"fmt"
	"strings"
	"time"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/verdict"
)

// windyKmh is the sustained wind at the start above which a day that fits is
// marginal rather than go: around Beaufort 7, unpleasant low down and
// serious on any ridge.
const windyKmh = 50

// Assessment is a verdict with its reason and, when the walk fits, the
// window to start in.
type Assessment struct {
	Verdict verdict.Verdict
	Why     string
	// StartFrom and StartBy bound the start time that keeps the walk inside
	// the best slot; zero unless the walk fits. StartFrom is earlier than the
	// slot when a long walk needs its dark-shoulder credit.
	StartFrom, StartBy time.Time
}

func local(t time.Time) string { return t.In(london).Format("15:04") }

func localDay(t time.Time) string { return t.In(london).Format("Mon 15:04") }

// outcome is which verdict rule decided a day.
type outcome int

const (
	beyondForecast outcome = iota
	forecastEnds
	noGoodHour
	tooShort
	darkShoulder
	badHourInside
	windyStart
	fits
)

func (o outcome) verdict() verdict.Verdict {
	switch o {
	case beyondForecast, forecastEnds:
		return verdict.Unknown
	case noGoodHour, tooShort:
		return verdict.No
	case darkShoulder, badHourInside, windyStart:
		return verdict.Marginal
	}
	return verdict.Go
}

func (w *Walk) outcome(d Day) outcome {
	switch {
	case !d.Decided && d.Coverage == CoverageNone:
		return beyondForecast
	case !d.Decided:
		return forecastEnds
	case !d.Doable && d.GoodHours == 0:
		return noGoodHour
	case !d.Doable:
		return tooShort
	}
	b := d.Best
	switch {
	case float64(b.GoodHours) < w.required-requirementEpsilon:
		return darkShoulder
	case int(b.End.Sub(b.Start)/time.Hour) > b.GoodHours:
		return badHourInside
	case b.MaxWindKmh > windyKmh:
		return windyStart
	}
	return fits
}

// Verdict is Assess's verdict alone, without building its reason: what a
// ranking over many walks needs.
func (w *Walk) Verdict(d Day) verdict.Verdict { return w.outcome(d).verdict() }

// Assess turns one assessed day into a verdict. hourlyUntil is the dataset's
// forecast horizon, quoted when the day cannot be decided.
func (w *Walk) Assess(d Day, hourlyUntil time.Time) Assessment {
	o := w.outcome(d)
	a := Assessment{Verdict: o.verdict()}
	switch o {
	case beyondForecast:
		a.Why = "Beyond the hourly forecast, which reaches " + localDay(hourlyUntil) + "."
		return a
	case forecastEnds:
		a.Why = "The hourly forecast reaches only " + localDay(hourlyUntil) + ", with no slot long enough before then."
		return a
	case noGoodHour:
		hour, left := "No dry, calm daylight hour", ""
		if d.Limited {
			hour = "No daylight hour within the limits"
		}
		if d.Started {
			left = " left today"
		}
		a.Why = hour + left + " at the start."
		return a
	case tooShort:
		spell, left := "good spell", ""
		if d.Limited {
			spell = "spell within the limits"
		}
		if d.Started {
			left = " left today"
		}
		a.Why = fmt.Sprintf("The longest %s%s is %d h; the walk takes about %.1f h.", spell, left, d.Best.GoodHours, w.DurationH)
		return a
	}
	b := d.Best
	a.StartFrom = b.Start
	a.StartBy = b.End.Add(-time.Duration(w.DurationH * float64(time.Hour))).Truncate(time.Minute)
	if a.StartBy.Before(a.StartFrom) {
		// It needs more than the slot: start early, unless its dark credit
		// can only be spent after the slot (see limitedEdges).
		a.StartFrom = a.StartBy
		if o == darkShoulder && d.DarkLateOnly {
			a.StartFrom, a.StartBy = b.Start, b.Start
		}
	}
	// A walk that needs more than the slot starts before it, but never
	// before the first hour that counts.
	if a.StartFrom.Before(d.From) {
		a.StartFrom = d.From
	}
	if a.StartBy.Before(a.StartFrom) {
		a.StartBy = a.StartFrom
	}
	weather := fmt.Sprintf("wind up to %.0f km/h", b.MaxWindKmh)
	if b.TotalPrecipMM >= WetMM {
		weather += fmt.Sprintf(", %.1f mm of rain", b.TotalPrecipMM)
	} else {
		weather += ", dry"
	}
	switch o {
	case darkShoulder:
		a.Why = fmt.Sprintf("Fits only by starting or finishing in the dark: %s %s to %s, %s.", goodHours(b.GoodHours), local(b.Start), local(b.End), weather)
	case badHourInside:
		bad := "one wet or windy hour"
		if d.GapOutsideLimits {
			// The weather sums cover the good hours, not the one turned away.
			bad, weather = "one hour outside the limits", strings.Replace(weather, ", dry", ", otherwise dry", 1)
		}
		a.Why = fmt.Sprintf("Fits with %s mid-walk: %s to %s, %s.", bad, local(b.Start), local(b.End), weather)
	case windyStart:
		a.Why = fmt.Sprintf("Fits, but windy at the start: %s to %s, %s.", local(b.Start), local(b.End), weather)
	default:
		a.Why = fmt.Sprintf("%s %s to %s, %s.", goodHours(b.GoodHours), local(b.Start), local(b.End), weather)
	}
	return a
}

func goodHours(n int) string {
	if n == 1 {
		return "1 good hour"
	}
	return fmt.Sprintf("%d good hours", n)
}
