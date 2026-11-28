package dispatch

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/github"
	"github.com/jomcgi/homelab/projects/factory/connector/itsaplan"
	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
	"github.com/jomcgi/homelab/projects/factory/connector/planner"
)

// PlannerRuntime starts planner sessions for itsaplan cron runs (HOME-546).
// A nil runtime starts none and leaves filing inert.
type PlannerRuntime struct {
	// AgentKey is the itsaplan agent whose runs the connector claims for
	// planning. It is never one of the coding agents.
	AgentKey string
	// Agent is the planner Agent, in its own namespace.
	Agent kagent.AgentRef
	// RunTimeout bounds one session from admission.
	RunTimeout time.Duration
}

const (
	// plannerMaxSends bounds definite refusals of one run's message.
	plannerMaxSends = 5
	// plannerMaxDeletes bounds the delete tries of one finished session.
	plannerMaxDeletes = 5
	// plannerReportBackoff spaces result reports an outage refuses.
	plannerReportBackoff = 30 * time.Second
	// What itsaplan shows for a refused or finished run. Neither carries
	// model text.
	plannerNotAdmitted = "Not admitted: "
	plannerCompleted   = "Planner run completed."
)

// stepPlannerRuns claims one planner run when none is active, then moves every
// active run one step. A run is never started without admit_planner_run, and
// a replayed claim, which reuses its row, never starts a second session.
// invariant: HOME-40-1
func (e *Engine) stepPlannerRuns(ctx context.Context) error {
	p := e.cfg.Planner
	if p == nil {
		return nil
	}
	var errs []error
	if err := e.claimPlanner(ctx); err != nil {
		if fatal(err) {
			return err
		}
		errs = append(errs, err)
	}
	runs, err := e.activePlannerRuns(ctx)
	if err != nil {
		return err
	}
	for i := range runs {
		if err := e.advancePlannerRun(ctx, &runs[i]); err != nil {
			if fatal(err) {
				return err
			}
			errs = append(errs, fmt.Errorf("planner run %s: %w", runs[i].ID, err))
		}
	}
	return errors.Join(errs...)
}

func (e *Engine) claimPlanner(ctx context.Context) error {
	p := e.cfg.Planner
	// One planner session at a time: the planner shares a low-priority slot
	// and never queues ahead of coding work.
	var active int
	if err := e.st.DB.QueryRowContext(ctx, `SELECT count(*) FROM planner_run
 WHERE itsaplan_run_id IS NOT NULL AND reported_at IS NULL`).Scan(&active); err != nil {
		return err
	}
	if active > 0 {
		return nil
	}
	if err := e.guard(ctx); err != nil {
		return err
	}
	run, err := e.its.Claim(ctx, p.AgentKey)
	if err != nil {
		e.m.Claims.WithLabelValues(p.AgentKey, "error").Inc()
		e.m.PlannerSessionError.WithLabelValues("claim").Inc()
		e.countRefusal(roleAgent, "claim", err)
		return fmt.Errorf("claim %s: %w", p.AgentKey, err)
	}
	if run == nil {
		e.m.Claims.WithLabelValues(p.AgentKey, "empty").Inc()
		return nil
	}
	return e.handlePlannerClaim(ctx, run)
}

// handlePlannerClaim admits a claimed run, or reports why it did not start.
// The claim is stored with its admission in one transaction.
// invariant: HOME-40-1
func (e *Engine) handlePlannerClaim(ctx context.Context, run *itsaplan.Run) error {
	p := e.cfg.Planner
	name := planner.ScheduleName(run.Prompt)
	runID := fmt.Sprintf("itsaplan-%d", run.ID)
	// The prompt is the schedule's name only. A person can start a cron
	// schedule's run by hand, so accept that trigger too: admission bounds
	// both alike.
	selected := (run.Trigger == "schedule" || run.Trigger == "manual") && run.IssueID == nil && name != ""
	var refusal, label string
	outcome := "replay"
	err := e.w.Tx(ctx, func(tx *sql.Tx) error {
		refusal, label, outcome = "", "unknown", "replay"
		var replay bool
		err := tx.QueryRowContext(ctx, `UPDATE planner_run SET itsaplan_attempt = greatest(coalesce(itsaplan_attempt, 0), $2),
 reported_at = CASE WHEN report_status IS NULL THEN reported_at END,
 report_attempts = CASE WHEN report_status IS NULL THEN report_attempts ELSE 0 END,
 report_after = NULL
 WHERE itsaplan_run_id = $1 RETURNING true`, run.ID, run.Attempts).Scan(&replay)
		if err == nil {
			// A transport redelivery resumes the stored run.
			return nil
		}
		if !errors.Is(err, sql.ErrNoRows) {
			return err
		}
		outcome = "refused"
		if !selected {
			refusal = "not a planner schedule run"
			return nil
		}
		var ready bool
		err = tx.QueryRowContext(ctx, `SELECT enabled AND prompt <> '' FROM planner_schedule WHERE name = $1`, name).Scan(&ready)
		if errors.Is(err, sql.ErrNoRows) {
			refusal = "unknown planner schedule"
			return nil
		}
		if err != nil {
			return err
		}
		label = name
		if !ready {
			refusal = "schedule is disabled or has no prompt"
			return nil
		}
		var admitted bool
		if err := tx.QueryRowContext(ctx, `SELECT admit_planner_run($1, $2)`, name, runID).Scan(&admitted); err != nil {
			return err
		}
		if !admitted {
			refusal = "admission refused by the schedule's bounds"
			return nil
		}
		outcome = "admitted"
		message := fmt.Sprintf("planner-%x", sha256.Sum256([]byte(runID)))
		_, err = tx.ExecContext(ctx, `UPDATE planner_run SET itsaplan_run_id = $2, itsaplan_attempt = $3,
 trigger = $4, message_id = $5, heartbeat_at = $6 WHERE run_id = $1`, runID, run.ID, run.Attempts, run.Trigger, message, e.cfg.Now())
		return err
	})
	if err != nil {
		return err
	}
	e.m.Claims.WithLabelValues(p.AgentKey, map[string]string{"admitted": "new", "refused": "refused", "replay": "redelivery"}[outcome]).Inc()
	if outcome == "replay" {
		return nil
	}
	e.m.PlannerRuns.WithLabelValues(label, outcome).Inc()
	if refusal == "" {
		return nil
	}
	// A refused run starts no session. Report it so itsaplan does not hand it
	// out again; a lost report only redelivers the claim to the same checks.
	if err := e.guard(ctx); err != nil {
		return err
	}
	err = e.its.Result(ctx, p.AgentKey, run.ID, itsaplan.Result{Attempt: run.Attempts, Status: "success", Output: plannerNotAdmitted + refusal + "."})
	if err != nil && !errors.Is(err, itsaplan.ErrNotFound) {
		e.countRefusal(roleAgent, "result", err)
		return fmt.Errorf("report refused planner run %d: %w", run.ID, err)
	}
	return nil
}

type plannerRun struct {
	ID, Schedule, Namespace, Prompt string
	PerRun                          int
	ItsRun                          int64
	Attempt                         int
	MessageID, ContextID, TaskID    string
	SendStarted                     sql.NullTime
	SendAttempts                    int
	Heartbeat                       sql.NullTime
	ReportStatus, Failure           sql.NullString
	Reported, ReportAfter, Deleted  sql.NullTime
	ReportAttempts, DeleteAttempts  int
	Expired                         bool
	InputTokens, OutputTokens       sql.NullInt64
}

func (e *Engine) activePlannerRuns(ctx context.Context) ([]plannerRun, error) {
	rows, err := e.st.DB.QueryContext(ctx, `SELECT r.run_id, r.schedule, s.namespace, s.prompt, s.per_run,
 r.itsaplan_run_id, coalesce(r.itsaplan_attempt, 0), r.message_id, coalesce(r.context_id, ''), coalesce(r.task_id, ''),
 r.send_started_at, r.send_attempts, r.heartbeat_at, r.report_status, r.failure,
 r.reported_at, r.report_after, r.session_deleted_at, r.report_attempts, r.delete_attempts,
 r.started_at < now() - make_interval(secs => $1), r.input_tokens, r.output_tokens
 FROM planner_run r JOIN planner_schedule s ON s.name = r.schedule
 WHERE r.itsaplan_run_id IS NOT NULL
 AND (r.reported_at IS NULL OR (r.context_id IS NOT NULL AND r.session_deleted_at IS NULL AND r.report_status IS NOT NULL))
 ORDER BY r.started_at, r.run_id`, e.cfg.Planner.RunTimeout.Seconds())
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []plannerRun
	for rows.Next() {
		var r plannerRun
		if err := rows.Scan(&r.ID, &r.Schedule, &r.Namespace, &r.Prompt, &r.PerRun, &r.ItsRun, &r.Attempt, &r.MessageID, &r.ContextID, &r.TaskID,
			&r.SendStarted, &r.SendAttempts, &r.Heartbeat, &r.ReportStatus, &r.Failure,
			&r.Reported, &r.ReportAfter, &r.Deleted, &r.ReportAttempts, &r.DeleteAttempts,
			&r.Expired, &r.InputTokens, &r.OutputTokens); err != nil {
			return nil, err
		}
		out = append(out, r)
	}
	return out, rows.Err()
}

func (e *Engine) plannerRef() kagent.AgentRef { return e.cfg.Planner.Agent }

// plannerCaller pins one kagent caller for a step, as coding dispatch does.
func (e *Engine) plannerCaller(ctx context.Context) (context.Context, error) {
	if e.cfg.Caller == nil {
		return ctx, errCallerUncertain
	}
	ctx, identity, err := e.cfg.Caller.Pin(ctx)
	if err != nil || !identity.Valid() {
		return ctx, callerUncertain(err)
	}
	return ctx, nil
}

func (e *Engine) advancePlannerRun(ctx context.Context, r *plannerRun) error {
	if err := e.guard(ctx); err != nil {
		return err
	}
	if r.ReportStatus.Valid {
		var errs []error
		if !r.Reported.Valid {
			errs = append(errs, e.reportPlannerRun(ctx, r))
		}
		errs = append(errs, e.deletePlannerSession(ctx, r))
		return errors.Join(errs...)
	}
	if r.Expired {
		return e.failPlannerRun(ctx, r, "planner run timed out")
	}
	now := e.cfg.Now()
	if !r.Heartbeat.Valid || now.Sub(r.Heartbeat.Time) >= e.cfg.Heartbeat {
		err := e.its.Heartbeat(ctx, e.cfg.Planner.AgentKey, r.ItsRun, r.Attempt)
		switch {
		case err == nil:
			if _, err := e.st.DB.ExecContext(ctx, `UPDATE planner_run SET heartbeat_at = $2 WHERE run_id = $1`, r.ID, now); err != nil {
				return err
			}
		case errors.Is(err, itsaplan.ErrNotFound):
			return e.failPlannerRun(ctx, r, "itsaplan run is no longer active")
		default:
			e.countRefusal(roleAgent, "heartbeat", err)
			e.log.WarnContext(ctx, "planner heartbeat failed", "run", r.ID, "error", err)
		}
	}
	ctx, err := e.plannerCaller(ctx)
	if err != nil {
		return err
	}
	if r.TaskID == "" {
		return e.sendPlanner(ctx, r)
	}
	return e.readPlanner(ctx, r)
}

// plannerTask is the message the planner receives: the reviewed prompt and the
// limits the connector enforces anyway. Nothing from itsaplan or the model
// reaches it.
func plannerTask(r *plannerRun) string {
	return strings.TrimSpace(r.Prompt) + fmt.Sprintf("\n\nReturn only a JSON array of at most %d proposals. "+
		"Every fingerprint must start with %q. An empty array means nothing to report.", r.PerRun, r.Namespace+":")
}

func (e *Engine) sendPlanner(ctx context.Context, r *plannerRun) error {
	ref := e.plannerRef()
	prompt := plannerTask(r)
	if ops := e.scheduleActions(r.Schedule); len(ops) > 0 && !r.SendStarted.Valid {
		var raw []byte
		if err := e.st.DB.QueryRowContext(ctx, "SELECT triage_snapshot FROM planner_run WHERE run_id=$1", r.ID).Scan(&raw); err != nil {
			return err
		}
		if raw == nil {
			if err := e.guard(ctx); err != nil {
				return err
			}
			data, err := e.triageSnapshot(ctx, r.MessageID)
			if errors.Is(err, errTriageRefusal) {
				return e.failPlannerRun(ctx, r, "triage snapshot credential refusal limit")
			}
			if err != nil {
				return err
			}
			raw, err = json.Marshal(data)
			if err != nil {
				return err
			}
			if err := e.w.Tx(ctx, func(tx *sql.Tx) error {
				_, err := tx.ExecContext(ctx, "UPDATE planner_run SET triage_snapshot=$2 WHERE run_id=$1", r.ID, string(raw))
				return err
			}); err != nil {
				return err
			}
		}
		// Re-encode JSONB on retry so database normalization cannot unescape a delimiter.
		quoted, err := json.Marshal(json.RawMessage(raw))
		if err != nil {
			return err
		}
		prompt = r.Prompt + "\n\nAllowed operations: " + strings.Join(ops, ", ") + "\n" + planner.EvidenceOpen + "\n> " + string(quoted) + "\n" + planner.EvidenceClose
	}
	if r.ContextID == "" {
		if err := e.guard(ctx); err != nil {
			return err
		}
		// The session is fresh for this run. Its request ID is the message ID,
		// so a retry after a lost response returns the same session.
		sess, err := e.sessions.CreateSession(ctx, ref, r.MessageID)
		if err != nil {
			e.m.PlannerSessionError.WithLabelValues("session").Inc()
			return fmt.Errorf("create planner session: %w", err)
		}
		if sess == nil || sess.ContextID == "" || sess.ID != sess.ContextID || sess.AgentNamespace != ref.Namespace || sess.AgentName != ref.Name {
			return errors.New("planner session does not belong to the planner Agent")
		}
		if err := e.w.Tx(ctx, func(tx *sql.Tx) error {
			_, err := tx.ExecContext(ctx, `UPDATE planner_run SET context_id = $2 WHERE run_id = $1 AND context_id IS NULL`, r.ID, sess.ContextID)
			return err
		}); err != nil {
			return err
		}
		r.ContextID = sess.ContextID
	}
	now := e.cfg.Now()
	if r.SendStarted.Valid {
		// An earlier send's outcome is unknown. Read only: the message ID
		// finds its task, and nothing is sent again.
		t, err := e.taskWithMessage(ctx, ref, r.ContextID, r.MessageID)
		if err != nil {
			return err
		}
		if t != nil {
			return e.attachPlanner(ctx, r, t)
		}
		if now.Sub(r.SendStarted.Time) >= e.cfg.DispatchWindow {
			return e.failPlannerRun(ctx, r, "send outcome unknown")
		}
		return nil
	}
	if r.SendAttempts >= plannerMaxSends {
		return e.failPlannerRun(ctx, r, "kagent did not accept the planner message")
	}
	if err := e.w.Tx(ctx, func(tx *sql.Tx) error {
		_, err := tx.ExecContext(ctx, `UPDATE planner_run SET send_started_at = $2, send_attempts = send_attempts + 1
 WHERE run_id = $1 AND task_id IS NULL AND send_started_at IS NULL`, r.ID, now)
		return err
	}); err != nil {
		return err
	}
	if err := e.guard(ctx); err != nil {
		return err
	}
	task, err := e.tasks.Send(ctx, ref, kagent.Message{
		MessageID: r.MessageID,
		ContextID: r.ContextID,
		Parts:     []kagent.Part{{Text: prompt}},
		Metadata: map[string]any{
			"factory.jomcgi.dev/planner-run":      r.ID,
			"factory.jomcgi.dev/planner-schedule": r.Schedule,
		},
	})
	switch {
	case err == nil:
		e.m.Sends.WithLabelValues("planner", "accepted").Inc()
		return e.attachPlanner(ctx, r, task)
	case errors.Is(err, kagent.ErrNotAccepted):
		e.m.Sends.WithLabelValues("planner", "not_accepted").Inc()
		// kagent dispatched nothing, so the same message may go again.
		return e.w.Tx(ctx, func(tx *sql.Tx) error {
			_, err := tx.ExecContext(ctx, `UPDATE planner_run SET send_started_at = NULL WHERE run_id = $1 AND task_id IS NULL`, r.ID)
			return err
		})
	case errors.Is(err, kagent.ErrTaskNotFound):
		e.m.Sends.WithLabelValues("planner", "not_found").Inc()
		return e.failPlannerRun(ctx, r, fmt.Sprintf("kagent agent %s/%s not found", ref.Namespace, ref.Name))
	case errors.Is(err, kagent.ErrRejected):
		e.m.Sends.WithLabelValues("planner", "rejected").Inc()
		return e.failPlannerRun(ctx, r, "kagent rejected the planner message")
	default:
		e.m.Sends.WithLabelValues("planner", "ambiguous").Inc()
		e.m.PlannerSessionError.WithLabelValues("send").Inc()
		e.log.WarnContext(ctx, "planner send outcome unknown; reconciling the original message read-only", "run", r.ID, "error", err)
		return nil
	}
}

func (e *Engine) attachPlanner(ctx context.Context, r *plannerRun, t *kagent.Task) error {
	if t == nil || t.ID == "" || t.ContextID != r.ContextID {
		return errors.New("planner task is not in the run's session")
	}
	if err := e.w.Tx(ctx, func(tx *sql.Tx) error {
		_, err := tx.ExecContext(ctx, `UPDATE planner_run SET task_id = $2 WHERE run_id = $1 AND task_id IS NULL`, r.ID, t.ID)
		return err
	}); err != nil {
		return err
	}
	r.TaskID = t.ID
	return nil
}

func (e *Engine) readPlanner(ctx context.Context, r *plannerRun) error {
	if err := e.guard(ctx); err != nil {
		return err
	}
	t, err := e.tasks.GetTask(ctx, e.plannerRef(), r.TaskID)
	if errors.Is(err, kagent.ErrTaskNotFound) {
		return e.failPlannerRun(ctx, r, "planner task is gone")
	}
	if err != nil {
		e.m.PlannerSessionError.WithLabelValues("read").Inc()
		return err
	}
	state := t.Status.State
	switch {
	case state == kagent.StateCompleted || state == kagent.StateInputRequired:
		out := strings.TrimSpace(Output(t))
		if out == "" {
			return e.failPlannerRun(ctx, r, "planner returned no final message")
		}
		return e.endPlannerRun(ctx, r, "success", out, "", t)
	case kagent.Quiescent(state):
		msg := StatusText(t)
		if msg == "" {
			msg = strings.ToLower(strings.TrimPrefix(state, "TASK_STATE_"))
		}
		return e.endPlannerRun(ctx, r, "failed", "", "kagent task "+clip(msg, maxErrorText), t)
	}
	return nil
}

func (e *Engine) failPlannerRun(ctx context.Context, r *plannerRun, reason string) error {
	if r.TaskID != "" {
		// Stop a task that may still run. Its outcome no longer matters.
		if ctx, err := e.plannerCaller(ctx); err == nil {
			if _, err := e.tasks.CancelTask(ctx, e.plannerRef(), r.TaskID); err != nil && !errors.Is(err, kagent.ErrTaskNotFound) {
				e.log.WarnContext(ctx, "planner task cancel failed", "run", r.ID, "error", err)
			}
		}
	}
	return e.endPlannerRun(ctx, r, "failed", "", reason, nil)
}

// endPlannerRun records the run's end once. A completed run stores its final
// message for filing; either end sets the report that tells itsaplan.
// A failed run counts as processed, so finish_planner_run closes it and the
// breaker sees it.
func (e *Engine) endPlannerRun(ctx context.Context, r *plannerRun, status, message, failure string, t *kagent.Task) error {
	var in, out sql.NullInt64
	if r.ContextID != "" {
		if err := e.st.DB.QueryRowContext(ctx, `SELECT input_tokens, output_tokens FROM planner_session_usage($1, $2)`,
			e.cfg.Planner.Agent.Namespace, r.ContextID).Scan(&in, &out); err != nil {
			return err
		}
	}
	if !in.Valid && t != nil {
		// The broker measured nothing: use usage kagent put on the task.
		if i, o, ok := taskUsage(t); ok {
			in, out = sql.NullInt64{Int64: i, Valid: true}, sql.NullInt64{Int64: o, Valid: true}
		}
	}
	var ended bool
	err := e.w.Tx(ctx, func(tx *sql.Tx) error {
		res, err := tx.ExecContext(ctx, `UPDATE planner_run SET report_status = $2, failure = nullif($4, ''),
 final_message = CASE WHEN $2 = 'success' THEN $3 END, processed = ($2 <> 'success'),
 input_tokens = $5, output_tokens = $6, report_after = NULL
 WHERE run_id = $1 AND report_status IS NULL AND finished_at IS NULL`, r.ID, status, message, clip(failure, maxErrorText), in, out)
		if err != nil {
			return err
		}
		n, err := res.RowsAffected()
		ended = n == 1
		return err
	})
	if err != nil {
		return err
	}
	if ended {
		outcome := "completed"
		if status != "success" {
			outcome = "failed"
			e.log.WarnContext(ctx, "planner run failed", "run", r.ID, "schedule", r.Schedule, "reason", failure)
		}
		e.m.PlannerRuns.WithLabelValues(r.Schedule, outcome).Inc()
	}
	return nil
}

func (e *Engine) reportPlannerRun(ctx context.Context, r *plannerRun) error {
	now := e.cfg.Now()
	if r.ReportAfter.Valid && now.Before(r.ReportAfter.Time) {
		return nil
	}
	res := itsaplan.Result{Attempt: r.Attempt, Status: r.ReportStatus.String}
	if res.Status == "success" {
		res.Output = plannerCompleted
	} else {
		res.Error = clip(r.Failure.String, maxErrorText)
	}
	if r.InputTokens.Valid && r.OutputTokens.Valid {
		res.Usage = &itsaplan.Usage{InputTokens: r.InputTokens.Int64, OutputTokens: r.OutputTokens.Int64}
	}
	err := e.its.Result(ctx, e.cfg.Planner.AgentKey, r.ItsRun, res)
	done := err == nil || errors.Is(err, itsaplan.ErrNotFound)
	if !done {
		e.countRefusal(roleAgent, "result", err)
		e.m.PlannerSessionError.WithLabelValues("report").Inc()
		// Give up after the outbox's bound: the run's own record is complete.
		done = r.ReportAttempts+1 >= outboxMaxTries
	}
	if _, werr := e.st.DB.ExecContext(ctx, `UPDATE planner_run SET reported_at = CASE WHEN $2 THEN $3::timestamptz END,
 report_attempts = report_attempts + 1, report_after = $4 WHERE run_id = $1`, r.ID, done, now, now.Add(plannerReportBackoff)); werr != nil {
		return werr
	}
	if err != nil && !done {
		return fmt.Errorf("report planner run %d: %w", r.ItsRun, err)
	}
	return nil
}

// deletePlannerSession removes a finished run's session, which holds a
// snapshot until deleted. A session already gone, or a delete refused too
// many times, ends the attempts: the session's idle expiry removes the rest.
func (e *Engine) deletePlannerSession(ctx context.Context, r *plannerRun) error {
	if r.ContextID == "" || r.Deleted.Valid {
		return nil
	}
	ctx, err := e.plannerCaller(ctx)
	if err != nil {
		return err
	}
	if err := e.guard(ctx); err != nil {
		return err
	}
	_, err = e.sessions.DeleteSession(ctx, r.ContextID)
	done := err == nil || errors.Is(err, kagent.ErrSessionNotFound)
	if !done {
		e.m.PlannerSessionError.WithLabelValues("delete").Inc()
		done = r.DeleteAttempts+1 >= plannerMaxDeletes
	}
	if _, werr := e.st.DB.ExecContext(ctx, `UPDATE planner_run SET session_deleted_at = CASE WHEN $2 THEN now() END,
 delete_attempts = delete_attempts + 1 WHERE run_id = $1`, r.ID, done); werr != nil {
		return werr
	}
	if err != nil && !done {
		return fmt.Errorf("delete planner session: %w", err)
	}
	if done && err == nil {
		e.m.SessionsDeleted.WithLabelValues("planner").Inc()
	}
	return nil
}

// taskUsage sums the usage kagent attaches to each message of a task
// (kagent.UsageMetadataKey). ok is false when no message carries any.
func taskUsage(t *kagent.Task) (in, out int64, ok bool) {
	number := func(m map[string]any, key string) int64 {
		switch v := m[key].(type) {
		case float64:
			if v >= 0 {
				return int64(v)
			}
		case int64:
			if v >= 0 {
				return v
			}
		case int:
			if v >= 0 {
				return int64(v)
			}
		}
		return 0
	}
	for _, m := range t.History {
		if m == nil {
			continue
		}
		usage, _ := m.Metadata[kagent.UsageMetadataKey].(map[string]any)
		if usage == nil {
			continue
		}
		ok = true
		in += number(usage, "inputTokens")
		out += number(usage, "outputTokens")
	}
	return in, out, ok
}

type triagePR struct {
	Issue  string
	Number int
	Head   string
	Review string
	Checks string
}

type triageSnapshot struct {
	Columns map[int64]string
	Labels  map[int64]string
	Board   map[string]json.RawMessage
	State   json.RawMessage
	PRs     []triagePR
}

func (e *Engine) scheduleActions(name string) []string {
	for _, s := range e.cfg.PlannerSchedules {
		if s.Name == name {
			return s.Actions
		}
	}
	return nil
}

func (e *Engine) triageSnapshot(ctx context.Context, message string) (triageSnapshot, error) {
	s := triageSnapshot{Board: map[string]json.RawMessage{}}
	b := e.binding("homelab")
	if b == nil || b.ItsaplanProject != "HOME" {
		return s, errors.New("missing HOME triage binding")
	}
	journal := triageRead{engine: e, message: message}
	project, err := readTriage(ctx, &journal, "project", "HOME", func() (*itsaplan.Project, error) { return e.its.GetProject(ctx, "HOME") })
	if err != nil {
		return s, err
	}
	s.Columns = map[int64]string{}
	for _, c := range project.Columns {
		s.Columns[c.ID] = c.Name
	}
	s.Labels = map[int64]string{}
	for _, l := range project.Labels {
		s.Labels[l.ID] = l.Name
	}
	issues, err := readTriage(ctx, &journal, "board", "HOME", func() ([]itsaplan.Issue, error) { return e.its.ProjectIssues(ctx, "HOME") })
	if err != nil {
		return s, err
	}
	for _, i := range issues {
		detail, err := readTriage(ctx, &journal, "issue", fmt.Sprint(i.ID), func() (*itsaplan.Issue, error) { return e.its.GetIssue(ctx, i.ID) })
		if err != nil {
			return s, err
		}
		raw, err := json.Marshal(map[string]any{"key": i.Identifier, "column": i.ColumnID, "priority": i.Priority, "position": i.Position, "assignee": i.AssigneeUserID, "delegate": i.DelegateUserID, "labels": i.LabelIDs, "links": detail.Links, "title": detail.Title, "description": detail.Description})
		if err != nil {
			return s, err
		}
		s.Board[i.Identifier] = raw
	}
	err = e.st.DB.QueryRowContext(ctx, `WITH work AS (SELECT id,issue_key,state,review_rounds,rebase_rounds,stops,stopped_reason,stopped_by FROM work_item WHERE binding_name='homelab'),
 runs AS (SELECT r.id,r.work_item_id,r.state,r.origin,r.attempt,r.stopped_reason FROM run r JOIN work w ON w.id=r.work_item_id),
 attempts AS (SELECT a.id,a.run_id,a.seq,a.state FROM delivery_attempt a JOIN runs r ON r.id=a.run_id),
 holds AS (SELECT h.id,h.work_item_id,h.state,h.reason FROM recovery_hold h JOIN work w ON w.id=h.work_item_id)
 SELECT json_build_object('work_items',(SELECT json_agg(work) FROM work),'runs',(SELECT json_agg(runs) FROM runs),'attempts',(SELECT json_agg(attempts) FROM attempts),'holds',(SELECT json_agg(holds) FROM holds))`).Scan(&s.State)
	if err != nil {
		return s, err
	}
	rows, err := e.st.DB.QueryContext(ctx, `SELECT issue_key,number,head_sha FROM pull_request JOIN work_item ON work_item.id=work_item_id WHERE binding_name='homelab' ORDER BY number`)
	if err != nil {
		return s, err
	}
	defer rows.Close()
	for rows.Next() {
		var pr triagePR
		if err := rows.Scan(&pr.Issue, &pr.Number, &pr.Head); err != nil {
			return s, err
		}
		s.PRs = append(s.PRs, pr)
	}
	if err := rows.Err(); err != nil {
		return s, err
	}
	if len(s.PRs) > 0 && e.gh == nil {
		return s, errors.New("missing GitHub client for triage checks")
	}
	for i := range s.PRs {
		p := &s.PRs[i]
		target := fmt.Sprintf("%s#%d@%s", b.repo(), p.Number, p.Head)
		if check := b.Review.ReviewerCheck; check != nil {
			checks, err := e.gh.ListCheckRunsWithJournal(ctx, b.repo(), p.Head, github.CheckRunFilter{Name: check.Name, AppID: check.AppID}, journal.pages(ctx, "check", target))
			if err := journal.finish(ctx, err); err != nil {
				return s, err
			}
			var newest int64
			for _, c := range checks {
				if c.Name == check.Name && c.AppID == check.AppID && c.ID > newest {
					newest = c.ID
					p.Review = c.Conclusion
				}
			}
		}
		for _, want := range b.Review.RequiredChecks {
			if want.Name == "pr-checks" && want.StatusCreator != "" {
				state, err := e.gh.StatusFrom(ctx, b.repo(), p.Head, want.Name, want.StatusCreator, journal.pages(ctx, "status", target))
				if err := journal.finish(ctx, err); err != nil {
					return s, err
				}
				p.Checks = state
				break
			}
		}
	}
	return s, nil
}

var errTriageRefusal = errors.New("triage read credential refusal limit")

type triageRead struct {
	engine                     *Engine
	message, operation, target string
	started                    bool
}

func (j *triageRead) begin(ctx context.Context, op, target string) error {
	j.operation, j.target, j.started = op, target, false
	err := j.engine.w.Tx(ctx, func(tx *sql.Tx) error {
		var refusals int
		err := tx.QueryRowContext(ctx, `INSERT INTO triage_read(message_id,operation,target,writer_epoch) VALUES ($1,$2,$3,$4)
 ON CONFLICT(message_id,operation,target) DO UPDATE SET attempts=triage_read.attempts+1,writer_epoch=excluded.writer_epoch RETURNING refusals`, j.message, op, target, j.engine.w.Epoch).Scan(&refusals)
		if err == nil && refusals >= outboxAuthMaxTries {
			return errTriageRefusal
		}
		return err
	})
	if err != nil {
		return err
	}
	if err := j.engine.guard(ctx); err != nil {
		return err
	}
	j.started = true
	return nil
}

func (j *triageRead) pages(ctx context.Context, op, target string) func(int) error {
	j.started = false
	return func(page int) error { return j.begin(ctx, op, fmt.Sprintf("%s/page/%d", target, page)) }
}

func (j *triageRead) finish(ctx context.Context, result error) error {
	if !j.started {
		return result
	}
	var gh *github.HTTPError
	refused := j.engine.countRefusal(roleCoordinator, "triage_"+j.operation, result) || (errors.As(result, &gh) && (gh.Status == 401 || gh.Status == 403))
	var streak int
	err := j.engine.w.Tx(ctx, func(tx *sql.Tx) error {
		return tx.QueryRowContext(ctx, `UPDATE triage_read SET refusals=CASE WHEN $4 THEN refusals+1 ELSE 0 END WHERE message_id=$1 AND operation=$2 AND target=$3 RETURNING refusals`, j.message, j.operation, j.target, refused).Scan(&streak)
	})
	if err != nil {
		return err
	}
	if streak >= outboxAuthMaxTries {
		return errTriageRefusal
	}
	return result
}

func readTriage[T any](ctx context.Context, j *triageRead, op, target string, read func() (T, error)) (T, error) {
	if err := j.begin(ctx, op, target); err != nil {
		var zero T
		return zero, err
	}
	value, err := read()
	return value, j.finish(ctx, err)
}
