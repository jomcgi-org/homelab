package whatsapp

import (
	"context"
	"io"
	"log/slog"
	"strings"
	"sync"
	"testing"

	"go.mau.fi/whatsmeow/types/events"
)

// fakeSession is a test double for Session: it records calls and never touches
// WhatsApp or a database.
type fakeSession struct {
	loggedIn     bool
	pairingCode  string
	connectCalls int
	disconnects  int
	handler      func(evt any)
}

func (f *fakeSession) IsLoggedIn() bool { return f.loggedIn }

func (f *fakeSession) Connect() error {
	f.connectCalls++
	return nil
}

func (f *fakeSession) RequestPairingCode(context.Context) (string, error) {
	return f.pairingCode, nil
}

func (f *fakeSession) Disconnect() { f.disconnects++ }

func (f *fakeSession) AddEventHandler(h func(evt any)) { f.handler = h }

// fakeNotifier records every alert delivered.
type fakeNotifier struct {
	mu    sync.Mutex
	calls []notifyCall
}

type notifyCall struct {
	level   string
	content string
}

func (n *fakeNotifier) Notify(_ context.Context, level, content string) error {
	n.mu.Lock()
	defer n.mu.Unlock()
	n.calls = append(n.calls, notifyCall{level, content})
	return nil
}

func (n *fakeNotifier) snapshot() []notifyCall {
	n.mu.Lock()
	defer n.mu.Unlock()
	return append([]notifyCall(nil), n.calls...)
}

func testLogger() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

func TestStartNoSessionEntersPairingAndDeliversCodeOnce(t *testing.T) {
	session := &fakeSession{loggedIn: false, pairingCode: "ABCD1234"}
	notifier := &fakeNotifier{}
	gw := NewGateway(Config{BotNumber: "+447700900123"}, testLogger(), session, notifier)

	if err := gw.Start(context.Background()); err != nil {
		t.Fatalf("Start: %v", err)
	}

	if gw.State() != StatePairing {
		t.Errorf("state = %s, want pairing", gw.State())
	}
	if session.connectCalls != 1 {
		t.Errorf("connectCalls = %d, want 1", session.connectCalls)
	}
	calls := notifier.snapshot()
	if len(calls) != 1 {
		t.Fatalf("notify called %d times, want exactly 1: %v", len(calls), calls)
	}
	if calls[0].level != "info" {
		t.Errorf("notify level = %q, want info", calls[0].level)
	}
	if !strings.Contains(calls[0].content, "ABCD1234") {
		t.Errorf("notify content %q should carry the pairing code", calls[0].content)
	}
}

func TestStartWithStoredSessionResumesWithoutPairing(t *testing.T) {
	session := &fakeSession{loggedIn: true}
	notifier := &fakeNotifier{}
	gw := NewGateway(Config{BotNumber: "+447700900123"}, testLogger(), session, notifier)

	if err := gw.Start(context.Background()); err != nil {
		t.Fatalf("Start: %v", err)
	}
	if session.connectCalls != 1 {
		t.Errorf("connectCalls = %d, want 1", session.connectCalls)
	}
	if calls := notifier.snapshot(); len(calls) != 0 {
		t.Errorf("resume should deliver no pairing code, got %v", calls)
	}
}

func TestConnectedEventSetsConnected(t *testing.T) {
	session := &fakeSession{loggedIn: true}
	gw := NewGateway(Config{}, testLogger(), session, &fakeNotifier{})

	gw.handleEvent(&events.Connected{})

	if gw.State() != StateConnected {
		t.Errorf("state = %s, want connected", gw.State())
	}
}

func TestParkedTransition(t *testing.T) {
	tests := []struct {
		name string
		evt  any
	}{
		{"logged out", &events.LoggedOut{OnConnect: true, Reason: events.ConnectFailureLoggedOut}},
		{"temporary ban", &events.TemporaryBan{Code: events.TempBanSentToTooManyPeople}},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			session := &fakeSession{loggedIn: true}
			notifier := &fakeNotifier{}
			gw := NewGateway(Config{}, testLogger(), session, notifier)

			// Fire the parking event twice: the alert must still fire exactly once
			// and the gateway must disconnect (work stops) exactly once.
			gw.handleEvent(tt.evt)
			gw.handleEvent(tt.evt)

			if gw.State() != StateParked {
				t.Errorf("state = %s, want parked", gw.State())
			}
			calls := notifier.snapshot()
			if len(calls) != 1 {
				t.Fatalf("notify called %d times, want exactly 1: %v", len(calls), calls)
			}
			if calls[0].level != "error" {
				t.Errorf("parked alert level = %q, want error", calls[0].level)
			}
			if session.disconnects != 1 {
				t.Errorf("disconnects = %d, want 1 (work stops once)", session.disconnects)
			}
		})
	}
}

func TestParkedStaysParkedOnLaterConnected(t *testing.T) {
	// A stray Connected after parking must not resurrect the gateway: parked is
	// terminal until an operator re-pairs.
	session := &fakeSession{loggedIn: true}
	gw := NewGateway(Config{}, testLogger(), session, &fakeNotifier{})

	gw.handleEvent(&events.LoggedOut{})
	gw.handleEvent(&events.Connected{})

	if gw.State() != StateParked {
		t.Errorf("state = %s, want parked (parked is terminal)", gw.State())
	}
}

func TestGroupMessageAllowList(t *testing.T) {
	// A message in a non-allow-listed group is dropped without panic; this pins
	// the allow-list filter (log-only in Phase 1, no forwarding).
	session := &fakeSession{loggedIn: true}
	gw := NewGateway(Config{GroupJIDs: []string{"allowed@g.us"}}, testLogger(), session, &fakeNotifier{})
	gw.handleEvent(&events.Connected{})

	msg := &events.Message{}
	msg.Info.IsGroup = true
	// Not in the allow-list: must be a no-op (and must not change state).
	gw.handleEvent(msg)
	if gw.State() != StateConnected {
		t.Errorf("state = %s, want connected (a dropped message must not change state)", gw.State())
	}
}
