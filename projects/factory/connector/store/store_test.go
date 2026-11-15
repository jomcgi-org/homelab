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
		"review_event", "delivery_read", "cancellation", "outbox", "writer",
		"human_gate_issue", "human_gate_attempt",
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

func TestRunStopMigrationPreservesExistingStops(t *testing.T) {
	for _, tc := range []struct {
		name, setup, reason string
	}{
		{"stopped", `UPDATE work_item SET state = 'stopped', stopped_reason = 'stopped before upgrade'`, "stopped before upgrade"},
		{"reassigned", `DELETE FROM delivery_attempt; UPDATE work_item SET agent_key = 'coder-b', epoch = epoch + 1`, "superseded: the work item was reassigned to another agent"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			f := newRecoveryFixture(t)
			_, err := f.st.DB.Exec(tc.setup)
			f.must(err)
			_, err = f.st.DB.Exec(`
				ALTER TABLE run DROP COLUMN stopped_reason;
				DELETE FROM schema_migrations WHERE version = '20261116120000_run_stops'`)
			f.must(err)
			f.must(migrations.Apply(f.ctx, f.st.DB))
			f.must(migrations.Apply(f.ctx, f.st.DB))
			r, err := f.st.GetRun(f.ctx, 1)
			f.must(err)
			if !r.StopRequested || r.StoppedReason != tc.reason {
				t.Fatalf("run lost its existing stop during upgrade: %+v", r)
			}
		})
	}
}

func TestSupersededUnsentRunCannotBorrowCurrentEpoch(t *testing.T) {
	f := newRecoveryFixture(t)
	// Start from an admitted run that has never recorded a delivery attempt.
	_, err := f.st.DB.Exec(`DELETE FROM delivery_attempt`)
	f.must(err)
	work := f.work
	for i, agent := range []string{"coder-b", "coder"} {
		id := int64(i + 2)
		_, _, err := f.st.RecordClaim(f.ctx, store.ClaimedRun{ID: id, AgentKey: agent, Attempt: 1})
		f.must(err)
		work, _, err = f.st.ResolveWorkItem(f.ctx, id, store.NewWorkItem{IssueID: 101, IssueKey: "HL-101", AgentKey: agent})
		f.must(err)
	}
	// Simulate a dispatcher that already loaded the resumed work item. Its
	// fresh epoch and matching agent key cannot restore the old admission.
	a, err := f.st.RecordAttempt(f.ctx, f.st.DB, store.Attempt{
		RunID: 1, RunAttempt: 1, WorkItemEpoch: work.Epoch, DecisionID: "initial", Kind: "initial",
		MessageID: "late-initial", KagentNamespace: "ns", KagentAgent: "coder", WriterEpoch: f.w.Epoch,
	})
	f.must(err)
	f.must(f.st.SetRunState(f.ctx, f.st.DB, 1, store.RunDispatched, ""))
	_, _, err = f.st.RecordClaim(f.ctx, store.ClaimedRun{ID: 1, AgentKey: "coder", Attempt: 2})
	f.must(err)
	if issued, err := f.st.MarkIssued(f.ctx, f.w, a, 2, f.now); err != nil || issued {
		t.Fatalf("superseded run send admitted: %t, %v", issued, err)
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
