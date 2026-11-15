package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"time"
)

type HumanGatePoll struct {
	WorkItemID int64
	PolledAt   time.Time
}

// HumanGateWork includes active revisions and stopped deliveries. Canceled
// gate issues stay eligible so reopening a PR is observed even after delivery
// stops. Only an acknowledged merge can end gate polling.
func (s *Store) HumanGateWork(ctx context.Context) ([]HumanGatePoll, error) {
	rows, err := s.DB.QueryContext(ctx, `SELECT p.work_item_id, p.human_gate_polled_at
		FROM pull_request p LEFT JOIN human_gate_issue h ON h.pull_request_id = p.id
		WHERE p.state = 'open' OR h.state <> 'Done'
			OR h.applied_revision <> h.revision ORDER BY p.id`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []HumanGatePoll
	for rows.Next() {
		var p HumanGatePoll
		var at sql.NullTime
		if err := rows.Scan(&p.WorkItemID, &at); err != nil {
			return nil, err
		}
		p.PolledAt = at.Time
		out = append(out, p)
	}
	return out, rows.Err()
}

func (s *Store) HumanGatePolled(ctx context.Context, prID int64, at time.Time) error {
	_, err := s.DB.ExecContext(ctx, `UPDATE pull_request SET human_gate_polled_at = $2 WHERE id = $1`, prID, at)
	return err
}

type HumanGateIssue struct {
	ID              int64
	IssueID         int64
	HeadSHA         string
	State           string
	Description     string
	Revision        int
	AppliedRevision int
}

const humanGateCols = `id, coalesce(issue_id, 0), head_sha, state, description, revision, applied_revision`

func scanHumanGate(row interface{ Scan(...any) error }) (*HumanGateIssue, error) {
	h := &HumanGateIssue{}
	err := row.Scan(&h.ID, &h.IssueID, &h.HeadSHA, &h.State, &h.Description, &h.Revision, &h.AppliedRevision)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	return h, err
}

func (s *Store) HumanGateFor(ctx context.Context, repo string, number int) (*HumanGateIssue, error) {
	return scanHumanGate(s.DB.QueryRowContext(ctx, `SELECT `+humanGateCols+`
		FROM human_gate_issue WHERE repository = $1 AND number = $2`, repo, number))
}

// ObserveHumanGate advances the desired version only when the evidence changes.
// The repository/PR key also deduplicates adoption by another work item.
func (s *Store) ObserveHumanGate(ctx context.Context, repo string, pr *PullRequest, head, state, description string) (*HumanGateIssue, error) {
	return scanHumanGate(s.DB.QueryRowContext(ctx, `INSERT INTO human_gate_issue
		(repository, number, pull_request_id, head_sha, state, description) VALUES ($1, $2, $3, $4, $5, $6)
		ON CONFLICT (repository, number) DO UPDATE SET
			pull_request_id = EXCLUDED.pull_request_id, head_sha = EXCLUDED.head_sha,
			state = EXCLUDED.state, description = EXCLUDED.description,
			revision = human_gate_issue.revision + CASE WHEN
				(human_gate_issue.head_sha, human_gate_issue.state, human_gate_issue.description)
				IS DISTINCT FROM (EXCLUDED.head_sha, EXCLUDED.state, EXCLUDED.description) THEN 1 ELSE 0 END
		RETURNING `+humanGateCols, repo, pr.Number, pr.ID, head, state, description))
}

type HumanGateAttempt struct {
	MessageID   string
	Payload     json.RawMessage
	SendStarted bool
	Finished    bool
}

// HumanGateWrite freezes a write before it is sent; retries load those bytes.
// A create proven not to have been accepted may take the current payload, so
// validation failures cannot freeze an invalid description under its stable ID.
func (s *Store) HumanGateWrite(ctx context.Context, h *HumanGateIssue, epoch int64, messageID, operation string, payload any) (*HumanGateAttempt, error) {
	data, err := json.Marshal(payload)
	if err != nil {
		return nil, err
	}
	_, err = s.DB.ExecContext(ctx, `INSERT INTO human_gate_attempt
		(message_id, gate_id, operation, revision, payload, writer_epoch) VALUES ($1, $2, $3, $4, $5, $6)
		ON CONFLICT (message_id) DO UPDATE SET payload = EXCLUDED.payload,
			revision = EXCLUDED.revision, writer_epoch = EXCLUDED.writer_epoch
		WHERE human_gate_attempt.operation = 'create' AND EXCLUDED.operation = 'create'
			AND NOT human_gate_attempt.send_started AND human_gate_attempt.finished_at IS NULL`,
		messageID, h.ID, operation, h.Revision, data, epoch)
	if err != nil {
		return nil, err
	}
	a := &HumanGateAttempt{MessageID: messageID}
	err = s.DB.QueryRowContext(ctx, `SELECT payload, send_started, finished_at IS NOT NULL
		FROM human_gate_attempt WHERE message_id = $1`, messageID).Scan(&a.Payload, &a.SendStarted, &a.Finished)
	return a, err
}

func (s *Store) StartHumanGateWrite(ctx context.Context, messageID string, epoch int64) (int, error) {
	var send int
	err := s.DB.QueryRowContext(ctx, `UPDATE human_gate_attempt SET send_started = true, finished_at = NULL,
		sends = sends + 1, writer_epoch = $2, attempted_at = now() WHERE message_id = $1
		RETURNING sends`, messageID, epoch).Scan(&send)
	return send, err
}

// ClearHumanGateSend records proof that this send did not create an issue,
// including a canceled writer guard before POST. Cleanup survives cancellation
// but cannot clear a later writer's send or an acknowledged attempt.
func (s *Store) ClearHumanGateSend(ctx context.Context, messageID string, epoch int64, send int) error {
	ctx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 5*time.Second)
	defer cancel()
	_, err := s.DB.ExecContext(ctx, `UPDATE human_gate_attempt SET send_started = false
		WHERE message_id = $1 AND writer_epoch = $2 AND sends = $3 AND finished_at IS NULL`, messageID, epoch, send)
	return err
}

func (s *Store) FinishHumanGateWrite(ctx context.Context, h *HumanGateIssue, messageID string, issueID int64, applied bool) error {
	return s.Tx(ctx, func(tx *sql.Tx) error {
		if _, err := tx.ExecContext(ctx, `UPDATE human_gate_attempt SET finished_at = now() WHERE message_id = $1`, messageID); err != nil {
			return err
		}
		_, err := tx.ExecContext(ctx, `UPDATE human_gate_issue SET issue_id = $2,
			applied_revision = CASE WHEN $3 THEN $4 ELSE applied_revision END WHERE id = $1`, h.ID, issueID, applied, h.Revision)
		return err
	})
}
