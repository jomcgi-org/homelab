package store_test

import (
	"context"
	"errors"
	"os"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/internal/pgtest"
	"github.com/jomcgi/homelab/projects/factory/connector/migrations"
	"github.com/jomcgi/homelab/projects/factory/connector/store"
)

func TestMain(m *testing.M) { os.Exit(pgtest.Main(m)) }

func open(t *testing.T) *store.Store {
	t.Helper()
	st, err := store.Open(pgtest.NewDB(t))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { st.DB.Close() })
	return st
}

func TestMigrationsAreIdempotentAndCreateTables(t *testing.T) {
	st := open(t)
	ctx := context.Background()
	if err := migrations.Apply(ctx, st.DB); err != nil {
		t.Fatal(err)
	}
	for _, table := range []string{
		"repo_binding", "work_item", "run", "delivery_attempt", "pull_request",
		"review_event", "cancellation", "outbox", "writer", "coordinator_auth_failure",
	} {
		var ok bool
		if err := st.DB.QueryRow(`SELECT to_regclass($1) IS NOT NULL`, table).Scan(&ok); err != nil || !ok {
			t.Errorf("table %s missing (%v)", table, err)
		}
	}
}

func TestSingleWriterAndEpoch(t *testing.T) {
	st := open(t)
	ctx := context.Background()
	a, err := st.TryAcquireWriter(ctx, "a")
	if err != nil || a == nil {
		t.Fatalf("a: %v %v", a, err)
	}
	if b, err := st.TryAcquireWriter(ctx, "b"); err != nil || b != nil {
		t.Fatalf("b acquired while a holds the lock: %v %v", b, err)
	}
	if err := a.Check(ctx); err != nil {
		t.Fatal(err)
	}
	a.Release()
	var b *store.Writer
	for deadline := time.Now().Add(5 * time.Second); b == nil && time.Now().Before(deadline); {
		if b, err = st.TryAcquireWriter(ctx, "b"); err != nil {
			t.Fatal(err)
		}
		time.Sleep(20 * time.Millisecond)
	}
	if b == nil || b.Epoch != a.Epoch+1 {
		t.Fatalf("b = %+v, want epoch %d", b, a.Epoch+1)
	}
	defer b.Release()
	if err := a.Check(ctx); !errors.Is(err, store.ErrNotWriter) {
		t.Fatalf("released writer check = %v", err)
	}
}

func TestRecordingAMessageIDTwiceReturnsTheAttempt(t *testing.T) {
	st := open(t)
	ctx := context.Background()
	if _, _, err := st.RecordClaim(ctx, store.ClaimedRun{ID: 1, AgentKey: "coder", Trigger: "mention", Attempt: 1}); err != nil {
		t.Fatal(err)
	}
	a := store.Attempt{
		RunID: 1, RunAttempt: 1, WorkItemEpoch: 1, DecisionID: "initial", Kind: "initial",
		MessageID: "itsaplan-run/1/initial", KagentNamespace: "ns", KagentAgent: "coder", WriterEpoch: 1,
	}
	first, err := st.RecordAttempt(ctx, st.DB, a)
	if err != nil {
		t.Fatal(err)
	}
	again, err := st.RecordAttempt(ctx, st.DB, a)
	if err != nil || again.ID != first.ID {
		t.Fatalf("recording the same message ID = %+v, %v; want the existing attempt", again, err)
	}
	if n, err := st.CountSends(ctx, 1); err != nil || n != 1 {
		t.Fatalf("attempts = %d, %v", n, err)
	}
}

// Windows survive a new store and isolate operations, owners and attempts.
func TestCoordinatorLookupWindows(t *testing.T) {
	st := open(t)
	ctx := context.Background()
	first := time.Date(2026, 11, 14, 12, 0, 0, 0, time.UTC)
	if at, err := st.RecordLookupOutcome(ctx, "run_check", 1, 1, true, first); err != nil || !at.Equal(first) {
		t.Fatalf("first refusal = %s, %v", at, err)
	}
	st = &store.Store{DB: st.DB}
	later := first.Add(72 * time.Hour)
	for _, k := range []struct {
		operation string
		id        int64
		attempt   int
		want      time.Time
	}{
		{"run_check", 1, 1, first},
		{"run_check", 1, 2, later},
		{"run_check", 2, 1, later},
		{"replacement", 1, 0, later},
	} {
		if at, err := st.RecordLookupOutcome(ctx, k.operation, k.id, k.attempt, true, later); err != nil || !at.Equal(k.want) {
			t.Fatalf("lookup %+v = %s, %v", k, at, err)
		}
	}
	if _, err := st.RecordLookupOutcome(ctx, "run_check", 1, 1, false, later); err != nil {
		t.Fatal(err)
	}
	if at, err := st.RecordLookupOutcome(ctx, "run_check", 1, 1, true, later); err != nil || !at.Equal(later) {
		t.Fatalf("new episode = %s, %v", at, err)
	}
	if at, err := st.RecordLookupOutcome(ctx, "replacement", 1, 0, true, later.Add(time.Hour)); err != nil || !at.Equal(later) {
		t.Fatalf("sibling reset = %s, %v", at, err)
	}
}

func TestClosingRunClearsOnlyItsLookupWindowsAtomically(t *testing.T) {
	st := open(t)
	ctx := context.Background()
	first := time.Date(2026, 11, 14, 12, 0, 0, 0, time.UTC)
	for _, id := range []int64{1, 2} {
		if _, _, err := st.RecordClaim(ctx, store.ClaimedRun{ID: id, AgentKey: "coder", Trigger: "mention", Attempt: 1}); err != nil {
			t.Fatal(err)
		}
		if _, err := st.RecordLookupOutcome(ctx, "run_check", id, 1, true, first); err != nil {
			t.Fatal(err)
		}
	}
	a, err := st.RecordAttempt(ctx, st.DB, store.Attempt{
		RunID: 1, RunAttempt: 1, WorkItemEpoch: 1, DecisionID: "initial", Kind: "initial",
		MessageID: "itsaplan-run/1/initial", KagentNamespace: "ns", KagentAgent: "coder", WriterEpoch: 1,
	})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := st.RecordLookupOutcome(ctx, "replacement", a.ID, 0, true, first); err != nil {
		t.Fatal(err)
	}
	count := func(want int) {
		t.Helper()
		var got int
		if err := st.DB.QueryRow(`SELECT count(*) FROM coordinator_auth_failure`).Scan(&got); err != nil || got != want {
			t.Fatalf("windows = %d, %v, want %d", got, err, want)
		}
	}
	tx, err := st.DB.BeginTx(ctx, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	if err := st.SetRunState(ctx, tx, 1, store.RunClosed, "test"); err != nil {
		t.Fatal(err)
	}
	if err := tx.Rollback(); err != nil {
		t.Fatal(err)
	}
	count(3)
	if r, err := st.GetRun(ctx, 1); err != nil || r.State != store.RunClaimed {
		t.Fatalf("rolled-back run = %+v, %v", r, err)
	}
	if err := st.SetRunState(ctx, st.DB, 1, store.RunFinishing, ""); err != nil {
		t.Fatal(err)
	}
	count(3)
	if err := st.SetRunState(ctx, st.DB, 1, store.RunClosed, "test"); err != nil {
		t.Fatal(err)
	}
	count(1)
	if at, err := st.RecordLookupOutcome(ctx, "run_check", 2, 1, true, first.Add(time.Hour)); err != nil || !at.Equal(first) {
		t.Fatalf("other run's window = %s, %v", at, err)
	}
}
