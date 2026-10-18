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

// Pacing is read-time advice for the longest reported budget. It does not
// participate in exhaustion, admission, grant selection or token refresh.
type Pacing struct {
	State                   string   `json:"state"`
	EvaluatedAt             string   `json:"evaluated_at"`
	BudgetWindowMinutes     *int     `json:"budget_window_minutes,omitempty"`
	BudgetUsedPercent       *float64 `json:"budget_used_percent,omitempty"`
	BudgetRemainingPercent  *float64 `json:"budget_remaining_percent,omitempty"`
	BudgetResetsAt          string   `json:"budget_resets_at,omitempty"`
	BudgetRemainingSeconds  *float64 `json:"budget_remaining_seconds,omitempty"`
	TargetUsedPercent       *float64 `json:"target_used_percent,omitempty"`
	PaceDeltaPercent        *float64 `json:"pace_delta_percent,omitempty"`
	BudgetPerDayPercent     *float64 `json:"budget_per_day_percent,omitempty"`
	BlockedUntil            string   `json:"blocked_until,omitempty"`
	ShortWindowMinutes      *int     `json:"short_window_minutes,omitempty"`
	ShortResetsAt           string   `json:"short_resets_at,omitempty"`
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
	Pacing        *Pacing
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
		Pacing        *Pacing      `json:"pacing"`
	}{
		Provider: v.Provider, Grant: v.Grant, ObservedAt: v.ObservedAt, Status: v.Status,
		ReachedType: v.ReachedType, OverageStatus: v.OverageStatus, Windows: v.Windows, Observed: true,
		ReceivedAt: v.ReceivedAt, AgeSeconds: v.AgeSeconds, Exhausted: v.Exhausted,
		Pacing: v.Pacing,
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
	return s.GetGrantAt(grant, time.Now().UTC())
}

// GetGrantAt derives age and expiration using the caller's clock.
func (s *Store) GetGrantAt(grant string, now time.Time) View {
	return s.getGrant(grant, now)
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
		Pacing:  derivePacing(obs, now),
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

// derivePacing uses the provider's reported durations, never window names.
// Limits with missing or contradictory metadata cannot establish spare capacity.
func derivePacing(obs Observation, now time.Time) *Pacing {
	pacing := &Pacing{State: "unknown", EvaluatedAt: now.UTC().Format(time.RFC3339Nano)}
	observedAt, err := time.Parse(time.RFC3339, obs.ObservedAt)
	if err != nil || observedAt.After(now) {
		return pacing
	}
	if now.Sub(observedAt) > 900*time.Second {
		pacing.State = "stale"
		return pacing
	}

	type candidate struct {
		window Window
		reset  time.Time
	}
	windows := make(map[int]candidate)
	reported := make(map[int]Window)
	ambiguous := make(map[int]bool)
	longest := 0
	unknown := len(obs.Windows) == 0
	expired := false
	binding, unknownBindingReset := false, false
	var blockedUntil time.Time
	for _, window := range obs.Windows {
		validUsage := !math.IsNaN(window.UsedPercent) && !math.IsInf(window.UsedPercent, 0) && window.UsedPercent >= 0 && window.UsedPercent <= 100
		// Match the public publisher's one-year advisory bound before any
		// duration arithmetic. The stored provider contract stays unchanged.
		validDuration := window.WindowMinutes > 0 && window.WindowMinutes <= 525600
		resetLimit := 365 * 24 * time.Hour
		if validDuration {
			resetLimit = time.Duration(window.WindowMinutes) * time.Minute
		}
		reset, resetErr := time.Parse(time.RFC3339, window.ResetsAt)
		// Providers round reset timestamps to seconds. Permit that rounding
		// without using a contradictory reset as a claimed unblock time.
		validReset := resetErr == nil && !reset.IsZero() && !reset.After(observedAt.Add(resetLimit+time.Second))
		if validReset && !reset.After(now) {
			expired = true
		}
		// A known exhausted window can block even when its duration is unknown.
		if validUsage && window.UsedPercent == 100 && (!validReset || reset.After(now)) {
			binding = true
			if !validReset {
				unknownBindingReset = true
			} else if reset.After(blockedUntil) {
				blockedUntil = reset
			}
		}
		if validDuration && window.WindowMinutes > longest {
			longest = window.WindowMinutes
		}
		if validDuration {
			if previous, exists := reported[window.WindowMinutes]; exists {
				previousReset, previousErr := time.Parse(time.RFC3339, previous.ResetsAt)
				if previous.UsedPercent != window.UsedPercent || previousErr != nil || !validReset || !previousReset.Equal(reset) {
					ambiguous[window.WindowMinutes] = true
					unknown = true
				}
			} else {
				reported[window.WindowMinutes] = window
			}
		}
		if !validUsage || !validDuration || !validReset {
			unknown = true
			continue
		}
		if !reset.After(now) {
			continue
		}
		if _, exists := windows[window.WindowMinutes]; !exists {
			windows[window.WindowMinutes] = candidate{window: window, reset: reset}
		}
	}

	budget, haveBudget := windows[longest]
	if haveBudget && !ambiguous[longest] {
		remainingSeconds := budget.reset.Sub(now).Seconds()
		remainingPercent := 100 - budget.window.UsedPercent
		target := math.Max(0, 100*(1-remainingSeconds/(float64(longest)*60)))
		delta := budget.window.UsedPercent - target
		daily := remainingPercent * 86400 / remainingSeconds
		pacing.BudgetWindowMinutes = &budget.window.WindowMinutes
		pacing.BudgetUsedPercent = &budget.window.UsedPercent
		pacing.BudgetRemainingPercent = &remainingPercent
		pacing.BudgetResetsAt = budget.reset.UTC().Format(time.RFC3339Nano)
		pacing.BudgetRemainingSeconds = &remainingSeconds
		pacing.TargetUsedPercent = &target
		pacing.PaceDeltaPercent = &delta
		pacing.BudgetPerDayPercent = &daily
		if !unknown && !expired {
			// More than one distinct short duration has no single short reset.
			var short candidate
			shortCount := 0
			for minutes, current := range windows {
				if minutes < longest {
					short, shortCount = current, shortCount+1
				}
			}
			if shortCount == 1 {
				pacing.ShortWindowMinutes = &short.window.WindowMinutes
				pacing.ShortResetsAt = short.reset.UTC().Format(time.RFC3339Nano)
			}
		}
	}

	switch {
	case binding:
		pacing.State = "wait_for_reset"
		if !unknownBindingReset {
			pacing.BlockedUntil = blockedUntil.UTC().Format(time.RFC3339Nano)
		}
	case expired:
		pacing.State = "awaiting_report"
	case obs.Status == "rejected":
		// A rejection without an identified binding window has no known end.
		pacing.State = "wait_for_reset"
	case pacing.PaceDeltaPercent == nil:
	case *pacing.PaceDeltaPercent > 5:
		pacing.State = "conserve"
	case unknown:
	case *pacing.PaceDeltaPercent < -5:
		pacing.State = "use_more"
	default:
		pacing.State = "on_pace"
	}
	return pacing
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
