package connector

import (
	"fmt"
	"math"
	"time"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/astro"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/stars"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/verdict"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/walks"
)

// Activities a plan or check can cover.
const (
	ActivityWalk     = "walk"
	ActivityStargaze = "stargaze"
)

// Notes every answer that covers walks or nights carries.
const (
	noteWalkStart    = "Walk weather is forecast at each walk's start, not on summits; for hill days also check the Mountain Weather Information Service (mwis.org.uk), and follow each walk's url for the route and safety notes."
	noteWalksLoading = "Walk data is not loaded yet; retry in a few minutes."
	noteVerdicts     = "verdict is go, marginal, no or unknown (the hourly forecast, about two and a half days ahead, does not reach that far yet); why says what decided it, in UK local time."
)

func (s *Service) walks() *walks.Dataset {
	if s.Walks == nil {
		return nil
	}
	return s.Walks.Current()
}

// SlotOut is a walk's best slot on a day and the weather across its good
// hours (daylight, at most 2 mm of rain and wind at most 80 km/h each, or
// the caller's tighter limits).
type SlotOut struct {
	Start         string  `json:"start"`
	End           string  `json:"end"`
	GoodHours     int     `json:"good_hours"`
	MaxWindKmh    float32 `json:"max_wind_kmh"`
	TotalPrecipMM float32 `json:"total_precip_mm"`
	MaxRainMMH    float32 `json:"max_rain_mm_h"`
	// WetHours counts good hours with at least 0.1 mm of rain, so an agent
	// can tell a dry slot from a drizzly one.
	WetHours int     `json:"wet_hours"`
	MinTempC float32 `json:"min_temp_c"`
	MaxTempC float32 `json:"max_temp_c"`
	// Hours is the slot hour by hour for the checked walk in a check; plan
	// and the alternatives a check offers leave it out to stay compact.
	Hours []SlotHour `json:"hours,omitempty"`
}

// SlotHour is one hour of a slot. The one bad hour a slot may hold is
// marked good false. It carries its weather when the caller's limits turned
// it away; when it failed the default test it carries none, since the data
// stores no such hour.
type SlotHour struct {
	Time          string   `json:"time"`
	Good          bool     `json:"good"`
	OutsideLimits bool     `json:"outside_limits,omitempty"`
	RainMM        *float32 `json:"rain_mm,omitempty"`
	WindKmh       *float32 `json:"wind_kmh,omitempty"`
	TempC         *float32 `json:"temp_c,omitempty"`
	CloudPct      *float32 `json:"cloud_pct,omitempty"`
}

// WalkFacts identify a Walkhighlands walk; url has the route.
type WalkFacts struct {
	WalkID    string  `json:"walk_id"`
	Name      string  `json:"name"`
	URL       string  `json:"url"`
	StartLat  float64 `json:"start_lat"`
	StartLon  float64 `json:"start_lon"`
	LengthKm  float64 `json:"length_km"`
	AscentM   int     `json:"ascent_m"`
	DurationH float64 `json:"duration_h"`
}

// WalkOption is one walk on one day.
type WalkOption struct {
	WalkFacts
	FromPointKm *float64        `json:"from_point_km,omitempty"`
	Verdict     verdict.Verdict `json:"verdict"`
	Why         string          `json:"why"`
	// StartFrom and StartBy bound the start time that keeps the walk inside
	// the slot; null unless it fits.
	StartFrom *string  `json:"start_from"`
	StartBy   *string  `json:"start_by"`
	Slot      *SlotOut `json:"slot"`
}

// SiteFacts identify a dark-sky site: a road-accessible point in the darkest
// light-pollution zones.
type SiteFacts struct {
	SiteID         string  `json:"site_id"`
	Lat            float64 `json:"lat"`
	Lon            float64 `json:"lon"`
	LightPollution string  `json:"light_pollution_zone"`
	// ClearSkyOdds is the historical share of dark hours that were clear in
	// this calendar month; null when not known.
	ClearSkyOdds *float64 `json:"clear_sky_odds"`
}

// NightOption is one dark-sky site on one night.
type NightOption struct {
	SiteFacts
	FromPointKm  float64         `json:"from_point_km"`
	Verdict      verdict.Verdict `json:"verdict"`
	Why          string          `json:"why"`
	ClearFrom    *string         `json:"clear_from"`
	ClearUntil   *string         `json:"clear_until"`
	ClearDark    int             `json:"clear_dark_hours"`
	MoonlessDark int             `json:"moonless_clear_dark_hours"`
}

// SkyOut is the sun and moon for a day at a place.
type SkyOut struct {
	Sunrise *string `json:"sunrise"`
	Sunset  *string `json:"sunset"`
	// MoonIllumination is the moon's lit fraction at local midnight after
	// the evening.
	MoonIllumination float64 `json:"moon_illumination"`
}

func rfc3339(t time.Time) string { return t.UTC().Format(time.RFC3339) }

func strp(s string) *string { return &s }

func timep(t time.Time) *string {
	if t.IsZero() {
		return nil
	}
	return strp(rfc3339(t))
}

// skyAt is the sun and moon for day (days since 1970-01-01, a UK date) at a
// point.
func skyAt(day int32, lat, lon float64) SkyOut {
	out := SkyOut{MoonIllumination: round(stars.NightMoonLit(day), 2)}
	if rise, set, ok := astro.SunTimes(time.Unix(int64(day)*86400, 0), lat, lon); ok {
		out.Sunrise, out.Sunset = timep(rise), timep(set)
	}
	return out
}

func walkFacts(w *walks.Walk) WalkFacts {
	return WalkFacts{
		WalkID: w.ID, Name: w.Name, URL: w.URL, StartLat: w.Lat, StartLon: w.Lon,
		LengthKm: w.LengthKm, AscentM: w.AscentM, DurationH: w.DurationH,
	}
}

func f32(v float32) *float32 { return &v }

func slotOut(d *walks.Day, withHours bool) *SlotOut {
	r := d.Best
	if r == nil {
		return nil
	}
	out := &SlotOut{
		Start: rfc3339(r.Start), End: rfc3339(r.End), GoodHours: r.GoodHours,
		MaxWindKmh: r.MaxWindKmh, TotalPrecipMM: float32(round(float64(r.TotalPrecipMM), 1)),
		MaxRainMMH: float32(round(float64(r.MaxPrecipMM), 1)), WetHours: r.WetHours,
		MinTempC: r.MinTempC, MaxTempC: r.MaxTempC,
	}
	if !withHours {
		return out
	}
	good := make(map[int64]bool, len(d.Hours))
	for _, h := range d.Hours {
		good[h.Unix] = true
	}
	known := make(map[int64]walks.Hour, len(d.All))
	for _, h := range d.All {
		known[h.Unix] = h
	}
	for t := r.Start.Unix(); t < r.End.Unix(); t += 3600 {
		sh := SlotHour{Time: rfc3339(time.Unix(t, 0)), Good: good[t]}
		if h, ok := known[t]; ok {
			sh.OutsideLimits = !sh.Good
			sh.RainMM, sh.WindKmh, sh.TempC, sh.CloudPct = f32(h.PrecipMM), f32(h.WindKmh), f32(h.TempC), f32(h.CloudPct)
		}
		out.Hours = append(out.Hours, sh)
	}
	return out
}

// walkAsk is how a walk is assessed: the caller's weather limits, and
// whether the slot comes back hour by hour.
type walkAsk struct {
	lim   walks.Limits
	hours bool
}

// walkOption assesses one walk on one day.
func walkOption(ds *walks.Dataset, w *walks.Walk, day int32, now time.Time, ask walkAsk) (WalkOption, float64) {
	d := ds.DayWithin(w, day, now, ask.lim)
	a := w.Assess(d, ds.HourlyUntil)
	o := WalkOption{WalkFacts: walkFacts(w), Verdict: a.Verdict, Why: a.Why}
	good := 0.0
	if a.Verdict == verdict.Go || a.Verdict == verdict.Marginal {
		o.StartFrom, o.StartBy, o.Slot = timep(a.StartFrom), timep(a.StartBy), slotOut(&d, ask.hours)
		good = float64(d.Best.GoodHours)
	}
	return o, good
}

// WeatherLimits tighten what counts as a good walking hour, for a caller
// who wants it drier, calmer or milder than the defaults (at most 2 mm of
// rain and 80 km/h of wind in an hour). Limits can only tighten: the data
// holds no hour outside the defaults.
type WeatherLimits struct {
	MaxRainMMH *float64 `json:"max_rain_mm_h,omitempty"`
	MaxWindKmh *float64 `json:"max_wind_kmh,omitempty"`
	MinTempC   *float64 `json:"min_temp_c,omitempty"`
}

func rounded(v *float64, places int) *float64 {
	r := round(*v, places)
	return &r
}

// strict rounds a limit to the data's 0.1 precision in its strict
// direction, so rounding never admits an hour the caller ruled out: a
// maximum rounds down and a minimum up. The epsilon keeps 0.3 at 0.3.
func strict(v float64, isMax bool) *float64 {
	r := math.Ceil(v*10-1e-9) / 10
	if isMax {
		r = math.Floor(v*10+1e-9) / 10
	}
	r += 0 // -0 reads as 0, in the echo and the ETag
	return &r
}

// weatherLimits validates the limits and rounds them strictly.
func weatherLimits(l WeatherLimits) (WeatherLimits, error) {
	var out WeatherLimits
	if v := l.MaxRainMMH; v != nil {
		if math.IsNaN(*v) || *v < 0 || *v > walks.ViableMaxPrecipMM {
			return out, &ParamError{"max_rain_mm_h", fmt.Sprintf("between 0 and %g: the data holds only hours with at most %g mm", walks.ViableMaxPrecipMM, walks.ViableMaxPrecipMM)}
		}
		out.MaxRainMMH = strict(*v, true)
	}
	if v := l.MaxWindKmh; v != nil {
		if math.IsNaN(*v) || *v < 0 || *v > walks.ViableMaxWindKmh {
			return out, &ParamError{"max_wind_kmh", fmt.Sprintf("between 0 and %g: the data holds only hours with wind at most %g km/h", walks.ViableMaxWindKmh, walks.ViableMaxWindKmh)}
		}
		out.MaxWindKmh = strict(*v, true)
	}
	if v := l.MinTempC; v != nil {
		if math.IsNaN(*v) || *v < MinTempLimitC || *v > MaxTempLimitC {
			return out, &ParamError{"min_temp_c", fmt.Sprintf("between %d and %d", MinTempLimitC, MaxTempLimitC)}
		}
		out.MinTempC = strict(*v, false)
	}
	return out, nil
}

func (l WeatherLimits) walks() walks.Limits {
	return walks.Limits{MaxPrecipMM: l.MaxRainMMH, MaxWindKmh: l.MaxWindKmh, MinTempC: l.MinTempC}
}

func siteFacts(site *stars.Site, c *stars.Climate) SiteFacts {
	f := SiteFacts{SiteID: site.ID, Lat: site.Lat, Lon: site.Lon, LightPollution: site.LPZone}
	if c != nil && c.Dark > 0 {
		odds := round(float64(c.ClearDark)/float64(c.Dark), 3)
		f.ClearSkyOdds = &odds
	}
	return f
}

// nightOption assesses one window (a site on one night).
func nightOption(ds *stars.Dataset, w *stars.Window, night int32, now time.Time) (NightOption, float64) {
	n := &w.Nights[0]
	a := ds.AssessNight(w.Site, n, night, now, w.Climate)
	o := NightOption{
		SiteFacts: siteFacts(w.Site, w.Climate), FromPointKm: round(w.DistanceKm, 1),
		Verdict: a.Verdict, Why: a.Why, ClearDark: n.ClearDark, MoonlessDark: n.MoonlessDark,
	}
	if from, ok := n.ClearFrom(); ok {
		o.ClearFrom = strp(rfc3339(from))
	}
	if until, ok := n.ClearUntil(); ok {
		o.ClearUntil = strp(rfc3339(until))
	}
	return o, stars.DarkScore(n)
}

// siteNight assesses one site on one night, with its distance from a point.
func siteNight(ds *stars.Dataset, site *stars.Site, lat, lon float64, night int32, now time.Time) (NightOption, bool) {
	ws := ds.Windows(stars.WindowsQuery{Lat: site.Lat, Lon: site.Lon, RadiusKm: 0.01, Nights: 1, Night: night, Limit: 1, Now: now})
	if len(ws) == 0 || ws[0].Site != site {
		return NightOption{}, false
	}
	o, _ := nightOption(ds, &ws[0], night, now)
	o.FromPointKm = round(stars.DistanceKm(lat, lon, site.Lat, site.Lon), 1)
	return o, true
}
