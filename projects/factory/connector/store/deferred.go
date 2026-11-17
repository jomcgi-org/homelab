package store

import (
	"context"
	"encoding/json"
	"time"
)

// An admission-held delegation can be stopped before it has a work item.
// Closing that run does not clear its stop or authorize automatic delegation.
// Use the same check for fresh enqueues, rollback recovery and delivery.
const deferredStoppedSQL = `
	EXISTS (SELECT 1 FROM work_item WHERE issue_id = $1 AND state = 'stopped')
	OR EXISTS (SELECT 1 FROM run WHERE issue_id = $1 AND work_item_id IS NULL AND stopped_reason IS NOT NULL)`

// EnqueueDeferred admits an eligible issue, or resumes the same request if an
// older connector abandoned its unsupported kind during a chart rollback.
// The caller checks current tracker eligibility; stored stops also block this
// upsert. Preserve the original payload, send marker and error retry count.
func (s *Store) EnqueueDeferred(ctx context.Context, q Querier, dedupe string, issueID int64, payload any) error {
	b, err := json.Marshal(payload)
	if err != nil {
		return err
	}
	_, err = q.ExecContext(ctx, `
		INSERT INTO outbox (kind, dedupe_key, payload, next_attempt_at)
		SELECT 'deferred_start', $2, $3, $4
		WHERE NOT (`+deferredStoppedSQL+`)
		ON CONFLICT (dedupe_key) DO UPDATE
		SET abandoned_at = NULL, last_error = NULL, next_attempt_at = EXCLUDED.next_attempt_at
		WHERE outbox.kind = 'deferred_start' AND outbox.delivered_at IS NULL
			AND outbox.abandoned_at IS NOT NULL
			AND outbox.last_error = 'unknown kind deferred_start'`, issueID, dedupe, b, s.now())
	return err
}

// DeferredWait keeps the same receipt key pending while Todo is full. A WIP
// refusal proves that the server rolled back, so it consumes no error retry.
func (s *Store) DeferredWait(ctx context.Context, q Querier, id int64, next time.Time) error {
	_, err := q.ExecContext(ctx, `UPDATE outbox SET last_error = 'wip_limit_exceeded', next_attempt_at = $2 WHERE id = $1`, id, next)
	return err
}

// DeferredStopped preserves an existing operator stop even if the issue was
// moved back to Backlog and its delegate was removed.
func (s *Store) DeferredStopped(ctx context.Context, issueID int64) (bool, error) {
	var stopped bool
	err := s.DB.QueryRowContext(ctx, `SELECT `+deferredStoppedSQL, issueID).Scan(&stopped)
	return stopped, err
}
