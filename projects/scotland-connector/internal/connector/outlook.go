package connector

import (
	"fmt"
	"math"
	"sort"
	"strings"
	"time"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/astro"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/stars"
)

const (
	OutlookDailyCap  = 92
	OutlookRateFloor = 300
)

// Outlook owns its date limits; forecast horizon caps do not apply.
type OutlookParams struct {
	Lat      *float64 `json:"lat,omitempty"`
	Lon      *float64 `json:"lon,omitempty"`
	RadiusKm *float64 `json:"radius_km,omitempty"`
	From     *string  `json:"from,omitempty"`
	To       *string  `json:"to,omitempty"`
}

type OutlookQuery struct {
	Lat      float64 `json:"lat"`
	Lon      float64 `json:"lon"`
	RadiusKm float64 `json:"radius_km"`
	From     string  `json:"from"`
	To       string  `json:"to"`
}

type OutlookSite struct {
	ID         string  `json:"site_id"`
	Lat        float64 `json:"lat"`
	Lon        float64 `json:"lon"`
	DistanceKm float64 `json:"distance_km"`
}

// Historical totals are whole calendar-month-of-year counts over the loaded
// archive. The snapshot does not carry archive years or exposure dates.
type OutlookClimate struct {
	Site           OutlookSite `json:"site"`
	ClearDarkHours int         `json:"historical_clear_dark_hours"`
	DarkHours      int         `json:"historical_dark_hours"`
	Share          *float64    `json:"clear_dark_share"`
}

type OutlookMonth struct {
	Month             string          `json:"month"`
	From              string          `json:"from"`
	To                string          `json:"to"`
	RateWinner        *OutlookClimate `json:"rate_winner"`
	HoursWinner       *OutlookClimate `json:"hours_winner"`
	RateRank          *int            `json:"climatology_rate_rank"`
	HoursRank         *int            `json:"climatology_hours_rank"`
	DarkHours         float64         `json:"calculated_dark_hours"`
	MoonlessDarkHours float64         `json:"calculated_moonless_dark_hours"`
	Verdict           string          `json:"verdict"`
	Why               string          `json:"why"`
}

type OutlookWindow struct {
	From  string `json:"from"`
	Until string `json:"until"`
}

type OutlookDay struct {
	Date              string          `json:"date"`
	ReferenceAt       string          `json:"reference_at"`
	Phase             float64         `json:"moon_phase"`
	Illumination      float64         `json:"moon_illumination"`
	Moonless          []OutlookWindow `json:"moonless_dark_windows"`
	DarkHours         float64         `json:"calculated_dark_hours"`
	MoonlessDarkHours float64         `json:"calculated_moonless_dark_hours"`
	RateRank          *int            `json:"climatology_rate_rank"`
	HoursRank         *int            `json:"climatology_hours_rank"`
	Verdict           string          `json:"verdict"`
	Why               string          `json:"why"`
}

type OutlookDetail struct {
	Status         string `json:"status"`
	RequestedDates int    `json:"requested_dates"`
	ReturnedDates  int    `json:"returned_dates"`
	MaxDates       int    `json:"max_dates"`
	Why            string `json:"why"`
}

type OutlookResult struct {
	Query        OutlookQuery   `json:"query"`
	Timezone     string         `json:"timezone"`
	Geometry     string         `json:"geometry"`
	Verdict      string         `json:"verdict"`
	Why          string         `json:"why"`
	Months       []OutlookMonth `json:"months"`
	RateRanking  []string       `json:"climatology_rate_ranking"`
	HoursRanking []string       `json:"climatology_hours_ranking"`
	DailyDetail  OutlookDetail  `json:"daily_detail"`
	Days         []OutlookDay   `json:"days"`
}

func outlookQuery(p OutlookParams, now time.Time) (q OutlookQuery, from, to time.Time, err error) {
	if q.Lat, q.Lon, err = coordinates(p.Lat, p.Lon); err != nil {
		return
	}
	q.RadiusKm = DefaultRadiusKm
	if p.RadiusKm != nil {
		if math.IsNaN(*p.RadiusKm) || *p.RadiusKm < 1 || *p.RadiusKm > MaxRadiusKm {
			err = &ParamError{"radius_km", fmt.Sprintf("between 1 and %d", MaxRadiusKm)}
			return
		}
		q.RadiusKm = round(*p.RadiusKm, 1)
	}
	parse := func(name string, raw *string, fallback time.Time) (time.Time, error) {
		t, e := fallback, error(nil)
		if raw != nil {
			t, e = time.ParseInLocation(time.DateOnly, strings.TrimSpace(*raw), lightLondon)
		}
		// Keep the truncated ephemeris in a bounded modern era. No forecast
		// freshness or 13-day horizon is inferred from this range.
		if e != nil || t.Year() < 2000 || t.Year() > 2100 {
			return time.Time{}, &ParamError{name, "a Europe/London date (YYYY-MM-DD) in 2000..2100"}
		}
		return t, nil
	}
	y, m, d := now.In(lightLondon).Date()
	from, err = parse("from", p.From, time.Date(y, m, d, 0, 0, 0, 0, lightLondon))
	if err != nil {
		return
	}
	// Clamp a leap-day anniversary to February's last date, rather than
	// Go's normalized March 1. The inclusive end precedes the anniversary.
	anniversary := from.AddDate(1, 0, 0)
	if anniversary.Month() != from.Month() {
		anniversary = anniversary.AddDate(0, 0, -1)
	}
	defaultEnd := anniversary.AddDate(0, 0, -1)
	if defaultEnd.Year() > 2100 {
		defaultEnd = time.Date(2100, 12, 31, 0, 0, 0, 0, lightLondon)
	}
	to, err = parse("to", p.To, defaultEnd)
	if err != nil {
		return
	}
	if to.Before(from) || !to.Before(anniversary) {
		err = &ParamError{"to", "inclusive end must be on or after from and before its next calendar-year anniversary (February 29 clamps to February 28)"}
		return
	}
	q.From, q.To = from.Format(time.DateOnly), to.Format(time.DateOnly)
	return
}

func outlookClimate(c stars.Candidate, month time.Month) *OutlookClimate {
	s := c.Site
	clear, dark := s.ClimateClear[month-1], s.ClimateDark[month-1]
	if !s.HasClimate || clear < 0 || dark < 0 || clear > dark {
		return nil
	}
	x := &OutlookClimate{Site: OutlookSite{s.ID, s.Lat, s.Lon, round(c.DistanceKm, 2)}, ClearDarkHours: clear, DarkHours: dark}
	if dark >= OutlookRateFloor {
		share := float64(clear) / float64(dark)
		x.Share = &share
	}
	return x
}

func outlookWinners(candidates []stars.Candidate, month time.Month) (rate, hours *OutlookClimate) {
	// Candidates are already ordered by exact distance then site ID. Equal
	// scores retain that order. Forecast counts/fetches are never consulted.
	for _, c := range candidates {
		x := outlookClimate(c, month)
		if x == nil {
			continue
		}
		if hours == nil || x.ClearDarkHours > hours.ClearDarkHours {
			hours = x
		}
		if x.Share != nil && (rate == nil || *x.Share > *rate.Share) {
			rate = x
		}
	}
	return
}

func outlookWhy(m OutlookMonth) string {
	why := "Weather unknown: climatology is historical CERRA support, never a forecast. "
	if m.RateWinner == nil {
		why += fmt.Sprintf("Rate rank unknown: no nearby valid month has at least %d historical dark hours. ", OutlookRateFloor)
	} else {
		x := m.RateWinner
		why += fmt.Sprintf("%s rate winner %s: %.1f%% clear-dark (%d clear-dark / %d dark historical hours). ", m.Month, x.Site.ID, *x.Share*100, x.ClearDarkHours, x.DarkHours)
	}
	if m.HoursWinner == nil {
		return why + "No nearby usable climatology; astronomical geometry remains independent."
	}
	x := m.HoursWinner
	return why + fmt.Sprintf("%s hours winner %s: %d clear-dark / %d dark historical hours; whole archive month totals, not expected hours for these dates.", m.Month, x.Site.ID, x.ClearDarkHours, x.DarkHours)
}

func outlookRanks(months []OutlookMonth, rate bool) []string {
	var indices []int
	for i, m := range months {
		if (rate && m.RateWinner != nil) || (!rate && m.HoursWinner != nil) {
			indices = append(indices, i)
		}
	}
	value := func(i int) float64 {
		if rate {
			return *months[i].RateWinner.Share
		}
		return float64(months[i].HoursWinner.ClearDarkHours)
	}
	sort.SliceStable(indices, func(i, j int) bool { return value(indices[i]) > value(indices[j]) })
	out := []string{}
	rank := 0
	for n, i := range indices {
		if n == 0 || value(i) != value(indices[n-1]) {
			rank = n + 1
		}
		r := rank
		if rate {
			months[i].RateRank = &r
		} else {
			months[i].HoursRank = &r
		}
		out = append(out, months[i].Month)
	}
	return out
}

// Outlook uses loaded climatology only. Astronomy is available even when no
// snapshot has loaded; missing climatology remains explicitly unknown.
func (s *Service) Outlook(p OutlookParams) (*Envelope, error) {
	now := s.Now()
	q, from, to, err := outlookQuery(p, now)
	if err != nil {
		return nil, err
	}
	res := OutlookResult{Query: q, Timezone: "Europe/London", Geometry: "requested point (query.lat, query.lon), independent of historical winning sites", Verdict: "unknown", Why: "Weather unknown for every date. Compare the separate historical rate and absolute-hours rankings; neither predicts weather. Month explanations give the applicable CERRA totals.", Months: []OutlookMonth{}, Days: []OutlookDay{}}
	var ds *stars.Dataset
	var candidates []stars.Candidate
	if s.Data != nil {
		ds = s.Data.Current()
	}
	if ds != nil {
		candidates = ds.Within(q.Lat, q.Lon, q.RadiusKm)
	}
	sort.Slice(candidates, func(i, j int) bool {
		if candidates[i].DistanceKm != candidates[j].DistanceKm {
			return candidates[i].DistanceKm < candidates[j].DistanceKm
		}
		return candidates[i].Site.ID < candidates[j].Site.ID
	})
	dates := 0
	for date := from; !date.After(to); date = date.AddDate(0, 0, 1) {
		dates++
	}
	detail := dates <= OutlookDailyCap
	res.DailyDetail = OutlookDetail{Status: "full", RequestedDates: dates, ReturnedDates: dates, MaxDates: OutlookDailyCap, Why: "All requested civil dates returned."}
	if !detail {
		res.DailyDetail.Status = "omitted"
		res.DailyDetail.ReturnedDates = 0
		res.DailyDetail.Why = "Span exceeds 92 inclusive civil dates; no daily rows returned. Monthly astronomical totals still cover every requested date."
	}
	for date := from; !date.After(to); date = date.AddDate(0, 0, 1) {
		month := date.Format("2006-01")
		if len(res.Months) == 0 || res.Months[len(res.Months)-1].Month != month {
			rate, hours := outlookWinners(candidates, date.Month())
			res.Months = append(res.Months, OutlookMonth{Month: month, From: date.Format(time.DateOnly), RateWinner: rate, HoursWinner: hours, Verdict: "unknown"})
		}
		m := &res.Months[len(res.Months)-1]
		m.To = date.Format(time.DateOnly)
		end := date.AddDate(0, 0, 1)
		sun := newLightCurve(date, end, func(t time.Time) float64 { return astro.SunElevation(t, q.Lat, q.Lon) })
		moon := newLightCurve(date, end, func(t time.Time) float64 { return astro.MoonAltitude(t, q.Lat, q.Lon) })
		bounds := []lightInterval{{date, end}}
		dark := lightComplement(lightIntersect(sun.above(-12), bounds), date, end)
		moonless := lightIntersect(dark, lightComplement(lightIntersect(moon.above(-0.833), bounds), date, end))
		hours := func(xs []lightInterval) float64 {
			var total time.Duration
			for _, x := range xs {
				total += x.until.Sub(x.from)
			}
			return total.Hours()
		}
		dh, mh := hours(dark), hours(moonless)
		m.DarkHours += dh
		m.MoonlessDarkHours += mh
		if detail {
			ref := time.Date(date.Year(), date.Month(), date.Day(), 12, 0, 0, 0, lightLondon)
			d := OutlookDay{Date: date.Format(time.DateOnly), ReferenceAt: rfc3339(ref), Phase: round(astro.MoonAt(ref).Phase(), 4), Illumination: round(astro.MoonIllumination(ref), 4), DarkHours: round(dh, 4), MoonlessDarkHours: round(mh, 4), Moonless: []OutlookWindow{}, Verdict: "unknown"}
			for _, x := range moonless {
				d.Moonless = append(d.Moonless, OutlookWindow{rfc3339(x.from), rfc3339(x.until)})
			}
			res.Days = append(res.Days, d)
		}
	}
	res.RateRanking = outlookRanks(res.Months, true)
	res.HoursRanking = outlookRanks(res.Months, false)
	byMonth := map[string]*OutlookMonth{}
	for i := range res.Months {
		m := &res.Months[i]
		m.DarkHours = round(m.DarkHours, 4)
		m.MoonlessDarkHours = round(m.MoonlessDarkHours, 4)
		m.Why = outlookWhy(*m)
		byMonth[m.Month] = m
	}
	for i := range res.Days {
		d := &res.Days[i]
		m := byMonth[d.Date[:7]]
		d.RateRank = m.RateRank
		d.HoursRank = m.HoursRank
		d.Why = m.Why
	}
	if len(res.RateRanking) > 0 {
		m := byMonth[res.RateRanking[0]]
		x := m.RateWinner
		res.Why += fmt.Sprintf(" Highest supported climatology rate: %s at %s, %.1f%% (%d clear-dark / %d dark historical hours).", m.Month, x.Site.ID, *x.Share*100, x.ClearDarkHours, x.DarkHours)
	} else {
		res.Why += " Climatology rate rank unknown: no nearby month meets the historical dark-hour floor."
	}
	if len(res.HoursRanking) > 0 {
		m := byMonth[res.HoursRanking[0]]
		x := m.HoursWinner
		res.Why += fmt.Sprintf(" Highest absolute climatology hours: %s at %s, %d clear-dark / %d dark historical hours.", m.Month, x.Site.ID, x.ClearDarkHours, x.DarkHours)
	} else {
		res.Why += " No nearby usable climatology; astronomy has independent coverage."
	}
	meta := s.meta(ds, nil, now, []Source{sourceCERRA, {Name: "Local astronomical calculation", Attribution: "Truncated Meeus lunar/solar series; NOAA solar elevation", License: "MPL 2.0", URL: "https://github.com/jomcgi-org/homelab/tree/main/projects/scotland-connector/internal/astro", Use: "requested-point nautical darkness, moon phase and moonless intervals"}})
	meta.ForecastFetchedAt = nil
	meta.Notes = []string{
		"Inclusive Europe/London civil dates; default today through the day before next year's anniversary, capped at 2100-12-31. February 29 anniversary clamps to February 28. Dates 2000..2100; no forecast horizon restriction. More than 92 dates omits all daily detail, never monthly astronomy.",
		"CERRA historical whole month-of-year totals, pooled across the loaded archive; archive years and exposure dates are unavailable in history-map.json. Native 3-hour cloud samples were nearest-reindexed hourly; dark is sun < -12 degrees and clear also cloud < 10%. These are neither independent hourly observations, a fresh probability nor expected hours for one requested month. Partial requested months never prorate history.",
		"Rate ranking requires at least 300 historical dark hours per calendar month at each candidate. Null means absent, invalid or insufficient support; hours ranking is separate and may disagree. Rankings compare returned months' whole historical totals, ties share competition rank; chronological order breaks ranking-list ties. Site ties use distance then ID. Daily ranks inherit their month's climatology, with no within-month weather distinction.",
		"Geometry is at the rounded requested point. Sun centre < -12 degrees, topocentric moon centre < -0.833 degrees (approximate refraction/disc allowance), no terrain. UTC RFC3339 intervals clipped to civil dates, including DST. Empty windows mean no qualifying interval, including no darkness or moon always up. Phase at local noon: 0=new, .25=first quarter, .5=full, .75=last quarter. One-second solver resolution is not ephemeris accuracy. Monthly calculated hours sum all requested civil dates independently of climatology coverage.",
	}
	return &Envelope{Data: res, Meta: meta, ETag: etag("outlook-v1", meta.DataVersion, meta.AsOf, canonical(q)), Results: len(res.Months) + len(res.Days)}, nil
}
