package store

import (
	"context"
	"time"
)

// BosunRequest is a factory request Joe made in Discord (HOME-941). The
// Discord IDs come from the bridge's record of the turn.
type BosunRequest struct {
	ID                      int64
	Guild, Channel, Message string
	Title, Description      string
	State                   string
	IssueID                 int64
	IssueKey                string
	Tries                   int
}

// Request states.
const (
	BosunPending  = "pending"
	BosunFiled    = "filed"
	BosunDone     = "done"
	BosunCanceled = "canceled"
	BosunFailed   = "failed"
)

// DueBosunRequests lists requests with work left: pending ones to file and
// filed ones to follow.
func (s *Store) DueBosunRequests(ctx context.Context, now time.Time, limit int) ([]BosunRequest, error) {
	rows, err := s.DB.QueryContext(ctx, `
		SELECT id, guild_id, channel_id, message_id, title, description, state,
			coalesce(issue_id, 0), coalesce(issue_key, ''), tries
		FROM bosun_request
		WHERE state IN ('pending', 'filed') AND next_check_at <= $1
		ORDER BY next_check_at, id LIMIT $2`, now, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []BosunRequest
	for rows.Next() {
		var r BosunRequest
		if err := rows.Scan(&r.ID, &r.Guild, &r.Channel, &r.Message, &r.Title, &r.Description, &r.State,
			&r.IssueID, &r.IssueKey, &r.Tries); err != nil {
			return nil, err
		}
		out = append(out, r)
	}
	return out, rows.Err()
}

// FileBosunRequest records the request's issue.
func (s *Store) FileBosunRequest(ctx context.Context, q Querier, id, issueID int64, key, column string, next time.Time) error {
	_, err := q.ExecContext(ctx, `UPDATE bosun_request
		SET state = 'filed', issue_id = $2, issue_key = $3, column_name = $4, filed_at = now(),
			error = NULL, next_check_at = $5
		WHERE id = $1 AND state = 'pending'`, id, issueID, key, column, next)
	return err
}

// FollowBosunRequest records the issue's column and when to look again.
func (s *Store) FollowBosunRequest(ctx context.Context, q Querier, id int64, column string, next time.Time) error {
	_, err := q.ExecContext(ctx, `UPDATE bosun_request SET column_name = $2, next_check_at = $3
		WHERE id = $1 AND state = 'filed'`, id, column, next)
	return err
}

// FinishBosunRequest records that the issue reached Done or Canceled.
func (s *Store) FinishBosunRequest(ctx context.Context, q Querier, id int64, state, column string, at time.Time) error {
	_, err := q.ExecContext(ctx, `UPDATE bosun_request
		SET state = $2, column_name = $3, finished_at = $4, next_check_at = $4
		WHERE id = $1 AND state = 'filed' AND $2 IN ('done', 'canceled')`, id, state, column, at)
	return err
}

// RetryBosunRequest counts a failed step and backs off. After maxTries a
// pending request fails; a filed one keeps retrying, because its issue
// already exists.
func (s *Store) RetryBosunRequest(ctx context.Context, q Querier, id int64, reason string, next time.Time, maxTries int) error {
	_, err := q.ExecContext(ctx, `UPDATE bosun_request
		SET tries = tries + 1, error = left($2, 500), next_check_at = $3,
			state = CASE WHEN state = 'pending' AND tries + 1 >= $4 THEN 'failed' ELSE state END
		WHERE id = $1`, id, reason, next, maxTries)
	return err
}
