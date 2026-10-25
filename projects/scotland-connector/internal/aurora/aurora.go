// Package aurora evaluates one source-supported instant, never a multi-day forecast.
// Snapshot construction is separate from the request path.
package aurora

import (
	"bytes"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"time"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/astro"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/stars"
)

const (
	MaxBytes = 16 << 20
	MaxSites = 128
	MaxHours = 96
	// These are application freshness limits, not upstream validity fields.
	NOAAMaxAge        = 20 * time.Minute
	ObservationMaxAge = 2 * time.Hour
	METMaxAge         = 4 * time.Hour
	METIssueMaxAge    = 6 * time.Hour
	Policy            = "aurora-point-v1"
)

type NOAA struct {
	ObservedAt  time.Time `json:"observed_at"`
	ForecastAt  time.Time `json:"forecast_at"`
	CollectedAt time.Time `json:"collected_at"`
	// Cells use the provider's [longitude, latitude, intensity] order.
	Cells []Cell `json:"cells"`
}

// Cell rejects missing/null numbers rather than accepting encoding/json's zero.
type Cell []float64

func (c *Cell) UnmarshalJSON(b []byte) error {
	var raw []*float64
	if err := json.Unmarshal(b, &raw); err != nil {
		return err
	}
	if len(raw) != 3 {
		return fmt.Errorf("aurora: cell needs three numbers")
	}
	*c = nil
	for _, v := range raw {
		if v == nil || !finite(*v) {
			return fmt.Errorf("aurora: missing cell number")
		}
		*c = append(*c, *v)
	}
	return nil
}

type Hour struct {
	Time  time.Time `json:"time"`
	Cloud *float64  `json:"cloud_pct"`
}

type Weather struct {
	IssuedAt    time.Time `json:"issued_at"`
	CollectedAt time.Time `json:"collected_at"`
	Hours       []Hour    `json:"hours"`
}

type Terrain struct {
	Source    string `json:"source"`
	SHA256    string `json:"sha256"`
	Algorithm string `json:"algorithm"`
	// 19 rays, azimuth -45 through +45 degrees, at five-degree spacing.
	// Null means outside the raster or nodata, not a zero-degree horizon.
	North []*float64 `json:"north_deg"`
}

type Site struct {
	ID      string   `json:"id"`
	Lat     *float64 `json:"lat"`
	Lon     *float64 `json:"lon"`
	Weather Weather  `json:"weather"`
	Terrain *Terrain `json:"terrain"`
}

type Snapshot struct {
	Version   string    `json:"-"`
	Schema    int       `json:"schema"`
	CreatedAt time.Time `json:"created_at"`
	NOAA      NOAA      `json:"noaa"`
	Sites     []Site    `json:"sites"`
}

func finite(v float64) bool { return !math.IsNaN(v) && !math.IsInf(v, 0) }
func point(lat, lon *float64) bool {
	return lat != nil && lon != nil && finite(*lat) && finite(*lon) && *lat >= 54.5 && *lat <= 61 && *lon >= -9 && *lon <= -.5
}

// Decode rejects extra fields and trailing documents, and caps bytes before parsing.
func Decode(body []byte, out any) error {
	if len(body) == 0 || len(body) > MaxBytes {
		return fmt.Errorf("aurora: payload size outside limit")
	}
	d := json.NewDecoder(bytes.NewReader(body))
	d.DisallowUnknownFields()
	if err := d.Decode(out); err != nil {
		return err
	}
	if err := d.Decode(new(any)); err != io.EOF {
		return fmt.Errorf("aurora: trailing JSON")
	}
	return nil
}

func Parse(body []byte) (*Snapshot, error) {
	var s Snapshot
	if err := Decode(body, &s); err != nil {
		return nil, err
	}
	if err := s.Validate(); err != nil {
		return nil, err
	}
	s.Version = fmt.Sprintf("%x", sha256.Sum256(body))
	return &s, nil
}

// Validate checks structure and chronology, not wall-clock freshness. Old data
// may remain in the store, but Assess rechecks every clock on every request.
func (s *Snapshot) Validate() error {
	bad := func() error { return fmt.Errorf("aurora: invalid snapshot contract") }
	n := s.NOAA
	if s.Schema != 1 || s.CreatedAt.IsZero() || n.ObservedAt.IsZero() || n.ForecastAt.Before(n.ObservedAt) || n.CollectedAt.Before(n.ObservedAt) || n.CollectedAt.After(s.CreatedAt) || n.ForecastAt.Sub(n.ObservedAt) > 2*time.Hour || len(n.Cells) == 0 || len(n.Cells) > 65160 || len(s.Sites) == 0 || len(s.Sites) > MaxSites {
		return bad()
	}
	seenCells := map[[2]float64]bool{}
	for _, c := range n.Cells {
		if len(c) != 3 || !finite(c[0]) || !finite(c[1]) || !finite(c[2]) || c[0] < 0 || c[0] >= 360 || c[1] < -90 || c[1] > 90 || c[2] < 0 || c[2] > 100 {
			return bad()
		}
		key := [2]float64{c[0], c[1]}
		if seenCells[key] {
			return bad()
		}
		seenCells[key] = true
	}
	seen := map[string]bool{}
	for _, site := range s.Sites {
		w := site.Weather
		if site.ID == "" || len(site.ID) > 100 || seen[site.ID] || !point(site.Lat, site.Lon) || w.IssuedAt.IsZero() || w.CollectedAt.Before(w.IssuedAt) || w.CollectedAt.After(s.CreatedAt) || len(w.Hours) > MaxHours {
			return bad()
		}
		seen[site.ID] = true
		for i, h := range w.Hours {
			if h.Time.IsZero() || h.Time.Before(w.IssuedAt) || (i > 0 && !h.Time.After(w.Hours[i-1].Time)) || (h.Cloud != nil && (!finite(*h.Cloud) || *h.Cloud < 0 || *h.Cloud > 100)) {
				return bad()
			}
		}
		if tr := site.Terrain; tr != nil {
			if tr.Source != "USGS GMTED2010 mean 7.5 arc-second" || tr.Algorithm != TerrainAlgorithm || len(tr.SHA256) != 64 || len(tr.North) != 19 {
				return bad()
			}
			for _, c := range tr.SHA256 {
				if !((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f')) {
					return bad()
				}
			}
			for _, h := range tr.North {
				if h != nil && (!finite(*h) || *h < 0 || *h > 90) {
					return bad()
				}
			}
		}
	}
	return nil
}

type InputEvidence struct {
	Status string `json:"status"`
	Why    string `json:"why"`
}

type Assessment struct {
	Verdict      string                   `json:"verdict"`
	Why          string                   `json:"why"`
	At           time.Time                `json:"at"`
	SiteID       string                   `json:"site_id,omitempty"`
	SiteLat      *float64                 `json:"site_lat,omitempty"`
	SiteLon      *float64                 `json:"site_lon,omitempty"`
	DistanceKm   *float64                 `json:"distance_km,omitempty"`
	Policy       string                   `json:"policy"`
	Evidence     map[string]InputEvidence `json:"evidence"`
	NOAA         *NOAATiming              `json:"noaa,omitempty"`
	Weather      *Weather                 `json:"weather,omitempty"`
	Terrain      *Terrain                 `json:"terrain,omitempty"`
	Intensity    *float64                 `json:"noaa_cell_intensity,omitempty"`
	Cell         []float64                `json:"noaa_cell_lon_lat,omitempty"`
	Cloud        *float64                 `json:"cloud_pct,omitempty"`
	SunElevation *float64                 `json:"sun_elevation_deg,omitempty"`
}

type NOAATiming struct {
	ObservedAt      time.Time `json:"observed_at"`
	ForecastAt      time.Time `json:"forecast_at"`
	CollectedAt     time.Time `json:"collected_at"`
	PolicyExpiresAt time.Time `json:"policy_expires_at"`
	Support         string    `json:"support"`
	NoLead          bool      `json:"no_lead"`
}

func fresh(t, now time.Time, age time.Duration) bool {
	return !t.IsZero() && !t.After(now) && now.Sub(t) < age
}

// Assess requires every input before returning even an unfavorable assertion.
// NOAA publishes a forecast instant, not a validity interval. v1 supports only
// that exact instant; the TTL never widens this temporal support.
func Assess(s *Snapshot, lat, lon float64, at, now time.Time) Assessment {
	a := Assessment{Verdict: "unknown", Why: "insufficient input coverage", At: at, Policy: Policy, Evidence: map[string]InputEvidence{}}
	for _, k := range []string{"noaa", "weather", "terrain", "darkness"} {
		a.Evidence[k] = InputEvidence{"unknown", "not available"}
	}
	if s == nil {
		a.Why = "aurora is disabled or its snapshot is not loaded"
		return a
	}
	n := s.NOAA
	a.NOAA = &NOAATiming{n.ObservedAt, n.ForecastAt, n.CollectedAt, n.CollectedAt.Add(NOAAMaxAge), "forecast instant only; no upstream valid-until supplied", n.ForecastAt.Equal(n.ObservedAt)}
	if s.CreatedAt.After(now) || !fresh(n.CollectedAt, now, NOAAMaxAge) || !fresh(n.ObservedAt, now, ObservationMaxAge) || at.Before(now) || at.After(now.Add(2*time.Hour)) || !at.Equal(n.ForecastAt) {
		a.Evidence["noaa"] = InputEvidence{"unknown", "stale/future input or requested time outside the forecast instant"}
		return a
	}
	best := math.Inf(1)
	for _, c := range n.Cells {
		clon := c[0]
		if clon > 180 {
			clon -= 360
		}
		d := stars.DistanceKm(lat, lon, c[1], clon)
		if d < best {
			best = d
			value := c[2]
			a.Intensity = &value
			a.Cell = []float64{c[0], c[1]}
		}
	}
	if best > 80 {
		a.Intensity = nil
		a.Evidence["noaa"] = InputEvidence{"unknown", "no NOAA grid cell within 80 km"}
		return a
	}
	a.Evidence["noaa"] = InputEvidence{"supported", "nearest OVATION cell, not local sighting probability"}
	var site *Site
	best = math.Inf(1)
	for i := range s.Sites {
		p := &s.Sites[i]
		d := stars.DistanceKm(lat, lon, *p.Lat, *p.Lon)
		if d < best {
			best = d
			site = p
		}
	}
	if site == nil || best > 1 {
		a.Why = "no supported site within 1 km"
		return a
	}
	a.SiteID, a.SiteLat, a.SiteLon, a.DistanceKm = site.ID, site.Lat, site.Lon, &best
	a.Weather, a.Terrain = &site.Weather, site.Terrain
	w := site.Weather
	if fresh(w.CollectedAt, now, METMaxAge) && fresh(w.IssuedAt, now, METIssueMaxAge) {
		// No extrapolation from one row, and no bridging a missing/capped hour.
		for i, h := range w.Hours {
			if h.Cloud == nil {
				continue
			}
			if at.Equal(h.Time) {
				a.Cloud = h.Cloud
				break
			}
			if i+1 < len(w.Hours) {
				next := w.Hours[i+1]
				if at.After(h.Time) && at.Before(next.Time) && next.Time.Sub(h.Time) <= time.Hour && next.Cloud != nil {
					v := math.Max(*h.Cloud, *next.Cloud)
					a.Cloud = &v
					break
				}
			}
		}
	}
	if a.Cloud != nil {
		a.Evidence["weather"] = InputEvidence{"supported", "site MET instant, or conservative maximum of adjacent hourly instants"}
	}
	maxH := -1.0
	if tr := site.Terrain; tr != nil {
		maxH = 0
		for _, h := range tr.North {
			if h == nil {
				maxH = -1
				break
			}
			maxH = math.Max(maxH, *h)
		}
	}
	if maxH >= 0 {
		a.Evidence["terrain"] = InputEvidence{"supported", "sampled bare-terrain northern sector only; excludes trees, buildings and access"}
	}
	sun := astro.SunElevation(at, *site.Lat, *site.Lon)
	a.SunElevation = &sun
	a.Evidence["darkness"] = InputEvidence{"supported", "solar position at the requested instant using shared stars astronomy"}
	if a.Cloud == nil || maxH < 0 {
		return a
	}
	a.Verdict, a.Why = "go", "dark, low cloud, low northern terrain and stronger NOAA cell evidence; heuristic only"
	switch {
	case sun >= -10:
		a.Verdict, a.Why = "no", "daylight or bright twilight (sun at or above -10 degrees)"
	case *a.Cloud >= 75:
		a.Verdict, a.Why = "no", "cloud covers at least 75 percent of the site forecast"
	case maxH >= 10:
		a.Verdict, a.Why = "no", "sampled northern terrain reaches at least 10 degrees"
	case *a.Intensity < 10:
		a.Verdict, a.Why = "no", "weak NOAA cell evidence, not a claim that aurora is impossible"
	case sun >= -12 || *a.Cloud > 25 || maxH > 3 || *a.Intensity < 30:
		a.Verdict, a.Why = "marginal", "twilight, cloud, northern terrain or NOAA evidence is marginal"
	}
	return a
}
