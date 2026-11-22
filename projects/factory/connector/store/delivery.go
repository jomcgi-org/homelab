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

// DeliveryWork lists work items whose PR the connector follows: in review or
// in the merge queue, merged ones with a turn it has not looked at, and
// stopped ones whose PR is open, to dequeue it and observe a merge.
func (s *Store) DeliveryWork(ctx context.Context, due time.Time) ([]*WorkItem, error) {
	rows, err := s.DB.QueryContext(ctx, `SELECT `+workCols+` FROM work_item w
		WHERE (state IN ('review', 'merging') AND
			(turn_attempt_id IS DISTINCT FROM synced_attempt_id OR EXISTS (
				SELECT 1 FROM pull_request p WHERE p.work_item_id = w.id
				AND (p.polled_at IS NULL OR p.polled_at <= $1))))
			OR (state = 'done' AND turn_attempt_id IS DISTINCT FROM synced_attempt_id)
			OR (state = 'stopped' AND EXISTS (
				SELECT 1 FROM pull_request p WHERE p.work_item_id = w.id AND p.state = 'open'
				AND (p.polled_at IS NULL OR p.polled_at <= $1)))
		ORDER BY id`, due)
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
	State           string // open | closed | merged | refused
	MergeQueueState string
	PolledAt        time.Time
	EnqueuedHeadSHA string
	EjectedHeadSHA  string
	MergeCommitSHA  string
	MergeGroupSHA   string
}

const prCols = `id, work_item_id, number, node_id, url, head_sha, state, coalesce(merge_queue_state, ''),
	coalesce(polled_at, 'epoch'::timestamptz), coalesce(enqueued_head_sha, ''), coalesce(ejected_head_sha, ''),
	coalesce(merge_commit_sha, ''), coalesce(merge_group_sha, '')`

func scanPR(row interface{ Scan(...any) error }) (*PullRequest, error) {
	p := &PullRequest{}
	err := row.Scan(&p.ID, &p.WorkItemID, &p.Number, &p.NodeID, &p.URL, &p.HeadSHA, &p.State, &p.MergeQueueState,
		&p.PolledAt, &p.EnqueuedHeadSHA, &p.EjectedHeadSHA, &p.MergeCommitSHA, &p.MergeGroupSHA)
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

// RecordDeliveryRead records an attempt before reading external evidence.
// Repeating the same idempotent read keeps its message ID and counts the
// attempt, including retries after a writer takeover.
func (s *Store) RecordDeliveryRead(ctx context.Context, prID, epoch int64, messageID, operation, resource string) error {
	_, err := s.DB.ExecContext(ctx, `
		INSERT INTO delivery_read (message_id, pull_request_id, operation, resource, writer_epoch)
		VALUES ($1, $2, $3, $4, $5)
		ON CONFLICT (message_id) DO UPDATE SET attempts = delivery_read.attempts + 1,
			writer_epoch = EXCLUDED.writer_epoch, attempted_at = now()`, messageID, prID, operation, resource, epoch)
	return err
}

// RecordPullRequest records the work item's PR as GitHub reports it. A work
// item has one PR; recording another replaces it.
func (s *Store) RecordPullRequest(ctx context.Context, q Querier, p PullRequest) (*PullRequest, error) {
	return scanPR(q.QueryRowContext(ctx, `
		INSERT INTO pull_request (work_item_id, number, node_id, url, head_sha, state)
		VALUES ($1, $2, $3, $4, $5, $6)
		ON CONFLICT (work_item_id) DO UPDATE SET number = $2, node_id = $3, url = $4, head_sha = $5,
			state = $6, updated_at = now(),
            held_duration = CASE WHEN pull_request.node_id = EXCLUDED.node_id THEN pull_request.held_duration ELSE interval '0' END,
            held_since = CASE WHEN pull_request.node_id = EXCLUDED.node_id THEN pull_request.held_since END
        RETURNING `+prCols, p.WorkItemID, p.Number, p.NodeID, p.URL, p.HeadSHA, p.State))
}

// SetPolled records a poll of the PR and what it showed. Keep the latest
// synthetic commit when the queue entry disappears so its checks remain
// available after an ejection or a connector restart.
func (s *Store) SetPolled(ctx context.Context, id int64, headSHA, state, queueState, mergeGroupSHA string, held bool, at time.Time) error {
	_, err := s.DB.ExecContext(ctx, `
		UPDATE pull_request SET head_sha = $2, state = $3, merge_queue_state = nullif($4, ''), polled_at = $5,
            held_duration = held_duration + CASE WHEN NOT $7 AND held_since IS NOT NULL
                THEN greatest($5::timestamptz - held_since, interval '0') ELSE interval '0' END,
            held_since = CASE WHEN $7 THEN coalesce(held_since, $5) END,
			merge_group_sha = coalesce(nullif($6, ''), merge_group_sha),
			updated_at = now()
		WHERE id = $1`, id, headSHA, state, queueState, at, mergeGroupSHA, held)
	return err
}

// moveLive moves a work item to state unless delivery was stopped, which
// only a new itsaplan run undoes. moved reports whether the row changed.
func (s *Store) moveLive(ctx context.Context, q Querier, workID int64, state string) (moved bool, err error) {
	res, err := q.ExecContext(ctx, `UPDATE work_item SET state = $2, updated_at = now() WHERE id = $1 AND state <> 'stopped'`,
		workID, state)
	if err != nil {
		return false, err
	}
	n, err := res.RowsAffected()
	return n > 0, err
}

// MarkEnqueued records that the PR entered the merge queue at headSHA. The
// PR's queue entry is recorded either way, since GitHub holds it; moved is
// false when delivery stopped meanwhile and the entry must be dequeued.
// Each enqueue replaces the previous entry's synthetic commit, even when
// GitHub has not built the new one yet.
func (s *Store) MarkEnqueued(ctx context.Context, q Querier, p *PullRequest, headSHA, queueState, mergeGroupSHA string) (moved bool, err error) {
	if _, err := q.ExecContext(ctx, `
		UPDATE pull_request SET enqueued_head_sha = $2, merge_queue_state = nullif($3, ''),
			merge_group_sha = nullif($4, ''), updated_at = now()
		WHERE id = $1`, p.ID, headSHA, queueState, mergeGroupSHA); err != nil {
		return false, err
	}
	return s.moveLive(ctx, q, p.WorkItemID, WorkMerging)
}

// MarkDequeued records that the connector took the PR out of the queue.
func (s *Store) MarkDequeued(ctx context.Context, p *PullRequest) error {
	_, err := s.DB.ExecContext(ctx, `
		UPDATE pull_request SET enqueued_head_sha = NULL, merge_queue_state = 'dequeued', updated_at = now()
		WHERE id = $1`, p.ID)
	return err
}

// MarkEjected returns a work item whose PR left the merge queue unmerged to
// review. Its head is not enqueued again. Only an item still merging moves,
// so replaying an ejection cannot interrupt a revision or undo a stop.
func (s *Store) MarkEjected(ctx context.Context, q Querier, p *PullRequest) (moved bool, err error) {
	res, err := q.ExecContext(ctx, `UPDATE work_item SET state = 'review', updated_at = now()
		WHERE id = $1 AND state = 'merging'`, p.WorkItemID)
	if err != nil {
		return false, err
	}
	n, err := res.RowsAffected()
	if err != nil || n == 0 {
		return false, err
	}
	_, err = q.ExecContext(ctx, `
		UPDATE pull_request SET ejected_head_sha = enqueued_head_sha, enqueued_head_sha = NULL,
			merge_queue_state = 'ejected', updated_at = now()
		WHERE id = $1`, p.ID)
	return true, err
}

// MarkMerged records the observed merge. done is false for a stopped work
// item, which keeps its state: its issue is not moved to Done.
func (s *Store) MarkMerged(ctx context.Context, q Querier, p *PullRequest, mergeSHA string, at time.Time) (done bool, err error) {
	if _, err := q.ExecContext(ctx, `
		UPDATE pull_request SET state = 'merged', merge_commit_sha = nullif($2, ''), merged_at = $3,
			merge_queue_state = 'merged', updated_at = now()
		WHERE id = $1`, p.ID, mergeSHA, at); err != nil {
		return false, err
	}
	return s.moveLive(ctx, q, p.WorkItemID, WorkDone)
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
	EventConflicting      = "conflicting"
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

// LimitedReviews identifies refused reviews for a live state check in the PR poll.
func (s *Store) LimitedReviews(ctx context.Context, prID int64, head string) ([]string, error) {
	rows, err := s.DB.QueryContext(ctx, `SELECT github_node_id FROM review_event
		WHERE pull_request_id = $1 AND commit_sha = $2 AND kind = 'changes_requested'
		AND disposition = 'round_limit' ORDER BY id`, prID, head)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var ids []string
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			return nil, err
		}
		ids = append(ids, id)
	}
	return ids, rows.Err()
}

// invariant: HOME-109-1

// SeenEvents returns recorded node IDs, excluding retryable events on the current head.
func (s *Store) SeenEvents(ctx context.Context, nodeIDs []string, head string, retryReview, retryRebase bool) (map[string]bool, error) {
	seen := map[string]bool{}
	if len(nodeIDs) == 0 {
		return seen, nil
	}
	rows, err := s.DB.QueryContext(ctx, `SELECT github_node_id FROM review_event WHERE github_node_id = ANY($1)
		AND NOT (commit_sha = $2 AND disposition = 'round_limit'
			AND CASE WHEN kind = 'conflicting' THEN $4::boolean ELSE $3::boolean END)`,
		pq.Array(nodeIDs), head, retryReview, retryRebase)
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

// RecordEvent records a new event or resolves a previously round-limited event.
func (s *Store) RecordEvent(ctx context.Context, q Querier, ev ReviewEvent) (changed bool, err error) {
	res, err := q.ExecContext(ctx, `
		INSERT INTO review_event (pull_request_id, github_node_id, commit_sha, author, kind, source, disposition, round_run_id)
		VALUES ($1, $2, $3, $4, $5, $6, $7, nullif($8, 0))
		ON CONFLICT (github_node_id) DO UPDATE SET
			disposition = excluded.disposition, round_run_id = excluded.round_run_id
		WHERE review_event.disposition = 'round_limit' AND excluded.disposition <> 'round_limit'`,
		ev.PullRequestID, ev.NodeID, ev.CommitSHA, ev.Author, ev.Kind, ev.Source, ev.Disposition, ev.RoundRunID)
	if err != nil {
		return false, err
	}
	n, err := res.RowsAffected()
	return n > 0, err
}

// ErrNotReview means the work item left review, for example because
// delivery was stopped, before a round could start.
var ErrNotReview = errors.New("store: work item is no longer in review")

// StopWork stops delivery for a work item, persists the stop on its admitted
// runs and queues the announcement in one transaction. Dispatch then cancels
// those runs even if a new admission resumes the work item first. stopped is
// false when it was already stopped or has no delivery/activation left to stop.
func (s *Store) StopWork(ctx context.Context, workID int64, reason, by string) (stopped bool, err error) {
	err = s.Tx(ctx, func(tx *sql.Tx) error {
		stopped, err = s.stopWork(ctx, tx, workID, reason, by)
		return err
	})
	return stopped, err
}

// StopRefusedPR stops delivery and retires the recorded PR from stopped-work polling.
func (s *Store) StopRefusedPR(ctx context.Context, workID, prID int64, reason string) error {
	return s.Tx(ctx, func(tx *sql.Tx) error {
		reason += ". Check the GitHub merge queue and remove this PR if it is queued"
		stopped, err := s.stopWork(ctx, tx, workID, reason, "connector")
		if err != nil {
			return err
		}
		_, err = tx.ExecContext(ctx, `UPDATE pull_request SET state = 'refused', updated_at = now() WHERE id = $1 AND work_item_id = $2`, prID, workID)
		if err != nil || stopped {
			return err
		}
		// An earlier stop may already have been announced before the source check.
		var issueID, stops int64
		if err := tx.QueryRowContext(ctx, `SELECT issue_id, stops FROM work_item WHERE id = $1`, workID).Scan(&issueID, &stops); err != nil {
			return err
		}
		return s.Enqueue(ctx, tx, OutboxComment, fmt.Sprintf("comment/refused-pr/%d/%d", prID, stops), 0, "",
			map[string]any{"issueId": issueID, "body": reason + "."})
	})
}

func (s *Store) stopWork(ctx context.Context, tx *sql.Tx, workID int64, reason, by string) (bool, error) {
	var issueID int64
	var key string
	var n int
	err := tx.QueryRowContext(ctx, `
		UPDATE work_item SET state = 'stopped', stopped_reason = $2, stopped_by = $3, stops = stops + 1,
			updated_at = now()
		WHERE id = $1 AND state <> 'stopped' AND (state <> 'done' OR EXISTS (
			SELECT 1 FROM outbox WHERE kind = 'activation' AND (
				payload->>'workId' = work_item.id::text OR
				payload->>'followupId' = work_item.issue_id::text OR
				payload#>>'{complete,activationChild}' = work_item.issue_id::text)
			AND delivered_at IS NULL AND (abandoned_at IS NULL OR last_error = 'unknown kind activation')))
		RETURNING issue_id, issue_key, stops`, workID, reason, by).Scan(&issueID, &key, &n)
	if errors.Is(err, sql.ErrNoRows) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	// ResolveWorkItem and MarkIssued also lock the work item, so a new
	// admission cannot erase this stop and an old run cannot send past it.
	if _, err := tx.ExecContext(ctx, `
		UPDATE run SET stopped_reason = $2, updated_at = now()
		WHERE work_item_id = $1 AND stopped_reason IS NULL
			AND state IN ('claimed', 'waiting', 'dispatched', 'stale')`, workID, reason); err != nil {
		return false, err
	}
	// Poll the PR at once, so a queued PR is dequeued without waiting.
	if _, err := tx.ExecContext(ctx, `UPDATE pull_request SET polled_at = NULL WHERE work_item_id = $1`, workID); err != nil {
		return false, err
	}
	return true, s.Enqueue(ctx, tx, OutboxComment, fmt.Sprintf("comment/stopped/%d/%d", workID, n), 0, "",
		map[string]any{"issueId": issueID, "body": fmt.Sprintf(
			"The factory stopped delivering %s: %s. Its running turn is canceled. A new run on the issue resumes delivery.", key, reason)})
}

// IssueStop is what `connector cancel` stopped.
type IssueStop struct {
	WorkItem bool  // the issue's work item was live and is now stopped
	HeldRuns int64 // delegations not linked to a work item, stopped on their runs
}

// Any reports that the cancel stopped something.
func (s IssueStop) Any() bool { return s.WorkItem || s.HeldRuns > 0 }

// StopIssue is the operator's `connector cancel <issue>`. A live work item
// stops, and its stop counter fences each later delegation that admission
// has not linked yet. With no work item, or one already stopped or done, no
// counter moves, so the stop goes on each unlinked delegation's run, which
// ResolveWorkItem checks under the run's row lock (HOME-55).
func (s *Store) StopIssue(ctx context.Context, issueKey, reason, by string) (IssueStop, error) {
	var out IssueStop
	err := s.Tx(ctx, func(tx *sql.Tx) error {
		// Lock the work item before runs, in ResolveWorkItem's order.
		lookup := func() (int64, error) {
			var id int64
			err := tx.QueryRowContext(ctx, `SELECT id FROM work_item WHERE issue_key = $1 FOR UPDATE`, issueKey).Scan(&id)
			if errors.Is(err, sql.ErrNoRows) {
				return 0, nil
			}
			return id, err
		}
		id, err := lookup()
		if err != nil {
			return err
		}
		if id != 0 {
			if out.WorkItem, err = s.stopWork(ctx, tx, id, reason, by); err != nil || out.WorkItem {
				return err
			}
		}
		res, err := tx.ExecContext(ctx, `
			UPDATE run SET stopped_reason = $2, updated_at = now()
			WHERE issue_key = $1 AND work_item_id IS NULL AND origin = 'itsaplan'
				AND stopped_reason IS NULL AND state IN ('claimed', 'waiting', 'dispatched', 'stale')`, issueKey, reason)
		if err != nil {
			return err
		}
		if out.HeldRuns, err = res.RowsAffected(); err != nil || id != 0 {
			return err
		}
		// A run that resolved while this update waited for its row lock
		// committed a new work item. Stop that too.
		if id, err = lookup(); err != nil {
			return err
		}
		if id == 0 {
			// An undelegated activation issue has no work item yet. Its parent
			// owns provisioning and observation, so stop that work as well.
			err = tx.QueryRowContext(ctx, `SELECT work_item_id FROM activation_followup
				WHERE issue_key = $1`, issueKey).Scan(&id)
			if err != nil && !errors.Is(err, sql.ErrNoRows) {
				return err
			}
			if id != 0 {
				out.WorkItem, err = s.stopWork(ctx, tx, id, reason, by)
				return err
			}
			if out.HeldRuns == 0 {
				return fmt.Errorf("no work item or held run for %s", issueKey)
			}
			return nil
		}
		out.WorkItem, err = s.stopWork(ctx, tx, id, reason, by)
		return err
	})
	return out, err
}

// RouteReviewRound switches the next turn to a fresh control session in the
// same transaction that records the round and its triggering review events.
// review_rounds counts every round, CI fixes included, so the review
// threshold counts only the rounds that were not CI fixes.
func (s *Store) RouteReviewRound(ctx context.Context, q Querier, id int64, control string, ciFix bool) error {
	_, err := q.ExecContext(ctx, `UPDATE work_item SET ci_fix_rounds=ci_fix_rounds+CASE WHEN $2 THEN 1 ELSE 0 END WHERE id=$1`, id, ciFix)
	if err != nil {
		return err
	}
	_, err = q.ExecContext(ctx, `UPDATE work_item SET escalated=true,kagent_agent=$2,session_id=NULL
 WHERE id=$1 AND (review_rounds - ci_fix_rounds >= 3 OR ci_fix_rounds >= 2)`, id, control)
	return err
}

// StartRound records a review round: a connector run in the work item's
// session, counted against its bound. The run waits until the work item and
// its agent key's concurrency let dispatch send it.
func (s *Store) StartRound(ctx context.Context, q Querier, w *WorkItem, prompt, head string) (*Run, error) {
	return s.startRound(ctx, q, w, prompt, head, "review")
}

// StartRebaseRound uses the separate rebase budget, preserving review rounds.
func (s *Store) StartRebaseRound(ctx context.Context, q Querier, w *WorkItem, prompt, head string) (*Run, error) {
	return s.startRound(ctx, q, w, prompt, head, "rebase")
}

// StartSplitRound uses the separate split budget, preserving review rounds.
func (s *Store) StartSplitRound(ctx context.Context, q Querier, w *WorkItem, prompt, head string) (*Run, error) {
	return s.startRound(ctx, q, w, prompt, head, "split")
}

func (s *Store) startRound(ctx context.Context, q Querier, w *WorkItem, prompt, head, trigger string) (*Run, error) {
	res, err := q.ExecContext(ctx, `
		UPDATE work_item SET review_rounds = review_rounds + CASE WHEN $2 = 'review' THEN 1 ELSE 0 END,
			rebase_rounds = rebase_rounds + CASE WHEN $2 = 'rebase' THEN 1 ELSE 0 END,
			split_rounds = split_rounds + CASE WHEN $2 = 'split' THEN 1 ELSE 0 END,
			state = 'active', updated_at = now()
		WHERE id = $1 AND state = 'review'`, w.ID, trigger)
	if err != nil {
		return nil, err
	}
	if n, err := res.RowsAffected(); err != nil || n == 0 {
		if err == nil {
			err = ErrNotReview
		}
		return nil, err
	}
	return scanRun(q.QueryRowContext(ctx, `
		INSERT INTO run (id, origin, agent_key, work_item_id, issue_id, issue_key, trigger, prompt, system_prompt,
			attempt, state, seen_visible, provenance_required, round_head_sha)
		VALUES (nextval('connector_run_id'), 'connector', $1, $2, $3, $4, $7, $5, '', 1, 'waiting', true, true, nullif($6, ''))
		RETURNING `+runCols, w.AgentKey, w.ID, w.IssueID, w.IssueKey, prompt, head, trigger))
}

// Round is a work item's latest review round: its run, state and the head it
// was asked to change.
type Round struct {
	RunID   int64
	State   string
	HeadSHA string
	Trigger string
}

// LastRound returns the work item's most recent review round, or nil.
// Round run IDs count down, so the order is by creation.
func (s *Store) LastRound(ctx context.Context, workID int64) (*Round, error) {
	r := &Round{}
	err := s.DB.QueryRowContext(ctx, `
		SELECT id, state, coalesce(round_head_sha, ''), trigger FROM run
		WHERE work_item_id = $1 AND origin = 'connector' AND trigger IN ('review', 'rebase', 'split')
		ORDER BY created_at DESC, id ASC LIMIT 1`, workID).Scan(&r.RunID, &r.State, &r.HeadSHA, &r.Trigger)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return r, nil
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
