package store_test

import (
	"context"
	"database/sql"
	"fmt"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/migrations"
	"github.com/jomcgi/homelab/projects/factory/connector/store"
)

func TestEnqueueDeferredOnlyRecoversRollbackAbandonment(t *testing.T) {
	for _, tc := range []struct {
		name, kind, reason string
		delivered, recover bool
	}{
		{"rollback", store.OutboxDeferredStart, "unknown kind deferred_start", false, true},
		{"stopped", store.OutboxDeferredStart, "deferred issue stopped", false, false},
		{"binding changed", store.OutboxDeferredStart, "deferred binding changed", false, false},
		{"ineligible", store.OutboxDeferredStart, "itsaplan: conflict", false, false},
		{"retries exhausted", store.OutboxDeferredStart, "connection refused", false, false},
		{"delivered", store.OutboxDeferredStart, "unknown kind deferred_start", true, false},
		{"other kind", store.OutboxComment, "unknown kind deferred_start", false, false},
		{"pending", store.OutboxDeferredStart, "", false, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			st, ctx := open(t), t.Context()
			now := time.Date(2026, 11, 16, 0, 0, 0, 0, time.UTC)
			st.Now = func() time.Time { return now }
			const key = "deferred/118/2026-11-16"
			if err := st.Enqueue(ctx, st.DB, tc.kind, key, 0, "", map[string]string{"delegate": "original"}); err != nil {
				t.Fatal(err)
			}
			var id int64
			if err := st.DB.QueryRow(`SELECT id FROM outbox WHERE dedupe_key = $1`, key).Scan(&id); err != nil {
				t.Fatal(err)
			}
			if err := st.OutboxSendStarted(ctx, id); err != nil {
				t.Fatal(err)
			}
			if err := st.OutboxFailed(ctx, id, "waiting", now.Add(time.Hour)); err != nil {
				t.Fatal(err)
			}
			if tc.reason != "" {
				if err := st.OutboxAbandon(ctx, st.DB, id, tc.reason); err != nil {
					t.Fatal(err)
				}
			}
			if tc.delivered {
				if err := st.OutboxDelivered(ctx, st.DB, id); err != nil {
					t.Fatal(err)
				}
			}
			for i := 0; i < 2; i++ {
				if err := enqueueDeferred(ctx, st, key, 118, map[string]string{"delegate": "replacement"}); err != nil {
					t.Fatal(err)
				}
			}
			pending, err := st.PendingOutbox(ctx, now, 10)
			if err != nil || (len(pending) == 1) != tc.recover {
				t.Fatalf("pending = %v, err = %v", pending, err)
			}
			if tc.recover && (pending[0].ID != id || pending[0].Tries != 1 || !pending[0].SendStarted) {
				t.Fatalf("recovery changed the receipt: %+v", pending[0])
			}
			var count int
			if err := st.DB.QueryRow(`SELECT count(*) FROM outbox WHERE dedupe_key = $1 AND payload->>'delegate' = 'original'`, key).Scan(&count); err != nil || count != 1 {
				t.Fatalf("original requests = %d, err = %v", count, err)
			}
		})
	}
}

func TestEnqueueDeferredPreservesStopAcrossRollback(t *testing.T) {
	f := newRecoveryFixture(t)
	const key = "deferred/101/2026-11-15"
	f.must(enqueueDeferred(f.ctx, f.st, key, f.work.IssueID, map[string]string{"delegate": "coder"}))
	var id int64
	f.must(f.st.DB.QueryRow(`SELECT id FROM outbox WHERE dedupe_key = $1`, key).Scan(&id))
	f.must(f.st.OutboxAbandon(f.ctx, f.st.DB, id, "unknown kind deferred_start"))
	_, err := f.st.StopWork(f.ctx, f.work.ID, "operator stop", "operator")
	f.must(err)
	// Neither the existing row nor a fresh date may bypass the stored stop.
	for _, date := range []string{"2026-11-15", "2026-11-16"} {
		f.must(enqueueDeferred(f.ctx, f.st, "deferred/101/"+date, f.work.IssueID, nil))
	}
	var count int
	f.must(f.st.DB.QueryRow(`SELECT count(*) FROM outbox WHERE kind = 'deferred_start' AND abandoned_at IS NULL`).Scan(&count))
	if count != 0 {
		t.Fatalf("stopped starts = %d", count)
	}
}

func TestEnqueueDeferredPreservesUnlinkedStopAcrossRollback(t *testing.T) {
	for _, state := range []string{store.RunWaiting, store.RunClosed} {
		t.Run(state, func(t *testing.T) {
			st, ctx := open(t), t.Context()
			const key = "deferred/118/2026-11-16"
			if err := enqueueDeferred(ctx, st, key, 118, nil); err != nil {
				t.Fatal(err)
			}
			var id int64
			if err := st.DB.QueryRow(`SELECT id FROM outbox WHERE dedupe_key = $1`, key).Scan(&id); err != nil {
				t.Fatal(err)
			}
			if err := st.OutboxAbandon(ctx, st.DB, id, "unknown kind deferred_start"); err != nil {
				t.Fatal(err)
			}
			if _, _, err := st.RecordClaim(ctx, store.ClaimedRun{ID: 1, AgentKey: "coder", IssueID: 118, IssueKey: "HL-118", Attempt: 1}); err != nil {
				t.Fatal(err)
			}
			if stopped, err := st.StopIssue(ctx, "HL-118", "operator stop", "operator"); err != nil || stopped != (store.IssueStop{HeldRuns: 1}) {
				t.Fatalf("cancel = %+v, %v", stopped, err)
			}
			if err := st.SetRunState(ctx, st.DB, 1, state, "operator stop"); err != nil {
				t.Fatal(err)
			}
			// Neither rollback recovery nor a different date clears this stop.
			for _, date := range []string{"2026-11-16", "2026-11-17"} {
				if err := enqueueDeferred(ctx, st, "deferred/118/"+date, 118, nil); err != nil {
					t.Fatal(err)
				}
			}
			var pending int
			if err := st.DB.QueryRow(`SELECT count(*) FROM outbox WHERE kind = 'deferred_start' AND abandoned_at IS NULL`).Scan(&pending); err != nil || pending != 0 {
				t.Fatalf("stopped starts = %d, %v", pending, err)
			}
			if stopped, err := st.DeferredStopped(ctx, 118); err != nil || !stopped {
				t.Fatalf("unlinked stop = %v, %v", stopped, err)
			}
			// The stop belongs to this issue; other deferred work still starts.
			if err := enqueueDeferred(ctx, st, "deferred/119/2026-11-17", 119, nil); err != nil {
				t.Fatal(err)
			}
			if err := st.DB.QueryRow(`SELECT count(*) FROM outbox WHERE dedupe_key = 'deferred/119/2026-11-17' AND abandoned_at IS NULL`).Scan(&pending); err != nil || pending != 1 {
				t.Fatalf("unrelated start = %d, %v", pending, err)
			}
			if stopped, err := st.DeferredStopped(ctx, 119); err != nil || stopped {
				t.Fatalf("unrelated issue stopped = %v, %v", stopped, err)
			}
		})
	}
}

func enqueueDeferred(ctx context.Context, st *store.Store, key string, issueID int64, payload any) error {
	return st.Tx(ctx, func(tx *sql.Tx) error { return st.EnqueueDeferred(ctx, tx, key, issueID, payload) })
}

func TestDeferredSendAdmissionSerializesWithStop(t *testing.T) {
	for _, linked := range []bool{false, true} {
		name := "unlinked"
		if linked {
			name = "linked"
		}
		t.Run(name, func(t *testing.T) {
			f := newRecoveryFixture(t)
			if !linked {
				_, err := f.st.DB.Exec(`DELETE FROM delivery_attempt;
					UPDATE run SET work_item_id = NULL, issue_id = 101, issue_key = 'HL-101';
					DELETE FROM work_item`)
				f.must(err)
			}
			f.must(enqueueDeferred(f.ctx, f.st, "deferred/101/2026-11-15", 101, nil))
			var id int64
			f.must(f.st.DB.QueryRow(`SELECT id FROM outbox WHERE kind = 'deferred_start'`).Scan(&id))
			f.must(f.w.Tx(f.ctx, func(tx *sql.Tx) error {
				admitted, err := f.st.AdmitDeferred(f.ctx, tx, id, 101)
				if err != nil || !admitted {
					t.Fatalf("send admission = %v, %v", admitted, err)
				}
				// A stop cannot commit between checking the generation and
				// committing the send marker, even without a work item.
				ctx, cancel := context.WithTimeout(f.ctx, 100*time.Millisecond)
				defer cancel()
				_, err = f.st.StopIssue(ctx, "HL-101", "racing stop", "operator")
				if err == nil || ctx.Err() == nil {
					t.Fatalf("stop did not wait for send admission: %v", err)
				}
				return nil
			}))
			stopped, err := f.st.DeferredStopped(f.ctx, 101)
			f.must(err)
			if stopped {
				t.Fatal("rolled-back stop changed the fence")
			}

			// The opposite ordering: an earlier eligibility read is stale
			// by the time the durable send admission starts.
			f.must(enqueueDeferred(f.ctx, f.st, "deferred/101/2026-11-16", 101, nil))
			f.must(f.st.DB.QueryRow(`SELECT id FROM outbox WHERE dedupe_key = 'deferred/101/2026-11-16'`).Scan(&id))
			stop, err := f.st.StopIssue(f.ctx, "HL-101", "committed stop", "operator")
			f.must(err)
			if !stop.Any() {
				t.Fatal("stop did not commit")
			}
			f.must(f.w.Tx(f.ctx, func(tx *sql.Tx) error {
				admitted, err := f.st.AdmitDeferred(f.ctx, tx, id, 101)
				if admitted {
					t.Fatal("committed stop admitted a send")
				}
				return err
			}))
			var fenced bool
			f.must(f.st.DB.QueryRow(`SELECT send_started_at IS NULL AND abandoned_at IS NOT NULL
				AND last_error = 'deferred issue stopped' FROM outbox WHERE id = $1`, id).Scan(&fenced))
			if !fenced {
				t.Fatal("stopped request recorded a send marker")
			}
		})
	}
}

func TestDeferredStopMigrationAndRollbackAfterReadmission(t *testing.T) {
	for _, readmit := range []bool{false, true} {
		name := "stopped"
		if readmit {
			name = "readmitted"
		}
		t.Run(name, func(t *testing.T) {
			f := newRecoveryFixture(t)
			_, err := f.st.DB.Exec(`DELETE FROM delivery_attempt;
				UPDATE run SET work_item_id = NULL, issue_id = 101, issue_key = 'HL-101';
				DELETE FROM work_item`)
			f.must(err)
			const key = "deferred/101/2026-11-15"
			f.must(enqueueDeferred(f.ctx, f.st, key, 101, nil))
			var id int64
			f.must(f.st.DB.QueryRow(`SELECT id FROM outbox WHERE dedupe_key = $1`, key).Scan(&id))
			_, err = f.st.StopIssue(f.ctx, "HL-101", "operator stop", "operator")
			f.must(err)
			f.must(f.st.SetRunState(f.ctx, f.st.DB, 1, store.RunClosed, "operator stop"))
			if readmit {
				_, _, err := f.st.RecordClaim(f.ctx, store.ClaimedRun{ID: 2, AgentKey: "coder", IssueID: 101, IssueKey: "HL-101", Attempt: 1})
				f.must(err)
				_, _, err = f.st.ResolveWorkItem(f.ctx, 2, store.NewWorkItem{
					Binding: store.Binding{Name: "repo", KagentNamespace: "ns", KagentAgent: "coder", BranchPrefix: "factory/"},
					IssueID: 101, IssueKey: "HL-101", AgentKey: "coder",
				})
				f.must(err)
			}
			// Model a database from before this migration, including the
			// stopped unlinked run that remains after explicit re-admission.
			_, err = f.st.DB.Exec(`DROP TABLE deferred_fence;
				ALTER TABLE outbox DROP COLUMN deferred_stop_generation;
				DELETE FROM schema_migrations WHERE version = '20261117030000_deferred_stops'`)
			f.must(err)
			f.must(migrations.Apply(f.ctx, f.st.DB))
			f.must(migrations.Apply(f.ctx, f.st.DB))
			stopped, err := f.st.DeferredStopped(f.ctx, 101)
			f.must(err)
			if stopped == readmit {
				t.Fatalf("stopped after migration = %v, readmitted = %v", stopped, readmit)
			}

			// Recovery cannot replace the old generation, even when the
			// issue is eligible again and an older connector abandoned it.
			f.must(f.st.OutboxAbandon(f.ctx, f.st.DB, id, "unknown kind deferred_start"))
			f.must(enqueueDeferred(f.ctx, f.st, key, 101, nil))
			var abandoned bool
			f.must(f.st.DB.QueryRow(`SELECT abandoned_at IS NOT NULL AND deferred_stop_generation = 0 FROM outbox WHERE id = $1`, id).Scan(&abandoned))
			if !abandoned {
				t.Fatal("rollback recovery replaced the pre-stop generation")
			}
			f.must(f.w.Tx(f.ctx, func(tx *sql.Tx) error {
				admitted, err := f.st.AdmitDeferred(f.ctx, tx, id, 101)
				if admitted {
					t.Fatal("legacy request crossed a recorded stop")
				}
				return err
			}))
			f.must(enqueueDeferred(f.ctx, f.st, "deferred/101/2026-11-16", 101, nil))
			var fresh bool
			f.must(f.st.DB.QueryRow(`SELECT EXISTS (SELECT 1 FROM outbox WHERE dedupe_key = 'deferred/101/2026-11-16' AND deferred_stop_generation = 1)`).Scan(&fresh))
			if fresh != readmit {
				t.Fatalf("fresh schedule = %v, readmitted = %v", fresh, readmit)
			}
		})
	}
}

func TestDeferredSendWaitsForConcurrentStopCommit(t *testing.T) {
	f := newRecoveryFixture(t)
	_, err := f.st.DB.Exec(`DELETE FROM delivery_attempt;
		UPDATE run SET work_item_id = NULL, issue_id = 101, issue_key = 'HL-101';
		DELETE FROM work_item`)
	f.must(err)
	f.must(enqueueDeferred(f.ctx, f.st, "deferred/101/2026-11-15", 101, nil))
	var id int64
	f.must(f.st.DB.QueryRow(`SELECT id FROM outbox WHERE kind = 'deferred_start'`).Scan(&id))
	stopped, err := f.st.DeferredStopped(f.ctx, 101)
	f.must(err)
	if stopped {
		t.Fatal("request was already stopped")
	}

	ctx, cancel := context.WithTimeout(f.ctx, 10*time.Second)
	defer cancel()
	barrier, err := f.st.DB.BeginTx(ctx, nil)
	f.must(err)
	defer barrier.Rollback()
	_, err = barrier.ExecContext(ctx, `SELECT issue_id FROM deferred_fence WHERE issue_id = 101 FOR UPDATE`)
	f.must(err)
	stopDone := make(chan error, 1)
	go func() {
		stop, err := f.st.StopIssue(ctx, "HL-101", "racing stop", "operator")
		if err == nil && stop.HeldRuns != 1 {
			err = fmt.Errorf("stop = %+v, want one held run", stop)
		}
		stopDone <- err
	}()
	waitForLocks := func(want int) {
		t.Helper()
		for {
			var n int
			f.must(f.st.DB.QueryRowContext(ctx, `SELECT count(*) FROM pg_stat_activity
				WHERE datname = current_database() AND wait_event_type = 'Lock'`).Scan(&n))
			if n >= want {
				return
			}
			time.Sleep(time.Millisecond)
		}
	}
	// Queue the real StopIssue first, then admission, on the same row.
	waitForLocks(1)
	sendDone := make(chan error, 1)
	go func() {
		sendDone <- f.w.Tx(ctx, func(tx *sql.Tx) error {
			admitted, err := f.st.AdmitDeferred(ctx, tx, id, 101)
			if err == nil && admitted {
				return fmt.Errorf("send crossed the concurrent stop")
			}
			return err
		})
	}()
	waitForLocks(2)
	f.must(barrier.Commit())
	f.must(<-stopDone)
	f.must(<-sendDone)
	var fenced bool
	f.must(f.st.DB.QueryRow(`SELECT send_started_at IS NULL AND abandoned_at IS NOT NULL
		FROM outbox WHERE id = $1`, id).Scan(&fenced))
	if !fenced {
		t.Fatal("concurrent stop allowed a send marker")
	}
}
