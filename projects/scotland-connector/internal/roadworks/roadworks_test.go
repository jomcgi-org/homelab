package roadworks_test

import (
	"encoding/json"
	"math"
	"strings"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/fixture"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/roadworks"
)

func sample(t *testing.T) *roadworks.Snapshot {
	t.Helper()
	s, e := roadworks.Parse(fixture.RoadworksJSON)
	if e != nil {
		t.Fatal(e)
	}
	return s
}

func TestPythonContractAndWindows(t *testing.T) {
	s := sample(t)
	now := s.CollectedAt
	n := s.Match(56.7314, -5.2236, "2026-11-13", now)
	if n == nil || !strings.Contains(n.Activities[0].Timing, "amnesties") || !strings.Contains(n.Source, "OGL v3") {
		t.Fatal(n)
	}
	single := *s
	for _, a := range s.Activities {
		if a.Reference == "XQ001-NW/2026/45556-1" {
			single.Activities = []roadworks.Activity{a}
		}
	}
	if single.Match(56.7314, -5.2236, "2026-11-28", now) != nil {
		t.Fatal("date after expected end must be excluded")
	}
	if s.Match(57.17, -3.67, "2026-11-13", now) != nil {
		t.Fatal("remote activity matched")
	}
	if s.Match(56.7314, -5.2236, "2026-11-13", s.SourceModifiedAt.Add(roadworks.MaxAge)) != nil {
		t.Fatal("stale matched")
	}
	if s.Fresh(s.CollectedAt.Add(-time.Nanosecond)) {
		t.Fatal("future collection accepted")
	}
	if (*roadworks.Snapshot)(nil).Match(57, -4, "2026-11-13", now) != nil {
		t.Fatal("missing matched")
	}
}

func TestDistanceBoundaryOrderingAndCap(t *testing.T) {
	s := sample(t)
	a := s.Activities[2]
	s.Activities = []roadworks.Activity{a}
	// Haversine uses the same 6371 km sphere as documented stars.DistanceKm.
	for _, tc := range []struct {
		km   float64
		want bool
	}{{4.999, true}, {5.001, false}} {
		lat := a.Lat + tc.km/6371*180/math.Pi
		if got := s.Match(lat, a.Lon, "2026-11-13", s.CollectedAt); (got != nil) != tc.want {
			t.Fatalf("%g: %v", tc.km, got)
		}
	}
	for i := 0; i < 4; i++ {
		b := a
		b.ID = string(rune('b' + i))
		s.Activities = append(s.Activities, b)
	}
	n := s.Match(a.Lat, a.Lon, "2026-11-13", s.CollectedAt)
	if n == nil || len(n.Activities) != 3 || !n.Truncated || n.Activities[0].ID != a.ID {
		t.Fatal(n)
	}
}

func TestLondonDSTCivilDays(t *testing.T) {
	for _, tc := range []struct {
		date, start, end string
		match            bool
	}{
		{"2026-03-29", "2026-03-28T23:30:00Z", "2026-03-28T23:59:00Z", false},
		{"2026-03-29", "2026-03-29T22:59:00Z", "2026-03-29T23:00:00Z", true},
		{"2026-03-29", "2026-03-29T23:00:00Z", "2026-03-30T00:00:00Z", false},
		{"2026-10-25", "2026-10-24T23:00:00Z", "2026-10-25T00:00:00Z", true},
		{"2026-10-25", "2026-10-25T23:59:00Z", "2026-10-26T00:00:00Z", true},
	} {
		s := sample(t)
		s.Activities = s.Activities[:1]
		a := &s.Activities[0]
		a.Start, _ = time.Parse(time.RFC3339, tc.start)
		a.End, _ = time.Parse(time.RFC3339, tc.end)
		if got := s.Match(a.Lat, a.Lon, tc.date, s.CollectedAt); (got != nil) != tc.match {
			t.Fatalf("%+v got %v", tc, got)
		}
	}
}

func TestInvalidContractAndEmpty(t *testing.T) {
	for _, change := range []func(*roadworks.Snapshot){
		func(s *roadworks.Snapshot) { s.SchemaVersion = 2 },
		func(s *roadworks.Snapshot) { s.Activities = nil },
		func(s *roadworks.Snapshot) { s.Activities[0].Control = "Portable Traffic Lights (TTLS)" },
		func(s *roadworks.Snapshot) { s.Activities[0].Lat = 0 },
		func(s *roadworks.Snapshot) { s.Activities[0].End = s.Activities[0].Start.AddDate(0, 0, -1) },
		func(s *roadworks.Snapshot) { s.Activities = append(s.Activities, s.Activities[0]) },
	} {
		s := sample(t)
		change(s)
		b, _ := json.Marshal(s)
		if _, e := roadworks.Parse(b); e == nil {
			t.Fatal("bad contract accepted", string(b))
		}
	}
	s := sample(t)
	s.Activities = []roadworks.Activity{}
	b, _ := json.Marshal(s)
	empty, e := roadworks.Parse(b)
	if e != nil || !empty.Fresh(s.CollectedAt) {
		t.Fatal(e)
	}
	if _, e := roadworks.Parse(append(b, []byte(" {}")...)); e == nil {
		t.Fatal("trailing JSON")
	}
}

func TestSingleDayProviderWindow(t *testing.T) {
	s := sample(t)
	for _, a := range s.Activities {
		if a.Reference == "BT067-1463228" || a.Reference == "PK001-S24936" {
			one := *s
			one.Activities = []roadworks.Activity{a}
			date := a.Start.In(time.FixedZone("UTC", 0)).Format("2006-01-02")
			if one.Match(a.Lat, a.Lon, date, s.CollectedAt) == nil {
				t.Fatal("single-day expected window omitted", a)
			}
		}
	}
}
