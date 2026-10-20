// Package connector is the transport-neutral core: it validates parameters,
// applies the caps, runs the query against the in-memory dataset and wraps
// the answer in the provenance envelope. The REST API and the MCP endpoint
// are thin adapters over the same calls, so the two can never disagree.
package connector

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"strings"
	"time"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/stars"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/walks"
)

// APIVersion is the path prefix and the envelope's api_version.
const APIVersion = "v1"

// Caps. The connector answers narrow questions; nothing lists the corpus.
// openapi.json documents the same numbers and a test keeps them equal.
const (
	DefaultRadiusKm = 50
	MaxRadiusKm     = 150
	DefaultNights   = 3
	MaxNights       = 7
	DefaultLimit    = 5
	MaxLimit        = 10

	DefaultWalkRadiusKm = 30
	MaxWalkRadiusKm     = 100
	// Walk days stop at three: MET Norway's hourly forecast reaches about two
	// and a half days, and the hikes job keeps hourly steps only.
	DefaultWalkDays  = 3
	MaxWalkDays      = 3
	MaxWalkDurationH = 24
	MaxWalkAscentM   = 3000
	MaxNameLength    = 100
	// MaxWalkDateAhead is how far ahead a walk date may be asked about. Days
	// past the hourly forecast come back with forecast_coverage "none".
	MaxWalkDateAhead = 13
)

// Detail levels. Summary answers lead with the best night or slot and leave
// out the hourly arrays, which are most of an answer's size.
const (
	DetailSummary = "summary"
	DetailHours   = "hours"
)

func detail(p *string) (string, error) {
	if p == nil || *p == "" || *p == DetailSummary {
		return DetailSummary, nil
	}
	if *p == DetailHours {
		return DetailHours, nil
	}
	return "", &ParamError{"detail", `"summary" or "hours"`}
}

func dayName(day int32) string { return time.Unix(int64(day)*86400, 0).UTC().Format("2006-01-02") }

// parseDay reads a YYYY-MM-DD date as days since 1970-01-01 and keeps it
// within [lo, hi].
func parseDay(param, raw string, lo, hi int32) (int32, error) {
	msg := fmt.Sprintf("a date from %s to %s (YYYY-MM-DD)", dayName(lo), dayName(hi))
	t, err := time.Parse("2006-01-02", strings.TrimSpace(raw))
	if err != nil {
		return 0, &ParamError{param, msg}
	}
	day := int32(t.Unix() / 86400)
	if day < lo || day > hi {
		return 0, &ParamError{param, msg}
	}
	return day, nil
}

// canonical is the ETag key for a query echo: its JSON, whose field order is
// fixed and whose values are already rounded, so equal questions match.
func canonical(q any) string {
	b, err := json.Marshal(q)
	if err != nil {
		panic(err) // query echoes are plain structs
	}
	return string(b)
}

// Coverage is the bounding box the connector answers for: mainland Scotland,
// the Hebrides, Orkney and Shetland. openapi.json and the MCP input schema
// carry the same bounds.
var Coverage = struct{ MinLat, MaxLat, MinLon, MaxLon float64 }{54.5, 61.0, -9.0, -0.5}

// InCoverage reports whether a coordinate is inside Coverage.
func InCoverage(lat, lon float64) bool {
	return lat >= Coverage.MinLat && lat <= Coverage.MaxLat && lon >= Coverage.MinLon && lon <= Coverage.MaxLon
}

// ErrUnavailable means no dataset has loaded yet.
var ErrUnavailable = errors.New("connector: forecast data not loaded yet")

// ParamError is a caller mistake the caller can fix.
type ParamError struct {
	Param   string `json:"param"`
	Message string `json:"message"`
}

func (e *ParamError) Error() string { return e.Param + ": " + e.Message }

// DatasetSource is the store.
type DatasetSource interface {
	Current() *stars.Dataset
}

// WalksSource is the walk store.
type WalksSource interface {
	Current() *walks.Dataset
}

// Service answers queries.
type Service struct {
	Data  DatasetSource
	Walks WalksSource
	// MethodURL is published in every envelope.
	MethodURL string
	Now       func() time.Time
}

// Source is one attribution in the envelope.
type Source struct {
	Name        string `json:"name"`
	Attribution string `json:"attribution"`
	License     string `json:"license"`
	URL         string `json:"url"`
	Use         string `json:"use"`
}

// Meta is the provenance block on every response.
type Meta struct {
	APIVersion  string `json:"api_version"`
	GeneratedAt string `json:"generated_at"`
	// AsOf is the top of the hour the answer is computed for: elapsed hours
	// are dropped from it, and responses for the same inputs within the hour
	// are identical.
	AsOf string `json:"as_of"`
	// ForecastFetchedAt is the newest MET Norway fetch in the snapshot.
	ForecastFetchedAt *string  `json:"forecast_fetched_at"`
	DataVersion       string   `json:"data_version"`
	MethodURL         string   `json:"method_url"`
	Sources           []Source `json:"sources"`
	Notes             []string `json:"notes,omitempty"`
}

// Envelope is the response body shape for every successful call.
type Envelope struct {
	Data any  `json:"data"`
	Meta Meta `json:"meta"`
	// ETag is a strong validator for Data: it folds the dataset version, the
	// as-of hour and the canonical parameters. Not serialized.
	ETag string `json:"-"`
	// Results counts returned items, for the meter.
	Results int `json:"-"`
}

var (
	sourceMET = Source{
		Name:        "MET Norway Locationforecast",
		Attribution: "Weather forecast data from MET Norway",
		License:     "CC BY 4.0",
		URL:         "https://api.met.no/doc/License",
		Use:         "hourly cloud cover forecast",
	}
	sourceCERRA = Source{
		Name:        "Copernicus CERRA regional reanalysis",
		Attribution: "Generated using Copernicus Climate Change Service information",
		License:     "Licence to use Copernicus Products",
		URL:         "https://cds.climate.copernicus.eu/",
		Use:         "monthly clear-sky climatology",
	}
	sourceOSM = Source{
		Name:        "OpenStreetMap",
		Attribution: "© OpenStreetMap contributors",
		License:     "ODbL 1.0",
		URL:         "https://www.openstreetmap.org/copyright",
		Use:         "road access filter for the dark-sky site grid",
	}
	// The light-pollution render the grid samples is not attributed until its
	// licence is confirmed (#7253).
	sourceNaturalEarth = Source{
		Name:        "Natural Earth",
		Attribution: "Made with Natural Earth",
		License:     "Public domain",
		URL:         "https://www.naturalearthdata.com/about/terms-of-use/",
		Use:         "Scotland land boundary for the site grid",
	}
)

func (s *Service) meta(ds *stars.Dataset, now time.Time, sources []Source) Meta {
	m := Meta{
		APIVersion:  APIVersion,
		GeneratedAt: now.UTC().Format(time.RFC3339),
		AsOf:        now.UTC().Truncate(time.Hour).Format(time.RFC3339),
		MethodURL:   s.MethodURL,
		Sources:     sources,
	}
	if ds != nil {
		m.DataVersion = ds.Version
		if !ds.FetchedAt.IsZero() {
			f := ds.FetchedAt.Format(time.RFC3339)
			m.ForecastFetchedAt = &f
		}
	}
	return m
}

func etag(parts ...string) string {
	sum := sha256.Sum256([]byte(strings.Join(parts, "\x00")))
	return `"` + hex.EncodeToString(sum[:12]) + `"`
}

// --- dark_sky_windows ---

// DarkSkyParams are the dark_sky_windows inputs. Pointers distinguish
// "absent" (use the default) from an explicit value.
type DarkSkyParams struct {
	Lat      *float64 `json:"lat,omitempty"`
	Lon      *float64 `json:"lon,omitempty"`
	RadiusKm *float64 `json:"radius_km,omitempty"`
	Night    *string  `json:"night,omitempty"`
	Nights   *int     `json:"nights,omitempty"`
	Limit    *int     `json:"limit,omitempty"`
	Detail   *string  `json:"detail,omitempty"`
}

// DarkSkyQuery echoes the canonical inputs actually used.
type DarkSkyQuery struct {
	Lat      float64 `json:"lat"`
	Lon      float64 `json:"lon"`
	RadiusKm float64 `json:"radius_km"`
	Night    string  `json:"night,omitempty"`
	Nights   int     `json:"nights"`
	Limit    int     `json:"limit"`
	Detail   string  `json:"detail"`
}

// HourOut is one clear hour.
type HourOut struct {
	Time              string  `json:"time"`
	CloudAreaFraction float32 `json:"cloud_area_fraction"`
	Dark              bool    `json:"dark"`
	MoonAltitudeDeg   float32 `json:"moon_altitude_deg"`
}

// NightOut is one night at one site. ClearFrom and ClearUntil bound its clear
// hours, which may have gaps; Hours only come with detail=hours.
type NightOut struct {
	Night            string     `json:"night"`
	ClearDark        int        `json:"clear_dark_hours"`
	ClearTwilight    int        `json:"clear_twilight_hours"`
	MoonlessDark     int        `json:"moonless_clear_dark_hours"`
	MoonIllumination float64    `json:"moon_illumination"`
	ClearFrom        *string    `json:"clear_from"`
	ClearUntil       *string    `json:"clear_until"`
	Hours            *[]HourOut `json:"hours,omitempty"`
}

// ClimateOut is the site's historical month.
type ClimateOut struct {
	Month        int      `json:"month"`
	ClearDark    int      `json:"clear_dark_hours"`
	Dark         int      `json:"dark_hours"`
	ClearSkyOdds *float64 `json:"clear_sky_odds"`
}

// SiteOut is one ranked site.
type SiteOut struct {
	SiteID         string      `json:"site_id"`
	Lat            float64     `json:"lat"`
	Lon            float64     `json:"lon"`
	DistanceKm     float64     `json:"distance_km"`
	AltitudeM      int         `json:"altitude_m"`
	LightPollution string      `json:"light_pollution_zone"`
	ClearDark      int         `json:"clear_dark_hours"`
	ClearTwilight  int         `json:"clear_twilight_hours"`
	MoonlessDark   int         `json:"moonless_clear_dark_hours"`
	HoursTruncated bool        `json:"forecast_hours_truncated"`
	BestNight      *NightOut   `json:"best_night"`
	Nights         []NightOut  `json:"nights"`
	Climatology    *ClimateOut `json:"climatology"`
}

// DarkSkyResult is dark_sky_windows' data.
type DarkSkyResult struct {
	Query        DarkSkyQuery `json:"query"`
	DarknessMode string       `json:"darkness_mode"`
	Nights       []string     `json:"nights"`
	Sites        []SiteOut    `json:"sites"`
}

func round(v float64, places int) float64 {
	p := math.Pow(10, float64(places))
	return math.Round(v*p) / p
}

// coordinates validates a required point inside Coverage and rounds it to
// about 1 km: finer than any answer, and it makes equal questions equal.
func coordinates(lat, lon *float64) (float64, float64, error) {
	if lat == nil || lon == nil {
		return 0, 0, &ParamError{"lat", "lat and lon (decimal degrees) are required"}
	}
	if math.IsNaN(*lat) || math.IsNaN(*lon) || !InCoverage(*lat, *lon) {
		return 0, 0, &ParamError{"lat", "coordinates are outside Scotland, the area this service covers"}
	}
	return round(*lat, 2), round(*lon, 2), nil
}

func darkSkyQuery(p DarkSkyParams, now time.Time) (q DarkSkyQuery, night int32, err error) {
	q = DarkSkyQuery{RadiusKm: DefaultRadiusKm, Nights: DefaultNights, Limit: DefaultLimit}
	if q.Detail, err = detail(p.Detail); err != nil {
		return q, 0, err
	}
	if p.Night != nil && strings.TrimSpace(*p.Night) != "" {
		if p.Nights != nil {
			return q, 0, &ParamError{"night", "pass night or nights, not both"}
		}
		first := stars.FirstNight(now)
		if night, err = parseDay("night", *p.Night, first, first+MaxNights-1); err != nil {
			return q, 0, err
		}
		q.Night, q.Nights = dayName(night), 1
	}
	q2, err := darkSkyCaps(p, q)
	return q2, night, err
}

func darkSkyCaps(p DarkSkyParams, q DarkSkyQuery) (DarkSkyQuery, error) {
	var err error
	if q.Lat, q.Lon, err = coordinates(p.Lat, p.Lon); err != nil {
		return q, err
	}

	if p.RadiusKm != nil {
		r := *p.RadiusKm
		if math.IsNaN(r) || r < 1 || r > MaxRadiusKm {
			return q, &ParamError{"radius_km", fmt.Sprintf("between 1 and %d", MaxRadiusKm)}
		}
		q.RadiusKm = round(r, 1)
	}
	if p.Nights != nil && q.Night == "" {
		if *p.Nights < 1 || *p.Nights > MaxNights {
			return q, &ParamError{"nights", fmt.Sprintf("between 1 and %d", MaxNights)}
		}
		q.Nights = *p.Nights
	}
	if p.Limit != nil {
		if *p.Limit < 1 || *p.Limit > MaxLimit {
			return q, &ParamError{"limit", fmt.Sprintf("between 1 and %d", MaxLimit)}
		}
		q.Limit = *p.Limit
	}
	return q, nil
}

func nightOut(n stars.NightWindow, hours bool) NightOut {
	no := NightOut{
		Night: n.Night, ClearDark: n.ClearDark, ClearTwilight: n.ClearTwilight,
		MoonlessDark: n.MoonlessDark, MoonIllumination: round(n.MoonLit, 2),
	}
	if len(n.Hours) > 0 {
		from := rfc3339(n.Hours[0].Unix)
		until := rfc3339(n.Hours[len(n.Hours)-1].Unix + 3600)
		no.ClearFrom, no.ClearUntil = &from, &until
	}
	if hours {
		out := make([]HourOut, len(n.Hours))
		for j, h := range n.Hours {
			out[j] = HourOut{
				Time:              rfc3339(h.Unix),
				CloudAreaFraction: h.Cloud,
				Dark:              h.Dark,
				MoonAltitudeDeg:   float32(round(float64(n.MoonAlt[j]), 1)),
			}
		}
		no.Hours = &out
	}
	return no
}

// darkScore weighs a night's clear-dark hours by the moon: a moonless hour
// counts 1 and a moonlit one 1 minus the moon's lit fraction, so a long
// clear night under a thin crescent beats one moonless hour, and a short
// moonless night beats a long one under a full moon.
func darkScore(n *stars.NightWindow) float64 {
	return float64(n.MoonlessDark) + float64(n.ClearDark-n.MoonlessDark)*(1-n.MoonLit)
}

// BestNight is the night to go: the highest darkScore, then most
// clear-twilight hours (the midsummer fallback), earliest first. Nil when no
// night has a clear hour.
func BestNight(nights []stars.NightWindow) *stars.NightWindow {
	var best *stars.NightWindow
	for i := range nights {
		n := &nights[i]
		if n.ClearTwilight == 0 {
			continue
		}
		if best == nil {
			best = n
			continue
		}
		if a, b := darkScore(n), darkScore(best); a > b+1e-9 || (math.Abs(a-b) <= 1e-9 && n.ClearTwilight > best.ClearTwilight) {
			best = n
		}
	}
	return best
}

// DarkSkyWindows ranks dark-sky sites near a point for the next few nights,
// or for one named night.
func (s *Service) DarkSkyWindows(p DarkSkyParams) (*Envelope, error) {
	now := s.Now()
	q, night, err := darkSkyQuery(p, now)
	if err != nil {
		return nil, err
	}
	ds := s.Data.Current()
	if ds == nil {
		return nil, ErrUnavailable
	}
	hours := q.Detail == DetailHours
	wq := stars.WindowsQuery{Lat: q.Lat, Lon: q.Lon, RadiusKm: q.RadiusKm, Nights: q.Nights, Limit: q.Limit, Night: night, Now: now}
	res := DarkSkyResult{Query: q, DarknessMode: ds.Darkness, Nights: wq.NightNames(), Sites: []SiteOut{}}
	truncated := false
	for _, w := range ds.Windows(wq) {
		out := SiteOut{
			SiteID:         w.Site.ID,
			Lat:            w.Site.Lat,
			Lon:            w.Site.Lon,
			DistanceKm:     round(w.DistanceKm, 1),
			AltitudeM:      w.Site.AltitudeM,
			LightPollution: w.Site.LPZone,
			ClearDark:      w.ClearDark,
			ClearTwilight:  w.ClearTwilight,
			MoonlessDark:   w.MoonlessDark,
			HoursTruncated: w.Site.Truncated(),
			Nights:         make([]NightOut, len(w.Nights)),
		}
		truncated = truncated || out.HoursTruncated
		for i, n := range w.Nights {
			out.Nights[i] = nightOut(n, hours)
		}
		if b := BestNight(w.Nights); b != nil {
			bn := nightOut(*b, false)
			out.BestNight = &bn
		}
		if c := w.Climate; c != nil {
			co := &ClimateOut{Month: c.Month, ClearDark: c.ClearDark, Dark: c.Dark}
			if c.Dark > 0 {
				odds := round(float64(c.ClearDark)/float64(c.Dark), 3)
				co.ClearSkyOdds = &odds
			}
			out.Climatology = co
		}
		res.Sites = append(res.Sites, out)
	}
	meta := s.meta(ds, now, []Source{sourceMET, sourceCERRA, sourceOSM, sourceNaturalEarth})
	meta.Notes = append(meta.Notes,
		"best_night is each site's darkest clear night: clear-dark hours with the moon set count fully, moonlit ones by how little of the moon is lit. Pass detail=hours for each clear hour with the moon's altitude.")
	if truncated {
		meta.Notes = append(meta.Notes, "Some sites have more clear hours than the snapshot lists; their later nights may be undercounted (forecast_hours_truncated).")
	}
	if len(res.Sites) == 0 {
		meta.Notes = append(meta.Notes, "No dark-sky sites within the radius. Try a larger radius_km.")
	}
	return &Envelope{
		Data:    res,
		Meta:    meta,
		ETag:    etag("dark_sky_windows", ds.Version, meta.AsOf, canonical(q)),
		Results: len(res.Sites),
	}, nil
}
