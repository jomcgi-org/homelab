package main

import (
	"bytes"
	"strings"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/walks"
)

func TestForecastReplacement(t *testing.T) {
	now := time.Date(2026, 6, 20, 0, 0, 0, 0, time.UTC)
	w := walk{Lat: 57, Lon: -4, ID: "retained", Name: "Route", URL: "https://www.walkhighlands.co.uk/route.shtml", Duration: 2, Distance: 5, Ascent: 150}
	response := `{"properties":{"timeseries":[{"time":"2026-06-20T12:00:00Z","data":{"instant":{"details":{"wind_speed":4.1}},"next_1_hours":{"details":{}}}},{"time":"2026-06-20T00:00:00Z","data":{"next_1_hours":{"details":{}}}}]}}`
	if err := forecast(strings.NewReader(response), &w, now); err != nil {
		t.Fatal(err)
	}
	if len(w.Hours) != 1 || *w.Hours[0][3] != 14.76 || w.Hours[0][5] != nil {
		t.Fatalf("unexpected hours: %v", w.Hours)
	}
	if err := forecast(strings.NewReader("{}"), &w, now.Add(time.Hour)); err == nil || !w.Fetched.Equal(now) || len(w.Hours) != 1 || w.ID != "retained" {
		t.Fatal("bad response replaced last-good data")
	}
	var payload bytes.Buffer
	if err := encode(&payload, snapshot{Fetched: &now, Walks: []walk{w}}, now); err != nil {
		t.Fatal(err)
	}
	ds, err := walks.Parse(payload.Bytes(), "generation")
	if err != nil {
		t.Fatal(err)
	}
	got := ds.Walks[0]
	if got.ID != w.ID || got.Name != w.Name || got.URL != w.URL || got.DurationH != w.Duration || got.AscentM != w.Ascent || got.LengthKm != w.Distance || !got.ForecastFetchedAt.Equal(now) || len(got.Hours) != 1 || got.Hours[0].Unix != now.Add(12*time.Hour).Unix() || got.Hours[0].WindKmh != 14.76 {
		t.Fatalf("connector decoded %+v", got)
	}
}
