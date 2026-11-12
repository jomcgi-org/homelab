package walks

import (
	"encoding/json"
	"reflect"
	"testing"
)

func TestHillNameKeys(t *testing.T) {
	for _, tc := range []struct {
		name string
		keys []string
	}{
		{"Ben Nevis", []string{"ben nevis"}},
		{"Ben Nevis [Beinn Nibheis]", []string{"ben nevis beinn nibheis", "ben nevis", "beinn nibheis"}},
		{"Bla Bheinn [Blaven]", []string{"bla bheinn blaven", "bla bheinn", "blaven"}},
		{"Ben Macdui [Beinn Macduibh]", []string{"ben macdui beinn macduibh", "ben macdui", "beinn macduibh"}},
		{"Ben Nevis []", []string{"ben nevis"}},
	} {
		if got := HillNameKeys(tc.name); !reflect.DeepEqual(got, tc.keys) {
			t.Fatalf("%s: got %v, want %v", tc.name, got, tc.keys)
		}
		d := hillFixture()
		d.Hills = d.Hills[:1]
		d.Hills[0].Name = tc.name
		for _, key := range tc.keys {
			w := &Walk{SummitNames: []string{key}, Lat: 56.79685, Lon: -5.00351, LengthKm: 17}
			if hits, inferred := d.Match(w); len(hits) != 1 || inferred {
				t.Fatalf("%s: %+v, inferred=%v", key, hits, inferred)
			}
		}
	}
}

func hillFixture() *HillDataset {
	return &HillDataset{SchemaVersion: 1, Source: HillSource{Name: "DoBIH", Version: "18.6", Attribution: "The Database of British and Irish Hills v18.6", URL: HillSourceURL, License: HillLicense, Modifications: "Filtered to Scotland; joined to walks."}, Hills: []Hill{
		{Number: 1, Name: "Ben Nevis", Classes: []string{"Munro"}, Lat: 56.79685, Lon: -5.00351, HeightM: 1345},
		{Number: 2, Name: "Unrelated", Classes: []string{"Corbett"}, Lat: 56.8, Lon: -5.04, HeightM: 800},
	}}
}

func TestHillJoins(t *testing.T) {
	d := hillFixture()
	base := Walk{Name: "Ben Nevis via the mountain track", Lat: 56.8108, Lon: -5.076, LengthKm: 17, AscentM: 1350, DurationH: 9, cosLat: 0.5474}
	for _, tc := range []struct {
		name     string
		names    []string
		ascent   int
		want     int
		inferred bool
	}{
		{"inferred", nil, 1350, 1, true},
		{"explicit", []string{"Ben Nevis", "Ben Nevis"}, 1350, 1, false},
		{"empty list", []string{}, 1350, 0, false},
		{"unresolved explicit", []string{"Unknown"}, 1350, 0, false},
		{"insufficient ascent", nil, 100, 0, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			w := base
			w.SummitNames = tc.names
			w.AscentM = tc.ascent
			hits, inferred := d.Match(&w)
			if len(hits) != tc.want || inferred != tc.inferred {
				t.Fatalf("%+v inferred=%v", hits, inferred)
			}
			if len(hits) > 0 && (hits[0].Name != "Ben Nevis" || hits[0].HeightM != 1345 || hits[0].Class != "Munro") {
				t.Fatal(hits)
			}
		})
	}
	// A nearby duplicate makes a name ambiguous, even with explicit evidence.
	duplicate := d.Hills[0]
	duplicate.Number = 3
	d.Hills = append(d.Hills, duplicate)
	for _, names := range [][]string{nil, {"Ben Nevis"}} {
		w := base
		w.SummitNames = names
		if hits, inferred := d.Match(&w); len(hits) != 0 || inferred {
			t.Fatalf("ambiguous %+v %v", hits, inferred)
		}
	}
	d.Hills[2].Lat = 59
	base.SummitNames = []string{"Ben Nevis"}
	if hits, _ := d.Match(&base); len(hits) != 1 {
		t.Fatal(hits)
	}
	// Multiple classes survive one explicit summit match.
	d.Hills[0].Classes = []string{"Graham", "Donald"}
	if hits, _ := d.Match(&base); len(hits) != 2 {
		t.Fatal(hits)
	}
}

func TestHillSnapshotValidationAndDisabledCompatibility(t *testing.T) {
	d := hillFixture()
	b, _ := json.Marshal(d)
	if _, err := ParseHills(b, "h1"); err != nil {
		t.Fatal(err)
	}
	for _, mutate := range []func(*HillDataset){
		func(d *HillDataset) { d.Source.Version = "" }, func(d *HillDataset) { d.Source.Modifications = "" },
		func(d *HillDataset) { d.SchemaVersion = 2 }, func(d *HillDataset) { d.Hills[0].Lat = 51 },
		func(d *HillDataset) { d.Hills[0].Classes = []string{"Marilyn"} }, func(d *HillDataset) { d.Hills[1].Number = 1 },
	} {
		bad := hillFixture()
		mutate(bad)
		b, _ := json.Marshal(bad)
		if _, err := ParseHills(b, "bad"); err == nil {
			t.Fatal("accepted invalid snapshot")
		}
	}
	walks := &Dataset{Version: "old", Walks: []Walk{{Name: "No hills"}}}
	if walks.WithHills(nil) != walks {
		t.Fatal("disabled path changed")
	}
	joined := walks.WithHills(d)
	if joined == walks || walks.HillSource != nil || joined.Version == walks.Version {
		t.Fatal("shared snapshot mutated")
	}
}
