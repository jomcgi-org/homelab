package mcp

import (
	"io"
	"log/slog"
	"testing"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/fixture"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/meter"
	"github.com/prometheus/client_golang/prometheus"
)

func TestRoadworksPlanAndCheckUseSharedSemantics(t *testing.T) {
	svc := fixture.RoadworksService()
	svc.RoadworksData = fixture.WalkRoadworks()
	h := New(svc, meter.New(prometheus.NewRegistry()), slog.New(slog.NewTextHandler(io.Discard, nil)), ServerInfo{Name: "test", Version: "test"}, nil)
	for name, args := range map[string]string{
		"plan":  `{"lat":57.17,"lon":-3.67,"activities":["walk"],"from":"2026-10-20","to":"2026-10-20"}`,
		"check": `{"items":[{"activity":"walk","walk_id":"wh-cairn-gorm","date":"2026-10-20"}]}`,
	} {
		code, _, r := post(t, h, `{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"`+name+`","arguments":`+args+`,`+modernMeta+`}}`, modern("tools/call", name))
		if code != 200 || r.Error != nil || r.Result["isError"] != false {
			t.Fatal(r)
		}
		data := r.Result["structuredContent"].(map[string]any)["data"].(map[string]any)
		var options []any
		if name == "plan" {
			options = data["days"].([]any)[0].(map[string]any)["walks"].([]any)
		} else {
			item := data["items"].([]any)[0].(map[string]any)
			options = append(options, item["walk"])
			options = append(options, item["nearby_walks"].([]any)...)
		}
		if len(options) == 0 {
			t.Fatal(data)
		}
		for _, v := range options {
			note, ok := v.(map[string]any)["road_note"].(map[string]any)
			if !ok || note["message"] != "Planned works listed; check Traffic Scotland on the day." {
				t.Fatal(v)
			}
		}
	}
}
