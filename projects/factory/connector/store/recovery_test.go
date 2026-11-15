package store_test

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"strings"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/store"
)

type recoveryFixture struct {
	t      *testing.T
	ctx    context.Context
	st     *store.Store
	w      *store.Writer
	now    time.Time
	work   *store.WorkItem
	target *store.Attempt
	seq    int
}

func newRecoveryFixture(t *testing.T) *recoveryFixture {
	f := &recoveryFixture{t: t, ctx: t.Context(), st: open(t), now: time.Date(2026, 11, 15, 18, 0, 0, 0, time.UTC)}
	f.st.Now = func() time.Time { return f.now }
	var err error
	f.w, err = f.st.TryAcquireWriter(f.ctx, "recovery-test")
	f.must(err)
	t.Cleanup(f.w.Release)
	b := store.Binding{Name: "repo", Owner: "org", Repo: "repo", BaseBranch: "main", ItsaplanProject: "HL", KagentNamespace: "ns", KagentAgent: "coder", BranchPrefix: "factory/"}
	f.must(f.st.UpsertBinding(f.ctx, b))
	_, _, err = f.st.RecordClaim(f.ctx, store.ClaimedRun{ID: 1, AgentKey: "coder", Attempt: 1})
	f.must(err)
	f.work, _, err = f.st.ResolveWorkItem(f.ctx, 1, store.NewWorkItem{Binding: b, IssueID: 101, IssueKey: "HL-101", AgentKey: "coder"})
	f.must(err)
	f.target = f.add(store.AttemptAmbiguous, "", true)
	return f
}

func (f *recoveryFixture) must(err error) {
	f.t.Helper()
	if err != nil {
		f.t.Fatal(err)
	}
}

func (f *recoveryFixture) add(state, taskID string, sent bool) *store.Attempt {
	f.t.Helper()
	f.seq++
	a, err := f.st.RecordAttempt(f.ctx, f.st.DB, store.Attempt{
		RunID: 1, RunAttempt: 1,
		WorkItemEpoch: f.work.Epoch, DecisionID: fmt.Sprint(f.seq), MessageID: fmt.Sprint("message-", f.seq),
		Kind: "initial", KagentNamespace: "ns", KagentAgent: "coder", WriterEpoch: f.w.Epoch,
	})
	f.must(err)
	issues := 0
	if sent {
		issues = 1
	}
	_, err = f.st.DB.Exec(`UPDATE delivery_attempt SET state=$2, task_id=nullif($3,''), issues=$4,
		first_sent_at=CASE WHEN $4 > 0 THEN $5::timestamptz END, sent_at=CASE WHEN $4 > 0 THEN $5::timestamptz END WHERE id=$1`, a.ID, state, taskID, issues, f.now)
	f.must(err)
	return a
}

func (f *recoveryFixture) ensure() *store.Recovery {
	return f.ensureReason("missing task")
}

func (f *recoveryFixture) ensureReason(reason string) *store.Recovery {
	f.t.Helper()
	var r *store.Recovery
	f.must(f.w.Tx(f.ctx, func(tx *sql.Tx) error {
		var err error
		r, err = f.st.EnsureRecovery(f.ctx, tx, f.target.ID, reason, time.Minute)
		return err
	}))
	return r
}

func (f *recoveryFixture) attest(r *store.Recovery) error {
	return f.st.AttestRecovery(f.ctx, r.ID, r.Fingerprint, "retry", "operator", "node power-off verified")
}

func (f *recoveryFixture) complete(r *store.Recovery) error {
	return f.w.Tx(f.ctx, func(tx *sql.Tx) error { return f.st.CompleteRecovery(f.ctx, tx, r.ID, r.Fingerprint) })
}

func TestRecoveryAttestationBoundaryAndSingleConsumption(t *testing.T) {
	f := newRecoveryFixture(t)
	r := f.ensure()
	_, err := f.st.DB.Exec(`UPDATE run SET last_heartbeat_at=now(); UPDATE delivery_attempt SET error='next observation'`)
	f.must(err)
	if again := f.ensure(); again.ID != r.ID || again.Fingerprint != r.Fingerprint {
		t.Fatal("same snapshot changed hold")
	}
	f.now = r.NotBefore.Add(-time.Nanosecond)
	if err := f.attest(r); !errors.Is(err, store.ErrRecoveryConflict) {
		t.Fatalf("early attestation: %v", err)
	}
	f.now = r.NotBefore
	for _, args := range [][4]string{{strings.Repeat("0", 64), "retry", "operator", "proof"}, {r.Fingerprint, "other", "operator", "proof"}, {r.Fingerprint, "retry", " ", "proof"}, {r.Fingerprint, "cancel", "operator", " "}} {
		if err := f.st.AttestRecovery(f.ctx, r.ID, args[0], args[1], args[2], args[3]); !errors.Is(err, store.ErrRecoveryConflict) {
			t.Fatalf("invalid attestation: %v", err)
		}
	}
	f.must(f.attest(r))
	if err := f.attest(r); !errors.Is(err, store.ErrRecoveryConflict) {
		t.Fatalf("replayed attestation: %v", err)
	}
	f.must(f.complete(r))
	if err := f.complete(r); !errors.Is(err, store.ErrRecoveryConflict) {
		t.Fatalf("replayed consumption: %v", err)
	}
	if again := f.ensure(); again.ID != r.ID || again.State != "consumed" {
		t.Fatal("consumed hold reactivated")
	}
	var active *store.Recovery
	f.must(f.w.Tx(f.ctx, func(tx *sql.Tx) error {
		var err error
		active, err = f.st.FindRecovery(f.ctx, tx, f.target.ID)
		return err
	}))
	if active != nil {
		t.Fatal("consumed hold remained active")
	}
	history, err := f.st.InspectRecoveries(f.ctx, "HL-101")
	f.must(err)
	if len(history) != 1 || history[0].By != "operator" || history[0].AttestedAt == nil || history[0].Evidence == "" {
		t.Fatalf("audit lost: %+v", history)
	}
}

func TestRecoverySnapshotSerializesNewSentCopies(t *testing.T) {
	f := newRecoveryFixture(t)
	copy := *f.target
	copy.MessageID, copy.DecisionID = "concurrent", "concurrent"
	f.must(f.w.Tx(f.ctx, func(tx *sql.Tx) error {
		if _, err := f.st.RecoverySnapshotFor(f.ctx, tx, f.target.ID); err != nil {
			return err
		}
		ctx, cancel := context.WithTimeout(f.ctx, 100*time.Millisecond)
		defer cancel()
		if _, err := f.st.RecordAttempt(ctx, f.st.DB, copy); err == nil {
			t.Fatal("new predecessor entered a locked recovery inventory")
		}
		return nil
	}))
	// Releasing the snapshot permits the same insert; the block was the
	// authority lock, not a malformed fixture or a persistent SQL conflict.
	_, err := f.st.RecordAttempt(f.ctx, f.st.DB, copy)
	f.must(err)
}

func TestRecoveryReasonChangeRequiresFreshAttestation(t *testing.T) {
	for _, consumed := range []bool{false, true} {
		t.Run(fmt.Sprint("consumed=", consumed), func(t *testing.T) {
			f := newRecoveryFixture(t)
			old := f.ensure()
			f.now = old.NotBefore
			f.must(f.attest(old))
			if consumed {
				f.must(f.complete(old))
			}
			fresh := f.ensureReason("session lifecycle uncertain")
			if fresh.ID == old.ID || fresh.Fingerprint == old.Fingerprint || fresh.State != "pending" || fresh.Reason != "session lifecycle uncertain" {
				t.Fatal("stronger recovery reused weaker evidence")
			}
			if err := f.complete(old); !errors.Is(err, store.ErrRecoveryConflict) {
				t.Fatalf("obsolete approval consumed: %v", err)
			}
			f.must(f.attest(fresh))
			f.must(f.complete(fresh))
			if again := f.ensureReason(fresh.Reason); again.ID != fresh.ID || again.State != "consumed" {
				t.Fatal("consumed reason fingerprint reactivated")
			}
		})
	}
}

func TestRecoveryPositiveAttachSerializesWithAttestation(t *testing.T) {
	f := newRecoveryFixture(t)
	r := f.ensure()
	f.now = r.NotBefore
	done := make(chan error, 1)
	f.must(f.w.Tx(f.ctx, func(tx *sql.Tx) error {
		if err := f.st.InvalidateRecovery(f.ctx, tx, f.target.ID); err != nil {
			return err
		}
		if err := f.st.MarkAccepted(f.ctx, tx, f.target.ID, "found-session", "found-task"); err != nil {
			return err
		}
		if err := f.st.SetSession(f.ctx, tx, f.work.ID, "found-session"); err != nil {
			return err
		}
		go func() { done <- f.attest(r) }()
		select {
		case err := <-done:
			t.Fatalf("attestation bypassed uncommitted attachment: %v", err)
		case <-time.After(30 * time.Millisecond):
		}
		return nil
	}))
	if err := <-done; !errors.Is(err, store.ErrRecoveryConflict) {
		t.Fatalf("obsolete attachment evidence attested: %v", err)
	}
	if err := f.complete(r); !errors.Is(err, store.ErrRecoveryConflict) {
		t.Fatalf("obsolete attachment evidence consumed: %v", err)
	}
}

func TestRecoverySnapshotRejectsChangedAuthorityAndPredecessors(t *testing.T) {
	for name, query := range map[string]string{
		"work epoch":           `UPDATE work_item SET epoch=epoch+1`,
		"session":              `UPDATE work_item SET session_id='replacement', session_generation=session_generation+1`,
		"run attempt":          `UPDATE run SET attempt=attempt+1`,
		"caller":               `UPDATE work_item SET caller_identity='bound-caller'`,
		"original run attempt": `UPDATE delivery_attempt SET run_attempt=run_attempt+1`,
		"task":                 `UPDATE delivery_attempt SET task_id='observed-task'`,
		"state":                `UPDATE delivery_attempt SET state='accepted'`,
		"sent time":            `UPDATE delivery_attempt SET sent_at=sent_at+interval '1 second'`,
		"new predecessor":      "",
	} {
		t.Run(name, func(t *testing.T) {
			f := newRecoveryFixture(t)
			r := f.ensure()
			f.now = r.NotBefore
			if query == "" {
				f.add(store.AttemptFailed, "", true)
			} else {
				_, err := f.st.DB.Exec(query)
				f.must(err)
			}
			if err := f.attest(r); !errors.Is(err, store.ErrRecoveryConflict) {
				t.Fatalf("stale attestation: %v", err)
			}
			fresh := f.ensure()
			if fresh.ID == r.ID || fresh.Fingerprint == r.Fingerprint {
				t.Fatal("changed evidence reused hold")
			}
			f.now = fresh.NotBefore
			f.must(f.attest(fresh))
			_, err := f.st.DB.Exec(`UPDATE work_item SET epoch=epoch+1`)
			f.must(err)
			if err := f.complete(fresh); !errors.Is(err, store.ErrRecoveryConflict) {
				t.Fatalf("stale consumption: %v", err)
			}
		})
	}
}

func TestRecoveryLegacySnapshotIncludesAllUncertainCopies(t *testing.T) {
	f := newRecoveryFixture(t)
	f.add(store.AttemptCompleted, "complete", true)
	f.add(store.AttemptCanceled, "canceled", true)
	f.add(store.AttemptSending, "", false)
	want := []int64{f.target.ID, f.add(store.AttemptFailed, "", true).ID, f.add(store.AttemptCanceled, "", true).ID}
	// A historical run has independent transport authority; inventory must
	// retain it even when the recovery target belongs to the current run.
	_, err := f.st.DB.Exec(`INSERT INTO run (id, agent_key, work_item_id, trigger, prompt, system_prompt, attempt, state)
		SELECT 2, agent_key, work_item_id, trigger, prompt, system_prompt, 2, state FROM run WHERE id=1`)
	f.must(err)
	_, err = f.st.DB.Exec(`UPDATE delivery_attempt SET run_id=2 WHERE id=$1`, want[1])
	f.must(err)
	f.now = f.now.Add(time.Minute)
	want = append(want, f.add(store.AttemptAccepted, "running", true).ID)
	f.target = f.add(store.AttemptSending, "", false)
	want = append(want, f.target.ID)
	r := f.ensure()
	if r.Snapshot.CallerIdentity != "" || len(r.Snapshot.Attempts) != len(want) || !r.NotBefore.Equal(f.now.Add(time.Minute)) {
		t.Fatalf("incomplete legacy inventory: %+v", r)
	}
	for i, a := range r.Snapshot.Attempts {
		if a.ID != want[i] || a.KagentNamespace != "ns" || a.KagentAgent != "coder" || a.WriterEpoch != f.w.Epoch {
			t.Fatalf("copy %d: %+v", i, a)
		}
	}
	if prior := r.Snapshot.Attempts[1]; prior.RunID != 2 || prior.RunAttempt != 1 || prior.CurrentRunAttempt != 2 {
		t.Fatalf("historical run authority lost: %+v", prior)
	}
	f.now = r.NotBefore
	f.must(f.attest(r))
	work, err := f.st.GetWorkItem(f.ctx, f.work.ID)
	f.must(err)
	if work.CallerIdentity != "" {
		t.Fatal("attestation rebound legacy ownership")
	}
	f.must(f.w.Tx(f.ctx, func(tx *sql.Tx) error { return f.st.InvalidateRecovery(f.ctx, tx, f.target.ID) }))
	if err := f.complete(r); !errors.Is(err, store.ErrRecoveryConflict) {
		t.Fatalf("invalidated approval consumed: %v", err)
	}
	if fresh := f.ensure(); fresh.ID == r.ID || fresh.Fingerprint != r.Fingerprint || fresh.State != "pending" {
		t.Fatal("new occurrence did not get fresh attestation")
	}
}

func TestWriterTransactionCannotCommitAfterConnectionTakeover(t *testing.T) {
	f := newRecoveryFixture(t)
	var next *store.Writer
	err := f.w.Tx(f.ctx, func(tx *sql.Tx) error {
		var pid int
		if err := tx.QueryRowContext(f.ctx, `SELECT pg_backend_pid()`).Scan(&pid); err != nil {
			return err
		}
		if _, err := tx.ExecContext(f.ctx, `UPDATE work_item SET engineering_retries=9`); err != nil {
			return err
		}
		// Wait for the backend to exit: its advisory lock is released only
		// then, and the takeover below must see it free.
		if _, err := f.st.DB.Exec(`SELECT pg_terminate_backend($1, 5000)`, pid); err != nil {
			return err
		}
		var err error
		next, err = f.st.TryAcquireWriter(f.ctx, "takeover")
		return err
	})
	if !errors.Is(err, store.ErrNotWriter) || next == nil {
		t.Fatalf("old writer committed or takeover failed: %v %+v", err, next)
	}
	defer next.Release()
	work, err := f.st.GetWorkItem(f.ctx, f.work.ID)
	f.must(err)
	if work.EngineeringRetries != 0 {
		t.Fatal("lost writer transaction committed")
	}
	if err := f.w.Tx(f.ctx, func(*sql.Tx) error { t.Fatal("stale callback invoked"); return nil }); !errors.Is(err, store.ErrNotWriter) {
		t.Fatalf("stale writer: %v", err)
	}
	ctx, cancel := context.WithCancel(f.ctx)
	cancel()
	if err := next.Tx(ctx, func(*sql.Tx) error { return nil }); !errors.Is(err, context.Canceled) {
		t.Fatalf("context cancellation: %v", err)
	}
}

func TestSendAdmissionAfterTransportRedelivery(t *testing.T) {
	for _, tc := range []struct {
		name, gate string
		admit      bool
	}{
		{name: "unsent", admit: true},
		{name: "definitively refused", admit: true},
		{name: "ambiguous"},
		{name: "stale executing run"},
		{name: "stopped", gate: `UPDATE work_item SET state='stopped'`},
		{name: "stopped then resumed"},
		{name: "blocked", gate: `UPDATE work_item SET state='blocked'`},
		{name: "canceling", gate: `UPDATE run SET state='canceling'`},
		{name: "work epoch changed", gate: `UPDATE work_item SET epoch=epoch+1`},
		{name: "cancellation"},
		{name: "superseded"},
		{name: "writer lost"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			f := newRecoveryFixture(t)
			f.must(f.st.SetRunState(f.ctx, f.st.DB, 1, store.RunDispatched, ""))
			if tc.name == "definitively refused" {
				f.must(f.st.MarkNotAccepted(f.ctx, f.target))
			} else if tc.name != "ambiguous" {
				_, err := f.st.DB.Exec(`UPDATE delivery_attempt SET state='sending', issues=0, first_sent_at=NULL, sent_at=NULL`)
				f.must(err)
			}
			a, err := f.st.GetAttempt(f.ctx, f.target.ID)
			f.must(err)
			_, outcome, err := f.st.RecordClaim(f.ctx, store.ClaimedRun{ID: 1, AgentKey: "coder", Attempt: 2})
			f.must(err)
			if outcome != store.ClaimRedelivery {
				t.Fatal("fixture did not redeliver the run")
			}
			if tc.gate != "" {
				_, err := f.st.DB.Exec(tc.gate)
				f.must(err)
			}
			switch tc.name {
			case "stopped then resumed":
				// The dispatcher still holds the old attempt when a stop and
				// a new admission both commit. Even an empty stop reason fences it.
				stopped, err := f.st.StopIssue(f.ctx, "HL-101", "", "operator:test")
				f.must(err)
				if !stopped {
					t.Fatal("work item was not stopped")
				}
				_, _, err = f.st.RecordClaim(f.ctx, store.ClaimedRun{ID: 2, AgentKey: "coder", Attempt: 1})
				f.must(err)
				work, _, err := f.st.ResolveWorkItem(f.ctx, 2, store.NewWorkItem{IssueID: 101, IssueKey: "HL-101", AgentKey: "coder"})
				f.must(err)
				if work.State != store.WorkActive {
					t.Fatal("new admission did not resume the work item")
				}
			case "cancellation":
				f.must(f.st.RequestCancellation(f.ctx, f.st.DB, a.ID, a.RunID, store.CancelReassignment))
			case "superseded":
				f.add(store.AttemptSending, "", false)
			case "writer lost":
				f.w.Release()
			}
			expectedRunAttempt := 2
			if tc.name == "stale executing run" {
				expectedRunAttempt = 1
			}
			issued, err := f.st.MarkIssued(f.ctx, f.w, a, expectedRunAttempt, f.now)
			if tc.name == "writer lost" {
				if !errors.Is(err, store.ErrNotWriter) {
					t.Fatalf("lost writer admitted work: %t %v", issued, err)
				}
				err = nil
			}
			if err != nil || issued != tc.admit {
				t.Fatalf("redelivery admission=%t, err=%v; want %t", issued, err, tc.admit)
			}
			stored, err := f.st.GetAttempt(f.ctx, a.ID)
			f.must(err)
			if tc.admit {
				if a.RunAttempt != 1 || stored.RunAttempt != 1 || stored.State != store.AttemptAmbiguous || stored.MessageID != f.target.MessageID {
					t.Fatalf("redelivery lost original message or authority: %+v", stored)
				}
				if issued, err := f.st.MarkIssued(f.ctx, f.w, a, expectedRunAttempt, f.now); err != nil || issued {
					t.Fatalf("ambiguous send admitted twice: %t %v", issued, err)
				}
			} else if stored.RunAttempt != 1 || stored.Issues != a.Issues {
				t.Fatal("ineligible attempt's authority changed")
			}
		})
	}
}

func TestWriterSerializesTransactionsChecksAndRelease(t *testing.T) {
	for _, operation := range []string{"commit", "check", "release"} {
		t.Run(operation, func(t *testing.T) {
			f := newRecoveryFixture(t)
			entered, resume := make(chan struct{}), make(chan struct{})
			first, second := make(chan error, 1), make(chan error, 1)
			rollback := errors.New("rollback first transaction")
			go func() {
				first <- f.w.Tx(f.ctx, func(tx *sql.Tx) error {
					_, err := tx.ExecContext(f.ctx, `UPDATE work_item SET engineering_retries=9`)
					close(entered)
					<-resume
					if err != nil {
						return err
					}
					return rollback
				})
			}()
			<-entered
			go func() {
				switch operation {
				case "commit":
					second <- f.w.Tx(f.ctx, func(tx *sql.Tx) error {
						_, err := tx.ExecContext(f.ctx, `UPDATE work_item SET engineering_retries=engineering_retries+1`)
						return err
					})
				case "check":
					second <- f.w.Check(f.ctx)
				case "release":
					f.w.Release()
					second <- nil
				}
			}()
			var secondErr error
			early := false
			select {
			case secondErr = <-second:
				early = true
				t.Error("operation ran while another transaction owned the session")
			case <-time.After(30 * time.Millisecond):
			}
			close(resume)
			if err := <-first; !errors.Is(err, rollback) {
				t.Errorf("first transaction: %v", err)
			}
			if !early {
				secondErr = <-second
			}
			f.must(secondErr)
			work, err := f.st.GetWorkItem(f.ctx, f.work.ID)
			f.must(err)
			want := 0
			if operation == "commit" {
				want = 1
			}
			if work.EngineeringRetries != want {
				t.Fatalf("rollback/commit crossed transactions: got %d want %d", work.EngineeringRetries, want)
			}
			if operation == "release" && !errors.Is(f.w.Check(f.ctx), store.ErrNotWriter) {
				t.Fatal("released writer remained usable")
			}
		})
	}
}
