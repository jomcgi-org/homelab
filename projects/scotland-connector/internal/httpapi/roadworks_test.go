package httpapi

import (
	"io"
	"log/slog"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/fixture"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/meter"
	"github.com/prometheus/client_golang/prometheus"
)

func TestRoadNotesAndETagTransitions(t *testing.T) {
	svc := fixture.RoadworksService()
	roads := fixture.WalkRoadworks()
	svc.RoadworksData = roads
	h, e := New(svc, meter.New(prometheus.NewRegistry()), slog.New(slog.NewTextHandler(io.Discard, nil)), "")
	if e != nil {
		t.Fatal(e)
	}
	url := "/v1/plan?lat=57.17&lon=-3.67&activities=walk&from=2026-10-20&to=2026-10-20"
	r, body := get(t, h, url)
	days := body["data"].(map[string]any)["days"].([]any)
	options := days[0].(map[string]any)["walks"].([]any)
	if len(options) == 0 {
		t.Fatal(body)
	}
	for _, v := range options {
		o := v.(map[string]any)
		if o["road_note"] == nil || o["verdict"] != "go" {
			t.Fatal(o)
		}
	}
	tag := r.Header().Get("ETag")
	r, _ = get(t, h, url, "If-None-Match", tag)
	if r.Code != 304 {
		t.Fatal(r.Code)
	}
	roads.DS.Version = "replacement"
	r, _ = get(t, h, url, "If-None-Match", tag)
	if r.Code != 200 || r.Header().Get("ETag") == tag {
		t.Fatal("replacement retained 304")
	}
	// Expire inside the same as-of hour to isolate the freshness transition.
	now := fixture.Now
	roads.DS.SourceModifiedAt = now.Add(-36 * time.Hour).Add(time.Second)
	roads.DS.CollectedAt = now.Add(-time.Minute)
	svc.Now = func() time.Time { return now }
	r, _ = get(t, h, url)
	tag = r.Header().Get("ETag")
	now = now.Add(time.Second)
	r, body = get(t, h, url, "If-None-Match", tag)
	if r.Code != 200 || r.Header().Get("ETag") == tag {
		t.Fatal("expiry retained 304")
	}
	options = body["data"].(map[string]any)["days"].([]any)[0].(map[string]any)["walks"].([]any)
	for _, v := range options {
		if v.(map[string]any)["road_note"] != nil {
			t.Fatal("stale road note")
		}
	}
	// Check includes the original walk, other dates and better nearby walks.
	svc.Now = func() time.Time { return fixture.Now }
	roads.DS = fixture.WalkRoadworks().DS
	_, body = postCheck(t, h, `{"items":[{"activity":"walk","walk_id":"wh-cairn-gorm","date":"2026-10-20"}]}`)
	item := body["data"].(map[string]any)["items"].([]any)[0].(map[string]any)
	if item["walk"].(map[string]any)["road_note"] == nil {
		t.Fatal(item)
	}
	nearby, ok := item["nearby_walks"].([]any)
	if !ok || len(nearby) == 0 {
		t.Fatal("expected better nearby walk", item)
	}
	for _, v := range nearby {
		if v.(map[string]any)["road_note"] == nil {
			t.Fatal(v)
		}
	}
	// Disabled and missing feeds preserve existing eligibility and verdicts.
	svc.RoadworksData = nil
	_, off := get(t, h, url)
	offWalk := off["data"].(map[string]any)["days"].([]any)[0].(map[string]any)["walks"].([]any)[0].(map[string]any)
	if offWalk["road_note"] != nil || offWalk["verdict"] != "go" {
		t.Fatal(offWalk)
	}
}
