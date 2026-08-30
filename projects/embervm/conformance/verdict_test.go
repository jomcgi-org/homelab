package main

import (
	"encoding/json"
	"strings"
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

func TestPreviousVerdictSurvivesFinishAndCarriesItsOutcome(t *testing.T) {
	store := newVerdictStore("0.1.0")
	t0 := time.Now().UTC()

	store.start(t0)
	store.finish(t0, []scenarioVerdict{{ID: "S4", Verdict: verdictFail, Detail: "first cycle residue"}})

	store.start(t0.Add(time.Minute))
	store.finish(t0.Add(time.Minute), []scenarioVerdict{{ID: "S4", Verdict: verdictFail, Detail: "second cycle"}})

	got := store.snapshot()
	if got.Verdict != verdictFail {
		t.Fatalf("current verdict = %q, want fail", got.Verdict)
	}
	if got.Previous == nil {
		t.Fatal("finished report dropped Previous; the gate cannot see consecutive outcomes")
	}
	if got.Previous.Verdict != verdictFail {
		t.Fatalf("previous verdict = %q, want fail", got.Previous.Verdict)
	}

	payload, err := json.Marshal(got)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(payload), `"previous":{"verdict":"fail"`) {
		t.Fatalf("previous verdict missing from JSON the gate reads: %s", payload)
	}
}

func TestFirstCycleOfAFreshRunnerHasNoPrevious(t *testing.T) {
	store := newVerdictStore("0.1.0")
	t0 := time.Now().UTC()

	store.start(t0)
	store.finish(t0, []scenarioVerdict{{ID: "S4", Verdict: verdictFail, Detail: "post-roll residue"}})

	if got := store.snapshot(); got.Previous != nil {
		t.Fatalf("fresh runner reported a previous cycle: %#v; a chart roll must never fast-fail on its first red", got.Previous)
	}
}
