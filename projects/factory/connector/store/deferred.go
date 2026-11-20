package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"time"
)

// lockDeferredFence serializes enqueue and send admission with stops and
// explicit re-admission. These callers take no work-item or run locks after
// this lock. Deferred database triggers take it when stop and admission
// transactions commit, including transactions from rollback versions.
func lockDeferredFence(ctx context.Context, tx *sql.Tx, issueID int64) (generation int64, stopped bool, err error) {
	if _, err = tx.ExecContext(ctx, `INSERT INTO deferred_fence (issue_id) VALUES ($1) ON CONFLICT DO NOTHING`, issueID); err != nil {
		return
	}
	err = tx.QueryRowContext(ctx, `SELECT generation, stopped FROM deferred_fence WHERE issue_id = $1 FOR UPDATE`, issueID).Scan(&generation, &stopped)
	return
}

// WithDecisionFence holds the issue's stop lock through the decision writes.
// A concurrent stop must commit before admission or after the writes finish.
func (s *Store) WithDecisionFence(ctx context.Context, issueID int64, write func() error) (bool, error) {
	admitted := false
	err := s.Tx(ctx, func(tx *sql.Tx) error {
		_, stopped, err := lockDeferredFence(ctx, tx, issueID)
		if err != nil || stopped {
			return err
		}
		admitted = true
		return write()
	})
	return admitted, err
}

// EnqueueDeferred admits an eligible issue, or resumes the same request if an
// older connector abandoned its unsupported kind during a chart rollback.
// The caller checks current tracker eligibility; stored stops also block this
// upsert. Preserve the original payload, send marker and error retry count.
func (s *Store) EnqueueDeferred(ctx context.Context, tx *sql.Tx, dedupe string, issueID int64, payload any) error {
	b, err := json.Marshal(payload)
	if err != nil {
		return err
	}
	generation, stopped, err := lockDeferredFence(ctx, tx, issueID)
	if err != nil || stopped {
		return err
	}
	_, err = tx.ExecContext(ctx, `
		INSERT INTO outbox (kind, dedupe_key, payload, next_attempt_at, deferred_stop_generation)
		VALUES ('deferred_start', $1, $2, $3, $4)
		ON CONFLICT (dedupe_key) DO UPDATE
		SET abandoned_at = NULL, unavailable_since = NULL, last_error = NULL, next_attempt_at = EXCLUDED.next_attempt_at
		WHERE outbox.kind = 'deferred_start' AND outbox.delivered_at IS NULL
			AND outbox.abandoned_at IS NOT NULL
			AND outbox.last_error = 'unknown kind deferred_start'
			AND (outbox.deferred_stop_generation IS NULL
				OR outbox.deferred_stop_generation = EXCLUDED.deferred_stop_generation)`, dedupe, b, s.now(), generation)
	return err
}

// AdmitDeferred checks the request's original stop generation and journals
// this send under the same lock as StopIssue. The caller holds the writer
// fence through commit and sends only if admitted is true.
func (s *Store) AdmitDeferred(ctx context.Context, tx *sql.Tx, id, issueID int64) (bool, error) {
	generation, stopped, err := lockDeferredFence(ctx, tx, issueID)
	if err != nil {
		return false, err
	}
	var requestGeneration sql.NullInt64
	if err := tx.QueryRowContext(ctx, `SELECT deferred_stop_generation FROM outbox WHERE id = $1`, id).Scan(&requestGeneration); err != nil {
		return false, err
	}
	if !requestGeneration.Valid {
		// Legacy timestamps cannot prove commit order across a stop.
		// Keep uncertainty recoverable and tell the operator how to
		// authorize a new schedule.
		if _, err := tx.ExecContext(ctx, `UPDATE outbox SET unavailable_since = NULL, last_error = 'deferred stop order unknown',
			next_attempt_at = $2 WHERE id = $1`, id, s.now().Add(time.Hour)); err != nil {
			return false, err
		}
		return false, s.Enqueue(ctx, tx, OutboxComment, fmt.Sprintf("comment/deferred-reconcile/%d", id), 0, "",
			map[string]any{"issueId": issueID, "body": "The factory held this deferred start because its order relative to an earlier stop is unknown. " +
				"The request remains pending and will not start automatically. To authorize work, delegate the issue explicitly. " +
				"After that admission, a different start date creates a new schedule. " +
				fmt.Sprintf("Deferred request: %d.", id)})
	}
	if stopped || requestGeneration.Int64 != generation {
		return false, s.OutboxAbandon(ctx, tx, id, "deferred issue stopped")
	}
	res, err := tx.ExecContext(ctx, `UPDATE outbox SET send_started_at = now()
		WHERE id = $1 AND kind = 'deferred_start' AND delivered_at IS NULL AND abandoned_at IS NULL`, id)
	if err != nil {
		return false, err
	}
	n, err := res.RowsAffected()
	return n == 1, err
}

// DeferredWait keeps the same receipt key pending while Todo is full. A WIP
// refusal proves that the server rolled back, so it consumes no error retry.
func (s *Store) DeferredWait(ctx context.Context, q Querier, id int64, next time.Time) error {
	_, err := q.ExecContext(ctx, `UPDATE outbox SET unavailable_since = NULL, last_error = 'wip_limit_exceeded', next_attempt_at = $2 WHERE id = $1`, id, next)
	return err
}

// DeferredStopped preserves an existing operator stop even if the issue was
// moved back to Backlog and its delegate was removed.
func (s *Store) DeferredStopped(ctx context.Context, issueID int64) (bool, error) {
	var stopped bool
	err := s.DB.QueryRowContext(ctx, `SELECT EXISTS (SELECT 1 FROM deferred_fence WHERE issue_id = $1 AND stopped)`, issueID).Scan(&stopped)
	return stopped, err
}
