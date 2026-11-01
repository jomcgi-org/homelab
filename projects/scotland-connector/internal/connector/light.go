package connector

import (
	"encoding/json"
	"math"
	"sort"
	"strings"
	"time"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/astro"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/stars"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/verdict"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/walks"
)

// LightParams uses plan's point, date and radius limits. The radius is a
// search ceiling, further limited to check's existing 30 km site reach.
type LightParams struct {
	Lat      *float64 `json:"lat,omitempty"`
	Lon      *float64 `json:"lon,omitempty"`
	RadiusKm *float64 `json:"radius_km,omitempty"`
	From     *string  `json:"from,omitempty"`
	To       *string  `json:"to,omitempty"`
	Kinds    []string `json:"kinds,omitempty"`
}

type LightQuery struct {
	Lat      float64  `json:"lat"`
	Lon      float64  `json:"lon"`
	RadiusKm float64  `json:"radius_km"`
	From     string   `json:"from"`
	To       string   `json:"to"`
	Kinds    []string `json:"kinds"`
}

type LightSite struct {
	ID          string  `json:"site_id"`
	Lat         float64 `json:"lat"`
	Lon         float64 `json:"lon"`
	DistanceKm  float64 `json:"distance_km"`
	FetchedAt   *string `json:"forecast_fetched_at"`
	HourlyUntil *string `json:"hourly_until"`
	Truncated   bool    `json:"truncated"`
}

// LightCloud describes positive sample support only. Missing rows never prove
// cloud. Fractions measure overlapping hourly buckets, not clear probability.
type LightCloud struct {
	Verdict          string         `json:"verdict"`
	Coverage         stars.Coverage `json:"coverage"`
	SupportedSeconds int64          `json:"supported_seconds"`
	Samples          int            `json:"samples"`
	Why              string         `json:"why,omitempty"`
}

type LightWindow struct {
	Kind  string     `json:"kind"`
	From  string     `json:"from"`
	Until string     `json:"until"`
	Night string     `json:"night"`
	Cloud LightCloud `json:"cloud"`
}

// Windows without sample support inherit the day's unknown cloud evidence.
// The in-memory value remains explicit for callers of the shared service.
func (w LightWindow) MarshalJSON() ([]byte, error) {
	type window LightWindow
	var cloud *LightCloud
	if w.Cloud.Samples > 0 {
		c := w.Cloud
		c.Why = ""
		cloud = &c
	}
	return json.Marshal(struct {
		window
		Cloud *LightCloud `json:"cloud,omitempty"`
	}{window(w), cloud})
}

type LightTwilight struct {
	Dawn *string `json:"dawn"`
	Dusk *string `json:"dusk"`
}

type LightMoon struct {
	Rise         *string `json:"rise"`
	Set          *string `json:"set"`
	ReferenceAt  string  `json:"reference_at"`
	Phase        float64 `json:"phase"`
	Direction    string  `json:"direction"`
	Illumination float64 `json:"illumination"`
}

type LightNight struct {
	Night    string          `json:"night"`
	Verdict  verdict.Verdict `json:"verdict"`
	Coverage stars.Coverage  `json:"coverage"`
	Why      string          `json:"why"`
}

type LightDay struct {
	Date         string        `json:"date"`
	Sunrise      *string       `json:"sunrise"`
	Sunset       *string       `json:"sunset"`
	Civil        LightTwilight `json:"civil"`
	Nautical     LightTwilight `json:"nautical"`
	Astronomical LightTwilight `json:"astronomical"`
	Moon         LightMoon     `json:"moon"`
	Windows      []LightWindow `json:"windows"`
	CloudWhy     string        `json:"cloud_why"`
}

var lightKinds = []string{"golden", "blue", "civil", "nautical", "astronomical", "darkness", "milky_way"}

func canonicalLightKinds(raw []string) ([]string, error) {
	if len(raw) == 0 {
		return append([]string{}, lightKinds...), nil
	}
	seen := map[string]bool{}
	for _, k := range raw {
		k = strings.TrimSpace(k)
		if !has(lightKinds, k) {
			return nil, &ParamError{"kinds", "each kind must be golden, blue, civil, nautical, astronomical, darkness or milky_way"}
		}
		seen[k] = true
	}
	var out []string
	for _, k := range lightKinds {
		if seen[k] {
			out = append(out, k)
		}
	}
	return out, nil
}

type LightResult struct {
	Query    LightQuery   `json:"query"`
	Timezone string       `json:"timezone"`
	Site     *LightSite   `json:"site"`
	Days     []LightDay   `json:"days"`
	Nights   []LightNight `json:"nights"`
}

type (
	lightInterval struct{ from, until time.Time }
	lightSample   struct {
		at    time.Time
		value float64
	}
)

type lightCurve struct {
	samples []lightSample
	at      func(time.Time) float64
}

var lightLondon = func() *time.Location {
	loc, err := time.LoadLocation("Europe/London")
	if err != nil {
		panic(err)
	}
	return loc
}()

// Sample each hour, then explicitly locate every bracketed extremum. Adding
// extrema prevents two crossings within a sample from disappearing near a
// solstice. Each of these altitude curves has one maximum and minimum per
// sidereal/solar day. Local quadratic extremum refinement and safeguarded
// secant crossings are capped; tests compare with a slow independent scan.
func newLightCurve(from, until time.Time, at func(time.Time) float64, seeds ...time.Time) lightCurve {
	c := lightCurve{at: at, samples: make([]lightSample, 0, 36)}
	for t := from.Add(-time.Hour); t.Before(until.Add(time.Hour)); t = t.Add(time.Hour) {
		c.samples = append(c.samples, lightSample{t, at(t)})
	}
	c.samples = append(c.samples, lightSample{until.Add(time.Hour), at(until.Add(time.Hour))})
	original := len(c.samples)
	for i := 1; i < original-1; i++ {
		a, b, d := c.samples[i-1], c.samples[i], c.samples[i+1]
		maximum := b.value > a.value && b.value > d.value
		if !maximum && !(b.value < a.value && b.value < d.value) {
			continue
		}
		// Reuse the three hourly values for the first quadratic fit.
		fraction := (a.value - d.value) / (2 * (a.value - 2*b.value + d.value))
		t, step := b.at.Add(time.Duration(fraction*float64(time.Hour))), 2*time.Minute
		left, mid, right := at(t.Add(-step)), at(t), at(t.Add(step))
		f := (left - right) / (2 * (left - 2*mid + right))
		if !math.IsNaN(f) && math.Abs(f) <= 1 {
			t = t.Add(time.Duration(f * float64(step)))
		}
		c.samples = append(c.samples, lightSample{t, at(t)})
	}
	for _, t := range seeds {
		if t.After(from) && t.Before(until) {
			c.samples = append(c.samples, lightSample{t, at(t)})
		}
	}
	kept := c.samples[:0]
	for _, p := range c.samples {
		if !p.at.Before(from) && !p.at.After(until) {
			kept = append(kept, p)
		}
	}
	c.samples = kept
	// until is not necessarily an integer number of hours from from on a
	// timezone transition; endpoints are always explicit.
	c.samples = append(c.samples, lightSample{from, at(from)}, lightSample{until, at(until)})
	sort.Slice(c.samples, func(i, j int) bool { return c.samples[i].at.Before(c.samples[j].at) })
	return c
}

func (c lightCurve) crossing(a, b lightSample, level float64) time.Time {
	for i := 0; i < 24 && b.at.Sub(a.at) > time.Second; i++ {
		f := (level - a.value) / (b.value - a.value)
		if f < 0.05 || f > 0.95 || math.IsNaN(f) {
			f = 0.5
		}
		t := a.at.Add(time.Duration(float64(b.at.Sub(a.at)) * f))
		m := lightSample{t, c.at(t)}
		if math.Abs(m.value-level)*float64(b.at.Sub(a.at))/math.Abs(b.value-a.value) < float64(time.Second)/4 {
			return t.Truncate(time.Second)
		}
		if (m.value >= level) == (a.value >= level) {
			a = m
		} else {
			b = m
		}
	}
	return a.at.Add(b.at.Sub(a.at) / 2).Truncate(time.Second)
}

func (c lightCurve) above(level float64) []lightInterval {
	var out []lightInterval
	var start time.Time
	if c.samples[0].value >= level {
		start = c.samples[0].at
	}
	for i := 1; i < len(c.samples); i++ {
		a, b := c.samples[i-1], c.samples[i]
		if (a.value >= level) == (b.value >= level) {
			continue
		}
		t := c.crossing(a, b, level)
		if b.value >= level {
			start = t
		} else {
			if t.After(start) {
				out = append(out, lightInterval{start, t})
			}
			start = time.Time{}
		}
	}
	end := c.samples[len(c.samples)-1].at
	if !start.IsZero() && end.After(start) {
		out = append(out, lightInterval{start, end})
	}
	return out
}

func lightIntersect(a, b []lightInterval) []lightInterval {
	var out []lightInterval
	for _, x := range a {
		for _, y := range b {
			lo, hi := x.from, x.until
			if y.from.After(lo) {
				lo = y.from
			}
			if y.until.Before(hi) {
				hi = y.until
			}
			if lo.Before(hi) {
				out = append(out, lightInterval{lo, hi})
			}
		}
	}
	return out
}

func lightComplement(a []lightInterval, from, until time.Time) []lightInterval {
	var out []lightInterval
	for _, x := range a {
		if from.Before(x.from) {
			out = append(out, lightInterval{from, x.from})
		}
		from = x.until
	}
	if from.Before(until) {
		out = append(out, lightInterval{from, until})
	}
	return out
}

func lightCrossings(above []lightInterval, from, until time.Time) LightTwilight {
	var out LightTwilight
	for _, x := range above {
		if x.from.After(from) {
			out.Dawn = timep(x.from)
		}
		if x.until.Before(until) {
			out.Dusk = timep(x.until)
		}
	}
	return out
}

func lightCloud(x lightInterval, eligible []lightInterval, ds *stars.Dataset, site *stars.Site, now time.Time) LightCloud {
	c := LightCloud{Verdict: "unknown", Coverage: stars.CoverageNone, Why: "No nearby snapshot site; astronomy is independent of weather."}
	if site == nil {
		return c
	}
	c.Why = "Only clear twilight-dark samples; gaps are unknown."
	until := site.Horizon(ds.HourlyUntil)
	if site.ForecastFetchedAt.IsZero() || site.ForecastFetchedAt.After(now) || !until.After(now) {
		c.Why = "Missing, future-dated or expired site forecast."
		return c
	}
	cutoff := now.UTC().Truncate(time.Hour)
	var supportedUntil time.Time
	for _, h := range site.Hours {
		t := time.Unix(h.Unix, 0)
		if t.Before(cutoff) || t.Before(site.ForecastFetchedAt.Truncate(time.Hour)) || t.Add(time.Hour).After(until) || h.Cloud < 0 || h.Cloud >= 10 {
			continue
		}
		counted := false
		// Parse sorts hours; eligible intervals are disjoint and ordered.
		// Union the overlapping buckets while scanning, including duplicates.
		for _, e := range eligible {
			lo, hi := maxTime(x.from, t, e.from), minTime(x.until, t.Add(time.Hour), e.until)
			if !lo.Before(hi) {
				continue
			}
			counted = true
			if supportedUntil.After(lo) {
				lo = supportedUntil
			}
			if lo.Before(hi) {
				c.SupportedSeconds += int64(hi.Sub(lo) / time.Second)
			}
			if hi.After(supportedUntil) {
				supportedUntil = hi
			}
		}
		if counted {
			c.Samples++
		}
	}
	if c.SupportedSeconds > 0 {
		c.Coverage = stars.CoveragePartial
	}
	if c.SupportedSeconds >= int64(x.until.Sub(x.from)/time.Second) {
		c.Coverage = stars.CoverageFull
		c.Verdict = "clear"
	}
	return c
}

func maxTime(a, b, c time.Time) time.Time {
	if b.After(a) {
		a = b
	}
	if c.After(a) {
		a = c
	}
	return a
}

func minTime(a, b, c time.Time) time.Time {
	if b.Before(a) {
		a = b
	}
	if c.Before(a) {
		a = c
	}
	return a
}

// Light computes geometry afresh, even with no snapshot. Dates own the local
// civil day [midnight, next midnight); NightDay labels each separate window.
func (s *Service) Light(p LightParams) (*Envelope, error) {
	now := s.Now()
	q, from, to, err := planQuery(PlanParams{Lat: p.Lat, Lon: p.Lon, RadiusKm: p.RadiusKm, From: p.From, To: p.To, Activities: []string{ActivityWalk}}, walks.LocalDay(now), stars.FirstNight(now))
	if err != nil {
		return nil, err
	}
	kinds, err := canonicalLightKinds(p.Kinds)
	if err != nil {
		return nil, err
	}
	query := LightQuery{q.Lat, q.Lon, q.RadiusKm, q.From, q.To, kinds}
	res := LightResult{Query: query, Timezone: "Europe/London", Days: []LightDay{}, Nights: []LightNight{}}
	var ds *stars.Dataset
	if s.Data != nil {
		ds = s.Data.Current()
	}
	var site *stars.Site
	if ds != nil {
		var km float64
		site, km = ds.Nearest(q.Lat, q.Lon, min(q.RadiusKm, float64(siteFarKm)))
		if site != nil {
			res.Site = &LightSite{site.ID, site.Lat, site.Lon, round(km, 2), timep(site.ForecastFetchedAt), timep(site.Horizon(ds.HourlyUntil)), site.Truncated()}
		}
	}
	nights := map[int32]bool{}
	for day := from; day <= to; day++ {
		y, m, d := time.Unix(int64(day)*86400, 0).UTC().Date()
		start := time.Date(y, m, d, 0, 0, 0, 0, lightLondon)
		end := start.AddDate(0, 0, 1)
		rise, set, _ := astro.SunTimes(time.Unix(int64(day)*86400, 0), q.Lat, q.Lon)
		sun := newLightCurve(start, end, func(t time.Time) float64 { return astro.SunElevation(t, q.Lat, q.Lon) }, rise, set)
		moon := newLightCurve(start, end, func(t time.Time) float64 { return astro.MoonAltitude(t, q.Lat, q.Lon) })
		core := newLightCurve(start, end, func(t time.Time) float64 { return astro.GalacticCentreAltitude(t, q.Lat, q.Lon) })
		above := map[float64][]lightInterval{}
		for _, level := range []float64{-18, -12, -10, -6, -4, -0.833, 6} {
			above[level] = sun.above(level)
		}
		solar := lightCrossings(above[-0.833], start, end)
		lunar := lightCrossings(moon.above(-0.833), start, end)
		ref := time.Date(y, m, d, 12, 0, 0, 0, lightLondon)
		phase := astro.MoonAt(ref).Phase()
		direction := "waxing"
		if phase >= 0.5 {
			direction = "waning"
		}
		out := LightDay{Date: dayName(day), Sunrise: solar.Dawn, Sunset: solar.Dusk, Civil: lightCrossings(above[-6], start, end), Nautical: lightCrossings(above[-12], start, end), Astronomical: lightCrossings(above[-18], start, end), Moon: LightMoon{lunar.Dawn, lunar.Dusk, rfc3339(ref), round(phase, 4), direction, round(astro.MoonIllumination(ref), 4)}, Windows: []LightWindow{}}
		below := func(level float64) []lightInterval { return lightComplement(above[level], start, end) }
		bands := []struct {
			kind      string
			intervals []lightInterval
		}{
			{"civil_twilight", lightIntersect(above[-6], below(-0.833))},
			{"nautical_twilight", lightIntersect(above[-12], below(-6))},
			{"astronomical_twilight", lightIntersect(above[-18], below(-12))},
			{"golden", lightIntersect(above[-4], below(6))},
			{"blue", lightIntersect(above[-6], below(-4))},
			{"astronomical_darkness", below(-18)},
			// Match stars' nautical darkness. Scottish July can retain
			// astronomical twilight throughout the night.
			{"milky_way", lightIntersect(core.above(0), below(-12))},
		}
		eligible := below(-10)
		out.CloudWhy = lightCloud(lightInterval{start, end}, nil, ds, site, now).Why
		beforeNoon, afterNoon := stars.NightDay(start), stars.NightDay(end.Add(-time.Second))
		for _, band := range bands {
			kind := strings.TrimSuffix(band.kind, "_twilight")
			if kind == "astronomical_darkness" {
				kind = "darkness"
			}
			if !has(kinds, kind) {
				continue
			}
			for _, x := range band.intervals {
				n := afterNoon
				if x.from.Add(x.until.Sub(x.from) / 2).Before(start.Add(12 * time.Hour)) {
					n = beforeNoon
				}
				nights[n] = true
				out.Windows = append(out.Windows, LightWindow{band.kind, rfc3339(x.from), rfc3339(x.until), stars.NightName(n), lightCloud(x, eligible, ds, site, now)})
			}
		}
		sort.Slice(out.Windows, func(i, j int) bool {
			a, b := out.Windows[i], out.Windows[j]
			if a.From != b.From {
				return a.From < b.From
			}
			return a.Kind < b.Kind
		})
		res.Days = append(res.Days, out)
	}
	var ordered []int32
	for night := range nights {
		ordered = append(ordered, night)
	}
	sort.Slice(ordered, func(i, j int) bool { return ordered[i] < ordered[j] })
	firstNight := stars.FirstNight(now)
	for _, night := range ordered {
		n := LightNight{Night: stars.NightName(night), Verdict: verdict.Unknown, Coverage: stars.CoverageNone, Why: "No nearby snapshot site."}
		if night < firstNight {
			// Morning geometry can refer to a night whose forecast hours
			// have already been dropped. AssessNight only covers live nights.
			n.Why = "This night has ended; the current snapshot cannot assess it."
		} else if site != nil {
			n.Why = "No night assessment for the selected snapshot site."
			ws := ds.Windows(stars.WindowsQuery{Lat: site.Lat, Lon: site.Lon, RadiusKm: 0.01, Night: night, Nights: 1, Limit: 1, Now: now})
			// Clear-hour ranking can prefer a different co-located site.
			if len(ws) > 0 && ws[0].Site == site {
				a := ds.AssessNight(site, &ws[0].Nights[0], night, now.UTC().Truncate(time.Hour), ws[0].Climate, stars.NightAssessmentOptions{OmitMoonAdvice: true})
				n.Verdict, n.Coverage, n.Why = a.Verdict, a.Coverage, a.Why
			}
			if site.ForecastFetchedAt.IsZero() || site.ForecastFetchedAt.After(now) || !site.Horizon(ds.HourlyUntil).After(now) {
				n.Verdict, n.Coverage, n.Why = verdict.Unknown, stars.CoverageNone, "Missing, future-dated or expired site forecast."
			}
		}
		res.Nights = append(res.Nights, n)
	}
	sources := []Source{{Name: "Local astronomical calculation", Attribution: "Truncated Meeus lunar/solar series; NOAA sunrise equation", License: "MPL 2.0", URL: "https://github.com/jomcgi-org/homelab/tree/main/projects/scotland-connector/internal/astro", Use: "light and lunar geometry"}}
	if site != nil {
		sources = append(sources, sourceMET, sourceCERRA, sourceOSM, sourceNaturalEarth)
	}
	meta := s.meta(ds, nil, now, sources)
	meta.Notes = []string{"Light release acceptance: #7367. Missing cloud: unknown/none, zero samples. Nights assess suitability. See method_url."}
	// Exact freshness boundaries can fall inside an as-of hour. Include that
	// state so within-hour expiry never revalidates a changed cloud answer.
	freshness := "unknown"
	if site != nil && !site.ForecastFetchedAt.IsZero() && !site.ForecastFetchedAt.After(now) && site.Horizon(ds.HourlyUntil).After(now) {
		freshness = "fresh"
	}
	return &Envelope{Data: res, Meta: meta, ETag: etag("light-v2", meta.DataVersion, meta.AsOf, freshness, canonical(query)), Results: len(res.Days)}, nil
}
