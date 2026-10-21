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
// openapi.json and the MCP input schemas carry the same numbers and tests
// keep them equal.
const (
	DefaultRadiusKm = 30
	MaxRadiusKm     = 100
	// A plan covers up to a week, starting no earlier than today and ending
	// no later than MaxDaysAhead days out; days past the hourly forecast
	// come back with verdict unknown.
	DefaultSpanDays = 3
	MaxSpanDays     = 7
	MaxDaysAhead    = 13
	DefaultOptions  = 3
	MaxOptions      = 5
	MaxCheckItems   = 10
	MaxDurationH    = 24
	MaxAscentM      = 3000
	MaxNameLength   = 100
)

// Alternatives in a check: walks starting within nearbyWalkKm of the checked
// walk, and dark-sky sites within siteKm of the checked point (widened to
// siteFarKm when none is that close).
const (
	nearbyWalkKm = 20
	siteKm       = 10
	siteFarKm    = 30
	// siteSpacingKm keeps a plan's stargazing options apart: grid sites
	// 2 km apart are one place to a person.
	siteSpacingKm = 5
)

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
		Use:         "hourly cloud, rain, wind and temperature forecasts",
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

// meta is the provenance block for an answer drawing on sky, walks or both
// (either may be nil). forecast_fetched_at is the older of the two fetches,
// so it never overstates freshness.
func (s *Service) meta(sky *stars.Dataset, wk *walks.Dataset, now time.Time, sources []Source) Meta {
	m := Meta{
		APIVersion:  APIVersion,
		GeneratedAt: now.UTC().Format(time.RFC3339),
		AsOf:        now.UTC().Truncate(time.Hour).Format(time.RFC3339),
		MethodURL:   s.MethodURL,
		Sources:     sources,
	}
	var versions []string
	var fetched time.Time
	older := func(t time.Time) {
		if !t.IsZero() && (fetched.IsZero() || t.Before(fetched)) {
			fetched = t
		}
	}
	if sky != nil {
		versions = append(versions, sky.Version)
		older(sky.FetchedAt)
	}
	if wk != nil {
		versions = append(versions, wk.Version)
		older(wk.FetchedAt)
	}
	m.DataVersion = strings.Join(versions, "|")
	if !fetched.IsZero() {
		f := fetched.Format(time.RFC3339)
		m.ForecastFetchedAt = &f
	}
	return m
}

func etag(parts ...string) string {
	sum := sha256.Sum256([]byte(strings.Join(parts, "\x00")))
	return `"` + hex.EncodeToString(sum[:12]) + `"`
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
