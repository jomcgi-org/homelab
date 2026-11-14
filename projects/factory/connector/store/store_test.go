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
		"review_event", "cancellation", "outbox", "writer",
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
