package main

import (
	"encoding/json"
	"testing"
	"time"
)

func TestVerdictJSONMarshalling(t *testing.T) {
	started := time.Date(2026, 8, 23, 12, 0, 0, 0, time.UTC)
	finished := started.Add(time.Second)
	verdict := suiteVerdict{
		ChartVersion: "1.2.3",
		Verdict:      verdictPass,
		StartedAt:    started,
		FinishedAt:   &finished,
		Scenarios:    []scenarioVerdict{{ID: "S1", Verdict: verdictPass, Detail: "ok", MS: 1234}},
	}
	raw, err := json.Marshal(verdict)
	if err != nil {
		t.Fatal(err)
	}
	want := `{"chart_version":"1.2.3","verdict":"pass","started_at":"2026-08-23T12:00:00Z","finished_at":"2026-08-23T12:00:01Z","scenarios":[{"id":"S1","verdict":"pass","detail":"ok","ms":1234}]}`
	if string(raw) != want {
		t.Fatalf("JSON = %s, want %s", raw, want)
	}
}

func TestRunningVerdictHasNullFinishedAt(t *testing.T) {
	raw, err := json.Marshal(suiteVerdict{ChartVersion: "x", Verdict: verdictRunning, StartedAt: time.Unix(0, 0).UTC(), Scenarios: []scenarioVerdict{}})
	if err != nil {
		t.Fatal(err)
	}
	if string(raw) != `{"chart_version":"x","verdict":"running","started_at":"1970-01-01T00:00:00Z","finished_at":null,"scenarios":[]}` {
		t.Fatalf("unexpected JSON: %s", raw)
	}
}
