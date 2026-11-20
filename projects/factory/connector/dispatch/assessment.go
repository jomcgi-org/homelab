package dispatch

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/github"
	"github.com/jomcgi/homelab/projects/factory/connector/itsaplan"
	"github.com/jomcgi/homelab/projects/factory/connector/store"
)

var assessmentWindow = regexp.MustCompile(`^([0-9]+)d(\s|$)`)

func assessmentTrailer(message string) (int, string) {
	if _, ok := github.ActivationTrailer(message); !ok {
		return 0, ""
	}
	message = strings.TrimSpace(strings.ReplaceAll(message, "\r\n", "\n"))
	var measure string
	var collecting bool
	for _, line := range strings.Split(message[strings.LastIndex(message, "\n\n")+2:], "\n") {
		if strings.HasPrefix(line, " ") || strings.HasPrefix(line, "\t") {
			if collecting {
				measure += " " + strings.TrimSpace(line)
			}
			continue
		}
		value, ok := strings.CutPrefix(line, "Assess:")
		collecting = ok
		if !ok {
			continue
		}
		if measure != "" || strings.TrimSpace(value) == "" {
			return 0, ""
		}
		measure = strings.TrimSpace(value)
	}
	if measure == "" {
		return 0, ""
	}
	window := assessmentWindow.FindStringSubmatch(measure)
	if window == nil {
		return 3, measure
	}
	days, err := strconv.ParseInt(window[1], 10, 32)
	if err != nil || days < 1 || strings.TrimSpace(measure[len(window[0]):]) == "" {
		return 0, ""
	}
	return int(days), measure
}

type assessmentJob struct {
	WorkID, ParentID, ChildID                                             int64
	Project, Key, Title, Measure, IssueURL, PRURL, StartDate, ColumnState string
	Anchor                                                                time.Time
}

func (e *Engine) queueAssessment(ctx context.Context, tx *sql.Tx, p issueDonePayload, is *itsaplan.Issue) error {
	j := assessmentJob{WorkID: p.WorkID, ParentID: p.IssueID, Project: p.Project, Title: is.Title, Key: is.Identifier, Anchor: e.cfg.Now().UTC()}
	var days int
	err := tx.QueryRowContext(ctx, "SELECT days,measure,issue_url,pr_url FROM assessment WHERE work_id=$1", p.WorkID).Scan(&days, &j.Measure, &j.IssueURL, &j.PRURL)
	if errors.Is(err, sql.ErrNoRows) {
		return nil
	}
	if err != nil {
		return err
	}
	j.StartDate = j.Anchor.AddDate(0, 0, days+1).Format(time.DateOnly)
	return e.st.Enqueue(ctx, tx, "assessment", fmt.Sprintf("assessment/%d", p.WorkID), 0, "", j)
}

func (e *Engine) deliverAssessment(ctx context.Context, it *store.OutboxItem) error {
	var j assessmentJob
	if err := json.Unmarshal(it.Payload, &j); err != nil {
		return err
	}
	project, err := e.its.Project(ctx, j.Project)
	if err != nil {
		return err
	}
	if j.ChildID != 0 {
		issue, err := e.its.GetIssue(ctx, j.ChildID)
		if err != nil {
			return err
		}
		if !inProject(issue, j.Project) || issue.ArchivedAt != nil || issueState(issue, project.Columns) == "canceled" {
			return e.st.OutboxAbandon(ctx, e.st.DB, it.ID, "assessment moved, archived or canceled")
		}
		col := assessmentColumn(project.Columns, j.ColumnState)
		if col == nil {
			return errors.New("assessment verdict column missing")
		}
		if issue.ColumnID == col.ID {
			return e.st.OutboxDelivered(ctx, e.st.DB, it.ID)
		}
		if err = e.guard(ctx); err != nil {
			return err
		}
		if err = e.its.MoveIssue(ctx, j.ChildID, col.ID); err != nil {
			return err
		}
		return e.st.OutboxDelivered(ctx, e.st.DB, it.ID)
	}
	var label int64
	for _, l := range project.Labels {
		if l.Name == "deferred" {
			label = l.ID
		}
	}
	backlog := assessmentColumn(project.Columns, "backlog")
	if label == 0 || backlog == nil {
		return errors.New("assessment requires deferred label and Backlog")
	}
	marker := fmt.Sprintf("<!-- factory-assessment work-item=%d -->", j.WorkID)
	// Once the create has an identity, retries use that receipt even if the
	// issue has been edited or search indexing is delayed.
	var child *itsaplan.Issue
	var id sql.NullInt64
	if err := e.st.DB.QueryRowContext(ctx, "SELECT child_id FROM assessment WHERE work_id=$1", j.WorkID).Scan(&id); err != nil {
		return err
	}
	if id.Valid {
		child, err = e.its.GetIssue(ctx, id.Int64)
		if err != nil {
			return err
		}
		if !inProject(child, j.Project) {
			return errors.New("assessment left project")
		}
	} else {
		hits, err := e.its.SearchIssues(ctx, j.Project, marker)
		if err != nil {
			return err
		}
		for _, h := range hits {
			is, err := e.its.GetIssue(ctx, h.ID)
			if err != nil {
				return err
			}
			if inProject(is, j.Project) && strings.Contains(is.Description, marker) {
				if child != nil {
					return errors.New("ambiguous assessment marker")
				}
				child = is
			}
		}
	}
	if child == nil {
		if it.SendStarted {
			return errors.New("assessment create outcome unknown")
		}
		title := []rune("Assess " + j.Key + ": " + j.Title)
		if err = e.st.OutboxSendStarted(ctx, it.ID); err != nil {
			return err
		}
		if err = e.guard(ctx); err != nil {
			return err
		}
		child, err = e.its.CreateIssue(ctx, j.Project, itsaplan.NewIssue{Title: string(title[:min(len(title), 255)]), ColumnID: backlog.ID, Description: marker})
		if err != nil {
			if notCreated(err) {
				if clear := e.st.OutboxSendCleared(ctx, it.ID); clear != nil {
					return clear
				}
			}
			return err
		}
	}
	if _, err = e.st.DB.ExecContext(ctx, "UPDATE assessment SET child_id=$2 WHERE work_id=$1", j.WorkID, child.ID); err != nil {
		return err
	}
	linked := false
	for _, l := range child.Links {
		if l.Kind == "relates" && l.Issue.ID == j.ParentID {
			linked = true
		}
	}
	if !linked {
		if err = e.guard(ctx); err != nil {
			return err
		}
		if err = e.its.RelateIssue(ctx, child.ID, j.ParentID); err != nil {
			return err
		}
	}
	description := fmt.Sprintf("Put `Refs %s` in the commit message.\n\nAssess: %s\nWindow start: %s\nOriginal issue: %s\nMerged PR: %s\n\nCompare the measure before and after rollout. Name confounders. Finish without commits with a single Verdict: accepted, Verdict: regressed or Verdict: inconclusive line. The recorded verdict is final.\n\n%s", child.Identifier, j.Measure, j.Anchor.Format(time.RFC3339Nano), mdLink(j.Key, j.IssueURL), j.PRURL, marker)
	if child.Description == marker {
		if err = e.guard(ctx); err != nil {
			return err
		}
		if err = e.its.UpdateIssue(ctx, child.ID, itsaplan.IssueUpdate{Description: &description, StartDate: &j.StartDate, LabelIDs: []int64{label}}); err != nil {
			return err
		}
	}
	return e.st.OutboxDelivered(ctx, e.st.DB, it.ID)
}

// Match the named columns the deferred sweep admits.
func assessmentColumn(columns []itsaplan.Column, state string) *itsaplan.Column {
	name := map[string]string{"backlog": "Backlog", "unstarted": "Todo", "completed": "Done"}[state]
	for i := range columns {
		if columns[i].Name == name && columns[i].StateType == state {
			return &columns[i]
		}
	}
	return nil
}

func assessmentOutcome(answer string) string {
	verdict := ""
	for _, line := range strings.Split(answer, "\n") {
		switch strings.TrimSpace(line) {
		case "Verdict: accepted", "Verdict: regressed", "Verdict: inconclusive":
			if verdict != "" {
				return ""
			}
			verdict = strings.TrimPrefix(strings.TrimSpace(line), "Verdict: ")
		}
	}
	return verdict
}

func (e *Engine) assessmentVerdict(ctx context.Context, tx *sql.Tx, wi *store.WorkItem, r *store.Run, answer string) (bool, error) {
	var parent int64
	var recorded sql.NullString
	err := tx.QueryRowContext(ctx, "SELECT parent_id,verdict FROM assessment WHERE child_id=$1 FOR UPDATE", wi.IssueID).Scan(&parent, &recorded)
	if errors.Is(err, sql.ErrNoRows) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	if recorded.Valid {
		return true, nil
	}
	verdict := assessmentOutcome(answer)
	body := answer
	if verdict == "" {
		body = strings.TrimSpace(body + "\n\nNo unambiguous verdict line was recorded. The assessment remains open.")
	}
	key := fmt.Sprintf("assessment-result/%d", r.ID)
	if err = e.st.Enqueue(ctx, tx, store.OutboxComment, key, 0, "", commentPayload{IssueID: wi.IssueID, Body: body}); err != nil {
		return true, err
	}
	state := "unstarted"
	if verdict != "" {
		if err = e.st.SetWorkState(ctx, tx, wi.ID, store.WorkDone); err != nil {
			return true, err
		}
		if _, err = tx.ExecContext(ctx, "UPDATE assessment SET verdict=$1 WHERE child_id=$2", verdict, wi.IssueID); err != nil {
			return true, err
		}
		if verdict == "regressed" {
			if err = e.st.Enqueue(ctx, tx, store.OutboxComment, key+"/parent", 0, "", commentPayload{IssueID: parent, Body: wi.IssueKey + "\n\n" + answer}); err != nil {
				return true, err
			}
		} else {
			state = "completed"
		}
	}
	b := e.binding(wi.BindingName)
	return true, e.st.Enqueue(ctx, tx, "assessment", key+"/move", 0, "", assessmentJob{Project: b.ItsaplanProject, ChildID: wi.IssueID, ColumnState: state})
}
