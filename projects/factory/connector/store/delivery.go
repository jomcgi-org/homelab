package store

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"time"

	"github.com/lib/pq"
)

// FinishTurn records a completed turn: the work item goes to review, unless
// its merge was already observed, and its PR must reflect the attempt.
func (s *Store) FinishTurn(ctx context.Context, q Querier, workID, attemptID int64) error {
	_, err := q.ExecContext(ctx, `
		UPDATE work_item SET turn_attempt_id = $2,
			state = CASE WHEN state IN ('done', 'stopped') THEN state ELSE 'review' END, updated_at = now()
		WHERE id = $1`, workID, attemptID)
	return err
}

// MarkSynced records that the work item's PR reflects the attempt's turn.
func (s *Store) MarkSynced(ctx context.Context, q Querier, workID, attemptID int64) error {
	_, err := q.ExecContext(ctx, `UPDATE work_item SET synced_attempt_id = $2, updated_at = now() WHERE id = $1`,
		workID, attemptID)
	return err
}

// DeliveryWork lists work items whose PR the connector follows, in review or
// in the merge queue, and merged ones with a turn it has not looked at.
func (s *Store) DeliveryWork(ctx context.Context) ([]*WorkItem, error) {
	rows, err := s.DB.QueryContext(ctx, `SELECT `+workCols+` FROM work_item
		WHERE state IN ('review', 'merging')
			OR (state = 'done' AND turn_attempt_id IS DISTINCT FROM synced_attempt_id)
		ORDER BY id`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []*WorkItem
	for rows.Next() {
		w, err := scanWork(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, w)
	}
	return out, rows.Err()
}

// PullRequest is the one PR of a work item.
type PullRequest struct {
	ID              int64
	WorkItemID      int64
	Number          int
	NodeID          string
	URL             string
	HeadSHA         string
	State           string // open | closed | merged
	MergeQueueState string
	PolledAt        time.Time
	EnqueuedHeadSHA string
	EjectedHeadSHA  string
	MergeCommitSHA  string
}

const prCols = `id, work_item_id, number, node_id, url, head_sha, state, coalesce(merge_queue_state, ''),
	coalesce(polled_at, 'epoch'::timestamptz), coalesce(enqueued_head_sha, ''), coalesce(ejected_head_sha, ''),
	coalesce(merge_commit_sha, '')`

func scanPR(row interface{ Scan(...any) error }) (*PullRequest, error) {
	p := &PullRequest{}
	err := row.Scan(&p.ID, &p.WorkItemID, &p.Number, &p.NodeID, &p.URL, &p.HeadSHA, &p.State, &p.MergeQueueState,
		&p.PolledAt, &p.EnqueuedHeadSHA, &p.EjectedHeadSHA, &p.MergeCommitSHA)
	return p, err
}

// PullRequestFor returns the work item's PR, or nil.
func (s *Store) PullRequestFor(ctx context.Context, workID int64) (*PullRequest, error) {
	p, err := scanPR(s.DB.QueryRowContext(ctx, `SELECT `+prCols+` FROM pull_request WHERE work_item_id = $1`, workID))
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	return p, err
}

// RecordPullRequest records the work item's PR as GitHub reports it. A work
// item has one PR; recording another replaces it.
func (s *Store) RecordPullRequest(ctx context.Context, q Querier, p PullRequest) (*PullRequest, error) {
	return scanPR(q.QueryRowContext(ctx, `
		INSERT INTO pull_request (work_item_id, number, node_id, url, head_sha, state)
		VALUES ($1, $2, $3, $4, $5, $6)
		ON CONFLICT (work_item_id) DO UPDATE SET number = $2, node_id = $3, url = $4, head_sha = $5,
			state = $6, updated_at = now()
		RETURNING `+prCols, p.WorkItemID, p.Number, p.NodeID, p.URL, p.HeadSHA, p.State))
}

// SetPolled records a poll of the PR and what it showed.
func (s *Store) SetPolled(ctx context.Context, id int64, headSHA, state, queueState string, at time.Time) error {
	_, err := s.DB.ExecContext(ctx, `
		UPDATE pull_request SET head_sha = $2, state = $3, merge_queue_state = nullif($4, ''), polled_at = $5,
			updated_at = now()
		WHERE id = $1`, id, headSHA, state, queueState, at)
	return err
}

// MarkEnqueued records that the PR entered the merge queue at headSHA.
func (s *Store) MarkEnqueued(ctx context.Context, q Querier, p *PullRequest, headSHA, queueState string) error {
	if _, err := q.ExecContext(ctx, `
		UPDATE pull_request SET enqueued_head_sha = $2, merge_queue_state = nullif($3, ''), updated_at = now()
		WHERE id = $1`, p.ID, headSHA, queueState); err != nil {
		return err
	}
	return s.SetWorkState(ctx, q, p.WorkItemID, WorkMerging)
}

// MarkEjected returns a work item whose PR left the merge queue unmerged to
// review. Its head is not enqueued again.
func (s *Store) MarkEjected(ctx context.Context, q Querier, p *PullRequest) error {
	if _, err := q.ExecContext(ctx, `
		UPDATE pull_request SET ejected_head_sha = enqueued_head_sha, enqueued_head_sha = NULL,
			merge_queue_state = 'ejected', updated_at = now()
		WHERE id = $1`, p.ID); err != nil {
		return err
	}
	return s.SetWorkState(ctx, q, p.WorkItemID, WorkReview)
}

// MarkMerged records the observed merge and finishes the work item.
func (s *Store) MarkMerged(ctx context.Context, q Querier, p *PullRequest, mergeSHA string, at time.Time) error {
	if _, err := q.ExecContext(ctx, `
		UPDATE pull_request SET state = 'merged', merge_commit_sha = nullif($2, ''), merged_at = $3,
			merge_queue_state = 'merged', updated_at = now()
		WHERE id = $1`, p.ID, mergeSHA, at); err != nil {
		return err
	}
	return s.SetWorkState(ctx, q, p.WorkItemID, WorkDone)
}

// Review event dispositions.
const (
	EventActed      = "acted"
	EventStaleHead  = "stale_head"
	EventUntrusted  = "untrusted"
	EventDuplicate  = "duplicate"
	EventRoundLimit = "round_limit"
)

// Review event kinds.
const (
	EventChangesRequested = "changes_requested"
	EventCheckFailed      = "check_failed"
	EventStatusFailed     = "status_failed"
)

// ReviewEvent is one GitHub event on an owned PR that could request changes.
type ReviewEvent struct {
	PullRequestID int64
	NodeID        string
	CommitSHA     string
	Author        string
	Kind          string
	// Source is the reviewer login or the check or status name.
	Source      string
	Disposition string
	RoundRunID  int64
}

// SeenEvents returns which of the node IDs are already recorded.
func (s *Store) SeenEvents(ctx context.Context, nodeIDs []string) (map[string]bool, error) {
	seen := map[string]bool{}
	if len(nodeIDs) == 0 {
		return seen, nil
	}
	rows, err := s.DB.QueryContext(ctx, `SELECT github_node_id FROM review_event WHERE github_node_id = ANY($1)`,
		pq.Array(nodeIDs))
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			return nil, err
		}
		seen[id] = true
	}
	return seen, rows.Err()
}

// ActedOn reports whether a round already acted on an event of this kind and
// source at the commit.
func (s *Store) ActedOn(ctx context.Context, prID int64, commit, kind, source string) (bool, error) {
	var ok bool
	err := s.DB.QueryRowContext(ctx, `
		SELECT EXISTS (SELECT 1 FROM review_event WHERE pull_request_id = $1 AND commit_sha = $2
			AND kind = $3 AND source = $4 AND disposition = 'acted')`, prID, commit, kind, source).Scan(&ok)
	return ok, err
}

// RecordEvent records an event once, by its GitHub node ID. inserted is false
// for a node already recorded.
func (s *Store) RecordEvent(ctx context.Context, q Querier, ev ReviewEvent) (inserted bool, err error) {
	res, err := q.ExecContext(ctx, `
		INSERT INTO review_event (pull_request_id, github_node_id, commit_sha, author, kind, source, disposition, round_run_id)
		VALUES ($1, $2, $3, $4, $5, $6, $7, nullif($8, 0))
		ON CONFLICT (github_node_id) DO NOTHING`,
		ev.PullRequestID, ev.NodeID, ev.CommitSHA, ev.Author, ev.Kind, ev.Source, ev.Disposition, ev.RoundRunID)
	if err != nil {
		return false, err
	}
	n, err := res.RowsAffected()
	return n > 0, err
}

// StopWork stops delivery for a work item and queues the announcement, in
// one transaction. Dispatch then cancels its review rounds. stopped is false
// when it was already stopped or is done.
func (s *Store) StopWork(ctx context.Context, workID int64, reason string) (stopped bool, err error) {
	err = s.Tx(ctx, func(tx *sql.Tx) error {
		var issueID int64
		var key string
		var n int
		err := tx.QueryRowContext(ctx, `
			UPDATE work_item SET state = 'stopped', stopped_reason = $2, stops = stops + 1, updated_at = now()
			WHERE id = $1 AND state NOT IN ('stopped', 'done')
			RETURNING issue_id, issue_key, stops`, workID, reason).Scan(&issueID, &key, &n)
		if errors.Is(err, sql.ErrNoRows) {
			return nil
		}
		if err != nil {
			return err
		}
		stopped = true
		return s.Enqueue(ctx, tx, OutboxComment, fmt.Sprintf("comment/stopped/%d/%d", workID, n), 0, "",
			map[string]any{"issueId": issueID, "body": fmt.Sprintf(
				"The factory stopped delivering %s: %s. Its review rounds are canceled and no PR updates follow. "+
					"A new run on the issue resumes delivery.", key, reason)})
	})
	return stopped, err
}

// StopIssue is the operator's `connector cancel <issue>`: it stops the
// work item of the issue key.
func (s *Store) StopIssue(ctx context.Context, issueKey, reason string) (bool, error) {
	var id int64
	err := s.DB.QueryRowContext(ctx, `SELECT id FROM work_item WHERE issue_key = $1`, issueKey).Scan(&id)
	if errors.Is(err, sql.ErrNoRows) {
		return false, fmt.Errorf("no work item for %s", issueKey)
	}
	if err != nil {
		return false, err
	}
	return s.StopWork(ctx, id, reason)
}

// StartRound records a review round: a connector run in the work item's
// session, counted against its bound. The run waits until the work item and
// its agent key's concurrency let dispatch send it.
func (s *Store) StartRound(ctx context.Context, q Querier, w *WorkItem, prompt string) (*Run, error) {
	if _, err := q.ExecContext(ctx, `
		UPDATE work_item SET review_rounds = review_rounds + 1, state = 'active', updated_at = now()
		WHERE id = $1`, w.ID); err != nil {
		return nil, err
	}
	return scanRun(q.QueryRowContext(ctx, `
		INSERT INTO run (id, origin, agent_key, work_item_id, issue_id, issue_key, trigger, prompt, system_prompt,
			attempt, state, seen_visible)
		VALUES (nextval('connector_run_id'), 'connector', $1, $2, $3, $4, 'review', $5, '', 1, 'waiting', true)
		RETURNING `+runCols, w.AgentKey, w.ID, w.IssueID, w.IssueKey, prompt))
}

// LatestTurn returns the work item's latest completed attempt and its run,
// or nils.
func (s *Store) LatestTurn(ctx context.Context, workID int64) (*Attempt, *Run, error) {
	a, err := scanAttempt(s.DB.QueryRowContext(ctx, `
		SELECT `+attemptCols+` FROM delivery_attempt
		WHERE id = (SELECT turn_attempt_id FROM work_item WHERE id = $1)`, workID))
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil, nil
	}
	if err != nil {
		return nil, nil, err
	}
	r, err := s.GetRun(ctx, a.RunID)
	return a, r, err
}

// CountResends counts a run's resends after a crashed session was reverted.
func (s *Store) CountResends(ctx context.Context, runID int64) (int, error) {
	var n int
	err := s.DB.QueryRowContext(ctx,
		`SELECT count(*) FROM delivery_attempt WHERE run_id = $1 AND decision_id LIKE 'resend-%'`, runID).Scan(&n)
	return n, err
}
