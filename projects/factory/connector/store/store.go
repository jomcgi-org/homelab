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
	"strings"
	"sync"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/internal/tracing"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"

	"github.com/jomcgi/homelab/projects/factory/connector/itsaplan"
	"github.com/jomcgi/homelab/projects/factory/connector/provenance"
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
	// Now stamps the one column the dispatch loop compares with its own
	// clock, an outbox item's first due time. Nil is time.Now. A database
	// default would mix the database's clock with the engine's, so a test
	// clock behind real time never saw its items come due.
	Now func() time.Time
}

func (s *Store) now() time.Time {
	if s.Now != nil {
		return s.Now()
	}
	return time.Now()
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
	mu    sync.Mutex
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
	if w == nil {
		return ErrNotWriter
	}
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.conn == nil {
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

// Tx commits authority-sensitive changes on the advisory-lock connection. The
// writer row lock prevents an epoch change until this transaction has finished.
// The mutex serializes use of that one PostgreSQL session. Callbacks must not
// call Writer.Check, Writer.Tx, or Writer.Release while holding it.
func (w *Writer) Tx(ctx context.Context, fn func(*sql.Tx) error) error {
	if w == nil {
		return ErrNotWriter
	}
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.conn == nil {
		return ErrNotWriter
	}
	lost := func(err error) error {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		return fmt.Errorf("%w: %v", ErrNotWriter, err)
	}
	tx, err := w.conn.BeginTx(ctx, nil)
	if err != nil {
		return lost(err)
	}
	defer tx.Rollback() //nolint:errcheck // no-op after commit
	var epoch int64
	if err := tx.QueryRowContext(ctx, `SELECT epoch FROM writer WHERE id = 1 FOR UPDATE`).Scan(&epoch); err != nil {
		return lost(err)
	}
	if epoch != w.Epoch {
		return ErrNotWriter
	}
	commitHooks.Store(tx, new([]func()))
	defer commitHooks.Delete(tx)
	if err := fn(tx); err != nil {
		var state interface{ SQLState() string }
		if errors.Is(err, driver.ErrBadConn) || errors.Is(err, sql.ErrConnDone) || errors.Is(err, sql.ErrTxDone) ||
			(errors.As(err, &state) && (state.SQLState() == "57P01" || len(state.SQLState()) >= 2 && state.SQLState()[:2] == "08")) {
			return lost(err)
		}
		return err
	}
	if err := commitWithHooks(tx); err != nil {
		return lost(err)
	}
	return nil
}

// Release drops the lock by closing its session: driver.ErrBadConn makes
// database/sql discard the connection instead of pooling it.
func (w *Writer) Release() {
	if w == nil {
		return
	}
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.conn != nil {
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

// Run records coder execution or an externally managed reviewer receipt.
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
	Origin          string // OriginItsaplan, OriginConnector or OriginReviewer
	StopRequested   bool   // stop or supersession survives a new admission
	StoppedReason   string
	HoldReason      string
	AdmissionStops  int             // stop counter observed on the first claim, never redelivery
	AdmissionIssue  *itsaplan.Issue // poll-local issue read; its brief persists only with a delivery attempt

	// Historical runs remain unknown; new runs snapshot their first dispatch.
	Provenance         provenance.Record
	ProvenanceRequired bool
}

// Run origins. A connector run is a review round the connector started; it
// has no itsaplan record, so nothing about it goes to the runner API.
const (
	OriginItsaplan  = "itsaplan"
	OriginConnector = "connector"
	OriginReviewer  = "reviewer"
)

// Connector reports whether the connector started the run itself.
func (r *Run) Connector() bool { return r.Origin == OriginConnector }

const runCols = `id, agent_key, coalesce(work_item_id, 0), coalesce(issue_id, 0), coalesce(issue_key, ''),
	trigger, prompt, system_prompt, attempt, state, coalesce(event_attempt, 0), event_offset,
	coalesce(event_task_id, ''), event_task_base, coalesce(last_heartbeat_at, 'epoch'::timestamptz), seen_visible, coalesce(closed_reason, ''),
	origin, stopped_reason IS NOT NULL, coalesce(stopped_reason, ''), hold_reason, admission_stops, coalesce(model, ''), coalesce(reasoning_effort, ''),
	coalesce(arm, ''), coalesce(base_sha, ''), coalesce(prompt_hash, ''), coalesce(task_class, ''), provenance_required`

func scanRun(row interface{ Scan(...any) error }) (*Run, error) {
	r := &Run{}
	err := row.Scan(&r.ID, &r.AgentKey, &r.WorkItemID, &r.IssueID, &r.IssueKey, &r.Trigger,
		&r.Prompt, &r.SystemPrompt, &r.Attempt, &r.State, &r.EventAttempt, &r.EventOffset,
		&r.EventTaskID, &r.EventTaskBase, &r.LastHeartbeatAt, &r.SeenVisible, &r.ClosedReason, &r.Origin,
		&r.StopRequested, &r.StoppedReason, &r.HoldReason, &r.AdmissionStops,
		&r.Provenance.Model, &r.Provenance.ReasoningEffort, &r.Provenance.Arm, &r.Provenance.BaseSHA, &r.Provenance.PromptHash, &r.Provenance.TaskClass, &r.ProvenanceRequired)
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
				INSERT INTO run (id, agent_key, issue_id, issue_key, trigger, prompt, system_prompt, attempt, state, admission_stops)
				VALUES ($1, $2, nullif($3, 0), nullif($4, ''), $5, $6, $7, $8, 'claimed',
					coalesce((SELECT stops FROM work_item WHERE issue_id = $3), 0))
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
			AND r.hold_reason = ''
			AND NOT (r.state = 'waiting' AND EXISTS (
				SELECT 1 FROM work_item w
				LEFT JOIN grant_block g ON g.scope = 'kagent:' || w.kagent_namespace || '/' || w.kagent_agent
					AND g.cleared_at IS NULL
				WHERE w.id = r.work_item_id AND (w.state = 'blocked' OR g.scope IS NOT NULL)))`, agentKey).Scan(&n)
	return n, err
}

// CountBusyRuns counts an agent key's runs that hold a slot beyond waiting.
// It never counts run except, so a run that asks for a slot is not its own
// occupant.
func (s *Store) CountBusyRuns(ctx context.Context, agentKey string, except int64) (int, error) {
	var n int
	err := s.DB.QueryRowContext(ctx,
		`SELECT count(*) FROM run WHERE agent_key = $1 AND id <> $2 AND state NOT IN ('closed', 'stale', 'waiting')`,
		agentKey, except).Scan(&n)
	return n, err
}

// SetRunState moves a run to state, recording a reason when it closes.
func (s *Store) SetRunState(ctx context.Context, q Querier, id int64, state, reason string) error {
	if state == RunClosed {
		// Cancellation can close a run without sending an itsaplan result.
		if _, err := s.RunUsage(ctx, q, id); err != nil {
			return err
		}
		var issue, previous, head, origin string
		var number, round int
		var created time.Time
		err := q.QueryRowContext(ctx, `WITH old AS (SELECT id,state FROM run WHERE id=$1 AND state <> 'closed' FOR UPDATE)
 UPDATE run r SET state='closed',closed_reason=nullif($2,''),updated_at=now() FROM old WHERE r.id=old.id
 RETURNING coalesce(r.issue_key,''),old.state,r.created_at,
 coalesce((select number from pull_request where work_item_id=r.work_item_id),0),
 coalesce(r.round_head_sha,(select head_sha from pull_request where work_item_id=r.work_item_id),''),
 coalesce((select case r.trigger when 'split' then split_rounds when 'rebase' then rebase_rounds else review_rounds end from work_item where id=r.work_item_id),0),r.origin`, id, reason).Scan(&issue, &previous, &created, &number, &head, &round, &origin)
		if errors.Is(err, sql.ErrNoRows) {
			return nil
		}
		if err != nil {
			return err
		}
		emit := func() {
			emitUnit := func(name string) {
				attrs := []attribute.KeyValue{
					attribute.String("issue.key", issue), attribute.Int64("run.id", id),
					attribute.String("state.from", previous), attribute.String("state.to", RunClosed),
					attribute.Int("pr.number", number), attribute.String("head.sha", head), attribute.Int("round", round),
				}
				tracing.Observe(ctx, name, attrs...)
				_, span := tracing.Start(ctx, name, trace.WithTimestamp(created), trace.WithAttributes(attrs...))
				// The close reason may include arbitrary task output. Export only
				// the connector's known terminal classifications.
				switch {
				case reason == "reported success" || reason == "review round success":
					tracing.Finish(span, nil, "success")
				case strings.HasPrefix(reason, "review round failed") || reason == "reported failed" || strings.HasPrefix(reason, "result "):
					tracing.Finish(span, errors.New("run failed"), "error")
				case strings.HasPrefix(reason, "canceled"):
					tracing.Finish(span, nil, "canceled")
				default:
					tracing.Finish(span, nil, RunClosed)
				}
			}
			emitUnit("factory.run")
			if origin == OriginConnector {
				emitUnit("factory.review_round")
			}
		}
		if tx, ok := q.(*sql.Tx); ok {
			if hooks, registered := commitHooks.Load(tx); registered {
				callbacks := hooks.(*[]func())
				*callbacks = append(*callbacks, emit)
			}
		} else {
			emit()
		}
		return nil
	}
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
	FirstArm           string
	Escalated          bool
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
	RebaseRounds       int
	SplitRounds        int
	// TurnAttemptID is the completed turn the PR must reflect, and
	// SyncedAttemptID the last one it does.
	TurnAttemptID   int64
	SyncedAttemptID int64
	StoppedReason   string
	// CallerIdentity is the immutable non-secret kagent caller witness.
	// Empty means legacy work whose ownership must be established positively.
	CallerIdentity string
}

const workCols = `id, binding_name, issue_id, issue_key, branch, agent_key, kagent_namespace, kagent_agent,
	coalesce(session_id, ''), session_generation, epoch, engineering_retries, state,
	coalesce(blocked_class, ''), coalesce(blocked_reason, ''), review_rounds, rebase_rounds, split_rounds,
	coalesce(turn_attempt_id, 0), coalesce(synced_attempt_id, 0), coalesce(stopped_reason, ''),
	coalesce(caller_identity, ''), first_arm, escalated`

func scanWork(row interface{ Scan(...any) error }) (*WorkItem, error) {
	w := &WorkItem{}
	err := row.Scan(&w.ID, &w.BindingName, &w.IssueID, &w.IssueKey, &w.Branch, &w.AgentKey,
		&w.KagentNamespace, &w.KagentAgent, &w.SessionID, &w.SessionGeneration, &w.Epoch,
		&w.EngineeringRetries, &w.State, &w.BlockedClass, &w.BlockedReason, &w.ReviewRounds, &w.RebaseRounds, &w.SplitRounds,
		&w.TurnAttemptID, &w.SyncedAttemptID, &w.StoppedReason, &w.CallerIdentity, &w.FirstArm, &w.Escalated)
	return w, err
}

// BindCaller records a caller witness once. An existing witness never changes;
// a matching repeat is harmless, but a mismatch remains fenced.
func (s *Store) BindCaller(ctx context.Context, workID int64, identity string) (bool, error) {
	if identity == "" {
		return false, errors.New("store: empty caller identity")
	}
	res, err := s.DB.ExecContext(ctx, `UPDATE work_item SET caller_identity = $2, updated_at = now()
		WHERE id = $1 AND (caller_identity IS NULL OR caller_identity = $2)`, workID, identity)
	if err != nil {
		return false, err
	}
	n, err := res.RowsAffected()
	return n == 1, err
}

// SentAttemptsForWork returns the newest sent attempt first. Legacy adoption
// must account for every uncertain predecessor, not only the latest retry.
func (s *Store) SentAttemptsForWork(ctx context.Context, workID int64) ([]*Attempt, error) {
	rows, err := s.DB.QueryContext(ctx, `SELECT `+attemptCols+` FROM delivery_attempt
		WHERE run_id IN (SELECT id FROM run WHERE work_item_id = $1)
			AND first_sent_at IS NOT NULL ORDER BY id DESC`, workID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var attempts []*Attempt
	for rows.Next() {
		a, err := scanAttempt(rows)
		if err != nil {
			return nil, err
		}
		attempts = append(attempts, a)
	}
	return attempts, rows.Err()
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
	FirstArm string
	Binding  Binding
	IssueID  int64
	IssueKey string
	AgentKey string
}

// ResolveWorkItem finds or creates the run's work item and links the run to
// it. When the work item was last delivered by another agent key, its epoch
// is bumped, reassigned is true, and in the same transaction any open
// attempt of another run gets a pending cancellation; runs without an attempt
// are durably stopped as superseded. The session stays. A
// new run makes the work item active again, except a blocked one: only an
// operator's unblock clears a block.
func (s *Store) ResolveWorkItem(ctx context.Context, runID int64, n NewWorkItem) (w *WorkItem, reassigned bool, err error) {
	err = s.Tx(ctx, func(tx *sql.Tx) error {
		w, err = scanWork(tx.QueryRowContext(ctx, `SELECT `+workCols+` FROM work_item WHERE issue_id = $1 FOR UPDATE`, n.IssueID))
		if err != nil && !errors.Is(err, sql.ErrNoRows) {
			return err
		}
		// Lock the run after the work item, in StopIssue's order. An
		// operator stop of a held run, linked or not, stands.
		var runStopped bool
		if err := tx.QueryRowContext(ctx, `SELECT stopped_reason IS NOT NULL FROM run WHERE id = $1 FOR UPDATE`,
			runID).Scan(&runStopped); err != nil {
			return err
		}
		if runStopped {
			return ErrAdmissionStopped
		}
		// Admission links a held run only when it is released, so a newer
		// delegation to another agent may already exist. It owns the issue.
		if superseded, err := s.Superseded(ctx, tx, runID); err != nil {
			return err
		} else if superseded {
			return ErrSuperseded
		}
		switch {
		case errors.Is(err, sql.ErrNoRows):
			armAgent := n.Binding.KagentAgent
			if n.FirstArm != "" && n.FirstArm != "control" {
				armAgent += "-" + n.FirstArm
			}
			w, err = scanWork(tx.QueryRowContext(ctx, `
				INSERT INTO work_item (binding_name, issue_id, issue_key, branch, agent_key, kagent_namespace, kagent_agent, first_arm)
				VALUES ($1, $2, $3, $4, $5, $6, $7, coalesce(nullif($8,''),'control')) RETURNING `+workCols,
				n.Binding.Name, n.IssueID, n.IssueKey, n.Binding.BranchPrefix+n.IssueKey, n.AgentKey,
				n.Binding.KagentNamespace, armAgent, n.FirstArm))
			if err != nil {
				return err
			}
		case err != nil:
			return err
		default:
			// Keep the stop boundary stable under the same row lock as
			// StopWork. Admission may have spent time reading dependencies.
			var stopped bool
			if err := tx.QueryRowContext(ctx, `SELECT w.stops > r.admission_stops
				FROM work_item w JOIN run r ON r.id = $2
				WHERE w.id = $1`, w.ID, runID).Scan(&stopped); err != nil {
				return err
			}
			if stopped {
				return ErrAdmissionStopped
			}
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
				// A brief-held run has no attempt to cancel. Persist its loss of
				// authority before another admission can return to the same agent.
				// MarkIssued also checks this fence if dispatch is already underway.
				if _, err := tx.ExecContext(ctx, `
					UPDATE run r SET stopped_reason = 'superseded: the work item was reassigned to another agent',
						updated_at = now()
					WHERE r.work_item_id = $1 AND r.id <> $2 AND r.stopped_reason IS NULL
						AND r.state IN ('claimed', 'waiting', 'dispatched', 'stale')
						AND NOT EXISTS (SELECT 1 FROM delivery_attempt a WHERE a.run_id = r.id)`, w.ID, runID); err != nil {
					return err
				}
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

// LockStop locks the work item, then the run, in StopWork's order, and
// reports whether either is stopped. A stop that commits after the
// caller's earlier reads is seen here, and a later StopWork waits until the
// caller's transaction ends.
func (s *Store) LockStop(ctx context.Context, tx *sql.Tx, workID, runID int64) (bool, error) {
	var workStopped, runStopped bool
	if err := tx.QueryRowContext(ctx, `SELECT state = 'stopped' FROM work_item WHERE id = $1 FOR UPDATE`, workID).Scan(&workStopped); err != nil {
		return false, err
	}
	if err := tx.QueryRowContext(ctx, `SELECT stopped_reason IS NOT NULL FROM run WHERE id = $1 FOR UPDATE`, runID).Scan(&runStopped); err != nil {
		return false, err
	}
	return workStopped || runStopped, nil
}

// Block marks a work item blocked by a credential or quota failure and
// pauses its grant's other work items. started is false when the grant was
// already blocked, so the block is announced once. A credential failure
// turns a standing quota block into a credential block and starts it: the
// grant then needs a person, and a usable quota reading must not clear it.
func (s *Store) Block(ctx context.Context, q Querier, w *WorkItem, class, reason string) (started bool, err error) {
	if _, err := q.ExecContext(ctx, `
		UPDATE work_item SET state = 'blocked', blocked_class = $2, blocked_reason = $3, updated_at = now()
		WHERE id = $1`, w.ID, class, reason); err != nil {
		return false, err
	}
	res, err := q.ExecContext(ctx, `
		INSERT INTO grant_block (scope, class, reason) VALUES ($1, $2, $3)
		ON CONFLICT (scope) DO UPDATE SET class = $2, reason = $3, created_at = now(), cleared_at = NULL
		WHERE grant_block.cleared_at IS NOT NULL
			OR (grant_block.class = 'quota' AND EXCLUDED.class = 'credential')`,
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

// QuotaBlock is a quota failure's pause of a grant, from CreatedAt.
type QuotaBlock struct {
	Scope     string
	CreatedAt time.Time
}

// QuotaBlocks lists the grants a quota failure pauses.
func (s *Store) QuotaBlocks(ctx context.Context) ([]QuotaBlock, error) {
	rows, err := s.DB.QueryContext(ctx,
		`SELECT scope, created_at FROM grant_block WHERE class = 'quota' AND cleared_at IS NULL ORDER BY scope`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []QuotaBlock
	for rows.Next() {
		var b QuotaBlock
		if err := rows.Scan(&b.Scope, &b.CreatedAt); err != nil {
			return nil, err
		}
		out = append(out, b)
	}
	return out, rows.Err()
}

// RecordQuotaReads records a broker quota read under each of its message
// IDs before the call, in one transaction under the writer's epoch.
// Repeating a read keeps its message ID and counts the attempt, including
// after a writer takeover.
func (s *Store) RecordQuotaReads(ctx context.Context, w *Writer, messageIDs []string) error {
	return w.Tx(ctx, func(tx *sql.Tx) error {
		for _, id := range messageIDs {
			if _, err := tx.ExecContext(ctx, `
				INSERT INTO quota_read (message_id, writer_epoch) VALUES ($1, $2)
				ON CONFLICT (message_id) DO UPDATE SET attempts = quota_read.attempts + 1,
					writer_epoch = EXCLUDED.writer_epoch, attempted_at = now()`, id, w.Epoch); err != nil {
				return err
			}
		}
		return nil
	})
}

// ClearQuotaBlocks clears the quota blocks that began before a usable quota
// reading was received, and the work items quota failures blocked. It
// returns the cleared scopes. A credential failure needs a person: a grant
// with a credential-blocked work item keeps its block, and that work item
// stays blocked, until the operator's unblock (ClearGrantBlock).
func (s *Store) ClearQuotaBlocks(ctx context.Context, before time.Time) ([]string, error) {
	var scopes []string
	err := s.Tx(ctx, func(tx *sql.Tx) error {
		scopes = nil
		rows, err := tx.QueryContext(ctx, `
			UPDATE grant_block g SET cleared_at = now()
			WHERE g.class = 'quota' AND g.cleared_at IS NULL AND g.created_at < $1
				AND NOT EXISTS (SELECT 1 FROM work_item w
					WHERE w.state = 'blocked' AND w.blocked_class = 'credential'
						AND 'kagent:' || w.kagent_namespace || '/' || w.kagent_agent = g.scope)
			RETURNING g.scope`, before)
		if err != nil {
			return err
		}
		for rows.Next() {
			var scope string
			if err := rows.Scan(&scope); err != nil {
				rows.Close()
				return err
			}
			scopes = append(scopes, scope)
		}
		if err := rows.Close(); err != nil {
			return err
		}
		if err := rows.Err(); err != nil {
			return err
		}
		for _, scope := range scopes {
			if _, err := tx.ExecContext(ctx, `
				UPDATE work_item SET state = 'active', blocked_class = NULL, blocked_reason = NULL, updated_at = now()
				WHERE state = 'blocked' AND blocked_class = 'quota'
					AND 'kagent:' || kagent_namespace || '/' || kagent_agent = $1`, scope); err != nil {
				return err
			}
		}
		return nil
	})
	return scopes, err
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
	AttemptSending   = "sending"   // ready to send; every prior call definitively unaccepted
	AttemptAmbiguous = "ambiguous" // the send's outcome is unknown
	AttemptAccepted  = "accepted"  // kagent holds the message in a task
	AttemptCompleted = "completed"
	AttemptFailed    = "failed"
	AttemptCanceled  = "canceled"
)

// Attempt is one send to kagent.
type Attempt struct {
	Provenance      provenance.Record
	RenderedPrompt  string
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
	// EventCount is the task's size when the connector last saw it change,
	// at EventAt. Both are zero before the first read, including on an
	// attempt sent before the quota_stall migration.
	EventCount int
	EventAt    time.Time
}

// Open reports whether the attempt has not reached a final state.
func (a *Attempt) Open() bool {
	return a.State == AttemptSending || a.State == AttemptAmbiguous || a.State == AttemptAccepted
}

const attemptCols = `id, run_id, seq, run_attempt, work_item_epoch, decision_id, kind, message_id,
	kagent_namespace, kagent_agent, coalesce(context_id, ''), coalesce(task_id, ''), state,
	coalesce(error_class, ''), coalesce(error, ''), writer_epoch, issues,
	coalesce(first_sent_at, 'epoch'::timestamptz), coalesce(sent_at, 'epoch'::timestamptz), coalesce(context, ''),
	event_count, event_at, coalesce(rendered_prompt, ''),
	coalesce(model, ''), coalesce(reasoning_effort, ''), coalesce(arm, ''),
	coalesce(base_sha, ''), coalesce(prompt_hash, ''), coalesce(task_class, '')`

func scanAttempt(row interface{ Scan(...any) error }) (*Attempt, error) {
	a := &Attempt{}
	var eventAt sql.NullTime
	err := row.Scan(&a.ID, &a.RunID, &a.Seq, &a.RunAttempt, &a.WorkItemEpoch, &a.DecisionID, &a.Kind,
		&a.MessageID, &a.KagentNamespace, &a.KagentAgent, &a.ContextID, &a.TaskID, &a.State,
		&a.ErrorClass, &a.Error, &a.WriterEpoch, &a.Issues, &a.FirstSentAt, &a.SentAt, &a.Context,
		&a.EventCount, &eventAt, &a.RenderedPrompt,
		&a.Provenance.Model, &a.Provenance.ReasoningEffort, &a.Provenance.Arm,
		&a.Provenance.BaseSHA, &a.Provenance.PromptHash, &a.Provenance.TaskClass)
	a.EventAt = eventAt.Time
	return a, err
}

// SetAttemptEvents records that the attempt's task changed to count at at,
// and whether a tool call in it has no result yet.
func (s *Store) SetAttemptEvents(ctx context.Context, q Querier, id int64, count int, toolRunning bool, at time.Time) error {
	_, err := q.ExecContext(ctx, `UPDATE delivery_attempt SET event_count = $2, tool_running = $3, event_at = $4 WHERE id = $1`,
		id, count, toolRunning, at)
	return err
}

// QuietAttempts lists the running turns whose task has not changed since
// before: the latest attempt of a dispatched run without a stop, accepted,
// not held, without a running tool call and without a pending
// cancellation. A turn whose task the connector has not read yet is not
// quiet: event_at is unset on a new attempt and, at rollout, on one sent
// before the migration, whose activity is unknown. An open tool call keeps
// its exemption here without a bound: a turn that waits on a tool sends no
// model call, so the exhausted quota does not explain its silence, and a
// quota_stall cancellation would pause the grant as a quota failure. The
// liveness lease's ToolLease bounds that turn instead (LeaseExpired).
func (s *Store) QuietAttempts(ctx context.Context, before time.Time) ([]*Attempt, error) {
	rows, err := s.DB.QueryContext(ctx, `SELECT `+attemptCols+` FROM delivery_attempt a
		WHERE a.state = 'accepted' AND a.error IS NULL AND a.event_at < $1
			AND NOT a.tool_running
			AND a.id = (SELECT max(id) FROM delivery_attempt WHERE run_id = a.run_id)
			AND EXISTS (SELECT 1 FROM run r WHERE r.id = a.run_id AND r.state = 'dispatched' AND r.stopped_reason IS NULL)
			AND NOT EXISTS (SELECT 1 FROM cancellation c WHERE c.delivery_attempt_id = a.id AND c.state = 'pending')
		ORDER BY a.id`, before)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []*Attempt
	for rows.Next() {
		a, err := scanAttempt(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, a)
	}
	return out, rows.Err()
}

// LeaseExpired lists the running turns past their liveness lease (HOME-141):
// the latest attempt of a dispatched run without a stop or a pending
// cancellation, either sent before sentBefore without a task, or accepted
// with no task event since quietBefore and no open tool call, or since
// toolBefore with one (HOME-159). A recovery hold does not exempt a turn:
// the lease's cancellation settles it. An operator's attestation does: its
// recovery owns the turn. A turn whose task the connector has not read yet
// has no event_at and waits for a read.
func (s *Store) LeaseExpired(ctx context.Context, sentBefore, quietBefore, toolBefore time.Time) ([]*Attempt, error) {
	rows, err := s.DB.QueryContext(ctx, `SELECT `+attemptCols+` FROM delivery_attempt a
		WHERE ((a.state = 'ambiguous' AND a.task_id IS NULL AND a.sent_at < $1)
				OR (a.state = 'accepted' AND NOT a.tool_running AND a.event_at < $2)
				OR (a.state = 'accepted' AND a.tool_running AND a.event_at < $3))
			AND a.id = (SELECT max(id) FROM delivery_attempt WHERE run_id = a.run_id)
			AND EXISTS (SELECT 1 FROM run r WHERE r.id = a.run_id AND r.state = 'dispatched' AND r.stopped_reason IS NULL)
			AND NOT EXISTS (SELECT 1 FROM cancellation c WHERE c.delivery_attempt_id = a.id AND c.state = 'pending')
			AND NOT EXISTS (SELECT 1 FROM recovery_hold h WHERE h.target_attempt_id = a.id AND h.state = 'attested')
		ORDER BY a.id`, sentBefore, quietBefore, toolBefore)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []*Attempt
	for rows.Next() {
		a, err := scanAttempt(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, a)
	}
	return out, rows.Err()
}

// StopMergedRun stops a live run whose work item's pull request merged after
// the run was claimed, as StopWork stops a run: the run carries the stop, so
// step cancels its turn and reports it. A review round always predates the
// merge. A run claimed after the merge, such as a question on the done
// issue, keeps running. It reports whether it stopped the run.
func (s *Store) StopMergedRun(ctx context.Context, runID int64) (bool, error) {
	res, err := s.DB.ExecContext(ctx, `
		UPDATE run r SET stopped_reason = 'its pull request #' || p.number || ' merged', updated_at = now()
		FROM work_item w, pull_request p
		WHERE r.id = $1 AND w.id = r.work_item_id AND w.state = 'done' AND p.work_item_id = w.id
			AND p.state = 'merged' AND r.stopped_reason IS NULL
			AND r.state IN ('claimed', 'waiting', 'dispatched')
			AND (r.origin = 'connector' OR r.created_at < p.merged_at)`, runID)
	if err != nil {
		return false, err
	}
	n, err := res.RowsAffected()
	return n == 1, err
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

// ErrAttemptChanged means an observation no longer describes the current turn.
var ErrAttemptChanged = errors.New("store: attempt or work generation changed")

func lockAttempt(ctx context.Context, tx *sql.Tx, workID, attemptID int64) error {
	for _, lock := range []struct {
		query string
		id    int64
	}{
		{`SELECT id FROM work_item WHERE id = $1 FOR UPDATE`, workID},
		{`SELECT id FROM run WHERE work_item_id = $1 ORDER BY id FOR UPDATE`, workID},
		{`SELECT id FROM delivery_attempt WHERE id = $1 FOR UPDATE`, attemptID},
	} {
		if _, err := tx.ExecContext(ctx, lock.query, lock.id); err != nil {
			return err
		}
	}
	return nil
}

// SupersededGrantFailure locks a waiting grant failure and reports whether
// another run has since started work on the same issue. Call from Writer.Tx
// and resolve only the old run in that transaction; newer work keeps its state.
func (s *Store) SupersededGrantFailure(ctx context.Context, tx *sql.Tx, r *Run, a *Attempt) (bool, error) {
	if err := lockAttempt(ctx, tx, r.WorkItemID, a.ID); err != nil {
		return false, err
	}
	var superseded bool
	err := tx.QueryRowContext(ctx, `SELECT EXISTS (
		SELECT 1 FROM run r JOIN delivery_attempt a ON a.run_id = r.id
		WHERE r.id = $1 AND r.work_item_id = $2 AND r.attempt = $3 AND r.state = 'waiting'
			AND r.stopped_reason IS NULL
			AND a.id = $4 AND a.state = 'failed' AND a.error_class IN ('credential', 'quota')
			AND a.id = (SELECT max(id) FROM delivery_attempt WHERE run_id = r.id)
			AND EXISTS (SELECT 1 FROM delivery_attempt newer JOIN run nr ON nr.id = newer.run_id
				WHERE nr.work_item_id = r.work_item_id AND nr.id <> r.id AND newer.id > a.id
					AND coalesce(newer.task_id, '') <> ''))`,
		r.ID, r.WorkItemID, r.Attempt, a.ID).Scan(&superseded)
	return superseded, err
}

// AttemptTx applies a task outcome under the executing writer's authority.
// Lock the work item, its runs and the attempt in send-admission order, then
// validate the snapshots used to read the task. Locking all runs also fences
// new attempts on another existing run through their foreign keys.
// Keep those locks through commit:
// a separate check would let a stop, redelivery or newer turn race the writes.
// The attempt's WriterEpoch is its original sender, not the executing writer;
// a successor must be able to finish an inherited task.
// A waiting grant failure may retry past another run's failed or canceled,
// unattached send. Cancellation reaches that state only after non-acceptance
// or operator-attested fencing; an uncertain cancellation keeps its send open.
// Accepted tasks and open sends (including ambiguous ones) still fence it.
func (s *Store) AttemptTx(ctx context.Context, writer *Writer, r *Run, wi *WorkItem, a *Attempt, fn func(*sql.Tx) error) error {
	return writer.Tx(ctx, func(tx *sql.Tx) error {
		if err := lockAttempt(ctx, tx, wi.ID, a.ID); err != nil {
			return err
		}
		var current bool
		err := tx.QueryRowContext(ctx, `SELECT EXISTS (
			SELECT 1 FROM delivery_attempt a
			JOIN run r ON r.id = a.run_id JOIN work_item w ON w.id = r.work_item_id
			WHERE a.id = $1 AND r.id = $2 AND w.id = $3
				AND r.attempt = $4 AND r.state = $5 AND r.state IN ('dispatched', 'waiting')
				AND r.stopped_reason IS NULL
				AND w.epoch = $6 AND a.work_item_epoch = w.epoch
				AND w.session_generation = $7 AND coalesce(w.session_id, '') = $8
				AND w.state = $9 AND w.state NOT IN ('stopped', 'blocked')
				AND w.review_rounds = $10 AND w.engineering_retries = $11
				AND coalesce(w.turn_attempt_id, 0) = $12
				AND a.state = $13 AND coalesce(a.task_id, '') = $14 AND coalesce(a.context_id, '') = $15
				AND NOT EXISTS (SELECT 1 FROM delivery_attempt other
					JOIN run other_run ON other_run.id = other.run_id
					WHERE other_run.work_item_id = w.id AND other.id > a.id
						AND NOT (r.state = 'waiting' AND a.state = 'failed'
							AND coalesce(a.error_class, '') IN ('credential', 'quota')
							AND other.run_id <> a.run_id AND other.state IN ('failed', 'canceled')
							AND coalesce(other.task_id, '') = ''))
				AND NOT EXISTS (SELECT 1 FROM cancellation c JOIN run cr ON cr.id = c.run_id
					WHERE cr.work_item_id = w.id AND c.state = 'pending'))`,
			a.ID, r.ID, wi.ID, r.Attempt, r.State, wi.Epoch, wi.SessionGeneration, wi.SessionID,
			wi.State, wi.ReviewRounds, wi.EngineeringRetries, wi.TurnAttemptID, a.State, a.TaskID, a.ContextID).Scan(&current)
		if err != nil {
			return err
		}
		if !current {
			return ErrAttemptChanged
		}
		return fn(tx)
	})
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

// MarkIssued linearizes send admission with stop/cancel and writer takeover.
// A stop committed before admission suppresses the call; a later stop cancels
// this now-ambiguous send. An unknown outcome never becomes sendable again.
// expectedRunAttempt fences a stale dispatcher after transport redelivery;
// a.RunAttempt retains the original delivery's authority for recovery evidence.
func (s *Store) MarkIssued(ctx context.Context, w *Writer, a *Attempt, expectedRunAttempt int, at time.Time) (bool, error) {
	issued := false
	err := w.Tx(ctx, func(tx *sql.Tx) error {
		var workID int64
		if err := tx.QueryRowContext(ctx, `SELECT work_item_id FROM run WHERE id = $1`, a.RunID).Scan(&workID); err != nil {
			return err
		}
		// StopIssue locks the work item; cancellation inserts lock the attempt
		// through its foreign key. Keep both stable until admission commits.
		for _, lock := range []struct {
			table string
			id    int64
		}{{"work_item", workID}, {"run", a.RunID}, {"delivery_attempt", a.ID}} {
			if _, err := tx.ExecContext(ctx, "SELECT id FROM "+lock.table+" WHERE id = $1 FOR UPDATE", lock.id); err != nil {
				return err
			}
		}
		res, err := tx.ExecContext(ctx, `
			UPDATE delivery_attempt a SET state = 'ambiguous', execution_possible = true, sent_at = $2,
				first_sent_at = coalesce(first_sent_at, $2), issues = issues + 1
			FROM run r JOIN work_item w ON w.id = r.work_item_id
			WHERE a.id = $1 AND a.state = 'sending' AND r.id = a.run_id
				AND r.state = 'dispatched' AND a.work_item_epoch = w.epoch AND r.attempt = $3
				AND r.stopped_reason IS NULL
				AND a.id = (SELECT max(id) FROM delivery_attempt WHERE run_id = r.id)
				AND w.state NOT IN ('stopped', 'blocked')
				AND NOT EXISTS (SELECT 1 FROM cancellation c JOIN run cr ON cr.id = c.run_id
					WHERE cr.work_item_id = w.id AND c.state = 'pending')`, a.ID, at, expectedRunAttempt)
		if err != nil {
			return err
		}
		n, err := res.RowsAffected()
		issued = n == 1
		return err
	})
	if err == nil && issued {
		a.State, a.SentAt, a.Issues = AttemptAmbiguous, at, a.Issues+1
		if a.FirstSentAt.Unix() <= 0 {
			a.FirstSentAt = at
		}
	}
	return issued, err
}

// MarkNotAccepted persists a definitive refusal of a call made from sending.
// A crash before this write leaves ambiguity and therefore cannot resend.
func (s *Store) MarkNotAccepted(ctx context.Context, a *Attempt) error {
	_, err := s.DB.ExecContext(ctx, `
		UPDATE delivery_attempt SET state = 'sending', execution_possible = false
		WHERE id = $1 AND state = 'ambiguous'`, a.ID)
	if err == nil {
		a.State = AttemptSending
	}
	return err
}

// HoldAttempt records an operator-visible reason without ending the attempt or
// releasing its slot. The original message and caller remain authoritative.
func (s *Store) HoldAttempt(ctx context.Context, q Querier, id int64, reason string) error {
	_, err := q.ExecContext(ctx, `
		UPDATE delivery_attempt SET error = $2
		WHERE id = $1 AND state IN ('sending', 'ambiguous', 'accepted')`, id, reason)
	return err
}

// MarkAccepted attaches the attempt to the kagent task holding its message.
func (s *Store) MarkAccepted(ctx context.Context, q Querier, id int64, contextID, taskID string) error {
	_, err := q.ExecContext(ctx, `
		UPDATE delivery_attempt SET state = 'accepted', context_id = $2, task_id = $3, error = NULL
		WHERE id = $1 AND state IN ('sending', 'ambiguous', 'accepted')`, id, contextID, taskID)
	if err != nil {
		return err
	}
	return s.AttributeUsage(ctx, q, id)
}

// FinishAttempt records an attempt's final state.
func (s *Store) FinishAttempt(ctx context.Context, q Querier, id int64, state, class, msg string) error {
	_, err := q.ExecContext(ctx, `
		UPDATE delivery_attempt SET state = $2, error_class = nullif($3, ''), error = nullif($4, ''),
			finished_at = now()
		WHERE id = $1`, id, state, class, msg)
	return err
}

// CompleteTask preserves the kagent outcome for successful connector attempts.
func (s *Store) CompleteTask(ctx context.Context, q Querier, id int64, outcome string) error {
	_, err := q.ExecContext(ctx, `
        UPDATE delivery_attempt SET state = 'completed', kagent_outcome = $2,
            error_class = NULL, error = NULL, finished_at = now()
        WHERE id = $1`, id, outcome)
	return err
}

// Briefs lists the contexts the work item's session-opening sends
// recorded, newest first.
func (s *Store) Briefs(ctx context.Context, workID int64) ([]string, error) {
	rows, err := s.DB.QueryContext(ctx, `
		SELECT a.context FROM delivery_attempt a JOIN run r ON r.id = a.run_id
		WHERE r.work_item_id = $1 AND a.kind = 'initial' AND a.context IS NOT NULL
		ORDER BY a.id DESC`, workID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []string
	for rows.Next() {
		var c string
		if err := rows.Scan(&c); err != nil {
			return nil, err
		}
		out = append(out, c)
	}
	return out, rows.Err()
}

// RecordTaskMetadataRead journals each poll before it reaches kagent. The caller
// commits on the writer's lock connection; retries keep the same read identity.
func (s *Store) RecordTaskMetadataRead(ctx context.Context, q Querier, a *Attempt, epoch int64, messageID string) error {
	_, err := q.ExecContext(ctx, `
		INSERT INTO task_metadata_read (message_id, attempt_id, task_id, context_id, writer_epoch)
		VALUES ($1, $2, $3, $4, $5)
		ON CONFLICT (message_id) DO UPDATE SET attempts = task_metadata_read.attempts + 1,
			writer_epoch = EXCLUDED.writer_epoch, attempted_at = now()`,
		messageID, a.ID, a.TaskID, a.ContextID, epoch)
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
	CancelQuotaStall   = "quota_stall"   // the turn went quiet on an exhausted quota
	CancelLeaseExpired = "lease_expired" // the turn showed no task or progress within its lease
	CancelPending      = "pending"
	CancelConfirmed    = "confirmed"
	CancelWithdrawn    = "withdrawn" // a quota_stall or lease_expired the task's activity overtook before CancelTask
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
	// EventCount is the task size a quota_stall or lease_expired
	// cancellation was decided on.
	EventCount int
	// RequestedAt is when the cancellation was recorded, on the engine's
	// clock (Store.Now).
	RequestedAt time.Time
}

// RequestCancellation records a pending cancellation for an attempt; a
// pending one already recorded is kept. requested_at is the engine's
// clock, which the force-delete escalation compares with.
func (s *Store) RequestCancellation(ctx context.Context, q Querier, attemptID, runID int64, reason string) error {
	_, err := q.ExecContext(ctx, `
		INSERT INTO cancellation (delivery_attempt_id, run_id, reason, state, requested_at)
		VALUES ($1, $2, $3, 'pending', $4)
		ON CONFLICT (delivery_attempt_id) WHERE state = 'pending' DO NOTHING`, attemptID, runID, reason, s.now())
	return err
}

// WorkerGone reports evidence still actionable for this attempt. A confirmed
// lease cancellation may leave a terminal task open for progress to deliver.
func (s *Store) WorkerGone(ctx context.Context, a *Attempt) (bool, error) {
	var gone bool
	err := s.DB.QueryRowContext(ctx, `SELECT EXISTS (SELECT 1 FROM worker_fence
		WHERE attempt_id=$1 AND session_id=$2 AND gone_at IS NOT NULL
		AND actor_uid <> '' AND actor_version > 0
		AND NOT EXISTS (SELECT 1 FROM cancellation c WHERE c.delivery_attempt_id=$1
			AND c.reason='lease_expired' AND c.state='confirmed'))`, a.ID, a.ContextID).Scan(&gone)
	return gone, err
}

// RequestQuietCancel records a pending quota_stall or lease_expired
// cancellation decided on the task at eventCount; a pending one already
// recorded is kept.
func (s *Store) RequestQuietCancel(ctx context.Context, q Querier, attemptID, runID int64, reason string, eventCount int) error {
	_, err := q.ExecContext(ctx, `
		INSERT INTO cancellation (delivery_attempt_id, run_id, reason, state, event_count, requested_at)
		VALUES ($1, $2, $3, 'pending', $4, $5)
		ON CONFLICT (delivery_attempt_id) WHERE state = 'pending' DO NOTHING`, attemptID, runID, reason, eventCount, s.now())
	return err
}

// WithdrawQuietCancel closes a pending quota_stall or lease_expired
// cancellation that has sent no CancelTask, while its run is dispatched. It
// reports whether it withdrew it. The caller holds the run's row lock
// (LockStop).
func (s *Store) WithdrawQuietCancel(ctx context.Context, q Querier, id int64, outcome string) (bool, error) {
	res, err := q.ExecContext(ctx, `
		UPDATE cancellation c SET state = 'withdrawn', outcome = $2
		WHERE c.id = $1 AND c.state = 'pending' AND c.reason IN ('quota_stall', 'lease_expired') AND c.requests = 0
			AND EXISTS (SELECT 1 FROM run r WHERE r.id = c.run_id AND r.state = 'dispatched')`, id, outcome)
	if err != nil {
		return false, err
	}
	n, err := res.RowsAffected()
	return n == 1, err
}

// WithdrawLease closes the attempt's pending lease_expired cancellation that
// has sent no CancelTask, while its run is dispatched. It reports whether it
// withdrew one. The caller holds the run's row lock (LockStop).
func (s *Store) WithdrawLease(ctx context.Context, q Querier, attemptID int64, outcome string) (bool, error) {
	res, err := q.ExecContext(ctx, `
		UPDATE cancellation c SET state = 'withdrawn', outcome = $2
		WHERE c.delivery_attempt_id = $1 AND c.state = 'pending' AND c.reason = 'lease_expired' AND c.requests = 0
			AND EXISTS (SELECT 1 FROM run r WHERE r.id = c.run_id AND r.state = 'dispatched')`, attemptID, outcome)
	if err != nil {
		return false, err
	}
	n, err := res.RowsAffected()
	return n == 1, err
}

// CancelPending reports whether the attempt has a pending cancellation.
func (s *Store) CancelPending(ctx context.Context, attemptID int64) (bool, error) {
	var ok bool
	err := s.DB.QueryRowContext(ctx, `
		SELECT EXISTS (SELECT 1 FROM cancellation WHERE delivery_attempt_id = $1 AND state = 'pending')`, attemptID).Scan(&ok)
	return ok, err
}

// PendingCancellations lists cancellations kagent has not confirmed.
func (s *Store) PendingCancellations(ctx context.Context) ([]*Cancellation, error) {
	rows, err := s.DB.QueryContext(ctx, `
		SELECT id, delivery_attempt_id, run_id, reason, state, requests,
			coalesce(last_request_at, 'epoch'::timestamptz), coalesce(outcome, ''), coalesce(event_count, 0), requested_at
		FROM cancellation WHERE state = 'pending' ORDER BY id`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []*Cancellation
	for rows.Next() {
		c := &Cancellation{}
		if err := rows.Scan(&c.ID, &c.AttemptID, &c.RunID, &c.Reason, &c.State, &c.Requests, &c.LastAt, &c.Outcome, &c.EventCount, &c.RequestedAt); err != nil {
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

// CountCancelRequest records that CancelTask, or a force delete of the
// attempt's session, was sent.
func (s *Store) CountCancelRequest(ctx context.Context, q Querier, id int64, at time.Time) error {
	_, err := q.ExecContext(ctx, `UPDATE cancellation SET requests = requests + 1, last_request_at = $2 WHERE id = $1`, id, at)
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
	OutboxResult         = "result"
	OutboxComment        = "comment"
	OutboxIssueDone      = "issue_done"
	OutboxDeferredStart  = "deferred_start"
	OutboxReviewFollowup = "review_followup"
	OutboxActivation     = "activation"
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
	// SendStarted is set once an at-most-once item's send began.
	SendStarted bool
}

// Enqueue records a side effect; a duplicate dedupe key is ignored.
func (s *Store) Enqueue(ctx context.Context, q Querier, kind, dedupe string, runID int64, agentKey string, payload any) error {
	_, err := s.EnqueueOnce(ctx, q, kind, dedupe, runID, agentKey, payload)
	return err
}

// EnqueueOnce is Enqueue that reports whether this call added the item, false
// when its dedupe key was already queued.
func (s *Store) EnqueueOnce(ctx context.Context, q Querier, kind, dedupe string, runID int64, agentKey string, payload any) (bool, error) {
	b, err := json.Marshal(payload)
	if err != nil {
		return false, err
	}
	res, err := q.ExecContext(ctx, `
		INSERT INTO outbox (kind, dedupe_key, run_id, agent_key, payload, next_attempt_at)
		VALUES ($1, $2, nullif($3, 0), nullif($4, ''), $5, $6)
		ON CONFLICT (dedupe_key) DO NOTHING`, kind, dedupe, runID, agentKey, b, s.now())
	if err != nil {
		return false, err
	}
	n, err := res.RowsAffected()
	return n > 0, err
}

// PendingOutbox lists undelivered side effects that are due, in order. A
// failing item backs off, so it does not hold newer ones back.
func (s *Store) PendingOutbox(ctx context.Context, now time.Time, limit int) ([]*OutboxItem, error) {
	rows, err := s.DB.QueryContext(ctx, `
		SELECT id, kind, dedupe_key, coalesce(run_id, 0), coalesce(agent_key, ''), payload, tries,
			send_started_at IS NOT NULL
		FROM outbox WHERE delivered_at IS NULL AND abandoned_at IS NULL AND next_attempt_at <= $1
		ORDER BY id LIMIT $2`, now, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []*OutboxItem
	for rows.Next() {
		o := &OutboxItem{}
		if err := rows.Scan(&o.ID, &o.Kind, &o.DedupeKey, &o.RunID, &o.AgentKey, &o.Payload, &o.Tries, &o.SendStarted); err != nil {
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

// OutboxSendStarted records, before an at-most-once item is sent, that its
// send began; OutboxSendCleared undoes it once the request provably never
// left.
func (s *Store) OutboxSendStarted(ctx context.Context, id int64) error {
	_, err := s.DB.ExecContext(ctx, `UPDATE outbox SET send_started_at = now() WHERE id = $1`, id)
	return err
}

func (s *Store) OutboxSendCleared(ctx context.Context, id int64) error {
	_, err := s.DB.ExecContext(ctx, `UPDATE outbox SET send_started_at = NULL WHERE id = $1`, id)
	return err
}

// OutboxFailed records a failed delivery and when to try again.
func (s *Store) OutboxFailed(ctx context.Context, id int64, msg string, next time.Time, unavailableSince *time.Time) error {
	_, err := s.DB.ExecContext(ctx, `UPDATE outbox SET tries = tries + 1, last_error = $2, next_attempt_at = $3,
		unavailable_since = CASE WHEN $4::timestamptz IS NULL THEN NULL ELSE coalesce(unavailable_since, $4) END WHERE id = $1`,
		id, msg, next, unavailableSince)
	return err
}

// PendingOutage includes failed operations waiting for their next retry.
func (s *Store) PendingOutage(ctx context.Context) (sql.NullTime, error) {
	var since sql.NullTime
	err := s.DB.QueryRowContext(ctx, `SELECT min(unavailable_since) FROM outbox
		WHERE unavailable_since IS NOT NULL AND delivered_at IS NULL AND abandoned_at IS NULL`).Scan(&since)
	return since, err
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

// Both transaction owners register their after-commit work here. Dropping
// the entry on any exit discards observations of rolled-back changes.
var commitHooks sync.Map // *sql.Tx -> *[]func()

func commitWithHooks(tx *sql.Tx) error {
	if err := tx.Commit(); err != nil {
		return err
	}
	if hooks, ok := commitHooks.Load(tx); ok {
		for _, fn := range *hooks.(*[]func()) {
			fn()
		}
	}
	return nil
}

// Tx runs fn in a transaction.
func (s *Store) Tx(ctx context.Context, fn func(*sql.Tx) error) error {
	tx, err := s.DB.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	commitHooks.Store(tx, new([]func()))
	defer commitHooks.Delete(tx)
	if err := fn(tx); err != nil {
		_ = tx.Rollback()
		return err
	}
	return commitWithHooks(tx)
}
