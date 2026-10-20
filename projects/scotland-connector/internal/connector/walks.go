package connector

import (
	"fmt"
	"math"
	"strconv"
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
	"Follow each walk's url for the route, maps and safety notes.",
}

// WalkWindowsParams are the walk_windows inputs.
type WalkWindowsParams struct {
	Lat          *float64 `json:"lat,omitempty"`
	Lon          *float64 `json:"lon,omitempty"`
	RadiusKm     *float64 `json:"radius_km,omitempty"`
	Days         *int     `json:"days,omitempty"`
	Limit        *int     `json:"limit,omitempty"`
	MaxDurationH *float64 `json:"max_duration_h,omitempty"`
	MaxAscentM   *int     `json:"max_ascent_m,omitempty"`
}

// WalkQuery echoes the canonical inputs actually used.
type WalkQuery struct {
	Lat          float64  `json:"lat"`
	Lon          float64  `json:"lon"`
	RadiusKm     float64  `json:"radius_km"`
	Days         int      `json:"days"`
	Limit        int      `json:"limit"`
	MaxDurationH *float64 `json:"max_duration_h,omitempty"`
	MaxAscentM   *int     `json:"max_ascent_m,omitempty"`
}

// WalkHourOut is one viable hour.
type WalkHourOut struct {
	Time     string  `json:"time"`
	TempC    float32 `json:"temp_c"`
	PrecipMM float32 `json:"precip_mm"`
	WindKmh  float32 `json:"wind_kmh"`
	CloudPct float32 `json:"cloud_pct"`
}

// WalkSlotOut is a day's best slot.
type WalkSlotOut struct {
	Start     string `json:"start"`
	End       string `json:"end"`
	GoodHours int    `json:"good_hours"`
}

// WalkDayOut is one day at one walk. Doable is null when the forecast does
// not reach far enough to decide.
type WalkDayOut struct {
	Date             string        `json:"date"`
	ForecastCoverage string        `json:"forecast_coverage"`
	Doable           *bool         `json:"doable"`
	GoodHours        int           `json:"good_hours"`
	BestSlot         *WalkSlotOut  `json:"best_slot"`
	Hours            []WalkHourOut `json:"hours"`
}

// WalkOut is one ranked walk.
type WalkOut struct {
	WalkID      string       `json:"walk_id"`
	Name        string       `json:"name"`
	URL         string       `json:"url"`
	StartLat    float64      `json:"start_lat"`
	StartLon    float64      `json:"start_lon"`
	FromPointKm float64      `json:"from_point_km"`
	LengthKm    float64      `json:"length_km"`
	AscentM     int          `json:"ascent_m"`
	DurationH   float64      `json:"duration_h"`
	DoableDays  int          `json:"doable_days"`
	Days        []WalkDayOut `json:"days"`
}

// WalkWindowsResult is walk_windows' data.
type WalkWindowsResult struct {
	Query WalkQuery `json:"query"`
	Days  []string  `json:"days"`
	Walks []WalkOut `json:"walks"`
}

func walkQuery(p WalkWindowsParams) (WalkQuery, error) {
	q := WalkQuery{RadiusKm: DefaultWalkRadiusKm, Days: DefaultWalkDays, Limit: DefaultLimit}
	var err error
	if q.Lat, q.Lon, err = coordinates(p.Lat, p.Lon); err != nil {
		return q, err
	}
	if p.RadiusKm != nil {
		r := *p.RadiusKm
		if math.IsNaN(r) || r < 1 || r > MaxWalkRadiusKm {
			return q, &ParamError{"radius_km", fmt.Sprintf("between 1 and %d", MaxWalkRadiusKm)}
		}
		q.RadiusKm = round(r, 1)
	}
	if p.Days != nil {
		if *p.Days < 1 || *p.Days > MaxWalkDays {
			return q, &ParamError{"days", fmt.Sprintf("between 1 and %d", MaxWalkDays)}
		}
		q.Days = *p.Days
	}
	if p.Limit != nil {
		if *p.Limit < 1 || *p.Limit > MaxLimit {
			return q, &ParamError{"limit", fmt.Sprintf("between 1 and %d", MaxLimit)}
		}
		q.Limit = *p.Limit
	}
	if p.MaxDurationH != nil {
		d := *p.MaxDurationH
		if math.IsNaN(d) || d < 0.5 || d > MaxWalkDurationH {
			return q, &ParamError{"max_duration_h", fmt.Sprintf("between 0.5 and %d", MaxWalkDurationH)}
		}
		d = round(d, 1)
		q.MaxDurationH = &d
	}
	if p.MaxAscentM != nil {
		a := *p.MaxAscentM
		if a < 1 || a > MaxWalkAscentM {
			return q, &ParamError{"max_ascent_m", fmt.Sprintf("between 1 and %d", MaxWalkAscentM)}
		}
		q.MaxAscentM = &a
	}
	return q, nil
}

func rfc3339(unix int64) string { return time.Unix(unix, 0).UTC().Format(time.RFC3339) }

// WalkWindows ranks walks near a point by the days they fit in good weather.
func (s *Service) WalkWindows(p WalkWindowsParams) (*Envelope, error) {
	q, err := walkQuery(p)
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
	now := s.Now()
	wq := walks.Query{Lat: q.Lat, Lon: q.Lon, RadiusKm: q.RadiusKm, Days: q.Days, Limit: q.Limit, Now: now}
	if q.MaxDurationH != nil {
		wq.MaxDurationH = *q.MaxDurationH
	}
	if q.MaxAscentM != nil {
		wq.MaxAscentM = *q.MaxAscentM
	}
	res := WalkWindowsResult{Query: q, Days: wq.DayNames(), Walks: []WalkOut{}}
	for _, r := range ds.Windows(wq) {
		w := r.Walk
		out := WalkOut{
			WalkID: w.ID, Name: w.Name, URL: w.URL, StartLat: w.Lat, StartLon: w.Lon,
			FromPointKm: round(r.DistanceKm, 1), LengthKm: w.LengthKm, AscentM: w.AscentM, DurationH: w.DurationH,
			DoableDays: r.DoableDays, Days: make([]WalkDayOut, len(r.Days)),
		}
		for i, d := range r.Days {
			do := WalkDayOut{Date: walks.DayName(d.Day), ForecastCoverage: string(d.Coverage), GoodHours: d.GoodHours, Hours: make([]WalkHourOut, len(d.Hours))}
			if d.Decided {
				doable := d.Doable
				do.Doable = &doable
			}
			if d.Best != nil {
				do.BestSlot = &WalkSlotOut{Start: d.Best.Start.Format(time.RFC3339), End: d.Best.End.Format(time.RFC3339), GoodHours: d.Best.GoodHours}
			}
			for j, h := range d.Hours {
				do.Hours[j] = WalkHourOut{Time: rfc3339(h.Unix), TempC: h.TempC, PrecipMM: h.PrecipMM, WindKmh: h.WindKmh, CloudPct: h.CloudPct}
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
	if len(res.Walks) == 0 {
		meta.Notes = append(meta.Notes, "No walks within the radius match. Try a larger radius_km or drop the duration and ascent limits.")
	}
	key := []string{
		strconv.FormatFloat(q.Lat, 'f', 2, 64), strconv.FormatFloat(q.Lon, 'f', 2, 64),
		strconv.FormatFloat(q.RadiusKm, 'f', 1, 64), strconv.Itoa(q.Days), strconv.Itoa(q.Limit),
		"", "",
	}
	if q.MaxDurationH != nil {
		key[5] = strconv.FormatFloat(*q.MaxDurationH, 'f', 1, 64)
	}
	if q.MaxAscentM != nil {
		key[6] = strconv.Itoa(*q.MaxAscentM)
	}
	return &Envelope{
		Data:    res,
		Meta:    meta,
		ETag:    etag("walk_windows", ds.Version, meta.AsOf, strings.Join(key, ",")),
		Results: len(res.Walks),
	}, nil
}
