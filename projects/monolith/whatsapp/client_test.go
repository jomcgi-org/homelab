package whatsapp

import (
	"context"
	"io"
	"log/slog"
	"strings"
	"sync"
	"testing"

	"go.mau.fi/whatsmeow/proto/waCommon"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/types"
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
	gw := NewGateway(Config{BotNumber: "+447700900123"}, testLogger(), session, notifier, nil, nil)

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
	gw := NewGateway(Config{BotNumber: "+447700900123"}, testLogger(), session, notifier, nil, nil)

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
	gw := NewGateway(Config{}, testLogger(), session, &fakeNotifier{}, nil, nil)

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
			gw := NewGateway(Config{}, testLogger(), session, notifier, nil, nil)

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
	gw := NewGateway(Config{}, testLogger(), session, &fakeNotifier{}, nil, nil)

	gw.handleEvent(&events.LoggedOut{})
	gw.handleEvent(&events.Connected{})

	if gw.State() != StateParked {
		t.Errorf("state = %s, want parked (parked is terminal)", gw.State())
	}
}

// fakeForwarder records forwarded payloads in call order.
type fakeForwarder struct {
	mu        sync.Mutex
	payloads  []InboundPayload
	reactions []ReactionPayload
}

func (f *fakeForwarder) Forward(p InboundPayload) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.payloads = append(f.payloads, p)
}

func (f *fakeForwarder) ForwardReaction(p ReactionPayload) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.reactions = append(f.reactions, p)
}

func (f *fakeForwarder) snapshot() []InboundPayload {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]InboundPayload(nil), f.payloads...)
}

func (f *fakeForwarder) reactionSnapshot() []ReactionPayload {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]ReactionPayload(nil), f.reactions...)
}

// reactionEvent builds an allow-listed group reaction: reactor reacts with emoji
// to a message keyed by targetID; fromMe marks whether that target was a bot-sent
// message. An empty emoji is WhatsApp's removed-reaction representation.
func reactionEvent(group, reactor, targetID, emoji string, fromMe bool) *events.Message {
	e := &events.Message{}
	e.Info.IsGroup = true
	e.Info.Chat = types.NewJID(group, types.GroupServer)
	e.Info.Sender = types.NewJID(reactor, types.DefaultUserServer)
	fm := fromMe
	tid := targetID
	txt := emoji
	e.Message = &waE2E.Message{
		ReactionMessage: &waE2E.ReactionMessage{
			Key:  &waCommon.MessageKey{FromMe: &fm, ID: &tid},
			Text: &txt,
		},
	}
	return e
}

func TestGroupReactionOnBotMessageForwarded(t *testing.T) {
	// A human 👍 on one of Bosun's messages (FromMe target) is forwarded to the
	// reaction path, not the message path, carrying the target id and reactor.
	session := &fakeSession{loggedIn: true}
	fwd := &fakeForwarder{}
	gw := NewGateway(Config{GroupJIDs: []string{"fam@g.us"}}, testLogger(), session, &fakeNotifier{}, fwd, nil)
	gw.handleEvent(&events.Connected{})

	gw.handleEvent(reactionEvent("fam", "alice", "BOT_MSG_1", "👍", true))

	if got := len(fwd.snapshot()); got != 0 {
		t.Errorf("forwarded %d messages for a reaction, want 0 (reactions take the reaction path)", got)
	}
	rs := fwd.reactionSnapshot()
	if len(rs) != 1 {
		t.Fatalf("forwarded %d reactions, want 1", len(rs))
	}
	if rs[0].GroupJID != "fam@g.us" || rs[0].TargetMessageID != "BOT_MSG_1" || rs[0].Emoji != "👍" {
		t.Errorf("reaction payload = %+v, want group fam@g.us, target BOT_MSG_1, emoji 👍", rs[0])
	}
	if rs[0].ReactorJID != "alice@s.whatsapp.net" {
		t.Errorf("reactor = %q, want alice@s.whatsapp.net", rs[0].ReactorJID)
	}
}

func TestGroupReactionOnHumanMessageDropped(t *testing.T) {
	// A reaction on a human's message (FromMe=false) is not a signal about Bosun;
	// it is dropped at the gateway and never forwarded.
	session := &fakeSession{loggedIn: true}
	fwd := &fakeForwarder{}
	gw := NewGateway(Config{GroupJIDs: []string{"fam@g.us"}}, testLogger(), session, &fakeNotifier{}, fwd, nil)
	gw.handleEvent(&events.Connected{})

	gw.handleEvent(reactionEvent("fam", "alice", "HUMAN_MSG_1", "👍", false))

	if got := len(fwd.reactionSnapshot()); got != 0 {
		t.Errorf("forwarded %d reactions on a human message, want 0", got)
	}
	if got := len(fwd.snapshot()); got != 0 {
		t.Errorf("forwarded %d messages for a reaction, want 0", got)
	}
}

func TestGroupMessageAllowList(t *testing.T) {
	// A non-allow-listed group is dropped without forwarding; this pins the
	// allow-list filter.
	session := &fakeSession{loggedIn: true}
	fwd := &fakeForwarder{}
	gw := NewGateway(Config{GroupJIDs: []string{"allowed@g.us"}}, testLogger(), session, &fakeNotifier{}, fwd, nil)
	gw.handleEvent(&events.Connected{})

	dropped := &events.Message{}
	dropped.Info.IsGroup = true // no Chat JID set -> not in the allow-list
	gw.handleEvent(dropped)
	if got := len(fwd.snapshot()); got != 0 {
		t.Errorf("forwarded %d messages from a non-allow-listed group, want 0", got)
	}
	if gw.State() != StateConnected {
		t.Errorf("state = %s, want connected (a dropped message must not change state)", gw.State())
	}
}
