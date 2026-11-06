package connector

import (
	"time"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/camping"
)

// Primary byelaws, paragraph 2: inclusive 1 March to 30 September.
// https://www.lochlomond-trossachs.org/wp-content/uploads/2017/02/Camping-Management-Byelaws-2017.pdf
const campingSeason = "03-01/09-30"

const (
	permitURL     = "https://www.lochlomond-trossachs.org/things-to-do/camping/get-a-permit/"
	accessCodeURL = "https://www.outdooraccess-scotland.scot/practical-guide-all/camping"
)

// CampingNote applies only to the named point on Date, never an entire route
// or the caller's nearby coordinate. Membership is nil on legacy snapshots.
type CampingNote struct {
	Location    string              `json:"location"`
	Date        string              `json:"date"`
	Status      string              `json:"status"`
	Text        string              `json:"text"`
	URL         string              `json:"url"`
	BoundaryURL string              `json:"boundary_url,omitempty"`
	Membership  *camping.Membership `json:"membership"`
}

func campingNote(location string, day int32, m *camping.Membership) CampingNote {
	date := dayName(day)
	n := CampingNote{
		Location: location, Date: date, Status: "access_code", Membership: m,
		Text: "Access Code guidance: camp in small numbers for two or three nights, away from buildings and roads; leave no trace. Check local restrictions.", URL: accessCodeURL,
	}
	if !m.Known() {
		n.Status = "unknown"
		n.Text = "Boundary membership unknown. Check the LLTNP map and local rules. Access Code: small numbers, two or three nights, away from buildings and roads."
		n.BoundaryURL = permitURL
		return n
	}
	md := time.Unix(int64(day)*86400, 0).UTC().Format("01-02")
	if len(m.Zones) > 0 && md >= campingSeason[:5] && md <= campingSeason[6:] {
		n.Status = "byelaws"
		n.Text = "Camping here needs a permit or a campsite (LLTNP byelaws, 1 Mar to 30 Sep)"
		n.URL = permitURL
	}
	return n
}

var sourceCampingZones = Source{
	Name:        "LLTNP Camping Management Byelaws zones 2017",
	Attribution: "Data source LLTNPA. Contains OS data © Crown copyright and database right 2014.",
	License:     "Open Government Licence v3.0",
	URL:         "https://spatialdata.gov.scot/geonetwork/srv/eng/catalog.search#/metadata/8902e7ef-4a95-4ef0-bf2a-dc6870960e15",
	Use:         "camping-management zone membership at walk starts and stars sites",
}

var sourceNationalParks = Source{
	Name:        "Scottish Government National Park boundaries (2013 shapefile exports)",
	Attribution: "Copyright Scottish Government, contains Ordnance Survey data © Crown copyright and database right 2013.",
	License:     "Open Government Licence v3.0",
	URL:         "https://maps.gov.scot/ATOM/",
	Use:         "National Park membership at walk starts and stars sites; not camping-management zone membership",
}
