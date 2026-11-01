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
	noteWalkStart    = "Walk weather is forecast at each walk's start, not on summits. Low cloud/fog cover does not establish cloud base or summit visibility; for hill days also check the Mountain Weather Information Service (mwis.org.uk), and follow each walk's url for the route and safety notes."
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

// SlotHour is one hour of a slot with its weather. The one bad hour a slot
// may hold is marked good false, with fails naming what it failed ("rain",
// "wind", "cold"); when the forecast data holds good
// hours only, a bad hour that failed the default test has no weather and no
// fails. Fields the forecast leaves out are omitted.
type SlotHour struct {
	Time        string   `json:"time"`
	Good        bool     `json:"good"`
	Fails       []string `json:"fails,omitempty"`
	RainMM      *float32 `json:"rain_mm,omitempty"`
	WindKmh     *float32 `json:"wind_kmh,omitempty"`
	TempC       *float32 `json:"temp_c,omitempty"`
	CloudPct    *float32 `json:"cloud_pct,omitempty"`
	LowCloudPct *float32 `json:"low_cloud_pct,omitempty"`
	FogPct      *float32 `json:"fog_pct,omitempty"`
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
	// the slot; null unless it fits. A walk longer than its slot gets one
	// start, before the slot or at sunrise (see walks.Assessment).
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

// known is v when the forecast carries it, else nil.
func known(v float32) *float32 {
	if !walks.Known(v) {
		return nil
	}
	return f32(v)
}

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
	all := make(map[int64]walks.Hour, len(d.All))
	for _, h := range d.All {
		all[h.Unix] = h
	}
	for t := r.Start.Unix(); t < r.End.Unix(); t += 3600 {
		sh := SlotHour{Time: rfc3339(time.Unix(t, 0)), Good: good[t]}
		if h, ok := all[t]; ok {
			if !sh.Good {
				sh.Fails = d.Test.Fails(h)
			}
			sh.RainMM, sh.WindKmh, sh.TempC, sh.CloudPct = f32(h.PrecipMM), f32(h.WindKmh), f32(h.TempC), f32(h.CloudPct)
			sh.LowCloudPct, sh.FogPct = known(h.LowCloudPct), known(h.FogPct)
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

// WeatherLimits change what counts as a good walking hour from the default
// (at most 2 mm of rain and wind at most 80 km/h in an hour): drier, calmer
// or milder, or wetter and windier when the person will put up with it.
// Loosening needs forecast data that carries every daylight hour; on data
// that holds good hours only it is refused (needsComplete).
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
	check := func(v *float64, param string, lo, hi float64, isMax bool) (*float64, error) {
		if v == nil {
			return nil, nil
		}
		if math.IsNaN(*v) || *v < lo || *v > hi {
			return nil, &ParamError{param, fmt.Sprintf("between %g and %g", lo, hi)}
		}
		return strict(*v, isMax), nil
	}
	var err error
	if out.MaxRainMMH, err = check(l.MaxRainMMH, "max_rain_mm_h", 0, MaxRainLimitMM, true); err != nil {
		return out, err
	}
	if out.MaxWindKmh, err = check(l.MaxWindKmh, "max_wind_kmh", 0, MaxWindLimitKmh, true); err != nil {
		return out, err
	}
	if out.MinTempC, err = check(l.MinTempC, "min_temp_c", MinTempLimitC, MaxTempLimitC, false); err != nil {
		return out, err
	}
	return out, nil
}

// needsComplete is the error for limits the walk data cannot apply yet:
// hours past the defaults arrive with the version 2 snapshot.
func (l WeatherLimits) needsComplete(ds *walks.Dataset) error {
	if ds == nil || ds.Complete || !l.walks().NeedsComplete() {
		return nil
	}
	param, what := "max_wind_kmh", fmt.Sprintf("hours windier than %g km/h", walks.DefaultMaxWindKmh)
	if l.MaxRainMMH != nil && *l.MaxRainMMH > walks.DefaultMaxPrecipMM {
		param, what = "max_rain_mm_h", fmt.Sprintf("hours wetter than %g mm", walks.DefaultMaxPrecipMM)
	}
	return &ParamError{param, "not available yet: the walk forecast data does not carry " + what}
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
