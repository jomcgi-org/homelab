package connector_test

import (
	"fmt"
	"strings"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/connector"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/fixture"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/stars"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/walks"
)

// dense is a service over a site grid three times the real density and
// 3000 walks with a day of good hours each: worse than the live data on
// every axis a plan or check scans.
func dense(b *testing.B) *connector.Service {
	b.Helper()
	var sb strings.Builder
	sb.WriteString(`{"fetched_at":"2026-10-19T14:05:00+00:00","sites":[`)
	n := 0
	for lat := 54.6; lat <= 60.8; lat += 0.02 {
		for lon := -7.6; lon <= -0.8; lon += 0.06 {
			if n > 0 {
				sb.WriteByte(',')
			}
			fmt.Fprintf(&sb, `{"forecast_fetched_at":"2026-10-19T14:05:00Z","id":"s%d","lat":%.4f,"lon":%.4f,"lp_zone":"rural","clear_dark_hours":6,"clear_twilight_hours":6,"best_hours":[`, n, lat, lon)
			for h := 0; h < 25; h++ {
				if h > 0 {
					sb.WriteByte(',')
				}
				fmt.Fprintf(&sb, `{"time":"2026-10-%02dT%02d:00:00Z","cloud_area_fraction":%d,"dark":%t}`, 19+h/8, 18+h%6, h%9, h%3 != 0)
			}
			sb.WriteString(`]}`)
			n++
		}
	}
	sb.WriteString(`]}`)
	sky, err := stars.Parse([]byte(sb.String()), []byte(`{"sites":[]}`), "v")
	if err != nil {
		b.Fatal(err)
	}
	var wb strings.Builder
	wb.WriteString(`{"schema_version":1,"fetched_at":"2026-10-19T14:20:00+00:00","walks":[`)
	day := time.Date(2026, 10, 20, 8, 0, 0, 0, time.UTC).Unix()
	for i := 0; i < 3000; i++ {
		if i > 0 {
			wb.WriteByte(',')
		}
		var hours []string
		for h := int64(0); h < 9; h++ {
			hours = append(hours, fmt.Sprintf("[%d,7,0.1,20,40]", day+h*3600))
		}
		fmt.Fprintf(&wb, `{"forecast_fetched_at":"2026-10-19T14:20:00Z","id":"w%d","name":"Walk %d","url":"https://www.walkhighlands.co.uk/x/%d.shtml","lat":%.3f,"lon":%.3f,"distance_km":10,"ascent_m":500,"duration_h":%d,"hours":[%s]}`,
			i, i, i, 56.5+float64(i%60)*0.02, -4.8+float64(i/60)*0.04, 2+i%8, strings.Join(hours, ","))
	}
	wb.WriteString(`]}`)
	wk, err := walks.Parse([]byte(wb.String()), "w")
	if err != nil {
		b.Fatal(err)
	}
	b.Logf("%d sites, %d walks", len(sky.Sites), len(wk.Walks))
	return &connector.Service{Data: fixture.Source{DS: sky}, Walks: fixture.WalkSource{DS: wk}, Now: func() time.Time { return fixture.Now }}
}

// The widest plan: 100 km, seven days, both activities, five options.
func BenchmarkPlan(b *testing.B) {
	svc := dense(b)
	p := connector.PlanParams{Lat: f(57.19), Lon: f(-3.83), RadiusKm: f(connector.MaxRadiusKm), To: s("2026-10-25"), Limit: i(connector.MaxOptions)}
	b.ResetTimer()
	for b.Loop() {
		if _, err := svc.Plan(p); err != nil {
			b.Fatal(err)
		}
	}
}

// Full computation on every iteration, with the dense snapshot and seven days.
// No response cache exists on the service path.
func BenchmarkLight(b *testing.B) {
	svc := dense(b)
	p := connector.LightParams{Lat: f(57.19), Lon: f(-3.83), RadiusKm: f(connector.MaxRadiusKm), To: s("2026-10-25")}
	b.ResetTimer()
	for b.Loop() {
		if _, err := svc.Light(p); err != nil {
			b.Fatal(err)
		}
	}
}

// The default plan: 30 km, three days, both activities.
func BenchmarkPlanDefault(b *testing.B) {
	svc := dense(b)
	p := connector.PlanParams{Lat: f(57.19), Lon: f(-3.83)}
	b.ResetTimer()
	for b.Loop() {
		if _, err := svc.Plan(p); err != nil {
			b.Fatal(err)
		}
	}
}

// The widest plan with weather limits, which copy each day's hours.
func BenchmarkPlanLimited(b *testing.B) {
	svc := dense(b)
	p := connector.PlanParams{
		Lat: f(57.19), Lon: f(-3.83), RadiusKm: f(connector.MaxRadiusKm), To: s("2026-10-25"), Limit: i(connector.MaxOptions),
		WeatherLimits: connector.WeatherLimits{MaxRainMMH: f(0.5), MaxWindKmh: f(30)},
	}
	b.ResetTimer()
	for b.Loop() {
		if _, err := svc.Plan(p); err != nil {
			b.Fatal(err)
		}
	}
}

// The widest check: ten items, half walks and half nights, each with its
// other days and nearby alternatives.
func BenchmarkCheck(b *testing.B) {
	svc := dense(b)
	var items []connector.CheckItem
	for n := 0; n < connector.MaxCheckItems/2; n++ {
		items = append(items,
			connector.CheckItem{Activity: "walk", Date: "2026-10-20", WalkName: fmt.Sprintf("walk %d", n*7)},
			connector.CheckItem{Activity: "stargaze", Date: "2026-10-19", Lat: f(57 + float64(n)*0.1), Lon: f(-4)})
	}
	p := connector.CheckParams{Items: items}
	b.ResetTimer()
	for b.Loop() {
		if _, err := svc.Check(p); err != nil {
			b.Fatal(err)
		}
	}
}
