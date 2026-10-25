package connector

import (
	"time"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/aurora"
)

type AuroraParams struct {
	Lat *float64 `json:"lat"`
	Lon *float64 `json:"lon"`
	At  string   `json:"at"`
}

// Aurora returns an explained heuristic at the requested instant. Unlike plan,
// this response must never be served from a cache after source expiry.
func (s *Service) Aurora(p AuroraParams) (*Envelope, error) {
	// Validate using existing bounds, but do not round away terrain identity.
	if _, _, err := coordinates(p.Lat, p.Lon); err != nil {
		return nil, err
	}
	if len(p.At) > 40 {
		return nil, &ParamError{"at", "RFC3339 timestamp required"}
	}
	at, err := time.Parse(time.RFC3339, p.At)
	if err != nil {
		return nil, &ParamError{"at", "RFC3339 timestamp with timezone required"}
	}
	now := s.Now().UTC()
	var ds *aurora.Snapshot
	if s.AuroraData != nil {
		ds = s.AuroraData.Current()
	}
	a := aurora.Assess(ds, *p.Lat, *p.Lon, at.UTC(), now)
	m := s.meta(nil, nil, now, []Source{
		{Name: "NOAA SWPC OVATION", Attribution: "NOAA Space Weather Prediction Center", License: "US government public domain data", URL: "https://www.spaceweather.gov/products/aurora-30-minute-forecast", Use: "nearest auroral intensity grid cell at the published forecast instant"},
		sourceMET,
		{Name: "USGS GMTED2010", Attribution: "US Geological Survey, Danielson and Gesch (2011)", License: "Public domain", URL: "https://doi.org/10.3133/ofr20111073", Use: "precomputed sampled northern terrain horizons, not trees or buildings"},
	})
	m.AsOf = now.Format(time.RFC3339Nano)
	m.Notes = []string{"Heuristic, not calibrated local sighting probability. Snapshot-only. Missing coverage remains unknown.", "NOAA support is the forecast instant only, not a fixed 90-minute interval. Freshness TTLs are application policy."}
	if ds != nil {
		m.DataVersion = ds.Version
		f := ds.Sites[0].Weather.CollectedAt
		for _, site := range ds.Sites {
			if site.Weather.CollectedAt.Before(f) {
				f = site.Weather.CollectedAt
			}
		}
		v := f.Format(time.RFC3339)
		m.ForecastFetchedAt = &v
	}
	return &Envelope{Data: a, Meta: m, Results: 1}, nil
}
