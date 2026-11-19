package dispatch

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"strings"

	"github.com/jomcgi/homelab/projects/factory/connector/github"
	"github.com/jomcgi/homelab/projects/factory/connector/itsaplan"
	"github.com/jomcgi/homelab/projects/factory/connector/store"
)

const activationFeedback = "The head commit must end with exactly one git trailer: Activation: none if nothing remains to make the issue's goal live, or Activation: <what must still happen, and its precondition>. Add the trailer and push a new head. Default-off flags and suspended schedules must be recorded here even when documented elsewhere."

// Commit authors are not trusted instruction sources. A pending step needs an
// independent assessment from the configured reviewer before delegation.
const activationPending = "Activation pending: the remaining step and precondition need a trusted reviewer's assessment before delegation."

var (
	errActivationOwned       = errors.New("activation follow-up has another owner or delegate")
	errActivationUnqueued    = errors.New("activation delegate is set but no delegation run is recorded")
	errActivationPermissions = errors.New("coordinator activation permissions are not effective")
)

func reviewedActivation(p *ReviewPolicy, d *github.PullRequestDetail, checks []github.CheckRun) string {
	if p.ReviewerCheck != nil && p.ReviewerCheck.AppID != 0 {
		var latest *github.CheckRun
		for i := range checks {
			c := &checks[i]
			if c.Name == p.ReviewerCheck.Name && c.AppID == p.ReviewerCheck.AppID && c.HeadSHA == d.HeadSHA &&
				(latest == nil || c.ID > latest.ID) {
				latest = c
			}
		}
		if latest != nil && latest.Status == "completed" && latest.Conclusion == "success" {
			if a := github.ReviewedActivation(latest.Text, d.HeadSHA); a != nil {
				return a.Text
			}
		}
	}
	// Review bodies have no authenticated edit provenance on this read path.
	// Even a trusted author's approval cannot supply activation instructions.
	return ""
}

type activationPayload struct {
	WorkID     int64             `json:"workId"`
	IssueID    int64             `json:"issueId"`
	IssueKey   string            `json:"issueKey"`
	Project    string            `json:"project"`
	Column     string            `json:"doneColumn,omitempty"`
	Agent      string            `json:"agent"`
	IssueURL   string            `json:"issueUrl"`
	PRURL      string            `json:"prUrl"`
	CommentKey string            `json:"commentKey"`
	Step       string            `json:"reviewedStep,omitempty"` // only authenticated reviewer text
	HeadURL    string            `json:"headUrl,omitempty"`
	FollowupID int64             `json:"followupId,omitempty"` // observe completion, without requiring another PR
	Complete   *issueDonePayload `json:"complete,omitempty"`
}

func (p activationPayload) marker() string {
	return fmt.Sprintf("<!-- factory-activation work-item=%d -->", p.WorkID)
}

func (p activationPayload) description(key string) string {
	return fmt.Sprintf("Put `Refs %s` in the commit message.\n\n%s\n\nOriginal issue: %s\nMerged PR: %s\nDelivered head: %s\n\n%s\n",
		key, p.step(), mdLink(p.IssueKey, p.IssueURL), p.PRURL, p.HeadURL, p.marker())
}

func (p activationPayload) step() string {
	if p.Step != "" {
		return p.Step
	}
	return activationPending
}

func firstColumn(cols []itsaplan.Column, state string) *itsaplan.Column {
	var found *itsaplan.Column
	for i := range cols {
		c := &cols[i]
		if c.StateType == state && (found == nil || c.Position < found.Position) {
			found = c
		}
	}
	return found
}

func issueState(is *itsaplan.Issue, cols []itsaplan.Column) string {
	for _, c := range cols {
		if c.ID == is.ColumnID {
			return c.StateType
		}
	}
	return ""
}

func inProject(is *itsaplan.Issue, project string) bool {
	i := strings.LastIndex(is.Identifier, "-")
	return i > 0 && is.Identifier[:i] == project
}

func blocks(is *itsaplan.Issue, target int64) bool {
	for _, link := range is.Links {
		if link.Kind == "blocks" && link.Direction == "outward" && link.Issue.ID == target {
			return true
		}
	}
	return false
}

func activationDelegate(project *itsaplan.Project, agent string) string {
	for _, a := range project.Assignees {
		if a.Kind == "agent" && a.Username == agent {
			return a.UserID
		}
	}
	return ""
}

func activationDelegateID(is *itsaplan.Issue) string {
	if is.DelegateUserID == nil {
		return ""
	}
	return *is.DelegateUserID
}

func activationTakenOver(is *itsaplan.Issue, delegate string) bool {
	// Assignment and delegation are separate: a human may own the issue
	// while explicitly delegating its delivery to this agent. The human
	// handoff clears that delegation. An undelegated new follow-up still
	// respects any human assignee before the factory starts its first run.
	return is.ArchivedAt != nil || (is.AssigneeUserID != "" && is.AssigneeUserID != delegate && activationDelegateID(is) != delegate) ||
		(activationDelegateID(is) != "" && activationDelegateID(is) != delegate)
}

func (e *Engine) activationStopped(ctx context.Context, workID, followupID int64) (bool, error) {
	w, err := e.st.GetWorkItem(ctx, workID)
	if err != nil || w.State == store.WorkStopped {
		return true, err
	}
	if followupID != 0 {
		w, err = e.st.WorkItemByIssue(ctx, followupID)
		if errors.Is(err, sql.ErrNoRows) {
			return false, nil
		}
		if err != nil || w.State == store.WorkStopped {
			return true, err
		}
	}
	return false, nil
}

// beginActivationCall journals every external attempt under a stable identity.
// Reads always refresh current state; writes reconcile the recorded issue first.
func (e *Engine) beginActivationCall(ctx context.Context, it *store.OutboxItem, workID int64, operation, resource string) error {
	if err := e.guard(ctx); err != nil {
		return err
	}
	if workID == 0 {
		return nil // legacy Done payload
	}
	pr, err := e.st.PullRequestFor(ctx, workID)
	if err != nil {
		return err
	}
	if pr == nil {
		return errors.New("activation has no recorded PR for its external attempt")
	}
	messageID := fmt.Sprintf("activation/%d/%s/%s", it.ID, operation, resource)
	if err := e.st.RecordDeliveryRead(ctx, pr.ID, e.w.Epoch, messageID, "activation/"+operation, resource); err != nil {
		return err
	}
	return e.guard(ctx)
}

// deliverActivation runs a resumable sequence: move the original, create or
// recover the undelegated issue, persist its identity, finalize its description,
// link it, then delegate. Every external call is fenced by the leader guard.
func (e *Engine) deliverActivation(ctx context.Context, it *store.OutboxItem) error {
	var p activationPayload
	if err := json.Unmarshal(it.Payload, &p); err != nil {
		return e.st.OutboxAbandon(ctx, e.st.DB, it.ID, err.Error())
	}
	if p.Complete != nil {
		copy := *it
		copy.Payload, _ = json.Marshal(p.Complete)
		return e.deliverIssueDone(ctx, &copy)
	}
	if ready, err := e.st.CommentSettled(ctx, p.CommentKey); err != nil || !ready {
		return err
	}
	var err error
	if p.FollowupID != 0 {
		err = e.observeActivation(ctx, it, p)
	} else {
		err = e.activate(ctx, it, p)
	}
	// WIP conflicts retry. Other permanent API refusals take the supported
	// issue-comment handoff path without switching to a human credential.
	if !errors.Is(err, itsaplan.ErrConflict) && e.refused(ctx, it, err) {
		return e.abandonActivation(ctx, it, p, err)
	}
	return err
}

func (e *Engine) activate(ctx context.Context, it *store.OutboxItem, p activationPayload) error {
	if stopped, err := e.activationStopped(ctx, p.WorkID, 0); err != nil {
		return err
	} else if stopped {
		return e.st.OutboxAbandon(ctx, e.st.DB, it.ID, "activation delivery stopped")
	}
	if err := e.beginActivationCall(ctx, it, p.WorkID, "GetProject", fmt.Sprint(p.Project)); err != nil {
		return err
	}
	project, err := e.its.GetProject(ctx, p.Project)
	if err != nil {
		return err
	}
	if !project.CanActivate() {
		return errActivationPermissions
	}
	if err := e.beginActivationCall(ctx, it, p.WorkID, "GetIssue", fmt.Sprint(p.IssueID)); err != nil {
		return err
	}
	original, err := e.its.GetIssue(ctx, p.IssueID)
	if err != nil {
		return err
	}
	state := issueState(original, project.Columns)
	delegate := activationDelegate(project, p.Agent)
	if delegate == "" {
		return e.abandonActivation(ctx, it, p, errors.New("original agent is not an assignable project agent"))
	}
	if !inProject(original, p.Project) || state == "canceled" || state == "completed" || activationTakenOver(original, delegate) {
		return e.st.OutboxAbandon(ctx, e.st.DB, it.ID, "original issue moved or stopped; activation not delegated")
	}
	started := firstColumn(project.Columns, "started")
	if started == nil {
		return e.abandonActivation(ctx, it, p, errors.New("project has no started column"))
	}
	if original.ColumnID != started.ID {
		if stopped, err := e.activationStopped(ctx, p.WorkID, 0); err != nil {
			return err
		} else if stopped {
			return e.st.OutboxAbandon(ctx, e.st.DB, it.ID, "activation delivery stopped")
		}
		if err := e.beginActivationCall(ctx, it, p.WorkID, "MoveIssue", fmt.Sprint(original.ID)); err != nil {
			return err
		}
		if err := e.its.MoveIssue(ctx, original.ID, started.ID); err != nil {
			return err
		}
	}
	id, _, err := e.st.ActivationFollowup(ctx, p.WorkID)
	if err != nil {
		return err
	}
	if id == 0 {
		follow, err := e.findActivation(ctx, it, p)
		if err != nil {
			return err
		}
		if follow == nil {
			if it.SendStarted {
				return errors.New("activation create outcome unknown; marker not found; refusing a second create")
			}
			todo := firstColumn(project.Columns, "unstarted")
			if todo == nil {
				todo = firstColumn(project.Columns, "backlog")
			}
			if todo == nil {
				return e.abandonActivation(ctx, it, p, errors.New("project has no unstarted or backlog column"))
			}
			// Bound by characters, keeping UTF-8 intact. The pinned API's title
			// field is text; 255 also fits ordinary tracker title limits.
			title := []rune("Activate " + p.IssueKey + ": " + original.Title)
			if len(title) > 255 {
				title = title[:255]
			}
			if err := e.st.OutboxSendStarted(ctx, it.ID); err != nil {
				return err
			}
			if stopped, err := e.activationStopped(ctx, p.WorkID, 0); err != nil {
				return err
			} else if stopped {
				return e.st.OutboxAbandon(ctx, e.st.DB, it.ID, "activation delivery stopped")
			}
			if err := e.beginActivationCall(ctx, it, p.WorkID, "CreateIssue", fmt.Sprint(p.Project)); err != nil {
				return err
			}
			follow, err = e.its.CreateIssue(ctx, p.Project, itsaplan.NewIssue{
				Title: string(title), ColumnID: todo.ID,
				Description: activationPending + "\n\n" + p.marker(),
			})
			if err != nil {
				if notCreated(err) {
					if clearErr := e.st.OutboxSendCleared(ctx, it.ID); clearErr != nil {
						return clearErr
					}
				}
				return err
			}
		}
		if follow.ID == 0 || !inProject(follow, p.Project) {
			return errors.New("activation create returned an incomplete issue identity")
		}
		if err := e.st.Tx(ctx, func(tx *sql.Tx) error {
			if err := e.st.RecordActivationFollowup(ctx, tx, p.WorkID, follow.ID, follow.Identifier); err != nil {
				return err
			}
			observe := p
			observe.FollowupID = follow.ID
			return e.st.Enqueue(ctx, tx, store.OutboxActivation, fmt.Sprintf("activation-observe/%d", p.WorkID), 0, "", observe)
		}); err != nil {
			return err
		}
		id = follow.ID
	}
	if err := e.beginActivationCall(ctx, it, p.WorkID, "GetIssue", fmt.Sprint(id)); err != nil {
		return err
	}
	follow, err := e.its.GetIssue(ctx, id)
	if err != nil {
		return err
	}
	if stopped, err := e.activationStopped(ctx, p.WorkID, id); err != nil {
		return err
	} else if stopped || !inProject(follow, p.Project) || issueState(follow, project.Columns) == "canceled" || follow.ArchivedAt != nil {
		return e.st.OutboxAbandon(ctx, e.st.DB, it.ID, "activation issue moved or canceled; not delegated")
	}
	if activationTakenOver(follow, delegate) {
		// This also covers the column's creation-time automatic assignee.
		// Preserve the owned placeholder and explain how to finish the handoff.
		return e.abandonActivation(ctx, it, p, errActivationOwned)
	}
	if activationDelegateID(follow) == delegate {
		// The field can persist without a run. Do not toggle delegation or
		// rewrite a description an executing agent or person may have edited.
		return e.confirmActivationDelegation(ctx, it, p, id)
	}
	if activationDelegateID(follow) != "" || (issueState(follow, project.Columns) != "unstarted" && issueState(follow, project.Columns) != "backlog") {
		return e.st.OutboxAbandon(ctx, e.st.DB, it.ID, "activation issue was taken over or completed")
	}
	description := p.description(follow.Identifier)
	if follow.Description != description {
		if follow.Description != activationPending+"\n\n"+p.marker() {
			return e.st.OutboxAbandon(ctx, e.st.DB, it.ID, "activation description was edited; not overwritten")
		}
		if stopped, err := e.activationStopped(ctx, p.WorkID, id); err != nil {
			return err
		} else if stopped {
			return e.st.OutboxAbandon(ctx, e.st.DB, it.ID, "activation stopped before description update")
		}
		if err := e.beginActivationCall(ctx, it, p.WorkID, "finalize-description", fmt.Sprint(id)); err != nil {
			return err
		}
		if err := e.its.UpdateIssue(ctx, id, itsaplan.IssueUpdate{Description: &description}); err != nil {
			return err
		}
	}
	if !blocks(follow, p.IssueID) {
		if stopped, err := e.activationStopped(ctx, p.WorkID, id); err != nil {
			return err
		} else if stopped {
			return e.st.OutboxAbandon(ctx, e.st.DB, it.ID, "activation stopped before linking")
		}
		if err := e.beginActivationCall(ctx, it, p.WorkID, "BlockIssue", fmt.Sprint(id)); err != nil {
			return err
		}
		if err := e.its.BlockIssue(ctx, id, p.IssueID); err != nil {
			return err
		}
	}
	if p.Step == "" {
		// Legacy or externally merged work still gets a tracked, linked issue,
		// but raw commit instructions never start a coding session.
		return e.abandonActivation(ctx, it, p, errors.New("no trusted activation assessment; review the delivered head and finalize the follow-up before delegating it"))
	}
	// A person may have canceled the original during the create/link calls.
	if err := e.beginActivationCall(ctx, it, p.WorkID, "GetIssue", fmt.Sprint(p.IssueID)); err != nil {
		return err
	}
	original, err = e.its.GetIssue(ctx, p.IssueID)
	if err != nil {
		return err
	}
	if !inProject(original, p.Project) || issueState(original, project.Columns) != "started" || activationTakenOver(original, delegate) {
		return e.st.OutboxAbandon(ctx, e.st.DB, it.ID, "original no longer started; activation not delegated")
	}
	// Re-read ownership after description/link writes, before the delegation
	// that starts a session. The API has no conditional PATCH/ETag contract.
	if err := e.beginActivationCall(ctx, it, p.WorkID, "GetIssue", fmt.Sprint(id)); err != nil {
		return err
	}
	follow, err = e.its.GetIssue(ctx, id)
	if err != nil {
		return err
	}
	if stopped, err := e.activationStopped(ctx, p.WorkID, id); err != nil {
		return err
	} else if stopped || !inProject(follow, p.Project) || activationTakenOver(follow, delegate) ||
		(issueState(follow, project.Columns) != "unstarted" && issueState(follow, project.Columns) != "backlog") ||
		follow.Description != description || !blocks(follow, p.IssueID) {
		return e.st.OutboxAbandon(ctx, e.st.DB, it.ID, "activation changed or stopped before delegation")
	}
	if err := e.beginActivationCall(ctx, it, p.WorkID, "delegate", fmt.Sprint(id)); err != nil {
		return err
	}
	if err := e.its.UpdateIssue(ctx, id, itsaplan.IssueUpdate{DelegateUserID: &delegate}); err != nil {
		return err
	}
	return e.confirmActivationDelegation(ctx, it, p, id)
}

func (e *Engine) confirmActivationDelegation(ctx context.Context, it *store.OutboxItem, p activationPayload, id int64) error {
	pr, err := e.st.PullRequestFor(ctx, p.WorkID)
	if err != nil {
		return err
	}
	if pr == nil {
		return errors.New("activation has no recorded PR for its run-history read")
	}
	var before int64
	for range 20 {
		if err := e.beginEjectionRead(ctx, pr, "activation-delegation-run", fmt.Sprintf("%d/%d", id, before)); err != nil {
			return err
		}
		page, err := e.its.IssueRuns(ctx, id, before)
		if err != nil {
			return err // 401/403 use the activation outbox's bounded retry policy
		}
		if stopped, err := e.activationStopped(ctx, p.WorkID, id); err != nil {
			return err
		} else if stopped {
			return e.st.OutboxAbandon(ctx, e.st.DB, it.ID, "activation stopped while confirming delegation")
		}
		for _, run := range page.Items {
			if run.ID > 0 && run.Username == p.Agent && run.Trigger == "delegation" {
				return e.st.OutboxDelivered(ctx, e.st.DB, it.ID)
			}
		}
		if page.NextCursor == nil {
			// The API can still be finishing its enqueue. Retry the read, never
			// the delegation transition; exhausted retries report the missing run.
			return errActivationUnqueued
		}
		if *page.NextCursor <= 0 || (before != 0 && *page.NextCursor >= before) {
			return errors.New("activation run history cursor did not advance")
		}
		before = *page.NextCursor
	}
	return errors.New("activation run history may be truncated")
}

func (e *Engine) findActivation(ctx context.Context, it *store.OutboxItem, p activationPayload) (*itsaplan.Issue, error) {
	if err := e.beginActivationCall(ctx, it, p.WorkID, "SearchIssues", fmt.Sprint(p.Project)); err != nil {
		return nil, err
	}
	hits, err := e.its.SearchIssues(ctx, p.Project, p.marker())
	if err != nil {
		return nil, err
	}
	var found *itsaplan.Issue
	for _, hit := range hits {
		if err := e.beginActivationCall(ctx, it, p.WorkID, "GetIssue", fmt.Sprint(hit.ID)); err != nil {
			return nil, err
		}
		is, err := e.its.GetIssue(ctx, hit.ID)
		if err != nil {
			return nil, err
		}
		if !inProject(is, p.Project) || !strings.Contains(is.Description, p.marker()) {
			continue
		}
		if found != nil {
			return nil, errors.New("multiple activation issues have the same work item marker")
		}
		found = is
	}
	return found, nil
}

func (e *Engine) abandonActivation(ctx context.Context, it *store.OutboxItem, p activationPayload, err error) error {
	if p.Complete != nil {
		return e.abandonDone(ctx, it, *p.Complete, "", "activation completion could not be delivered", err)
	}
	id, key, lookupErr := e.st.ActivationFollowup(ctx, p.WorkID)
	if lookupErr != nil {
		return lookupErr
	}
	where := "Create an activation issue in this project, finalize its description with its own Refs key, link it as blocking this issue, then delegate it to " + p.Agent + "."
	operation := "finish creating/delegating the follow-up"
	if errors.Is(err, errActivationPermissions) {
		where = "Verify the coordinator's work_items read/create/edit and ai_agents read grants from HOME-106, then " + strings.ToLower(where[:1]) + where[1:]
	}
	if id != 0 {
		where = fmt.Sprintf("Finish the description and blocks link on %s (issue %d), then delegate it to %s.", key, id, p.Agent)
	}
	if errors.Is(err, errActivationOwned) {
		where = fmt.Sprintf("%s (issue %d) already has another assignee or delegate, possibly assigned automatically by its column. Its ownership and description were preserved. The owner should finalize its description with its Refs key and the outstanding step above, and add a blocks link to %s. Delegate it only after an explicit handoff.", key, id, p.IssueKey)
	}
	if errors.Is(err, errActivationUnqueued) {
		where = fmt.Sprintf("%s (issue %d) has its delegate set but no delegation run was found. Inspect its run history; if no run exists, explicitly re-delegate it or take ownership of the work. The factory has not toggled its delegate, because a delayed run could still appear.", key, id)
	}
	if p.FollowupID != 0 {
		operation = "observe completion of the follow-up"
		where = fmt.Sprintf("Check whether %s (issue %d) is complete and still blocks this issue; if so, complete this issue while respecting any cancellation or takeover.", key, id)
	}
	return e.st.Tx(ctx, func(tx *sql.Tx) error {
		if err := e.st.Enqueue(ctx, tx, store.OutboxComment, fmt.Sprintf("comment/activation-abandoned/%d/%d", p.WorkID, it.ID), 0, "",
			commentPayload{IssueID: p.IssueID, Body: "The PR merged, but activation remains: " + p.step() +
				"\n\nThe factory could not " + operation + " (" + publicReason(err) + "). " + where +
				" Search for `" + p.marker() + "` before creating anything; an earlier create may have succeeded.\n\nMerged PR: " + p.PRURL}); err != nil {
			return err
		}
		return e.st.OutboxAbandon(ctx, tx, it.ID, clip(err.Error(), maxErrorText))
	})
}

// observeActivation follows tracker completion independently of code delivery.
// It never writes the follow-up, so a person may take it over and finish it.
// Stops, cancellation, unlinking and parent ownership still prevent propagation.
func (e *Engine) observeActivation(ctx context.Context, it *store.OutboxItem, p activationPayload) error {
	if stopped, err := e.activationStopped(ctx, p.WorkID, p.FollowupID); err != nil {
		return err
	} else if stopped {
		return e.st.OutboxAbandon(ctx, e.st.DB, it.ID, "activation observation stopped")
	}
	if err := e.beginActivationCall(ctx, it, p.WorkID, "GetProject", fmt.Sprint(p.Project)); err != nil {
		return err
	}
	project, err := e.its.GetProject(ctx, p.Project)
	if err != nil {
		return err
	}
	if err := e.beginActivationCall(ctx, it, p.WorkID, "GetIssue", fmt.Sprint(p.IssueID)); err != nil {
		return err
	}
	original, err := e.its.GetIssue(ctx, p.IssueID)
	if err != nil {
		return err
	}
	delegate := activationDelegate(project, p.Agent)
	if delegate == "" || !inProject(original, p.Project) || activationTakenOver(original, delegate) ||
		(issueState(original, project.Columns) != "started" && issueState(original, project.Columns) != "completed") {
		return e.st.OutboxAbandon(ctx, e.st.DB, it.ID, "activation parent moved or was taken over")
	}
	if err := e.beginActivationCall(ctx, it, p.WorkID, "GetIssue", fmt.Sprint(p.FollowupID)); err != nil {
		return err
	}
	follow, err := e.its.GetIssue(ctx, p.FollowupID)
	if err != nil {
		return err
	}
	if !inProject(follow, p.Project) || issueState(follow, project.Columns) == "canceled" ||
		(follow.ArchivedAt != nil && issueState(follow, project.Columns) != "completed") {
		return e.st.OutboxAbandon(ctx, e.st.DB, it.ID, "activation follow-up moved or canceled")
	}
	if issueState(follow, project.Columns) != "completed" {
		return e.st.WaitActivation(ctx, it.ID, e.cfg.Now().Add(e.cfg.Heartbeat))
	}
	return e.completeIssue(ctx, it, issueDonePayload{IssueID: follow.ID, Project: p.Project, Column: p.Column}, follow)
}

// completeIssue queues the next activation ancestor in the same transaction as
// acknowledging this move. Retries therefore cannot lose completion of a chain.
func (e *Engine) completeIssue(ctx context.Context, it *store.OutboxItem, p issueDonePayload, is *itsaplan.Issue) error {
	return e.st.Tx(ctx, func(tx *sql.Tx) error {
		parent, workID, err := e.st.ActivationParent(ctx, tx, p.IssueID)
		if err != nil {
			return err
		}
		if parent != 0 && blocks(is, parent) {
			// Use the activation kind for conditional completion too: a rollback
			// binary must not execute a Done move without these new conditions.
			if err := e.st.Enqueue(ctx, tx, store.OutboxActivation, fmt.Sprintf("activation-done/%d/%d", p.IssueID, parent), 0, "",
				activationPayload{WorkID: workID, Complete: &issueDonePayload{
					WorkID: workID, IssueID: parent, Project: p.Project, Column: p.Column,
					StartedOnly: true, ActivationChild: p.IssueID,
				}}); err != nil {
				return err
			}
		}
		return e.st.OutboxDelivered(ctx, tx, it.ID)
	})
}
