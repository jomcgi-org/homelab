package connector_test

import (
	"encoding/json"
	"fmt"
	"strings"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/connector"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/fixture"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/stars"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/walks"
)

func TestCampingThroughSnapshotsPlanAndCheck(t *testing.T) {
	inside := `{"zones":["East Loch Lomond"],"parks":["Loch Lomond and The Trossachs"]}`
	outside := `{"zones":[],"parks":["Cairngorms"]}`
	for _, tc := range []struct{ date, membership, want string }{
		{"2027-04-01", inside, "byelaws"},
		{"2027-10-01", inside, "access_code"},
		{"2027-04-01", outside, "access_code"},
		{"2027-02-28", inside, "access_code"},
		{"2027-03-01", inside, "byelaws"},
		{"2027-09-30", inside, "byelaws"},
		{"2028-02-29", inside, "access_code"},
		{"2027-04-01", "null", "unknown"},
		{"2027-04-01", "{}", "unknown"},
		{"2027-04-01", "", "unknown"}, // legacy producer omitted field
	} {
		t.Run(tc.date+tc.membership, func(t *testing.T) {
			day, _ := time.Parse("2006-01-02", tc.date)
			now := day.Add(8 * time.Hour)
			field := ""
			if tc.membership != "" {
				field = `,"camping_membership":` + tc.membership
			}
			walkBody := fmt.Sprintf(`{"schema_version":2,"fetched_at":%q,"walks":[{"id":"shore","name":"Shore walk","url":"https://example.test/walk","lat":56.084,"lon":-4.548,"distance_km":3,"ascent_m":10,"duration_h":1,"forecast_fetched_at":%q,"hours":[[%d,10,0,5,0,0,0],[%d,10,0,5,0,0,0]]%s}]}`, now.Format(time.RFC3339), now.Format(time.RFC3339), day.Add(12*time.Hour).Unix(), day.Add(13*time.Hour).Unix(), field)
			starBody := fmt.Sprintf(`{"fetched_at":%q,"sites":[{"id":"shore-site","lat":56.084,"lon":-4.548,"lp_zone":"pristine","forecast_fetched_at":%q,"clear_dark_hours":1,"clear_twilight_hours":1,"best_hours":[{"time":%q,"cloud_area_fraction":0,"dark":true}]%s}]}`, now.Format(time.RFC3339), now.Format(time.RFC3339), day.Add(22*time.Hour).Format(time.RFC3339), field)
			wd, err := walks.Parse([]byte(walkBody), "walks-camping")
			if err != nil {
				t.Fatal(err)
			}
			sd, err := stars.Parse([]byte(starBody), []byte(`{"sites":[]}`), "sites-camping")
			if err != nil {
				t.Fatal(err)
			}
			svc := fixture.Service(true)
			svc.Now = func() time.Time { return now }
			svc.Walks, svc.Data = fixture.WalkSource{DS: wd}, fixture.Source{DS: sd}
			verify := func(n connector.CampingNote, location string) {
				t.Helper()
				if n.Date != tc.date || n.Status != tc.want || !strings.Contains(n.Location, location) {
					t.Fatalf("note = %+v", n)
				}
				if tc.want == "byelaws" {
					if n.Text != "Camping here needs a permit or a campsite (LLTNP byelaws, 1 Mar to 30 Sep)" || !strings.Contains(n.URL, "lochlomond-trossachs.org") {
						t.Fatalf("permit = %+v", n)
					}
				} else {
					for _, phrase := range []string{"small numbers", "two or three nights", "away from buildings and roads"} {
						if !strings.Contains(n.Text, phrase) {
							t.Fatalf("missing %s: %+v", phrase, n)
						}
					}
				}
				if tc.want == "unknown" && (!strings.Contains(n.Text, "unknown") || n.BoundaryURL == "") {
					t.Fatal(n.Text)
				}
				if tc.want == "access_code" && !strings.Contains(n.URL, "outdooraccess-scotland.scot") {
					t.Fatal(n.URL)
				}
			}
			verifySources := func(env *connector.Envelope) {
				t.Helper()
				b, _ := json.Marshal(env.Meta.Sources)
				for _, credit := range []string{"Data source LLTNPA. Contains OS data © Crown copyright and database right 2014.", "Copyright Scottish Government, contains Ordnance Survey data © Crown copyright and database right 2013."} {
					if !strings.Contains(string(b), credit) {
						t.Fatalf("sources = %s", b)
					}
				}
			}
			env, err := svc.Plan(connector.PlanParams{Lat: f(56.084), Lon: f(-4.548), From: s(tc.date), To: s(tc.date)})
			if err != nil {
				t.Fatal(err)
			}
			pd := env.Data.(connector.PlanResult).Days[0]
			if len(pd.Walks) != 1 || len(pd.Stargazing) != 1 {
				t.Fatalf("plan = %+v", pd)
			}
			verify(pd.Walks[0].Camping, "Walk start: Shore walk (shore)")
			verify(pd.Stargazing[0].Camping, "Stars site: shore-site")
			verifySources(env)
			env, err = svc.Check(connector.CheckParams{Items: []connector.CheckItem{{Activity: "walk", Date: tc.date, WalkID: "shore"}, {Activity: "stargaze", Date: tc.date, Lat: f(56.085), Lon: f(-4.549)}}})
			if err != nil {
				t.Fatal(err)
			}
			items := env.Data.(connector.CheckResult).Items
			if items[0].Walk == nil || items[1].Night == nil {
				t.Fatalf("check = %+v", items)
			}
			verify(items[0].Walk.Camping, "Walk start: Shore walk (shore)")
			verify(items[1].Night.Camping, "Stars site: shore-site")
			verifySources(env)
		})
	}
}
