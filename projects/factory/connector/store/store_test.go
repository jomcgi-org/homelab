package store_test

import (
	"context"
	"database/sql"
	"errors"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/internal/pgtest"
	"github.com/jomcgi/homelab/projects/factory/connector/migrations"
	"github.com/jomcgi/homelab/projects/factory/connector/store"
)

func TestMain(m *testing.M) { os.Exit(pgtest.Main(m)) }

func TestNotifyAttemptsRequireCurrentWriterAndContainNoPayload(t *testing.T) {
	st := open(t)
	ctx := context.Background()
	w, err := st.TryAcquireWriter(ctx, "notify")
	if err != nil {
		t.Fatal(err)
	}
	defer w.Release()
	key := "factory/2026-11-15/" + strings.Repeat("a", 64)
	for i := 0; i < 2; i++ {
		if err := st.RecordNotifyAttempt(ctx, w, key, "tools/call"); err != nil {
			t.Fatal(err)
		}
	}
	var attempts, epoch int64
	if err := st.DB.QueryRow(`SELECT attempts, writer_epoch FROM notify_attempt WHERE dedupe_key=$1`, key).Scan(&attempts, &epoch); err != nil {
		t.Fatal(err)
	}
	if attempts != 2 || epoch != w.Epoch {
		t.Fatal("attempt not recorded")
	}
	w.Release()
	if err := st.RecordNotifyAttempt(ctx, w, key, "tools/call"); !errors.Is(err, store.ErrNotWriter) {
		t.Fatalf("released writer: %v", err)
	}
	if err := st.RecordNotifyAttempt(ctx, nil, key, "token"); !errors.Is(err, store.ErrNotWriter) {
		t.Fatalf("missing writer: %v", err)
	}
}

// A broker quota read is recorded only by the current writer. Repeating it
// keeps its message ID and counts the attempt.
func TestQuotaReadsRequireTheCurrentWriter(t *testing.T) {
	st := open(t)
	ctx := context.Background()
	w, err := st.TryAcquireWriter(ctx, "quota")
	if err != nil {
		t.Fatal(err)
	}
	defer w.Release()
	ids := []string{"quota/block/kagent:factory-homelab/coder/1", "quota/stall/7"}
	for i := 0; i < 2; i++ {
		if err := st.RecordQuotaReads(ctx, w, ids); err != nil {
			t.Fatal(err)
		}
	}
	for _, id := range ids {
		var attempts, epoch int64
		if err := st.DB.QueryRow(`SELECT attempts, writer_epoch FROM quota_read WHERE message_id = $1`, id).Scan(&attempts, &epoch); err != nil {
			t.Fatal(err)
		}
		if attempts != 2 || epoch != w.Epoch {
			t.Fatalf("%s: attempts=%d epoch=%d, want 2 and %d", id, attempts, epoch, w.Epoch)
		}
	}
	w.Release()
	if err := st.RecordQuotaReads(ctx, w, ids); !errors.Is(err, store.ErrNotWriter) {
		t.Fatalf("released writer: %v", err)
	}
}

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
		"repo_binding", "work_item", "run", "delivery_attempt", "pull_request", "renovate_major", "renovate_attempt",
		"review_event", "delivery_read", "status_read", "quota_read", "cancellation", "outbox", "writer", "session_deletion",
		"human_gate_issue", "human_gate_attempt", "codex_call", "codex_usage",
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

// `connector cancel` of a held first delegation stops its run, which has
// no work item. Resolution then creates none, and a later delegation of the
// same issue is not fenced by the earlier stop (HOME-55).
func TestStopIssueFencesAHeldFirstDelegation(t *testing.T) {
	st := open(t)
	ctx := context.Background()
	claim := store.ClaimedRun{ID: 7, AgentKey: "coder", IssueID: 707, IssueKey: "HL-707", Attempt: 1}
	if _, _, err := st.RecordClaim(ctx, claim); err != nil {
		t.Fatal(err)
	}
	if _, err := st.StopIssue(ctx, "HL-708", "no such issue", "operator:test"); err == nil {
		t.Fatal("stop of an unknown issue reported success")
	}
	if stopped, err := st.StopIssue(ctx, "HL-707", "operator canceled", "operator:test"); err != nil || stopped != (store.IssueStop{HeldRuns: 1}) {
		t.Fatalf("stop = %+v, %v; want only the held run stopped", stopped, err)
	}
	b := store.Binding{Name: "repo", Owner: "org", Repo: "repo", BaseBranch: "main", ItsaplanProject: "HL", KagentNamespace: "ns", KagentAgent: "coder", BranchPrefix: "factory/"}
	if err := st.UpsertBinding(ctx, b); err != nil {
		t.Fatal(err)
	}
	n := store.NewWorkItem{Binding: b, IssueID: 707, IssueKey: "HL-707", AgentKey: "coder"}
	if _, _, err := st.ResolveWorkItem(ctx, 7, n); !errors.Is(err, store.ErrAdmissionStopped) {
		t.Fatalf("resolve = %v, want the stop to fence it", err)
	}
	if _, err := st.WorkItemByIssue(ctx, 707); !errors.Is(err, sql.ErrNoRows) {
		t.Fatalf("stopped resolution created a work item: %v", err)
	}
	if r, err := st.GetRun(ctx, 7); err != nil || !r.StopRequested || r.StoppedReason != "operator canceled" {
		t.Fatalf("run = %+v, %v; want the stop on the run", r, err)
	}
	claim.ID = 8
	if _, _, err := st.RecordClaim(ctx, claim); err != nil {
		t.Fatal(err)
	}
	if w, _, err := st.ResolveWorkItem(ctx, 8, n); err != nil || w.State != store.WorkActive {
		t.Fatalf("later delegation = %+v, %v; want it admitted", w, err)
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

func terminalFixture(t *testing.T) (*recoveryFixture, *store.Run) {
	t.Helper()
	f := newRecoveryFixture(t)
	f.must(f.st.SetRunState(f.ctx, f.st.DB, 1, store.RunDispatched, ""))
	f.must(f.st.SetSession(f.ctx, f.st.DB, f.work.ID, "session"))
	f.must(f.st.MarkAccepted(f.ctx, f.st.DB, f.target.ID, "session", "task"))
	var err error
	f.work, err = f.st.GetWorkItem(f.ctx, f.work.ID)
	f.must(err)
	f.target, err = f.st.GetAttempt(f.ctx, f.target.ID)
	f.must(err)
	r, err := f.st.GetRun(f.ctx, 1)
	f.must(err)
	return f, r
}

// An accepted turn with an open tool call is past its liveness lease only
// once the tool lease has passed since its last task event. A turn without
// one keeps the progress lease (HOME-159).
func TestLeaseExpiredBoundsAnOpenToolCall(t *testing.T) {
	f, _ := terminalFixture(t)
	expired := func(after time.Duration) bool {
		t.Helper()
		now := f.now.Add(after)
		got, err := f.st.LeaseExpired(f.ctx, now.Add(-5*time.Minute), now.Add(-20*time.Minute), now.Add(-time.Hour))
		f.must(err)
		return len(got) == 1 && got[0].ID == f.target.ID
	}
	f.must(f.st.SetAttemptEvents(f.ctx, f.st.DB, f.target.ID, 3, true, f.now))
	if expired(59 * time.Minute) {
		t.Fatal("an open tool call expired inside the tool lease")
	}
	if !expired(61 * time.Minute) {
		t.Fatal("an open tool call held its turn past the tool lease")
	}
	f.must(f.st.SetAttemptEvents(f.ctx, f.st.DB, f.target.ID, 4, false, f.now))
	if expired(19*time.Minute) || !expired(21*time.Minute) {
		t.Fatal("a turn without an open tool call left the progress lease")
	}
}

func TestTaskOutcomeRejectsChangedGenerations(t *testing.T) {
	for name, query := range map[string]string{
		"closed run":       `UPDATE run SET state = 'closed'`,
		"finishing run":    `UPDATE run SET state = 'finishing'`,
		"canceling run":    `UPDATE run SET state = 'canceling'`,
		"redelivery":       `UPDATE run SET attempt = attempt + 1`,
		"reassignment":     `UPDATE work_item SET epoch = epoch + 1`,
		"session":          `UPDATE work_item SET session_id = 'new-session', session_generation = session_generation + 1`,
		"review round":     `UPDATE work_item SET review_rounds = review_rounds + 1`,
		"retry":            `UPDATE work_item SET engineering_retries = engineering_retries + 1`,
		"completed turn":   `UPDATE work_item SET turn_attempt_id = (SELECT id FROM delivery_attempt LIMIT 1)`,
		"stop":             `UPDATE work_item SET state = 'stopped'`,
		"stopped run":      `UPDATE run SET stopped_reason = 'operator stop'`,
		"block":            `UPDATE work_item SET state = 'blocked'`,
		"finished attempt": `UPDATE delivery_attempt SET state = 'completed'`,
		"different task":   `UPDATE delivery_attempt SET task_id = 'new-task'`,
		"cancellation": `INSERT INTO cancellation (delivery_attempt_id, run_id, reason, state)
			SELECT id, run_id, 'run_canceled', 'pending' FROM delivery_attempt`,
		"newer attempt": `INSERT INTO delivery_attempt (run_id, seq, run_attempt, work_item_epoch,
			decision_id, kind, message_id, kagent_namespace, kagent_agent, state, writer_epoch)
			SELECT run_id, seq + 1, run_attempt, work_item_epoch, 'retry', kind, 'new-message',
				kagent_namespace, kagent_agent, 'sending', writer_epoch FROM delivery_attempt`,
		"newer rejected run": `INSERT INTO run (id, agent_key, work_item_id, trigger, prompt, system_prompt, attempt, state)
			SELECT 2, agent_key, work_item_id, trigger, prompt, system_prompt, 1, 'closed' FROM run WHERE id = 1;
			INSERT INTO delivery_attempt (run_id, seq, run_attempt, work_item_epoch,
				decision_id, kind, message_id, kagent_namespace, kagent_agent, state, writer_epoch)
			SELECT 2, 1, 1, work_item_epoch, 'initial', kind, 'rejected-message',
				kagent_namespace, kagent_agent, 'failed', writer_epoch FROM delivery_attempt`,
		"newer canceled run": `INSERT INTO run (id, agent_key, work_item_id, trigger, prompt, system_prompt, attempt, state)
			SELECT 2, agent_key, work_item_id, trigger, prompt, system_prompt, 1, 'closed' FROM run WHERE id = 1;
			INSERT INTO delivery_attempt (run_id, seq, run_attempt, work_item_epoch,
				decision_id, kind, message_id, kagent_namespace, kagent_agent, state, writer_epoch)
			SELECT 2, 1, 1, work_item_epoch, 'initial', kind, 'canceled-message',
				kagent_namespace, kagent_agent, 'canceled', writer_epoch FROM delivery_attempt`,
	} {
		t.Run(name, func(t *testing.T) {
			f, r := terminalFixture(t)
			_, err := f.st.DB.Exec(query)
			f.must(err)
			err = f.st.AttemptTx(f.ctx, f.w, r, f.work, f.target, func(*sql.Tx) error {
				t.Fatal("stale task outcome was admitted")
				return nil
			})
			if !errors.Is(err, store.ErrAttemptChanged) {
				t.Fatalf("stale outcome: %v", err)
			}
		})
	}
}

func TestSupersededGrantFailureRequiresCurrentWaitingFailure(t *testing.T) {
	for _, tc := range []struct {
		name       string
		query      string
		superseded bool
		retry      bool
	}{
		{"newer run working", "", true, false},
		{"quota failure", `UPDATE delivery_attempt SET error_class = 'quota' WHERE run_id = 1`, true, false},
		{"newer run finished", `UPDATE run SET state = 'closed' WHERE id = 2;
			UPDATE delivery_attempt SET state = 'completed' WHERE run_id = 2`, true, false},
		{"newer unsent", `UPDATE delivery_attempt SET state = 'sending', task_id = NULL WHERE run_id = 2`, false, false},
		{"newer refused", `UPDATE delivery_attempt SET state = 'sending', task_id = NULL, issues = 1 WHERE run_id = 2`, false, false},
		{"newer ambiguous", `UPDATE delivery_attempt SET state = 'ambiguous', task_id = NULL, issues = 1 WHERE run_id = 2`, false, false},
		{"newer rejected", `UPDATE delivery_attempt SET state = 'failed', task_id = NULL WHERE run_id = 2`, false, true},
		{"newer canceled without acceptance", `UPDATE run SET state = 'closed' WHERE id = 2;
			UPDATE delivery_attempt SET state = 'canceled', task_id = NULL, issues = 1 WHERE run_id = 2`, false, true},
		{"newer canceled after acceptance", `UPDATE run SET state = 'closed' WHERE id = 2;
			UPDATE delivery_attempt SET state = 'canceled' WHERE run_id = 2`, true, false},
		{"newer cancellation pending", `UPDATE run SET state = 'canceling' WHERE id = 2;
			UPDATE delivery_attempt SET state = 'ambiguous', task_id = NULL, issues = 1 WHERE run_id = 2;
			INSERT INTO cancellation (delivery_attempt_id, run_id, reason, state)
				SELECT id, run_id, 'run_canceled', 'pending' FROM delivery_attempt WHERE run_id = 2`, false, false},
		{"no newer attempt", `DELETE FROM delivery_attempt WHERE run_id = 2`, false, true},
		{"closed run", `UPDATE run SET state = 'closed' WHERE id = 1`, false, false},
		{"stopped run", `UPDATE run SET stopped_reason = 'operator stop' WHERE id = 1`, false, false},
		{"redelivered run", `UPDATE run SET attempt = attempt + 1 WHERE id = 1`, false, false},
		{"accepted attempt", `UPDATE delivery_attempt SET state = 'accepted' WHERE run_id = 1`, false, false},
		{"engineering failure", `UPDATE delivery_attempt SET error_class = 'engineering' WHERE run_id = 1`, false, false},
		{"different work", `UPDATE run SET work_item_id = NULL WHERE id = 2`, false, true},
		{"own retry", `UPDATE delivery_attempt SET run_id = 1, seq = 2 WHERE run_id = 2`, false, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			f, r := terminalFixture(t)
			f.must(f.st.FinishAttempt(f.ctx, f.st.DB, f.target.ID, store.AttemptFailed, "credential", "login required"))
			f.must(f.st.SetRunState(f.ctx, f.st.DB, r.ID, store.RunWaiting, ""))
			var err error
			r, err = f.st.GetRun(f.ctx, r.ID)
			f.must(err)
			f.target, err = f.st.GetAttempt(f.ctx, f.target.ID)
			f.must(err)
			_, err = f.st.DB.Exec(`INSERT INTO run (id, agent_key, work_item_id, trigger, prompt, system_prompt, attempt, state)
				SELECT 2, agent_key, work_item_id, trigger, prompt, system_prompt, 1, 'dispatched' FROM run WHERE id = 1`)
			f.must(err)
			newer, err := f.st.RecordAttempt(f.ctx, f.st.DB, store.Attempt{
				RunID: 2, RunAttempt: 1, WorkItemEpoch: f.work.Epoch, DecisionID: "initial", Kind: "initial",
				MessageID: "itsaplan-run/2/initial", KagentNamespace: "ns", KagentAgent: "coder", WriterEpoch: f.w.Epoch,
			})
			f.must(err)
			f.must(f.st.MarkAccepted(f.ctx, f.st.DB, newer.ID, "session", "new-task"))
			if tc.query != "" {
				_, err = f.st.DB.Exec(tc.query)
				f.must(err)
			}
			f.must(f.w.Tx(f.ctx, func(tx *sql.Tx) error {
				superseded, err := f.st.SupersededGrantFailure(f.ctx, tx, r, f.target)
				if err == nil && superseded != tc.superseded {
					t.Errorf("superseded = %v, want %v", superseded, tc.superseded)
				}
				return err
			}))
			admitted := false
			err = f.st.AttemptTx(f.ctx, f.w, r, f.work, f.target, func(*sql.Tx) error {
				admitted = true
				return nil
			})
			if admitted != tc.retry || (tc.retry && err != nil) || (!tc.retry && !errors.Is(err, store.ErrAttemptChanged)) {
				t.Fatalf("retry admitted = %v, err = %v; want %v", admitted, err, tc.retry)
			}
		})
	}
}

func TestTaskOutcomeLocksGenerationsThroughCommit(t *testing.T) {
	f, r := terminalFixture(t)
	_, err := f.st.DB.Exec(`INSERT INTO run (id, agent_key, work_item_id, trigger, prompt, system_prompt, attempt, state)
		SELECT 2, agent_key, work_item_id, trigger, prompt, system_prompt, 1, 'waiting' FROM run WHERE id = 1`)
	f.must(err)
	f.must(f.st.AttemptTx(f.ctx, f.w, r, f.work, f.target, func(tx *sql.Tx) error {
		for _, query := range []string{
			`UPDATE writer SET epoch = epoch + 1`,
			`UPDATE work_item SET state = 'stopped'`,
			`UPDATE run SET attempt = attempt + 1`,
			`UPDATE delivery_attempt SET state = 'canceled'`,
			`INSERT INTO run (id, agent_key, work_item_id, trigger, prompt, system_prompt, attempt, state)
				SELECT 3, agent_key, work_item_id, trigger, prompt, system_prompt, 1, 'waiting' FROM run WHERE id = 1`,
			`INSERT INTO delivery_attempt (run_id, seq, run_attempt, work_item_epoch,
				decision_id, kind, message_id, kagent_namespace, kagent_agent, state, writer_epoch)
				SELECT 2, 1, 1, work_item_epoch, 'next-run', kind, 'new-message',
					kagent_namespace, kagent_agent, 'sending', writer_epoch FROM delivery_attempt`,
			`INSERT INTO cancellation (delivery_attempt_id, run_id, reason, state)
				SELECT id, run_id, 'run_canceled', 'pending' FROM delivery_attempt`,
		} {
			ctx, cancel := context.WithTimeout(f.ctx, 100*time.Millisecond)
			_, err := f.st.DB.ExecContext(ctx, query)
			blocked := ctx.Err() != nil
			cancel()
			if err == nil || !blocked {
				t.Fatalf("generation write did not wait for the terminal transaction: %s: %v", query, err)
			}
		}
		if err := f.st.FinishAttempt(f.ctx, tx, f.target.ID, store.AttemptCompleted, "", ""); err != nil {
			return err
		}
		if err := f.st.FinishTurn(f.ctx, tx, f.work.ID, f.target.ID); err != nil {
			return err
		}
		return f.st.SetRunState(f.ctx, tx, r.ID, store.RunClosed, "completed")
	}))
	work, err := f.st.GetWorkItem(f.ctx, f.work.ID)
	f.must(err)
	if work.State != store.WorkReview || work.TurnAttemptID != f.target.ID {
		t.Fatalf("current task outcome did not commit: %+v", work)
	}
}

func TestSuccessorCanFinishInheritedTaskAfterRedelivery(t *testing.T) {
	f, r := terminalFixture(t)
	f.w.Release()
	var next *store.Writer
	var err error
	for deadline := time.Now().Add(5 * time.Second); next == nil && time.Now().Before(deadline); {
		next, err = f.st.TryAcquireWriter(f.ctx, "successor")
		f.must(err)
		if next == nil {
			time.Sleep(20 * time.Millisecond)
		}
	}
	if next == nil || next.Epoch <= f.target.WriterEpoch {
		t.Fatal("successor did not acquire a new epoch")
	}
	defer next.Release()
	r, _, err = f.st.RecordClaim(f.ctx, store.ClaimedRun{ID: r.ID, AgentKey: r.AgentKey, Attempt: r.Attempt + 1})
	f.must(err)
	// Delivery metadata retains the original sender and transport attempt;
	// the current run snapshot and executing writer authorize this completion.
	f.must(f.st.AttemptTx(f.ctx, next, r, f.work, f.target, func(tx *sql.Tx) error {
		if err := f.st.FinishAttempt(f.ctx, tx, f.target.ID, store.AttemptCompleted, "", ""); err != nil {
			return err
		}
		return f.st.FinishTurn(f.ctx, tx, f.work.ID, f.target.ID)
	}))
}

func TestStoryboardDeliveryPrivileges(t *testing.T) {
	st := open(t)
	_, err := st.DB.Exec(`
		INSERT INTO repo_binding (name, owner, repo, base_branch, itsaplan_project,
			kagent_namespace, kagent_agent, branch_prefix, max_engineering_retries)
		VALUES ('homelab', 'org', 'homelab', 'main', 'HOME', 'factory', 'coder', 'factory/', 2);
		INSERT INTO work_item (binding_name, issue_id, issue_key, branch, agent_key,
			kagent_namespace, kagent_agent)
		VALUES ('homelab', 213, 'HOME-213', 'factory/HOME-213', 'coder', 'factory', 'coder')`)
	if err != nil {
		t.Fatal(err)
	}
	reader := pgtest.AsRole(t, st.DB, "factory_storyboard")
	var project, issue string
	if err := reader.QueryRow(`SELECT itsaplan_project, issue_key FROM public.storyboard_delivery
		WHERE issue_id = 213`).Scan(&project, &issue); err != nil {
		t.Fatal(err)
	}
	if project != "HOME" || issue != "HOME-213" {
		t.Fatalf("unexpected projection: %q %q", project, issue)
	}
	_, err = st.DB.Exec(`
        UPDATE work_item SET session_id='session' WHERE issue_id=213;
        INSERT INTO run (id, agent_key, work_item_id, trigger, prompt, system_prompt, attempt, state)
        SELECT 414, 'coder', id, 'initial', '', '', 1, 'closed' FROM work_item WHERE issue_id=213;
        INSERT INTO delivery_attempt (run_id,seq,run_attempt,work_item_epoch,decision_id,kind,message_id,
            kagent_namespace,kagent_agent,context_id,task_id,state,writer_epoch,kagent_outcome)
        SELECT 414,n,1,1,'decision-' || n,'initial','message-' || n,'factory','coder',session,task,state,1,outcome
        FROM (VALUES
            (1,'session','finished','completed','TASK_STATE_COMPLETED'),
            (2,'session','waiting','completed','TASK_STATE_INPUT_REQUIRED'),
            (3,'old','stale','completed','TASK_STATE_COMPLETED'),
            (4,'session','running','accepted',NULL),
            (5,'session','legacy','completed',NULL)
        ) AS cases(n,session,task,state,outcome)`)
	if err != nil {
		t.Fatal(err)
	}
	var projection bool
	err = reader.QueryRow(`SELECT jsonb_agg(to_jsonb(t)) = '[{"itsaplan_project":"HOME","issue_id":213,"session_id":"session","kagent_namespace":"factory","kagent_agent":"coder","task_id":"finished"}]'::jsonb FROM public.storyboard_tasks t`).Scan(&projection)
	if err != nil || !projection {
		t.Fatalf("unexpected storyboard task projection: %t (%v)", projection, err)
	}
	unrelated := pgtest.AsRole(t, st.DB, "factory_reaper")
	_, err = unrelated.Exec("SELECT * FROM public.storyboard_tasks")
	var refusal interface{ SQLState() string }
	if !errors.As(err, &refusal) || refusal.SQLState() != "42501" {
		t.Fatalf("unrelated role read: %v", err)
	}
	for _, query := range []string{
		"SELECT prompt FROM public.run",
		"SELECT state FROM public.task_transcript",
		"SELECT * FROM public.work_item",
		"UPDATE public.work_item SET review_rounds = 1 WHERE issue_id = 213",
		"DELETE FROM public.run",
	} {
		_, err := reader.Exec(query)
		var state interface{ SQLState() string }
		if !errors.As(err, &state) || state.SQLState() != "42501" {
			t.Errorf("%s: got %v, want insufficient_privilege (42501)", query, err)
		}
	}
}
