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
		{"activation kind", store.OutboxActivation, "unknown kind deferred_start", false, false},
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
			if err := st.OutboxFailed(ctx, st.DB, id, "waiting", now.Add(time.Hour), nil); err != nil {
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

func waitForDeferredLocks(f *recoveryFixture, ctx context.Context, want int) {
	f.t.Helper()
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
			f.must(enqueueDeferred(f.ctx, f.st, "deferred/101/2026-11-16", 101, nil))
			ctx, cancel := context.WithTimeout(f.ctx, 10*time.Second)
			defer cancel()
			stopDone := make(chan error, 1)
			f.must(f.w.Tx(f.ctx, func(tx *sql.Tx) error {
				admitted, err := f.st.AdmitDeferred(f.ctx, tx, id, 101)
				if err != nil || !admitted {
					t.Fatalf("send admission = %v, %v", admitted, err)
				}
				// A stop cannot commit between checking the generation and
				// committing the send marker, even without a work item.
				go func() {
					stop, err := f.st.StopIssue(ctx, "HL-101", "racing stop", "operator")
					if err == nil && !stop.Any() {
						err = fmt.Errorf("racing stop did not stop anything")
					}
					stopDone <- err
				}()
				waitForDeferredLocks(f, ctx, 1)
				stopped, err := f.st.DeferredStopped(ctx, 101)
				if err != nil || stopped {
					t.Fatalf("stop committed before send admission: %v, %v", stopped, err)
				}
				return nil
			}))
			f.must(<-stopDone)

			// The opposite ordering: an earlier eligibility read is stale
			// by the time the durable send admission starts.
			f.must(f.st.DB.QueryRow(`SELECT id FROM outbox WHERE dedupe_key = 'deferred/101/2026-11-16'`).Scan(&id))
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
	// Queue the real StopIssue first, then admission, on the same row.
	waitForDeferredLocks(f, ctx, 1)
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
	waitForDeferredLocks(f, ctx, 2)
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

func deferredStopFixture(t *testing.T, linked bool) *recoveryFixture {
	t.Helper()
	f := newRecoveryFixture(t)
	if !linked {
		_, err := f.st.DB.Exec(`DELETE FROM delivery_attempt;
			UPDATE run SET work_item_id = NULL, issue_id = 101, issue_key = 'HL-101';
			DELETE FROM work_item`)
		f.must(err)
	}
	return f
}

// Reproduce the authoritative writes from trusted-main StopWork/StopIssue
// (51e01a269), without calling any new fence-aware store method.
func rollbackStop(f *recoveryFixture, linked bool) {
	f.t.Helper()
	f.must(f.st.Tx(f.ctx, func(tx *sql.Tx) error {
		if linked {
			if _, err := tx.ExecContext(f.ctx, `UPDATE work_item SET state = 'stopped',
				stopped_reason = 'rollback stop', stopped_by = 'operator', stops = stops + 1, updated_at = now()
				WHERE issue_id = 101 AND state NOT IN ('stopped', 'done')`); err != nil {
				return err
			}
			_, err := tx.ExecContext(f.ctx, `UPDATE run SET stopped_reason = 'rollback stop', updated_at = now()
				WHERE work_item_id = $1 AND stopped_reason IS NULL
				AND state IN ('claimed', 'waiting', 'dispatched', 'stale')`, f.work.ID)
			return err
		}
		_, err := tx.ExecContext(f.ctx, `UPDATE run SET stopped_reason = 'rollback stop', updated_at = now()
			WHERE issue_key = 'HL-101' AND work_item_id IS NULL AND origin = 'itsaplan'
			AND stopped_reason IS NULL AND state IN ('claimed', 'waiting', 'dispatched', 'stale')`)
		return err
	}))
}

func readmitDeferred(f *recoveryFixture) {
	f.t.Helper()
	f.must(f.st.SetRunState(f.ctx, f.st.DB, 1, store.RunClosed, "rollback stop"))
	_, _, err := f.st.RecordClaim(f.ctx, store.ClaimedRun{ID: 2, AgentKey: "coder", IssueID: 101, IssueKey: "HL-101", Attempt: 1})
	f.must(err)
	_, _, err = f.st.ResolveWorkItem(f.ctx, 2, store.NewWorkItem{
		Binding: store.Binding{Name: "repo", KagentNamespace: "ns", KagentAgent: "coder", BranchPrefix: "factory/"},
		IssueID: 101, IssueKey: "HL-101", AgentKey: "coder",
	})
	f.must(err)
}

func admitDeferred(f *recoveryFixture, id int64) bool {
	f.t.Helper()
	var admitted bool
	f.must(f.w.Tx(f.ctx, func(tx *sql.Tx) error {
		var err error
		admitted, err = f.st.AdmitDeferred(f.ctx, tx, id, 101)
		return err
	}))
	return admitted
}

func TestDeferredUpgradeRollbackStopAndReupgrade(t *testing.T) {
	for _, linked := range []bool{false, true} {
		t.Run(fmt.Sprintf("linked=%v", linked), func(t *testing.T) {
			f := deferredStopFixture(t, linked) // The upgrade is already applied.
			const oldKey = "deferred/101/2026-11-15"
			f.must(enqueueDeferred(f.ctx, f.st, oldKey, 101, nil))
			var oldID int64
			f.must(f.st.DB.QueryRow(`SELECT id FROM outbox WHERE dedupe_key = $1`, oldKey).Scan(&oldID))
			f.must(f.st.OutboxAbandon(f.ctx, f.st.DB, oldID, "unknown kind deferred_start"))
			rollbackStop(f, linked)
			// Re-upgrade skips the recorded migration. The old binary's
			// stop must already be durable in the same fence.
			f.must(migrations.Apply(f.ctx, f.st.DB))
			var fenced bool
			f.must(f.st.DB.QueryRow(`SELECT generation > 0 AND stopped FROM deferred_fence WHERE issue_id = 101`).Scan(&fenced))
			if !fenced {
				t.Fatal("rollback stop did not advance the fence")
			}
			f.must(enqueueDeferred(f.ctx, f.st, oldKey, 101, nil))
			f.must(enqueueDeferred(f.ctx, f.st, "deferred/101/2026-11-16", 101, nil))
			var pending int
			f.must(f.st.DB.QueryRow(`SELECT count(*) FROM outbox WHERE kind = 'deferred_start' AND abandoned_at IS NULL`).Scan(&pending))
			if pending != 0 || admitDeferred(f, oldID) {
				t.Fatal("rollback stop allowed recovery or a new schedule")
			}

			// The original ResolveWorkItem SQL also clears the active stop.
			// Its run-link update fires the database admission trigger.
			readmitDeferred(f)
			if admitDeferred(f, oldID) {
				t.Fatal("old request borrowed the rollback re-admission")
			}
			// An old connector knows no generation column. The INSERT
			// trigger must snapshot the generation for its new schedule.
			const newKey = "deferred/101/2026-11-17"
			f.must(f.st.Enqueue(f.ctx, f.st.DB, store.OutboxDeferredStart, newKey, 0, "", nil))
			var newID int64
			f.must(f.st.DB.QueryRow(`SELECT id FROM outbox WHERE dedupe_key = $1`, newKey).Scan(&newID))
			f.must(migrations.Apply(f.ctx, f.st.DB))
			if !admitDeferred(f, newID) {
				t.Fatal("new rollback schedule lost its stop boundary")
			}
		})
	}
}

func removeDeferredStopMigration(f *recoveryFixture) {
	f.t.Helper()
	_, err := f.st.DB.Exec(`DROP TRIGGER deferred_work_fence ON work_item;
		DROP TRIGGER deferred_run_fence ON run;
		DROP TRIGGER deferred_request_fence ON outbox;
		DROP FUNCTION deferred_work_fence(); DROP FUNCTION deferred_run_fence(); DROP FUNCTION deferred_request_fence();
		DROP TABLE deferred_fence;
		ALTER TABLE outbox DROP COLUMN deferred_stop_generation;
		DELETE FROM schema_migrations WHERE version = '20261117030000_deferred_stops'`)
	f.must(err)
}

func TestDeferredMigrationHoldsRequestInsertedDuringLegacyStop(t *testing.T) {
	f := deferredStopFixture(t, false)
	removeDeferredStopMigration(f)
	ctx, cancel := context.WithTimeout(f.ctx, 10*time.Second)
	defer cancel()
	stop, err := f.st.DB.BeginTx(ctx, nil)
	f.must(err)
	defer stop.Rollback()
	// Leave the legacy held-run stop uncommitted while another connection
	// sees the old run and enqueues. now() records the stop transaction's
	// start, so the request looks newer despite preceding the stop's commit.
	_, err = stop.ExecContext(ctx, `UPDATE run SET stopped_reason = 'legacy stop', updated_at = now()
		WHERE id = 1 AND work_item_id IS NULL AND stopped_reason IS NULL`)
	f.must(err)
	var unstopped bool
	f.must(f.st.DB.QueryRowContext(ctx, `SELECT stopped_reason IS NULL FROM run WHERE id = 1`).Scan(&unstopped))
	if !unstopped {
		t.Fatal("legacy reader saw an uncommitted stop")
	}
	const key = "deferred/101/2026-11-15"
	f.must(f.st.Enqueue(ctx, f.st.DB, store.OutboxDeferredStart, key, 0, "", nil))
	f.must(stop.Commit())
	// Admit the new delegation without any metadata write to the old run.
	_, _, err = f.st.RecordClaim(ctx, store.ClaimedRun{ID: 2, AgentKey: "coder", IssueID: 101, IssueKey: "HL-101", Attempt: 1})
	f.must(err)
	_, _, err = f.st.ResolveWorkItem(ctx, 2, store.NewWorkItem{
		Binding: store.Binding{Name: "repo", KagentNamespace: "ns", KagentAgent: "coder", BranchPrefix: "factory/"},
		IssueID: 101, IssueKey: "HL-101", AgentKey: "coder",
	})
	f.must(err)
	f.must(migrations.Apply(ctx, f.st.DB))
	var id int64
	var misleadingOrder bool
	f.must(f.st.DB.QueryRowContext(ctx, `SELECT o.id, o.created_at > r.updated_at
		AND f.generation = 1 AND NOT f.stopped
		FROM outbox o JOIN run r ON r.id = 1 JOIN deferred_fence f ON f.issue_id = r.issue_id
		WHERE o.dedupe_key = $1`, key).Scan(&id, &misleadingOrder))
	if !misleadingOrder {
		t.Fatal("fixture did not reproduce the misleading legacy timestamp order")
	}
	if admitDeferred(f, id) || admitDeferred(f, id) {
		t.Fatal("request inserted before the stop committed borrowed the later admission")
	}
	var held bool
	f.must(f.st.DB.QueryRowContext(ctx, `SELECT deferred_stop_generation IS NULL
		AND send_started_at IS NULL AND abandoned_at IS NULL AND delivered_at IS NULL
		AND tries = 0 AND last_error = 'deferred stop order unknown'
		FROM outbox WHERE id = $1`, id).Scan(&held))
	var notices int
	f.must(f.st.DB.QueryRowContext(ctx, `SELECT count(*) FROM outbox WHERE dedupe_key = $1`,
		fmt.Sprintf("comment/deferred-reconcile/%d", id)).Scan(&notices))
	if !held || notices != 1 {
		t.Fatalf("held = %v, reconciliation notices = %d", held, notices)
	}
}

func TestDeferredMigrationHoldsLegacyRequestsWithStops(t *testing.T) {
	for _, linked := range []bool{false, true} {
		for _, order := range []string{"no_stop", "after_stop", "metadata_update", "before_stop"} {
			t.Run(fmt.Sprintf("linked=%v/order=%s", linked, order), func(t *testing.T) {
				f := deferredStopFixture(t, linked)
				removeDeferredStopMigration(f)
				const key = "deferred/101/2026-11-15"
				var id int64
				enqueueLegacy := func() {
					f.must(f.st.Enqueue(f.ctx, f.st.DB, store.OutboxDeferredStart, key, 0, "", map[string]string{"delegate": "original"}))
					f.must(f.st.DB.QueryRow(`SELECT id FROM outbox WHERE dedupe_key = $1`, key).Scan(&id))
					f.must(f.st.OutboxSendStarted(f.ctx, id))
					f.must(f.st.DeferredWait(f.ctx, f.st.DB, id, f.now.Add(time.Minute)))
				}
				if order == "before_stop" {
					enqueueLegacy()
				}
				if order != "no_stop" {
					rollbackStop(f, linked)
					readmitDeferred(f)
				}
				if order != "before_stop" {
					enqueueLegacy()
				}
				if order == "metadata_update" {
					// Neither the original stop timestamp nor a later metadata
					// write establishes commit order for a legacy request.
					if linked {
						_, err := f.st.DB.Exec(`UPDATE work_item SET updated_at = now() WHERE issue_id = 101`)
						f.must(err)
					} else {
						_, err := f.st.DB.Exec(`UPDATE run SET updated_at = now() WHERE id = 1`)
						f.must(err)
					}
				}
				f.must(migrations.Apply(f.ctx, f.st.DB))
				f.must(migrations.Apply(f.ctx, f.st.DB))
				var preserved bool
				f.must(f.st.DB.QueryRow(`SELECT abandoned_at IS NULL AND send_started_at IS NOT NULL
					AND tries = 0 AND payload->>'delegate' = 'original' FROM outbox WHERE id = $1`, id).Scan(&preserved))
				if !preserved || admitDeferred(f, id) != (order == "no_stop") {
					t.Fatal("migration lost or incorrectly admitted the pending request")
				}
				if order == "no_stop" {
					return
				}
				// Repeated delivery and chart rollback preserve the hold and
				// queue exactly one observable reconciliation notice.
				f.must(f.st.OutboxAbandon(f.ctx, f.st.DB, id, "unknown kind deferred_start"))
				// Follow the notice with an explicit new delegation. This
				// also clears an uncertain legacy held stop before rescheduling.
				_, _, err := f.st.RecordClaim(f.ctx, store.ClaimedRun{ID: 3, AgentKey: "coder", IssueID: 101, IssueKey: "HL-101", Attempt: 1})
				f.must(err)
				_, _, err = f.st.ResolveWorkItem(f.ctx, 3, store.NewWorkItem{IssueID: 101, IssueKey: "HL-101", AgentKey: "coder"})
				f.must(err)
				f.must(enqueueDeferred(f.ctx, f.st, key, 101, nil))
				if admitDeferred(f, id) || admitDeferred(f, id) {
					t.Fatal("uncertain request borrowed a later admission")
				}
				var held bool
				f.must(f.st.DB.QueryRow(`SELECT abandoned_at IS NULL AND delivered_at IS NULL
					AND deferred_stop_generation IS NULL AND last_error = 'deferred stop order unknown'
					AND next_attempt_at > $2 AND tries = 0 FROM outbox WHERE id = $1`, id, f.now).Scan(&held))
				var notices int
				f.must(f.st.DB.QueryRow(`SELECT count(*) FROM outbox WHERE dedupe_key = $1
					AND payload->>'body' LIKE '%delegate the issue explicitly%'`, fmt.Sprintf("comment/deferred-reconcile/%d", id)).Scan(&notices))
				if !held || notices != 1 {
					t.Fatalf("held = %v, reconciliation notices = %d", held, notices)
				}
				// The documented reconciliation path uses a different date.
				f.must(enqueueDeferred(f.ctx, f.st, "deferred/101/2026-11-16", 101, nil))
				var freshID int64
				f.must(f.st.DB.QueryRow(`SELECT id FROM outbox WHERE dedupe_key = 'deferred/101/2026-11-16'`).Scan(&freshID))
				if !admitDeferred(f, freshID) {
					t.Fatal("explicit rescheduling did not reconcile the legacy hold")
				}
			})
		}
	}
}
