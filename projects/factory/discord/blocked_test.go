package discord

import (
	"context"
	"errors"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
)

// blockedErr is the failure the Hermes adapter reports when the provider's
// safety filter refuses a turn.
var blockedErr = &TurnFailure{State: kagent.StateFailed, Message: "Hermes turn failed: " + ReasonContentPolicy}

func TestContentBlockedNeedsAFailedTaskWithTheReason(t *testing.T) {
	cases := map[error]bool{
		blockedErr: true,
		&TurnFailure{State: kagent.StateFailed, Message: "Hermes turn failed"}:     false,
		&TurnFailure{State: kagent.StateCanceled, Message: ReasonContentPolicy}:    false,
		errors.New("discord: agent turn did not complete: " + ReasonContentPolicy): false,
		errors.Join(errors.New("wrapped"), blockedErr):                             true,
		nil: false,
	}
	for err, want := range cases {
		if got := ContentBlocked(err); got != want {
			t.Errorf("ContentBlocked(%v) = %v, want %v", err, got, want)
		}
	}
	if !errors.Is(blockedErr, ErrTurnFailed) {
		t.Error("a TurnFailure is not ErrTurnFailed")
	}
}

// The limit reply says when a turn is next allowed: when the oldest turn in
// the window leaves it.
func TestALimitedTurnSaysWhenToTryAgain(t *testing.T) {
	b, _, _ := newBridge(&fakeAgent{})
	b.Config.UserLimit = Limit{Count: 2, Window: time.Hour}
	b.start(context.Background())
	t0 := time.Date(2026, 11, 28, 11, 0, 0, 0, time.UTC)
	m := tagged("1", "hi")
	b.mu.Lock()
	defer b.mu.Unlock()
	for _, at := range []time.Time{t0, t0.Add(10 * time.Minute)} {
		if ok, _ := b.take(at, m, false); !ok {
			t.Fatalf("turn at %s refused", at)
		}
	}
	ok, retry := b.take(t0.Add(20*time.Minute), m, false)
	if ok || !retry.Equal(t0.Add(time.Hour)) {
		t.Fatalf("third turn = %v, retry %s; want refused until %s", ok, retry, t0.Add(time.Hour))
	}
	if got, want := LimitedReply(retry), "I've reached my limit for now; try again <t:1795867200:R>."; got != want {
		t.Errorf("reply = %q, want %q", got, want)
	}
	if LimitedReply(time.Time{}) != ReplyLimited {
		t.Error("an unknown retry time changes the reply")
	}
	if ok, _ := b.take(t0.Add(time.Hour), m, false); !ok {
		t.Error("turn at the retry time refused")
	}
}

// 2026-11-28: a refused joke got "I couldn't answer that right now", and
// the next message was refused too. A refused turn gets its own reply, and
// later turns leave the refused message out of their context.
func TestABlockedTurnSaysSoAndLeavesTheMessageOutOfLaterTurns(t *testing.T) {
	agent := &fakeAgent{err: blockedErr}
	b, _, rep := ingestBridge(agent)
	b.Turn(context.Background(), tagged("20", "how would Ross hack it"))
	if len(rep.sent) != 1 || rep.sent[0].Components[0].Content != ReplyBlocked {
		t.Fatalf("sent %+v, want the blocked reply", rep.sent)
	}

	agent.err, agent.answer = nil, "ok"
	joke := said("20", "ann", "how would Ross hack it", 0)
	answer := said("22", "ann", "or would you just not", 2)
	answer.Type = 19
	answer.Referenced = &Referenced{ID: "20", Author: User{ID: "u1", Username: "ann"}, Content: "how would Ross hack it"}
	b.History = &channelHistory{msgs: []HistoryMessage{answer, said("21", "dave", "lol", 1), joke}}
	next := tagged("23", "can't or won't")
	next.Type = 19
	next.Referenced = &Referenced{ID: "20", Author: User{ID: "u1", Username: "ann"}, Content: "how would Ross hack it"}
	b.Turn(context.Background(), next)
	prompt := lastPrompt(agent)
	if strings.Contains(prompt, "Ross") {
		t.Errorf("the blocked message reached a later prompt: %s", prompt)
	}
	got := texts(sinceLines(t, prompt))
	if len(got) != 2 || got[0] != "dave: lol" || got[1] != "ann: or would you just not" {
		t.Errorf("lines = %q", got)
	}
}

// deletingSessions is fakeSessions with DeleteSession.
type deletingSessions struct {
	*fakeSessions
	deleted []string
}

func (f *deletingSessions) DeleteSession(_ context.Context, id string) (*kagent.Session, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.deleted = append(f.deleted, id)
	for _, s := range f.byReq {
		if s.ID == id {
			s.State = kagent.RuntimeDeleted
		}
	}
	return &kagent.Session{ID: id, State: kagent.RuntimeDeleted}, nil
}

// A refused turn deletes its session: the provider judges the whole
// conversation, so the next turn opens a clean one. Another failure keeps
// the session.
func TestKagentOperatorDropsASessionTheFilterRefused(t *testing.T) {
	gw := &a2aServer{fail: "Hermes turn failed"}
	srv := httptest.NewServer(gw)
	defer srv.Close()
	sessions := &deletingSessions{fakeSessions: &fakeSessions{}}
	k := newKagentOperator(t, srv.URL, sessions.fakeSessions)
	k.Sessions = sessions
	ctx := context.Background()

	if _, err := k.Turn(ctx, opThread, "m1", "p"); err == nil || ContentBlocked(err) {
		t.Fatalf("plain failure = %v", err)
	}
	if len(sessions.deleted) != 0 {
		t.Fatalf("a plain failure deleted %v", sessions.deleted)
	}

	gw.mu.Lock()
	gw.fail = "Hermes turn failed: " + ReasonContentPolicy
	gw.mu.Unlock()
	first := "sess-" + OperatorRequestID(opThread, 0)
	if _, err := k.Turn(ctx, opThread, "m2", "p"); !ContentBlocked(err) {
		t.Fatalf("blocked turn = %v", err)
	}
	if len(sessions.deleted) != 1 || sessions.deleted[0] != first {
		t.Fatalf("deleted %v, want %s", sessions.deleted, first)
	}
	s, err := k.Session(ctx, opThread)
	if err != nil || !s.Fresh || s.ContextID != "sess-"+OperatorRequestID(opThread, 1) {
		t.Errorf("next session = %+v, %v; want a fresh generation 1", s, err)
	}
}
