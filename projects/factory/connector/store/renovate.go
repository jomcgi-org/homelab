package store

import (
	"context"
	"encoding/json"
)

// RenovateMajor reserves one of the two major-upgrade slots until the issue
// is terminal. Closed rows remain as deduplication tombstones.
type RenovateMajor struct {
	ID         int64
	Repository string
	Number     int
	IssueID    int64
	IssueKey   string
	Stopped    bool
	Closed     bool
}

func (s *Store) RenovateMajors(ctx context.Context, repo string) ([]RenovateMajor, error) {
	rows, err := s.DB.QueryContext(ctx, `SELECT id, repository, number, coalesce(issue_id, 0), issue_key, stopped, closed
		FROM renovate_major WHERE repository = $1 ORDER BY number`, repo)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []RenovateMajor
	for rows.Next() {
		var m RenovateMajor
		if err := rows.Scan(&m.ID, &m.Repository, &m.Number, &m.IssueID, &m.IssueKey, &m.Stopped, &m.Closed); err != nil {
			return nil, err
		}
		out = append(out, m)
	}
	return out, rows.Err()
}

// AdmitRenovateMajor is called under the writer's transaction. Reservations,
// including an unknown create, count against the cap before the POST.
func (s *Store) AdmitRenovateMajor(ctx context.Context, q Querier, repo string, number int) (bool, error) {
	r, err := q.ExecContext(ctx, `INSERT INTO renovate_major (repository, number)
		SELECT $1, $2 WHERE (SELECT count(*) FROM renovate_major WHERE NOT closed) < 2
		ON CONFLICT (repository, number) DO NOTHING`, repo, number)
	if err != nil {
		return false, err
	}
	n, err := r.RowsAffected()
	return n == 1, err
}

func (s *Store) AttachRenovateIssue(ctx context.Context, q Querier, id, issueID int64, key string) error {
	_, err := q.ExecContext(ctx, `UPDATE renovate_major SET issue_id = $2, issue_key = $3
		WHERE id = $1 AND (issue_id IS NULL OR issue_id = $2)`, id, issueID, key)
	return err
}

func (s *Store) CloseRenovateMajor(ctx context.Context, q Querier, id int64) error {
	_, err := q.ExecContext(ctx, `UPDATE renovate_major SET closed = true WHERE id = $1`, id)
	return err
}

// ReleaseUnsentRenovateMajor runs in the writer transaction. No sent attempt,
// attached issue or ownership stop may be forgotten. Delete the unused payload
// with the reservation so a later admission prepares current evidence.
func (s *Store) ReleaseUnsentRenovateMajor(ctx context.Context, q Querier, id int64, createMessage string) error {
	r, err := q.ExecContext(ctx, `DELETE FROM renovate_major
		WHERE id = $1 AND issue_id IS NULL AND NOT stopped AND NOT closed
		AND NOT EXISTS (SELECT 1 FROM renovate_attempt WHERE message_id = $2 AND (state <> 'ready' OR tries <> 0))`, id, createMessage)
	if err != nil {
		return err
	}
	n, err := r.RowsAffected()
	if err != nil || n == 0 {
		return err
	}
	_, err = q.ExecContext(ctx, `DELETE FROM renovate_attempt WHERE message_id = $1 AND state = 'ready' AND tries = 0`, createMessage)
	return err
}

func (s *Store) StopRenovateMajor(ctx context.Context, q Querier, id int64) error {
	_, err := q.ExecContext(ctx, `UPDATE renovate_major SET stopped = true WHERE id = $1`, id)
	return err
}

// RenovateAttempt journals both the intent and whether a write may have left
// the process. Payload is fixed at preparation, so restarts use the same brief.
type RenovateAttempt struct {
	MessageID string
	Payload   json.RawMessage
	State     string
	Tries     int
}

func (s *Store) PrepareRenovateAttempt(ctx context.Context, q Querier, id string, epoch int64, payload any) (*RenovateAttempt, error) {
	b, err := json.Marshal(payload)
	if err != nil {
		return nil, err
	}
	_, err = q.ExecContext(ctx, `INSERT INTO renovate_attempt (message_id, writer_epoch, payload)
		VALUES ($1, $2, $3) ON CONFLICT (message_id) DO NOTHING`, id, epoch, string(b))
	if err != nil {
		return nil, err
	}
	a := &RenovateAttempt{MessageID: id}
	err = q.QueryRowContext(ctx, `SELECT payload, state, tries FROM renovate_attempt WHERE message_id = $1`, id).
		Scan(&a.Payload, &a.State, &a.Tries)
	return a, err
}

func (s *Store) SendRenovateAttempt(ctx context.Context, q Querier, id string, epoch int64) error {
	_, err := q.ExecContext(ctx, `UPDATE renovate_attempt SET state = 'sent', writer_epoch = $2,
		tries = tries + 1, updated_at = now() WHERE message_id = $1`, id, epoch)
	return err
}

func (s *Store) SetRenovateAttempt(ctx context.Context, q Querier, id, state, reason string) error {
	_, err := q.ExecContext(ctx, `UPDATE renovate_attempt SET state = $2, last_error = $3,
		updated_at = now() WHERE message_id = $1`, id, state, reason)
	return err
}

// RenovateHasFactoryPR includes a PR already recorded by delivery, even if
// its branch has since been deleted. Discovery also checks GitHub directly.
func (s *Store) RenovateHasFactoryPR(ctx context.Context, issueID int64) (bool, error) {
	var found bool
	err := s.DB.QueryRowContext(ctx, `SELECT EXISTS (SELECT 1 FROM pull_request p
		JOIN work_item w ON w.id = p.work_item_id WHERE w.issue_id = $1)`, issueID).Scan(&found)
	return found, err
}

func (s *Store) RenovateMajorForIssue(ctx context.Context, issueID int64) (*RenovateMajor, error) {
	m := &RenovateMajor{}
	err := s.DB.QueryRowContext(ctx, `SELECT id, repository, number, issue_id, issue_key, stopped, closed
		FROM renovate_major WHERE issue_id = $1`, issueID).
		Scan(&m.ID, &m.Repository, &m.Number, &m.IssueID, &m.IssueKey, &m.Stopped, &m.Closed)
	return m, err
}

func (s *Store) RenovateSourcePR(ctx context.Context, repo string, issueID int64) (int, error) {
	var number int
	err := s.DB.QueryRowContext(ctx, `SELECT coalesce((SELECT number FROM renovate_major
		WHERE repository = $1 AND issue_id = $2), 0)`, repo, issueID).Scan(&number)
	return number, err
}
