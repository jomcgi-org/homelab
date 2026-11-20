package store

import (
	"context"
	"database/sql"
	"errors"
	"time"
)

func (s *Store) ActivationFollowup(ctx context.Context, workID int64) (id int64, key string, err error) {
	err = s.DB.QueryRowContext(ctx, `SELECT issue_id, issue_key FROM activation_followup WHERE work_item_id = $1`, workID).Scan(&id, &key)
	if errors.Is(err, sql.ErrNoRows) {
		err = nil
	}
	return
}

func (s *Store) RecordActivationFollowup(ctx context.Context, q Querier, workID, issueID int64, key string) error {
	_, err := q.ExecContext(ctx, `INSERT INTO activation_followup (work_item_id, issue_id, issue_key)
		VALUES ($1, $2, $3) ON CONFLICT (work_item_id) DO NOTHING`, workID, issueID, key)
	return err
}

// ActivationParent only follows relationships the connector created, never
// arbitrary dependency links. Completion can propagate one parent at a time.
func (s *Store) ActivationParent(ctx context.Context, q Querier, issueID int64) (id, workID int64, err error) {
	err = q.QueryRowContext(ctx, `SELECT w.issue_id, w.id FROM activation_followup f
		JOIN work_item w ON w.id = f.work_item_id WHERE f.issue_id = $1`, issueID).Scan(&id, &workID)
	if errors.Is(err, sql.ErrNoRows) {
		err = nil
	}
	return
}

// RecoverActivation repairs only the previous binary's unknown-kind rejection.
// Keep the send fence and identity: an ambiguous create must never be resent.
// Deliberate stops and exhausted deliveries are never revived.
func (s *Store) RecoverActivation(ctx context.Context) error {
	_, err := s.DB.ExecContext(ctx, `UPDATE outbox SET abandoned_at = NULL,
		last_error = NULL, next_attempt_at = $1
		WHERE kind IN ('activation','assessment') AND delivered_at IS NULL AND abandoned_at IS NOT NULL
		AND last_error = 'unknown kind ' || kind`, s.now())
	return err
}

// WaitActivation schedules another observation after successful reads. Waiting
// for a person is not a failed attempt; API failures still use the outbox bound.
func (s *Store) WaitActivation(ctx context.Context, id int64, next time.Time) error {
	_, err := s.DB.ExecContext(ctx, `UPDATE outbox SET next_attempt_at = $2,
		tries = 0, last_error = NULL WHERE id = $1`, id, next)
	return err
}

// CommentSettled keeps the merge comment ahead of the resulting issue move.
// An abandoned comment must not strand the actual activation work.
func (s *Store) CommentSettled(ctx context.Context, key string) (bool, error) {
	if key == "" {
		return true, nil // older outbox payloads
	}
	var settled bool
	err := s.DB.QueryRowContext(ctx, `SELECT EXISTS (SELECT 1 FROM outbox WHERE dedupe_key = $1
		AND (delivered_at IS NOT NULL OR abandoned_at IS NOT NULL))`, key).Scan(&settled)
	return settled, err
}
