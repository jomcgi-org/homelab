package quota

import (
	"encoding/json"
	"errors"
	"fmt"
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
