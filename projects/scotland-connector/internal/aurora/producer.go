package aurora

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"time"
)

const NOAAURL = "https://services.swpc.noaa.gov/json/ovation_aurora_latest.json"
const METURL = "https://api.met.no/weatherapi/locationforecast/2.0/complete"

// WeatherInput carries the unfiltered provider body. The existing stars
// sites.json hours are intentionally not an input: daylight and capped rows
// have already been lost there. This is an additive versioned producer contract.
type WeatherInput struct {
	ID          string          `json:"id"`
	Lat         *float64        `json:"lat"`
	Lon         *float64        `json:"lon"`
	CollectedAt time.Time       `json:"collected_at"`
	MET         json.RawMessage `json:"met"`
	Terrain     *Terrain        `json:"terrain"`
}

type Inputs struct {
	CollectedAt     time.Time       `json:"collected_at"`
	NOAACollectedAt time.Time       `json:"noaa_collected_at"`
	NOAA            json.RawMessage `json:"noaa"`
	Weather         []WeatherInput  `json:"weather"`
}

// Materialize consumes provider bytes, not hand-authored derived scores.
// Missing cloud stays null. The 96-row cap is explicit, never coverage by omission.
func Materialize(in Inputs) ([]byte, error) {
	if in.CollectedAt.IsZero() || len(in.NOAA) > MaxBytes || len(in.NOAA) == 0 || len(in.Weather) == 0 || len(in.Weather) > MaxSites {
		return nil, fmt.Errorf("aurora: invalid producer inputs")
	}
	var n struct {
		Observation time.Time `json:"Observation Time"`
		Forecast    time.Time `json:"Forecast Time"`
		Format      string    `json:"Data Format"`
		Coordinates []Cell    `json:"coordinates"`
	}
	if err := json.Unmarshal(in.NOAA, &n); err != nil {
		return nil, err
	}
	if n.Format != "[Longitude, Latitude, Aurora]" {
		return nil, fmt.Errorf("aurora: unsupported NOAA coordinate format")
	}
	s := Snapshot{Schema: 1, CreatedAt: in.CollectedAt, NOAA: NOAA{n.Observation, n.Forecast, in.NOAACollectedAt, n.Coordinates}}
	for _, p := range in.Weather {
		if len(p.MET) == 0 || len(p.MET) > 2<<20 {
			return nil, fmt.Errorf("aurora: MET payload outside limit")
		}
		var met struct {
			Geometry struct {
				Type        string     `json:"type"`
				Coordinates []*float64 `json:"coordinates"`
			} `json:"geometry"`
			Properties struct {
				Meta struct {
					UpdatedAt time.Time         `json:"updated_at"`
					Units     map[string]string `json:"units"`
				} `json:"meta"`
				Timeseries []struct {
					Time time.Time `json:"time"`
					Data struct {
						Instant struct {
							Details struct {
								Cloud *float64 `json:"cloud_area_fraction"`
							} `json:"details"`
						} `json:"instant"`
					} `json:"data"`
				} `json:"timeseries"`
			} `json:"properties"`
		}
		if err := json.Unmarshal(p.MET, &met); err != nil {
			return nil, err
		}
		g := met.Geometry
		if !point(p.Lat, p.Lon) || g.Type != "Point" || len(g.Coordinates) < 2 || g.Coordinates[0] == nil || g.Coordinates[1] == nil || *g.Coordinates[0] != *p.Lon || *g.Coordinates[1] != *p.Lat || met.Properties.Meta.Units["cloud_area_fraction"] != "%" || len(met.Properties.Timeseries) > 256 {
			return nil, fmt.Errorf("aurora: MET identity, units or row limit invalid")
		}
		w := Weather{IssuedAt: met.Properties.Meta.UpdatedAt, CollectedAt: p.CollectedAt, Hours: []Hour{}}
		var previous time.Time
		for i, row := range met.Properties.Timeseries {
			if row.Time.IsZero() || (i > 0 && !row.Time.After(previous)) {
				return nil, fmt.Errorf("aurora: unordered MET timeseries")
			}
			previous = row.Time
			cloud := row.Data.Instant.Details.Cloud
			if cloud != nil && (!finite(*cloud) || *cloud < 0 || *cloud > 100) {
				return nil, fmt.Errorf("aurora: invalid MET cloud")
			}
			if i < MaxHours {
				w.Hours = append(w.Hours, Hour{row.Time, cloud})
			}
		}
		s.Sites = append(s.Sites, Site{p.ID, p.Lat, p.Lon, w, p.Terrain})
	}
	if err := s.Validate(); err != nil {
		return nil, err
	}
	body, err := json.Marshal(s)
	if len(body) > MaxBytes {
		return nil, fmt.Errorf("aurora: snapshot too large")
	}
	return body, err
}

// Collect is for an explicitly invoked producer, never a caller request.
// URLs are fixed, redirects forbidden, site count bounded. Tests supply a
// RoundTripper and clock, so enabled-path collection needs no external I/O.
// Scheduling and publication are intentionally not installed in this delivery.
func Collect(ctx context.Context, client *http.Client, sites []WeatherInput, now func() time.Time) (Inputs, error) {
	var in Inputs
	if len(sites) == 0 || len(sites) > MaxSites {
		return in, fmt.Errorf("aurora: invalid site count")
	}
	for _, s := range sites {
		if !point(s.Lat, s.Lon) || s.ID == "" {
			return in, fmt.Errorf("aurora: invalid site")
		}
	}
	c := *client
	c.Timeout = 15 * time.Second
	c.CheckRedirect = func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }
	fetch := func(raw string, limit int64) ([]byte, error) {
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, raw, nil)
		if err != nil {
			return nil, err
		}
		req.Header.Set("User-Agent", "jomcgi-homelab-stars-aurora/1.0 https://jomcgi.dev")
		resp, err := c.Do(req)
		if err != nil {
			return nil, err
		}
		defer resp.Body.Close()
		if resp.StatusCode != http.StatusOK {
			return nil, fmt.Errorf("aurora: upstream status %d", resp.StatusCode)
		}
		b, err := io.ReadAll(io.LimitReader(resp.Body, limit+1))
		if int64(len(b)) > limit {
			return nil, fmt.Errorf("aurora: upstream too large")
		}
		return b, err
	}
	var err error
	in.NOAA, err = fetch(NOAAURL, MaxBytes)
	if err != nil {
		return in, err
	}
	// Preserve the actual NOAA completion time even when later MET requests take time.
	noaaCollected := now().UTC()
	for i, p := range sites {
		if i > 0 {
			select {
			case <-ctx.Done():
				return in, ctx.Err()
			case <-time.After(time.Second):
			}
		}
		q := url.Values{"lat": {fmt.Sprint(*p.Lat)}, "lon": {fmt.Sprint(*p.Lon)}}
		p.MET, err = fetch(METURL+"?"+q.Encode(), 2<<20)
		if err != nil {
			return in, err
		}
		p.CollectedAt = now().UTC()
		in.Weather = append(in.Weather, p)
	}
	in.NOAACollectedAt = noaaCollected
	in.CollectedAt = now().UTC()
	return in, nil
}
