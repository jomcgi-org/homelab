package dispatch

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"errors"
	"fmt"
	"strconv"
	"strings"

	"github.com/jomcgi/homelab/projects/factory/connector/itsaplan"
	"github.com/jomcgi/homelab/projects/factory/connector/planner"
)

func fingerprintMarker(fp string) string {
	return fmt.Sprintf("<!-- factory-planner-fingerprint-%x -->", sha256.Sum256([]byte(fp)))
}

func (e *Engine) plannerDuplicate(ctx context.Context, call *plannerTrackerCall, fp string) (bool, error) {
	marker := fingerprintMarker(fp)
	hits, err := plannerRead(ctx, call, "LookupSearch", marker, func() ([]itsaplan.Issue, error) {
		return e.its.SearchIssues(ctx, "HOME", strings.TrimSuffix(strings.TrimPrefix(marker, "<!-- "), " -->"))
	})
	if err != nil || len(hits) == 0 {
		return false, err
	}
	project, err := plannerRead(ctx, call, "LookupProject", "HOME", func() (*itsaplan.Project, error) { return e.its.GetProject(ctx, "HOME") })
	if err != nil {
		return false, err
	}
	for _, hit := range hits {
		is, err := plannerRead(ctx, call, "LookupIssue", fmt.Sprint(hit.ID), func() (*itsaplan.Issue, error) { return e.its.GetIssue(ctx, hit.ID) })
		if err != nil {
			return false, err
		}
		if !inProject(is, "HOME") || is.ArchivedAt != nil || !strings.Contains("\n"+is.Description+"\n", "\n"+fingerprintMarker(fp)+"\n") {
			continue
		}
		for _, c := range project.Columns {
			if c.ID == is.ColumnID && c.StateType != "completed" && c.StateType != "canceled" {
				return true, nil
			}
		}
	}
	return false, nil
}

// Consume each final message once in the same transaction as its reservations.
// Only the schedule supplies the title and instructions.
func (e *Engine) preparePlannerResult(ctx context.Context) error {
	var run, message, namespace, title, instructions string
	err := e.st.DB.QueryRowContext(ctx, `SELECT r.run_id,r.final_message,s.namespace,s.issue_title,s.instructions
 FROM planner_run r JOIN planner_schedule s ON s.name=r.schedule
 WHERE lookup_after<=now() AND r.final_message IS NOT NULL AND NOT processed AND finished_at IS NULL
 AND s.issue_title <> '' AND s.instructions <> '' ORDER BY started_at LIMIT 1`).Scan(&run, &message, &namespace, &title, &instructions)
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
	duplicates := make(map[string]bool, len(items))
	for _, p := range items {
		var localDuplicate, stopped bool
		err := e.st.DB.QueryRowContext(ctx, `SELECT
 EXISTS(SELECT FROM planner_existing_issue WHERE fingerprint=$2) OR
 EXISTS(SELECT FROM planner_proposal p WHERE p.fingerprint=$2 AND
        (p.closed_at IS NULL OR p.closed_at>now()-s.cooldown OR p.run_id=r.run_id)),
 EXISTS(SELECT FROM planner_tracker_attempt WHERE run_id=$1 AND fingerprint=$2 AND operation LIKE 'Lookup%' AND refusals>=$3)
 FROM planner_run r JOIN planner_schedule s ON s.name=r.schedule WHERE r.run_id=$1`,
			run, p.Fingerprint, outboxAuthMaxTries).Scan(&localDuplicate, &stopped)
		if err != nil {
			return err
		}
		if localDuplicate {
			duplicates[p.Fingerprint] = true
			continue
		}
		if stopped {
			continue
		}
		call := plannerTrackerCall{engine: e, run: run, fingerprint: p.Fingerprint}
		duplicate, err := e.plannerDuplicate(ctx, &call, p.Fingerprint)
		if err != nil {
			return err
		}
		duplicates[p.Fingerprint] = duplicate
	}
	return e.w.Tx(ctx, func(tx *sql.Tx) error {
		var claimed string
		err := tx.QueryRowContext(ctx, `UPDATE planner_run SET processed=true WHERE run_id=$1 AND NOT processed AND finished_at IS NULL AND final_message=$2 RETURNING run_id`, run, message).Scan(&claimed)
		if errors.Is(err, sql.ErrNoRows) {
			return nil
		}
		if err != nil {
			return err
		}
		for _, p := range items {
			duplicate, checked := duplicates[p.Fingerprint]
			if !checked {
				dropped++
				continue
			}
			if duplicate {
				if _, err := tx.ExecContext(ctx, "UPDATE planner_run SET deduplicated=deduplicated+1 WHERE run_id=$1", run); err != nil {
					return err
				}
				continue
			}
			var route sql.NullString
			if err := tx.QueryRowContext(ctx, "SELECT reserve_planner_proposal($1,$2,$3)", run, p.Fingerprint, p.Fix).Scan(&route); err != nil {
				return err
			}
			if !route.Valid {
				continue
			}
			description, fits := p.Description(instructions)
			if !fits {
				// Release this new reservation in the same transaction. Existing
				// fingerprints have already been counted by the reservation function.
				if _, err := tx.ExecContext(ctx, "DELETE FROM planner_proposal WHERE run_id=$1 AND fingerprint=$2", run, p.Fingerprint); err != nil {
					return err
				}
				dropped++
				continue
			}
			description += "\n\n" + fingerprintMarker(p.Fingerprint)
			_, err = tx.ExecContext(ctx, `UPDATE planner_proposal SET title=$3,description=$4,priority=$5 WHERE run_id=$1 AND fingerprint=$2`, run, p.Fingerprint, title, description, p.Priority)
			if err != nil {
				return err
			}
		}
		_, err = tx.ExecContext(ctx, `UPDATE planner_run SET final_message=NULL,dropped=dropped+$2 WHERE run_id=$1`, run, dropped)
		return err
	})
}

// File at most one issue per tick after coding admission. A committed send
// intent permits only reconciliation on later ticks, even after a lost POST.
func (e *Engine) stepPlanner(ctx context.Context) error {
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

func plannerRead[T any](ctx context.Context, call *plannerTrackerCall, operation, resource string, read func() (T, error)) (T, error) {
	if err := call.begin(ctx, operation, resource); err != nil {
		var zero T
		return zero, err
	}
	value, err := read()
	return value, call.result(ctx, err)
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
		if strings.HasPrefix(c.operation, "Lookup") {
			if result != nil {
				_, err = tx.ExecContext(ctx, "UPDATE planner_run SET lookup_after=now()+interval '1 minute' WHERE run_id=$1", c.run)
			}
			return err
		}
		if strings.HasPrefix(c.operation, "Outcome") {
			if streak >= outboxAuthMaxTries {
				_, err = tx.ExecContext(ctx, "UPDATE planner_proposal SET next_attempt=now()+interval '1 hour' WHERE run_id=$1 AND fingerprint=$2", c.run, c.fingerprint)
			}
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

func (e *Engine) syncPlannerOutcome(ctx context.Context) error {
	call := plannerTrackerCall{engine: e}
	var key, route string
	err := e.w.Tx(ctx, func(tx *sql.Tx) error {
		return tx.QueryRowContext(ctx, `UPDATE planner_proposal SET next_attempt=now()+interval '5 minutes'
 WHERE issue_key=(SELECT issue_key FROM planner_proposal JOIN planner_run USING(run_id)
 WHERE issue_key IS NOT NULL AND finished_at IS NOT NULL AND NOT reproducible AND next_attempt<=now()
 ORDER BY next_attempt,issue_key LIMIT 1) RETURNING issue_key,disposition,run_id,fingerprint`).Scan(&key, &route, &call.run, &call.fingerprint)
	})
	if errors.Is(err, sql.ErrNoRows) {
		return nil
	}
	if err != nil {
		return err
	}
	n, err := strconv.ParseInt(strings.TrimPrefix(key, "HOME-"), 10, 64)
	if err != nil {
		return err
	}
	is, err := plannerRead(ctx, &call, "OutcomeIssue", key, func() (*itsaplan.Issue, error) { return e.its.GetIssueByNumber(ctx, "HOME", n) })
	if err != nil {
		return err
	}
	project, err := plannerRead(ctx, &call, "OutcomeProject", "HOME", func() (*itsaplan.Project, error) { return e.its.GetProject(ctx, "HOME") })
	if err != nil {
		return err
	}
	done := is.ColumnID > 0 && is.ColumnID == columnNamed(project, "Done")
	if !done {
		if route != "delegate" {
			return nil
		}
		rejected, err := e.plannerDelegationRejected(ctx, &call, is.ID)
		if err != nil || !rejected {
			return err
		}
	}
	return e.w.Tx(ctx, func(tx *sql.Tx) error {
		if done {
			if _, err := tx.ExecContext(ctx, "SELECT mark_planner_reproducible($1)", key); err != nil {
				return err
			}
		}
		_, err := tx.ExecContext(ctx, "UPDATE planner_proposal SET closed_at=now() WHERE issue_key=$1 AND closed_at IS NULL", key)
		return err
	})
}

func (e *Engine) plannerDelegationRejected(ctx context.Context, call *plannerTrackerCall, id int64) (bool, error) {
	var before int64
	for {
		page, err := plannerRead(ctx, call, "OutcomeRuns", fmt.Sprintf("%d/%d", id, before), func() (*itsaplan.IssueRunPage, error) { return e.its.IssueRuns(ctx, id, before) })
		if err != nil {
			return false, err
		}
		for _, r := range page.Items {
			if r.Username == "homelab-codex" && r.Trigger == "delegation" {
				return r.Status == "failed" || r.Status == "canceled", nil
			}
		}
		if page.NextCursor == nil {
			return false, nil
		}
		if *page.NextCursor <= 0 || (before != 0 && *page.NextCursor >= before) {
			return false, errors.New("invalid planner run cursor")
		}
		before = *page.NextCursor
	}
}
