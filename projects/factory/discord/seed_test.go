package discord

import (
	"context"
	"errors"
	"fmt"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
)

// freshSessionAgent is a fakeSessionAgent whose sessions are new until
// their first turn.
type freshSessionAgent struct {
	fakeSessionAgent
	used map[string]bool
}

func (f *freshSessionAgent) Session(_ context.Context, key string) (OperatorSession, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return OperatorSession{ContextID: "s-" + key, Fresh: !f.used[key]}, nil
}

func (f *freshSessionAgent) Turn(ctx context.Context, key, id, prompt string, images ...TurnImage) (string, error) {
	f.mu.Lock()
	if f.used == nil {
		f.used = map[string]bool{}
	}
	f.used[key] = true
	f.mu.Unlock()
	return f.fakeSessionAgent.Turn(ctx, key, id, prompt, images...)
}

func (f *freshSessionAgent) lastPrompt() string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.prompts[len(f.prompts)-1]
}

// seedLines reads a seeded prompt's channel lines; seeded reports whether
// the prompt starts with SeedNote.
func seedLines(t *testing.T, prompt string) (lines []string, seeded bool) {
	t.Helper()
	rest, seeded := strings.CutPrefix(prompt, SeedNote+"\n\n")
	return texts(sinceLines(t, rest)), seeded
}

// history builds a channel page, newest first: n messages, every fifth one
// Bosun's answer.
func seedHistory(n int) []HistoryMessage {
	var msgs []HistoryMessage
	for i := n; i >= 1; i-- {
		id := fmt.Sprint(100 + i)
		if i%5 == 0 {
			h := bosunSaid(id, i%60)
			h.Components = []Component{{Type: componentText, Content: "answer " + id}}
			msgs = append(msgs, h)
			continue
		}
		msgs = append(msgs, said(id, "ann", "line "+id, i%60))
	}
	return msgs
}

// The first turn of a new channel session (HOME-944) gets the channel's last
// maxSeedLines messages, Bosun's own included, not only those since Bosun's
// last reply. A later turn in that session gets only those since.
func TestANewChannelSessionIsSeededWithTheRecentThread(t *testing.T) {
	b, _, _ := ingestBridge(&fakeAgent{})
	b.Config.UserLimit.Count = 10
	server := &freshSessionAgent{}
	b.Servers = map[string]SessionAgent{guild: server}
	b.History = &channelHistory{msgs: seedHistory(40)}
	ctx := context.Background()

	b.Turn(ctx, tagged("200", "where were we"))
	got, seeded := seedLines(t, server.lastPrompt())
	if !seeded || len(got) != maxSeedLines {
		t.Fatalf("seeded=%v, %d lines; want the seed note and %d lines: %q", seeded, len(got), maxSeedLines, got)
	}
	if got[0] != "ann: line 121" || got[len(got)-1] != OwnAuthor+": answer 140" {
		t.Errorf("lines run %q .. %q, want line 121 .. answer 140", got[0], got[len(got)-1])
	}
	own := 0
	for _, l := range got {
		if strings.HasPrefix(l, OwnAuthor+": ") {
			own++
		}
	}
	if own != 4 {
		t.Errorf("%d of Bosun's messages, want 4: %q", own, got)
	}

	b.Turn(ctx, tagged("201", "and then"))
	got, seeded = seedLines(t, server.lastPrompt())
	if seeded || len(got) != 1 || got[0] != OwnAuthor+": answer 140" {
		t.Errorf("later turn: seeded=%v, lines %q; want only Bosun's last answer", seeded, got)
	}
}

// The seed stays within maxSinceChars, keeping the newest lines.
func TestTheSeedIsBoundedInCharacters(t *testing.T) {
	b, _, _ := ingestBridge(&fakeAgent{})
	server := &freshSessionAgent{}
	b.Servers = map[string]SessionAgent{guild: server}
	var msgs []HistoryMessage
	for i := 30; i >= 1; i-- {
		msgs = append(msgs, said(fmt.Sprint(100+i), "ann", strings.Repeat("x", maxLineChars), i))
	}
	b.History = &channelHistory{msgs: msgs}
	b.Turn(context.Background(), tagged("200", "hi"))
	got, _ := seedLines(t, server.lastPrompt())
	chars := 0
	for _, l := range got {
		chars += len(l) - len("ann: ")
	}
	if want := maxSinceChars / maxLineChars; len(got) != want || chars > maxSinceChars {
		t.Errorf("%d lines, %d chars; want %d lines within %d chars", len(got), chars, want, maxSinceChars)
	}
}

// A seed reads no further back than Bosun's newest blocked reply: what came
// before it may hold what the filter refused, even after a restart empties
// the blocked set (review of #8821). A blocked message after it, and a
// reply's quote of it, stay out too.
func TestTheSeedLeavesBlockedMessagesOut(t *testing.T) {
	for _, restarted := range []bool{false, true} {
		b, _, _ := ingestBridge(&fakeAgent{})
		server := &freshSessionAgent{}
		b.Servers = map[string]SessionAgent{guild: server}
		if !restarted {
			b.block("102")
			b.block("106")
		}
		refusal := bosunSaid("103", 3)
		refusal.Components = []Component{{Type: componentText, Content: ReplyBlocked}}
		reply := said("107", "dave", "ha", 7)
		reply.Type = 19
		reply.Referenced = &Referenced{ID: "106", Author: User{ID: "id-ann", Username: "ann"}, Content: "a second refused joke"}
		msgs := []HistoryMessage{reply, said("105", "ann", "after", 5), refusal, said("102", "ann", "the refused joke", 2), said("101", "ann", "hello", 1)}
		if restarted {
			// Bosun answers each blocked message, so after a restart the
			// newest refusal is newer than every blocked message.
			reply.Referenced = &Referenced{ID: "105", Author: User{ID: "id-ann", Username: "ann"}, Content: "after"}
			msgs[0] = reply
		} else {
			msgs = append([]HistoryMessage{said("106", "ann", "a second refused joke", 6)}, msgs...)
			msgs[0], msgs[1] = msgs[1], msgs[0] // 107 is newest, then 106.
		}
		b.History = &channelHistory{msgs: msgs}
		b.Turn(context.Background(), tagged("200", "hi"))
		prompt := server.lastPrompt()
		if strings.Contains(prompt, "refused joke") || strings.Contains(prompt, "hello") {
			t.Errorf("restarted=%v: blocked or earlier content reached the seed: %s", restarted, prompt)
		}
		got, seeded := seedLines(t, prompt)
		if !seeded || len(got) != 2 || got[0] != "ann: after" || got[1] != "dave: ha" {
			t.Errorf("restarted=%v: lines = %q", restarted, got)
		}
	}
}

// fakeRevisions is kagent's Agent revision.
type fakeRevisions struct {
	mu    sync.Mutex
	rev   string
	err   error
	reads int
}

func (f *fakeRevisions) AgentRevision(context.Context, kagent.AgentRef) (string, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.reads++
	return f.rev, f.err
}

func (f *fakeRevisions) set(rev string, err error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.rev, f.err = rev, err
}

// The epoch is a function of the manual epoch and the Agent revision, and
// fits the sessionEpoch limits and kagent's request ID length.
func TestDeriveEpoch(t *testing.T) {
	a := DeriveEpoch("", "homelab-rev-7")
	if !sessionEpoch.MatchString(a) || len(a) != 12 {
		t.Fatalf("epoch %q is outside the sessionEpoch limits", a)
	}
	if DeriveEpoch("", "homelab-rev-7") != a {
		t.Error("the same inputs give another epoch")
	}
	if DeriveEpoch("", "homelab-rev-8") == a || DeriveEpoch("notes1", "homelab-rev-7") == a {
		t.Error("a new revision or manual epoch keeps the epoch")
	}
	id := EpochChannelRequestID(a)("150196585204233030111", maxSessionGenerations-1)
	if len(id) > 128 {
		t.Errorf("request ID %q is over kagent's 128 characters", id)
	}
}

// RevisionEpoch reads the revision at most once a minute, keeps its last
// epoch when a read fails, and fails with no epoch yet.
func TestRevisionEpochCachesAndKeepsTheLastEpoch(t *testing.T) {
	revs := &fakeRevisions{err: errors.New("kagent down")}
	now := time.Date(2026, 11, 28, 12, 0, 0, 0, time.UTC)
	r := &RevisionEpoch{Revisions: revs, Manual: "notes1", Now: func() time.Time { return now }}
	ctx := context.Background()
	if _, err := r.Epoch(ctx); err == nil {
		t.Fatal("an epoch with no revision read")
	}
	revs.set("", nil)
	if _, err := r.Epoch(ctx); !errors.Is(err, errNoRevision) {
		t.Fatalf("an Agent with no revision = %v", err)
	}
	revs.set("rev-1", nil)
	first, err := r.Epoch(ctx)
	if err != nil || first != DeriveEpoch("notes1", "rev-1") {
		t.Fatalf("epoch = %q, %v", first, err)
	}
	revs.set("rev-2", nil)
	if e, _ := r.Epoch(ctx); e != first {
		t.Error("read the revision again inside the minute")
	}
	now = now.Add(epochTTL)
	second, _ := r.Epoch(ctx)
	if second != DeriveEpoch("notes1", "rev-2") {
		t.Errorf("epoch after a new revision = %q", second)
	}
	revs.set("", errors.New("kagent down"))
	now = now.Add(epochTTL)
	if e, err := r.Epoch(ctx); err != nil || e != second {
		t.Errorf("a failed read = %q, %v; want the last epoch", e, err)
	}
}

// A new Agent revision moves each channel to a new session on that
// channel's next turn, starting at generation 0, and retires the old
// session. A channel without a turn keeps its session.
func TestANewRevisionMovesEachChannelOnItsNextTurn(t *testing.T) {
	gw := &a2aServer{}
	srv := httptest.NewServer(gw)
	defer srv.Close()
	sessions := &deletingSessions{fakeSessions: &fakeSessions{}}
	k := newKagentOperator(t, srv.URL, sessions.fakeSessions)
	k.Sessions = sessions
	revs := &fakeRevisions{rev: "rev-1"}
	now := time.Date(2026, 11, 28, 12, 0, 0, 0, time.UTC)
	epoch := &RevisionEpoch{Revisions: revs, Agent: k.Agent, Now: func() time.Time { return now }}
	k.Epoch = epoch.Epoch
	ctx := context.Background()
	const a, c = "301", "302"
	e1 := DeriveEpoch("", "rev-1")
	for _, ch := range []string{a, c} {
		if _, err := k.Turn(ctx, ch, "m-"+ch, "p"); err != nil {
			t.Fatal(err)
		}
	}
	oldA := "sess-" + EpochChannelRequestID(e1)(a, 0)
	if s, _ := k.Session(ctx, a); s.ContextID != oldA {
		t.Fatalf("session = %s, want %s", s.ContextID, oldA)
	}

	revs.set("rev-2", nil)
	now = now.Add(epochTTL)
	s, err := k.Session(ctx, a)
	want := "sess-" + EpochChannelRequestID(DeriveEpoch("", "rev-2"))(a, 0)
	if err != nil || s.ContextID != want || !s.Fresh {
		t.Fatalf("after the new revision: %+v, %v; want a fresh %s", s, err, want)
	}
	waitFor(t, func() bool {
		sessions.mu.Lock()
		defer sessions.mu.Unlock()
		return len(sessions.deleted) == 1 && sessions.deleted[0] == oldA
	})
	sessions.mu.Lock()
	for _, req := range sessions.requests {
		if strings.Contains(req, c) && !strings.Contains(req, e1) {
			t.Errorf("channel %s moved before its turn: %s", c, req)
		}
	}
	sessions.mu.Unlock()
}

// A blocked turn's next generation stays inside the epoch: a new epoch
// starts at generation 0 again.
func TestABlockedTurnBumpsTheGenerationInItsEpoch(t *testing.T) {
	gw := &a2aServer{fail: "Hermes turn failed: " + ReasonContentPolicy}
	srv := httptest.NewServer(gw)
	defer srv.Close()
	k := newKagentOperator(t, srv.URL, &fakeSessions{})
	revs := &fakeRevisions{rev: "rev-1"}
	now := time.Date(2026, 11, 28, 12, 0, 0, 0, time.UTC)
	k.Epoch = (&RevisionEpoch{Revisions: revs, Agent: k.Agent, Now: func() time.Time { return now }}).Epoch
	ctx := context.Background()
	if _, err := k.Turn(ctx, "301", "m1", "p"); !ContentBlocked(err) {
		t.Fatalf("turn = %v", err)
	}
	e1 := DeriveEpoch("", "rev-1")
	if s, _ := k.Session(ctx, "301"); s.ContextID != "sess-"+EpochChannelRequestID(e1)("301", 1) {
		t.Errorf("after a block: %s, want generation 1", s.ContextID)
	}
	revs.set("rev-2", nil)
	now = now.Add(epochTTL)
	if s, _ := k.Session(ctx, "301"); s.ContextID != "sess-"+EpochChannelRequestID(DeriveEpoch("", "rev-2"))("301", 0) {
		t.Errorf("new epoch: %s, want generation 0", s.ContextID)
	}
}
