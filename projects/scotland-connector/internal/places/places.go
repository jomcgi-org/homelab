// Package places resolves free-text Scottish place names to coordinates.
//
// The stars grid sites carry no names (generate_grid_v2.py writes name: null),
// so the connector ships its own gazetteer. seed.json is a small curated list
// of towns, villages and dark-sky places with Gaelic names where they are in
// common use; coordinates are approximate centroids to two decimal places
// (about 1 km), which is finer than any radius query needs. A full gazetteer
// built from an open dataset replaces it (#7247); callers that already know the
// coordinates should pass them directly.
package places

import (
	_ "embed"
	"encoding/json"
	"fmt"
	"sort"
	"strconv"
	"strings"
	"unicode"
)

//go:embed seed.json
var seedJSON []byte

// Coverage is the bounding box the connector answers for: mainland Scotland,
// the Hebrides, Orkney and Shetland.
var Coverage = struct{ MinLat, MaxLat, MinLon, MaxLon float64 }{54.5, 61.0, -9.0, -0.5}

// InCoverage reports whether a coordinate is inside Coverage.
func InCoverage(lat, lon float64) bool {
	return lat >= Coverage.MinLat && lat <= Coverage.MaxLat && lon >= Coverage.MinLon && lon <= Coverage.MaxLon
}

// Place is one gazetteer entry.
type Place struct {
	Name     string   `json:"name"`
	AltNames []string `json:"alt"`
	Type     string   `json:"type"`
	Lat      float64  `json:"lat"`
	Lon      float64  `json:"lon"`
}

// Match is a resolved candidate with a confidence in (0, 1].
type Match struct {
	Place
	Confidence  float64
	MatchedName string
}

// Gazetteer is an immutable name index.
type Gazetteer struct {
	places []Place
	// keys[i] holds the normalized names of places[i].
	keys [][]string
}

// Seed returns the gazetteer built from the embedded seed list.
func Seed() (*Gazetteer, error) {
	var ps []Place
	if err := json.Unmarshal(seedJSON, &ps); err != nil {
		return nil, fmt.Errorf("places: decode seed: %w", err)
	}
	return New(ps)
}

// New indexes places, rejecting entries outside Coverage.
func New(ps []Place) (*Gazetteer, error) {
	g := &Gazetteer{places: ps, keys: make([][]string, len(ps))}
	for i, p := range ps {
		if p.Name == "" || p.Type == "" {
			return nil, fmt.Errorf("places: entry %d needs a name and type", i)
		}
		if !InCoverage(p.Lat, p.Lon) {
			return nil, fmt.Errorf("places: %s (%.2f, %.2f) is outside coverage", p.Name, p.Lat, p.Lon)
		}
		for _, n := range append([]string{p.Name}, p.AltNames...) {
			g.keys[i] = append(g.keys[i], Normalize(n))
		}
	}
	return g, nil
}

// Len is the number of entries.
func (g *Gazetteer) Len() int { return len(g.places) }

// Resolve returns up to limit candidates for q, best first. A "lat, lon"
// query resolves to itself with type "coordinates".
func (g *Gazetteer) Resolve(q string, limit int) []Match {
	if lat, lon, ok := ParseCoordinates(q); ok {
		return []Match{{Place: Place{Name: q, Type: "coordinates", Lat: lat, Lon: lon}, Confidence: 1, MatchedName: q}}
	}
	nq := Normalize(q)
	if nq == "" {
		return nil
	}
	var out []Match
	for i, p := range g.places {
		best, matched := 0.0, ""
		for j, k := range g.keys[i] {
			score := 0.0
			switch {
			case k == nq:
				score = 1
			case strings.HasPrefix(k, nq) && len(nq) >= 3:
				score = 0.8
			case strings.Contains(k, nq) && len(nq) >= 4:
				score = 0.6
			}
			if score > best {
				best = score
				matched = append([]string{p.Name}, p.AltNames...)[j]
			}
		}
		if best > 0 {
			out = append(out, Match{Place: p, Confidence: best, MatchedName: matched})
		}
	}
	sort.SliceStable(out, func(i, j int) bool {
		if out[i].Confidence != out[j].Confidence {
			return out[i].Confidence > out[j].Confidence
		}
		return out[i].Name < out[j].Name
	})
	if len(out) > limit {
		out = out[:limit]
	}
	return out
}

// ParseCoordinates accepts "lat, lon" or "lat lon" in decimal degrees.
func ParseCoordinates(q string) (lat, lon float64, ok bool) {
	f := strings.FieldsFunc(q, func(r rune) bool { return r == ',' || unicode.IsSpace(r) })
	if len(f) != 2 {
		return 0, 0, false
	}
	lat, err1 := strconv.ParseFloat(f[0], 64)
	lon, err2 := strconv.ParseFloat(f[1], 64)
	if err1 != nil || err2 != nil || lat < -90 || lat > 90 || lon < -180 || lon > 180 {
		return 0, 0, false
	}
	return lat, lon, true
}

var folds = strings.NewReplacer(
	"à", "a", "á", "a", "â", "a", "ä", "a", "è", "e", "é", "e", "ê", "e", "ë", "e",
	"ì", "i", "í", "i", "î", "i", "ï", "i", "ò", "o", "ó", "o", "ô", "o", "ö", "o",
	"ù", "u", "ú", "u", "û", "u", "ü", "u", "ñ", "n", "ç", "c",
	"’", "'", "‘", "'",
)

// Normalize lowercases, folds Gaelic and other accents, expands "st" to
// "saint", drops punctuation and collapses whitespace, so "St. Andrews",
// "saint andrews" and "ST ANDREWS" share a key.
func Normalize(s string) string {
	s = folds.Replace(strings.ToLower(s))
	var b strings.Builder
	for _, r := range s {
		switch {
		case unicode.IsLetter(r) || unicode.IsDigit(r):
			b.WriteRune(r)
		default:
			b.WriteByte(' ')
		}
	}
	words := strings.Fields(b.String())
	for i, w := range words {
		if w == "st" {
			words[i] = "saint"
		}
	}
	return strings.Join(words, " ")
}
