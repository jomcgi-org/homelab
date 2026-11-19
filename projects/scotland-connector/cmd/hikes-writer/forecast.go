package main

import (
	"encoding/json"
	"fmt"
	"io"
	"math"
	"time"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/astro"
)

type walk struct {
	ID       string        `json:"id"`
	Name     string        `json:"name"`
	URL      string        `json:"url"`
	Lat      float64       `json:"lat"`
	Lon      float64       `json:"lon"`
	Distance float64       `json:"distance_km"`
	Ascent   int           `json:"ascent_m"`
	Duration float64       `json:"duration_h"`
	Fetched  *time.Time    `json:"forecast_fetched_at"`
	Hours    [][7]*float64 `json:"hours"`
}

type step struct {
	Time time.Time
	Data struct {
		Instant struct {
			Details struct {
				Temp  float64  `json:"air_temperature"`
				Wind  float64  `json:"wind_speed"`
				Cloud *float64 `json:"cloud_area_fraction"`
				Low   *float64 `json:"cloud_area_fraction_low"`
				Fog   *float64 `json:"fog_area_fraction"`
			}
		}
		Next *struct {
			Details struct {
				Rain float64 `json:"precipitation_amount"`
			}
		} `json:"next_1_hours"`
	}
}

func rounded(p *float64) *float64 {
	if p == nil {
		return nil
	}
	v := math.RoundToEven(*p)
	return &v
}

func forecast(r io.Reader, w *walk, now time.Time) error {
	var data struct{ Properties struct{ Timeseries []step } }
	if err := json.NewDecoder(r).Decode(&data); err != nil {
		return err
	}
	if len(data.Properties.Timeseries) == 0 {
		return fmt.Errorf("missing timeseries")
	}
	hours := make([][7]*float64, 0, 48)
	for _, s := range data.Properties.Timeseries {
		if s.Time.IsZero() {
			return fmt.Errorf("missing timestamp")
		}
		if s.Data.Next == nil || s.Time.Before(now) || s.Time.After(now.AddDate(0, 0, 7)) {
			continue
		}
		rise, set, ok := astro.SunTimes(s.Time, w.Lat, w.Lon)
		if !ok || s.Time.Before(rise) || s.Time.Add(time.Hour).After(set) {
			continue
		}
		d := s.Data.Instant.Details
		ts := float64(s.Time.Unix())
		temp := math.RoundToEven(d.Temp*10) / 10
		wind := math.RoundToEven(d.Wind*360) / 100
		rain := math.RoundToEven(math.Max(0, s.Data.Next.Details.Rain)*10) / 10
		cloud := 50.
		if d.Cloud != nil {
			cloud = math.RoundToEven(*d.Cloud)
		}
		hours = append(hours, [7]*float64{&ts, &temp, &rain, &wind, &cloud, rounded(d.Fog), rounded(d.Low)})
	}
	w.Hours = hours
	w.Fetched = &now
	return nil
}
