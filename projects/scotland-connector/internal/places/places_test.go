package places

import "testing"

func TestSeedLoadsInsideCoverage(t *testing.T) {
	g, err := Seed()
	if err != nil {
		t.Fatal(err)
	}
	if g.Len() < 40 {
		t.Fatalf("seed has %d places", g.Len())
	}
}

func TestResolve(t *testing.T) {
	g, err := Seed()
	if err != nil {
		t.Fatal(err)
	}
	cases := map[string]string{
		"Aviemore":      "Aviemore",
		"  AVIEMORE ":   "Aviemore",
		"St. Andrews":   "St Andrews",
		"saint andrews": "St Andrews",
		"Inbhir Nis":    "Inverness",
		"An t-Òban":     "Oban",
		"an t oban":     "Oban",
		"isle of coll":  "Arinagour",
		"galloway":      "Galloway Forest Park",
		"Kyle of":       "Kyle of Lochalsh",
		"Dùn Èideann":   "Edinburgh",
		"dun eideann":   "Edinburgh",
	}
	for q, want := range cases {
		got := g.Resolve(q, 5)
		if len(got) == 0 || got[0].Name != want {
			t.Errorf("Resolve(%q) = %+v, want %s first", q, got, want)
		}
	}
	if got := g.Resolve("Aviemore", 5); got[0].Confidence != 1 || got[0].Type != "town" {
		t.Errorf("exact match = %+v", got[0])
	}
	if got := g.Resolve("Gotham", 5); len(got) != 0 {
		t.Errorf("unknown place matched %+v", got)
	}
	if got := g.Resolve("ab", 5); len(got) != 0 {
		t.Errorf("two-letter prefix matched %+v", got)
	}
}

func TestResolveCoordinates(t *testing.T) {
	g, _ := Seed()
	got := g.Resolve("57.19, -3.83", 5)
	if len(got) != 1 || got[0].Type != "coordinates" || got[0].Lat != 57.19 || got[0].Lon != -3.83 {
		t.Fatalf("coordinates = %+v", got)
	}
	for _, q := range []string{"57.19", "91, 0", "a, b", "1, 2, 3"} {
		if _, _, ok := ParseCoordinates(q); ok {
			t.Errorf("ParseCoordinates(%q) accepted", q)
		}
	}
}

func TestNewRejectsOutOfCoverage(t *testing.T) {
	if _, err := New([]Place{{Name: "London", Type: "city", Lat: 51.5, Lon: -0.12}}); err == nil {
		t.Fatal("accepted London")
	}
}
