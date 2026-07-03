package whatsapp

import (
	"context"
	"database/sql"
	"errors"
	"io"
	"log/slog"
	"sort"
	"testing"
	"time"
)

// --- fakes -----------------------------------------------------------------

type sendCall struct {
	method   string // "text" | "edit" | "reaction"
	groupJID string
	arg1     string // text: content;  edit: targetMessageID;  reaction: targetMessageID
	arg2     string // text: quotedID; edit: newContent;       reaction: targetSenderJID
	arg3     string // reaction: reaction string
}

type fakeSender struct {
	calls       []sendCall
	textErr     error
	editErr     error
	reactionErr error
	sentID      string
}

func (f *fakeSender) SendText(_ context.Context, groupJID, content, quotedMessageID string) (string, error) {
	f.calls = append(f.calls, sendCall{"text", groupJID, content, quotedMessageID, ""})
	if f.textErr != nil {
		return "", f.textErr
	}
	id := f.sentID
	if id == "" {
		id = "wamid.SENT"
	}
	return id, nil
}

func (f *fakeSender) SendEdit(_ context.Context, groupJID, targetMessageID, newContent string) error {
	f.calls = append(f.calls, sendCall{"edit", groupJID, targetMessageID, newContent, ""})
	return f.editErr
}

func (f *fakeSender) SendReaction(_ context.Context, groupJID, targetMessageID, targetSenderJID, reaction string) error {
	f.calls = append(f.calls, sendCall{"reaction", groupJID, targetMessageID, targetSenderJID, reaction})
	return f.reactionErr
}

type fakeRow struct {
	id              int64
	groupJID        string
	kind            string
	content         string
	quotedMessageID string
	editOf          sql.NullInt64
	targetMessageID string
	targetSenderJID string
	reaction        string
	reactionRemove  bool
	sentMessageID   string
	createdAt       time.Time
	posted          bool
	attempts        int
	lastError       string
}

type fakeStore struct {
	rows []*fakeRow
}

func nullStr(s string) sql.NullString {
	return sql.NullString{String: s, Valid: s != ""}
}

func (s *fakeStore) claimPending(_ context.Context) ([]outboxRow, error) {
	var pending []*fakeRow
	for _, r := range s.rows {
		if !r.posted && r.attempts < outboxMaxAttempts {
			pending = append(pending, r)
		}
	}
	sort.SliceStable(pending, func(i, j int) bool {
		if pending[i].groupJID != pending[j].groupJID {
			return pending[i].groupJID < pending[j].groupJID
		}
		if !pending[i].createdAt.Equal(pending[j].createdAt) {
			return pending[i].createdAt.Before(pending[j].createdAt)
		}
		return pending[i].id < pending[j].id
	})
	if len(pending) > outboxBatch {
		pending = pending[:outboxBatch]
	}
	out := make([]outboxRow, 0, len(pending))
	for _, r := range pending {
		out = append(out, outboxRow{
			id:              r.id,
			groupJID:        r.groupJID,
			kind:            r.kind,
			content:         nullStr(r.content),
			quotedMessageID: nullStr(r.quotedMessageID),
			editOf:          r.editOf,
			targetMessageID: nullStr(r.targetMessageID),
			targetSenderJID: nullStr(r.targetSenderJID),
			reaction:        nullStr(r.reaction),
			reactionRemove:  r.reactionRemove,
		})
	}
	return out, nil
}

func (s *fakeStore) find(id int64) *fakeRow {
	for _, r := range s.rows {
		if r.id == id {
			return r
		}
	}
	return nil
}

func (s *fakeStore) sentMessageID(_ context.Context, id int64) (string, error) {
	if r := s.find(id); r != nil {
		return r.sentMessageID, nil
	}
	return "", nil
}

func (s *fakeStore) markPosted(_ context.Context, id int64, sentMessageID string) error {
	r := s.find(id)
	if r == nil {
		return errors.New("no such row")
	}
	r.posted = true
	if sentMessageID != "" {
		r.sentMessageID = sentMessageID
	}
	return nil
}

func (s *fakeStore) markFailed(_ context.Context, id int64, errMsg string) error {
	r := s.find(id)
	if r == nil {
		return errors.New("no such row")
	}
	r.attempts++
	r.lastError = errMsg
	return nil
}

func (s *fakeStore) markEditExpired(_ context.Context, id int64) error {
	r := s.find(id)
	if r == nil {
		return errors.New("no such row")
	}
	r.posted = true
	r.lastError = "edit_window_expired"
	return nil
}

func testDrain(st store, snd sender) *OutboxDrain {
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	return newOutboxDrain(st, snd, func() State { return StateConnected }, log)
}

// --- tests -----------------------------------------------------------------

func TestDrainMessageStampsSentIDAndPosted(t *testing.T) {
	st := &fakeStore{rows: []*fakeRow{
		{id: 1, groupJID: "g@wa", kind: "message", content: "hello", createdAt: time.Unix(1, 0)},
	}}
	snd := &fakeSender{sentID: "wamid.ABC"}
	if err := testDrain(st, snd).drainOnce(context.Background()); err != nil {
		t.Fatal(err)
	}
	if len(snd.calls) != 1 || snd.calls[0].method != "text" || snd.calls[0].arg1 != "hello" {
		t.Fatalf("expected one text send of 'hello', got %+v", snd.calls)
	}
	r := st.find(1)
	if !r.posted || r.sentMessageID != "wamid.ABC" {
		t.Fatalf("message row not stamped: posted=%v sent=%q", r.posted, r.sentMessageID)
	}
}

func TestDrainMessageQuotedPassesQuoteID(t *testing.T) {
	st := &fakeStore{rows: []*fakeRow{
		{id: 1, groupJID: "g@wa", kind: "message", content: "re", quotedMessageID: "Q1", createdAt: time.Unix(1, 0)},
	}}
	snd := &fakeSender{}
	_ = testDrain(st, snd).drainOnce(context.Background())
	if snd.calls[0].arg2 != "Q1" {
		t.Fatalf("expected quoted id Q1, got %q", snd.calls[0].arg2)
	}
}

func TestDrainEditResolvesOriginalSentID(t *testing.T) {
	st := &fakeStore{rows: []*fakeRow{
		{id: 1, groupJID: "g@wa", kind: "message", content: "v1", sentMessageID: "wamid.ORIG", posted: true, createdAt: time.Unix(1, 0)},
		{id: 2, groupJID: "g@wa", kind: "edit", content: "v2", editOf: sql.NullInt64{Int64: 1, Valid: true}, createdAt: time.Unix(2, 0)},
	}}
	snd := &fakeSender{}
	if err := testDrain(st, snd).drainOnce(context.Background()); err != nil {
		t.Fatal(err)
	}
	if len(snd.calls) != 1 || snd.calls[0].method != "edit" {
		t.Fatalf("expected one edit send, got %+v", snd.calls)
	}
	if snd.calls[0].arg1 != "wamid.ORIG" || snd.calls[0].arg2 != "v2" {
		t.Fatalf("edit did not resolve original id / new content: %+v", snd.calls[0])
	}
	if r := st.find(2); !r.posted {
		t.Fatalf("edit row not marked posted")
	}
}

func TestDrainEditWindowExpiredConsumesRow(t *testing.T) {
	st := &fakeStore{rows: []*fakeRow{
		{id: 1, groupJID: "g@wa", kind: "message", content: "v1", sentMessageID: "wamid.ORIG", posted: true, createdAt: time.Unix(1, 0)},
		{id: 2, groupJID: "g@wa", kind: "edit", content: "v2", editOf: sql.NullInt64{Int64: 1, Valid: true}, createdAt: time.Unix(2, 0)},
	}}
	snd := &fakeSender{editErr: errors.New("server ignored late edit")}
	d := testDrain(st, snd)
	// Two ticks: an edit failure must NOT be retried on the second tick.
	_ = d.drainOnce(context.Background())
	_ = d.drainOnce(context.Background())
	r := st.find(2)
	if !r.posted || r.lastError != "edit_window_expired" {
		t.Fatalf("edit not consumed as window-expired: posted=%v last_error=%q", r.posted, r.lastError)
	}
	if r.attempts != 0 {
		t.Fatalf("edit window failure should not increment attempts, got %d", r.attempts)
	}
	editCalls := 0
	for _, c := range snd.calls {
		if c.method == "edit" {
			editCalls++
		}
	}
	if editCalls != 1 {
		t.Fatalf("expected exactly one edit attempt (no retry), got %d", editCalls)
	}
}

func TestDrainReactionAddAndRemove(t *testing.T) {
	st := &fakeStore{rows: []*fakeRow{
		{id: 1, groupJID: "g@wa", kind: "reaction", targetMessageID: "M1", targetSenderJID: "a@wa", reaction: "\U0001f44d", createdAt: time.Unix(1, 0)},
		{id: 2, groupJID: "g@wa", kind: "reaction", targetMessageID: "M1", targetSenderJID: "a@wa", reaction: "\U0001f44d", reactionRemove: true, createdAt: time.Unix(2, 0)},
	}}
	snd := &fakeSender{}
	if err := testDrain(st, snd).drainOnce(context.Background()); err != nil {
		t.Fatal(err)
	}
	if len(snd.calls) != 2 {
		t.Fatalf("expected two reaction sends, got %d", len(snd.calls))
	}
	if snd.calls[0].arg3 != "\U0001f44d" {
		t.Fatalf("add reaction should carry the emoji, got %q", snd.calls[0].arg3)
	}
	if snd.calls[1].arg3 != "" {
		t.Fatalf("remove reaction should send an empty string, got %q", snd.calls[1].arg3)
	}
}

func TestDrainParkedRowDoesNotBlockOtherGroup(t *testing.T) {
	st := &fakeStore{rows: []*fakeRow{
		{id: 1, groupJID: "a@wa", kind: "message", content: "fails", createdAt: time.Unix(1, 0)},
		{id: 2, groupJID: "b@wa", kind: "message", content: "ok", createdAt: time.Unix(1, 0)},
	}}
	// SendText always fails: row 1 can never post. Row 2 is in another group.
	snd := &fakeSender{textErr: errors.New("boom")}
	d := testDrain(st, snd)
	_ = d.drainOnce(context.Background())
	// Row 2 (group b) posts on the first tick even though row 1 (group a, ordered
	// first) failed: a poison row blocks only its own group.
	if !st.find(2).posted {
		t.Fatalf("row in group b was blocked by failing row in group a")
	}
	// Drive until row 1 parks (attempts == max) and is excluded from claims.
	for i := 0; i < outboxMaxAttempts+2; i++ {
		_ = d.drainOnce(context.Background())
	}
	r1 := st.find(1)
	if r1.posted {
		t.Fatalf("failing row should never be posted")
	}
	if r1.attempts != outboxMaxAttempts {
		t.Fatalf("failing row should park at %d attempts, got %d", outboxMaxAttempts, r1.attempts)
	}
	// Once parked, claimPending must not return it (no poison-pill loop).
	pending, _ := st.claimPending(context.Background())
	for _, p := range pending {
		if p.id == 1 {
			t.Fatalf("parked row 1 still claimed")
		}
	}
}

func TestDrainPerGroupOldestFirst(t *testing.T) {
	st := &fakeStore{rows: []*fakeRow{
		// Deliberately out of insertion order in the slice; claimPending must sort.
		{id: 2, groupJID: "g@wa", kind: "message", content: "second", createdAt: time.Unix(2, 0)},
		{id: 1, groupJID: "g@wa", kind: "message", content: "first", createdAt: time.Unix(1, 0)},
	}}
	snd := &fakeSender{}
	if err := testDrain(st, snd).drainOnce(context.Background()); err != nil {
		t.Fatal(err)
	}
	if len(snd.calls) != 2 || snd.calls[0].arg1 != "first" || snd.calls[1].arg1 != "second" {
		t.Fatalf("group not drained oldest-first: %+v", snd.calls)
	}
}

func TestDrainFailureHoldsGroupOrder(t *testing.T) {
	// If the oldest row in a group fails, the group's later rows wait (order is
	// preserved) rather than jumping ahead of the stuck send.
	st := &fakeStore{rows: []*fakeRow{
		{id: 1, groupJID: "g@wa", kind: "message", content: "first", createdAt: time.Unix(1, 0)},
		{id: 2, groupJID: "g@wa", kind: "message", content: "second", createdAt: time.Unix(2, 0)},
	}}
	snd := &fakeSender{textErr: errors.New("boom")}
	_ = testDrain(st, snd).drainOnce(context.Background())
	if len(snd.calls) != 1 || snd.calls[0].arg1 != "first" {
		t.Fatalf("expected only the oldest row attempted, got %+v", snd.calls)
	}
	if st.find(2).attempts != 0 {
		t.Fatalf("later same-group row should be held, not attempted")
	}
}
