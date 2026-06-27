package reconnect

import (
	"context"
	"errors"
	"testing"
	"time"
)

func TestReconnectRunsInOrder(t *testing.T) {
	var order []string
	m := &Manager{Sleep: func(time.Duration) {}}
	m.Register("git", func(context.Context) error { order = append(order, "git"); return nil })
	m.Register("model", func(context.Context) error { order = append(order, "model"); return nil })
	m.Register("mcp", func(context.Context) error { order = append(order, "mcp"); return nil })

	if err := m.Reconnect(context.Background()); err != nil {
		t.Fatalf("Reconnect: %v", err)
	}
	want := []string{"git", "model", "mcp"}
	if len(order) != 3 || order[0] != want[0] || order[1] != want[1] || order[2] != want[2] {
		t.Fatalf("order = %v, want %v", order, want)
	}
}

func TestReconnectRetriesThenSucceeds(t *testing.T) {
	calls := 0
	m := &Manager{Attempts: 3, Sleep: func(time.Duration) {}}
	m.Register("model", func(context.Context) error {
		calls++
		if calls < 3 {
			return errors.New("connection refused")
		}
		return nil
	})
	if err := m.Reconnect(context.Background()); err != nil {
		t.Fatalf("Reconnect should succeed on 3rd attempt: %v", err)
	}
	if calls != 3 {
		t.Fatalf("calls = %d, want 3", calls)
	}
}

func TestReconnectFailsAfterAttempts(t *testing.T) {
	m := &Manager{Attempts: 2, Sleep: func(time.Duration) {}}
	m.Register("mcp", func(context.Context) error { return errors.New("down") })
	err := m.Reconnect(context.Background())
	if err == nil {
		t.Fatal("Reconnect should fail when a reconnector never succeeds")
	}
	if !errorContains(err, "mcp") || !errorContains(err, "down") {
		t.Fatalf("error should name the failing reconnector and cause: %v", err)
	}
}

func TestReconnectStopsAtFirstFailure(t *testing.T) {
	secondRan := false
	m := &Manager{Attempts: 1, Sleep: func(time.Duration) {}}
	m.Register("git", func(context.Context) error { return errors.New("no remote") })
	m.Register("model", func(context.Context) error { secondRan = true; return nil })
	if err := m.Reconnect(context.Background()); err == nil {
		t.Fatal("expected failure")
	}
	if secondRan {
		t.Fatal("should not run later reconnectors after one fails (avoid half-live env)")
	}
}

func errorContains(err error, sub string) bool {
	s := err.Error()
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return true
		}
	}
	return false
}
