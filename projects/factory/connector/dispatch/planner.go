package dispatch

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/itsaplan"
	"github.com/jomcgi/homelab/projects/factory/connector/planner"
)

// Consume each final message once in the same transaction as its reservations.
// Only the schedule supplies the title and instructions. The tracker lookup
// precedes the transaction: it removes a proposal whose fingerprint an open
// issue already covers, and counts it as deduplicated.
func (e *Engine) preparePlannerResult(ctx context.Context) error {
	var run, message, namespace string
	err := e.st.DB.QueryRowContext(ctx, `SELECT r.run_id,r.final_message,s.namespace
 FROM planner_run r JOIN planner_schedule s ON s.name=r.schedule
 WHERE r.final_message IS NOT NULL AND NOT processed AND finished_at IS NULL
 AND s.issue_title <> '' AND s.instructions <> '' ORDER BY started_at LIMIT 1`).Scan(&run, &message, &namespace)
	if errors.Is(err, sql.ErrNoRows) {
		return nil
	}
	if err != nil {
		return err
	}
	items, _, _ := planner.Parse([]byte(message), namespace)
	covered, err := e.plannerCovered(ctx, items)
	if err != nil {
		return err
	}
	return e.w.Tx(ctx, func(tx *sql.Tx) error {
		var title, instructions string
		err := tx.QueryRowContext(ctx, `SELECT r.final_message,s.issue_title,s.instructions
 FROM planner_run r JOIN planner_schedule s ON s.name=r.schedule
 WHERE r.run_id=$1 AND r.final_message IS NOT NULL AND NOT processed AND finished_at IS NULL FOR UPDATE OF r`, run).Scan(&message, &title, &instructions)
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
		deduplicated := 0
		for _, p := range items {
			if covered[p.Fingerprint] {
				deduplicated++
				continue
			}
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
		_, err = tx.ExecContext(ctx, `UPDATE planner_run SET final_message=NULL,processed=true,dropped=dropped+$2,deduplicated=deduplicated+$3 WHERE run_id=$1`, run, dropped, deduplicated)
		return err
	})
}

// plannerCovered returns the fingerprints an open tracker issue already
// covers: an issue that carries the fingerprint's marker, or an open issue
// that reviewed configuration assigns the fingerprint to (HOME-4 to HOME-11).
// Closed and rejected proposals are covered by the cooldown in Postgres.
func (e *Engine) plannerCovered(ctx context.Context, items []planner.Proposal) (map[string]bool, error) {
	covered := map[string]bool{}
	if len(items) == 0 {
		return covered, nil
	}
	b := e.binding("homelab")
	if b == nil || b.ItsaplanProject != "HOME" {
		return nil, errors.New("missing HOME planner binding")
	}
	if err := e.guard(ctx); err != nil {
		return nil, err
	}
	project, err := e.its.GetProject(ctx, b.ItsaplanProject)
	if err != nil {
		return nil, err
	}
	if err := e.guard(ctx); err != nil {
		return nil, err
	}
	issues, err := e.its.ProjectIssues(ctx, b.ItsaplanProject)
	if err != nil {
		return nil, err
	}
	closed := map[int64]bool{columnNamed(project, "Done"): true, columnNamed(project, "Canceled"): true}
	tracked := map[string]string{}
	for _, t := range e.cfg.PlannerTracked {
		tracked[t.Fingerprint] = t.Issue
	}
	for _, is := range issues {
		if is.ArchivedAt != nil || closed[is.ColumnID] {
			continue
		}
		for _, p := range items {
			if tracked[p.Fingerprint] == is.Identifier || strings.Contains(is.Description, planner.FingerprintMarker(p.Fingerprint)) {
				covered[p.Fingerprint] = true
			}
		}
	}
	return covered, nil
}

// File at most one issue per tick after coding admission. A committed send
// intent permits only reconciliation on later ticks, even after a lost POST.
func (e *Engine) stepPlanner(ctx context.Context) error {
	if err := e.preparePlannerResult(ctx); err != nil {
		return err
	}
	if err := e.syncPlannerOutcomes(ctx); err != nil {
		if fatal(err) {
			return err
		}
		e.log.WarnContext(ctx, "planner outcome sync failed", "error", err)
	}
	if err := e.observePlannerBreakers(ctx); err != nil {
		return err
	}
	var run, fp, route, title, description, priority string
	var started bool
	err := e.st.DB.QueryRowContext(ctx, `SELECT run_id,fingerprint,disposition,title,description,priority,create_started
 FROM planner_proposal WHERE title IS NOT NULL AND issue_key IS NULL AND closed_at IS NULL AND (NOT filing_stopped OR create_started) AND next_attempt<=now()
 ORDER BY next_attempt,run_id,fingerprint LIMIT 1`).Scan(&run, &fp, &route, &title, &description, &priority, &started)
	if errors.Is(err, sql.ErrNoRows) {
		return e.finishPlannerRuns(ctx)
	}
	if err != nil {
		return err
	}
	marker := fmt.Sprintf("factory-planner-%x", sha256.Sum256([]byte(run+"\x00"+fp)))
	// Both lines follow the evidence block. The fingerprint line lets a later
	// proposal find this issue; the hash line finds it after a lost create.
	description += "\n\n" + planner.FingerprintMarker(fp) + "\n<!-- " + marker + " -->"
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
			}
			if route == "delegate" {
				req.DelegateUserID = coderDelegate(project.Assignees)
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
		_, err := tx.ExecContext(ctx, `UPDATE planner_proposal SET issue_key=$3,issue_id=$4,filing_stopped=false WHERE run_id=$1 AND fingerprint=$2 AND issue_key IS NULL`, run, fp, filed.Identifier, filed.ID)
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

// finishPlannerRuns closes each processed run whose proposals are all filed or
// closed. Postgres counts the run once; the connector adds its metrics only
// then, so a retry after a lost response counts nothing twice.
func (e *Engine) finishPlannerRuns(ctx context.Context) error {
	rows, err := e.st.DB.QueryContext(ctx, `SELECT run_id FROM planner_run r WHERE processed AND finished_at IS NULL AND NOT EXISTS
 (SELECT FROM planner_proposal p WHERE p.run_id=r.run_id AND issue_key IS NULL AND closed_at IS NULL AND NOT filing_stopped)`)
	if err != nil {
		return err
	}
	var runs []string
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			rows.Close()
			return err
		}
		runs = append(runs, id)
	}
	if err := rows.Close(); err != nil {
		return err
	}
	if err := rows.Err(); err != nil {
		return err
	}
	for _, id := range runs {
		var counts plannerCounts
		err := e.w.Tx(ctx, func(tx *sql.Tx) error {
			var first bool
			if err := tx.QueryRowContext(ctx, `SELECT finish_planner_run($1)`, id).Scan(&first); err != nil || !first {
				return err
			}
			counts.first = true
			return tx.QueryRowContext(ctx, `SELECT r.schedule, r.dropped, r.deduplicated, r.input_tokens, r.output_tokens,
 count(p.*), count(p.*) FILTER (WHERE p.disposition='delegate'), count(p.*) FILTER (WHERE p.disposition='joe'),
 count(p.*) FILTER (WHERE p.disposition='backlog')
 FROM planner_run r LEFT JOIN planner_proposal p USING (run_id) WHERE r.run_id=$1 GROUP BY r.run_id`, id).Scan(
				&counts.schedule, &counts.dropped, &counts.deduplicated, &counts.in, &counts.out,
				&counts.created, &counts.delegated, &counts.joe, &counts.backlog)
		})
		if err != nil {
			return err
		}
		if counts.first {
			e.observePlannerRun(counts)
		}
	}
	return nil
}

type plannerCounts struct {
	first                                                   bool
	schedule                                                string
	created, delegated, joe, backlog, dropped, deduplicated int
	in, out                                                 sql.NullInt64
}

func (e *Engine) observePlannerRun(c plannerCounts) {
	for outcome, n := range map[string]int{
		"created": c.created, "delegated": c.delegated, "joe": c.joe,
		"backlog": c.backlog, "dropped": c.dropped, "deduplicated": c.deduplicated,
	} {
		e.m.PlannerProposals.WithLabelValues(c.schedule, outcome).Add(float64(n))
		e.m.PlannerLastRun.WithLabelValues(c.schedule, outcome).Set(float64(n))
	}
	if c.in.Valid {
		e.m.PlannerTokens.WithLabelValues(c.schedule, "input").Add(float64(c.in.Int64))
		e.m.PlannerLastTokens.WithLabelValues(c.schedule, "input").Set(float64(c.in.Int64))
	}
	if c.out.Valid {
		e.m.PlannerTokens.WithLabelValues(c.schedule, "output").Add(float64(c.out.Int64))
		e.m.PlannerLastTokens.WithLabelValues(c.schedule, "output").Set(float64(c.out.Int64))
	}
}

// observePlannerBreakers reports each enabled schedule's breaker, and drops a
// removed schedule's series.
func (e *Engine) observePlannerBreakers(ctx context.Context) error {
	rows, err := e.st.DB.QueryContext(ctx, `SELECT name, enabled, low_yield >= 4 FROM planner_schedule`)
	if err != nil {
		return err
	}
	defer rows.Close()
	for rows.Next() {
		var name string
		var enabled, open bool
		if err := rows.Scan(&name, &enabled, &open); err != nil {
			return err
		}
		if !enabled {
			e.m.PlannerBreakerOpen.DeleteLabelValues(name)
			continue
		}
		v := 0.0
		if open {
			v = 1
		}
		e.m.PlannerBreakerOpen.WithLabelValues(name).Set(v)
	}
	return rows.Err()
}

// plannerOutcomeEvery spaces the reads of one filed issue.
const (
	plannerOutcomeEvery = 5 * time.Minute
	plannerOutcomeBatch = 3
)

// syncPlannerOutcomes reads a few filed issues per tick. A Done issue proves
// its proposal (mark_planner_reproducible) and closes it, which starts the
// fingerprint's cooldown. A Canceled or deleted issue, or a delegated fix whose
// latest run failed, closes the proposal as rejected: the cooldown then stops
// the planner from delegating the same fingerprint again (HOME-548).
func (e *Engine) syncPlannerOutcomes(ctx context.Context) error {
	b := e.binding("homelab")
	if b == nil || b.ItsaplanProject != "HOME" {
		return nil
	}
	type filed struct {
		run, fp, key, route string
		id                  sql.NullInt64
	}
	rows, err := e.st.DB.QueryContext(ctx, `SELECT run_id,fingerprint,issue_key,issue_id,disposition FROM planner_proposal
 WHERE issue_key IS NOT NULL AND closed_at IS NULL AND (outcome_checked_at IS NULL OR outcome_checked_at < $1)
 ORDER BY outcome_checked_at NULLS FIRST, run_id, fingerprint LIMIT $2`, e.cfg.Now().Add(-plannerOutcomeEvery), plannerOutcomeBatch)
	if err != nil {
		return err
	}
	var open []filed
	for rows.Next() {
		var f filed
		if err := rows.Scan(&f.run, &f.fp, &f.key, &f.id, &f.route); err != nil {
			rows.Close()
			return err
		}
		open = append(open, f)
	}
	if err := rows.Close(); err != nil {
		return err
	}
	if len(open) == 0 {
		return nil
	}
	if err := e.guard(ctx); err != nil {
		return err
	}
	project, err := e.its.GetProject(ctx, b.ItsaplanProject)
	if err != nil {
		return err
	}
	done, canceled := columnNamed(project, "Done"), columnNamed(project, "Canceled")
	for _, f := range open {
		outcome := ""
		if !f.id.Valid {
			outcome = "rejected"
		} else {
			if err := e.guard(ctx); err != nil {
				return err
			}
			is, err := e.its.GetIssue(ctx, f.id.Int64)
			switch {
			case errors.Is(err, itsaplan.ErrNotFound):
				outcome = "rejected"
			case err != nil:
				return err
			case is.ColumnID == done && done > 0:
				outcome = "reproducible"
			case (is.ColumnID == canceled && canceled > 0) || is.ArchivedAt != nil:
				outcome = "rejected"
			case f.route == "delegate":
				var status sql.NullString
				err := e.st.DB.QueryRowContext(ctx, `SELECT o.payload->>'status' FROM run r JOIN outbox o ON o.run_id=r.id AND o.kind='result'
 WHERE r.issue_id=$1 ORDER BY r.id DESC, o.id DESC LIMIT 1`, f.id.Int64).Scan(&status)
				if err != nil && !errors.Is(err, sql.ErrNoRows) {
					return err
				}
				if status.Valid && status.String == "failed" {
					outcome = "rejected"
				}
			}
		}
		err := e.w.Tx(ctx, func(tx *sql.Tx) error {
			if outcome == "reproducible" {
				if _, err := tx.ExecContext(ctx, `SELECT mark_planner_reproducible($1)`, f.key); err != nil {
					return err
				}
			}
			if outcome == "" {
				_, err := tx.ExecContext(ctx, `UPDATE planner_proposal SET outcome_checked_at=$3 WHERE run_id=$1 AND fingerprint=$2`, f.run, f.fp, e.cfg.Now())
				return err
			}
			_, err := tx.ExecContext(ctx, `UPDATE planner_proposal SET closed_at=now(),outcome_checked_at=$3 WHERE run_id=$1 AND fingerprint=$2 AND closed_at IS NULL`, f.run, f.fp, e.cfg.Now())
			return err
		})
		if err != nil {
			return err
		}
	}
	return nil
}

// coderDelegate returns the itsaplan agent that takes coder work: task-agent,
// else the earlier homelab-codex until its issues finish (HOME-879). Only an
// agent user counts, never a human account with the same name.
func coderDelegate(assignees []itsaplan.Assignee) string {
	var fallback string
	for _, a := range assignees {
		if a.Kind != "agent" {
			continue
		}
		switch a.Username {
		case "task-agent":
			return a.UserID
		case "homelab-codex":
			fallback = a.UserID
		}
	}
	return fallback
}
