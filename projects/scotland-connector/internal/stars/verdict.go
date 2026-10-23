package stars

import (
	"fmt"
	"time"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/astro"
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
	case n.ClearDark > 0 && n.MoonlessDark == n.ClearDark:
		return "the moon below the horizon"
	case n.MoonlessDark == 0:
		return fmt.Sprintf("under a %d%% moon", pct)
	}
	return fmt.Sprintf("the %d%% moon up for %d of them", pct, n.ClearDark-n.MoonlessDark)
}

// AssessNight gives the verdict for one site on one night. climate, when
// known, is quoted for nights past the forecast.
func (d *Dataset) AssessNight(site *Site, n *NightWindow, night int32, now time.Time, climate *Climate) (a NightAssessment) {
	a = NightAssessment{Coverage: d.NightCoverage(night)}
	defer func() {
		if n.ClearDark == 0 {
			a.Why += " " + MoonAdvice(site.Lat, site.Lon, night, now)
		} else if n.MoonLit >= 0.8 && n.MoonlessDark < n.ClearDark {
			a.Why += " Bright moonlight limits faint objects; lunar and planetary viewing can still be worthwhile if skies clear."
		}
	}()
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
	if first, ok := n.ClearFrom(); ok {
		from = " from " + first.In(london).Format("15:04")
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
	case n.ClearTwilight == 0 && now.After(localHour(night, nightStartHour)):
		// Earlier hours have been dropped, so "all night" would overstate.
		a.Verdict, a.Why = verdict.No, "No clear hour left tonight."
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

// ClearFrom is when the night's clear spell starts: its first clear-dark
// hour, or its first clear-twilight hour when none is dark.
func (n *NightWindow) ClearFrom() (time.Time, bool) {
	for _, h := range n.Hours {
		if h.Dark || n.ClearDark == 0 {
			return time.Unix(h.Unix, 0), true
		}
	}
	return time.Time{}, false
}

// ClearUntil is the end of the night's last clear-dark hour, or of its last
// clear-twilight hour when none is dark.
func (n *NightWindow) ClearUntil() (time.Time, bool) {
	for i := len(n.Hours) - 1; i >= 0; i-- {
		if h := n.Hours[i]; h.Dark || n.ClearDark == 0 {
			return time.Unix(h.Unix+3600, 0), true
		}
	}
	return time.Time{}, false
}

// CappedNear is the nearest site within radiusKm whose capped hour list
// ends before night does, so that its verdict for night is unknown however
// few hours it lists; nil when there is none. Windows ranks sites on listed
// counts and can pass such a site over: by being capped it is among the
// clearest.
func (d *Dataset) CappedNear(lat, lon, radiusKm float64, night int32) *Site {
	end := localHour(night+1, nightEndHour)
	var best *Site
	bestKm := 0.0
	d.visit(lat, lon, radiusKm, func(s *Site, km float64) {
		if s.Truncated() && s.listedUntil().Before(end) && (best == nil || km < bestKm || km == bestKm && s.ID < best.ID) {
			best, bestKm = s, km
		}
	})
	return best
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

// MoonAdvice uses geometry independently of the forecast's clear-hour list.
// Ten-minute samples describe the remaining sunset-to-sunrise interval, even
// on twilight-only nights. Civil dates and zone labels disambiguate DST.
func MoonAdvice(lat, lon float64, night int32, now time.Time) string {
	day := time.Unix(int64(night)*86400, 0)
	_, set, ok := astro.SunTimes(day, lat, lon)
	rise, _, nextOK := astro.SunTimes(day.AddDate(0, 0, 1), lat, lon)
	lit := NightMoonLit(night)
	prefix := fmt.Sprintf("Moon illumination about %.0f%% at local midnight.", lit*100)
	if !ok || !nextOK {
		return prefix + " Nighttime horizon geometry unavailable."
	}
	from := set
	if now.After(from) {
		from = now
	}
	if !from.Before(rise) {
		return prefix + " No sunset-to-sunrise interval remains for this night."
	}
	total, up := 0, 0
	for at := from; at.Before(rise); at = at.Add(10 * time.Minute) {
		total++
		if astro.MoonAltitude(at, lat, lon) > 0 {
			up++
		}
	}
	prefix += fmt.Sprintf(" At ten-minute checks between %s and %s, the moon is above the horizon at %d of %d samples (independent of cloud).", from.In(london).Format("Mon 15:04 MST"), rise.In(london).Format("Mon 15:04 MST"), up, total)
	if lit >= 0.8 && up > 0 {
		prefix += " Bright moonlight limits faint objects while it is up; lunar and planetary viewing can still be worthwhile if skies clear."
	}
	return prefix
}
