package main

import (
	"bytes"
	"crypto/sha256"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
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

func TestBootstrapSeedKeepsHubWalks(t *testing.T) {
	fetched := time.Date(2026, 11, 19, 20, 0, 0, 0, time.UTC)
	hub := `{"schema_version":2,"generated_at":"2026-11-19T20:20:38.530470+00:00","fetched_at":"2026-11-19T20:00:00+00:00","walks":[` +
		`{"id":"kept","name":"Old name","url":"https://www.walkhighlands.co.uk/kept.shtml","lat":57,"lon":-4,"distance_km":5,"ascent_m":100,"duration_h":2,"forecast_fetched_at":"2026-11-19T20:00:00+00:00","hours":[[1795636800,1,2,3,4,5,6]]},` +
		`{"id":"unscraped","name":"Gone","url":"https://www.walkhighlands.co.uk/gone.shtml","lat":56,"lon":-5,"distance_km":3,"ascent_m":50,"duration_h":1,"forecast_fetched_at":"2026-11-19T20:00:00+00:00","hours":[]}]}`
	v1 := `{"schema_version":1,"walks":[{"id":"x"}]}`
	sha12 := func(body string) string { return fmt.Sprintf("%x", sha256.Sum256([]byte(body)))[:12] }
	// The site's layout: latest.json names a copy of the hub object by the
	// first 12 hex digits of its SHA-256.
	files := map[string]string{}
	publish := func(dir, walksPath, body string) {
		if walksPath == "" {
			walksPath = "/data/hikes/v/walks-" + sha12(body) + ".json"
		}
		files[dir+"/latest.json"] = fmt.Sprintf(`{"asOf":"2026-11-19T20:20:40.000Z","files":{"walks":%q},"sources":{"walks":{"bucket":"stars","key":"walks.json","lastModified":"2026-11-19T20:20:40.000Z"}}}`, walksPath)
		if strings.HasPrefix(walksPath, "/") {
			files[walksPath] = body
		}
	}
	publish("/data/hikes", "", hub)
	publish("/v1", "", v1)
	publish("/mismatch", "/data/hikes/v/walks-000000000000.json", hub)
	publish("/elsewhere", "https://example.com/data/hikes/v/walks-"+sha12(hub)+".json", hub)
	publish("/unnamed", "/data/hikes/v/walks.json", hub)
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, ok := files[r.URL.Path]
		if !ok || r.Method != http.MethodGet {
			http.NotFound(w, r)
			return
		}
		io.WriteString(w, body)
	}))
	defer server.Close()
	hc := server.Client()
	for _, c := range []struct{ name, address, want string }{
		{"missing manifest", server.URL + "/missing/latest.json", "HTTP 404"},
		{"plain HTTP fixture URL", strings.Replace(server.URL, "https://", "http://", 1) + "/data/hikes/latest.json", "need an https URL"},
		{"walks on another host", server.URL + "/elsewhere/latest.json", "unexpected walks path"},
		{"walks not content-named", server.URL + "/unnamed/latest.json", "unexpected walks path"},
		{"digest mismatch", server.URL + "/mismatch/latest.json", "digest mismatch"},
		{"schema version 1", server.URL + "/v1/latest.json", "need schema version 2"},
	} {
		if _, err := seed(hc, c.address); err == nil || !strings.Contains(err.Error(), c.want) {
			t.Errorf("%s: got error %v, want %q", c.name, err, c.want)
		}
	}
	data, err := seed(hc, server.URL+"/data/hikes/latest.json")
	if err != nil {
		t.Fatal(err)
	}
	merge(&data, []walk{
		{ID: "kept", Name: "New name", URL: "https://www.walkhighlands.co.uk/kept.shtml", Lat: 57, Lon: -4, Distance: 6, Ascent: 120, Duration: 2.5},
		{ID: "new", Name: "New route", URL: "https://www.walkhighlands.co.uk/new.shtml", Lat: 58, Lon: -3, Distance: 4, Ascent: 80, Duration: 1.5},
	})
	byID := map[string]walk{}
	for _, w := range data.Walks {
		byID[w.ID] = w
	}
	kept := byID["kept"]
	if len(byID) != 3 || kept.Name != "New name" || kept.Distance != 6 || !kept.Fetched.Equal(fetched) || len(kept.Hours) != 1 || *kept.Hours[0][1] != 1 {
		t.Fatalf("catalogue lost the seeded forecast: %+v", data.Walks)
	}
	if gone := byID["unscraped"]; gone.Name != "Gone" || !gone.Fetched.Equal(fetched) {
		t.Fatalf("catalogue dropped a seeded walk: %+v", gone)
	}
	if added := byID["new"]; added.Fetched != nil || len(added.Hours) != 0 {
		t.Fatalf("new walk has a forecast: %+v", added)
	}
}
