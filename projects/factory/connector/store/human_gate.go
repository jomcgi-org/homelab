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

// invariant: FACT-14

// HumanGateWork includes active revisions and stopped deliveries. Canceled
// gate issues stay eligible so reopening a PR is observed even after delivery
// stops. Only an acknowledged merge can end gate polling.
func (s *Store) HumanGateWork(ctx context.Context) ([]HumanGatePoll, error) {
	rows, err := s.DB.QueryContext(ctx, `SELECT p.work_item_id, p.human_gate_polled_at
		FROM pull_request p LEFT JOIN human_gate_issue h ON h.pull_request_id = p.id
		WHERE (p.state = 'open' AND h.id IS NULL) OR h.state <> 'Done'
			OR EXISTS (SELECT 1 FROM human_gate_attempt a WHERE a.gate_id = h.id
				AND a.operation = 'update' AND a.revision = h.revision
				AND a.finished_at > now() - interval '24 hours')
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
	// CreateSends counts the create's started sends, cleared ones included.
	CreateSends int
}

const humanGateCols = `id, coalesce(issue_id, 0), head_sha, state, description, revision, applied_revision,
	(SELECT coalesce(max(a.sends), 0) FROM human_gate_attempt a
		WHERE a.gate_id = human_gate_issue.id AND a.operation = 'create')`

func scanHumanGate(row interface{ Scan(...any) error }) (*HumanGateIssue, error) {
	h := &HumanGateIssue{}
	err := row.Scan(&h.ID, &h.IssueID, &h.HeadSHA, &h.State, &h.Description, &h.Revision, &h.AppliedRevision, &h.CreateSends)
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
	Sends       int
	// SendAge is the time since the last send was marked, by the database
	// clock, so the connector's clock cannot shorten it. Zero if never sent.
	SendAge time.Duration
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
	var age float64
	err = s.DB.QueryRowContext(ctx, `SELECT payload, send_started, finished_at IS NOT NULL, sends,
			coalesce(extract(epoch FROM now() - attempted_at), 0)::float8
		FROM human_gate_attempt WHERE message_id = $1`, messageID).Scan(&a.Payload, &a.SendStarted, &a.Finished, &a.Sends, &age)
	a.SendAge = time.Duration(age * float64(time.Second))
	return a, err
}

// StartHumanGateWrite checks authority and marks the send in one transaction
// on the advisory-lock session. A failed guard or aborted transaction leaves
// the attempt unsent without requiring a compensating write. After commit the
// caller must send immediately, without another database guard in between.
// previous is the send count the caller read, so two sends cannot both start.
func (s *Store) StartHumanGateWrite(ctx context.Context, w *Writer, messageID string, previous int) (int, error) {
	var send int
	err := w.Tx(ctx, func(tx *sql.Tx) error {
		return tx.QueryRowContext(ctx, `UPDATE human_gate_attempt SET send_started = true, finished_at = NULL,
			sends = sends + 1, writer_epoch = $2, attempted_at = now() WHERE message_id = $1 AND sends = $3
			RETURNING sends`, messageID, w.Epoch, previous).Scan(&send)
	})
	return send, err
}

// ClearHumanGateSend records a definite refusal of this send, so the next
// poll can retry at once with its current payload. The epoch/send token
// cannot clear a later send or an acknowledged attempt.
func (s *Store) ClearHumanGateSend(ctx context.Context, messageID string, epoch int64, send int) error {
	_, err := s.DB.ExecContext(ctx, `UPDATE human_gate_attempt SET send_started = false
		WHERE message_id = $1 AND writer_epoch = $2 AND sends = $3 AND finished_at IS NULL`, messageID, epoch, send)
	return err
}

func (s *Store) FinishHumanGateWrite(ctx context.Context, h *HumanGateIssue, messageID string, issueID int64, applied bool) error {
	return s.Tx(ctx, func(tx *sql.Tx) error {
		if _, err := tx.ExecContext(ctx, `UPDATE human_gate_attempt SET finished_at = now() WHERE message_id = $1`, messageID); err != nil {
			return err
		}
		return tx.QueryRowContext(ctx, `UPDATE human_gate_issue SET issue_id = $2,
			applied_revision = CASE WHEN $3 THEN $4 ELSE applied_revision END
			WHERE id = $1 AND (issue_id IS NULL OR issue_id = $2) RETURNING id`, h.ID, issueID, applied, h.Revision).Scan(new(int64))
	})
}

// FinishHumanGateCancel acknowledges a duplicate's move to Canceled and, in
// the same transaction, queues its comment under the attempt's message ID.
func (s *Store) FinishHumanGateCancel(ctx context.Context, messageID string, issueID int64, comment string) error {
	return s.Tx(ctx, func(tx *sql.Tx) error {
		if _, err := tx.ExecContext(ctx, `UPDATE human_gate_attempt SET finished_at = now() WHERE message_id = $1`, messageID); err != nil {
			return err
		}
		return s.Enqueue(ctx, tx, OutboxComment, "comment/"+messageID, 0, "", map[string]any{"issueId": issueID, "body": comment, "once": true})
	})
}

// SettleLegacyGateComment delays the first feed lookup after takeover. Save
// the deadline with the row so a restart cannot skip the settling period.
func (s *Store) SettleLegacyGateComment(ctx context.Context, w *Writer, id int64, next time.Time) error {
	return w.Tx(ctx, func(tx *sql.Tx) error {
		_, err := tx.ExecContext(ctx, `UPDATE outbox SET payload=payload || '{"legacySettling":true}'::jsonb, next_attempt_at=$2 WHERE id=$1`, id, next)
		return err
	})
}

// HoldGateComment keeps the explanation pending for operator resolution and
// schedules another feed check and reminder if the outcome remains unknown.
func (s *Store) HoldGateComment(ctx context.Context, w *Writer, id int64, reason string, next time.Time) error {
	return w.Tx(ctx, func(tx *sql.Tx) error {
		_, err := tx.ExecContext(ctx, `UPDATE outbox SET last_error=$2, next_attempt_at=$3 WHERE id=$1 AND delivered_at IS NULL AND abandoned_at IS NULL AND NOT (payload @> '{"once":true}'::jsonb)`, id, reason, next)
		return err
	})
}
