package connector

import (
	"fmt"
	"sort"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/stars"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/verdict"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/walks"
)

// CheckItem is one part of a plan to check: a walk on a date, or stargazing
// at a point on a night (named by its evening's date).
type CheckItem struct {
	Activity string   `json:"activity"`
	Date     string   `json:"date"`
	WalkID   string   `json:"walk_id,omitempty"`
	WalkName string   `json:"walk_name,omitempty"`
	Lat      *float64 `json:"lat,omitempty"`
	Lon      *float64 `json:"lon,omitempty"`
}

// CheckParams are the check inputs.
type CheckParams struct {
	Items []CheckItem `json:"items"`
	// WeatherLimits apply to every walk item, its other days and the
	// walks offered nearby.
	WeatherLimits
}

// OtherDay is the same walk or place on another forecast day.
type OtherDay struct {
	Date    string          `json:"date"`
	Verdict verdict.Verdict `json:"verdict"`
	Why     string          `json:"why"`
}

// CheckWalk is a checked walk.
type CheckWalk struct {
	WalkOption
	// AlsoMatched lists other walks walk_name matched, best first.
	AlsoMatched []string `json:"also_matched,omitempty"`
}

// CheckNight is checked stargazing: the best dark-sky site near the point.
type CheckNight struct {
	NightOption
	// SiteNote is set when no site was within 10 km of the point.
	SiteNote string `json:"site_note,omitempty"`
}

// CheckResultItem is the answer for one item. Error is set, and the rest
// empty, when the item cannot be resolved (an unknown walk, a point with no
// dark-sky site near it).
type CheckResultItem struct {
	Activity  string          `json:"activity"`
	Date      string          `json:"date"`
	Verdict   verdict.Verdict `json:"verdict,omitempty"`
	Why       string          `json:"why,omitempty"`
	Error     string          `json:"error,omitempty"`
	Walk      *CheckWalk      `json:"walk,omitempty"`
	Night     *CheckNight     `json:"night,omitempty"`
	Sky       *SkyOut         `json:"sky,omitempty"`
	OtherDays []OtherDay      `json:"other_days,omitempty"`
	// Nearby lists better options the same day: walks starting within 20 km,
	// or dark-sky sites within 30 km.
	NearbyWalks  []WalkOption  `json:"nearby_walks,omitempty"`
	NearbyNights []NightOption `json:"nearby_nights,omitempty"`
}

// CheckResult is check's data.
type CheckResult struct {
	// Limits echoes the weather limits used, when any were given.
	Limits *WeatherLimits    `json:"limits,omitempty"`
	Items  []CheckResultItem `json:"items"`
}

type checkPlan struct {
	item     CheckItem
	day      int32
	name     string
	lat, lon float64
}

// checkItems validates a check. A walk's date runs from today (UK date); a
// night's from tonight, which before 09:00 is yesterday's evening.
func checkItems(p CheckParams, today, tonight int32) ([]checkPlan, error) {
	if len(p.Items) == 0 || len(p.Items) > MaxCheckItems {
		return nil, &ParamError{"items", fmt.Sprintf("between 1 and %d items", MaxCheckItems)}
	}
	plans := make([]checkPlan, len(p.Items))
	for i, it := range p.Items {
		param := func(f string) string { return fmt.Sprintf("items[%d].%s", i, f) }
		cp := checkPlan{item: it}
		var err error
		first := today
		if it.Activity == ActivityStargaze {
			first = tonight
		}
		if cp.day, err = parseDay(param("date"), it.Date, first, today+MaxDaysAhead); err != nil {
			return nil, err
		}
		cp.item.Date = dayName(cp.day)
		switch it.Activity {
		case ActivityWalk:
			if it.WalkID == "" && strings.TrimSpace(it.WalkName) == "" {
				return nil, &ParamError{param("walk_name"), "a walk needs walk_id or walk_name"}
			}
			if it.WalkID == "" {
				if utf8.RuneCountInString(it.WalkName) > MaxNameLength {
					return nil, &ParamError{param("walk_name"), fmt.Sprintf("at most %d characters", MaxNameLength)}
				}
				if cp.name = walks.Normalize(it.WalkName); cp.name == "" {
					return nil, &ParamError{param("walk_name"), "needs letters or digits"}
				}
			}
		case ActivityStargaze:
			if cp.lat, cp.lon, err = coordinates(it.Lat, it.Lon); err != nil {
				pe := err.(*ParamError)
				return nil, &ParamError{param("lat"), pe.Message}
			}
		default:
			return nil, &ParamError{param("activity"), `"walk" or "stargaze"`}
		}
		plans[i] = cp
	}
	return plans, nil
}

// otherDays is the item on each other day from today to the end of the
// hourly forecast, good ones only.
func otherDays(today, asked, last int32, assess func(day int32) (verdict.Verdict, string)) []OtherDay {
	var out []OtherDay
	for day := today; day <= last; day++ {
		if day == asked {
			continue
		}
		if v, why := assess(day); v == verdict.Go || v == verdict.Marginal {
			out = append(out, OtherDay{Date: dayName(day), Verdict: v, Why: why})
		}
	}
	return out
}

func (s *Service) checkWalk(ds *walks.Dataset, cp checkPlan, now time.Time, lim walks.Limits) CheckResultItem {
	r := CheckResultItem{Activity: ActivityWalk, Date: cp.item.Date}
	var w *walks.Walk
	var also []string
	if cp.item.WalkID != "" {
		if w = ds.ByID(cp.item.WalkID); w == nil {
			r.Error = "No walk has walk_id " + cp.item.WalkID + "."
			return r
		}
	} else {
		hits := ds.Named(cp.name)
		if len(hits) == 0 {
			r.Error = fmt.Sprintf("No Walkhighlands walk name matches %q. Try fewer words, or use plan to find walks near a point.", cp.item.WalkName)
			return r
		}
		w = hits[0]
		for _, h := range hits[1:min(len(hits), 4)] {
			also = append(also, h.Name)
		}
	}
	// A check is the close look, so its slot comes back hour by hour.
	o, _ := walkOption(ds, w, cp.day, now, walkAsk{lim: lim, hours: true})
	r.Verdict, r.Why = o.Verdict, o.Why
	r.Walk = &CheckWalk{WalkOption: o, AlsoMatched: also}
	sky := skyAt(cp.day, w.Lat, w.Lon)
	r.Sky = &sky
	today := walks.LocalDay(now)
	last := walks.LocalDay(ds.HourlyUntil)
	r.OtherDays = otherDays(today, cp.day, last, func(day int32) (verdict.Verdict, string) {
		a := w.Assess(ds.DayWithin(w, day, now, lim), ds.HourlyUntil)
		return a.Verdict, a.Why
	})
	if o.Verdict != verdict.Go {
		r.NearbyWalks = nearbyWalks(ds, w, o.Verdict, cp.day, now, lim)
	}
	return r
}

// nearbyWalks is up to two walks that fit better than w on day: starting
// within nearbyWalkKm of it and of a similar length (half to one and a half
// times its time), so they are a substitute for it rather than any walk at
// all. Nearest first within a verdict.
func nearbyWalks(ds *walks.Dataset, w *walks.Walk, v verdict.Verdict, day int32, now time.Time, lim walks.Limits) []WalkOption {
	var near []walkCandidate
	for i := range ds.Walks {
		n := &ds.Walks[i]
		if n == w || n.DurationH < w.DurationH/2 || n.DurationH > w.DurationH*1.5 {
			continue
		}
		dist := n.DistanceKm(w.Lat, w.Lon)
		if dist > nearbyWalkKm {
			continue
		}
		nv := n.Verdict(ds.DayWithin(n, day, now, lim))
		if nv != verdict.Go && nv != verdict.Marginal || nv.Rank() >= v.Rank() {
			continue
		}
		near = append(near, walkCandidate{w: n, rank: nv.Rank(), dist: dist})
	}
	sort.Slice(near, func(i, j int) bool { return near[i].better(near[j]) })
	var out []WalkOption
	for _, c := range near[:min(len(near), 2)] {
		o, _ := walkOption(ds, c.w, day, now, walkAsk{lim: lim})
		km := round(c.dist, 1)
		o.FromPointKm = &km
		out = append(out, o)
	}
	return out
}

func (s *Service) checkNight(ds *stars.Dataset, cp checkPlan, now time.Time) CheckResultItem {
	r := CheckResultItem{Activity: ActivityStargaze, Date: cp.item.Date}
	sky := skyAt(cp.day, cp.lat, cp.lon)
	r.Sky = &sky
	query := func(radius float64) []stars.Window {
		return ds.Windows(stars.WindowsQuery{Lat: cp.lat, Lon: cp.lon, RadiusKm: radius, Nights: 1, Night: cp.day, Limit: 50, Now: now})
	}
	note := ""
	windows := query(siteKm)
	if len(windows) == 0 {
		if windows = query(siteFarKm); len(windows) == 0 {
			r.Error = fmt.Sprintf("No dark-sky site within %d km of the point: the sky there is too light-polluted or too far from a road.", siteFarKm)
			return r
		}
		note = fmt.Sprintf("No dark-sky site within %d km of the point; this is the best within %d km.", siteKm, siteFarKm)
	}
	radius := float64(siteKm)
	if note != "" {
		radius = siteFarKm
	}
	best, bestScore := nightOption(ds, &windows[0], cp.day, now)
	for i := 1; i < len(windows); i++ {
		o, score := nightOption(ds, &windows[i], cp.day, now)
		if o.Verdict.Rank() < best.Verdict.Rank() || (o.Verdict == best.Verdict && score > bestScore) {
			best, bestScore = o, score
		}
	}
	// The windows are the most clear hours listed; a capped site may list
	// none for this night and still be unknown rather than cloudy.
	if best.Verdict == verdict.No {
		if site := ds.CappedNear(cp.lat, cp.lon, radius, cp.day); site != nil {
			if o, ok := siteNight(ds, site, cp.lat, cp.lon, cp.day, now); ok {
				best = o
			}
		}
	}
	r.Verdict, r.Why = best.Verdict, best.Why
	r.Night = &CheckNight{NightOption: best, SiteNote: note}
	today := stars.FirstNight(now)
	last := stars.NightDay(ds.HourlyUntil)
	r.OtherDays = otherDays(today, cp.day, last, func(night int32) (verdict.Verdict, string) {
		ws := ds.Windows(stars.WindowsQuery{Lat: best.Lat, Lon: best.Lon, RadiusKm: 0.5, Nights: 1, Night: night, Limit: 1, Now: now})
		if len(ws) == 0 {
			return verdict.Unknown, ""
		}
		a := ds.AssessNight(ws[0].Site, &ws[0].Nights[0], night, now, ws[0].Climate)
		return a.Verdict, a.Why
	})
	if best.Verdict != verdict.Go {
		var near []rankedNight
		for i := range windows {
			o, score := nightOption(ds, &windows[i], cp.day, now)
			if o.Verdict.Rank() < best.Verdict.Rank() && o.Verdict != verdict.Unknown {
				near = append(near, rankedNight{o, score})
			}
		}
		if len(windows) > 0 && note == "" {
			for _, w := range query(siteFarKm) {
				if w.DistanceKm <= siteKm {
					continue
				}
				o, score := nightOption(ds, &w, cp.day, now)
				if o.Verdict.Rank() < best.Verdict.Rank() && o.Verdict != verdict.Unknown {
					near = append(near, rankedNight{o, score})
				}
			}
		}
		sort.SliceStable(near, func(i, j int) bool {
			if ra, rb := near[i].o.Verdict.Rank(), near[j].o.Verdict.Rank(); ra != rb {
				return ra < rb
			}
			if near[i].score != near[j].score {
				return near[i].score > near[j].score
			}
			return near[i].o.FromPointKm < near[j].o.FromPointKm
		})
		for _, n := range near[:min(len(near), 2)] {
			r.NearbyNights = append(r.NearbyNights, n.o)
		}
	}
	return r
}

// Check answers "is my plan on": a verdict, its reason and timing for each
// walk or night, the other forecast days that suit it, and better options
// nearby the same day.
func (s *Service) Check(p CheckParams) (*Envelope, error) {
	now := s.Now()
	plans, err := checkItems(p, walks.LocalDay(now), stars.FirstNight(now))
	if err != nil {
		return nil, err
	}
	limits, err := weatherLimits(p.WeatherLimits)
	if err != nil {
		return nil, err
	}
	var sky *stars.Dataset
	var wk *walks.Dataset
	sources := []Source{sourceMET}
	walk, stargaze := false, false
	for _, cp := range plans {
		walk = walk || cp.item.Activity == ActivityWalk
		stargaze = stargaze || cp.item.Activity == ActivityStargaze
	}
	if stargaze {
		if sky = s.Data.Current(); sky == nil {
			return nil, ErrUnavailable
		}
		sources = append(sources, sourceCERRA, sourceOSM, sourceNaturalEarth)
	}
	if walk {
		if wk = s.walks(); wk != nil {
			sources = append(sources, sourceWalkhighlands)
		} else if !stargaze {
			// The walk snapshot is optional; walk items get an item error
			// while stargazing items still answer.
			return nil, fmt.Errorf("%w: walks", ErrUnavailable)
		}
	}
	res := CheckResult{Items: make([]CheckResultItem, len(plans))}
	if walk && limits.walks().Active() {
		res.Limits = &limits
	}
	results := 0
	for i, cp := range plans {
		switch {
		case cp.item.Activity == ActivityStargaze:
			res.Items[i] = s.checkNight(sky, cp, now)
		case wk == nil:
			res.Items[i] = CheckResultItem{Activity: ActivityWalk, Date: cp.item.Date, Error: noteWalksLoading}
		default:
			res.Items[i] = s.checkWalk(wk, cp, now, limits.walks())
		}
		if res.Items[i].Error == "" {
			results++
		}
	}
	meta := s.meta(sky, wk, now, sources)
	meta.Notes = []string{noteVerdicts}
	if wk != nil {
		meta.Notes = append(meta.Notes, noteWalkStart)
	}
	// No ETag: check is a POST, which no cache keys on.
	return &Envelope{Data: res, Meta: meta, Results: results}, nil
}
