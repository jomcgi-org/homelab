package warnings

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/verdict"
)

func stamp(s string) time.Time {
	t, e := time.Parse(time.RFC3339, s)
	if e != nil {
		panic(e)
	}
	return t
}

func empty(now time.Time) *Snapshot {
	s := &Snapshot{Version: 1, CreatedAt: now}
	for _, c := range Regions {
		s.Regions = append(s.Regions, Region{Code: c, Complete: true, CheckedAt: now, Items: []Item{}})
	}
	return s
}

// Synthetic Scottish warning using the historical RSS item's text grammar.
func synthetic(severity, from, to string) Item {
	title := severity + " warning of wind affecting Grampian"
	return Item{title, title + ": Moray valid from " + from + " to " + to, OfficialURL + "#?date=2026-10-24&id=synthetic&referrer=rss"}
}

func TestSavedFeeds(t *testing.T) {
	for _, c := range Regions {
		b, e := os.ReadFile("testdata/" + c + "-empty.xml")
		if e != nil {
			t.Fatal(e)
		}
		items, e := ParseFeed(b, c)
		if e != nil || items == nil || len(items) != 0 {
			t.Fatal(c, items, e)
		}
	}
	b, e := os.ReadFile("testdata/se-2023.xml")
	if e != nil {
		t.Fatal(e)
	}
	items, e := parseFeed(b, "London & South East England")
	if e != nil || len(items) != 1 {
		t.Fatal(items, e)
	}
	i := items[0]
	m, ok := derive(i, "London & South East England")
	if !ok || m.Severity != "Yellow" || m.From != stamp("2023-05-10T11:00:00Z") || m.Until != stamp("2023-05-10T19:00:00Z") {
		t.Fatal(m, ok)
	}
	if !strings.Contains(i.Description, "Bracknell Forest, Buckinghamshire") || !strings.Contains(i.Link, "id=83993be1-fbe2-4a1c-9f3d-c371a454e169&referrer=rss") {
		t.Fatal("original words/link lost", i)
	}
	// A feed from another region cannot masquerade as a successful Scottish read.
	if _, e := ParseFeed(b, "gr"); e == nil {
		t.Fatal("wrong regional coverage accepted")
	}
	for _, bad := range [][]byte{nil, []byte("<rss/>"), bytes.Replace(b, []byte("<title>Yellow"), []byte("<alien>Yellow"), 1), append(b, []byte("<rss/>")...), []byte("<!DOCTYPE rss><rss/>"), bytes.Repeat([]byte("x"), MaxFeedBytes+1)} {
		if _, e := parseFeed(bad, "London & South East England"); e == nil {
			t.Fatal("bad XML accepted")
		}
	}
}

func TestFreshnessCoverageAndOriginalWords(t *testing.T) {
	now := stamp("2026-10-24T12:00:00Z")
	for _, tc := range []struct {
		name   string
		change func(*Snapshot)
		want   verdict.Verdict
	}{
		{"empty", func(*Snapshot) {}, verdict.Go},
		{"just fresh", func(s *Snapshot) { s.Regions[0].CheckedAt = now.Add(-MaxAge + time.Nanosecond) }, verdict.Go},
		{"exact boundary", func(s *Snapshot) { s.Regions[0].CheckedAt = now.Add(-MaxAge) }, verdict.Unknown},
		{"future source", func(s *Snapshot) { s.Regions[0].CheckedAt = now.Add(time.Second) }, verdict.Unknown},
		{"future package", func(s *Snapshot) { s.CreatedAt = now.Add(time.Second) }, verdict.Unknown},
		{"partial", func(s *Snapshot) { s.Regions[0].Complete = false }, verdict.Unknown},
		{"absent", func(s *Snapshot) { s.Regions = nil }, verdict.Unknown},
		{"yellow", func(s *Snapshot) {
			s.Regions[2].Items = []Item{synthetic("Yellow", "1200 Sat 24 Oct", "2100 Sat 24 Oct")}
		}, verdict.Marginal},
		{"amber", func(s *Snapshot) {
			s.Regions[2].Items = []Item{synthetic("Amber", "1200 Sat 24 Oct", "2100 Sat 24 Oct")}
		}, verdict.No},
		{"red", func(s *Snapshot) { s.Regions[2].Items = []Item{synthetic("Red", "1200 Sat 24 Oct", "2100 Sat 24 Oct")} }, verdict.No},
		{"unresolved severity", func(s *Snapshot) {
			s.Regions[2].Items = []Item{synthetic("Blue", "1200 Sat 24 Oct", "2100 Sat 24 Oct")}
		}, verdict.Unknown},
		{"unmatched area", func(s *Snapshot) {
			i := synthetic("Yellow", "1200 Sat 24 Oct", "2100 Sat 24 Oct")
			i.Description = strings.ReplaceAll(i.Description, "Grampian", "Atlantis")
			s.Regions[2].Items = []Item{i}
		}, verdict.Unknown},
		{"unresolved time", func(s *Snapshot) { s.Regions[2].Items = []Item{synthetic("Yellow", "noon", "later")} }, verdict.Unknown},
	} {
		t.Run(tc.name, func(t *testing.T) {
			s := empty(now)
			tc.change(s)
			v, why := Apply(s, now, now, now.Add(3*time.Hour), verdict.Go, "weather")
			if v != tc.want {
				t.Fatal(v, why)
			}
			if v == verdict.Unknown && !strings.Contains(why, OfficialURL) {
				t.Fatal(why)
			}
			for _, original := range []verdict.Verdict{verdict.No, verdict.Unknown} {
				v, _ = Apply(s, now, now, now.Add(time.Hour), original, "bad")
				if v != original && !(original == verdict.Unknown && v == verdict.No) {
					t.Fatal("upgraded", v)
				}
			}
		})
	}
	s := empty(now)
	i := synthetic("Amber", "1200 Sat 24 Oct", "2100 Sat 24 Oct")
	s.Regions[2].Items = []Item{i, i}
	_, why := Apply(s, now, now, now.Add(time.Hour), verdict.Go, "")
	if strings.Count(why, i.Description) != 1 || !strings.Contains(why, i.Title+"\n"+i.Description+"\n"+i.Link) {
		t.Fatal(why)
	}
}

func TestValidityWithdrawalAndDST(t *testing.T) {
	now := stamp("2026-10-24T12:00:00Z")
	s := empty(now)
	i := synthetic("Yellow", "2300 Sat 24 Oct", "0400 Sun 25 Oct")
	s.Regions[2].Items = []Item{i}
	m, ok := Derive(i, "gr")
	if !ok || m.From != stamp("2026-10-24T22:00:00Z") || m.Until != stamp("2026-10-25T04:00:00Z") {
		t.Fatal(m, ok)
	}
	v, _ := Apply(s, now, stamp("2026-10-25T00:00:00Z"), stamp("2026-10-25T02:00:00Z"), verdict.Go, "")
	if v != verdict.Marginal {
		t.Fatal(v)
	}
	v, why := Apply(s, now, stamp("2026-10-25T04:00:00Z"), stamp("2026-10-25T05:00:00Z"), verdict.Go, "")
	if v != verdict.Go || strings.Contains(why, i.Description) {
		t.Fatal(v, why)
	}
	// Remove expired text even for an activity interval earlier in the warning.
	later := stamp("2026-10-25T04:00:00Z")
	s.CreatedAt = later
	for j := range s.Regions {
		s.Regions[j].CheckedAt = later
	}
	v, why = Apply(s, later, now, later, verdict.Go, "")
	if v != verdict.Go || strings.Contains(why, i.Description) {
		t.Fatal(v, why)
	}
	s.Regions[2].Items = nil
	v, _ = Apply(s, later, later, later.Add(time.Hour), verdict.Go, "")
	if v != verdict.Go {
		t.Fatal("withdrawal", v)
	}
	for _, i := range []Item{synthetic("Yellow", "0130 Sun 25 Oct", "0400 Sun 25 Oct"), {Title: "x", Description: "y", Link: "https://evil.example/"}} {
		if _, ok := Derive(i, "gr"); ok {
			t.Fatal("ambiguous/unsafe metadata")
		}
	}
	spring := synthetic("Yellow", "0130 Sun 29 Mar", "0400 Sun 29 Mar")
	spring.Link = OfficialURL + "#?date=2026-03-29&id=synthetic"
	if _, ok := Derive(spring, "gr"); ok {
		t.Fatal("nonexistent London time")
	}
}

type transport func(*http.Request) (*http.Response, error)

func (f transport) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }
func TestCollectorRealHTTPContractAndPartial(t *testing.T) {
	now := stamp("2026-11-13T12:00:00Z")
	for _, failure := range []string{"none", "status", "malformed", "timeout", "redirect", "age", "oversized"} {
		t.Run(failure, func(t *testing.T) {
			calls := 0
			tr := transport(func(r *http.Request) (*http.Response, error) {
				calls++
				if r.URL.Scheme != "https" || r.URL.Host != "weather.metoffice.gov.uk" || r.Header.Get("Authorization") != "" || r.Header.Get("If-None-Match") != "" || r.Header.Get("Cache-Control") != "no-cache" {
					t.Fatal(r)
				}
				code := strings.TrimPrefix(r.URL.String(), FeedBase)
				b, e := os.ReadFile("testdata/" + code + "-empty.xml")
				if e != nil {
					t.Fatal(e)
				}
				status := 200
				h := make(http.Header)
				if code == "he" {
					switch failure {
					case "status":
						status = 503
					case "malformed":
						b = []byte("<rss/>")
					case "timeout":
						return nil, context.DeadlineExceeded
					case "redirect":
						status = 302
						h.Set("Location", "https://evil.example/")
					case "age":
						h.Set("Age", "60")
					case "oversized":
						b = bytes.Repeat([]byte("x"), MaxFeedBytes+1)
					}
				}
				return &http.Response{StatusCode: status, Header: h, Body: io.NopCloser(bytes.NewReader(b)), Request: r}, nil
			})
			s := Collect(context.Background(), tr, func() time.Time { return now })
			if calls != 6 || s.Regions[1].Complete != (failure == "none") {
				t.Fatal(calls, s)
			}
			b, e := json.Marshal(s)
			if e != nil {
				t.Fatal(e)
			}
			if _, e := Parse(b); e != nil {
				t.Fatal(e)
			}
			v, _ := Apply(s, now, now, now.Add(time.Hour), verdict.Go, "")
			want := verdict.Unknown
			if failure == "none" {
				want = verdict.Go
			}
			if v != want {
				t.Fatal(v)
			}
		})
	}
}

func TestSnapshotBounds(t *testing.T) {
	now := stamp("2026-10-24T12:00:00Z")
	for n, change := range []func(*Snapshot){func(s *Snapshot) { s.Version = 2 }, func(s *Snapshot) { s.Regions = s.Regions[:5] }, func(s *Snapshot) { s.Regions[0].CheckedAt = now.Add(time.Second) }, func(s *Snapshot) { s.Regions[0].Items = make([]Item, 101) }} {
		s := empty(now)
		change(s)
		b, _ := json.Marshal(s)
		if _, e := Parse(b); e == nil {
			t.Fatal(fmt.Sprint(n))
		}
	}
}

func TestWarningExplanationBoundFailsClosedWithoutTruncatingWords(t *testing.T) {
	now := stamp("2026-10-24T12:00:00Z")
	s := empty(now)
	i := synthetic("Yellow", "1200 Sat 24 Oct", "2100 Sat 24 Oct")
	i.Description = strings.Replace(i.Description, ": Moray", ": "+strings.Repeat("Moray, ", 1000)+"Moray", 1)
	for n := 0; n < 20; n++ {
		next := i
		next.Link += fmt.Sprintf("&duplicate=%d", n)
		s.Regions[2].Items = append(s.Regions[2].Items, next)
	}
	v, why := Apply(s, now, now, now.Add(time.Hour), verdict.Go, "")
	if v != verdict.Unknown || len(why) > MaxExplanationBytes || !strings.Contains(why, OfficialURL) || strings.Contains(why, i.Title) {
		t.Fatal(v, len(why), why)
	}
}
