package connector

import (
	"fmt"
	"math"
	"strings"
	"time"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/walks"
)

var (
	sourceMETWalks = Source{
		Name:        "MET Norway Locationforecast",
		Attribution: "Weather forecast data from MET Norway",
		License:     "CC BY 4.0",
		URL:         "https://api.met.no/doc/License",
		Use:         "hourly temperature, precipitation, wind and cloud at each walk's start",
	}
	// Walk facts come from Walkhighlands pages; the route itself is not
	// reproduced, and every walk links to its page (#7252).
	sourceWalkhighlands = Source{
		Name:        "Walkhighlands",
		Attribution: "Walk listings from Walkhighlands; follow each walk's url for the route",
		License:     "Routes and descriptions stay on walkhighlands.co.uk; each walk links to its page",
		URL:         "https://www.walkhighlands.co.uk/",
		Use:         "walk names, start points, length, ascent and estimated time",
	}
)

// Every walk answer carries these, because the weather is forecast where the
// walk starts and the snapshot only keeps hours that passed the limits.
var walkNotes = []string{
	"Weather is forecast at each walk's start point, not along the route or on summits.",
	"Listed hours are daylight hours with at most 2 mm of rain and wind at most 80 km/h; within the forecast, a missing daylight hour failed those limits.",
	"The hourly forecast reaches about two and a half days: forecast_coverage says how much of each day it covers, and doable is null when it cannot be decided yet.",
	"best is each walk's earliest doable day and slot; pass detail=hours for the hourly weather.",
	"Follow each walk's url for the route, maps and safety notes.",
}

// WalkWindowsParams are the walk_windows inputs.
type WalkWindowsParams struct {
	Lat          *float64 `json:"lat,omitempty"`
	Lon          *float64 `json:"lon,omitempty"`
	WalkName     *string  `json:"walk_name,omitempty"`
	RadiusKm     *float64 `json:"radius_km,omitempty"`
	Date         *string  `json:"date,omitempty"`
	Days         *int     `json:"days,omitempty"`
	Limit        *int     `json:"limit,omitempty"`
	MaxDurationH *float64 `json:"max_duration_h,omitempty"`
	MaxAscentM   *int     `json:"max_ascent_m,omitempty"`
	Detail       *string  `json:"detail,omitempty"`
}

// WalkQuery echoes the canonical inputs actually used.
type WalkQuery struct {
	Lat          *float64 `json:"lat,omitempty"`
	Lon          *float64 `json:"lon,omitempty"`
	WalkName     string   `json:"walk_name,omitempty"`
	RadiusKm     *float64 `json:"radius_km,omitempty"`
	Date         string   `json:"date,omitempty"`
	Days         int      `json:"days"`
	Limit        int      `json:"limit"`
	MaxDurationH *float64 `json:"max_duration_h,omitempty"`
	MaxAscentM   *int     `json:"max_ascent_m,omitempty"`
	Detail       string   `json:"detail"`
}

// WalkHourOut is one viable hour.
type WalkHourOut struct {
	Time     string  `json:"time"`
	TempC    float32 `json:"temp_c"`
	PrecipMM float32 `json:"precip_mm"`
	WindKmh  float32 `json:"wind_kmh"`
	CloudPct float32 `json:"cloud_pct"`
}

// WalkSlotOut is a day's best slot and the weather across its good hours.
type WalkSlotOut struct {
	Start         string  `json:"start"`
	End           string  `json:"end"`
	GoodHours     int     `json:"good_hours"`
	MaxWindKmh    float32 `json:"max_wind_kmh"`
	TotalPrecipMM float32 `json:"total_precip_mm"`
	MinTempC      float32 `json:"min_temp_c"`
	MaxTempC      float32 `json:"max_temp_c"`
}

// WalkBestOut is a walk's earliest doable day.
type WalkBestOut struct {
	Date string `json:"date"`
	WalkSlotOut
}

// WalkDayOut is one day at one walk. Doable is null when the forecast does
// not reach far enough to decide; Hours only come with detail=hours.
type WalkDayOut struct {
	Date             string         `json:"date"`
	ForecastCoverage string         `json:"forecast_coverage"`
	Doable           *bool          `json:"doable"`
	GoodHours        int            `json:"good_hours"`
	BestSlot         *WalkSlotOut   `json:"best_slot"`
	Hours            *[]WalkHourOut `json:"hours,omitempty"`
}

// WalkOut is one ranked walk.
type WalkOut struct {
	WalkID      string       `json:"walk_id"`
	Name        string       `json:"name"`
	URL         string       `json:"url"`
	StartLat    float64      `json:"start_lat"`
	StartLon    float64      `json:"start_lon"`
	FromPointKm *float64     `json:"from_point_km,omitempty"`
	LengthKm    float64      `json:"length_km"`
	AscentM     int          `json:"ascent_m"`
	DurationH   float64      `json:"duration_h"`
	DoableDays  int          `json:"doable_days"`
	Best        *WalkBestOut `json:"best"`
	Days        []WalkDayOut `json:"days"`
}

// WalkWindowsResult is walk_windows' data.
type WalkWindowsResult struct {
	Query WalkQuery `json:"query"`
	Days  []string  `json:"days"`
	Walks []WalkOut `json:"walks"`
}

// walkDateAhead is how far ahead a date may be asked about. Days past the
// hourly forecast come back with forecast_coverage "none".
const walkDateAhead = 13

type walkPlan struct {
	out   WalkQuery
	query walks.Query
	hours bool
}

func walkQuery(p WalkWindowsParams, now time.Time) (walkPlan, error) {
	plan := walkPlan{out: WalkQuery{Days: DefaultWalkDays, Limit: DefaultLimit}}
	q := &plan.out
	if p.WalkName != nil {
		name := strings.TrimSpace(*p.WalkName)
		if len(name) > MaxNameLength {
			return plan, &ParamError{"walk_name", fmt.Sprintf("at most %d characters", MaxNameLength)}
		}
		if plan.query.Name = walks.Normalize(name); plan.query.Name == "" {
			return plan, &ParamError{"walk_name", "needs letters or digits"}
		}
		q.WalkName = name
	}
	if p.Lat != nil || p.Lon != nil || plan.query.Name == "" {
		lat, lon, err := coordinates(p.Lat, p.Lon)
		if err != nil {
			if plan.query.Name == "" {
				err = &ParamError{"lat", "lat and lon (decimal degrees) are required, or a walk_name"}
			}
			return plan, err
		}
		radius := float64(DefaultWalkRadiusKm)
		if p.RadiusKm != nil {
			r := *p.RadiusKm
			if math.IsNaN(r) || r < 1 || r > MaxWalkRadiusKm {
				return plan, &ParamError{"radius_km", fmt.Sprintf("between 1 and %d", MaxWalkRadiusKm)}
			}
			radius = round(r, 1)
		}
		q.Lat, q.Lon, q.RadiusKm = &lat, &lon, &radius
		plan.query.HasPoint, plan.query.Lat, plan.query.Lon, plan.query.RadiusKm = true, lat, lon, radius
	} else if p.RadiusKm != nil {
		return plan, &ParamError{"radius_km", "needs lat and lon"}
	}
	today := walks.LocalDay(now)
	if p.Date != nil {
		if p.Days != nil {
			return plan, &ParamError{"date", "pass date or days, not both"}
		}
		day, err := parseDay("date", *p.Date, today, today+walkDateAhead)
		if err != nil {
			return plan, err
		}
		q.Date, q.Days, plan.query.Date = walks.DayName(day), 1, day
	} else if p.Days != nil {
		if *p.Days < 1 || *p.Days > MaxWalkDays {
			return plan, &ParamError{"days", fmt.Sprintf("between 1 and %d", MaxWalkDays)}
		}
		q.Days = *p.Days
	}
	if p.Limit != nil {
		if *p.Limit < 1 || *p.Limit > MaxLimit {
			return plan, &ParamError{"limit", fmt.Sprintf("between 1 and %d", MaxLimit)}
		}
		q.Limit = *p.Limit
	}
	if p.MaxDurationH != nil {
		d := *p.MaxDurationH
		if math.IsNaN(d) || d < 0.5 || d > MaxWalkDurationH {
			return plan, &ParamError{"max_duration_h", fmt.Sprintf("between 0.5 and %d", MaxWalkDurationH)}
		}
		d = round(d, 1)
		q.MaxDurationH, plan.query.MaxDurationH = &d, d
	}
	if p.MaxAscentM != nil {
		a := *p.MaxAscentM
		if a < 1 || a > MaxWalkAscentM {
			return plan, &ParamError{"max_ascent_m", fmt.Sprintf("between 1 and %d", MaxWalkAscentM)}
		}
		q.MaxAscentM, plan.query.MaxAscentM = &a, a
	}
	var err error
	if q.Detail, err = detail(p.Detail); err != nil {
		return plan, err
	}
	plan.hours = q.Detail == DetailHours
	plan.query.Days, plan.query.Limit, plan.query.Now = q.Days, q.Limit, now
	return plan, nil
}

func rfc3339(unix int64) string { return time.Unix(unix, 0).UTC().Format(time.RFC3339) }

func slotOut(r *walks.Run) *WalkSlotOut {
	if r == nil {
		return nil
	}
	return &WalkSlotOut{
		Start: r.Start.Format(time.RFC3339), End: r.End.Format(time.RFC3339), GoodHours: r.GoodHours,
		MaxWindKmh: r.MaxWindKmh, TotalPrecipMM: float32(round(float64(r.TotalPrecipMM), 1)),
		MinTempC: r.MinTempC, MaxTempC: r.MaxTempC,
	}
}

// WalkWindows ranks walks near a point, or by name, by the days they fit in
// good weather.
func (s *Service) WalkWindows(p WalkWindowsParams) (*Envelope, error) {
	now := s.Now()
	plan, err := walkQuery(p, now)
	if err != nil {
		return nil, err
	}
	var ds *walks.Dataset
	if s.Walks != nil {
		ds = s.Walks.Current()
	}
	if ds == nil {
		return nil, fmt.Errorf("%w: walks", ErrUnavailable)
	}
	q := plan.out
	dayNames := make([]string, q.Days)
	for i := range dayNames {
		dayNames[i] = walks.DayName(plan.query.FirstDay() + int32(i))
	}
	res := WalkWindowsResult{Query: q, Days: dayNames, Walks: []WalkOut{}}
	for _, r := range ds.Windows(plan.query) {
		w := r.Walk
		out := WalkOut{
			WalkID: w.ID, Name: w.Name, URL: w.URL, StartLat: w.Lat, StartLon: w.Lon,
			LengthKm: w.LengthKm, AscentM: w.AscentM, DurationH: w.DurationH,
			DoableDays: r.DoableDays, Days: make([]WalkDayOut, len(r.Days)),
		}
		if plan.query.HasPoint {
			km := round(r.DistanceKm, 1)
			out.FromPointKm = &km
		}
		if d, ok := r.FirstDoable(); ok {
			out.Best = &WalkBestOut{Date: walks.DayName(d.Day), WalkSlotOut: *slotOut(d.Best)}
		}
		for i, d := range r.Days {
			do := WalkDayOut{Date: walks.DayName(d.Day), ForecastCoverage: string(d.Coverage), GoodHours: d.GoodHours, BestSlot: slotOut(d.Best)}
			if d.Decided {
				doable := d.Doable
				do.Doable = &doable
			}
			if plan.hours {
				hours := make([]WalkHourOut, len(d.Hours))
				for j, h := range d.Hours {
					hours[j] = WalkHourOut{Time: rfc3339(h.Unix), TempC: h.TempC, PrecipMM: h.PrecipMM, WindKmh: h.WindKmh, CloudPct: h.CloudPct}
				}
				do.Hours = &hours
			}
			out.Days[i] = do
		}
		res.Walks = append(res.Walks, out)
	}
	meta := Meta{
		APIVersion:  APIVersion,
		GeneratedAt: now.UTC().Format(time.RFC3339),
		AsOf:        now.UTC().Truncate(time.Hour).Format(time.RFC3339),
		DataVersion: ds.Version,
		MethodURL:   s.MethodURL,
		Sources:     []Source{sourceMETWalks, sourceWalkhighlands},
		Notes:       append([]string{}, walkNotes...),
	}
	if !ds.FetchedAt.IsZero() {
		f := ds.FetchedAt.Format(time.RFC3339)
		meta.ForecastFetchedAt = &f
	}
	switch {
	case len(res.Walks) == 0 && q.WalkName != "":
		meta.Notes = append(meta.Notes, "No Walkhighlands walk name matches walk_name here. Try fewer words, a larger radius_km, or search by lat and lon.")
	case len(res.Walks) == 0:
		meta.Notes = append(meta.Notes, "No walks within the radius match. Try a larger radius_km or drop the duration and ascent limits.")
	}
	return &Envelope{
		Data:    res,
		Meta:    meta,
		ETag:    etag("walk_windows", ds.Version, meta.AsOf, canonical(q)),
		Results: len(res.Walks),
	}, nil
}
