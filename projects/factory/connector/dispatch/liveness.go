package dispatch

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
	"github.com/jomcgi/homelab/projects/factory/connector/store"
)

// Liveness lease (HOME-141). A dispatched turn must show a kagent task
// within TaskLease of its send, and a task event or an open tool call at
// least every ProgressLease. A turn past its lease gets a lease_expired
// cancellation. A cancellation of any reason that kagent has not confirmed
// within ForceDelete deletes the turn's session with force, which ends its
// runtime Actor. Its confirmation closes the attempt as abandoned, and the
// run takes an engineering retry in a fresh session.
//
// The steps, each counted in factory_connector_stuck_turns_total{step}.
const (
	stepLease       = "lease"        // a lease_expired cancellation was requested
	stepNotStarted  = "not_started"  // kagent never admitted the send
	stepForceDelete = "force_delete" // DeleteSession with force was sent
	stepRefused     = "refused"      // kagent refused or failed the force delete
	stepRevert      = "revert"       // RevertSession was sent after a refusal
	stepDeleted     = "deleted"      // kagent reported the session gone
)

// checkLeases requests a lease_expired cancellation for each running turn
// past its lease. A turn with a task is read again first, as the quota stall
// does (cancelIfQuiet): a changed task or an open tool call restarts its
// lease. A send with no task needs no read here: its cancellation looks the
// message up in kagent before it decides anything.
func (e *Engine) checkLeases(ctx context.Context) error {
	now := e.cfg.Now()
	expired, err := e.st.LeaseExpired(ctx, now.Add(-e.cfg.TaskLease), now.Add(-e.cfg.ProgressLease))
	if err != nil {
		return err
	}
	var errs []error
	for _, a := range expired {
		if a.TaskID == "" {
			err = e.expireUnstarted(ctx, a)
		} else {
			err = e.cancelIfQuiet(ctx, a, store.CancelLeaseExpired)
		}
		if err != nil {
			if fatal(err) {
				return err
			}
			errs = append(errs, fmt.Errorf("attempt %d: %w", a.ID, err))
		}
	}
	return errors.Join(errs...)
}

// expireUnstarted requests the lease_expired cancellation of a send that has
// no task TaskLease after it went out.
func (e *Engine) expireUnstarted(ctx context.Context, a *store.Attempt) error {
	defer e.lockRun(a.RunID)()
	r, err := e.st.GetRun(ctx, a.RunID)
	if err != nil || r.State != store.RunDispatched || r.StopRequested {
		return err
	}
	cur, err := e.st.GetAttempt(ctx, a.ID)
	if err != nil || cur.State != store.AttemptAmbiguous || cur.TaskID != "" {
		return err
	}
	if err := e.w.Tx(ctx, func(tx *sql.Tx) error {
		return e.st.RequestQuietCancel(ctx, tx, a.ID, a.RunID, store.CancelLeaseExpired, 0)
	}); err != nil {
		return err
	}
	e.leaseRequested(ctx, a, "no task")
	return nil
}

func (e *Engine) leaseRequested(ctx context.Context, a *store.Attempt, why string) {
	e.m.Cancellations.WithLabelValues(store.CancelLeaseExpired, "requested").Inc()
	e.m.StuckTurns.WithLabelValues(stepLease).Inc()
	e.log.WarnContext(ctx, "turn past its liveness lease; canceling it", "run", a.RunID, "attempt", a.Seq,
		"task", a.TaskID, "why", why)
}

// escalating reports whether a pending cancellation deletes its attempt's
// session with force: kagent has not confirmed it within ForceDelete, or a
// force delete is already under way. An attempt with no known session has
// nothing to delete.
func (e *Engine) escalating(ctx context.Context, c *store.Cancellation, a *store.Attempt) (bool, error) {
	if e.sessions == nil || a.ContextID == "" {
		return false, nil
	}
	if e.cfg.Now().Sub(c.RequestedAt) >= e.cfg.ForceDelete {
		return true, nil
	}
	return e.st.ForceDeleting(ctx, a.ContextID)
}

// forceDelete deletes the attempt's session with force, spaced by
// CancelInterval. The call is recorded in session_deletion first, under the
// writer epoch, with the session ID as its idempotency key, and the work
// item stops recording the session. kagent's answer that the session is
// DELETED, or NOT_FOUND, confirms the cancellation: deletion cancels the
// session's active tasks once Substrate has deleted the Actor
// (kagent.Sessions.ForceDeleteSession). A refused or failed delete leaves
// the cancellation pending and the run holding its slot, since kagent may
// still run the turn. The connector then sends RevertSession, recorded
// first, which acts only on a crashed Actor: it fails the interrupted turn,
// and the next task read confirms the cancellation
// (https://github.com/kagent-dev/kagent/blob/558e3d761d8ed2b0d23bfef1867a9d1915210121/go/core/internal/service/session/recovery.go#L71-L91).
// An Actor wedged in a suspend refuses both until a person deletes its
// worker pod: the connector holds no Kubernetes credentials.
func (e *Engine) forceDelete(ctx context.Context, c *store.Cancellation, r *store.Run, wi *store.WorkItem, a *store.Attempt) error {
	now := e.cfg.Now()
	if c.Requests > 0 && now.Sub(c.LastAt) < e.cfg.CancelInterval {
		return nil
	}
	if err := e.w.Tx(ctx, func(tx *sql.Tx) error {
		if err := e.st.RecordForceDelete(ctx, tx, a.ContextID, wi.ID, e.w.Epoch, wi.CallerIdentity); err != nil {
			return err
		}
		return e.st.CountCancelRequest(ctx, tx, c.ID, now)
	}); err != nil {
		return err
	}
	if err := e.guard(ctx); err != nil {
		return err
	}
	e.m.StuckTurns.WithLabelValues(stepForceDelete).Inc()
	s, err := e.sessions.ForceDeleteSession(ctx, a.ContextID)
	switch {
	case errors.Is(err, kagent.ErrSessionNotFound) || (err == nil && s != nil && s.State == kagent.RuntimeDeleted):
		e.m.StuckTurns.WithLabelValues(stepDeleted).Inc()
		e.m.Cancellations.WithLabelValues(c.Reason, "confirmed").Inc()
		e.log.WarnContext(ctx, "deleted the session of a turn kagent did not cancel", "run", r.ID, "attempt", a.Seq,
			"context_id", a.ContextID, "reason", c.Reason)
		return e.w.Tx(ctx, func(tx *sql.Tx) error {
			if err := e.st.ConfirmSessionDeletion(ctx, tx, a.ContextID); err != nil {
				return err
			}
			return e.confirmCancelTx(ctx, tx, c, a, "session_deleted")
		})
	case err == nil:
		return nil // still DELETING: the next pass sends it again
	}
	e.m.StuckTurns.WithLabelValues(stepRefused).Inc()
	e.log.ErrorContext(ctx, "kagent refused to delete a stuck turn's session; the run keeps its slot",
		"run", r.ID, "attempt", a.Seq, "context_id", a.ContextID, "error", err)
	if rerr := e.w.Tx(ctx, func(tx *sql.Tx) error { return e.st.RecordRevert(ctx, tx, a.ContextID, e.w.Epoch) }); rerr != nil {
		return errors.Join(err, rerr)
	}
	if gerr := e.guard(ctx); gerr != nil {
		return gerr
	}
	e.m.StuckTurns.WithLabelValues(stepRevert).Inc()
	if _, rerr := e.sessions.RevertSession(ctx, a.ContextID); rerr != nil {
		err = errors.Join(err, fmt.Errorf("revert: %w", rerr))
	}
	return fmt.Errorf("force delete session %s: %w", a.ContextID, err)
}

// abandonTx closes a lease_expired turn whose cancellation kagent confirmed,
// in confirmCancelTx's transaction. A turn that ended on its own meanwhile
// keeps its attempt open, and progress reads its outcome, as
// confirmQuotaStall does. A stop of its run or work item wins: it locks both
// first, as blockTurn does, and reports the stop. So does a reassignment.
// Otherwise the attempt fails as abandoned, and progress takes an
// engineering retry (engineeringFailure). A send kagent never admitted
// leaves the session it named, so the retry opens a fresh one; a force
// delete already cleared it.
func (e *Engine) abandonTx(ctx context.Context, tx *sql.Tx, r *store.Run, a *store.Attempt, outcome string) error {
	switch {
	case !a.Open():
		return nil
	case outcome != "canceled" && outcome != stepNotStarted && outcome != "session_deleted":
		return nil
	}
	stopped, err := e.st.LockStop(ctx, tx, r.WorkItemID, r.ID)
	if err != nil {
		return err
	}
	var epoch int64
	var reason string
	if err := tx.QueryRowContext(ctx, `
		SELECT w.epoch, coalesce(r.stopped_reason, w.stopped_reason, '')
		FROM run r JOIN work_item w ON w.id = r.work_item_id WHERE r.id = $1`, r.ID).Scan(&epoch, &reason); err != nil {
		return err
	}
	msg := store.CancelLeaseExpired + ": " + outcome
	switch {
	case stopped:
		if err := e.st.FinishAttempt(ctx, tx, a.ID, store.AttemptCanceled, ClassCanceled, msg); err != nil {
			return err
		}
		return e.failRun(ctx, tx, r, "delivery stopped: "+reason)
	case a.WorkItemEpoch != epoch:
		if err := e.st.FinishAttempt(ctx, tx, a.ID, store.AttemptCanceled, ClassCanceled, msg); err != nil {
			return err
		}
		return e.failRun(ctx, tx, r, "superseded: the work item was reassigned to another agent")
	}
	if outcome == stepNotStarted {
		if _, err := tx.ExecContext(ctx, `UPDATE work_item SET session_id = NULL, updated_at = now()
			WHERE id = $1 AND session_id = $2`, r.WorkItemID, a.ContextID); err != nil {
			return err
		}
	}
	e.log.WarnContext(ctx, "turn abandoned after its liveness lease", "run", r.ID, "attempt", a.Seq, "outcome", outcome)
	return e.st.FinishAttempt(ctx, tx, a.ID, store.AttemptFailed, ClassAbandoned, msg)
}

// leaseYields withdraws a lease_expired cancellation that has sent no
// CancelTask when its turn ended on its own: progress then reports the
// outcome. A stop of the run or work item keeps the cancellation, as
// withdrawQuiet does.
func (e *Engine) leaseYields(ctx context.Context, r *store.Run, wi *store.WorkItem, a *store.Attempt) (bool, error) {
	var withdrawn bool
	err := e.w.Tx(ctx, func(tx *sql.Tx) error {
		withdrawn = false
		if stopped, err := e.st.LockStop(ctx, tx, wi.ID, r.ID); err != nil || stopped {
			return err
		}
		var err error
		withdrawn, err = e.st.WithdrawLease(ctx, tx, a.ID, "withdrawn: the turn ended before CancelTask")
		return err
	})
	if err == nil && withdrawn {
		e.m.Cancellations.WithLabelValues(store.CancelLeaseExpired, store.CancelWithdrawn).Inc()
	}
	return withdrawn, err
}

// notStartedWithin bounds when a complete task listing without the send
// proves kagent never admitted it. kagent's idle expiry can remove a session
// with its tasks and creation receipt, but only after the session's idle
// TTL, at least a day; later, absence may hide a turn that ran (HOME-141).
const notStartedWithin = time.Hour

// replacementNote tells the agent in a fresh session why the previous one
// ended. It is fixed text, so a resend stays identical.
const replacementNote = "\n## Replacement session\nThe factory ended the previous turn because it showed no " +
	"progress, and opened this session. Nothing from the previous session carries over: continue from this " +
	"context, the work branch and its pull request, if any.\n"

// recordedBrief is the newest issue brief the work item's sends recorded,
// or "" when none did. A retry of an abandoned continuation reuses it.
func (e *Engine) recordedBrief(ctx context.Context, wi *store.WorkItem) (string, error) {
	briefs, err := e.st.Briefs(ctx, wi.ID)
	if err != nil {
		return "", err
	}
	for _, b := range briefs {
		if hasIssueBrief(b, wi.IssueKey) {
			return b, nil
		}
	}
	return "", nil
}
