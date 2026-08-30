package main

import (
	"encoding/json"
	"fmt"
	"net/http"
	"sort"
	"strings"
	"sync"
	"time"
)

const (
	verdictPass    = "pass"
	verdictFail    = "fail"
	verdictRunning = "running"
	verdictVacuous = "vacuous"
)

type scenarioVerdict struct {
	ID      string `json:"id"`
	Verdict string `json:"verdict"`
	Detail  string `json:"detail"`
	MS      int64  `json:"ms"`
}

// The previous COMPLETED cycle's outcome, kept so the promotion gate can
// distinguish one red cycle (post-roll settling: dead-brick redials, inventory
// adoption in progress) from a persistent red. The gate's failureExpression
// requires two consecutive fails; a lone red keeps the poll waiting for the
// next cycle instead of failing the promotion.
type previousVerdict struct {
	Verdict   string            `json:"verdict"`
	Scenarios []scenarioVerdict `json:"scenarios"`
}

type suiteVerdict struct {
	ChartVersion string            `json:"chart_version"`
	Verdict      string            `json:"verdict"`
	StartedAt    time.Time         `json:"started_at"`
	FinishedAt   *time.Time        `json:"finished_at"`
	Scenarios    []scenarioVerdict `json:"scenarios"`
	Previous     *previousVerdict  `json:"previous,omitempty"`
}

type verdictStore struct {
	mu      sync.RWMutex
	current suiteVerdict
}

func newVerdictStore(chartVersion string) *verdictStore {
	return &verdictStore{current: suiteVerdict{
		ChartVersion: chartVersion,
		Verdict:      verdictRunning,
		StartedAt:    time.Now().UTC(),
		Scenarios:    []scenarioVerdict{},
	}}
}

func (s *verdictStore) start(started time.Time) {
	s.mu.Lock()
	defer s.mu.Unlock()

	var previous *previousVerdict
	if s.current.Verdict != verdictRunning || len(s.current.Scenarios) > 0 {
		previous = &previousVerdict{
			Verdict:   s.current.Verdict,
			Scenarios: append([]scenarioVerdict(nil), s.current.Scenarios...),
		}
	}
	s.current = suiteVerdict{
		ChartVersion: s.current.ChartVersion,
		Verdict:      verdictRunning,
		StartedAt:    started.UTC(),
		Scenarios:    []scenarioVerdict{},
		Previous:     previous,
	}
}

func (s *verdictStore) finish(started time.Time, scenarios []scenarioVerdict) {
	finished := time.Now().UTC()
	verdict := verdictPass
	for _, scenario := range scenarios {
		if scenario.Verdict != verdictPass {
			verdict = verdictFail
			break
		}
	}

	s.mu.Lock()
	defer s.mu.Unlock()
	// Carry Previous through: dropping it here meant a finished report never
	// exposed the prior cycle, so the gate could not see consecutive outcomes.
	s.current = suiteVerdict{
		ChartVersion: s.current.ChartVersion,
		Verdict:      verdict,
		StartedAt:    started.UTC(),
		FinishedAt:   &finished,
		Scenarios:    append([]scenarioVerdict(nil), scenarios...),
		Previous:     s.current.Previous,
	}
}

func (s *verdictStore) snapshot() suiteVerdict {
	s.mu.RLock()
	defer s.mu.RUnlock()

	copy := s.current
	copy.Scenarios = append([]scenarioVerdict(nil), s.current.Scenarios...)
	if s.current.Previous != nil {
		copy.Previous = &previousVerdict{
			Verdict:   s.current.Previous.Verdict,
			Scenarios: append([]scenarioVerdict(nil), s.current.Previous.Scenarios...),
		}
	}
	return copy
}

func (s *verdictStore) verdictHandler(w http.ResponseWriter, _ *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	if err := json.NewEncoder(w).Encode(s.snapshot()); err != nil {
		http.Error(w, "could not encode verdict", http.StatusInternalServerError)
	}
}

func (s *verdictStore) metricsHandler(w http.ResponseWriter, _ *http.Request) {
	verdict := s.snapshot()
	value := 0.0
	if verdict.Verdict == verdictPass {
		value = 1.0
	}
	scenarios := verdict.Scenarios
	if verdict.Verdict == verdictRunning && verdict.Previous != nil {
		scenarios = verdict.Previous.Scenarios
	}
	sort.Slice(scenarios, func(i, j int) bool { return scenarios[i].ID < scenarios[j].ID })

	w.Header().Set("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
	fmt.Fprintf(w, "# TYPE embervm_conformance_suite_verdict gauge\n")
	fmt.Fprintf(w, "embervm_conformance_suite_verdict{chart_version=\"%s\"} %.1f\n", prometheusEscape(verdict.ChartVersion), value)
	fmt.Fprintf(w, "# TYPE embervm_conformance_scenario_ms gauge\n")
	for _, scenario := range scenarios {
		fmt.Fprintf(w, "embervm_conformance_scenario_ms{id=\"%s\"} %d\n", prometheusEscape(scenario.ID), scenario.MS)
	}
}

func prometheusEscape(value string) string {
	value = strings.ReplaceAll(value, `\`, `\\`)
	value = strings.ReplaceAll(value, "\n", `\n`)
	return strings.ReplaceAll(value, "\"", `\"`)
}
