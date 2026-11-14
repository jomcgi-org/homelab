// Package store is the connector's Postgres state: the single-writer lock
// and epoch, repository bindings, work items, runs, delivery attempts,
// cancellations and the outbox.
package store

import (
	"context"
	"database/sql"
	"database/sql/driver"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	_ "github.com/lib/pq" // registers the postgres driver
)

// ErrNotWriter means this process no longer holds the writer lock at its
// epoch. Every external side effect checks for it first.
var ErrNotWriter = errors.New("store: not the current writer")

// writerLockKey is the session advisory lock that admits one writer.
const writerLockKey = 0x666163746f7279 // "factory"

// Store wraps the connector database.
type Store struct {
	DB *sql.DB
}

// Open connects to Postgres with the lib/pq driver.
func Open(dsn string) (*Store, error) {
	db, err := sql.Open("postgres", dsn)
	if err != nil {
		return nil, err
	}
	return &Store{DB: db}, nil
}

// Writer is a held writer lock. The lock lives on conn's session, so the
// epoch check runs there: a lost connection fails the check.
type Writer struct {
	conn  *sql.Conn
	Epoch int64
}

// TryAcquireWriter takes the advisory lock without waiting and, when it gets
// it, increments the writer epoch. It returns nil when another process holds
// the lock.
func (s *Store) TryAcquireWriter(ctx context.Context, holder string) (*Writer, error) {
	conn, err := s.DB.Conn(ctx)
	if err != nil {
		return nil, err
	}
	var ok bool
	if err := conn.QueryRowContext(ctx, `SELECT pg_try_advisory_lock($1)`, writerLockKey).Scan(&ok); err != nil {
		conn.Close()
		return nil, err
	}
	if !ok {
		conn.Close()
		return nil, nil
	}
	var epoch int64
	err = conn.QueryRowContext(ctx, `
		INSERT INTO writer (id, epoch, holder) VALUES (1, 1, $1)
		ON CONFLICT (id) DO UPDATE SET epoch = writer.epoch + 1, holder = $1, acquired_at = now()
		RETURNING epoch`, holder).Scan(&epoch)
	if err != nil {
		conn.Close()
		return nil, err
	}
	return &Writer{conn: conn, Epoch: epoch}, nil
}

// Check confirms this writer still holds the lock at its epoch.
func (w *Writer) Check(ctx context.Context) error {
	if w == nil || w.conn == nil {
		return ErrNotWriter
	}
	var epoch int64
	if err := w.conn.QueryRowContext(ctx, `SELECT epoch FROM writer WHERE id = 1`).Scan(&epoch); err != nil {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		return fmt.Errorf("%w: %v", ErrNotWriter, err)
	}
	if epoch != w.Epoch {
		return ErrNotWriter
	}
	return nil
}

// Release drops the lock by closing its session: driver.ErrBadConn makes
// database/sql discard the connection instead of pooling it.
func (w *Writer) Release() {
	if w != nil && w.conn != nil {
		_ = w.conn.Raw(func(any) error { return driver.ErrBadConn })
		w.conn.Close()
		w.conn = nil
	}
}

// Binding is a repository binding from configuration.
type Binding struct {
	Name                  string
	Owner                 string
	Repo                  string
	BaseBranch            string
	ItsaplanProject       string
	KagentNamespace       string
	KagentAgent           string
	BranchPrefix          string
	MaxEngineeringRetries int
}

// UpsertBinding records a binding from configuration.
func (s *Store) UpsertBinding(ctx context.Context, b Binding) error {
	_, err := s.DB.ExecContext(ctx, `
		INSERT INTO repo_binding (name, owner, repo, base_branch, itsaplan_project,
			kagent_namespace, kagent_agent, branch_prefix, max_engineering_retries)
		VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
		ON CONFLICT (name) DO UPDATE SET owner = $2, repo = $3, base_branch = $4,
			itsaplan_project = $5, kagent_namespace = $6, kagent_agent = $7,
			branch_prefix = $8, max_engineering_retries = $9, updated_at = now()`,
		b.Name, b.Owner, b.Repo, b.BaseBranch, b.ItsaplanProject, b.KagentNamespace,
		b.KagentAgent, b.BranchPrefix, b.MaxEngineeringRetries)
	return err
}

// Run states.
const (
	RunClaimed    = "claimed"    // claimed, nothing sent yet
	RunWaiting    = "waiting"    // waiting for a reassignment cancellation
	RunDispatched = "dispatched" // a delivery attempt is open
	RunFinishing  = "finishing"  // result queued in the outbox
	RunCanceling  = "canceling"  // itsaplan canceled it; kagent cancel pending
	RunStale      = "stale"      // a newer itsaplan attempt exists; stop writing
	RunClosed     = "closed"
)

// Run is the connector's record of one itsaplan run.
type Run struct {
	ID              int64
	AgentKey        string
	WorkItemID      int64 // 0 when unresolved
	IssueID         int64 // 0 when the run has no issue
	IssueKey        string
	Trigger         string
	Prompt          string
	SystemPrompt    string
	Attempt         int
	State           string
	EventAttempt    int
	EventOffset     int
	EventTaskID     string
	EventTaskBase   int
	LastHeartbeatAt time.Time
	SeenVisible     bool
	ClosedReason    string
	Origin          string // OriginItsaplan or OriginConnector
}

// Run origins. A connector run is a review round the connector started; it
// has no itsaplan record, so nothing about it goes to the runner API.
const (
	OriginItsaplan  = "itsaplan"
	OriginConnector = "connector"
)

// Connector reports whether the connector started the run itself.
func (r *Run) Connector() bool { return r.Origin == OriginConnector }

const runCols = `id, agent_key, coalesce(work_item_id, 0), coalesce(issue_id, 0), coalesce(issue_key, ''),
	trigger, prompt, system_prompt, attempt, state, coalesce(event_attempt, 0), event_offset,
	coalesce(event_task_id, ''), event_task_base, coalesce(last_heartbeat_at, 'epoch'::timestamptz), seen_visible, coalesce(closed_reason, ''),
	origin`

func scanRun(row interface{ Scan(...any) error }) (*Run, error) {
	r := &Run{}
	err := row.Scan(&r.ID, &r.AgentKey, &r.WorkItemID, &r.IssueID, &r.IssueKey, &r.Trigger,
		&r.Prompt, &r.SystemPrompt, &r.Attempt, &r.State, &r.EventAttempt, &r.EventOffset,
		&r.EventTaskID, &r.EventTaskBase, &r.LastHeartbeatAt, &r.SeenVisible, &r.ClosedReason, &r.Origin)
	return r, err
}

// ClaimOutcome says how a claim related to what the connector knew.
type ClaimOutcome string

const (
	ClaimNew        ClaimOutcome = "new"        // first claim of this run
	ClaimRedelivery ClaimOutcome = "redelivery" // a higher attempt of a known run
	ClaimDuplicate  ClaimOutcome = "duplicate"  // an attempt already held
)

// ClaimedRun is the claim response the store records.
type ClaimedRun struct {
	ID           int64
	AgentKey     string
	IssueID      int64
	IssueKey     string
	Trigger      string
	Prompt       string
	SystemPrompt string
	Attempt      int
}

// RecordClaim upserts the run. A higher attempt of a known run is a
// transport redelivery: the attempt moves forward, a stale run resumes and
// the event offset restarts for the new attempt's transcript.
func (s *Store) RecordClaim(ctx context.Context, c ClaimedRun) (*Run, ClaimOutcome, error) {
	var run *Run
	var outcome ClaimOutcome
	err := s.Tx(ctx, func(tx *sql.Tx) error {
		existing, err := scanRun(tx.QueryRowContext(ctx, `SELECT `+runCols+` FROM run WHERE id = $1 FOR UPDATE`, c.ID))
		if errors.Is(err, sql.ErrNoRows) {
			run, err = scanRun(tx.QueryRowContext(ctx, `
				INSERT INTO run (id, agent_key, issue_id, issue_key, trigger, prompt, system_prompt, attempt, state)
				VALUES ($1, $2, nullif($3, 0), nullif($4, ''), $5, $6, $7, $8, 'claimed')
				RETURNING `+runCols,
				c.ID, c.AgentKey, c.IssueID, c.IssueKey, c.Trigger, c.Prompt, c.SystemPrompt, c.Attempt))
			outcome = ClaimNew
			return err
		}
		if err != nil {
			return err
		}
		if c.Attempt <= existing.Attempt {
			run, outcome = existing, ClaimDuplicate
			return nil
		}
		state := existing.State
		if state == RunStale {
			state = RunDispatched
		}
		run, err = scanRun(tx.QueryRowContext(ctx, `
			UPDATE run SET attempt = $2, state = $3, event_attempt = NULL, event_offset = 0,
				event_task_id = NULL, event_task_base = 0, last_heartbeat_at = NULL, updated_at = now()
			WHERE id = $1 RETURNING `+runCols, c.ID, c.Attempt, state))
		outcome = ClaimRedelivery
		return err
	})
	return run, outcome, err
}

// GetRun reads one run.
func (s *Store) GetRun(ctx context.Context, id int64) (*Run, error) {
	return scanRun(s.DB.QueryRowContext(ctx, `SELECT `+runCols+` FROM run WHERE id = $1`, id))
}

// OpenRuns lists runs that are not closed, oldest first.
func (s *Store) OpenRuns(ctx context.Context) ([]*Run, error) {
	rows, err := s.DB.QueryContext(ctx, `SELECT `+runCols+` FROM run WHERE state <> 'closed' ORDER BY id`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []*Run
	for rows.Next() {
		r, err := scanRun(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, r)
	}
	return out, rows.Err()
}

// CountActiveRuns counts open runs of an agent key, for its concurrency.
// A waiting run held by a block keeps no concurrency slot, so further runs
// are still claimed while an operator fixes the grant.
func (s *Store) CountActiveRuns(ctx context.Context, agentKey string) (int, error) {
	var n int
	err := s.DB.QueryRowContext(ctx, `
		SELECT count(*) FROM run r WHERE r.agent_key = $1 AND r.state NOT IN ('closed', 'stale')
			AND NOT (r.state = 'waiting' AND EXISTS (
				SELECT 1 FROM work_item w
				LEFT JOIN grant_block g ON g.scope = 'kagent:' || w.kagent_namespace || '/' || w.kagent_agent
					AND g.cleared_at IS NULL
				WHERE w.id = r.work_item_id AND (w.state = 'blocked' OR g.scope IS NOT NULL)))`, agentKey).Scan(&n)
	return n, err
}

// CountBusyRuns counts an agent key's runs that hold a slot beyond waiting.
func (s *Store) CountBusyRuns(ctx context.Context, agentKey string) (int, error) {
	var n int
	err := s.DB.QueryRowContext(ctx,
		`SELECT count(*) FROM run WHERE agent_key = $1 AND state NOT IN ('closed', 'stale', 'waiting')`, agentKey).Scan(&n)
	return n, err
}

// SetRunState moves a run to state, recording a reason when it closes.
func (s *Store) SetRunState(ctx context.Context, q Querier, id int64, state, reason string) error {
	_, err := q.ExecContext(ctx, `UPDATE run SET state = $2, closed_reason = nullif($3, ''), updated_at = now() WHERE id = $1`,
		id, state, reason)
	return err
}

// MarkStale stops a run's writing when itsaplan reports a newer attempt than
// the one held. A redelivery claim at that attempt resumes it.
func (s *Store) MarkStale(ctx context.Context, id int64, attempt int) error {
	_, err := s.DB.ExecContext(ctx,
		`UPDATE run SET state = 'stale', updated_at = now() WHERE id = $1 AND attempt = $2 AND state <> 'closed'`, id, attempt)
	return err
}

// MarkVisible records that the coordinator's queue lookup showed the run.
func (s *Store) MarkVisible(ctx context.Context, id int64) error {
	_, err := s.DB.ExecContext(ctx, `UPDATE run SET seen_visible = true WHERE id = $1`, id)
	return err
}

// SetHeartbeat records a successful heartbeat.
func (s *Store) SetHeartbeat(ctx context.Context, id int64, at time.Time) error {
	_, err := s.DB.ExecContext(ctx, `UPDATE run SET last_heartbeat_at = $2 WHERE id = $1`, id, at)
	return err
}

// SetEventOffset persists the acknowledged transcript offset of an attempt
// and the task whose events it has reached, which began at base.
func (s *Store) SetEventOffset(ctx context.Context, id int64, attempt, offset int, taskID string, base int) error {
	_, err := s.DB.ExecContext(ctx, `
		UPDATE run SET event_attempt = $2, event_offset = $3, event_task_id = $4, event_task_base = $5
		WHERE id = $1 AND attempt = $2`, id, attempt, offset, taskID, base)
	return err
}

// Work item states.
const (
	WorkActive  = "active"
	WorkBlocked = "blocked"
	WorkReview  = "review"  // a turn finished; the PR is reviewed
	WorkMerging = "merging" // the PR is in the merge queue
	WorkDone    = "done"    // the merge was observed
	WorkStopped = "stopped" // delivery stopped; a new itsaplan run resumes it
)

// WorkItem is one issue delivered through one kagent session.
type WorkItem struct {
	ID                 int64
	BindingName        string
	IssueID            int64
	IssueKey           string
	Branch             string
	AgentKey           string
	KagentNamespace    string
	KagentAgent        string
	SessionID          string
	SessionGeneration  int
	Epoch              int64
	EngineeringRetries int
	State              string
	BlockedClass       string
	BlockedReason      string
	ReviewRounds       int
	// TurnAttemptID is the completed turn the PR must reflect, and
	// SyncedAttemptID the last one it does.
	TurnAttemptID   int64
	SyncedAttemptID int64
	StoppedReason   string
}

const workCols = `id, binding_name, issue_id, issue_key, branch, agent_key, kagent_namespace, kagent_agent,
	coalesce(session_id, ''), session_generation, epoch, engineering_retries, state,
	coalesce(blocked_class, ''), coalesce(blocked_reason, ''), review_rounds,
	coalesce(turn_attempt_id, 0), coalesce(synced_attempt_id, 0), coalesce(stopped_reason, '')`

func scanWork(row interface{ Scan(...any) error }) (*WorkItem, error) {
	w := &WorkItem{}
	err := row.Scan(&w.ID, &w.BindingName, &w.IssueID, &w.IssueKey, &w.Branch, &w.AgentKey,
		&w.KagentNamespace, &w.KagentAgent, &w.SessionID, &w.SessionGeneration, &w.Epoch,
		&w.EngineeringRetries, &w.State, &w.BlockedClass, &w.BlockedReason, &w.ReviewRounds,
		&w.TurnAttemptID, &w.SyncedAttemptID, &w.StoppedReason)
	return w, err
}

// SessionMapped reports whether a work item records the kagent session.
func (s *Store) SessionMapped(ctx context.Context, sessionID string) (bool, error) {
	var ok bool
	err := s.DB.QueryRowContext(ctx, `SELECT EXISTS (SELECT 1 FROM work_item WHERE session_id = $1)`, sessionID).Scan(&ok)
	return ok, err
}

// GetWorkItem reads one work item.
func (s *Store) GetWorkItem(ctx context.Context, id int64) (*WorkItem, error) {
	return scanWork(s.DB.QueryRowContext(ctx, `SELECT `+workCols+` FROM work_item WHERE id = $1`, id))
}

// WorkItemByIssue reads the work item of an issue.
func (s *Store) WorkItemByIssue(ctx context.Context, issueID int64) (*WorkItem, error) {
	return scanWork(s.DB.QueryRowContext(ctx, `SELECT `+workCols+` FROM work_item WHERE issue_id = $1`, issueID))
}

// NewWorkItem describes the work item a run creates.
type NewWorkItem struct {
	Binding  Binding
	IssueID  int64
	IssueKey string
	AgentKey string
}

// ResolveWorkItem finds or creates the run's work item and links the run to
// it. When the work item was last delivered by another agent key, its epoch
// is bumped, reassigned is true, and in the same transaction any open
// attempt of another run gets a pending cancellation; the session stays. A
// new run makes the work item active again, except a blocked one: only an
// operator's unblock clears a block.
func (s *Store) ResolveWorkItem(ctx context.Context, runID int64, n NewWorkItem) (w *WorkItem, reassigned bool, err error) {
	err = s.Tx(ctx, func(tx *sql.Tx) error {
		w, err = scanWork(tx.QueryRowContext(ctx, `SELECT `+workCols+` FROM work_item WHERE issue_id = $1 FOR UPDATE`, n.IssueID))
		switch {
		case errors.Is(err, sql.ErrNoRows):
			w, err = scanWork(tx.QueryRowContext(ctx, `
				INSERT INTO work_item (binding_name, issue_id, issue_key, branch, agent_key, kagent_namespace, kagent_agent)
				VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING `+workCols,
				n.Binding.Name, n.IssueID, n.IssueKey, n.Binding.BranchPrefix+n.IssueKey, n.AgentKey,
				n.Binding.KagentNamespace, n.Binding.KagentAgent))
			if err != nil {
				return err
			}
		case err != nil:
			return err
		default:
			reassigned = w.AgentKey != n.AgentKey
			w, err = scanWork(tx.QueryRowContext(ctx, `
				UPDATE work_item SET agent_key = $2,
					epoch = epoch + CASE WHEN $3 THEN 1 ELSE 0 END,
					state = CASE WHEN state IN ('blocked', 'done') THEN state ELSE 'active' END,
					updated_at = now()
				WHERE id = $1 RETURNING `+workCols, w.ID, n.AgentKey, reassigned))
			if err != nil {
				return err
			}
			if reassigned {
				if _, err := tx.ExecContext(ctx, `
					INSERT INTO cancellation (delivery_attempt_id, run_id, reason, state)
					SELECT a.id, a.run_id, 'reassignment', 'pending' FROM delivery_attempt a
					JOIN run r ON r.id = a.run_id
					WHERE r.work_item_id = $1 AND a.run_id <> $2
						AND a.state IN ('sending', 'ambiguous', 'accepted')
					ON CONFLICT (delivery_attempt_id) WHERE state = 'pending' DO NOTHING`, w.ID, runID); err != nil {
					return err
				}
			}
		}
		_, err = tx.ExecContext(ctx, `UPDATE run SET work_item_id = $2, updated_at = now() WHERE id = $1`, runID, w.ID)
		return err
	})
	return w, reassigned, err
}

// SetSession records the work item's kagent session.
func (s *Store) SetSession(ctx context.Context, q Querier, workID int64, sessionID string) error {
	_, err := q.ExecContext(ctx, `
		UPDATE work_item SET session_id = $2,
			session_generation = session_generation + CASE WHEN session_id IS DISTINCT FROM $2 THEN 1 ELSE 0 END,
			updated_at = now()
		WHERE id = $1`, workID, sessionID)
	return err
}

// GrantScope names the grant a kagent Agent's runs use.
func GrantScope(namespace, agent string) string { return "kagent:" + namespace + "/" + agent }

// Block marks a work item blocked by a credential or quota failure and
// pauses its grant's other work items. started is false when the grant was
// already blocked, so the block is announced once.
func (s *Store) Block(ctx context.Context, q Querier, w *WorkItem, class, reason string) (started bool, err error) {
	if _, err := q.ExecContext(ctx, `
		UPDATE work_item SET state = 'blocked', blocked_class = $2, blocked_reason = $3, updated_at = now()
		WHERE id = $1`, w.ID, class, reason); err != nil {
		return false, err
	}
	res, err := q.ExecContext(ctx, `
		INSERT INTO grant_block (scope, class, reason) VALUES ($1, $2, $3)
		ON CONFLICT (scope) DO UPDATE SET class = $2, reason = $3, created_at = now(), cleared_at = NULL
		WHERE grant_block.cleared_at IS NOT NULL`,
		GrantScope(w.KagentNamespace, w.KagentAgent), class, reason)
	if err != nil {
		return false, err
	}
	n, err := res.RowsAffected()
	return n > 0, err
}

// GrantBlocked reports whether a grant is paused, and why.
func (s *Store) GrantBlocked(ctx context.Context, scope string) (bool, string, error) {
	var reason string
	err := s.DB.QueryRowContext(ctx, `SELECT reason FROM grant_block WHERE scope = $1 AND cleared_at IS NULL`, scope).Scan(&reason)
	if errors.Is(err, sql.ErrNoRows) {
		return false, "", nil
	}
	return err == nil, reason, err
}

// ClearGrantBlock is the operator's unblock. It clears the grant and the
// work items it blocked.
func (s *Store) ClearGrantBlock(ctx context.Context, scope string) (bool, error) {
	var n int64
	err := s.Tx(ctx, func(tx *sql.Tx) error {
		res, err := tx.ExecContext(ctx, `UPDATE grant_block SET cleared_at = now() WHERE scope = $1 AND cleared_at IS NULL`, scope)
		if err != nil {
			return err
		}
		if n, err = res.RowsAffected(); err != nil || n == 0 {
			return err
		}
		_, err = tx.ExecContext(ctx, `
			UPDATE work_item SET state = 'active', blocked_class = NULL, blocked_reason = NULL, updated_at = now()
			WHERE state = 'blocked' AND 'kagent:' || kagent_namespace || '/' || kagent_agent = $1`, scope)
		return err
	})
	return n > 0, err
}

// SetWorkState moves a work item to state.
func (s *Store) SetWorkState(ctx context.Context, q Querier, workID int64, state string) error {
	_, err := q.ExecContext(ctx, `UPDATE work_item SET state = $2, updated_at = now() WHERE id = $1`, workID, state)
	return err
}

// TakeEngineeringRetry consumes one retry from the work item's budget,
// reporting false when the budget is spent.
func (s *Store) TakeEngineeringRetry(ctx context.Context, q Querier, workID int64, max int) (int, bool, error) {
	var n int
	err := q.QueryRowContext(ctx, `
		UPDATE work_item SET engineering_retries = engineering_retries + 1, updated_at = now()
		WHERE id = $1 AND engineering_retries < $2 RETURNING engineering_retries`, workID, max).Scan(&n)
	if errors.Is(err, sql.ErrNoRows) {
		return 0, false, nil
	}
	return n, err == nil, err
}

// Delivery attempt states.
const (
	AttemptSending   = "sending"   // recorded before the send
	AttemptAmbiguous = "ambiguous" // the send's outcome is unknown
	AttemptAccepted  = "accepted"  // kagent holds the message in a task
	AttemptCompleted = "completed"
	AttemptFailed    = "failed"
	AttemptCanceled  = "canceled"
)

// Attempt is one send to kagent.
type Attempt struct {
	ID              int64
	RunID           int64
	Seq             int
	RunAttempt      int
	WorkItemEpoch   int64
	DecisionID      string
	Kind            string
	MessageID       string
	KagentNamespace string
	KagentAgent     string
	ContextID       string
	TaskID          string
	State           string
	ErrorClass      string
	Error           string
	WriterEpoch     int64
	Issues          int
	FirstSentAt     time.Time
	SentAt          time.Time
	// Context is what a replacement session starts from, sent with the input.
	Context string
}

// Open reports whether the attempt has not reached a final state.
func (a *Attempt) Open() bool {
	return a.State == AttemptSending || a.State == AttemptAmbiguous || a.State == AttemptAccepted
}

const attemptCols = `id, run_id, seq, run_attempt, work_item_epoch, decision_id, kind, message_id,
	kagent_namespace, kagent_agent, coalesce(context_id, ''), coalesce(task_id, ''), state,
	coalesce(error_class, ''), coalesce(error, ''), writer_epoch, issues,
	coalesce(first_sent_at, 'epoch'::timestamptz), coalesce(sent_at, 'epoch'::timestamptz), coalesce(context, '')`

func scanAttempt(row interface{ Scan(...any) error }) (*Attempt, error) {
	a := &Attempt{}
	err := row.Scan(&a.ID, &a.RunID, &a.Seq, &a.RunAttempt, &a.WorkItemEpoch, &a.DecisionID, &a.Kind,
		&a.MessageID, &a.KagentNamespace, &a.KagentAgent, &a.ContextID, &a.TaskID, &a.State,
		&a.ErrorClass, &a.Error, &a.WriterEpoch, &a.Issues, &a.FirstSentAt, &a.SentAt, &a.Context)
	return a, err
}

// LatestAttempt returns the run's most recent attempt, or nil.
func (s *Store) LatestAttempt(ctx context.Context, runID int64) (*Attempt, error) {
	a, err := scanAttempt(s.DB.QueryRowContext(ctx,
		`SELECT `+attemptCols+` FROM delivery_attempt WHERE run_id = $1 ORDER BY seq DESC LIMIT 1`, runID))
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	return a, err
}

// GetAttempt reads one attempt.
func (s *Store) GetAttempt(ctx context.Context, id int64) (*Attempt, error) {
	return scanAttempt(s.DB.QueryRowContext(ctx, `SELECT `+attemptCols+` FROM delivery_attempt WHERE id = $1`, id))
}

// OpenAttemptForWork returns the open attempt of any run on a work item, or nil.
func (s *Store) OpenAttemptForWork(ctx context.Context, workID int64) (*Attempt, error) {
	a, err := scanAttempt(s.DB.QueryRowContext(ctx, `
		SELECT `+attemptCols+` FROM delivery_attempt
		WHERE run_id IN (SELECT id FROM run WHERE work_item_id = $1)
			AND state IN ('sending', 'ambiguous', 'accepted')
		ORDER BY id DESC LIMIT 1`, workID))
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	return a, err
}

// RecordAttempt inserts an attempt in state sending before anything is sent.
// Recording a message ID that exists returns the existing attempt.
func (s *Store) RecordAttempt(ctx context.Context, q Querier, a Attempt) (*Attempt, error) {
	return scanAttempt(q.QueryRowContext(ctx, `
		INSERT INTO delivery_attempt (run_id, seq, run_attempt, work_item_epoch, decision_id, kind, message_id,
			kagent_namespace, kagent_agent, context_id, state, writer_epoch, context)
		VALUES ($1, coalesce((SELECT max(seq) FROM delivery_attempt WHERE run_id = $1), 0) + 1,
			$2, $3, $4, $5, $6, $7, $8, nullif($9, ''), 'sending', $10, nullif($11, ''))
		ON CONFLICT (message_id) DO UPDATE SET message_id = EXCLUDED.message_id
		RETURNING `+attemptCols,
		a.RunID, a.RunAttempt, a.WorkItemEpoch, a.DecisionID, a.Kind, a.MessageID,
		a.KagentNamespace, a.KagentAgent, a.ContextID, a.WriterEpoch, a.Context))
}

// MarkIssued records, before the call, that the message is being sent. From
// then on only kagent's answer to the same message decides whether it holds it.
func (s *Store) MarkIssued(ctx context.Context, a *Attempt, at time.Time) error {
	_, err := s.DB.ExecContext(ctx, `
		UPDATE delivery_attempt SET state = 'ambiguous', sent_at = $2, first_sent_at = coalesce(first_sent_at, $2),
			issues = issues + 1
		WHERE id = $1 AND state IN ('sending', 'ambiguous')`, a.ID, at)
	if err == nil {
		a.State, a.SentAt, a.Issues = AttemptAmbiguous, at, a.Issues+1
		if a.FirstSentAt.Unix() <= 0 {
			a.FirstSentAt = at
		}
	}
	return err
}

// MarkAccepted attaches the attempt to the kagent task holding its message.
func (s *Store) MarkAccepted(ctx context.Context, q Querier, id int64, contextID, taskID string) error {
	_, err := q.ExecContext(ctx, `
		UPDATE delivery_attempt SET state = 'accepted', context_id = $2, task_id = $3
		WHERE id = $1 AND state IN ('sending', 'ambiguous', 'accepted')`, id, contextID, taskID)
	return err
}

// FinishAttempt records an attempt's final state.
func (s *Store) FinishAttempt(ctx context.Context, q Querier, id int64, state, class, msg string) error {
	_, err := q.ExecContext(ctx, `
		UPDATE delivery_attempt SET state = $2, error_class = nullif($3, ''), error = nullif($4, ''),
			finished_at = now()
		WHERE id = $1`, id, state, class, msg)
	return err
}

// CountSends counts attempts of a run that were sent.
func (s *Store) CountSends(ctx context.Context, runID int64) (int, error) {
	var n int
	err := s.DB.QueryRowContext(ctx, `SELECT count(*) FROM delivery_attempt WHERE run_id = $1`, runID).Scan(&n)
	return n, err
}

// Cancellation reasons and states.
const (
	CancelRunCanceled  = "run_canceled"
	CancelRunClosed    = "run_closed" // itsaplan finished the run without this attempt's result
	CancelReassignment = "reassignment"
	CancelRoundStopped = "round_stopped" // the round's work item stopped delivery
	CancelPending      = "pending"
	CancelConfirmed    = "confirmed"
)

// Cancellation is a requested kagent task cancellation.
type Cancellation struct {
	ID        int64
	AttemptID int64
	RunID     int64
	Reason    string
	State     string
	Requests  int
	LastAt    time.Time
	Outcome   string
}

// RequestCancellation records a pending cancellation for an attempt; a
// pending one already recorded is kept.
func (s *Store) RequestCancellation(ctx context.Context, q Querier, attemptID, runID int64, reason string) error {
	_, err := q.ExecContext(ctx, `
		INSERT INTO cancellation (delivery_attempt_id, run_id, reason, state)
		VALUES ($1, $2, $3, 'pending')
		ON CONFLICT (delivery_attempt_id) WHERE state = 'pending' DO NOTHING`, attemptID, runID, reason)
	return err
}

// PendingCancellations lists cancellations kagent has not confirmed.
func (s *Store) PendingCancellations(ctx context.Context) ([]*Cancellation, error) {
	rows, err := s.DB.QueryContext(ctx, `
		SELECT id, delivery_attempt_id, run_id, reason, state, requests,
			coalesce(last_request_at, 'epoch'::timestamptz), coalesce(outcome, '')
		FROM cancellation WHERE state = 'pending' ORDER BY id`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []*Cancellation
	for rows.Next() {
		c := &Cancellation{}
		if err := rows.Scan(&c.ID, &c.AttemptID, &c.RunID, &c.Reason, &c.State, &c.Requests, &c.LastAt, &c.Outcome); err != nil {
			return nil, err
		}
		out = append(out, c)
	}
	return out, rows.Err()
}

// HasPendingCancellation reports whether any attempt of the work item has a
// pending cancellation.
func (s *Store) HasPendingCancellation(ctx context.Context, workID int64) (bool, error) {
	var ok bool
	err := s.DB.QueryRowContext(ctx, `
		SELECT EXISTS (SELECT 1 FROM cancellation c JOIN run r ON r.id = c.run_id
			WHERE r.work_item_id = $1 AND c.state = 'pending')`, workID).Scan(&ok)
	return ok, err
}

// CountCancelRequest records that CancelTask was sent.
func (s *Store) CountCancelRequest(ctx context.Context, id int64, at time.Time) error {
	_, err := s.DB.ExecContext(ctx, `UPDATE cancellation SET requests = requests + 1, last_request_at = $2 WHERE id = $1`, id, at)
	return err
}

// ConfirmCancellation records kagent's terminal state for the task.
func (s *Store) ConfirmCancellation(ctx context.Context, q Querier, id int64, outcome string) error {
	_, err := q.ExecContext(ctx, `
		UPDATE cancellation SET state = 'confirmed', outcome = $2, confirmed_at = now()
		WHERE id = $1 AND state = 'pending'`, id, outcome)
	return err
}

// Outbox kinds.
const (
	OutboxResult    = "result"
	OutboxComment   = "comment"
	OutboxIssueDone = "issue_done"
)

// OutboxItem is a side effect waiting for delivery.
type OutboxItem struct {
	ID        int64
	Kind      string
	DedupeKey string
	RunID     int64
	AgentKey  string
	Payload   json.RawMessage
	Tries     int
}

// Enqueue records a side effect; a duplicate dedupe key is ignored.
func (s *Store) Enqueue(ctx context.Context, q Querier, kind, dedupe string, runID int64, agentKey string, payload any) error {
	b, err := json.Marshal(payload)
	if err != nil {
		return err
	}
	_, err = q.ExecContext(ctx, `
		INSERT INTO outbox (kind, dedupe_key, run_id, agent_key, payload)
		VALUES ($1, $2, nullif($3, 0), nullif($4, ''), $5)
		ON CONFLICT (dedupe_key) DO NOTHING`, kind, dedupe, runID, agentKey, b)
	return err
}

// PendingOutbox lists undelivered side effects that are due, in order. A
// failing item backs off, so it does not hold newer ones back.
func (s *Store) PendingOutbox(ctx context.Context, now time.Time, limit int) ([]*OutboxItem, error) {
	rows, err := s.DB.QueryContext(ctx, `
		SELECT id, kind, dedupe_key, coalesce(run_id, 0), coalesce(agent_key, ''), payload, tries
		FROM outbox WHERE delivered_at IS NULL AND abandoned_at IS NULL AND next_attempt_at <= $1
		ORDER BY id LIMIT $2`, now, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []*OutboxItem
	for rows.Next() {
		o := &OutboxItem{}
		if err := rows.Scan(&o.ID, &o.Kind, &o.DedupeKey, &o.RunID, &o.AgentKey, &o.Payload, &o.Tries); err != nil {
			return nil, err
		}
		out = append(out, o)
	}
	return out, rows.Err()
}

// OutboxDelivered marks an item delivered.
func (s *Store) OutboxDelivered(ctx context.Context, q Querier, id int64) error {
	_, err := q.ExecContext(ctx, `UPDATE outbox SET delivered_at = now(), tries = tries + 1 WHERE id = $1`, id)
	return err
}

// OutboxFailed records a failed delivery and when to try again.
func (s *Store) OutboxFailed(ctx context.Context, id int64, msg string, next time.Time) error {
	_, err := s.DB.ExecContext(ctx, `UPDATE outbox SET tries = tries + 1, last_error = $2, next_attempt_at = $3 WHERE id = $1`,
		id, msg, next)
	return err
}

// OutboxAbandon gives up on an item whose target no longer accepts it.
func (s *Store) OutboxAbandon(ctx context.Context, q Querier, id int64, msg string) error {
	_, err := q.ExecContext(ctx, `UPDATE outbox SET abandoned_at = now(), last_error = $2 WHERE id = $1`, id, msg)
	return err
}

// Querier is satisfied by *sql.DB and *sql.Tx.
type Querier interface {
	ExecContext(ctx context.Context, query string, args ...any) (sql.Result, error)
	QueryRowContext(ctx context.Context, query string, args ...any) *sql.Row
}

// Tx runs fn in a transaction.
func (s *Store) Tx(ctx context.Context, fn func(*sql.Tx) error) error {
	tx, err := s.DB.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	if err := fn(tx); err != nil {
		_ = tx.Rollback()
		return err
	}
	return tx.Commit()
}
