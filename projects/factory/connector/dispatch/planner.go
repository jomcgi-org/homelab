// invariant: FACT-14
package dispatch

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"errors"
	"fmt"
	"strings"

	"github.com/jomcgi/homelab/projects/factory/connector/itsaplan"
	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
	"github.com/jomcgi/homelab/projects/factory/connector/planner"
	"github.com/jomcgi/homelab/projects/factory/connector/store"
)

// Consume each final message once in the same transaction as its reservations.
// Only the schedule supplies the title and instructions.
func (e *Engine) preparePlannerResult(ctx context.Context) error {
	return e.w.Tx(ctx, func(tx *sql.Tx) error {
		var run, message, namespace, title, instructions string
		err := tx.QueryRowContext(ctx, `SELECT r.run_id,r.final_message,s.namespace,s.issue_title,s.instructions
 FROM planner_run r JOIN planner_schedule s ON s.name=r.schedule
 WHERE r.final_message IS NOT NULL AND NOT processed AND finished_at IS NULL
 AND s.issue_title <> '' AND s.instructions <> '' ORDER BY started_at LIMIT 1 FOR UPDATE`).Scan(&run, &message, &namespace, &title, &instructions)
		if errors.Is(err, sql.ErrNoRows) {
			return nil
		}
		if err != nil {
			return err
		}
		items, dropped, parseErr := planner.Parse([]byte(message), namespace)
		if parseErr != nil {
			dropped++
		}
		for _, p := range items {
			description, fits := p.Description(instructions)
			if !fits {
				dropped++
				continue
			}
			var route sql.NullString
			if err := tx.QueryRowContext(ctx, "SELECT reserve_planner_proposal($1,$2,$3)", run, p.Fingerprint, p.Fix).Scan(&route); err != nil {
				return err
			}
			if !route.Valid {
				continue
			}
			_, err := tx.ExecContext(ctx, `UPDATE planner_proposal SET title=$3,description=$4,priority=$5 WHERE run_id=$1 AND fingerprint=$2`, run, p.Fingerprint, title, description, p.Priority)
			if err != nil {
				return err
			}
		}
		_, err = tx.ExecContext(ctx, `UPDATE planner_run SET final_message=NULL,processed=true,dropped=dropped+$2 WHERE run_id=$1`, run, dropped)
		return err
	})
}

// File at most one issue per tick after coding admission. A committed send
// intent permits only reconciliation on later ticks, even after a lost POST.
func (e *Engine) stepPlanner(ctx context.Context) error {
	if err := e.preparePlannerResult(ctx); err != nil {
		return err
	}
	var run, fp, route, title, description, priority string
	var started bool
	err := e.st.DB.QueryRowContext(ctx, `SELECT run_id,fingerprint,disposition,title,description,priority,create_started
 FROM planner_proposal WHERE title IS NOT NULL AND issue_key IS NULL AND closed_at IS NULL AND (NOT filing_stopped OR create_started) AND next_attempt<=now()
 ORDER BY next_attempt,run_id,fingerprint LIMIT 1`).Scan(&run, &fp, &route, &title, &description, &priority, &started)
	if errors.Is(err, sql.ErrNoRows) {
		return e.w.Tx(ctx, func(tx *sql.Tx) error {
			_, err := tx.ExecContext(ctx, `SELECT finish_planner_run(run_id) FROM planner_run r WHERE processed AND finished_at IS NULL AND NOT EXISTS
 (SELECT FROM planner_proposal p WHERE p.run_id=r.run_id AND issue_key IS NULL AND closed_at IS NULL AND NOT filing_stopped)`)
			return err
		})
	}
	if err != nil {
		return err
	}
	marker := fmt.Sprintf("factory-planner-%x", sha256.Sum256([]byte(run+"\x00"+fp)))
	description += "\n\n<!-- " + marker + " -->"
	b := e.binding("homelab")
	if b == nil || b.ItsaplanProject != "HOME" {
		return errors.New("missing HOME planner binding")
	}
	if err = e.w.Tx(ctx, func(tx *sql.Tx) error {
		_, err := tx.ExecContext(ctx, `UPDATE planner_proposal SET next_attempt=now()+CASE WHEN filing_stopped THEN interval '1 hour' ELSE interval '1 minute' END WHERE run_id=$1 AND fingerprint=$2`, run, fp)
		return err
	}); err != nil {
		return err
	}
	call := plannerTrackerCall{engine: e, run: run, fingerprint: fp}
	var filed *itsaplan.Issue
	if started {
		if err := call.begin(ctx, "SearchIssues", marker); err != nil {
			return err
		}
		hits, err := e.its.SearchIssues(ctx, b.ItsaplanProject, marker)
		err = call.result(ctx, err)
		if err != nil {
			return err
		}
		for _, hit := range hits {
			if err := call.begin(ctx, "GetIssue", fmt.Sprint(hit.ID)); err != nil {
				return err
			}
			is, err := e.its.GetIssue(ctx, hit.ID)
			err = call.result(ctx, err)
			if err != nil {
				return err
			}
			if inProject(is, b.ItsaplanProject) && is.Title == title && is.Description == description {
				if filed != nil {
					return errors.New("ambiguous planner receipt")
				}
				filed = is
			}
		}
		if filed == nil {
			// The create may have failed before itsaplan committed it. After
			// bounded empty lookups, release the run and its schedule. The
			// fence and open fingerprint stay, so hourly lookups continue.
			return e.w.Tx(ctx, func(tx *sql.Tx) error {
				_, err := tx.ExecContext(ctx, `UPDATE planner_proposal p SET filing_stopped=true FROM planner_tracker_attempt a
 WHERE p.run_id=$1 AND p.fingerprint=$2 AND NOT p.filing_stopped AND a.run_id=p.run_id AND a.fingerprint=p.fingerprint
 AND a.operation='SearchIssues' AND a.resource=$3 AND a.attempts>=$4`, run, fp, marker, outboxAuthMaxTries)
				return err
			})
		}
	} else {
		if err := call.begin(ctx, "GetProject", b.ItsaplanProject); err != nil {
			return err
		}
		project, err := e.its.GetProject(ctx, b.ItsaplanProject)
		err = call.result(ctx, err)
		if err != nil {
			return err
		}
		req := itsaplan.NewIssue{Title: title, Description: description, Priority: priority, ColumnID: columnNamed(project, "Backlog")}
		if route != "backlog" {
			req.ColumnID = columnNamed(project, "Todo")
			for _, a := range project.Assignees {
				if route == "joe" && a.Kind == "member" && strings.EqualFold(a.Username, b.HumanGateAssignee) {
					req.AssigneeUserID = a.UserID
				}
				if route == "delegate" && a.Kind == "agent" && a.Username == "homelab-codex" {
					req.DelegateUserID = a.UserID
				}
			}
			if req.AssigneeUserID == "" && req.DelegateUserID == "" {
				return errors.New("planner disposition has no recipient")
			}
		}
		if req.ColumnID <= 0 {
			return errors.New("planner disposition has no column")
		}
		if err := call.begin(ctx, "CreateIssue", b.ItsaplanProject); err != nil {
			return err
		}
		filed, err = e.its.CreateIssue(ctx, b.ItsaplanProject, req)
		err = call.result(ctx, err)
		if err != nil {
			return err
		}
	}
	return e.w.Tx(ctx, func(tx *sql.Tx) error {
		_, err := tx.ExecContext(ctx, `UPDATE planner_proposal SET issue_key=$3,filing_stopped=false WHERE run_id=$1 AND fingerprint=$2 AND issue_key IS NULL`, run, fp, filed.Identifier)
		return err
	})
}

type plannerTrackerCall struct {
	engine                                *Engine
	run, fingerprint, operation, resource string
}

func (c *plannerTrackerCall) begin(ctx context.Context, operation, resource string) error {
	c.operation, c.resource = operation, resource
	err := c.engine.w.Tx(ctx, func(tx *sql.Tx) error {
		if operation == "CreateIssue" {
			res, err := tx.ExecContext(ctx, `UPDATE planner_proposal SET create_started=true WHERE run_id=$1 AND fingerprint=$2 AND NOT create_started AND NOT filing_stopped AND closed_at IS NULL AND issue_key IS NULL`, c.run, c.fingerprint)
			if err != nil {
				return err
			}
			n, err := res.RowsAffected()
			if err != nil {
				return err
			}
			if n != 1 {
				return errors.New("planner send already claimed")
			}
		}
		_, err := tx.ExecContext(ctx, `INSERT INTO planner_tracker_attempt(run_id,fingerprint,operation,resource,writer_epoch) VALUES($1,$2,$3,$4,$5)
 ON CONFLICT(run_id,fingerprint,operation,resource) DO UPDATE SET writer_epoch=excluded.writer_epoch,last_attempt=now(),attempts=planner_tracker_attempt.attempts+1`, c.run, c.fingerprint, operation, resource, c.engine.w.Epoch)
		return err
	})
	if err != nil {
		return err
	}
	return c.engine.guard(ctx)
}

func (c *plannerTrackerCall) result(ctx context.Context, result error) error {
	refused := c.engine.countRefusal(roleCoordinator, "planner_"+c.operation, result)
	err := c.engine.w.Tx(ctx, func(tx *sql.Tx) error {
		var streak, attempts int
		err := tx.QueryRowContext(ctx, `UPDATE planner_tracker_attempt SET refusals=CASE WHEN $5 THEN refusals+1 ELSE 0 END
 WHERE run_id=$1 AND fingerprint=$2 AND operation=$3 AND resource=$4 RETURNING refusals, attempts`, c.run, c.fingerprint, c.operation, c.resource, refused).Scan(&streak, &attempts)
		if err != nil {
			return err
		}
		var status *itsaplan.HTTPError
		throttled := errors.As(result, &status) && status.Status == 429
		rejected := c.operation == "CreateIssue" && (refused || terminalHTTP(result) || throttled || errors.Is(result, itsaplan.ErrWIPLimit))
		retryable := refused || throttled || errors.Is(result, itsaplan.ErrWIPLimit)
		stop := streak >= outboxAuthMaxTries || (rejected && (!retryable || attempts >= outboxAuthMaxTries))
		if !rejected && !stop {
			return nil
		}
		_, err = tx.ExecContext(ctx, `UPDATE planner_proposal SET create_started=create_started AND NOT $3, filing_stopped=$4,
 next_attempt=CASE WHEN $4 AND create_started AND NOT $3 THEN now()+interval '1 hour' ELSE next_attempt END,
 closed_at=CASE WHEN $4 AND (NOT create_started OR $3) THEN now() ELSE closed_at END
 WHERE run_id=$1 AND fingerprint=$2`, c.run, c.fingerprint, rejected, stop)
		return err
	})
	if err != nil {
		return err
	}
	return result
}

// The cron prompt is the exact reviewed schedule name. Admission commits the
// single send intent. On takeover, recover by message ID without resending.
func (e *Engine) plannerSession(ctx context.Context, r *store.Run) error {
	b := e.binding("homelab")
	if r.Trigger != "schedule" || r.IssueID != 0 || b == nil || b.ItsaplanProject != "HOME" {
		return e.failRun(ctx, e.st.DB, r, "Expected a HOME planner schedule")
	}
	id := fmt.Sprintf("planner-%d", r.ID)
	ref := kagent.AgentRef{Name: "homelab-planner", Namespace: b.KagentNamespace + "-planner"}
	var task *kagent.Task
	if e.cfg.Caller == nil {
		return errCallerUncertain
	}
	ctx, identity, err := e.cfg.Caller.Pin(ctx)
	if err != nil || !identity.Valid() {
		return callerUncertain(err)
	}
	read := func(ctx context.Context, op string) (context.Context, error) {
		key := id + "/" + op
		return kagent.WithRequestID(ctx, key), e.BeginStatusRead(ctx, key)
	}
	var sessionID, taskID string
	if r.State == store.RunClaimed {
		var admitted bool
		var namespace, title, instructions string
		err = e.w.Tx(ctx, func(tx *sql.Tx) error {
			if err := tx.QueryRowContext(ctx, "SELECT admit_planner_run($1,$2)", r.Prompt, id).Scan(&admitted); err != nil {
				return err
			}
			if !admitted {
				return e.failRun(ctx, tx, r, "Planner admission refused")
			}
			if err := tx.QueryRowContext(ctx, "SELECT namespace,issue_title,instructions FROM planner_schedule WHERE name=$1", r.Prompt).Scan(&namespace, &title, &instructions); err != nil {
				return err
			}
			if _, err := tx.ExecContext(ctx, "UPDATE planner_run SET caller_identity=$2 WHERE run_id=$1", id, identity.Key()); err != nil {
				return err
			}
			return e.st.SetRunState(ctx, tx, r.ID, store.RunDispatched, "")
		})
		if err != nil || !admitted {
			return err
		}
		if err = e.guard(ctx); err != nil {
			return err
		}
		prompt := fmt.Sprintf("Inspect %s/%s for %s. %s Return a JSON array of proposals with title, body, evidence (1-8 HTTPS URLs), priority (low, medium, high or urgent), fix (agent or human) and fingerprint (%s:<component>:<finding>). Return [] for no findings.", b.Owner, b.Repo, title, instructions, namespace)
		task, err = e.tasks.Send(ctx, ref, kagent.Message{MessageID: id, Parts: []kagent.Part{{Text: prompt}}})
	} else {
		var owner string
		if err := e.st.DB.QueryRowContext(ctx, "SELECT caller_identity,coalesce(session_id,''),coalesce(task_id,'') FROM planner_run WHERE run_id=$1", id).Scan(&owner, &sessionID, &taskID); err != nil {
			return err
		}
		if owner != identity.Key() {
			return errCallerUncertain
		}
		if taskID == "" {
			task, err = e.findMessage(ctx, ref, "", id, read)
		} else {
			readCtx, errRead := read(ctx, "task/"+taskID)
			if errRead != nil {
				return errRead
			}
			task, err = e.tasks.GetTask(readCtx, ref, taskID)
			if err == nil && (task == nil || task.ID != taskID || task.ContextID != sessionID) {
				return errors.New("planner task identity mismatch")
			}
		}
	}
	if r.State == store.RunClaimed && err != nil && (errors.Is(err, kagent.ErrRejected) || errors.Is(err, kagent.ErrNotAccepted)) {
		return e.failPlannerSession(ctx, r, id, "Planner dispatch refused")
	}
	if err == nil && task == nil {
		var expired bool
		if err := e.st.DB.QueryRowContext(ctx, "SELECT started_at < now() - $2 * interval '1 second' FROM planner_run WHERE run_id=$1", id, e.cfg.DispatchWindow.Seconds()).Scan(&expired); err != nil {
			return err
		}
		if expired {
			return e.failPlannerSession(ctx, r, id, "Planner dispatch was not accepted")
		}
		return nil
	}
	if err == nil && taskID == "" {
		if task.ID == "" || task.ContextID == "" {
			return errors.New("missing planner task identity")
		}
		err = e.w.Tx(ctx, func(tx *sql.Tx) error {
			_, err := tx.ExecContext(ctx, "UPDATE planner_run SET task_id=$2,session_id=$3 WHERE run_id=$1", id, task.ID, task.ContextID)
			return err
		})
	}
	if err == nil && r.State == store.RunCanceling {
		if !kagent.Terminal(task.Status.State) {
			requestID := id + "/cancel"
			if err := e.w.Tx(ctx, func(tx *sql.Tx) error {
				_, err := tx.ExecContext(ctx, "UPDATE planner_run SET cancel_request_id=$2,cancel_writer_epoch=$3,cancel_attempts=cancel_attempts+1 WHERE run_id=$1", id, requestID, e.w.Epoch)
				return err
			}); err != nil {
				return err
			}
			if err := e.guard(ctx); err != nil {
				return err
			}
			_, err = e.tasks.CancelTask(kagent.WithRequestID(ctx, requestID), ref, task.ID)
			return err
		}
		return e.failPlannerSession(ctx, r, id, "Planner run stopped")
	}
	if err != nil || !kagent.Quiescent(task.Status.State) {
		return err
	}
	if task.Status.State != kagent.StateCompleted {
		return e.failPlannerSession(ctx, r, id, "Planner task "+task.Status.State)
	}
	return e.w.Tx(ctx, func(tx *sql.Tx) error {
		if _, err := tx.ExecContext(ctx, "UPDATE planner_run SET final_message=$2 WHERE run_id=$1", id, plannerFinalMessage(task)); err != nil {
			return err
		}
		return e.finishRun(ctx, tx, r, itsaplan.Result{Status: "success"})
	})
}

func (e *Engine) failPlannerSession(ctx context.Context, r *store.Run, id, reason string) error {
	return e.w.Tx(ctx, func(tx *sql.Tx) error {
		if _, err := tx.ExecContext(ctx, "UPDATE planner_run SET processed=true WHERE run_id=$1", id); err != nil {
			return err
		}
		if r.State == store.RunCanceling {
			if _, err := tx.ExecContext(ctx, "SELECT finish_planner_run($1)", id); err != nil {
				return err
			}
		}
		if r.State == store.RunCanceling && !r.StopRequested {
			if err := abandonResult(ctx, tx, r.ID); err != nil {
				return err
			}
			return e.st.SetRunState(ctx, tx, r.ID, store.RunClosed, reason)
		}
		return e.failRun(ctx, tx, r, reason)
	})
}

func plannerFinalMessage(task *kagent.Task) string {
	if message := task.Status.Message; message != nil && message.Role != "ROLE_USER" {
		if text := messageText(message); text != "" {
			return itsaplanText(text)
		}
	}
	for i := len(task.History) - 1; i >= 0; i-- {
		if message := task.History[i]; message != nil && message.Role == "ROLE_AGENT" {
			return itsaplanText(messageText(message))
		}
	}
	return ""
}
