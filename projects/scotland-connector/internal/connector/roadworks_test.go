package connector_test

import (
	"reflect"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/connector"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/fixture"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/roadworks"
)

func TestRoadworksOnlyAnnotatesEachWalkStartAndDate(t *testing.T) {
	svc := fixture.RoadworksService()
	src := fixture.WalkRoadworks()
	// Opposite sides of the two start points. Neither the caller point nor the
	// checked walk's point is a substitute for an alternative's own point.
	a, b := src.DS.Activities[0], src.DS.Activities[1]
	a.ID = "a"
	a.Lat = 57.09
	a.Lon = -3.67
	b.ID = "b"
	b.Lat = 57.21
	b.Lon = -3.67
	b.Start = time.Date(2026, 10, 20, 0, 0, 0, 0, time.UTC)
	b.End = b.Start
	src.DS.Activities = []roadworks.Activity{a, b}
	svc.RoadworksData = src
	params := connector.CheckParams{Items: []connector.CheckItem{{Activity: "walk", WalkID: "wh-cairn-gorm", Date: "2026-10-20"}}}
	on, e := svc.Check(params)
	if e != nil {
		t.Fatal(e)
	}
	result := on.Data.(connector.CheckResult)
	item := &result.Items[0]
	if item.Walk.RoadNote == nil || item.Walk.RoadNote.Activities[0].ID != "a" || len(item.NearbyWalks) != 1 || item.NearbyWalks[0].RoadNote == nil || item.NearbyWalks[0].RoadNote.Activities[0].ID != "b" {
		t.Fatal(item)
	}
	item.Walk.RoadNote = nil
	for i := range item.NearbyWalks {
		item.NearbyWalks[i].RoadNote = nil
	}
	for i := range item.OtherDays {
		item.OtherDays[i].RoadNote = nil
	}
	svc.RoadworksData = nil
	off, e := svc.Check(params)
	if e != nil {
		t.Fatal(e)
	}
	if !reflect.DeepEqual(result, off.Data) {
		t.Fatal("roadworks changed check verdicts, alternatives, or weather details")
	}
	p := connector.PlanParams{Lat: f(57.17), Lon: f(-3.67), Activities: []string{"walk"}, From: s("2026-10-20"), To: s("2026-10-20")}
	off, e = svc.Plan(p)
	if e != nil {
		t.Fatal(e)
	}
	svc.RoadworksData = src
	on, e = svc.Plan(p)
	if e != nil {
		t.Fatal(e)
	}
	planned := on.Data.(connector.PlanResult)
	for i := range planned.Days {
		for j := range planned.Days[i].Walks {
			o := &planned.Days[i].Walks[j]
			if o.RoadNote == nil || o.RoadNote.Activities[0].ID != "b" {
				t.Fatal("plan used caller instead of walk start", o)
			}
			o.RoadNote = nil
		}
	}
	if !reflect.DeepEqual(planned, off.Data) {
		t.Fatal("roadworks changed plan order or weather")
	}
}
