package walks

import (
	"encoding/json"
	"fmt"
	"math"
	"strings"
)

const (
	HillLicense       = "https://creativecommons.org/licenses/by/4.0/"
	HillSourceURL     = "https://www.hill-bagging.co.uk/dobih/"
	HillInferenceNote = "Hill matches inferred from the walk name, ascent and a conservative radius around its start; route geometry is unavailable and proximity does not prove a summit is reached. Follow the Walkhighlands route."
)

var HillClasses = []string{"Munro", "Munro Top", "Corbett", "Graham", "Donald"}

type HillSource struct {
	Name          string `json:"name"`
	Version       string `json:"version"`
	Attribution   string `json:"attribution"`
	URL           string `json:"url"`
	License       string `json:"license"`
	Modifications string `json:"modifications"`
}

type Hill struct {
	Number  int      `json:"number"`
	Name    string   `json:"name"`
	Classes []string `json:"classes"`
	Lat     float64  `json:"lat"`
	Lon     float64  `json:"lon"`
	HeightM float64  `json:"height_m"`
}

// HillMatch repeats a multi-class hill once per class, preserving the API's
// singular class field without discarding overlapping classifications.
type HillMatch struct {
	Name    string  `json:"name"`
	Class   string  `json:"class"`
	HeightM float64 `json:"height_m"`
}

type HillDataset struct {
	SchemaVersion int        `json:"schema_version"`
	Source        HillSource `json:"source"`
	Hills         []Hill     `json:"hills"`
	Version       string     `json:"-"`
}

func ParseHills(body []byte, version string) (*HillDataset, error) {
	var d HillDataset
	if err := json.Unmarshal(body, &d); err != nil {
		return nil, err
	}
	if d.SchemaVersion != 1 || len(d.Hills) == 0 || len(d.Hills) > 30000 ||
		d.Source.Name != "DoBIH" || d.Source.Version == "" || d.Source.Attribution != "The Database of British and Irish Hills v"+d.Source.Version ||
		d.Source.License != HillLicense || d.Source.URL != HillSourceURL || d.Source.Modifications == "" {
		return nil, fmt.Errorf("hills: invalid schema or missing source provenance")
	}
	seen := map[int]bool{}
	for _, h := range d.Hills {
		if seen[h.Number] || h.Number <= 0 || Normalize(h.Name) == "" || len(h.Classes) == 0 ||
			!(h.Lat >= 54.5 && h.Lat <= 61 && h.Lon >= -9 && h.Lon <= -0.5 && h.HeightM > 0 && h.HeightM < 2000) {
			return nil, fmt.Errorf("hills: invalid or duplicate hill")
		}
		seen[h.Number] = true
		classes := map[string]bool{}
		for _, c := range h.Classes {
			if !ValidHillClass(c) || classes[c] {
				return nil, fmt.Errorf("hills: invalid class")
			}
			classes[c] = true
		}
	}
	d.Version = version
	return &d, nil
}

func ValidHillClass(c string) bool {
	for _, allowed := range HillClasses {
		if c == allowed {
			return true
		}
	}
	return false
}

func (d *HillDataset) nameIndex() map[string][]Hill {
	index := map[string][]Hill{}
	for _, h := range d.Hills {
		seen := map[string]bool{}
		for _, alias := range strings.FieldsFunc(h.Name, func(r rune) bool { return r == '[' || r == ']' }) {
			key := Normalize(alias)
			if key != "" && !seen[key] {
				index[key] = append(index[key], h)
				seen[key] = true
			}
		}
	}
	return index
}

func (d *HillDataset) Match(w *Walk) ([]HillMatch, bool) {
	return matchHills(w, d.nameIndex())
}

func matchHills(w *Walk, index map[string][]Hill) ([]HillMatch, bool) {
	var matches []Hill
	if w.SummitNames != nil {
		// Explicit evidence suppresses inference, even if every name is unknown
		// or ambiguous. Distance disambiguates identically named distant hills.
		seen := map[int]bool{}
		for _, name := range w.SummitNames {
			var candidates []Hill
			for _, h := range index[Normalize(name)] {
				if w.DistanceKm(h.Lat, h.Lon) <= math.Min(25, w.LengthKm/2) {
					candidates = append(candidates, h)
				}
			}
			if len(candidates) == 1 && !seen[candidates[0].Number] {
				matches = append(matches, candidates[0])
				seen[candidates[0].Number] = true
			}
		}
	} else {
		radius := math.Min(5, math.Min(w.LengthKm/2, float64(w.AscentM)/200))
		candidates := map[int]Hill{}
		words := strings.Fields(Normalize(w.Name))
		for start := range words {
			for end := start + 1; end <= len(words); end++ {
				for _, h := range index[strings.Join(words[start:end], " ")] {
					candidates[h.Number] = h
				}
			}
		}
		for _, h := range candidates {
			// Without start elevation or route geometry, require substantial ascent
			// relative to summit height and a whole hill name in the walk title.
			if w.AscentM > 0 && float64(w.AscentM) >= 0.8*h.HeightM &&
				w.DistanceKm(h.Lat, h.Lon) <= radius {
				matches = append(matches, h)
			}
		}
		if len(matches) != 1 {
			return nil, false
		}
	}
	var out []HillMatch
	for _, h := range matches {
		for _, c := range h.Classes {
			out = append(out, HillMatch{h.Name, c, h.HeightM})
		}
	}
	return out, len(out) > 0 && w.SummitNames == nil
}

// WithHills leaves the shared snapshot immutable. No hill source means the
// old dataset, including its version and answer shape, is used unchanged.
func (d *Dataset) WithHills(h *HillDataset) *Dataset {
	if h == nil {
		return d
	}
	out := *d
	out.Walks = append([]Walk(nil), d.Walks...)
	out.HillSource = &h.Source
	out.Version = d.Version + "|hills:" + h.Version
	index := h.nameIndex()
	for i := range out.Walks {
		out.Walks[i].Hills, out.Walks[i].HillsInferred = matchHills(&out.Walks[i], index)
	}
	return &out
}
