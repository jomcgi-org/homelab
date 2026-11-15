package quota

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"strconv"
	"sync"
	"testing"
	"time"
)

type memoryPersistence struct {
	mu        sync.Mutex
	data      []byte
	revision  int
	loadErr   error
	writeErr  error
	conflicts int
}

func (p *memoryPersistence) Load() ([]byte, string, error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.loadErr != nil {
		return nil, "", p.loadErr
	}
	return append([]byte(nil), p.data...), strconv.Itoa(p.revision), nil
}

func (p *memoryPersistence) CompareAndSwap(revision string, data []byte) (string, error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.writeErr != nil {
		return "", p.writeErr
	}
	if p.conflicts > 0 {
		p.conflicts--
		return "", ErrPersistenceConflict
	}
	if revision != strconv.Itoa(p.revision) {
		return "", ErrPersistenceConflict
	}
	p.data = append([]byte(nil), data...)
	p.revision++
	return strconv.Itoa(p.revision), nil
}

func TestStoreDerivesExhaustedAndExpired(t *testing.T) {
	now := time.Now().UTC()
	tests := []struct {
		name      string
		status    string
		used      float64
		reset     time.Time
		exhausted bool
		expired   bool
	}{
		{name: "rejected status", status: "rejected", used: 1, reset: now.Add(time.Hour), exhausted: true},
		{name: "full active window", status: "allowed", used: 100, reset: now.Add(time.Hour), exhausted: true},
		{name: "full expired window", status: "allowed", used: 100, reset: now.Add(-time.Hour), exhausted: false, expired: true},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			store := NewStore()
			store.Put("codex", Observation{
				ObservedAt: now.Add(-time.Minute).Format(time.RFC3339), Status: tt.status,
				Windows: []Window{{Name: "primary", UsedPercent: tt.used, ResetsAt: tt.reset.Format(time.RFC3339)}},
			}, now)
			view := store.Get("codex")
			if view.Exhausted != tt.exhausted || len(view.Windows) != 1 || view.Windows[0].Expired != tt.expired {
				t.Fatalf("view = %#v", view)
			}
			if !view.Observed || view.Provider != "codex" || view.AgeSeconds < 59 {
				t.Errorf("observation metadata = %#v", view)
			}
		})
	}
}

func TestStoreUnobservedProviderShape(t *testing.T) {
	view := NewStore().Get("claude")
	encoded, err := json.Marshal(view)
	if err != nil {
		t.Fatal(err)
	}
	if got, want := string(encoded), `{"provider":"claude","observed":false}`; got != want {
		t.Fatalf("JSON = %s, want %s", got, want)
	}
}

func TestStoreKeepsLatest(t *testing.T) {
	store := NewStore()
	now := time.Now().UTC()
	store.Put("codex", Observation{ObservedAt: now.Format(time.RFC3339), Status: "allowed", Windows: []Window{{Name: "primary", UsedPercent: 10}}}, now)
	store.Put("codex", Observation{ObservedAt: now.Format(time.RFC3339), Status: "warning", Windows: []Window{{Name: "primary", UsedPercent: 20}}}, now.Add(time.Second))
	view := store.Get("codex")
	if view.Status != "warning" || view.Windows[0].UsedPercent != 20 {
		t.Fatalf("view = %#v", view)
	}
}

func TestStoreCarriesOverageStatusWithoutExhaustingAllowedClaude(t *testing.T) {
	store := NewStore()
	now := time.Now().UTC()
	store.Put("claude", Observation{
		ObservedAt: now.Format(time.RFC3339), Status: "allowed", OverageStatus: "rejected",
		Windows: []Window{{Name: "5h", UsedPercent: 24}},
	}, now)
	view := store.Get("claude")
	if view.OverageStatus != "rejected" || view.ReachedType != "" || view.Exhausted {
		t.Fatalf("view = %#v", view)
	}
	encoded, err := json.Marshal(view)
	if err != nil {
		t.Fatal(err)
	}
	var decoded map[string]any
	if err := json.Unmarshal(encoded, &decoded); err != nil {
		t.Fatal(err)
	}
	if decoded["overage_status"] != "rejected" || decoded["reached_type"] != "" || decoded["exhausted"] != false {
		t.Fatalf("JSON = %s", encoded)
	}
}

func TestValidObservationRejectsUnknownWithoutWindows(t *testing.T) {
	receivedAt := time.Now().UTC()
	now := receivedAt.Format(time.RFC3339)
	if ValidObservation(Observation{ObservedAt: now, Status: "unknown"}, receivedAt) {
		t.Fatal("unknown observation without windows was accepted")
	}
	if !ValidObservation(Observation{ObservedAt: now, Status: "unknown", Windows: []Window{{Name: "5h", UsedPercent: 10}}}, receivedAt) {
		t.Fatal("unknown observation with a parsed window was rejected")
	}
}

func TestPersistentStoreRestoresProvenanceAndRecomputesDowntime(t *testing.T) {
	persistence := &memoryPersistence{}
	receivedAt := time.Date(2026, 9, 20, 12, 0, 0, 123000000, time.UTC)
	observedAt := receivedAt.Add(-time.Minute)
	resetAt := receivedAt.Add(30 * time.Minute)
	before, err := NewPersistentStore(persistence)
	if err != nil {
		t.Fatal(err)
	}
	observation := Observation{
		ObservedAt: observedAt.Format(time.RFC3339Nano), Status: "warning", ReachedType: "rate_limit",
		Windows: []Window{
			{Name: "5h", UsedPercent: 99, ResetsAt: resetAt.Format(time.RFC3339Nano)},
			{Name: "7d", UsedPercent: 45, ResetsAt: receivedAt.Add(7 * 24 * time.Hour).Format(time.RFC3339Nano)},
		},
	}
	if err := before.PutGrant("codex-cluster", "codex", observation, receivedAt); err != nil {
		t.Fatal(err)
	}
	after, err := NewPersistentStore(persistence)
	if err != nil {
		t.Fatal(err)
	}
	view := after.get("codex", receivedAt.Add(time.Hour))
	grant := after.getGrant("codex-cluster", receivedAt.Add(time.Hour))
	if view.ObservedAt != observation.ObservedAt || !view.ReceivedAt.Equal(receivedAt) || view.Status != observation.Status || view.ReachedType != observation.ReachedType {
		t.Fatalf("restored provenance changed: %#v", view)
	}
	if view.AgeSeconds != 3660 || len(view.Windows) != 2 || !view.Windows[0].Expired || view.Windows[1].Expired {
		t.Fatalf("downtime/window derivation = %#v", view)
	}
	if !grant.Observed || grant.Provider != "codex" || len(grant.Windows) != 2 {
		t.Fatalf("grant was not restored consistently: %#v", grant)
	}
}

func TestPersistentStoreOrdersRecordsMonotonically(t *testing.T) {
	persistence := &memoryPersistence{conflicts: 1}
	store, err := NewPersistentStore(persistence)
	if err != nil {
		t.Fatal(err)
	}
	receivedAt := time.Date(2026, 9, 20, 12, 0, 0, 0, time.UTC)
	newer := Observation{ObservedAt: receivedAt.Add(-time.Minute).Format(time.RFC3339), Status: "warning", Windows: []Window{{Name: "5h", UsedPercent: 70}}}
	older := Observation{ObservedAt: receivedAt.Add(-2 * time.Minute).Format(time.RFC3339), Status: "rejected", Windows: []Window{{Name: "5h", UsedPercent: 100}}}
	if err := store.Put("codex", newer, receivedAt); err != nil {
		t.Fatal(err)
	}
	if err := store.Put("codex", older, receivedAt.Add(time.Hour)); err != nil {
		t.Fatal(err)
	}
	if view := store.Get("codex"); view.Status != "warning" || view.Windows[0].UsedPercent != 70 {
		t.Fatalf("older observed_at rolled state back: %#v", view)
	}

	equalObserved := newer.ObservedAt
	first := storedObservation{Observation: Observation{Provider: "codex", ObservedAt: equalObserved, Status: "allowed", Windows: []Window{{Name: "5h", UsedPercent: 1}}}, ReceivedAt: receivedAt.Add(time.Second)}
	second := storedObservation{Observation: Observation{Provider: "codex", ObservedAt: equalObserved, Status: "warning", Windows: []Window{{Name: "5h", UsedPercent: 2}}}, ReceivedAt: receivedAt.Add(time.Second)}
	winner := first
	if compareRecords(second, first) > 0 {
		winner = second
	}
	for _, candidate := range []storedObservation{first, second} {
		if err := store.Put("codex", candidate.Observation, candidate.ReceivedAt); err != nil {
			t.Fatal(err)
		}
	}
	view := store.Get("codex")
	if view.Status != winner.Observation.Status || view.Windows[0].UsedPercent != winner.Observation.Windows[0].UsedPercent {
		t.Fatalf("canonical hash tie winner = %#v, want %#v", view, winner)
	}
}

func TestConcurrentPersistentWritersMergeProviderAndGrantState(t *testing.T) {
	persistence := &memoryPersistence{}
	left, err := NewPersistentStore(persistence)
	if err != nil {
		t.Fatal(err)
	}
	right, err := NewPersistentStore(persistence)
	if err != nil {
		t.Fatal(err)
	}
	now := time.Date(2026, 9, 20, 12, 0, 0, 0, time.UTC)
	var wait sync.WaitGroup
	errs := make(chan error, 2)
	for i, writer := range []*Store{left, right} {
		wait.Add(1)
		go func(i int, writer *Store) {
			defer wait.Done()
			observation := Observation{ObservedAt: now.Add(time.Duration(i) * time.Second).Format(time.RFC3339), Status: "allowed", Windows: []Window{{Name: "5h", UsedPercent: float64(i + 1)}}}
			errs <- writer.PutGrant(fmt.Sprintf("grant-%d", i), "codex", observation, now.Add(time.Duration(i)*time.Second))
		}(i, writer)
	}
	wait.Wait()
	close(errs)
	for err := range errs {
		if err != nil {
			t.Fatal(err)
		}
	}
	restored, err := NewPersistentStore(persistence)
	if err != nil {
		t.Fatal(err)
	}
	if view := restored.Get("codex"); view.Windows[0].UsedPercent != 2 {
		t.Fatalf("provider did not keep newest concurrent record: %#v", view)
	}
	grants := restored.Grants()
	if len(grants) != 2 || !grants["grant-0"].Observed || !grants["grant-1"].Observed || grants["grant-0"].Windows[0].UsedPercent != 1 || grants["grant-1"].Windows[0].UsedPercent != 2 {
		t.Fatalf("grant records were not atomically merged: %#v", grants)
	}
}

func TestPersistentStoreFailuresRemainUnknown(t *testing.T) {
	futureState := persistedState{
		Version: persistenceVersion,
		Observations: map[string]storedObservation{
			"codex": {
				Observation: Observation{Provider: "codex", ObservedAt: "2026-09-20T12:00:01Z", Status: "allowed", Windows: []Window{}},
				ReceivedAt:  time.Date(2026, 9, 20, 12, 0, 0, 0, time.UTC),
			},
		},
		Grants: map[string]storedObservation{},
	}
	futureData, err := json.Marshal(futureState)
	if err != nil {
		t.Fatal(err)
	}
	tests := []struct {
		name        string
		persistence *memoryPersistence
	}{
		{name: "missing state", persistence: &memoryPersistence{}},
		{name: "malformed state", persistence: &memoryPersistence{data: []byte(`{"version":1,"providers":`)}},
		{name: "incompatible state", persistence: &memoryPersistence{data: []byte(`{"version":2,"providers":{},"grants":{}}`)}},
		{name: "future observed_at", persistence: &memoryPersistence{data: futureData}},
		{name: "unavailable state", persistence: &memoryPersistence{loadErr: errors.New("api unavailable")}},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			store, _ := NewPersistentStore(test.persistence)
			if view := store.Get("codex"); view.Observed {
				t.Fatalf("failed restore fabricated observation: %#v", view)
			}
		})
	}
}

func TestWriteFailureDoesNotPublishHealthyMemory(t *testing.T) {
	persistence := &memoryPersistence{}
	store, err := NewPersistentStore(persistence)
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now().UTC()
	initial := Observation{ObservedAt: now.Add(-time.Minute).Format(time.RFC3339), Status: "warning", Windows: []Window{{Name: "5h", UsedPercent: 50}}}
	if err := store.Put("codex", initial, now); err != nil {
		t.Fatal(err)
	}
	persistence.mu.Lock()
	persistence.writeErr = errors.New("api unavailable")
	persistence.mu.Unlock()
	err = store.Put("codex", Observation{ObservedAt: now.Format(time.RFC3339), Status: "allowed", Windows: []Window{{Name: "5h", UsedPercent: 1}}}, now)
	if err == nil {
		t.Fatal("write failure was accepted")
	}
	if view := store.Get("codex"); view.Observed {
		t.Fatalf("failed write fabricated healthy memory: %#v", view)
	}
	persistence.mu.Lock()
	persistence.writeErr = nil
	persistence.mu.Unlock()
	freshAt := now.Add(time.Second)
	if err := store.Put("codex", Observation{ObservedAt: freshAt.Format(time.RFC3339Nano), Status: "allowed", Windows: []Window{{Name: "5h", UsedPercent: 2}}}, freshAt); err != nil {
		t.Fatal(err)
	}
	if view := store.Get("codex"); !view.Observed || view.Windows[0].UsedPercent != 2 {
		t.Fatalf("fresh successful write did not recover visibility: %#v", view)
	}
}

func TestFreshSuccessRecoversPersistedExpiredRejection(t *testing.T) {
	persistence := &memoryPersistence{}
	store, err := NewPersistentStore(persistence)
	if err != nil {
		t.Fatal(err)
	}
	now := time.Date(2026, 9, 20, 12, 0, 0, 0, time.UTC)
	rejected := Observation{
		ObservedAt: now.Add(-time.Hour).Format(time.RFC3339), Status: "rejected",
		Windows: []Window{{Name: "5h", UsedPercent: 100, ResetsAt: now.Add(-time.Minute).Format(time.RFC3339)}},
	}
	if err := store.Put("codex", rejected, now.Add(-time.Hour)); err != nil {
		t.Fatal(err)
	}
	if view := store.get("codex", now); !view.Exhausted || !view.Windows[0].Expired {
		t.Fatalf("passed reset established healthy capacity: %#v", view)
	}
	fresh := Observation{
		ObservedAt: now.Format(time.RFC3339), Status: "allowed",
		Windows: []Window{
			{Name: "5h", UsedPercent: 3, ResetsAt: now.Add(5 * time.Hour).Format(time.RFC3339)},
			{Name: "7d", UsedPercent: 40, ResetsAt: now.Add(7 * 24 * time.Hour).Format(time.RFC3339)},
		},
	}
	if err := store.Put("codex", fresh, now); err != nil {
		t.Fatal(err)
	}
	restored, err := NewPersistentStore(persistence)
	if err != nil {
		t.Fatal(err)
	}
	if view := restored.get("codex", now); view.Exhausted || view.Status != "allowed" || len(view.Windows) != 2 {
		t.Fatalf("fresh success did not recover all windows: %#v", view)
	}
}

func TestValidObservationRejectsFutureObservedAt(t *testing.T) {
	receivedAt := time.Date(2026, 9, 20, 12, 0, 0, 0, time.UTC)
	observation := Observation{ObservedAt: receivedAt.Add(time.Nanosecond).Format(time.RFC3339Nano), Status: "allowed"}
	if ValidObservation(observation, receivedAt) {
		t.Fatal("future observed_at was accepted")
	}
	observation.ObservedAt = receivedAt.Format(time.RFC3339Nano)
	if !ValidObservation(observation, receivedAt) {
		t.Fatal("observed_at equal to received_at was rejected")
	}
}

func TestPacingUsesLongestReportedBudget(t *testing.T) {
	now := time.Date(2026, 10, 18, 12, 0, 0, 0, time.UTC)
	window := func(name string, minutes int, used float64, remaining time.Duration) Window {
		return Window{Name: name, WindowMinutes: minutes, UsedPercent: used, ResetsAt: now.Add(remaining).Format(time.RFC3339Nano)}
	}
	tests := []struct {
		name    string
		windows []Window
		state   string
		budget  int
	}{
		{"weekly primary high burn with most of week left", []Window{window("primary", 10080, 70, 6*24*time.Hour)}, "conserve", 10080},
		{"short high burn is compatible with weekly room", []Window{window("primary", 300, 90, 4*time.Hour), window("secondary", 10080, 10, 5*24*time.Hour)}, "use_more", 10080},
		{"short exhausted still blocks weekly room", []Window{window("primary", 300, 100, 4*time.Hour), window("secondary", 10080, 10, 5*24*time.Hour)}, "wait_for_reset", 10080},
		{"steady budget", []Window{window("weekly", 10080, 50, 84*time.Hour)}, "on_pace", 10080},
		{"positive tolerance boundary", []Window{window("weekly", 10080, 55, 84*time.Hour)}, "on_pace", 10080},
		{"negative tolerance boundary", []Window{window("weekly", 10080, 45, 84*time.Hour)}, "on_pace", 10080},
		{"single reported window", []Window{window("arbitrary", 300, 10, time.Hour)}, "use_more", 300},
		{"unknown duration blocks reassurance", []Window{window("weekly", 10080, 10, 5*24*time.Hour), window("unknown", 0, 0, time.Hour)}, "unknown", 10080},
		{"known budget overuse remains useful", []Window{window("weekly", 10080, 70, 6*24*time.Hour), window("unknown", 0, 0, time.Hour)}, "conserve", 10080},
		{"duplicate identical budget", []Window{window("a", 10080, 50, 84*time.Hour), window("b", 10080, 50, 84*time.Hour)}, "on_pace", 10080},
		{"conflicting duplicate budget", []Window{window("a", 10080, 10, 84*time.Hour), window("b", 10080, 80, 84*time.Hour)}, "unknown", 0},
		{"conflicting duplicate reset", []Window{window("a", 10080, 70, 84*time.Hour), window("b", 10080, 70, 85*time.Hour)}, "unknown", 0},
		{"invalid duplicate budget", []Window{window("a", 10080, 70, 84*time.Hour), window("b", 10080, 101, 84*time.Hour)}, "unknown", 0},
		{"reset at now awaits report", []Window{window("weekly", 10080, 0, 0)}, "awaiting_report", 0},
		{"expired short limit blocks reassurance", []Window{window("short", 300, 100, -time.Second), window("weekly", 10080, 10, 5*24*time.Hour)}, "awaiting_report", 10080},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			view := makeView(storedObservation{Observation: Observation{ObservedAt: now.Format(time.RFC3339Nano), Status: "allowed", Windows: tt.windows}}, now)
			if view.Pacing == nil || view.Pacing.State != tt.state {
				t.Fatalf("pacing = %#v, want %s", view.Pacing, tt.state)
			}
			if tt.budget == 0 {
				if view.Pacing.BudgetWindowMinutes != nil {
					t.Fatalf("ambiguous budget = %#v", view.Pacing)
				}
			} else if view.Pacing.BudgetWindowMinutes == nil || *view.Pacing.BudgetWindowMinutes != tt.budget {
				t.Fatalf("budget = %#v, want %d minutes", view.Pacing, tt.budget)
			}
		})
	}
}

func TestPacingReportedShortResetAndDailyBudget(t *testing.T) {
	now := time.Date(2026, 10, 18, 12, 0, 0, 0, time.UTC)
	obs := Observation{ObservedAt: now.Format(time.RFC3339Nano), Status: "allowed", Windows: []Window{
		{Name: "weekly", WindowMinutes: 10080, UsedPercent: 70, ResetsAt: now.Add(6 * 24 * time.Hour).Format(time.RFC3339Nano)},
		{Name: "session", WindowMinutes: 300, UsedPercent: 50, ResetsAt: now.Add(4 * time.Hour).Format(time.RFC3339Nano)},
	}}
	pacing := makeView(storedObservation{Observation: obs}, now).Pacing
	if pacing.ShortWindowMinutes == nil || *pacing.ShortWindowMinutes != 300 || pacing.ShortResetsAt != obs.Windows[1].ResetsAt {
		t.Fatalf("reported short reset = %#v", pacing)
	}
	if pacing.BudgetPerDayPercent == nil || *pacing.BudgetPerDayPercent != 5 || pacing.BudgetRemainingPercent == nil || *pacing.BudgetRemainingPercent != 30 {
		t.Fatalf("remaining daily budget = %#v", pacing)
	}
	if pacing.TargetUsedPercent == nil || math.Abs(*pacing.TargetUsedPercent-100.0/7) > 1e-8 || pacing.PaceDeltaPercent == nil || math.Abs(*pacing.PaceDeltaPercent-(70-100.0/7)) > 1e-8 {
		t.Fatalf("budget target = %#v", pacing)
	}
	for _, gap := range []time.Duration{-time.Second, 0, time.Second} {
		// The actual short reset is preserved even when it falls at or after
		// the longer budget's next reset. No future cycle is inferred.
		obs.Windows[0].ResetsAt = now.Add(4*time.Hour + gap).Format(time.RFC3339Nano)
		pacing := makeView(storedObservation{Observation: obs}, now).Pacing
		if pacing.ShortResetsAt != obs.Windows[1].ResetsAt {
			t.Fatalf("reported reset changed for budget gap %v: %#v", gap, pacing)
		}
	}
}

func TestPacingOmitsUnknownOrAmbiguousShortReset(t *testing.T) {
	now := time.Date(2026, 10, 18, 12, 0, 0, 0, time.UTC)
	short := Window{Name: "session", WindowMinutes: 300, UsedPercent: 50, ResetsAt: now.Add(4 * time.Hour).Format(time.RFC3339Nano)}
	for _, windows := range [][]Window{
		nil,
		{{Name: "session", WindowMinutes: 300, UsedPercent: 50}},
		{{Name: "session", WindowMinutes: 300, UsedPercent: 50, ResetsAt: now.Format(time.RFC3339Nano)}},
		{short, {Name: "conflicting-session", WindowMinutes: 300, UsedPercent: 50, ResetsAt: now.Add(3 * time.Hour).Format(time.RFC3339Nano)}},
		{short, {Name: "another-short-limit", WindowMinutes: 60, UsedPercent: 50, ResetsAt: now.Add(time.Hour).Format(time.RFC3339Nano)}},
	} {
		obs := Observation{ObservedAt: now.Format(time.RFC3339Nano), Status: "allowed", Windows: append([]Window{{Name: "weekly", WindowMinutes: 10080, UsedPercent: 70, ResetsAt: now.Add(6 * 24 * time.Hour).Format(time.RFC3339Nano)}}, windows...)}
		pacing := makeView(storedObservation{Observation: obs}, now).Pacing
		if pacing.ShortWindowMinutes != nil || pacing.ShortResetsAt != "" {
			t.Fatalf("unknown or ambiguous short reset produced a clock: %#v", pacing)
		}
	}
}

func TestPacingWaitsForEveryBindingLimit(t *testing.T) {
	now := time.Date(2026, 10, 18, 12, 0, 0, 0, time.UTC)
	shortReset := now.Add(time.Hour).Format(time.RFC3339)
	longReset := now.Add(6 * 24 * time.Hour).Format(time.RFC3339)
	obs := Observation{ObservedAt: now.Format(time.RFC3339), Status: "allowed", Windows: []Window{
		{Name: "short", WindowMinutes: 300, UsedPercent: 100, ResetsAt: shortReset},
		{Name: "weekly", WindowMinutes: 10080, UsedPercent: 100, ResetsAt: longReset},
	}}
	pacing := makeView(storedObservation{Observation: obs}, now).Pacing
	if pacing.State != "wait_for_reset" || pacing.BlockedUntil != longReset {
		t.Fatalf("latest binding reset = %#v", pacing)
	}
	obs.Windows[0].ResetsAt = ""
	pacing = makeView(storedObservation{Observation: obs}, now).Pacing
	if pacing.State != "wait_for_reset" || pacing.BlockedUntil != "" {
		t.Fatalf("unknown binding reset claimed an unblock time: %#v", pacing)
	}
	obs.Windows[0].ResetsAt = now.Add(365 * 24 * time.Hour).Format(time.RFC3339)
	pacing = makeView(storedObservation{Observation: obs}, now).Pacing
	if pacing.State != "wait_for_reset" || pacing.BlockedUntil != "" {
		t.Fatalf("contradictory binding reset claimed an unblock time: %#v", pacing)
	}
	obs.Status = "rejected"
	obs.Windows = nil
	pacing = makeView(storedObservation{Observation: obs}, now).Pacing
	if pacing.State != "wait_for_reset" || pacing.BlockedUntil != "" {
		t.Fatalf("rejection without timing = %#v", pacing)
	}
}

func TestPacingRejectsUnreliableInputsWithoutChangingRawView(t *testing.T) {
	now := time.Date(2026, 10, 18, 12, 0, 0, 0, time.UTC)
	tests := []struct {
		name   string
		mutate func(*Observation)
		state  string
	}{
		{"stale", func(o *Observation) { o.ObservedAt = now.Add(-901 * time.Second).Format(time.RFC3339) }, "stale"},
		{"future", func(o *Observation) { o.ObservedAt = now.Add(time.Nanosecond).Format(time.RFC3339Nano) }, "unknown"},
		{"missing observation time", func(o *Observation) { o.ObservedAt = "" }, "unknown"},
		{"missing reset", func(o *Observation) { o.Windows[0].ResetsAt = "" }, "unknown"},
		{"invalid reset", func(o *Observation) { o.Windows[0].ResetsAt = "garbage" }, "unknown"},
		{"reset beyond duration", func(o *Observation) { o.Windows[0].ResetsAt = now.Add(8 * 24 * time.Hour).Format(time.RFC3339) }, "unknown"},
		{"missing duration", func(o *Observation) { o.Windows[0].WindowMinutes = 0 }, "unknown"},
		{"negative duration", func(o *Observation) { o.Windows[0].WindowMinutes = -1 }, "unknown"},
		{"oversize duration", func(o *Observation) { o.Windows[0].WindowMinutes = 525601 }, "unknown"},
		{"overflow duration", func(o *Observation) { o.Windows[0].WindowMinutes = int(^uint(0) >> 1) }, "unknown"},
		{"negative usage", func(o *Observation) { o.Windows[0].UsedPercent = -1 }, "unknown"},
		{"oversize usage", func(o *Observation) { o.Windows[0].UsedPercent = 101 }, "unknown"},
		{"infinite usage", func(o *Observation) { o.Windows[0].UsedPercent = math.Inf(1) }, "unknown"},
		{"nan usage", func(o *Observation) { o.Windows[0].UsedPercent = math.NaN() }, "unknown"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			obs := Observation{ObservedAt: now.Format(time.RFC3339), Status: "allowed", Windows: []Window{{Name: "weekly", WindowMinutes: 10080, UsedPercent: 10, ResetsAt: now.Add(6 * 24 * time.Hour).Format(time.RFC3339)}}}
			tt.mutate(&obs)
			view := makeView(storedObservation{Observation: obs}, now)
			if view.Pacing.State != tt.state || view.Pacing.BudgetWindowMinutes != nil || view.Pacing.BudgetPerDayPercent != nil || view.Pacing.ShortResetsAt != "" {
				t.Fatalf("unreliable input produced guidance: %#v", view.Pacing)
			}
			if view.Windows[0].ResetsAt != obs.Windows[0].ResetsAt || view.Windows[0].WindowMinutes != obs.Windows[0].WindowMinutes {
				t.Fatalf("advisory changed raw window: %#v", view)
			}
		})
	}
}

func TestPacingReadDoesNotPersistOrChangeExhaustion(t *testing.T) {
	now := time.Date(2026, 10, 18, 12, 0, 0, 0, time.UTC)
	persistence := &memoryPersistence{}
	store, err := NewPersistentStore(persistence)
	if err != nil {
		t.Fatal(err)
	}
	obs := Observation{ObservedAt: now.Format(time.RFC3339), Status: "allowed", Windows: []Window{{Name: "primary", WindowMinutes: 10080, UsedPercent: 70, ResetsAt: now.Add(6 * 24 * time.Hour).Format(time.RFC3339)}}}
	if err := store.PutGrant("account", "codex", obs, now); err != nil {
		t.Fatal(err)
	}
	before, revision, _ := persistence.Load()
	for _, readAt := range []time.Time{now, now.Add(900 * time.Second), now.Add(901 * time.Second), now.Add(7 * 24 * time.Hour)} {
		view := store.GetGrantAt("account", readAt)
		if view.Pacing.EvaluatedAt != readAt.Format(time.RFC3339Nano) || view.Exhausted {
			t.Fatalf("read changed control state: %#v", view)
		}
		if readAt.Equal(now.Add(900*time.Second)) && view.Pacing.State != "conserve" {
			t.Fatalf("freshness boundary = %#v", view.Pacing)
		}
	}
	after, afterRevision, _ := persistence.Load()
	if !bytes.Equal(before, after) || revision != afterRevision || bytes.Contains(after, []byte("pacing")) {
		t.Fatal("read-time guidance mutated durable observation")
	}
	encoded, err := json.Marshal(store.GetGrantAt("account", now))
	if err != nil || !bytes.Contains(encoded, []byte(`"pacing":{"state":"conserve"`)) {
		t.Fatalf("observed JSON missing guidance: %s, %v", encoded, err)
	}
}

func TestPacingBoundsAndResetTimestampRounding(t *testing.T) {
	now := time.Date(2026, 10, 18, 12, 0, 0, 0, time.UTC)
	for _, offset := range []time.Duration{-time.Second, 0, time.Second} {
		obs := Observation{ObservedAt: now.Format(time.RFC3339Nano), Status: "allowed", Windows: []Window{{Name: "weekly", WindowMinutes: 10080, UsedPercent: 0, ResetsAt: now.Add(7*24*time.Hour + offset).Format(time.RFC3339Nano)}}}
		pacing := makeView(storedObservation{Observation: obs}, now).Pacing
		if pacing.State != "on_pace" || pacing.TargetUsedPercent == nil || *pacing.TargetUsedPercent < 0 {
			t.Fatalf("reset rounding %v gave %#v", offset, pacing)
		}
	}
	obs := Observation{ObservedAt: now.Format(time.RFC3339Nano), Status: "allowed", Windows: []Window{
		{Name: "long", WindowMinutes: 525600, UsedPercent: 0, ResetsAt: now.Add(365 * 24 * time.Hour).Format(time.RFC3339Nano)},
		{Name: "short", WindowMinutes: 1, UsedPercent: 0, ResetsAt: now.Add(time.Minute).Format(time.RFC3339Nano)},
	}}
	pacing := makeView(storedObservation{Observation: obs}, now).Pacing
	if pacing.ShortResetsAt != obs.Windows[1].ResetsAt || pacing.BudgetWindowMinutes == nil || *pacing.BudgetWindowMinutes != 525600 {
		t.Fatalf("bounded duration changed reported reset: %#v", pacing)
	}
	obs.Windows[0].ResetsAt = now.Add(time.Nanosecond).Format(time.RFC3339Nano)
	pacing = makeView(storedObservation{Observation: obs}, now).Pacing
	if pacing.BudgetPerDayPercent == nil || math.IsInf(*pacing.BudgetPerDayPercent, 0) || math.IsNaN(*pacing.BudgetPerDayPercent) {
		t.Fatalf("near-reset daily budget overflowed: %#v", pacing)
	}
	encoded, err := json.Marshal(pacing)
	if err != nil {
		t.Fatalf("bounded guidance is not JSON-safe: %s, %v", encoded, err)
	}
}
