// Package stars holds the in-memory dark-sky dataset the connector answers
// from. It is built from the two snapshots the monolith's stars-refresh job
// materializes to R2 (projects/monolith/stars/materialize.py):
//
//   - sites.json: every grid site with its upcoming clear hours (a capped list)
//     and full-horizon clear-dark and clear-twilight counts.
//   - history-map.json: every site's 12-month CERRA climatology (clear-dark
//     and dark hours per calendar month).
//
// A Dataset is immutable once built, so readers share it without locks and a
// refresh swaps in a new one.
package stars

import (
	"encoding/json"
	"fmt"
	"math"
	"sort"
	"time"
)

// Hour is one upcoming clear hour at a site. Every hour in the snapshot is
// already clear (cloud < 10%) and at least twilight-dark (sun < -10 deg);
// Dark marks the stricter nautical darkness (sun < -12 deg).
type Hour struct {
	Unix int64
	// Night is NightDay of the hour, precomputed at parse time so queries
	// bucket by integer compare rather than time zone conversion.
	Night int32
	Cloud float32
	Dark  bool
}

// Site is one dark-sky grid point.
type Site struct {
	ID        string
	Lat, Lon  float64
	AltitudeM int
	LPZone    string
	// ClearDark and ClearTwilight are the snapshot's counts over the whole
	// forecast horizon. Hours is capped by the materializer, so when
	// ClearTwilight exceeds len(Hours) the list is truncated (#7246).
	ClearDark     int
	ClearTwilight int
	Hours         []Hour
	// Climatology, index 0 = January. HasClimate is false for sites absent
	// from history-map.json.
	ClimateClear [12]int
	ClimateDark  [12]int
	HasClimate   bool
	cosLat       float64
}

// Truncated reports whether the materializer capped this site's hour list.
func (s *Site) Truncated() bool { return s.ClearTwilight > len(s.Hours) }

// Dataset is one consistent pair of snapshots plus its spatial index.
type Dataset struct {
	Sites []Site
	// FetchedAt is the newest MET Norway fetch time across the snapshot.
	FetchedAt time.Time
	// Darkness is the snapshot-wide mode: astronomical, twilight or none.
	Darkness string
	// Version identifies the snapshot pair (both R2 ETags); responses fold it
	// into their ETag.
	Version string
	index   grid
}

type sitesDoc struct {
	FetchedAt *string `json:"fetched_at"`
	Darkness  string  `json:"darkness"`
	Sites     []struct {
		ID            string  `json:"id"`
		Lat           float64 `json:"lat"`
		Lon           float64 `json:"lon"`
		AltitudeM     float64 `json:"altitude_m"`
		LPZone        string  `json:"lp_zone"`
		ClearDark     int     `json:"clear_dark_hours"`
		ClearTwilight int     `json:"clear_twilight_hours"`
		BestHours     []struct {
			Time  string  `json:"time"`
			Cloud float64 `json:"cloud_area_fraction"`
			Dark  bool    `json:"dark"`
		} `json:"best_hours"`
	} `json:"sites"`
}

type historyDoc struct {
	Sites []struct {
		ID    string `json:"id"`
		Clear []int  `json:"clear"`
		Dark  []int  `json:"dark"`
	} `json:"sites"`
}

// Parse builds a Dataset from the raw sites.json and history-map.json bodies.
// It rejects structurally broken input rather than serving a partial answer.
func Parse(sitesJSON, historyJSON []byte, version string) (*Dataset, error) {
	var sd sitesDoc
	if err := json.Unmarshal(sitesJSON, &sd); err != nil {
		return nil, fmt.Errorf("stars: decode sites.json: %w", err)
	}
	var hd historyDoc
	if err := json.Unmarshal(historyJSON, &hd); err != nil {
		return nil, fmt.Errorf("stars: decode history-map.json: %w", err)
	}

	climate := make(map[string]int, len(hd.Sites))
	for i, s := range hd.Sites {
		if len(s.Clear) != 12 || len(s.Dark) != 12 {
			return nil, fmt.Errorf("stars: history site %q has %d/%d months, want 12", s.ID, len(s.Clear), len(s.Dark))
		}
		climate[s.ID] = i
	}

	ds := &Dataset{Darkness: sd.Darkness, Version: version, Sites: make([]Site, 0, len(sd.Sites))}
	if sd.FetchedAt != nil && *sd.FetchedAt != "" {
		t, err := time.Parse(time.RFC3339, *sd.FetchedAt)
		if err != nil {
			return nil, fmt.Errorf("stars: fetched_at %q: %w", *sd.FetchedAt, err)
		}
		ds.FetchedAt = t.UTC()
	}
	seen := make(map[string]struct{}, len(sd.Sites))
	for _, raw := range sd.Sites {
		if raw.ID == "" {
			return nil, fmt.Errorf("stars: site with empty id")
		}
		if _, dup := seen[raw.ID]; dup {
			return nil, fmt.Errorf("stars: duplicate site id %q", raw.ID)
		}
		seen[raw.ID] = struct{}{}
		if raw.Lat < -90 || raw.Lat > 90 || raw.Lon < -180 || raw.Lon > 180 {
			return nil, fmt.Errorf("stars: site %q has invalid coordinates", raw.ID)
		}
		site := Site{
			ID:            raw.ID,
			Lat:           raw.Lat,
			Lon:           raw.Lon,
			AltitudeM:     int(math.Round(raw.AltitudeM)),
			LPZone:        raw.LPZone,
			ClearDark:     raw.ClearDark,
			ClearTwilight: raw.ClearTwilight,
			Hours:         make([]Hour, 0, len(raw.BestHours)),
			cosLat:        math.Cos(raw.Lat * math.Pi / 180),
		}
		for _, h := range raw.BestHours {
			t, err := time.Parse(time.RFC3339, h.Time)
			if err != nil {
				return nil, fmt.Errorf("stars: site %q hour %q: %w", raw.ID, h.Time, err)
			}
			site.Hours = append(site.Hours, Hour{Unix: t.Unix(), Night: NightDay(t), Cloud: float32(h.Cloud), Dark: h.Dark})
		}
		sort.Slice(site.Hours, func(i, j int) bool { return site.Hours[i].Unix < site.Hours[j].Unix })
		if i, ok := climate[raw.ID]; ok {
			copy(site.ClimateClear[:], hd.Sites[i].Clear)
			copy(site.ClimateDark[:], hd.Sites[i].Dark)
			site.HasClimate = true
		}
		ds.Sites = append(ds.Sites, site)
	}
	ds.index = newGrid(ds.Sites)
	return ds, nil
}
