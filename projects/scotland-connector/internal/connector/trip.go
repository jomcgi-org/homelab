package connector

import (
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
// hours (daylight, at most 2 mm of rain and wind at most 80 km/h each).
type SlotOut struct {
	Start         string  `json:"start"`
	End           string  `json:"end"`
	GoodHours     int     `json:"good_hours"`
	MaxWindKmh    float32 `json:"max_wind_kmh"`
	TotalPrecipMM float32 `json:"total_precip_mm"`
	MinTempC      float32 `json:"min_temp_c"`
	MaxTempC      float32 `json:"max_temp_c"`
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

func slotOut(r *walks.Run) *SlotOut {
	if r == nil {
		return nil
	}
	return &SlotOut{
		Start: rfc3339(r.Start), End: rfc3339(r.End), GoodHours: r.GoodHours,
		MaxWindKmh: r.MaxWindKmh, TotalPrecipMM: float32(round(float64(r.TotalPrecipMM), 1)),
		MinTempC: r.MinTempC, MaxTempC: r.MaxTempC,
	}
}

// walkOption assesses one walk on one day.
func walkOption(ds *walks.Dataset, w *walks.Walk, day int32, now time.Time) (WalkOption, float64) {
	d := ds.DayFor(w, day, now)
	a := w.Assess(d, ds.HourlyUntil)
	o := WalkOption{WalkFacts: walkFacts(w), Verdict: a.Verdict, Why: a.Why}
	good := 0.0
	if a.Verdict == verdict.Go || a.Verdict == verdict.Marginal {
		o.StartFrom, o.StartBy, o.Slot = timep(a.StartFrom), timep(a.StartBy), slotOut(d.Best)
		good = float64(d.Best.GoodHours)
	}
	return o, good
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
func nightOption(ds *stars.Dataset, w *stars.Window, night int32) (NightOption, float64) {
	n := &w.Nights[0]
	a := ds.AssessNight(w.Site, n, night, w.Climate)
	o := NightOption{
		SiteFacts: siteFacts(w.Site, w.Climate), FromPointKm: round(w.DistanceKm, 1),
		Verdict: a.Verdict, Why: a.Why, ClearDark: n.ClearDark, MoonlessDark: n.MoonlessDark,
	}
	if len(n.Hours) > 0 {
		o.ClearFrom = strp(time.Unix(n.Hours[0].Unix, 0).UTC().Format(time.RFC3339))
		o.ClearUntil = strp(time.Unix(n.Hours[len(n.Hours)-1].Unix+3600, 0).UTC().Format(time.RFC3339))
	}
	return o, stars.DarkScore(n)
}
