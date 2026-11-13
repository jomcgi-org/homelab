package fixture

import (
	_ "embed"
	"time"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/connector"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/roadworks"
)

// RoadworksJSON is produced by Python from the attributed 2026-11-13 SRWR
// sample. Producer tests compare JSON output before Go consumes this contract.
//
//go:embed testdata/roadworks.json
var RoadworksJSON []byte

type RoadworksSource struct{ DS *roadworks.Snapshot }

func (s *RoadworksSource) Current() *roadworks.Snapshot { return s.DS }

// WalkRoadworks uses the real contract with explicitly synthetic coordinates
// and clocks aligned to the existing walk/weather fixtures for transport tests.
func WalkRoadworks() *RoadworksSource {
	s, err := roadworks.Parse(RoadworksJSON)
	if err != nil {
		panic(err)
	}
	s.SourceModifiedAt = Now.Add(-time.Hour)
	s.CollectedAt = Now.Add(-time.Minute)
	for i := range s.Activities {
		a := &s.Activities[i]
		a.Lat = 57.15
		a.Lon = -3.67
		a.Start = Now.Add(-time.Hour)
		a.End = Now.AddDate(0, 0, 4)
		a.UpdatedAt = s.SourceModifiedAt
	}
	return &RoadworksSource{DS: s}
}

// RoadworksService uses a six-hour checked route to exercise similar-duration
// alternatives. The weather remains the existing fixture's two good hours.
func RoadworksService() *connector.Service {
	s := Service(true)
	wk := WalkDataset()
	wk.ByID("wh-cairn-gorm").DurationH = 6
	s.Walks = WalkSource{DS: wk}
	s.RoadworksData = WalkRoadworks()
	return s
}
