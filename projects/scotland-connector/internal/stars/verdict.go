package stars

import (
	"fmt"
	"time"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/verdict"
)

// Coverage says how much of a night the hourly forecast reaches.
type Coverage string

const (
	CoverageFull    Coverage = "full"
	CoveragePartial Coverage = "partial"
	CoverageNone    Coverage = "none"
)

// Night bounds for coverage, in UK local time: no Scottish night is dark
// before 16:00 or after 08:00.
const (
	nightStartHour = 16
	nightEndHour   = 8
)

func localHour(day int32, hour int) time.Time {
	y, m, d := time.Unix(int64(day)*86400, 0).UTC().Date()
	return time.Date(y, m, d, hour, 0, 0, 0, london)
}

// NightCoverage is how much of night (a NightDay) lies before the hourly
// horizon.
func (d *Dataset) NightCoverage(night int32) Coverage {
	switch {
	case !d.HourlyUntil.Before(localHour(night+1, nightEndHour)):
		return CoverageFull
	case !d.HourlyUntil.After(localHour(night, nightStartHour)):
		return CoverageNone
	}
	return CoveragePartial
}

// DarkScore weighs a night's clear-dark hours by the moon: a moonless hour
// counts 1 and a moonlit one 1 minus the moon's lit fraction, so a long
// clear night under a thin crescent beats one moonless hour, and a short
// moonless night beats a long one under a full moon.
func DarkScore(n *NightWindow) float64 {
	return float64(n.MoonlessDark) + float64(n.ClearDark-n.MoonlessDark)*(1-n.MoonLit)
}

// goodScore is the DarkScore of a night worth the drive: three hours of
// properly dark sky.
const goodScore = 3

// NightAssessment is a night's verdict and its reason.
type NightAssessment struct {
	Verdict  verdict.Verdict
	Why      string
	Coverage Coverage
}

func localDay(t time.Time) string { return t.In(london).Format("Mon 15:04") }

func moonPhrase(n *NightWindow) string {
	pct := int(n.MoonLit*100 + 0.5)
	switch {
	case n.MoonLit < MoonlessMaxLit:
		return "no moon to speak of"
	case n.MoonlessDark == n.ClearDark:
		return "the moon below the horizon"
	case n.MoonlessDark == 0:
		return fmt.Sprintf("under a %d%% moon", pct)
	}
	return fmt.Sprintf("the %d%% moon up for %d of them", pct, n.ClearDark-n.MoonlessDark)
}

// AssessNight gives the verdict for one site on one night. climate, when
// known, is quoted for nights past the forecast.
func (d *Dataset) AssessNight(site *Site, n *NightWindow, night int32, climate *Climate) NightAssessment {
	a := NightAssessment{Coverage: d.NightCoverage(night)}
	if a.Coverage == CoverageNone {
		a.Verdict, a.Why = verdict.Unknown, "Beyond the hourly forecast, which reaches "+localDay(d.HourlyUntil)+"."
		if climate != nil && climate.Dark > 0 {
			a.Why += fmt.Sprintf(" In %s about %d%% of dark hours here are clear.",
				time.Month(climate.Month), (100*climate.ClearDark+climate.Dark/2)/climate.Dark)
		}
		return a
	}
	score := DarkScore(n)
	from := ""
	if len(n.Hours) > 0 {
		from = " from " + time.Unix(n.Hours[0].Unix, 0).In(london).Format("15:04")
	}
	switch {
	case score >= goodScore:
		a.Verdict = verdict.Go
		a.Why = fmt.Sprintf("%s%s, %s.", hours(n.ClearDark, "clear dark hour"), from, moonPhrase(n))
	case a.Coverage == CoveragePartial:
		a.Verdict = verdict.Unknown
		a.Why = "The hourly forecast reaches only " + localDay(d.HourlyUntil) + "; the rest of the night is not forecast yet."
	case site.Truncated() && site.listedUntil().Before(localHour(night+1, nightEndHour)):
		// The snapshot lists a site's first clear hours only (#7246), so a
		// night past the last listed hour reads as fewer than it has.
		a.Verdict = verdict.Unknown
		a.Why = "The data lists this site's clear hours only up to " + localDay(site.listedUntil()) + "; the rest of the night is not counted yet."
	case n.ClearTwilight == 0:
		a.Verdict, a.Why = verdict.No, "Cloudy all night."
	case n.ClearDark == 0:
		a.Verdict = verdict.Marginal
		a.Why = fmt.Sprintf("Clear for %s%s, but only twilight-dark.", hours(n.ClearTwilight, "hour"), from)
	case n.ClearDark < goodScore:
		a.Verdict = verdict.Marginal
		a.Why = fmt.Sprintf("Only %s%s, %s.", hours(n.ClearDark, "clear dark hour"), from, moonPhrase(n))
	default:
		a.Verdict = verdict.Marginal
		a.Why = fmt.Sprintf("%s%s, but %s.", hours(n.ClearDark, "clear dark hour"), from, moonPhrase(n))
	}
	return a
}

// listedUntil is the end of the site's last listed hour.
func (s *Site) listedUntil() time.Time {
	if len(s.Hours) == 0 {
		return time.Time{}
	}
	return time.Unix(s.Hours[len(s.Hours)-1].Unix+3600, 0)
}

// hours counts hours in words: "1 clear dark hour", "3 clear dark hours".
func hours(n int, unit string) string {
	if n == 1 {
		return "1 " + unit
	}
	return fmt.Sprintf("%d %ss", n, unit)
}
