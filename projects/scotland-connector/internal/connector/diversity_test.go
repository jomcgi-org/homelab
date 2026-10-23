package connector

import (
	"testing"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/walks"
)

// Synthetic candidates isolate the conservative tie policy from weather math.
func TestWalkDiversity(t *testing.T) {
	c := func(id string, rank, good int, dist, length float64, ascent int, lat float64) walkCandidate {
		return walkCandidate{w: &walks.Walk{ID: id, LengthKm: length, AscentM: ascent, Lat: lat, Lon: -4}, rank: rank, good: good, dist: dist}
	}
	a := c("a", 0, 8, 1, 5, 100, 57)
	b := c("b", 0, 8, 2, 5, 100, 57)
	varied := c("varied", 0, 8, 10, 15, 800, 57.1)
	far := c("far", 0, 8, 25, 20, 1000, 57.2)
	worse := c("worse", 1, 8, 1, 20, 1000, 57.2)
	shorter := c("shorter", 0, 7, 1, 20, 1000, 57.2)
	for _, tc := range []struct {
		name  string
		in    []walkCandidate
		limit int
		want  []string
	}{
		{"variety", []walkCandidate{b, varied, a}, 2, []string{"a", "varied"}},
		{"distance cap", []walkCandidate{far, b, a}, 2, []string{"a", "b"}},
		{"suitability", []walkCandidate{worse, shorter, b, a}, 2, []string{"a", "b"}},
		{"ties", []walkCandidate{b, a}, 4, []string{"a", "b"}},
		{"one", []walkCandidate{varied, a}, 1, []string{"a"}},
		{"empty", nil, 3, nil},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got := diverseWalks(tc.in, tc.limit)
			if len(got) != len(tc.want) {
				t.Fatalf("%+v", got)
			}
			for i := range got {
				if got[i].w.ID != tc.want[i] {
					t.Fatalf("position %d: %s != %s", i, got[i].w.ID, tc.want[i])
				}
			}
		})
	}
}

func TestThirdWalkIncludesSubstantialClimbPeer(t *testing.T) {
	mk := func(id string, dist, length float64, ascent, rank, good int) walkCandidate {
		return walkCandidate{w: &walks.Walk{ID: id, Lat: 55.86 + dist/111, Lon: -4.25, LengthKm: length, AscentM: ascent}, dist: dist, rank: rank, good: good}
	}
	city := []walkCandidate{mk("urban", 0.3, 4, 50, 0, 8), mk("long", 6, 17, 380, 0, 8), mk("canal", 3, 16, 140, 0, 8)}
	hill := mk("hill", 45, 12, 990, 0, 8)
	for _, tc := range []struct {
		name  string
		extra walkCandidate
		want  string
	}{
		{"full radius peer", hill, "hill"},
		{"marginal cannot displace go", mk("marginal", 45, 12, 990, 1, 8), "canal"},
		{"shorter slot cannot displace peer", mk("shorter", 45, 12, 990, 0, 7), "canal"},
		{"rolling route is not hill proxy", mk("rolling", 45, 25, 600, 0, 8), "canal"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			input := append(append([]walkCandidate{}, city...), tc.extra)
			got := diverseWalks(input, 3)
			if got[0].w.ID != "urban" || got[2].w.ID != tc.want {
				t.Fatalf("%s %s %s", got[0].w.ID, got[1].w.ID, got[2].w.ID)
			}
		})
	}
	// A pair retains the existing proximity policy. A sparse set returns all.
	if got := diverseWalks([]walkCandidate{city[0], city[1], hill}, 2); got[1].w.ID != "long" {
		t.Fatal(got)
	}
	if got := diverseWalks([]walkCandidate{city[0], hill}, 3); len(got) != 2 {
		t.Fatal(got)
	}
	// Equal-distance hills are chosen by ID, independently of input order.
	other := hill
	other.w = &walks.Walk{ID: "aaa", Lat: hill.w.Lat, Lon: hill.w.Lon, LengthKm: 12, AscentM: 990}
	got := diverseWalks(append(append([]walkCandidate{}, city...), hill, other), 3)
	if got[2].w.ID != "aaa" {
		t.Fatal(got[2].w.ID)
	}
}
