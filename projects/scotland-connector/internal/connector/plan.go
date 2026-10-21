package connector

import (
	"fmt"
	"math"
	"sort"
	"strings"
	"time"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/stars"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/verdict"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/walks"
)

// PlanParams are the plan inputs: where, when, what, which walks and the
// weather a walk needs.
type PlanParams struct {
	Lat        *float64 `json:"lat,omitempty"`
	Lon        *float64 `json:"lon,omitempty"`
	RadiusKm   *float64 `json:"radius_km,omitempty"`
	From       *string  `json:"from,omitempty"`
	To         *string  `json:"to,omitempty"`
	Activities []string `json:"activities,omitempty"`
	WalkFilter
	WeatherLimits
	Limit *int `json:"limit,omitempty"`
}

// WalkFilter narrows a plan's walks by their Walkhighlands facts.
type WalkFilter struct {
	MinDurationH *float64 `json:"min_duration_h,omitempty"`
	MaxDurationH *float64 `json:"max_duration_h,omitempty"`
	MinLengthKm  *float64 `json:"min_length_km,omitempty"`
	MaxLengthKm  *float64 `json:"max_length_km,omitempty"`
	MaxAscentM   *int     `json:"max_ascent_m,omitempty"`
}

// PlanQuery echoes the canonical inputs used.
type PlanQuery struct {
	Lat        float64  `json:"lat"`
	Lon        float64  `json:"lon"`
	RadiusKm   float64  `json:"radius_km"`
	From       string   `json:"from"`
	To         string   `json:"to"`
	Activities []string `json:"activities"`
	WalkFilter
	WeatherLimits
	Limit int `json:"limit"`
}

// PlanDay is one day of a plan: the sun and moon at the point, then the best
// walks and the best night's stargazing, each with a verdict. A list is
// empty when nothing fits; its note says why.
type PlanDay struct {
	Date string `json:"date"`
	SkyOut
	Walks      []WalkOption  `json:"walks,omitempty"`
	WalkNote   string        `json:"walk_note,omitempty"`
	Stargazing []NightOption `json:"stargazing,omitempty"`
	StarNote   string        `json:"stargazing_note,omitempty"`
}

// PlanResult is plan's data.
type PlanResult struct {
	Query PlanQuery `json:"query"`
	Days  []PlanDay `json:"days"`
}

func activities(raw []string) ([]string, error) {
	if len(raw) == 0 {
		return []string{ActivityWalk, ActivityStargaze}, nil
	}
	seen := map[string]bool{}
	for _, a := range raw {
		a = strings.TrimSpace(a)
		if a != ActivityWalk && a != ActivityStargaze {
			return nil, &ParamError{"activities", `each one of "walk" or "stargaze"`}
		}
		seen[a] = true
	}
	var out []string
	for _, a := range []string{ActivityWalk, ActivityStargaze} {
		if seen[a] {
			out = append(out, a)
		}
	}
	return out, nil
}

// walkFilter validates and rounds a filter; a minimum above its maximum is
// the minimum's error.
func walkFilter(f WalkFilter) (WalkFilter, error) {
	var out WalkFilter
	between := func(v *float64, param string, lo, hi float64) (*float64, error) {
		if v == nil {
			return nil, nil
		}
		if math.IsNaN(*v) || *v < lo || *v > hi {
			return nil, &ParamError{param, fmt.Sprintf("between %g and %g", lo, hi)}
		}
		return rounded(v, 1), nil
	}
	var err error
	if out.MinDurationH, err = between(f.MinDurationH, "min_duration_h", 0.5, MaxDurationH); err != nil {
		return out, err
	}
	if out.MaxDurationH, err = between(f.MaxDurationH, "max_duration_h", 0.5, MaxDurationH); err != nil {
		return out, err
	}
	if out.MinLengthKm, err = between(f.MinLengthKm, "min_length_km", 0.5, MaxLengthKm); err != nil {
		return out, err
	}
	if out.MaxLengthKm, err = between(f.MaxLengthKm, "max_length_km", 0.5, MaxLengthKm); err != nil {
		return out, err
	}
	if out.MinDurationH != nil && out.MaxDurationH != nil && *out.MinDurationH > *out.MaxDurationH {
		return out, &ParamError{"min_duration_h", "at most max_duration_h"}
	}
	if out.MinLengthKm != nil && out.MaxLengthKm != nil && *out.MinLengthKm > *out.MaxLengthKm {
		return out, &ParamError{"min_length_km", "at most max_length_km"}
	}
	if a := f.MaxAscentM; a != nil {
		if *a < 1 || *a > MaxAscentM {
			return out, &ParamError{"max_ascent_m", fmt.Sprintf("between 1 and %d", MaxAscentM)}
		}
		out.MaxAscentM = a
	}
	return out, nil
}

func (f WalkFilter) matches(w *walks.Walk) bool {
	return (f.MinDurationH == nil || w.DurationH >= *f.MinDurationH) &&
		(f.MaxDurationH == nil || w.DurationH <= *f.MaxDurationH) &&
		(f.MinLengthKm == nil || w.LengthKm >= *f.MinLengthKm) &&
		(f.MaxLengthKm == nil || w.LengthKm <= *f.MaxLengthKm) &&
		(f.MaxAscentM == nil || w.AscentM <= *f.MaxAscentM)
}

// planQuery validates a plan. Days run from today (UK date) to MaxDaysAhead
// out, except that before 09:00 the night still under way is yesterday's: a
// plan may start on it, and does by default when it covers stargazing.
func planQuery(p PlanParams, today, tonight int32) (q PlanQuery, from, to int32, err error) {
	if q.Lat, q.Lon, err = coordinates(p.Lat, p.Lon); err != nil {
		return
	}
	q.RadiusKm = DefaultRadiusKm
	if p.RadiusKm != nil {
		r := *p.RadiusKm
		if math.IsNaN(r) || r < 1 || r > MaxRadiusKm {
			err = &ParamError{"radius_km", fmt.Sprintf("between 1 and %d", MaxRadiusKm)}
			return
		}
		q.RadiusKm = round(r, 1)
	}
	if q.Activities, err = activities(p.Activities); err != nil {
		return
	}
	// The night under way is a day to plan only for stargazing.
	earliest := today
	if has(q.Activities, ActivityStargaze) {
		earliest = tonight
	}
	from = earliest
	if p.From != nil && strings.TrimSpace(*p.From) != "" {
		if from, err = parseDay("from", *p.From, earliest, today+MaxDaysAhead); err != nil {
			return
		}
	}
	// The default span counts from today, so a plan that starts on last
	// night still covers three days of walks.
	to = min(max(from, today)+DefaultSpanDays-1, today+MaxDaysAhead)
	if p.To != nil && strings.TrimSpace(*p.To) != "" {
		if to, err = parseDay("to", *p.To, from, min(from+MaxSpanDays-1, today+MaxDaysAhead)); err != nil {
			return
		}
	}
	q.From, q.To = dayName(from), dayName(to)
	if q.WalkFilter, err = walkFilter(p.WalkFilter); err != nil {
		return
	}
	if q.WeatherLimits, err = weatherLimits(p.WeatherLimits); err != nil {
		return
	}
	q.Limit = DefaultOptions
	if p.Limit != nil {
		if *p.Limit < 1 || *p.Limit > MaxOptions {
			err = &ParamError{"limit", fmt.Sprintf("between 1 and %d", MaxOptions)}
			return
		}
		q.Limit = *p.Limit
	}
	return
}

func has(list []string, v string) bool {
	for _, x := range list {
		if x == v {
			return true
		}
	}
	return false
}

// walkCandidate is a walk ranked by its verdict before its option is built:
// building every option would format a reason for walks that are dropped.
type walkCandidate struct {
	w    *walks.Walk
	rank int
	good int
	dist float64
}

func (a walkCandidate) better(b walkCandidate) bool {
	if a.rank != b.rank {
		return a.rank < b.rank
	}
	if a.good != b.good {
		return a.good > b.good
	}
	if a.dist != b.dist {
		return a.dist < b.dist
	}
	return a.w.ID < b.w.ID
}

// walksInRange is every walk starting within the radius that meets the
// limits, with its distance: the same for every day of a plan.
func walksInRange(ds *walks.Dataset, q PlanQuery) []walkCandidate {
	var out []walkCandidate
	for i := range ds.Walks {
		w := &ds.Walks[i]
		if !q.WalkFilter.matches(w) {
			continue
		}
		if dist := w.DistanceKm(q.Lat, q.Lon); dist <= q.RadiusKm {
			out = append(out, walkCandidate{w: w, dist: dist})
		}
	}
	return out
}

// planWalks is the day's best walks near the point that fit: go first, then
// marginal, longest good slot, nearest.
func planWalks(ds *walks.Dataset, q PlanQuery, inRange []walkCandidate, day int32, now time.Time) ([]WalkOption, string) {
	var top []walkCandidate // the best q.Limit that fit, best first
	var unknown *walks.Walk
	ask := walkAsk{lim: q.WeatherLimits.walks()}
	for _, c := range inRange {
		w := c.w
		d := ds.DayWithin(w, day, now, ask.lim)
		v := w.Verdict(d)
		if v != verdict.Go && v != verdict.Marginal {
			if v == verdict.Unknown && unknown == nil {
				unknown = w
			}
			continue
		}
		c.rank, c.good = v.Rank(), d.Best.GoodHours
		if len(top) == q.Limit && !c.better(top[len(top)-1]) {
			continue
		}
		at := sort.Search(len(top), func(j int) bool { return c.better(top[j]) })
		if len(top) < q.Limit {
			top = append(top, walkCandidate{})
		}
		copy(top[at+1:], top[at:])
		top[at] = c
	}
	switch {
	case len(inRange) == 0:
		return nil, fmt.Sprintf("No walk starts within %.0f km that matches the filters.", q.RadiusKm)
	case len(top) == 0 && unknown != nil:
		// An unknown day may yet be good, so it outranks "none fits".
		return nil, unknown.Assess(ds.DayWithin(unknown, day, now, ask.lim), ds.HourlyUntil).Why
	case len(top) == 0 && ask.lim.Active():
		return nil, fmt.Sprintf("None of the %d walks starting within %.0f km fits the weather within the limits.", len(inRange), q.RadiusKm)
	case len(top) == 0:
		return nil, fmt.Sprintf("None of the %d walks starting within %.0f km fits the weather.", len(inRange), q.RadiusKm)
	}
	out := make([]WalkOption, len(top))
	for i, c := range top {
		out[i], _ = walkOption(ds, c.w, day, now, ask)
		km := round(c.dist, 1)
		out[i].FromPointKm = &km
	}
	return out, ""
}

type rankedNight struct {
	o     NightOption
	score float64
}

// planNights is the night's best dark-sky sites near the point, kept at least
// siteSpacingKm apart so the options are different places.
func planNights(ds *stars.Dataset, q PlanQuery, night int32, now time.Time) ([]NightOption, string) {
	windows := ds.Windows(stars.WindowsQuery{Lat: q.Lat, Lon: q.Lon, RadiusKm: q.RadiusKm, Nights: 1, Night: night, Limit: 50, Now: now})
	if len(windows) == 0 {
		return nil, fmt.Sprintf("No dark-sky site within %.0f km.", q.RadiusKm)
	}
	ranked := make([]rankedNight, len(windows))
	for i := range windows {
		o, score := nightOption(ds, &windows[i], night, now)
		ranked[i] = rankedNight{o, score}
	}
	sort.SliceStable(ranked, func(i, j int) bool {
		a, b := ranked[i], ranked[j]
		if ra, rb := a.o.Verdict.Rank(), b.o.Verdict.Rank(); ra != rb {
			return ra < rb
		}
		if a.score != b.score {
			return a.score > b.score
		}
		return a.o.FromPointKm < b.o.FromPointKm
	})
	var out []NightOption
	for _, r := range ranked {
		if r.o.Verdict != verdict.Go && r.o.Verdict != verdict.Marginal {
			break
		}
		near := false
		for _, o := range out {
			if stars.DistanceKm(o.Lat, o.Lon, r.o.Lat, r.o.Lon) < siteSpacingKm {
				near = true
				break
			}
		}
		if !near {
			out = append(out, r.o)
		}
		if len(out) == q.Limit {
			break
		}
	}
	if len(out) > 0 {
		return out, ""
	}
	if ranked[0].o.Verdict == verdict.Unknown {
		return nil, ranked[0].o.Why
	}
	// The windows are the most clear hours listed; a capped site may list
	// none for this night and still be unknown rather than cloudy.
	if site := ds.CappedNear(q.Lat, q.Lon, q.RadiusKm, night); site != nil {
		if o, ok := siteNight(ds, site, q.Lat, q.Lon, night, now); ok {
			return nil, fmt.Sprintf("At site %s, %.1f km away: %s", o.SiteID, o.FromPointKm, o.Why)
		}
	}
	return nil, fmt.Sprintf("Cloudy at every dark-sky site within %.0f km.", q.RadiusKm)
}

// Plan answers "where and when should I go": for each day, the sun and moon,
// the walks that fit and the night's best stargazing near a point.
func (s *Service) Plan(p PlanParams) (*Envelope, error) {
	now := s.Now()
	today := walks.LocalDay(now)
	q, from, to, err := planQuery(p, today, stars.FirstNight(now))
	if err != nil {
		return nil, err
	}
	var sky *stars.Dataset
	var wk *walks.Dataset
	sources := []Source{sourceMET}
	walksMissing := false
	if has(q.Activities, ActivityWalk) {
		if wk = s.walks(); wk != nil {
			sources = append(sources, sourceWalkhighlands)
		} else if walksMissing = true; len(q.Activities) == 1 {
			// The walk snapshot is optional; a plan with stargazing still
			// answers without it.
			return nil, fmt.Errorf("%w: walks", ErrUnavailable)
		}
	}
	if has(q.Activities, ActivityStargaze) {
		if sky = s.Data.Current(); sky == nil {
			return nil, ErrUnavailable
		}
		sources = append(sources, sourceCERRA, sourceOSM, sourceNaturalEarth)
	}
	var inRange []walkCandidate
	if wk != nil {
		inRange = walksInRange(wk, q)
	}
	res := PlanResult{Query: q, Days: []PlanDay{}}
	results := 0
	for day := from; day <= to; day++ {
		pd := PlanDay{Date: dayName(day), SkyOut: skyAt(day, q.Lat, q.Lon)}
		if day < today && (wk != nil || walksMissing) {
			pd.WalkNote = "This day is over; its night is still under way."
		} else if wk != nil {
			pd.Walks, pd.WalkNote = planWalks(wk, q, inRange, day, now)
			results += len(pd.Walks)
		} else if walksMissing {
			pd.WalkNote = noteWalksLoading
		}
		if sky != nil {
			pd.Stargazing, pd.StarNote = planNights(sky, q, day, now)
			results += len(pd.Stargazing)
		}
		res.Days = append(res.Days, pd)
	}
	meta := s.meta(sky, wk, now, sources)
	meta.Notes = []string{noteVerdicts}
	if wk != nil {
		meta.Notes = append(meta.Notes, noteWalkStart)
	}
	return &Envelope{Data: res, Meta: meta, ETag: etag("plan", meta.DataVersion, meta.AsOf, canonical(q)), Results: results}, nil
}
