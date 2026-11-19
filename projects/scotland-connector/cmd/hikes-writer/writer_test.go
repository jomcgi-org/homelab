package main

import (
	"strings"
	"testing"
	"time"
)

func TestForecastReplacement(t *testing.T) {
	now := time.Date(2026, 6, 20, 0, 0, 0, 0, time.UTC)
	w := walk{Lat: 57, Lon: -4, ID: "retained"}
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
}
