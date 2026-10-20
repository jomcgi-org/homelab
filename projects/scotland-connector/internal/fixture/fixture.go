// Package fixture builds a small in-memory dataset and service for tests.
package fixture

import (
	"time"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/connector"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/stars"
)

// Now is the fixture clock: mid-afternoon on 19 October 2026.
var Now = time.Date(2026, 10, 19, 15, 20, 0, 0, time.UTC)

// Sites is a sites.json with three sites around Aviemore and one in Shetland.
const Sites = `{
 "count": 4, "total_sites": 4, "darkness": "astronomical", "fetched_at": "2026-10-19T14:05:00+00:00",
 "sites": [
  {"id":"scotland-0001","name":null,"lat":57.30,"lon":-3.70,"altitude_m":420,"lp_zone":"pristine",
   "clear_dark_hours":3,"clear_twilight_hours":4,
   "best_hours":[
     {"time":"2026-10-19T20:00:00+00:00","cloud_area_fraction":2.5,"dark":true},
     {"time":"2026-10-19T21:00:00+00:00","cloud_area_fraction":1.0,"dark":true},
     {"time":"2026-10-20T19:00:00+00:00","cloud_area_fraction":4.0,"dark":false},
     {"time":"2026-10-20T22:00:00+00:00","cloud_area_fraction":0.0,"dark":true}]},
  {"id":"scotland-0002","name":null,"lat":57.10,"lon":-3.90,"altitude_m":300,"lp_zone":"rural",
   "clear_dark_hours":1,"clear_twilight_hours":30,
   "best_hours":[{"time":"2026-10-19T23:00:00+00:00","cloud_area_fraction":8.0,"dark":true}]},
  {"id":"scotland-0003","name":null,"lat":57.25,"lon":-3.80,"altitude_m":350,"lp_zone":"excellent",
   "clear_dark_hours":0,"clear_twilight_hours":0,"best_hours":[]},
  {"id":"scotland-9999","name":null,"lat":60.30,"lon":-1.30,"altitude_m":80,"lp_zone":"pristine",
   "clear_dark_hours":40,"clear_twilight_hours":40,"best_hours":[]}
 ]}`

// History is the matching history-map.json.
const History = `{"sites":[
 {"id":"scotland-0001","name":null,"lat":57.30,"lon":-3.70,"clear":[10,10,10,10,10,10,10,10,10,30,10,10],"dark":[100,100,100,100,100,100,100,100,100,120,100,100]},
 {"id":"scotland-0002","name":null,"lat":57.10,"lon":-3.90,"clear":[0,0,0,0,0,0,0,0,0,0,0,0],"dark":[0,0,0,0,0,0,0,0,0,0,0,0]}
]}`

// Source serves a fixed dataset; a nil dataset means "not loaded yet".
type Source struct{ DS *stars.Dataset }

// Current implements connector.DatasetSource.
func (s Source) Current() *stars.Dataset { return s.DS }

// Dataset parses the fixture.
func Dataset() *stars.Dataset {
	ds, err := stars.Parse([]byte(Sites), []byte(History), `"s1"|"h1"`)
	if err != nil {
		panic(err)
	}
	return ds
}

// Service returns a service over the fixture at the fixture clock. Pass
// loaded=false for one whose dataset has not arrived.
func Service(loaded bool) *connector.Service {
	src := Source{}
	if loaded {
		src.DS = Dataset()
	}
	return &connector.Service{Data: src, MethodURL: "https://example.test/v1/method", Now: func() time.Time { return Now }}
}
