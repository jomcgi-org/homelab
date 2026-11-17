package dispatch

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"slices"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/itsaplan"
	"github.com/jomcgi/homelab/projects/factory/connector/store"
)

type deferredStart struct {
	Binding, ProjectKey, AgentKey                  string
	IssueID, ProjectID, BacklogID, TodoID, LabelID int64
	StartDate, DelegateID                          string
}

func due(issue itsaplan.Issue, label int64, today string) bool {
	_, err := time.Parse(time.DateOnly, issue.StartDate)
	return err == nil && issue.StartDate <= today && issue.DelegateUserID == nil &&
		issue.ArchivedAt == nil && slices.Contains(issue.LabelIDs, label)
}

// sweepDeferred admits once per issue and start date, recovering an unsupported
// kind after a chart rollback. No run slot is held by this poll.
func (e *Engine) sweepDeferred(ctx context.Context) error {
	var errs []error
	for _, b := range e.cfg.Bindings {
		if err := e.deferProject(ctx, b); err != nil {
			if fatal(err) {
				return err
			}
			e.countRefusal(roleCoordinator, "deferred_poll", err)
			errs = append(errs, fmt.Errorf("deferred %s: %w", b.Name, err))
		}
	}
	return errors.Join(errs...)
}

func (e *Engine) deferProject(ctx context.Context, b Binding) error {
	if err := e.BeginStatusRead(ctx, "deferred/project/"+b.Name); err != nil {
		return err
	}
	project, err := e.its.Project(ctx, b.ItsaplanProject)
	if err != nil {
		return err
	}
	p := deferredStart{Binding: b.Name, ProjectKey: b.ItsaplanProject, ProjectID: project.Project.ID}
	for _, label := range project.Labels {
		if label.Name == "deferred" {
			p.LabelID = label.ID
		}
	}
	if p.LabelID == 0 {
		return nil // Bootstrap has not installed the opt-in yet.
	}
	for _, col := range project.Columns {
		if col.Name == "Backlog" && col.StateType == "backlog" {
			p.BacklogID = col.ID
		}
		if col.Name == "Todo" && col.StateType == "unstarted" {
			p.TodoID = col.ID
		}
	}
	// The first configured agent is the binding's coding agent; later entries
	// accept reassigned runs. Never resolve a human account with the same name.
	if len(b.Agents) > 0 {
		p.AgentKey = b.Agents[0]
		for _, a := range project.Assignees {
			if a.Kind == "agent" && a.Username == b.Agents[0] {
				p.DelegateID = a.UserID
			}
		}
	}
	if p.BacklogID == 0 || p.TodoID == 0 || p.DelegateID == "" {
		return errors.New("deferred project lacks Backlog, Todo, coding agent")
	}
	if err := e.BeginStatusRead(ctx, "deferred/board/"+b.Name); err != nil {
		return err
	}
	issues, err := e.its.ProjectIssues(ctx, b.ItsaplanProject)
	if err != nil {
		return err
	}
	today := e.cfg.Now().UTC().Format(time.DateOnly)
	return e.w.Tx(ctx, func(tx *sql.Tx) error {
		for _, issue := range issues {
			if issue.ProjectID != p.ProjectID || issue.ColumnID != p.BacklogID || !due(issue, p.LabelID, today) {
				continue
			}
			p.IssueID, p.StartDate = issue.ID, issue.StartDate
			key := fmt.Sprintf("deferred/%d/%s", issue.ID, issue.StartDate)
			if err := e.st.EnqueueDeferred(ctx, tx, key, issue.ID, p); err != nil {
				return err
			}
		}
		return nil
	})
}

// deliverDeferred always resends the same server-side receipt key. The API
// transaction commits the comment, move, delegate and run together, so a crash
// either leaves no effects or a receipt that the next delivery can read back.
func (e *Engine) deliverDeferred(ctx context.Context, it *store.OutboxItem) error {
	var p deferredStart
	if err := json.Unmarshal(it.Payload, &p); err != nil {
		return e.st.OutboxAbandon(ctx, e.st.DB, it.ID, err.Error())
	}
	b := e.binding(p.Binding)
	if b == nil || b.ItsaplanProject != p.ProjectKey || len(b.Agents) == 0 || b.Agents[0] != p.AgentKey {
		return e.st.OutboxAbandon(ctx, e.st.DB, it.ID, "deferred binding changed")
	}
	stopped, err := e.st.DeferredStopped(ctx, p.IssueID)
	if err != nil {
		return err
	}
	if stopped {
		return e.st.OutboxAbandon(ctx, e.st.DB, it.ID, "deferred issue stopped")
	}
	// Journal every attempt, including a retry after a crash before the send.
	if err := e.w.Tx(ctx, func(tx *sql.Tx) error {
		_, err := tx.ExecContext(ctx, `UPDATE outbox SET send_started_at = now() WHERE id = $1`, it.ID)
		return err
	}); err != nil {
		return err
	}
	if err := e.guard(ctx); err != nil {
		return err
	}
	receipt, err := e.its.StartDeferred(ctx, p.IssueID, itsaplan.DeferredRequest{
		ProjectID: p.ProjectID, StartDate: p.StartDate, DelegateUserID: p.DelegateID,
	})
	if err != nil {
		if errors.Is(err, itsaplan.ErrWIPLimit) {
			// The API rolled back every effect. Capacity is not a delivery
			// failure: wait without consuming the bounded error retry budget.
			// This holds no run slot and survives an arbitrarily long wait.
			return e.w.Tx(ctx, func(tx *sql.Tx) error {
				return e.st.DeferredWait(ctx, tx, it.ID, e.cfg.Now().Add(time.Minute))
			})
		}
		if !fatal(err) && e.refused(ctx, it, err) {
			return e.st.OutboxAbandon(ctx, e.st.DB, it.ID, clip(err.Error(), maxErrorText))
		}
		return err
	}
	// An issue with a delegate is not evidence of a queued run. Only the
	// transaction receipt, with a verified run, can complete this outbox item.
	if receipt == nil || receipt.Key != it.DedupeKey || receipt.RunID <= 0 {
		return errors.New("invalid deferred start receipt")
	}
	err = e.w.Tx(ctx, func(tx *sql.Tx) error { return e.st.OutboxDelivered(ctx, tx, it.ID) })
	if err == nil {
		e.m.DeferredStarted.Inc()
	}
	return err
}
