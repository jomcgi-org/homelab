package connector

import (
	"time"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/verdict"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/walks"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/warnings"
)

var sourceWarnings = Source{Name: "Met Office", Attribution: "Met Office weather warnings, © Crown copyright", License: "Met Office website terms of use (RSS)", URL: warnings.TermsURL, Use: "verbatim regional RSS warnings and conservative activity suitability screen"}

type warningScreen struct {
	snapshot *warnings.Snapshot
	now      time.Time
}

func (s *Service) warningScreen(now time.Time) *warningScreen {
	if s.WarningsData == nil {
		return nil
	}
	return &warningScreen{s.WarningsData.Current(), now}
}

func firstScreen(s []*warningScreen) *warningScreen {
	if len(s) > 0 {
		return s[0]
	}
	return nil
}

// Use the whole civil walking date, or noon-to-noon night, as a conservative
// superset of every offered activity slot. Local AddDate preserves DST days.
func (s *warningScreen) apply(day int32, night bool, v verdict.Verdict, why string) (verdict.Verdict, string) {
	if s == nil {
		return v, why
	}
	from, until := warningBounds(day, night)
	return warnings.Apply(s.snapshot, s.now, from, until, v, why)
}

func warningBounds(day int32, night bool) (time.Time, time.Time) {
	loc, _ := time.LoadLocation("Europe/London")
	date := time.Unix(int64(day)*86400, 0).UTC()
	hour := 0
	if night {
		hour = 12
	}
	from := time.Date(date.Year(), date.Month(), date.Day(), hour, 0, 0, 0, loc)
	return from, from.AddDate(0, 0, 1)
}

func (s *warningScreen) annotate(m *Meta) {
	if s == nil {
		return
	}
	m.Sources = append(m.Sources, sourceWarnings)
	version := "unavailable"
	if s.snapshot != nil {
		version = s.snapshot.Digest
	}
	m.DataVersion += "|warnings:" + version
}

// Extend the civil-date screen for long walks whose offered starts can run past
// midnight. The same assessment is used before candidate ranking and output.
func (s *warningScreen) walk(day int32, w *walks.Walk, a walks.Assessment) (verdict.Verdict, string) {
	if s == nil {
		return a.Verdict, a.Why
	}
	from, until := warningBounds(day, false)
	if !a.StartFrom.IsZero() && a.StartFrom.Before(from) {
		from = a.StartFrom
	}
	if !a.StartBy.IsZero() {
		end := a.StartBy.Add(time.Duration(w.DurationH * float64(time.Hour)))
		if end.After(until) {
			until = end
		}
	}
	return warnings.Apply(s.snapshot, s.now, from, until, a.Verdict, a.Why)
}
