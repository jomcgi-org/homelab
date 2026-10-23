package connector

import (
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/walks"
	"testing"
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
