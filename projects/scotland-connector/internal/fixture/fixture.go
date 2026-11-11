// Package fixture builds a small in-memory dataset and service for tests.
package fixture

import (
	"time"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/connector"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/stars"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/walks"
)

// Now is the fixture clock: mid-afternoon on 19 October 2026.
var Now = time.Date(2026, 10, 19, 15, 20, 0, 0, time.UTC)

// LightArgs is shared by both transport round trips against Sites/History.
// Those synthetic snapshots exercise scope and provenance, not ephemeris accuracy.
const LightArgs = `{"lat":57.3,"lon":-3.7,"from":"2026-10-19","to":"2026-10-21"}`

func LightParams() connector.LightParams {
	lat, lon, from, to := 57.3, -3.7, "2026-10-19", "2026-10-21"
	return connector.LightParams{Lat: &lat, Lon: &lon, From: &from, To: &to}
}

// Sites is a sites.json with three sites around Aviemore and one in Shetland.
const Sites = `{
 "count": 4, "total_sites": 4, "darkness": "astronomical", "fetched_at": "2026-10-19T14:05:00+00:00",
 "sites": [
  {"forecast_fetched_at":"2026-10-19T14:05:00Z","id":"scotland-0001","name":null,"lat":57.30,"lon":-3.70,"altitude_m":420,"lp_zone":"pristine",
   "clear_dark_hours":3,"clear_twilight_hours":4,
   "best_hours":[
     {"time":"2026-10-19T20:00:00+00:00","cloud_area_fraction":2.5,"dark":true},
     {"time":"2026-10-19T21:00:00+00:00","cloud_area_fraction":1.0,"dark":true},
     {"time":"2026-10-20T19:00:00+00:00","cloud_area_fraction":4.0,"dark":false},
     {"time":"2026-10-20T22:00:00+00:00","cloud_area_fraction":0.0,"dark":true}]},
  {"forecast_fetched_at":"2026-10-19T14:05:00Z","id":"scotland-0002","name":null,"lat":57.10,"lon":-3.90,"altitude_m":300,"lp_zone":"rural",
   "clear_dark_hours":1,"clear_twilight_hours":30,
   "best_hours":[{"time":"2026-10-19T23:00:00+00:00","cloud_area_fraction":8.0,"dark":true}]},
  {"forecast_fetched_at":"2026-10-19T14:05:00Z","id":"scotland-0003","name":null,"lat":57.25,"lon":-3.80,"altitude_m":350,"lp_zone":"excellent",
   "clear_dark_hours":0,"clear_twilight_hours":0,"best_hours":[]},
  {"forecast_fetched_at":"2026-10-19T14:05:00Z","id":"scotland-9999","name":null,"lat":60.30,"lon":-1.30,"altitude_m":80,"lp_zone":"pristine",
   "clear_dark_hours":40,"clear_twilight_hours":40,"best_hours":[]}
 ]}`

// History is the matching history-map.json.
const History = `{"sites":[
 {"id":"scotland-0001","name":null,"lat":57.30,"lon":-3.70,"clear":[10,10,10,10,10,10,10,10,10,30,10,10],"dark":[100,100,100,100,100,100,100,100,100,120,100,100]},
 {"id":"scotland-0002","name":null,"lat":57.10,"lon":-3.90,"clear":[0,0,0,0,0,0,0,0,0,0,0,0],"dark":[0,0,0,0,0,0,0,0,0,0,0,0]}
]}`

// Walks is a walks.json with two walks near Aviemore, both on 20 October:
// a 3 h walk with six good hours and a 9 h walk that falls short.
const Walks = `{"schema_version":1,"generated_at":"2026-10-19T14:30:00+00:00","fetched_at":"2026-10-19T14:20:00+00:00","walks":[
 {"forecast_fetched_at":"2026-10-19T14:20:00Z","id":"wh-meall-a-bhuachaille","name":"Meall a' Bhuachaille","url":"https://www.walkhighlands.co.uk/cairngorms/meall-a-bhuachaille.shtml",
  "lat":57.17,"lon":-3.67,"distance_km":7.5,"ascent_m":560,"duration_h":3,
  "hours":[[1792483200,6.5,0.0,14,30],[1792486800,7.0,0.1,15,40],[1792490400,7.5,0.0,16,20],[1792494000,8.0,0.0,18,25],[1792497600,8.5,0.2,20,35],[1792501200,8.0,0.0,18,50]]},
 {"forecast_fetched_at":"2026-10-19T14:20:00Z","id":"wh-cairn-gorm","name":"Cairn Gorm","url":"https://www.walkhighlands.co.uk/cairngorms/cairngorm.shtml",
  "lat":57.13,"lon":-3.67,"distance_km":19,"ascent_m":1100,"duration_h":9,
  "hours":[[1792483200,2.0,0.0,40,60],[1792486800,2.5,0.0,45,70]]}
]}`

// WalkSource serves a fixed walk dataset; nil means "not published yet".
type WalkSource struct{ DS *walks.Dataset }

// Current implements connector.WalksSource.
func (s WalkSource) Current() *walks.Dataset { return s.DS }

// WalkDataset parses the walk fixture.
func WalkDataset() *walks.Dataset {
	ds, err := walks.Parse([]byte(Walks), `"w1"`)
	if err != nil {
		panic(err)
	}
	return ds
}

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
	src, ws := Source{}, WalkSource{}
	if loaded {
		src.DS, ws.DS = Dataset(), WalkDataset()
	}
	return &connector.Service{Data: src, Walks: ws, MethodURL: "https://example.test/v1/method", Now: func() time.Time { return Now }}
}
