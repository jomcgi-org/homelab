package summits

import (
	_ "embed"
	"strings"
	"testing"
	"time"
)

//go:embed testdata/summits.json
var fixture []byte

func parsed(t *testing.T) *Snapshot {
	t.Helper()
	d, e := Parse(fixture)
	if e != nil {
		t.Fatal(e)
	}
	return d
}
func val(v float64) *float64 { return &v }

func TestSharedContract(t *testing.T) {
	d := parsed(t)
	h := d.Points[0].Hours[0]
	if *h.Wind != 36 || *h.Gust != 54 || *h.Cloud != 200 || *h.Freezing != 750 || d.Points[0].Hours[1].Wind != nil {
		t.Fatal("units or nulls changed")
	}
}

func TestInvalidReplacements(t *testing.T) {
	for _, change := range [][2]string{
		{`"schema_version": 1`, `"schema_version": 2`},
		{`"fixture": true`, `"fixture": false`},
		{`"wind_kmh": 36`, `"wind_kmh": -1`},
		{`"gust_kmh": 54`, `"gust_kmh": 501`},
		{`"cloud_base_m": 200`, `"cloud_base_m": -1`},
		{`"freezing_level_m_asl": 750`, `"freezing_level_m_asl": 20001`},
		{`"elevation_m_asl": 810`, `"elevation_m_asl": 10000`},
		{`"sample_lat": 57.18`, `"sample_lat": 58`},
		{`2026-10-20T08:00:00Z`, `2026-10-20T08:30:00Z`},
		{`2026-10-20T08:00:00Z`, `2026-10-20T08:00:00+01:00`},
		{`2026-10-20T08:00:00Z`, `2026-10-26T08:00:00Z`},
		{`2026-10-20T09:00:00Z`, `2026-10-20T08:00:00Z`},
		{License, `https://invalid.test/license`},
		{`"above_ground"`, `"elevation"`},
	} {
		if _, e := Parse([]byte(strings.Replace(string(fixture), change[0], change[1], 1))); e == nil {
			t.Errorf("accepted %v", change)
		}
	}
	for _, body := range [][]byte{nil, []byte(`{}`), []byte(`{`), make([]byte, MaxBytes+1)} {
		if _, e := Parse(body); e == nil {
			t.Fatal("invalid body")
		}
	}
}

func TestHeightIdentityFreshnessAndSlot(t *testing.T) {
	for _, tc := range []struct {
		name             string
		modify           func(*Snapshot)
		status, contains string
	}{
		{"AGL", func(*Snapshot) {}, "sampled", "summit in cloud"},
		{"freezing", func(d *Snapshot) { d.Points[0].Hours[0].Cloud = nil }, "sampled", "above freezing level"},
		{"MSL", func(d *Snapshot) {
			d.Points[0].CloudReference = "mean_sea_level"
			d.Points[0].Hours[0].Cloud = val(700)
		}, "sampled", "summit in cloud"},
		{"AGL terrain missing", func(d *Snapshot) { d.Points[0].Orography = nil; d.Points[0].Hours[0].Freezing = nil }, "sampled", ""},
		{"unknown reference", func(d *Snapshot) { d.Points[0].CloudReference = "unknown"; d.Points[0].Hours[0].Freezing = nil }, "sampled", ""},
		{"no elevation", func(d *Snapshot) { d.Points[0].Elevation = nil }, "sampled", ""},
		{"walk start", func(d *Snapshot) {
			p := &d.Points[0]
			p.Kind = "walk_start"
			p.Lat = 57.17
			p.Lon = -3.67
			d.Fixture = false
		}, "sampled", ""},
		{"wrong walk start", func(d *Snapshot) { d.Points[0].Kind = "walk_start" }, "unavailable", ""},
		{"no hours", func(d *Snapshot) { d.Points[0].Hours = nil }, "unavailable", ""},
		{"outside slot", func(d *Snapshot) { d.Points[0].Hours[0].ValidAt = d.RunAt }, "sampled", ""},
		{"stale", func(d *Snapshot) { d.RunAt = d.RunAt.Add(-24 * time.Hour) }, "stale", ""},
		{"future", func(d *Snapshot) { d.RunAt = d.RunAt.Add(24 * time.Hour) }, "stale", ""},
	} {
		t.Run(tc.name, func(t *testing.T) {
			d := parsed(t)
			tc.modify(d)
			start := time.Date(2026, 10, 20, 8, 0, 0, 0, time.UTC)
			out, hazard := d.Assess("wh-meall-a-bhuachaille", 57.17, -3.67, start, start.Add(6*time.Hour), start.Add(-time.Hour))
			if out.Status != tc.status || (tc.contains == "" && hazard != "") || !strings.Contains(hazard, tc.contains) {
				t.Fatalf("%+v %q", out, hazard)
			}
		})
	}
	var d *Snapshot
	out, hazard := d.Assess("x", 57, -4, time.Time{}, time.Time{}, time.Now())
	if out.Status != "unavailable" || hazard != "" {
		t.Fatal(out, hazard)
	}
}
