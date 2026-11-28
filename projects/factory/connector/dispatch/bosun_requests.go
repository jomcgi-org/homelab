package dispatch

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"strconv"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/itsaplan"
	"github.com/jomcgi/homelab/projects/factory/connector/store"
)

// Bosun's factory requests (HOME-940). bosun-requests inserts a row when Joe
// asks Bosun for factory work in Discord. This phase files the row as an
// issue in Todo, delegated to the binding's coding agent, and follows the
// issue to Done or Canceled. The Discord bridge then takes the finished row
// from bosun-requests and answers the message (discord/requests).
//
//	pending --file--> filed --issue Done/Canceled--> done/canceled --bridge reply--> replied
//
// Each step is safe to repeat: the keyed create returns the first issue.

const (
	bosunRequestBatch = 20
	// bosunRequestTries bounds failed attempts to file one request.
	bosunRequestTries = 6
	// bosunFollowEvery spaces reads of a filed request's issue.
	bosunFollowEvery = 2 * time.Minute
)

// BosunRequestKey is the itsaplan create key of request id.
func BosunRequestKey(id int64) string { return "bosun-request/" + strconv.FormatInt(id, 10) }

func (e *Engine) stepBosunRequests(ctx context.Context) error {
	name := e.cfg.BosunRequests
	if name == "" {
		return nil
	}
	now := e.cfg.Now()
	due, err := e.st.DueBosunRequests(ctx, now, bosunRequestBatch)
	if err != nil || len(due) == 0 {
		return err
	}
	b := e.binding(name)
	if b == nil || len(b.Agents) == 0 {
		return fmt.Errorf("bosun requests: binding %q has no coding agent", name)
	}
	if err := e.BeginStatusRead(ctx, "bosun-requests/project/"+b.Name); err != nil {
		return err
	}
	project, err := e.its.Project(ctx, b.ItsaplanProject)
	if err != nil {
		return err
	}
	var errs []error
	for _, r := range due {
		err := e.stepBosunRequest(ctx, b, project, r)
		if err == nil {
			continue
		}
		if fatal(err) {
			return err
		}
		next := now.Add(min(time.Minute<<min(r.Tries, 6), time.Hour))
		if rerr := e.w.Tx(ctx, func(tx *sql.Tx) error {
			return e.st.RetryBosunRequest(ctx, tx, r.ID, clip(err.Error(), maxErrorText), next, bosunRequestTries)
		}); rerr != nil {
			return rerr
		}
		errs = append(errs, fmt.Errorf("bosun request %d: %w", r.ID, err))
	}
	return errors.Join(errs...)
}

func (e *Engine) stepBosunRequest(ctx context.Context, b *Binding, project *itsaplan.Project, r store.BosunRequest) error {
	switch r.State {
	case store.BosunPending:
		return e.fileBosunRequest(ctx, b, project, r)
	case store.BosunFiled:
		return e.followBosunRequest(ctx, project, r)
	}
	return nil
}

func (e *Engine) fileBosunRequest(ctx context.Context, b *Binding, project *itsaplan.Project, r store.BosunRequest) error {
	todo := firstColumn(project.Columns, "unstarted")
	delegate := ""
	for _, a := range project.Assignees {
		// Never resolve a human account with the agent's name.
		if a.Kind == "agent" && a.Username == b.Agents[0] {
			delegate = a.UserID
		}
	}
	if todo == nil || delegate == "" {
		return errors.New("project lacks a Todo column or the coding agent")
	}
	key := BosunRequestKey(r.ID)
	if err := e.BeginStatusRead(ctx, key+"/FileRequest"); err != nil {
		return err
	}
	id, err := e.its.FileRequest(ctx, b.ItsaplanProject, key, itsaplan.NewIssue{
		Title: r.Title, Description: r.Description, ColumnID: todo.ID,
	})
	if err != nil {
		return err
	}
	if err := e.BeginStatusRead(ctx, key+"/GetIssue"); err != nil {
		return err
	}
	issue, err := e.its.GetIssue(ctx, id)
	if err != nil {
		return err
	}
	if issue.DelegateUserID == nil {
		// The final description names the issue, so the coder's commit
		// can cite it. Delegating starts the run.
		body := bosunRequestBody(issue.Identifier, r)
		if err := e.guard(ctx); err != nil {
			return err
		}
		if err := e.its.UpdateIssue(ctx, id, itsaplan.IssueUpdate{Description: &body, DelegateUserID: &delegate}); err != nil {
			return err
		}
	}
	column := columnName(project.Columns, issue.ColumnID)
	return e.w.Tx(ctx, func(tx *sql.Tx) error {
		return e.st.FileBosunRequest(ctx, tx, r.ID, id, issue.Identifier, column, e.cfg.Now().Add(bosunFollowEvery))
	})
}

// bosunRequestBody is the issue description: the Refs line, Joe's request
// as Bosun wrote it, and the Discord message it came from.
func bosunRequestBody(identifier string, r store.BosunRequest) string {
	return fmt.Sprintf("Put `Refs %s` in the commit message.\n\n%s\n\nRequested by Joe in Discord through Bosun: https://discord.com/channels/%s/%s/%s",
		identifier, r.Description, r.Guild, r.Channel, r.Message)
}

func (e *Engine) followBosunRequest(ctx context.Context, project *itsaplan.Project, r store.BosunRequest) error {
	if err := e.BeginStatusRead(ctx, BosunRequestKey(r.ID)+"/follow"); err != nil {
		return err
	}
	issue, err := e.its.GetIssue(ctx, r.IssueID)
	if err != nil {
		return err
	}
	column := columnName(project.Columns, issue.ColumnID)
	return e.w.Tx(ctx, func(tx *sql.Tx) error {
		switch issueState(issue, project.Columns) {
		case "completed":
			return e.st.FinishBosunRequest(ctx, tx, r.ID, store.BosunDone, column, e.cfg.Now())
		case "canceled":
			return e.st.FinishBosunRequest(ctx, tx, r.ID, store.BosunCanceled, column, e.cfg.Now())
		}
		return e.st.FollowBosunRequest(ctx, tx, r.ID, column, e.cfg.Now().Add(bosunFollowEvery))
	})
}

func columnName(cols []itsaplan.Column, id int64) string {
	for _, c := range cols {
		if c.ID == id {
			return c.Name
		}
	}
	return ""
}
