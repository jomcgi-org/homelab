package quota

import (
	"encoding/json"
	"testing"
	"time"
)

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
	store.Put("codex", Observation{ObservedAt: now.Format(time.RFC3339), Status: "warning", Windows: []Window{{Name: "primary", UsedPercent: 20}}}, now)
	view := store.Get("codex")
	if view.Status != "warning" || view.Windows[0].UsedPercent != 20 {
		t.Fatalf("view = %#v", view)
	}
}

func TestValidObservationRejectsUnknownWithoutWindows(t *testing.T) {
	now := time.Now().UTC().Format(time.RFC3339)
	if ValidObservation(Observation{ObservedAt: now, Status: "unknown"}) {
		t.Fatal("unknown observation without windows was accepted")
	}
	if !ValidObservation(Observation{ObservedAt: now, Status: "unknown", Windows: []Window{{Name: "5h", UsedPercent: 10}}}) {
		t.Fatal("unknown observation with a parsed window was rejected")
	}
}
