// Package roadworks matches planned SRWR activity windows near walk starts.
package roadworks

import (
	"bytes"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"io"
	"regexp"
	"time"
	_ "time/tzdata"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/stars"
)

const (
	MaxBytes  = 4 << 20
	MaxAge    = 36 * time.Hour
	RadiusKm  = 5.0
	SourceURL = "https://downloads.srwr.scot/disruptions-export/daily/"
)

var (
	aOrMotorway = regexp.MustCompile(`(?i)^[AM][0-9]{1,4}t?\b`)
	trunkRoad   = regexp.MustCompile(`(?i)\btrunk road\b`)
)

var london = func() *time.Location {
	l, err := time.LoadLocation("Europe/London")
	if err != nil {
		panic(err)
	}
	return l
}()

type Activity struct {
	ID        string    `json:"id"`
	Reference string    `json:"reference"`
	Phase     string    `json:"phase"`
	Road      string    `json:"road"`
	Control   string    `json:"control"`
	Status    string    `json:"status"`
	Lat       float64   `json:"lat"`
	Lon       float64   `json:"lon"`
	Start     time.Time `json:"start"`
	End       time.Time `json:"end"`
	UpdatedAt time.Time `json:"updated_at"`
	Timing    string    `json:"timing"`
	Location  string    `json:"location"`
}

type Snapshot struct {
	SchemaVersion    int        `json:"schema_version"`
	SourceURL        string     `json:"source_url"`
	SourceModifiedAt time.Time  `json:"source_modified_at"`
	CollectedAt      time.Time  `json:"collected_at"`
	Activities       []Activity `json:"activities"`
	Version          string     `json:"-"`
}

func Parse(raw []byte) (*Snapshot, error) {
	if len(raw) == 0 || len(raw) > MaxBytes {
		return nil, fmt.Errorf("roadworks: size limit")
	}
	var s Snapshot
	d := json.NewDecoder(bytes.NewReader(raw))
	d.DisallowUnknownFields()
	if err := d.Decode(&s); err != nil {
		return nil, fmt.Errorf("roadworks: invalid snapshot")
	}
	if d.Decode(new(any)) != io.EOF || s.SchemaVersion != 1 || s.SourceURL != SourceURL || s.SourceModifiedAt.IsZero() || s.CollectedAt.Before(s.SourceModifiedAt) || s.CollectedAt.Sub(s.SourceModifiedAt) >= MaxAge || s.Activities == nil || len(s.Activities) > 5000 {
		return nil, fmt.Errorf("roadworks: invalid contract")
	}
	prev := ""
	for _, a := range s.Activities {
		if a.ID <= prev || a.Reference == "" || len(a.Reference) > 100 || len(a.ID) > 250 || len(a.Phase) > 3 || a.Phase == "" || (!aOrMotorway.MatchString(a.Road) && !trunkRoad.MatchString(a.Road)) || len(a.Road) > 2000 || len(a.Timing) > 16000 || len(a.Location) > 2000 || (a.Control != "Road Closure" && a.Control != "Convoy Working") || !active(a.Status) || a.Lat < 54.5 || a.Lat > 61 || a.Lon < -9 || a.Lon > -0.5 || a.Start.IsZero() || a.End.IsZero() || a.Start.In(london).Format("2006-01-02") > a.End.In(london).Format("2006-01-02") || a.UpdatedAt.IsZero() || a.UpdatedAt.After(s.CollectedAt) {
			return nil, fmt.Errorf("roadworks: invalid activity")
		}
		prev = a.ID
	}
	s.Version = fmt.Sprintf("%x", sha256.Sum256(raw))
	return &s, nil
}

func active(s string) bool {
	switch s {
	case "Advance Planning", "Planned", "Proposed", "Recorded", "Commenced", "In Progress":
		return true
	}
	return false
}

func (s *Snapshot) Fresh(now time.Time) bool {
	return s != nil && !now.Before(s.CollectedAt) && !now.Before(s.SourceModifiedAt) && now.Before(s.SourceModifiedAt.Add(MaxAge))
}

// Validator changes at future collection and source expiry, even within an hour.
func (s *Snapshot) Validator(now time.Time) string {
	if s == nil {
		return "missing"
	}
	return fmt.Sprintf("%s:%t", s.Version, s.Fresh(now))
}

type Note struct {
	Message          string     `json:"message"`
	Source           string     `json:"source"`
	SourceModifiedAt time.Time  `json:"source_modified_at"`
	Activities       []Activity `json:"activities"`
	Truncated        bool       `json:"truncated"`
}

func (s *Snapshot) Match(lat, lon float64, date string, now time.Time) *Note {
	if !s.Fresh(now) {
		return nil
	}
	day, err := time.ParseInLocation("2006-01-02", date, london)
	if err != nil {
		return nil
	}
	date = day.Format("2006-01-02")
	var out []Activity
	truncated := false
	for _, a := range s.Activities {
		if a.Start.In(london).Format("2006-01-02") <= date && a.End.In(london).Format("2006-01-02") >= date && stars.DistanceKm(lat, lon, a.Lat, a.Lon) <= RadiusKm {
			if len(out) == 3 {
				truncated = true
				break
			}
			out = append(out, a)
		}
	}
	if len(out) == 0 {
		return nil
	}
	return &Note{Message: "Planned works listed; check Traffic Scotland on the day.", Source: "Scottish Road Works Commissioner, OGL v3", SourceModifiedAt: s.SourceModifiedAt, Activities: out, Truncated: truncated}
}
