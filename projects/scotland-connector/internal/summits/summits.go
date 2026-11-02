// Package summits reads optional UKV sampled fields. Missing evidence never
// establishes safe summit conditions. Walk-start samples cannot assess summits.
package summits

import (
	"encoding/json"
	"fmt"
	"math"
	"strings"
	"time"
)

const (
	MaxBytes   = 16 << 20
	License    = "https://creativecommons.org/licenses/by-sa/4.0/"
	Source     = "s3://met-office-atmospheric-model-data/uk-deterministic-2km/"
	Adaptation = "UKV hourly fields sampled at declared points; no elevation correction or summit inference."
)

type Hour struct {
	ValidAt  time.Time `json:"valid_at"`
	Wind     *float64  `json:"wind_kmh"`
	Gust     *float64  `json:"gust_kmh"`
	Cloud    *float64  `json:"cloud_base_m"`
	Freezing *float64  `json:"freezing_level_m_asl"`
}
type Point struct {
	ID             string   `json:"id"`
	WalkID         string   `json:"walk_id"`
	Kind           string   `json:"kind"`
	Lat            float64  `json:"lat"`
	Lon            float64  `json:"lon"`
	SampleLat      float64  `json:"sample_lat"`
	SampleLon      float64  `json:"sample_lon"`
	Sampling       string   `json:"sampling"`
	Elevation      *float64 `json:"elevation_m_asl"`
	Orography      *float64 `json:"orography_m_asl"`
	CloudReference string   `json:"cloud_base_reference"`
	Hours          []Hour   `json:"hours"`
}
type Snapshot struct {
	SchemaVersion int       `json:"schema_version"`
	GeneratedAt   time.Time `json:"generated_at"`
	RunAt         time.Time `json:"run_at"`
	Source        string    `json:"source"`
	LicenseURI    string    `json:"license_uri"`
	Adaptation    string    `json:"adaptation"`
	Fixture       bool      `json:"fixture"`
	Points        []Point   `json:"points"`
}

func number(v float64, lo, hi float64) bool {
	return !math.IsNaN(v) && !math.IsInf(v, 0) && v >= lo && v <= hi
}
func optional(v *float64, lo, hi float64) bool { return v == nil || number(*v, lo, hi) }
func utc(t time.Time, hourly bool) bool {
	_, offset := t.Zone()
	return !t.IsZero() && offset == 0 && (!hourly || t.Equal(t.Truncate(time.Hour)))
}

func Parse(body []byte) (*Snapshot, error) {
	if len(body) > MaxBytes {
		return nil, fmt.Errorf("summits: byte limit")
	}
	var d Snapshot
	if err := json.Unmarshal(body, &d); err != nil {
		return nil, err
	}
	if d.SchemaVersion != 1 || !utc(d.RunAt, true) || !utc(d.GeneratedAt, false) || d.GeneratedAt.Before(d.RunAt) || d.GeneratedAt.Sub(d.RunAt) > 12*time.Hour || d.Source != Source || d.LicenseURI != License || d.Adaptation != Adaptation || len(d.Points) == 0 || len(d.Points) > 8192 {
		return nil, fmt.Errorf("summits: invalid header")
	}
	seen := map[string]bool{}
	perWalk := map[string]int{}
	for _, p := range d.Points {
		if p.ID == "" || len(p.ID) > 200 || p.WalkID == "" || len(p.WalkID) > 200 || seen[p.ID] || (p.Kind != "walk_start" && p.Kind != "summit") || (p.Kind == "summit" && !d.Fixture) || p.Sampling != "nearest_grid_point" || !number(p.Lat, 54, 61) || !number(p.Lon, -9, 0) || !number(p.SampleLat, 54, 61) || !number(p.SampleLon, -9, 0) || math.Abs(p.Lat-p.SampleLat) > .05 || math.Abs(p.Lon-p.SampleLon) > .05 || !optional(p.Elevation, -500, 2000) || !optional(p.Orography, -500, 2000) || len(p.Hours) > 121 {
			return nil, fmt.Errorf("summits: invalid point")
		}
		if p.CloudReference != "mean_sea_level" && p.CloudReference != "above_ground" && p.CloudReference != "unknown" {
			return nil, fmt.Errorf("summits: cloud reference")
		}
		seen[p.ID] = true
		perWalk[p.WalkID]++
		if perWalk[p.WalkID] > 8 {
			return nil, fmt.Errorf("summits: per-walk point limit")
		}
		var last time.Time
		for _, h := range p.Hours {
			if !utc(h.ValidAt, true) || h.ValidAt.Before(d.RunAt) || h.ValidAt.Sub(d.RunAt) > 120*time.Hour || (!last.IsZero() && !h.ValidAt.After(last)) || !optional(h.Wind, 0, 500) || !optional(h.Gust, 0, 500) || !optional(h.Cloud, 0, 20000) || !optional(h.Freezing, -1000, 20000) {
				return nil, fmt.Errorf("summits: invalid hour")
			}
			last = h.ValidAt
		}
	}
	return &d, nil
}

type Support struct {
	Status string     `json:"status"`
	Note   string     `json:"note"`
	RunAt  *time.Time `json:"run_at,omitempty"`
	Points []Point    `json:"points,omitempty"`
}

// Assess returns hours in the checked slot only. AGL cloud base needs model
// orography; route ascent is never an elevation. Absence of a hazard does not
// establish safe conditions.
func (d *Snapshot) Assess(walkID string, lat, lon float64, start, end, now time.Time) (Support, string) {
	out := Support{Status: "unavailable", Note: "UKV sample or elevation unavailable; no summit safety assessment."}
	if d == nil {
		return out, ""
	}
	if d.Fixture {
		out.Note = "Synthetic fixture support; no real summit coverage."
	}
	if d.RunAt.After(now) || d.GeneratedAt.After(now) || now.Sub(d.RunAt) > 12*time.Hour || now.Sub(d.GeneratedAt) > 12*time.Hour {
		out.Status = "stale"
		out.Note = "UKV support stale or future-dated; no summit safety assessment."
		return out, ""
	}
	out.RunAt = &d.RunAt
	var hazards []string
	for _, p := range d.Points {
		if p.WalkID != walkID || (p.Kind == "walk_start" && (math.Abs(p.Lat-lat) > 1e-6 || math.Abs(p.Lon-lon) > 1e-6)) {
			continue
		}
		sample := p
		sample.Hours = nil
		for _, h := range p.Hours {
			if h.ValidAt.Before(start) || !h.ValidAt.Before(end) {
				continue
			}
			sample.Hours = append(sample.Hours, h)
			if p.Kind != "summit" || p.Elevation == nil {
				continue
			}
			if h.Cloud != nil {
				base := *h.Cloud
				comparable := p.CloudReference == "mean_sea_level"
				if p.CloudReference == "above_ground" && p.Orography != nil {
					base += *p.Orography
					comparable = true
				}
				if comparable && *p.Elevation >= base {
					hazards = append(hazards, fmt.Sprintf("UKV sample %s at %s: summit in cloud", p.ID, h.ValidAt.Format(time.RFC3339)))
				}
			}
			if h.Freezing != nil && *p.Elevation > *h.Freezing {
				hazards = append(hazards, fmt.Sprintf("UKV sample %s at %s: summit above freezing level", p.ID, h.ValidAt.Format(time.RFC3339)))
			}
		}
		if len(sample.Hours) > 0 {
			out.Points = append(out.Points, sample)
		}
	}
	if len(out.Points) > 0 {
		out.Status = "sampled"
		if !d.Fixture {
			out.Note = "UKV walk-start model samples only; summit conditions unavailable until DoBIH coverage. No elevation correction."
		}
	}
	return out, strings.Join(hazards, "; ")
}
