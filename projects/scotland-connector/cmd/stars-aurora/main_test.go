package main

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	"github.com/jomcgi/homelab/projects/scotland-connector/internal/aurora"
	"github.com/jomcgi/homelab/projects/scotland-connector/internal/fixture"
)

func TestOfflineProducer(t *testing.T) {
	in, err := fixture.AuroraInputs()
	if err != nil {
		t.Fatal(err)
	}
	b, err := json.Marshal(in)
	if err != nil {
		t.Fatal(err)
	}
	p := filepath.Join(t.TempDir(), "input.json")
	if err = os.WriteFile(p, b, 0o600); err != nil {
		t.Fatal(err)
	}
	var out bytes.Buffer
	if err = run(p, "", "", false, &out); err != nil {
		t.Fatal(err)
	}
	s, err := aurora.Parse(out.Bytes())
	if err != nil {
		t.Fatal(err)
	}
	if a := aurora.Assess(s, 58, -4, s.NOAA.ForecastAt, fixture.AuroraNow); a.Verdict != "go" {
		t.Fatal(a)
	}
	if err = run(p, "", "", true, &out); err == nil {
		t.Fatal("conflicting modes")
	}
	if err = run("", "", "", false, &out); err == nil {
		t.Fatal("missing input")
	}
}
