// Package connector is the transport-neutral core: it validates parameters,
// applies the caps, runs the query against the in-memory dataset and wraps
// the answer in the provenance envelope. The REST API and the MCP endpoint
// are thin adapters over the same calls, so the two can never disagree.
package connector

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"math"
	"strconv"
	"strings"
	"time"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/places"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/stars"
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
	MaxPlaceResults = 5
	MaxQueryLength  = 100
)

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

// Service answers queries.
type Service struct {
	Data   DatasetSource
	Places *places.Gazetteer
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

// --- resolve_place ---

// ResolvePlaceParams are the resolve_place inputs.
type ResolvePlaceParams struct {
	Query string `json:"query"`
}

// PlaceMatch is one resolve_place candidate.
type PlaceMatch struct {
	Name        string  `json:"name"`
	MatchedName string  `json:"matched_name"`
	Type        string  `json:"type"`
	Lat         float64 `json:"lat"`
	Lon         float64 `json:"lon"`
	Confidence  float64 `json:"confidence"`
}

// ResolvePlaceResult is resolve_place's data.
type ResolvePlaceResult struct {
	Query   string       `json:"query"`
	Matches []PlaceMatch `json:"matches"`
}

// ResolvePlace turns free text into candidate coordinates.
func (s *Service) ResolvePlace(p ResolvePlaceParams) (*Envelope, error) {
	q := strings.TrimSpace(p.Query)
	if q == "" {
		return nil, &ParamError{"query", "required: a Scottish place name, or \"lat, lon\""}
	}
	if len(q) > MaxQueryLength {
		return nil, &ParamError{"query", fmt.Sprintf("at most %d characters", MaxQueryLength)}
	}
	now := s.Now()
	res := ResolvePlaceResult{Query: q, Matches: []PlaceMatch{}}
	for _, m := range s.Places.Resolve(q, MaxPlaceResults) {
		if m.Type == "coordinates" && !places.InCoverage(m.Lat, m.Lon) {
			return nil, &ParamError{"query", "coordinates are outside Scotland, the area this service covers"}
		}
		res.Matches = append(res.Matches, PlaceMatch{
			Name: m.Name, MatchedName: m.MatchedName, Type: m.Type,
			Lat: m.Lat, Lon: m.Lon, Confidence: m.Confidence,
		})
	}
	meta := s.meta(nil, now, nil)
	meta.Sources = []Source{}
	meta.DataVersion = "gazetteer-seed"
	if len(res.Matches) == 0 {
		meta.Notes = append(meta.Notes, "No match in the gazetteer. Pass coordinates directly to dark_sky_windows as lat and lon.")
	}
	return &Envelope{
		Data:    res,
		Meta:    meta,
		ETag:    etag("resolve_place", meta.DataVersion, meta.AsOf, places.Normalize(q)),
		Results: len(res.Matches),
	}, nil
}

// --- dark_sky_windows ---

// DarkSkyParams are the dark_sky_windows inputs. Pointers distinguish
// "absent" (use the default) from an explicit value.
type DarkSkyParams struct {
	Lat      *float64 `json:"lat,omitempty"`
	Lon      *float64 `json:"lon,omitempty"`
	Place    string   `json:"place,omitempty"`
	RadiusKm *float64 `json:"radius_km,omitempty"`
	Nights   *int     `json:"nights,omitempty"`
	Limit    *int     `json:"limit,omitempty"`
}

// DarkSkyQuery echoes the canonical inputs actually used.
type DarkSkyQuery struct {
	Lat      float64     `json:"lat"`
	Lon      float64     `json:"lon"`
	RadiusKm float64     `json:"radius_km"`
	Nights   int         `json:"nights"`
	Limit    int         `json:"limit"`
	Place    *PlaceMatch `json:"place,omitempty"`
}

// HourOut is one clear hour.
type HourOut struct {
	Time              string  `json:"time"`
	CloudAreaFraction float32 `json:"cloud_area_fraction"`
	Dark              bool    `json:"dark"`
}

// NightOut is one night at one site.
type NightOut struct {
	Night         string    `json:"night"`
	ClearDark     int       `json:"clear_dark_hours"`
	ClearTwilight int       `json:"clear_twilight_hours"`
	Hours         []HourOut `json:"hours"`
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
	HoursTruncated bool        `json:"forecast_hours_truncated"`
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

func (s *Service) darkSkyQuery(p DarkSkyParams) (DarkSkyQuery, error) {
	q := DarkSkyQuery{RadiusKm: DefaultRadiusKm, Nights: DefaultNights, Limit: DefaultLimit}
	place := strings.TrimSpace(p.Place)
	switch {
	case place != "" && (p.Lat != nil || p.Lon != nil):
		return q, &ParamError{"place", "pass either place or lat and lon, not both"}
	case place != "":
		if len(place) > MaxQueryLength {
			return q, &ParamError{"place", fmt.Sprintf("at most %d characters", MaxQueryLength)}
		}
		ms := s.Places.Resolve(place, 1)
		if len(ms) == 0 {
			return q, &ParamError{"place", "unknown place; pass lat and lon instead"}
		}
		m := ms[0]
		q.Lat, q.Lon = m.Lat, m.Lon
		q.Place = &PlaceMatch{Name: m.Name, MatchedName: m.MatchedName, Type: m.Type, Lat: m.Lat, Lon: m.Lon, Confidence: m.Confidence}
	case p.Lat == nil || p.Lon == nil:
		return q, &ParamError{"lat", "lat and lon (decimal degrees) or place is required"}
	default:
		q.Lat, q.Lon = *p.Lat, *p.Lon
	}
	if math.IsNaN(q.Lat) || math.IsNaN(q.Lon) || !places.InCoverage(q.Lat, q.Lon) {
		return q, &ParamError{"lat", "coordinates are outside Scotland, the area this service covers"}
	}
	// About 1 km: finer than any answer and it makes equal questions equal.
	q.Lat, q.Lon = round(q.Lat, 2), round(q.Lon, 2)

	if p.RadiusKm != nil {
		r := *p.RadiusKm
		if math.IsNaN(r) || r < 1 || r > MaxRadiusKm {
			return q, &ParamError{"radius_km", fmt.Sprintf("between 1 and %d", MaxRadiusKm)}
		}
		q.RadiusKm = round(r, 1)
	}
	if p.Nights != nil {
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

// DarkSkyWindows ranks dark-sky sites near a point for the next few nights.
func (s *Service) DarkSkyWindows(p DarkSkyParams) (*Envelope, error) {
	q, err := s.darkSkyQuery(p)
	if err != nil {
		return nil, err
	}
	ds := s.Data.Current()
	if ds == nil {
		return nil, ErrUnavailable
	}
	now := s.Now()
	wq := stars.WindowsQuery{Lat: q.Lat, Lon: q.Lon, RadiusKm: q.RadiusKm, Nights: q.Nights, Limit: q.Limit, Now: now}
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
			HoursTruncated: w.Site.Truncated(),
			Nights:         make([]NightOut, len(w.Nights)),
		}
		truncated = truncated || out.HoursTruncated
		for i, n := range w.Nights {
			no := NightOut{Night: n.Night, ClearDark: n.ClearDark, ClearTwilight: n.ClearTwilight, Hours: make([]HourOut, len(n.Hours))}
			for j, h := range n.Hours {
				no.Hours[j] = HourOut{
					Time:              time.Unix(h.Unix, 0).UTC().Format(time.RFC3339),
					CloudAreaFraction: h.Cloud,
					Dark:              h.Dark,
				}
			}
			out.Nights[i] = no
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
	if truncated {
		meta.Notes = append(meta.Notes, "Some sites have more clear hours than the snapshot lists; their later nights may be undercounted (forecast_hours_truncated).")
	}
	if len(res.Sites) == 0 {
		meta.Notes = append(meta.Notes, "No dark-sky sites within the radius. Try a larger radius_km.")
	}
	key := strings.Join([]string{
		strconv.FormatFloat(q.Lat, 'f', 2, 64), strconv.FormatFloat(q.Lon, 'f', 2, 64),
		strconv.FormatFloat(q.RadiusKm, 'f', 1, 64), strconv.Itoa(q.Nights), strconv.Itoa(q.Limit),
	}, ",")
	placeKey := ""
	if q.Place != nil {
		placeKey = q.Place.Name
	}
	return &Envelope{
		Data:    res,
		Meta:    meta,
		ETag:    etag("dark_sky_windows", ds.Version, meta.AsOf, key, placeKey),
		Results: len(res.Sites),
	}, nil
}
