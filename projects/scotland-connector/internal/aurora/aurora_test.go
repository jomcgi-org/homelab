package aurora_test

import (
	"encoding/json"
	"math"
	"strings"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/aurora"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/fixture"
)

func snapshot(t *testing.T) *aurora.Snapshot {
	t.Helper()
	in, err := fixture.AuroraInputs()
	if err != nil {
		t.Fatal(err)
	}
	b, err := aurora.Materialize(in)
	if err != nil {
		t.Fatal(err)
	}
	s, err := aurora.Parse(b)
	if err != nil {
		t.Fatal(err)
	}
	return s
}
func ptr(v float64) *float64 { return &v }

func TestAssessment(t *testing.T) {
	for _, tc := range []struct {
		name, want string
		change     func(*aurora.Snapshot, *time.Time, *time.Time)
	}{
		{"favorable", "go", func(*aurora.Snapshot, *time.Time, *time.Time) {}},
		{"cloudy", "no", func(s *aurora.Snapshot, _, _ *time.Time) { s.Sites[0].Weather.Hours[1].Cloud = ptr(80) }},
		{"cloud boundary", "marginal", func(s *aurora.Snapshot, _, _ *time.Time) { s.Sites[0].Weather.Hours[1].Cloud = ptr(25.01) }},
		{"obstructed", "no", func(s *aurora.Snapshot, _, _ *time.Time) { s.Sites[0].Terrain.North[0] = ptr(10) }},
		{"terrain marginal", "marginal", func(s *aurora.Snapshot, _, _ *time.Time) { s.Sites[0].Terrain.North[0] = ptr(3.01) }},
		{"weak", "no", func(s *aurora.Snapshot, _, _ *time.Time) { s.NOAA.Cells[0][2] = 9 }},
		{"moderate", "marginal", func(s *aurora.Snapshot, _, _ *time.Time) { s.NOAA.Cells[0][2] = 29 }},
		{"no terrain", "unknown", func(s *aurora.Snapshot, _, _ *time.Time) { s.Sites[0].Terrain = nil }},
		{"nodata", "unknown", func(s *aurora.Snapshot, _, _ *time.Time) { s.Sites[0].Terrain.North[9] = nil }},
		{"missing cloud", "unknown", func(s *aurora.Snapshot, _, _ *time.Time) { s.Sites[0].Weather.Hours[1].Cloud = nil }},
		{"missing hours", "unknown", func(s *aurora.Snapshot, _, _ *time.Time) { s.Sites[0].Weather.Hours = nil }},
		{"gap", "unknown", func(s *aurora.Snapshot, _, _ *time.Time) {
			s.Sites[0].Weather.Hours = append(s.Sites[0].Weather.Hours[:1], s.Sites[0].Weather.Hours[2:]...)
		}},
		{"capped", "unknown", func(s *aurora.Snapshot, _, _ *time.Time) { s.Sites[0].Weather.Hours = s.Sites[0].Weather.Hours[:1] }},
		{"noaa exact expiry", "unknown", func(s *aurora.Snapshot, _, now *time.Time) { s.NOAA.CollectedAt = now.Add(-aurora.NOAAMaxAge) }},
		{"noaa before expiry", "go", func(s *aurora.Snapshot, _, now *time.Time) {
			s.NOAA.CollectedAt = now.Add(-aurora.NOAAMaxAge + time.Nanosecond)
		}},
		{"observation exact expiry", "unknown", func(s *aurora.Snapshot, _, now *time.Time) { s.NOAA.ObservedAt = now.Add(-aurora.ObservationMaxAge) }},
		{"met exact expiry", "unknown", func(s *aurora.Snapshot, _, now *time.Time) {
			s.Sites[0].Weather.CollectedAt = now.Add(-aurora.METMaxAge)
		}},
		{"met before expiry", "go", func(s *aurora.Snapshot, _, now *time.Time) {
			s.Sites[0].Weather.CollectedAt = now.Add(-aurora.METMaxAge + time.Nanosecond)
		}},
		{"met issue exact expiry", "unknown", func(s *aurora.Snapshot, _, now *time.Time) {
			s.Sites[0].Weather.IssuedAt = now.Add(-aurora.METIssueMaxAge)
		}},
		{"met issue before expiry", "go", func(s *aurora.Snapshot, _, now *time.Time) {
			s.Sites[0].Weather.IssuedAt = now.Add(-aurora.METIssueMaxAge + time.Nanosecond)
		}},
		{"future collected", "unknown", func(s *aurora.Snapshot, _, now *time.Time) { s.NOAA.CollectedAt = now.Add(time.Nanosecond) }},
		{"future observation", "unknown", func(s *aurora.Snapshot, _, now *time.Time) { s.NOAA.ObservedAt = now.Add(time.Nanosecond) }},
		{"future MET", "unknown", func(s *aurora.Snapshot, _, now *time.Time) { s.Sites[0].Weather.IssuedAt = now.Add(time.Nanosecond) }},
		{"future snapshot", "unknown", func(s *aurora.Snapshot, _, now *time.Time) { s.CreatedAt = now.Add(time.Nanosecond) }},
		{"past request", "unknown", func(_ *aurora.Snapshot, at, now *time.Time) { *at = now.Add(-time.Nanosecond) }},
		{"multi-day request", "unknown", func(_ *aurora.Snapshot, at, _ *time.Time) { *at = at.Add(48 * time.Hour) }},
		{"support before", "unknown", func(_ *aurora.Snapshot, at, _ *time.Time) { *at = at.Add(-time.Nanosecond) }},
		{"support after", "unknown", func(_ *aurora.Snapshot, at, _ *time.Time) { *at = at.Add(time.Nanosecond) }},
		{"no lead", "go", func(s *aurora.Snapshot, at, now *time.Time) {
			s.NOAA.ObservedAt = *now
			s.NOAA.ForecastAt = *now
			*at = *now
		}},
		{"no lead cannot extrapolate", "unknown", func(s *aurora.Snapshot, _, now *time.Time) { s.NOAA.ObservedAt = *now; s.NOAA.ForecastAt = *now }},
		{"no grid coverage", "unknown", func(s *aurora.Snapshot, _, _ *time.Time) { s.NOAA.Cells[0][1] = 0 }},
	} {
		t.Run(tc.name, func(t *testing.T) {
			s := snapshot(t)
			at, now := s.NOAA.ForecastAt, fixture.AuroraNow
			tc.change(s, &at, &now)
			a := aurora.Assess(s, 58, -4, at, now)
			if a.Verdict != tc.want {
				t.Fatalf("%s: %+v", tc.want, a)
			}
			if len(a.Evidence) != 4 || a.Why == "" {
				t.Fatal(a)
			}
		})
	}
}

func TestSolarPolicy(t *testing.T) {
	// Shift all source and weather times together so only solar geometry changes.
	for _, tc := range []struct {
		hour int
		want string
	}{{12, "no"}, {16, "no"}, {17, "marginal"}, {23, "go"}} {
		s := snapshot(t)
		delta := time.Duration(tc.hour-23) * time.Hour
		s.CreatedAt = s.CreatedAt.Add(delta)
		s.NOAA.ObservedAt = s.NOAA.ObservedAt.Add(delta)
		s.NOAA.ForecastAt = s.NOAA.ForecastAt.Add(delta)
		s.NOAA.CollectedAt = s.NOAA.CollectedAt.Add(delta)
		w := &s.Sites[0].Weather
		w.CollectedAt = w.CollectedAt.Add(delta)
		w.IssuedAt = w.IssuedAt.Add(delta)
		for i := range w.Hours {
			w.Hours[i].Time = w.Hours[i].Time.Add(delta)
		}
		a := aurora.Assess(s, 58, -4, s.NOAA.ForecastAt, fixture.AuroraNow.Add(delta))
		if a.Verdict != tc.want {
			t.Errorf("hour %d: want %s: sun=%v, %+v", tc.hour, tc.want, *a.SunElevation, a)
		}
	}
}

func TestProducerRejectsMalformedContracts(t *testing.T) {
	for _, tc := range []struct {
		name   string
		change func(*aurora.Inputs)
	}{
		{"invented NOAA fields", func(in *aurora.Inputs) { in.NOAA = json.RawMessage(`{"observed_at":"2026-12-01T22:00:00Z"}`) }},
		{"null cell", func(in *aurora.Inputs) {
			in.NOAA = json.RawMessage(strings.Replace(fixture.AuroraNOAA, "356,58,50", "356,58,null", 1))
		}},
		{"omitted coordinate", func(in *aurora.Inputs) { in.Weather[0].Lat = nil }},
		{"nonfinite coordinate", func(in *aurora.Inputs) { in.Weather[0].Lat = ptr(math.NaN()) }},
		{"wrong MET coordinate", func(in *aurora.Inputs) {
			in.Weather[0].MET = json.RawMessage(strings.Replace(fixture.AuroraMET, "[-4,58,100]", "[-5,58,100]", 1))
		}},
		{"MET bad units", func(in *aurora.Inputs) {
			in.Weather[0].MET = json.RawMessage(strings.Replace(fixture.AuroraMET, `"%"`, `"fraction"`, 1))
		}},
		{"MET missing timestamp", func(in *aurora.Inputs) {
			in.Weather[0].MET = json.RawMessage(strings.Replace(fixture.AuroraMET, `"updated_at":"2026-12-01T22:00:00Z",`, "", 1))
		}},
		{"out of order", func(in *aurora.Inputs) {
			in.Weather[0].MET = json.RawMessage(strings.Replace(fixture.AuroraMET, "2026-12-02T00:00:00Z", "2026-12-01T22:00:00Z", 1))
		}},
		{"cloud out of range", func(in *aurora.Inputs) {
			in.Weather[0].MET = json.RawMessage(strings.Replace(fixture.AuroraMET, `"cloud_area_fraction":5`, `"cloud_area_fraction":101`, 1))
		}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			in, err := fixture.AuroraInputs()
			if err != nil {
				t.Fatal(err)
			}
			tc.change(&in)
			if _, err := aurora.Materialize(in); err == nil {
				t.Fatal("accepted malformed input")
			}
		})
	}
	var cell aurora.Cell
	for _, b := range []string{`[0,null,2]`, `[0,2]`, `[0,1,2,3]`, `[0,"1",2]`} {
		if json.Unmarshal([]byte(b), &cell) == nil {
			t.Fatal(b)
		}
	}
	if _, err := aurora.Parse([]byte(strings.Repeat(" ", aurora.MaxBytes+1))); err == nil {
		t.Fatal("oversized snapshot")
	}
	s := snapshot(t)
	b, _ := json.Marshal(s)
	for _, bad := range [][]byte{append(b, []byte(`{}`)...), []byte(`{}`), []byte(`{"schema":1,"unknown":true}`)} {
		if _, err := aurora.Parse(bad); err == nil {
			t.Fatal("accepted malformed snapshot")
		}
	}
}

func TestObservedNOAAFieldContract(t *testing.T) {
	in, _ := fixture.AuroraInputs()
	in.NOAA = json.RawMessage(fixture.NOAAObserved)
	in.CollectedAt = time.Date(2026, 10, 25, 17, 0, 0, 0, time.UTC)
	in.NOAACollectedAt = in.CollectedAt
	// Weather is deliberately later: independent chronology must reject it.
	if _, err := aurora.Materialize(in); err == nil {
		t.Fatal("future weather accepted")
	}
	in.Weather[0].CollectedAt = in.CollectedAt
	in.Weather[0].MET = json.RawMessage(strings.NewReplacer("2026-12-01T22:00:00Z", "2026-10-25T16:00:00Z", "2026-12-01T23:00:00Z", "2026-10-25T17:00:00Z", "2026-12-02T00:00:00Z", "2026-10-25T18:00:00Z").Replace(fixture.AuroraMET))
	b, err := aurora.Materialize(in)
	if err != nil {
		t.Fatal(err)
	}
	s, err := aurora.Parse(b)
	if err != nil {
		t.Fatal(err)
	}
	if s.NOAA.ForecastAt.Format(time.RFC3339) != "2026-10-25T18:05:00Z" || s.NOAA.Cells[0][2] != 0 {
		t.Fatal("observed provider fields lost")
	}
	var raw map[string]json.RawMessage
	if err := json.Unmarshal([]byte(fixture.NOAAObserved), &raw); err != nil {
		t.Fatal(err)
	}
	for _, k := range []string{"Observation Time", "Forecast Time", "Data Format", "coordinates"} {
		if len(raw[k]) == 0 {
			t.Fatal(k)
		}
	}
}
