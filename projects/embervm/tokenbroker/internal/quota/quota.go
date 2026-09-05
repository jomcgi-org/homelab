// Package quota stores the latest provider quota observation in memory and
// derives its read-time state.
package quota

import (
	"encoding/json"
	"math"
	"sync"
	"time"
)

// Window is one provider quota window in the shared sidecar/broker contract.
type Window struct {
	Name          string  `json:"name"`
	UsedPercent   float64 `json:"used_percent"`
	WindowMinutes int     `json:"window_minutes,omitempty"`
	ResetsAt      string  `json:"resets_at,omitempty"`
}

// Observation is the JSON contract accepted from an egress proxy.
type Observation struct {
	Provider    string   `json:"provider"`
	ObservedAt  string   `json:"observed_at"`
	Status      string   `json:"status"`
	ReachedType string   `json:"reached_type"`
	Windows     []Window `json:"windows"`
}

// ViewWindow adds read-time expiry state to an observed window.
type ViewWindow struct {
	Name          string  `json:"name"`
	UsedPercent   float64 `json:"used_percent"`
	WindowMinutes int     `json:"window_minutes,omitempty"`
	ResetsAt      string  `json:"resets_at,omitempty"`
	Expired       bool    `json:"expired"`
}

// View is the latest observation plus state derived when it is read.
type View struct {
	Provider    string
	Observed    bool
	ObservedAt  string
	Status      string
	ReachedType string
	Windows     []ViewWindow
	ReceivedAt  time.Time
	AgeSeconds  float64
	Exhausted   bool
}

// MarshalJSON keeps the unobserved shape deliberately small while preserving
// the shared observation fields at the top level for observed providers.
func (v View) MarshalJSON() ([]byte, error) {
	if !v.Observed {
		return json.Marshal(struct {
			Provider string `json:"provider"`
			Observed bool   `json:"observed"`
		}{Provider: v.Provider, Observed: false})
	}
	return json.Marshal(struct {
		Provider    string       `json:"provider"`
		ObservedAt  string       `json:"observed_at"`
		Status      string       `json:"status"`
		ReachedType string       `json:"reached_type"`
		Windows     []ViewWindow `json:"windows"`
		Observed    bool         `json:"observed"`
		ReceivedAt  time.Time    `json:"received_at"`
		AgeSeconds  float64      `json:"age_seconds"`
		Exhausted   bool         `json:"exhausted"`
	}{
		Provider: v.Provider, ObservedAt: v.ObservedAt, Status: v.Status,
		ReachedType: v.ReachedType, Windows: v.Windows, Observed: true,
		ReceivedAt: v.ReceivedAt, AgeSeconds: v.AgeSeconds, Exhausted: v.Exhausted,
	})
}

type storedObservation struct {
	observation Observation
	receivedAt  time.Time
}

// Store keeps only the latest observation for each provider. Its contents are
// process-local and are lost when tokenbroker restarts.
type Store struct {
	mu           sync.RWMutex
	observations map[string]storedObservation
}

func NewStore() *Store {
	return &Store{observations: make(map[string]storedObservation)}
}

func (s *Store) Put(provider string, obs Observation, receivedAt time.Time) {
	obs.Provider = provider
	obs.Windows = append([]Window(nil), obs.Windows...)
	s.mu.Lock()
	if s.observations == nil {
		s.observations = make(map[string]storedObservation)
	}
	s.observations[provider] = storedObservation{observation: obs, receivedAt: receivedAt.UTC()}
	s.mu.Unlock()
}

func (s *Store) Get(provider string) View {
	s.mu.RLock()
	stored, ok := s.observations[provider]
	s.mu.RUnlock()
	if !ok {
		return View{Provider: provider}
	}
	return makeView(stored, time.Now().UTC())
}

func makeView(stored storedObservation, now time.Time) View {
	obs := stored.observation
	view := View{
		Provider: obs.Provider, Observed: true, ObservedAt: obs.ObservedAt,
		Status: obs.Status, ReachedType: obs.ReachedType,
		ReceivedAt: stored.receivedAt, Exhausted: obs.Status == "rejected",
		Windows: make([]ViewWindow, 0, len(obs.Windows)),
	}
	if observedAt, err := time.Parse(time.RFC3339, obs.ObservedAt); err == nil {
		view.AgeSeconds = now.Sub(observedAt).Seconds()
	}
	for _, window := range obs.Windows {
		expired := false
		if reset, err := time.Parse(time.RFC3339, window.ResetsAt); err == nil {
			expired = !reset.After(now)
		}
		view.Windows = append(view.Windows, ViewWindow{
			Name: window.Name, UsedPercent: window.UsedPercent,
			WindowMinutes: window.WindowMinutes, ResetsAt: window.ResetsAt,
			Expired: expired,
		})
		if !expired && window.UsedPercent >= 100 {
			view.Exhausted = true
		}
	}
	return view
}

// ValidObservation checks the wire-level invariants needed by the read model.
func ValidObservation(obs Observation) bool {
	switch obs.Status {
	case "allowed", "warning", "rejected":
	case "unknown":
		if len(obs.Windows) == 0 {
			return false
		}
	default:
		return false
	}
	observedAt, err := time.Parse(time.RFC3339, obs.ObservedAt)
	if err != nil {
		return false
	}
	if _, offset := observedAt.Zone(); offset != 0 {
		return false
	}
	for _, window := range obs.Windows {
		if window.Name == "" || math.IsNaN(window.UsedPercent) || math.IsInf(window.UsedPercent, 0) || window.WindowMinutes < 0 {
			return false
		}
		if window.ResetsAt != "" {
			reset, err := time.Parse(time.RFC3339, window.ResetsAt)
			if err != nil {
				return false
			}
			if _, offset := reset.Zone(); offset != 0 {
				return false
			}
		}
	}
	return true
}
