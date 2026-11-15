package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
)

// Transcript is a task's durable event journal and source cursors. Base is its
// first offset within the itsaplan attempt, including earlier kagent tasks.
type Transcript struct {
	Base int
	// State is JSON stored in bytea; jsonb cannot retain escaped U+0000.
	State json.RawMessage
}

// GetTranscript reads a journal inside the writer's transaction. A missing
// journal returns nil; the caller initializes it from the legacy run offset.
func (s *Store) GetTranscript(ctx context.Context, q Querier, runID int64, attempt int, taskID string) (*Transcript, error) {
	t := &Transcript{}
	err := q.QueryRowContext(ctx, `
		SELECT base_offset, state FROM task_transcript
		WHERE run_id = $1 AND attempt = $2 AND task_id = $3`, runID, attempt, taskID).Scan(&t.Base, &t.State)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	return t, err
}

// SaveTranscript commits the journal and cursors together, before any events
// are sent. The caller holds the writer transaction and the run row lock.
func (s *Store) SaveTranscript(ctx context.Context, q Querier, runID int64, attempt int, taskID string, t *Transcript) error {
	_, err := q.ExecContext(ctx, `
		INSERT INTO task_transcript (run_id, attempt, task_id, base_offset, state)
		VALUES ($1, $2, $3, $4, $5)
		ON CONFLICT (run_id, attempt, task_id) DO UPDATE SET state = EXCLUDED.state`,
		runID, attempt, taskID, t.Base, []byte(t.State))
	return err
}
