package dispatch

import (
	"context"
	"database/sql"
	"errors"
	"fmt"

	"github.com/jomcgi/homelab/projects/factory/connector/itsaplan"
	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
	"github.com/jomcgi/homelab/projects/factory/connector/store"
)

const recoveryLifecycle = "session lifecycle requires fencing"

// recoveryReason is sticky for lifecycle uncertainty: READY after a revert is
// not an independent acknowledgment that the previous physical writer stopped.
func (e *Engine) recoveryReason(ctx context.Context, a *store.Attempt) (string, error) {
	if a.Error == "" {
		return "", nil
	}
	var reason string
	err := e.w.Tx(ctx, func(tx *sql.Tx) error {
		r, err := e.st.FindRecovery(ctx, tx, a.ID)
		if err == nil && r != nil {
			reason = r.Reason
		}
		return err
	})
	return reason, err
}

// originalVisible invalidates a missing-task decision before its attestation
// could be consumed. Lifecycle holds require fencing even if a task is visible.
func (e *Engine) originalVisible(ctx context.Context, a *store.Attempt) (bool, error) {
	reason, err := e.recoveryReason(ctx, a)
	if err != nil || reason == recoveryLifecycle {
		return reason == recoveryLifecycle, err
	}
	if reason == "" {
		return false, nil
	}
	return false, e.w.Tx(ctx, func(tx *sql.Tx) error {
		if err := e.st.InvalidateRecovery(ctx, tx, a.ID); err != nil {
			return err
		}
		_, err := tx.ExecContext(ctx, `UPDATE delivery_attempt SET error = NULL WHERE id = $1`, a.ID)
		return err
	})
}

// recoverHeld records immutable evidence and, only after explicit external
// fencing attestation, applies one current recovery decision. It never infers
// cessation from a controller lifecycle state or an empty task listing.
func (e *Engine) recoverHeld(ctx context.Context, r *store.Run, wi *store.WorkItem, a *store.Attempt, reason string) error {
	var hold *store.Recovery
	err := e.w.Tx(ctx, func(tx *sql.Tx) error {
		existing, err := e.st.FindRecovery(ctx, tx, a.ID)
		if err != nil {
			return err
		}
		if existing != nil && existing.Reason == recoveryLifecycle {
			reason = recoveryLifecycle
		}
		hold, err = e.st.EnsureRecovery(ctx, tx, a.ID, reason, e.cfg.DispatchWindow)
		if err != nil {
			return err
		}
		if err := e.st.HoldAttempt(ctx, tx, a.ID, "operator fencing required: "+reason); err != nil {
			return err
		}
		return e.st.Enqueue(ctx, tx, store.OutboxComment, fmt.Sprintf("comment/recovery-held/%d", a.ID), r.ID, "",
			commentPayload{IssueID: wi.IssueID, Body: fmt.Sprintf("The factory is holding %s run %d attempt %d. An operator must fence every possible copy and retired dispatcher before recovery. Inspect the exact target with `connector recovery inspect %s`; the factory does not infer physical termination from a missing task or session.", wi.IssueKey, r.ID, a.Seq, wi.IssueKey)})
	})
	if err != nil || hold.State != "attested" {
		return err
	}
	if e.cfg.Caller == nil {
		return errCallerUncertain
	}
	ctx, identity, err := e.cfg.Caller.Pin(ctx)
	if err != nil || !identity.Valid() {
		return callerUncertain(err)
	}
	caller := identity.Key()
	if hold.Snapshot.CallerIdentity != "" && hold.Snapshot.CallerIdentity != caller {
		return errCallerUncertain
	}
	b := e.binding(wi.BindingName)
	background := ""
	var canceled bool
	var retries int
	if err := e.st.DB.QueryRowContext(ctx, `SELECT w.state = 'stopped' OR r.state = 'canceling'
		OR EXISTS (SELECT 1 FROM cancellation WHERE delivery_attempt_id = $2 AND state = 'pending' AND reason <> 'lease_expired'),
		w.engineering_retries FROM work_item w JOIN run r ON r.work_item_id = w.id
		WHERE r.id = $1`, r.ID, a.ID).Scan(&canceled, &retries); err != nil {
		return err
	}
	// Cancellation and exhausted-budget completion need no replacement context.
	// Recheck cancellation under locks below if it arrives during these reads.
	if hold.Action == "retry" && !canceled && b != nil && retries < b.MaxEngineeringRetries {
		background, err = e.replacementContext(ctx, wi, b, a.ContextID, "operator attested fencing")
		if err != nil {
			return err
		}
	}
	var next *store.Attempt
	nextWork := *wi
	err = e.w.Tx(ctx, func(tx *sql.Tx) error {
		// This revalidates the entire predecessor set under locks, using the
		// executing writer epoch, before any target/session/budget mutation.
		if err := e.st.CompleteRecovery(ctx, tx, hold.ID, hold.Fingerprint); err != nil {
			return err
		}
		var state, workState string
		var latest int64
		if err := tx.QueryRowContext(ctx, `SELECT r.state, w.state,
   (SELECT max(id) FROM delivery_attempt WHERE run_id = r.id)
   FROM run r JOIN work_item w ON w.id = r.work_item_id WHERE r.id = $1`, r.ID).Scan(&state, &workState, &latest); err != nil {
			return err
		}
		if hold.Snapshot.RunAttempt != r.Attempt || hold.Snapshot.WorkEpoch != wi.Epoch || !a.Open() {
			return store.ErrRecoveryConflict
		}
		// A null legacy witness may be adopted only because this attestation
		// covered every uncertain predecessor and every possible historical copy.
		if hold.Snapshot.CallerIdentity == "" {
			res, err := tx.ExecContext(ctx, `UPDATE work_item SET caller_identity = $2 WHERE id = $1 AND caller_identity IS NULL`, wi.ID, caller)
			if err != nil {
				return err
			}
			n, err := res.RowsAffected()
			if err != nil {
				return err
			}
			if n != 1 {
				return store.ErrRecoveryConflict
			}
		}
		nextWork.CallerIdentity = caller
		var c store.Cancellation
		c.AttemptID, c.RunID = a.ID, r.ID
		err := tx.QueryRowContext(ctx, `SELECT id, reason FROM cancellation WHERE delivery_attempt_id = $1 AND state = 'pending' FOR UPDATE`, a.ID).Scan(&c.ID, &c.Reason)
		if err != nil && !errors.Is(err, sql.ErrNoRows) {
			return err
		}
		if c.Reason == store.CancelLeaseExpired {
			// The liveness lease's own cancellation yields to the operator's
			// decision (HOME-141).
			if err := e.st.ConfirmCancellation(ctx, tx, c.ID, "operator_attested_fencing"); err != nil {
				return err
			}
			c = store.Cancellation{AttemptID: a.ID, RunID: r.ID}
		}
		cancel := c.ID != 0 || hold.Action == "cancel" || workState == store.WorkStopped || state == store.RunCanceling
		if a.WorkItemEpoch == wi.Epoch {
			if _, err := tx.ExecContext(ctx, `UPDATE work_item SET session_id = NULL WHERE id = $1`, wi.ID); err != nil {
				return err
			}
			nextWork.SessionID = ""
		}
		if cancel {
			if c.ID == 0 {
				if latest != a.ID || a.WorkItemEpoch != wi.Epoch {
					return store.ErrRecoveryConflict
				}
				c.Reason = store.CancelRoundStopped
				if _, err := tx.ExecContext(ctx, `UPDATE work_item SET state = 'stopped', stopped_reason = 'operator attested fencing and canceled recovery', stopped_by = $2 WHERE id = $1 AND state <> 'done'`, wi.ID, "operator:"+hold.By); err != nil {
					return err
				}
				if err := e.st.RequestCancellation(ctx, tx, a.ID, r.ID, c.Reason); err != nil {
					return err
				}
				if err := tx.QueryRowContext(ctx, `SELECT id FROM cancellation WHERE delivery_attempt_id = $1 AND state = 'pending'`, a.ID).Scan(&c.ID); err != nil {
					return err
				}
			}
			return e.confirmCancelTx(ctx, tx, &c, a, "operator_attested_fencing")
		}
		// Historical attempts only contribute fencing evidence. Never resurrect
		// an old run or a superseded target to carry out a retry action.
		if latest != a.ID || a.WorkItemEpoch != wi.Epoch || state != store.RunDispatched {
			return store.ErrRecoveryConflict
		}
		if err := e.st.FinishAttempt(ctx, tx, a.ID, store.AttemptFailed, ClassEngineering, "operator attested fencing"); err != nil {
			return err
		}
		budget := 0
		if b != nil {
			budget = b.MaxEngineeringRetries
		}
		n, ok, err := e.st.TakeEngineeringRetry(ctx, tx, wi.ID, budget)
		if err != nil {
			return err
		}
		if !ok || b == nil {
			return e.finishRun(ctx, tx, r, itsaplan.Result{Status: "failed", Error: "operator-attested recovery exhausted the engineering retry budget"})
		}
		nextWork.EngineeringRetries = n
		next, err = e.recordNext(ctx, tx, r, &nextWork, n, background)
		return err
	})
	if errors.Is(err, store.ErrRecoveryConflict) {
		return nil
	}
	if err != nil || next == nil {
		return err
	}
	// MarkIssued freshly checks stop, cancellation, epoch and writer ownership;
	// cached pre-attestation work state cannot admit this replacement.
	return e.issue(ctx, r, &nextWork, b, next)
}

func (e *Engine) missingTask(ctx context.Context, r *store.Run, wi *store.WorkItem, a *store.Attempt) error {
	if reason, err := e.recoveryReason(ctx, a); err != nil {
		return err
	} else if reason == recoveryLifecycle {
		return e.recoverHeld(ctx, r, wi, a, reason)
	}
	task, err := e.taskWithMessage(ctx, kagent.AgentRef{Namespace: a.KagentNamespace, Name: a.KagentAgent}, a.ContextID, a.MessageID)
	if err == nil && task != nil {
		return e.attach(ctx, a, wi, task)
	}
	return e.recoverHeld(ctx, r, wi, a, "accepted task missing")
}

func (e *Engine) holdIdleSession(ctx context.Context, r *store.Run, wi *store.WorkItem, reason string) error {
	a, err := e.st.LatestAttempt(ctx, r.ID)
	if err != nil {
		return err
	}
	if a == nil || !a.Open() {
		err = e.w.Tx(ctx, func(tx *sql.Tx) error {
			kind, decision := nextDecision(wi, 0)
			if a != nil {
				decision = fmt.Sprintf("recovery-after-%d", a.ID)
			}
			var err error
			a, err = e.record(ctx, tx, r, wi, kind, decision, "")
			return err
		})
		if err != nil {
			return err
		}
	}
	if r.State != store.RunDispatched {
		if err := e.w.Tx(ctx, func(tx *sql.Tx) error {
			return e.st.SetRunState(ctx, tx, r.ID, store.RunDispatched, "")
		}); err != nil {
			return err
		}
		r.State = store.RunDispatched
	}
	return e.recoverHeld(ctx, r, wi, a, reason)
}
