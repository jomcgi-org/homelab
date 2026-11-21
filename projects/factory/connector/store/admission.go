package store

import (
	"context"
	"errors"
	"time"
)

// ErrAdmissionStopped fences a delegation that predates a work-item stop.
var ErrAdmissionStopped = errors.New("delivery stopped after this run was claimed; a new delegation is required")

// ErrSuperseded ends a delegation that a newer delegation of the issue to
// another agent replaced before it was linked to the work item. Linked runs
// are superseded by ResolveWorkItem's reassignment instead.
var ErrSuperseded = errors.New("superseded: the issue was delegated to another agent")

// Superseded reports a newer itsaplan run of r's issue for another agent.
func (s *Store) Superseded(ctx context.Context, q Querier, runID int64) (bool, error) {
	var superseded bool
	err := q.QueryRowContext(ctx, `SELECT EXISTS (
		SELECT 1 FROM run o JOIN run r ON r.id = $1
		WHERE o.issue_id = r.issue_id AND o.id > r.id AND o.agent_key <> r.agent_key
			AND o.origin = 'itsaplan')`, runID).Scan(&superseded)
	return superseded, err
}

// AdmissionStopped also finds an existing work item before run.work_item_id
// is resolved, without changing its ownership, grant, state or session.
func (s *Store) AdmissionStopped(ctx context.Context, r *Run) (bool, error) {
	var stopped bool
	err := s.DB.QueryRowContext(ctx, `SELECT EXISTS (
		SELECT 1 FROM work_item WHERE issue_id = $1 AND stops > $2)`, r.IssueID, r.AdmissionStops).Scan(&stopped)
	return stopped, err
}

// RecordAdmissionRead records an idempotent read before its external call.
func (s *Store) RecordAdmissionRead(ctx context.Context, q Querier, r *Run, epoch int64, messageID, operation, resource string) error {
	_, err := q.ExecContext(ctx, `
		INSERT INTO admission_read (message_id, run_id, run_attempt, operation, resource, writer_epoch)
		VALUES ($1, $2, $3, $4, $5, $6)
		ON CONFLICT (message_id) DO UPDATE SET attempts = admission_read.attempts + 1,
			writer_epoch = EXCLUDED.writer_epoch, attempted_at = now()`,
		messageID, r.ID, r.Attempt, operation, resource, epoch)
	return err
}

// SetAdmissionHold keeps the first hold time until admission succeeds.
func (s *Store) SetAdmissionHold(ctx context.Context, q Querier, id int64, reason string, now time.Time) error {
	_, err := q.ExecContext(ctx, `
		UPDATE run SET hold_reason = $2,
            admission_held = admission_held + CASE WHEN $2 = '' AND hold_since IS NOT NULL THEN greatest($3::timestamptz - hold_since, interval '0') ELSE interval '0' END,
			hold_since = CASE WHEN $2 = '' THEN NULL ELSE coalesce(hold_since, $3) END,
			state = CASE WHEN $2 = '' THEN state ELSE 'waiting' END, updated_at = now()
		WHERE id = $1`, id, reason, now)
	return err
}

// AdmissionHolds reads current holds from durable state, including after restart.
func (s *Store) AdmissionHolds(ctx context.Context, now time.Time) (count int, oldestSeconds float64, err error) {
	err = s.DB.QueryRowContext(ctx, `
		SELECT count(*), coalesce(greatest(0, extract(epoch FROM ($1::timestamptz - min(hold_since)))), 0)
		FROM run WHERE hold_since IS NOT NULL AND state IN ('claimed', 'waiting')`, now).Scan(&count, &oldestSeconds)
	return
}
