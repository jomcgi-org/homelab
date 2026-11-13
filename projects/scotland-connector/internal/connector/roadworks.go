package connector

import (
	"time"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/roadworks"
)

var sourceRoadworks = Source{Name: "Scottish Road Works Register", Attribution: "Scottish Road Works Commissioner", License: "OGL v3", URL: "https://roadworks.scot/opendata", Use: "planned A/trunk-road closures and explicit convoys within 5 km of walk starts; expected windows, no route graph"}

func (s *Service) roadworks() *roadworks.Snapshot {
	if s.RoadworksData == nil {
		return nil
	}
	return s.RoadworksData.Current()
}

func attachRoadworks(d *roadworks.Snapshot, options []WalkOption, date string, now time.Time) {
	for i := range options {
		o := &options[i]
		o.RoadNote = d.Match(o.StartLat, o.StartLon, date, now)
	}
}

func roadworksNote(enabled bool, d *roadworks.Snapshot, now time.Time) string {
	if !enabled {
		return ""
	}
	if !d.Fresh(now) {
		return "Planned roadworks data unavailable or stale; no note does not mean roads are open. Check Traffic Scotland on the day."
	}
	return "Planned roadworks may lag reality; no matching note does not mean roads are open."
}
