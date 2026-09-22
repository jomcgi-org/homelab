// Package quota stores the latest provider quota observation and derives its
// read-time state.
package quota

import (
	"bytes"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"sync"
	"time"
)

const persistenceVersion = 1

// ErrPersistenceConflict asks Store to reload durable state and retry its
// compare-and-swap. Persistence implementations return it when their revision
// has changed since Load.
var ErrPersistenceConflict = errors.New("quota persistence conflict")

// Persistence is the durable compare-and-swap boundary used by Store. The
// opaque revision must identify the exact object version returned by Load.
type Persistence interface {
	Load() (data []byte, revision string, err error)
	CompareAndSwap(revision string, data []byte) (newRevision string, err error)
}

// Window is one provider quota window in the shared sidecar/broker contract.
type Window struct {
	Name          string  `json:"name"`
	UsedPercent   float64 `json:"used_percent"`
	WindowMinutes int     `json:"window_minutes,omitempty"`
	ResetsAt      string  `json:"resets_at,omitempty"`
}

// Observation is the JSON contract accepted from an egress proxy.
type Observation struct {
	Provider      string   `json:"provider"`
	ObservedAt    string   `json:"observed_at"`
	Status        string   `json:"status"`
	ReachedType   string   `json:"reached_type"`
	OverageStatus string   `json:"overage_status"`
	Windows       []Window `json:"windows"`
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
	Provider      string
	Grant         string
	Observed      bool
	ObservedAt    string
	Status        string
	ReachedType   string
	OverageStatus string
	Windows       []ViewWindow
	ReceivedAt    time.Time
	AgeSeconds    float64
	Exhausted     bool
}

// MarshalJSON keeps the unobserved shape deliberately small while preserving
// the shared observation fields at the top level for observed providers.
func (v View) MarshalJSON() ([]byte, error) {
	if !v.Observed {
		return json.Marshal(struct {
			Provider string `json:"provider"`
			Grant    string `json:"grant,omitempty"`
			Observed bool   `json:"observed"`
		}{Provider: v.Provider, Grant: v.Grant, Observed: false})
	}
	return json.Marshal(struct {
		Provider      string       `json:"provider"`
		Grant         string       `json:"grant,omitempty"`
		ObservedAt    string       `json:"observed_at"`
		Status        string       `json:"status"`
		ReachedType   string       `json:"reached_type"`
		OverageStatus string       `json:"overage_status"`
		Windows       []ViewWindow `json:"windows"`
		Observed      bool         `json:"observed"`
		ReceivedAt    time.Time    `json:"received_at"`
		AgeSeconds    float64      `json:"age_seconds"`
		Exhausted     bool         `json:"exhausted"`
	}{
		Provider: v.Provider, Grant: v.Grant, ObservedAt: v.ObservedAt, Status: v.Status,
		ReachedType: v.ReachedType, OverageStatus: v.OverageStatus, Windows: v.Windows, Observed: true,
		ReceivedAt: v.ReceivedAt, AgeSeconds: v.AgeSeconds, Exhausted: v.Exhausted,
	})
}

type storedObservation struct {
	Observation Observation `json:"observation"`
	ReceivedAt  time.Time   `json:"received_at"`
}

type persistedState struct {
	Version      int                          `json:"version"`
	Observations map[string]storedObservation `json:"providers"`
	Grants       map[string]storedObservation `json:"grants"`
}

// Store keeps only the latest observation for each provider, and separately
// for each grant that reported one. A persistent Store publishes a mutation to
// memory only after the durable compare-and-swap succeeds.
type Store struct {
	mu          sync.RWMutex
	state       persistedState
	persistence Persistence
	revision    string
	// readable is false after a detected storage failure. state is retained
	// privately so recovery cannot roll durable ordering backward.
	readable bool
}

func emptyState() persistedState {
	return persistedState{
		Version:      persistenceVersion,
		Observations: make(map[string]storedObservation),
		Grants:       make(map[string]storedObservation),
	}
}

// NewStore returns an in-memory store for isolated callers and tests.
func NewStore() *Store {
	return &Store{state: emptyState(), readable: true}
}

// NewPersistentStore restores the complete durable snapshot. On any load or
// validation failure it returns an empty store plus the error, so callers can
// keep serving explicit unobserved views and later recover through a fresh
// supported observation.
func NewPersistentStore(persistence Persistence) (*Store, error) {
	store := &Store{state: emptyState(), persistence: persistence}
	data, revision, err := persistence.Load()
	if err != nil {
		return store, fmt.Errorf("load quota state: %w", err)
	}
	store.revision = revision
	if len(data) == 0 {
		store.readable = true
		return store, nil
	}
	state, err := decodeState(data)
	if err != nil {
		return store, fmt.Errorf("decode quota state: %w", err)
	}
	store.state = state
	store.readable = true
	return store, nil
}

// PutGrant atomically records a grant observation and updates the class-level
// provider view with the same candidate. Each view independently keeps its
// newest record, while both decisions are committed in one durable snapshot.
func (s *Store) PutGrant(grant, provider string, obs Observation, receivedAt time.Time) error {
	return s.put(grant, provider, obs, receivedAt)
}

// Put records one provider-level observation.
func (s *Store) Put(provider string, obs Observation, receivedAt time.Time) error {
	return s.put("", provider, obs, receivedAt)
}

func (s *Store) put(grant, provider string, obs Observation, receivedAt time.Time) error {
	obs.Provider = provider
	obs.Windows = append([]Window(nil), obs.Windows...)
	candidate := storedObservation{Observation: obs, ReceivedAt: receivedAt.UTC()}
	if provider == "" || !ValidObservation(candidate.Observation, candidate.ReceivedAt) {
		return errors.New("invalid quota observation")
	}

	s.mu.Lock()
	defer s.mu.Unlock()
	for attempts := 0; attempts < 5; attempts++ {
		next := cloneState(s.state)
		changed := replaceIfNewer(next.Observations, provider, candidate)
		if grant != "" {
			changed = replaceIfNewer(next.Grants, grant, candidate) || changed
		}
		if !changed {
			return nil
		}
		if s.persistence == nil {
			s.state = next
			s.readable = true
			return nil
		}
		encoded, err := json.Marshal(next)
		if err != nil {
			s.markUnavailableLocked()
			return fmt.Errorf("encode quota state: %w", err)
		}
		revision, err := s.persistence.CompareAndSwap(s.revision, encoded)
		if err == nil {
			s.state = next
			s.revision = revision
			s.readable = true
			return nil
		}
		if !errors.Is(err, ErrPersistenceConflict) {
			s.markUnavailableLocked()
			return fmt.Errorf("persist quota state: %w", err)
		}
		data, revision, loadErr := s.persistence.Load()
		if loadErr != nil {
			s.markUnavailableLocked()
			return fmt.Errorf("reload quota state after conflict: %w", loadErr)
		}
		reloaded := emptyState()
		if len(data) != 0 {
			reloaded, loadErr = decodeState(data)
			if loadErr != nil {
				s.markUnavailableLocked()
				s.revision = revision
				return fmt.Errorf("decode quota state after conflict: %w", loadErr)
			}
		}
		s.state = reloaded
		s.revision = revision
		s.readable = true
	}
	s.markUnavailableLocked()
	return errors.New("persist quota state: compare-and-swap retries exhausted")
}

func (s *Store) markUnavailableLocked() {
	s.readable = false
}

func replaceIfNewer(records map[string]storedObservation, key string, candidate storedObservation) bool {
	current, exists := records[key]
	if exists && compareRecords(candidate, current) <= 0 {
		return false
	}
	records[key] = candidate
	return true
}

func compareRecords(left, right storedObservation) int {
	leftObserved, _ := time.Parse(time.RFC3339, left.Observation.ObservedAt)
	rightObserved, _ := time.Parse(time.RFC3339, right.Observation.ObservedAt)
	if leftObserved.Before(rightObserved) {
		return -1
	}
	if leftObserved.After(rightObserved) {
		return 1
	}
	if left.ReceivedAt.Before(right.ReceivedAt) {
		return -1
	}
	if left.ReceivedAt.After(right.ReceivedAt) {
		return 1
	}
	leftJSON, _ := json.Marshal(left)
	rightJSON, _ := json.Marshal(right)
	leftHash := sha256.Sum256(leftJSON)
	rightHash := sha256.Sum256(rightJSON)
	return bytes.Compare(leftHash[:], rightHash[:])
}

func cloneState(state persistedState) persistedState {
	cloned := emptyState()
	for provider, record := range state.Observations {
		cloned.Observations[provider] = cloneRecord(record)
	}
	for grant, record := range state.Grants {
		cloned.Grants[grant] = cloneRecord(record)
	}
	return cloned
}

func cloneRecord(record storedObservation) storedObservation {
	record.Observation.Windows = append([]Window(nil), record.Observation.Windows...)
	return record
}

func decodeState(data []byte) (persistedState, error) {
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	var state persistedState
	if err := decoder.Decode(&state); err != nil {
		return persistedState{}, err
	}
	if err := decoder.Decode(&struct{}{}); err != io.EOF {
		return persistedState{}, errors.New("trailing data after quota state")
	}
	if state.Version != persistenceVersion {
		return persistedState{}, fmt.Errorf("unsupported version %d", state.Version)
	}
	if state.Observations == nil || state.Grants == nil {
		return persistedState{}, errors.New("provider and grant maps are required")
	}
	for provider, record := range state.Observations {
		if provider == "" || record.Observation.Provider != provider {
			return persistedState{}, fmt.Errorf("provider record %q has mismatched identity", provider)
		}
		if !ValidObservation(record.Observation, record.ReceivedAt) {
			return persistedState{}, fmt.Errorf("provider record %q is invalid", provider)
		}
	}
	for grant, record := range state.Grants {
		if grant == "" || record.Observation.Provider == "" {
			return persistedState{}, fmt.Errorf("grant record %q has invalid identity", grant)
		}
		if !ValidObservation(record.Observation, record.ReceivedAt) {
			return persistedState{}, fmt.Errorf("grant record %q is invalid", grant)
		}
	}
	return cloneState(state), nil
}

// GetGrant returns the latest view for one grant; unobserved grants come back
// with Observed false and an empty provider.
func (s *Store) GetGrant(grant string) View {
	return s.getGrant(grant, time.Now().UTC())
}

func (s *Store) getGrant(grant string, now time.Time) View {
	s.mu.RLock()
	if !s.readable {
		s.mu.RUnlock()
		return View{Grant: grant}
	}
	stored, ok := s.state.Grants[grant]
	s.mu.RUnlock()
	if !ok {
		return View{Grant: grant}
	}
	view := makeView(stored, now)
	view.Grant = grant
	return view
}

// Grants returns every grant that has reported at least one observation.
func (s *Store) Grants() map[string]View {
	s.mu.RLock()
	if !s.readable {
		s.mu.RUnlock()
		return map[string]View{}
	}
	names := make([]string, 0, len(s.state.Grants))
	for name := range s.state.Grants {
		names = append(names, name)
	}
	s.mu.RUnlock()
	views := make(map[string]View, len(names))
	for _, name := range names {
		views[name] = s.GetGrant(name)
	}
	return views
}

// Get returns the latest provider view.
func (s *Store) Get(provider string) View {
	return s.get(provider, time.Now().UTC())
}

func (s *Store) get(provider string, now time.Time) View {
	s.mu.RLock()
	if !s.readable {
		s.mu.RUnlock()
		return View{Provider: provider}
	}
	stored, ok := s.state.Observations[provider]
	s.mu.RUnlock()
	if !ok {
		return View{Provider: provider}
	}
	return makeView(stored, now)
}

func makeView(stored storedObservation, now time.Time) View {
	obs := stored.Observation
	view := View{
		Provider: obs.Provider, Observed: true, ObservedAt: obs.ObservedAt,
		Status: obs.Status, ReachedType: obs.ReachedType, OverageStatus: obs.OverageStatus,
		ReceivedAt: stored.ReceivedAt, Exhausted: obs.Status == "rejected",
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

// ValidObservation checks wire and durable-record invariants. Future
// observations are rejected with zero tolerated skew so they cannot poison
// monotonic ordering or fabricate freshness.
func ValidObservation(obs Observation, receivedAt time.Time) bool {
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
	if _, offset := observedAt.Zone(); offset != 0 || observedAt.After(receivedAt) {
		return false
	}
	if receivedAt.IsZero() {
		return false
	}
	if _, offset := receivedAt.Zone(); offset != 0 {
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
