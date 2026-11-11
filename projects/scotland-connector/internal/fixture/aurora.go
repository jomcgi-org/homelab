package fixture

import (
	"encoding/json"
	"fmt"
	"strings"
	"time"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/aurora"
)

// NOAAObserved is a reduced public payload read on 2026-10-25. The single
// Scottish cell and provider fields are verbatim; it is not a favorable fixture.
const NOAAObserved = `{"Observation Time":"2026-10-25T16:47:00Z","Forecast Time":"2026-10-25T18:05:00Z","Data Format":"[Longitude, Latitude, Aurora]","coordinates":[[356,58,0]]}`

// AuroraNOAA and AuroraMET are synthetic scenario fixtures using the provider
// wire formats. Neither they nor the flat raster establish Scottish coverage.
// MET can update mid-hour while retaining the first row at the hour boundary.
const (
	AuroraNOAA = `{"Observation Time":"2026-12-01T22:00:00Z","Forecast Time":"2026-12-01T23:00:00Z","Data Format":"[Longitude, Latitude, Aurora]","coordinates":[[356,58,50]]}`
	AuroraMET  = `{"type":"Feature","geometry":{"type":"Point","coordinates":[-4,58,100]},"properties":{"meta":{"updated_at":"2026-12-01T22:17:20Z","units":{"cloud_area_fraction":"%"}},"timeseries":[{"time":"2026-12-01T22:00:00Z","data":{"instant":{"details":{"cloud_area_fraction":5}}}},{"time":"2026-12-01T23:00:00Z","data":{"instant":{"details":{"cloud_area_fraction":10}}}},{"time":"2026-12-02T00:00:00Z","data":{"instant":{"details":{"cloud_area_fraction":5}}}}]}}`
)

var AuroraNow = time.Date(2026, 12, 1, 22, 50, 0, 0, time.UTC)

// AuroraInputs exercises local ASCII parsing and actual horizon computation.
func AuroraInputs() (aurora.Inputs, error) {
	var raster strings.Builder
	fmt.Fprintln(&raster, "ncols 400\nnrows 400\nxllcorner -4.4\nyllcorner 57.8\ncellsize 0.002\nNODATA_value -9999")
	raster.WriteString(strings.Repeat("100 ", 160000))
	g, err := aurora.ParseGrid([]byte(raster.String()))
	if err != nil {
		return aurora.Inputs{}, err
	}
	lat, lon := 58.0, -4.0
	return aurora.Inputs{CollectedAt: AuroraNow, NOAACollectedAt: AuroraNow, NOAA: json.RawMessage(AuroraNOAA), Weather: []aurora.WeatherInput{{ID: "synthetic-flat", Lat: &lat, Lon: &lon, CollectedAt: AuroraNow, MET: json.RawMessage(AuroraMET), Terrain: g.Profile(lat, lon)}}}, nil
}
