// Package warnings implements the staged Met Office regional RSS contract.
package warnings

import (
	"bytes"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"io"
	"net/url"
	"strings"
	"time"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/verdict"
)

const (
	MaxBytes     = 2 << 20
	MaxFeedBytes = 256 << 10
	MaxItems     = 100
	MaxAge       = 5 * time.Minute
	OfficialURL  = "https://www.metoffice.gov.uk/weather/warnings-and-advice/uk-warnings"
	TermsURL     = "https://www.metoffice.gov.uk/policies/tandc"
	FeedBase     = "https://weather.metoffice.gov.uk/public/data/PWSCache/WarningsRSS/Region/"
)

var (
	Regions     = []string{"os", "he", "gr", "st", "ta", "dg"}
	regionNames = map[string]string{
		"os": "Orkney & Shetland", "he": "Highlands & Eilean Siar", "gr": "Grampian",
		"st": "Strathclyde", "ta": "Central, Tayside & Fife", "dg": "Dumfries, Galloway, Lothian & Borders",
	}
)

// Item retains the original RSS character data. Derived metadata is deliberately
// separate, and is rederived by consumers rather than trusted from object JSON.
type Item struct {
	Title       string `json:"title"`
	Description string `json:"description"`
	Link        string `json:"link"`
}
type Region struct {
	Code string `json:"code"`
	// CheckedAt advances only on a complete valid HTTP 200 feed read.
	CheckedAt time.Time `json:"checked_at"`
	Complete  bool      `json:"complete"`
	Items     []Item    `json:"items"`
}
type Snapshot struct {
	Digest    string    `json:"-"`
	Version   int       `json:"version"`
	CreatedAt time.Time `json:"created_at"`
	Regions   []Region  `json:"regions"`
}

type Metadata struct {
	Severity    string
	Area        string
	From, Until time.Time
}

func validLink(raw string) bool {
	u, e := url.Parse(raw)
	return e == nil && u.Scheme == "https" && u.Host == "www.metoffice.gov.uk" && u.User == nil && u.Path == "/weather/warnings-and-advice/uk-warnings"
}

func validateItem(i Item) error {
	if i.Title == "" || i.Description == "" || len(i.Title) > 2048 || len(i.Description) > 16384 || len(i.Link) > 4096 || !validLink(i.Link) {
		return fmt.Errorf("warnings: invalid item")
	}
	return nil
}

func Parse(body []byte) (*Snapshot, error) {
	if len(body) > MaxBytes {
		return nil, fmt.Errorf("warnings: oversized snapshot")
	}
	var s Snapshot
	d := json.NewDecoder(bytes.NewReader(body))
	d.DisallowUnknownFields()
	if e := d.Decode(&s); e != nil {
		return nil, e
	}
	if d.Decode(new(any)) != io.EOF {
		return nil, fmt.Errorf("warnings: trailing JSON")
	}
	if s.Version != 1 || s.CreatedAt.IsZero() || len(s.Regions) != len(Regions) {
		return nil, fmt.Errorf("warnings: invalid contract")
	}
	seen := map[string]bool{}
	for _, r := range s.Regions {
		if regionNames[r.Code] == "" || seen[r.Code] || len(r.Items) > MaxItems {
			return nil, fmt.Errorf("warnings: invalid regions")
		}
		seen[r.Code] = true
		if r.Complete && (r.CheckedAt.IsZero() || r.CheckedAt.After(s.CreatedAt) || r.Items == nil) {
			return nil, fmt.Errorf("warnings: invalid source clock/coverage")
		}
		if !r.Complete && (!r.CheckedAt.IsZero() || len(r.Items) > 0) {
			return nil, fmt.Errorf("warnings: failed region carries observations")
		}
		for _, i := range r.Items {
			if e := validateItem(i); e != nil {
				return nil, e
			}
		}
	}
	s.Digest = fmt.Sprintf("%x", sha256.Sum256(body))
	return &s, nil
}

// Derive understands only the observed RSS description grammar. Dates without a
// year are anchored to the item's own date link, never packaging/collection time.
// RSS text has no timezone: use the union of UTC and London interpretations,
// and refuse ambiguous/nonexistent London wall clocks. This overstates validity
// by at most one hour; it cannot clear an activity inside either interpretation.
func Derive(i Item, region string) (Metadata, bool) {
	return derive(i, regionNames[region])
}

func derive(i Item, regionName string) (Metadata, bool) {
	var m Metadata
	if validateItem(i) != nil {
		return m, false
	}
	severity, rest, ok := strings.Cut(i.Description, " warning of ")
	if !ok || (severity != "Yellow" && severity != "Amber" && severity != "Red") || !strings.HasPrefix(i.Title, severity+" warning of ") {
		return m, false
	}
	_, rest, ok = strings.Cut(rest, " affecting ")
	if !ok {
		return m, false
	}
	area, span, ok := strings.Cut(rest, " valid from ")
	if !ok {
		return m, false
	}
	name, places, ok := strings.Cut(area, ": ")
	if !ok || i.Title != nameTitle(i.Description) || places == "" || name != regionName || regionName == "" {
		return m, false
	}
	first, last, ok := strings.Cut(span, " to ")
	if !ok {
		return m, false
	}
	u, _ := url.Parse(i.Link)
	q, err := url.ParseQuery(strings.TrimPrefix(u.Fragment, "?"))
	if err != nil {
		return m, false
	}
	anchor, err := time.Parse("2006-01-02", q.Get("date"))
	if err != nil {
		return m, false
	}
	from, ok := rssTime(first, anchor)
	if !ok {
		return m, false
	}
	until, ok := rssTime(last, anchor)
	if !ok {
		return m, false
	}
	if !until.After(from) || until.Sub(from) > 8*24*time.Hour {
		return m, false
	}
	loc, _ := time.LoadLocation("Europe/London")
	lf, ok := londonClock(from, loc)
	if !ok {
		return m, false
	}
	lu, ok := londonClock(until, loc)
	if !ok {
		return m, false
	}
	if lf.Before(from) {
		from = lf
	}
	if lu.After(until) {
		until = lu
	}
	return Metadata{Severity: severity, Area: area, From: from, Until: until}, true
}

func rssTime(raw string, anchor time.Time) (time.Time, bool) {
	var matches []time.Time
	for _, year := range []int{anchor.Year() - 1, anchor.Year(), anchor.Year() + 1} {
		t, e := time.Parse("1504 Mon 02 Jan 2006", raw+fmt.Sprintf(" %d", year))
		if e == nil && t.Format("1504 Mon 02 Jan") == raw && t.Sub(anchor) >= -24*time.Hour && t.Sub(anchor) <= 8*24*time.Hour {
			matches = append(matches, t)
		}
	}
	if len(matches) != 1 {
		return time.Time{}, false
	}
	return matches[0], true
}

func londonClock(t time.Time, loc *time.Location) (time.Time, bool) {
	// Enumerate candidate offsets to detect both gaps and folds independently of
	// time.Date's arbitrary resolution of ambiguous wall clocks.
	var matches []time.Time
	for _, offset := range []time.Duration{0, time.Hour} {
		c := t.Add(-offset)
		if c.In(loc).Format("2006-01-02 15:04") == t.Format("2006-01-02 15:04") {
			matches = append(matches, c)
		}
	}
	if len(matches) != 1 {
		return time.Time{}, false
	}
	return matches[0], true
}

const CoverageNote = "Met Office conservative regional screen across all six Scottish feeds; a warning in any region is treated as potentially relevant. No polygon match or whole-route coverage is established."

// Apply never upgrades an unsafe verdict. Missing/stale coverage cannot produce a
// reassuring verdict. Expired warning words are omitted from current assertions.
func Apply(s *Snapshot, now, from, until time.Time, v verdict.Verdict, why string) (verdict.Verdict, string) {
	unknown := s == nil || !until.After(from)
	var words []string
	level := 0
	seen := map[string]bool{}
	if s != nil {
		if s.CreatedAt.After(now) || len(s.Regions) != len(Regions) {
			unknown = true
		}
		covered := map[string]bool{}
		for _, r := range s.Regions {
			if covered[r.Code] || regionNames[r.Code] == "" {
				unknown = true
			}
			covered[r.Code] = true
			if !r.Complete || r.CheckedAt.IsZero() || r.CheckedAt.After(now) || now.Sub(r.CheckedAt) >= MaxAge {
				unknown = true
				continue
			}
			for _, i := range r.Items {
				m, ok := Derive(i, r.Code)
				if !ok {
					unknown = true
					continue
				}
				if !m.Until.After(now) || !m.Until.After(from) || !until.After(m.From) {
					continue
				}
				key := i.Title + "\x00" + i.Description + "\x00" + i.Link
				if !seen[key] {
					words = append(words, i.Title+"\n"+i.Description+"\n"+i.Link)
					seen[key] = true
				}
				if m.Severity == "Yellow" {
					level = max(level, 1)
				} else {
					level = 2
				}
			}
		}
	}
	if level == 2 {
		v = verdict.No
	}
	if unknown {
		if v == verdict.Go || v == verdict.Marginal {
			v = verdict.Unknown
		}
		why += " Met Office warnings unknown: missing, stale or unresolved regional coverage. See " + OfficialURL + "."
	} else if level == 1 && v == verdict.Go {
		v = verdict.Marginal
	}
	why += " " + CoverageNote
	if len(words) > 0 {
		why += "\n" + strings.Join(words, "\n")
	}
	return v, why
}

func nameTitle(d string) string { s, _, _ := strings.Cut(d, ": "); return s }
