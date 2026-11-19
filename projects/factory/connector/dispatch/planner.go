package dispatch

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"errors"
	"fmt"
	"strings"

	"github.com/jomcgi/homelab/projects/factory/connector/itsaplan"
	"github.com/jomcgi/homelab/projects/factory/connector/planner"
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
			var route sql.NullString
			if err := tx.QueryRowContext(ctx, "SELECT reserve_planner_proposal($1,$2,$3)", run, p.Fingerprint, p.Fix).Scan(&route); err != nil {
				return err
			}
			if !route.Valid {
				continue
			}
			_, err := tx.ExecContext(ctx, `UPDATE planner_proposal SET title=$3,description=$4,priority=$5 WHERE run_id=$1 AND fingerprint=$2`, run, p.Fingerprint, title, p.Description(instructions), p.Priority)
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
 FROM planner_proposal WHERE title IS NOT NULL AND issue_key IS NULL AND closed_at IS NULL AND next_attempt<=now()
 ORDER BY next_attempt,run_id,fingerprint LIMIT 1`).Scan(&run, &fp, &route, &title, &description, &priority, &started)
	if errors.Is(err, sql.ErrNoRows) {
		return e.w.Tx(ctx, func(tx *sql.Tx) error {
			_, err := tx.ExecContext(ctx, `SELECT finish_planner_run(run_id) FROM planner_run r WHERE processed AND finished_at IS NULL AND NOT EXISTS
 (SELECT FROM planner_proposal p WHERE p.run_id=r.run_id AND issue_key IS NULL AND closed_at IS NULL)`)
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
		_, err := tx.ExecContext(ctx, `UPDATE planner_proposal SET next_attempt=now()+interval '1 minute' WHERE run_id=$1 AND fingerprint=$2`, run, fp)
		return err
	}); err != nil {
		return err
	}
	var filed *itsaplan.Issue
	if started {
		hits, err := e.its.SearchIssues(ctx, b.ItsaplanProject, marker)
		if err != nil {
			return err
		}
		for _, hit := range hits {
			is, err := e.its.GetIssue(ctx, hit.ID)
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
			return nil
		}
	} else {
		project, err := e.its.GetProject(ctx, b.ItsaplanProject)
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
		if err = e.w.Tx(ctx, func(tx *sql.Tx) error {
			res, err := tx.ExecContext(ctx, `UPDATE planner_proposal SET create_started=true WHERE run_id=$1 AND fingerprint=$2 AND NOT create_started AND closed_at IS NULL`, run, fp)
			if err != nil {
				return err
			}
			n, err := res.RowsAffected()
			if n != 1 {
				return errors.New("planner send already claimed")
			}
			return err
		}); err != nil {
			return err
		}
		filed, err = e.its.CreateIssue(ctx, b.ItsaplanProject, req)
		if err != nil {
			return err
		}
	}
	return e.w.Tx(ctx, func(tx *sql.Tx) error {
		_, err := tx.ExecContext(ctx, `UPDATE planner_proposal SET issue_key=$3 WHERE run_id=$1 AND fingerprint=$2 AND issue_key IS NULL`, run, fp, filed.Identifier)
		return err
	})
}
