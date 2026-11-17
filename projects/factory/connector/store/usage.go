package store

import (
	"context"
	"database/sql"

	"github.com/jomcgi/homelab/projects/factory/connector/itsaplan"
)

// AttributeUsage links calls made before the initial Send returned its new
// session ID. Assigned calls never move to a later turn in the same session.
func (s *Store) AttributeUsage(ctx context.Context, q Querier, attemptID int64) error {
	_, err := q.ExecContext(ctx, `
		UPDATE codex_call c SET delivery_attempt_id = a.id
		FROM delivery_attempt a WHERE a.id = $1
		AND c.delivery_attempt_id IS NULL AND c.atespace = a.kagent_namespace
		AND c.session_id = lower(a.context_id)`, attemptID)
	return err
}

// RunUsage persists the sum of every measured call across the run's attempts.
// NULL means no usage was observed, including runs from before rollout.
func (s *Store) RunUsage(ctx context.Context, q Querier, runID int64) (*itsaplan.Usage, error) {
	// Also covers a Begin that raced the initial acknowledgement's transaction.
	_, err := q.ExecContext(ctx, `
		UPDATE codex_call c SET delivery_attempt_id = (
			SELECT min(a.id) FROM delivery_attempt a WHERE a.run_id = $1
			AND a.kagent_namespace = c.atespace AND lower(a.context_id) = c.session_id)
		WHERE c.delivery_attempt_id IS NULL AND EXISTS (
			SELECT 1 FROM delivery_attempt a WHERE a.run_id = $1
			AND a.kagent_namespace = c.atespace AND lower(a.context_id) = c.session_id)`, runID)
	if err != nil {
		return nil, err
	}
	var in, out sql.NullInt64
	err = q.QueryRowContext(ctx, `
		UPDATE run SET input_tokens = totals.input, cached_input_tokens = totals.cached,
			output_tokens = totals.output
		FROM (SELECT sum(u.input_tokens) AS input, sum(u.cached_input_tokens) AS cached,
			sum(u.output_tokens) AS output
			FROM codex_usage u JOIN codex_call c ON c.id = u.call_id
			JOIN delivery_attempt a ON a.id = c.delivery_attempt_id WHERE a.run_id = $1) totals
		WHERE run.id = $1 RETURNING run.input_tokens, run.output_tokens`, runID).Scan(&in, &out)
	if err != nil {
		return nil, err
	}
	if !in.Valid {
		return nil, nil
	}
	return &itsaplan.Usage{InputTokens: in.Int64, OutputTokens: out.Int64}, nil
}
