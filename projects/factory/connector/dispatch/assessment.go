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

var assessmentReference = regexp.MustCompile(`^([1-9][0-9]*d )?issue$`)

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
	window := assessmentReference.FindStringSubmatch(measure)
	if window == nil {
		return 0, ""
	}
	if window[1] == "" {
		return 3, measure
	}
	days, err := strconv.ParseInt(strings.TrimSuffix(window[1], "d "), 10, 32)
	if err != nil {
		return 0, ""
	}
	return int(days), measure
}

type assessmentJob struct {
	WorkID, ParentID, ChildID                             int64
	Project, Key, IssueURL, PRURL, StartDate, ColumnState string
	Days                                                  int
	Anchor                                                time.Time
}

// invariant: FACT-13
func (e *Engine) queueAssessment(ctx context.Context, tx *sql.Tx, p issueDonePayload, is *itsaplan.Issue) error {
	j := assessmentJob{WorkID: p.WorkID, ParentID: p.IssueID, Project: p.Project, Key: is.Identifier, Anchor: e.cfg.Now().UTC()}
	var days int
	err := tx.QueryRowContext(ctx, "SELECT days,issue_url,pr_url FROM assessment WHERE work_id=$1", p.WorkID).Scan(&days, &j.IssueURL, &j.PRURL)
	if errors.Is(err, sql.ErrNoRows) {
		return nil
	}
	if err != nil {
		return err
	}
	j.Days = days
	j.StartDate = j.Anchor.AddDate(0, 0, days+1).Format(time.DateOnly)
	return e.st.Enqueue(ctx, tx, "assessment", fmt.Sprintf("assessment/%d", p.WorkID), 0, "", j)
}

func (e *Engine) deliverAssessment(ctx context.Context, it *store.OutboxItem) error {
	var j assessmentJob
	if err := json.Unmarshal(it.Payload, &j); err != nil {
		return err
	}
	before := func(operation string, resource any) error {
		return e.BeginStatusRead(ctx, fmt.Sprintf("assessment/%d/%s/%v", it.ID, operation, resource))
	}
	if err := before("Project", j.Project); err != nil {
		return err
	}
	project, err := e.its.Project(ctx, j.Project)
	if err != nil {
		return err
	}
	if j.ChildID != 0 {
		if err := before("GetIssue", j.ChildID); err != nil {
			return err
		}
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
		if err = before("MoveIssue", j.ChildID); err != nil {
			return err
		}
		work, err := e.st.WorkItemByIssue(ctx, j.ChildID)
		if err != nil {
			return err
		}
		delegate := activationDelegate(project, work.AgentKey)
		if (j.WorkID != 0 && work.ID != j.WorkID) || work.State == store.WorkStopped || delegate == "" || activationDelegateID(issue) != delegate {
			return e.st.OutboxAbandon(ctx, e.st.DB, it.ID, "assessment stopped or taken over")
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
	var trailer string
	if err := e.st.DB.QueryRowContext(ctx, "SELECT child_id,days,measure FROM assessment WHERE work_id=$1", j.WorkID).Scan(&id, &j.Days, &trailer); err != nil {
		return err
	}
	if id.Valid {
		if err = before("GetIssue", id.Int64); err != nil {
			return err
		}
		child, err = e.its.GetIssue(ctx, id.Int64)
		if err != nil {
			return err
		}
	} else {
		if err = before("CreateAssessment", j.ParentID); err != nil {
			return err
		}
		child, err = e.its.CreateAssessment(ctx, j.ParentID, j.WorkID)
		if err != nil {
			return err
		}
	}
	if !inProject(child, j.Project) || child.ArchivedAt != nil {
		return e.st.OutboxAbandon(ctx, e.st.DB, it.ID, "assessment left project or was archived")
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
		if err = before("RelateIssue", child.ID); err != nil {
			return err
		}
		if err = e.its.RelateIssue(ctx, child.ID, j.ParentID); err != nil {
			return err
		}
	}
	if child.Description == marker {
		if err = before("GetIssue", j.ParentID); err != nil {
			return err
		}
		original, err := e.its.GetIssue(ctx, j.ParentID)
		if err != nil {
			return err
		}
		if !inProject(original, j.Project) {
			return e.st.OutboxAbandon(ctx, e.st.DB, it.ID, "assessment parent left project")
		}
		spec := assessmentSpecification(original)
		// Jobs saved by earlier connectors may contain free-form commit text.
		// Render those with the same opaque reference as new jobs.
		if !assessmentReference.MatchString(trailer) {
			trailer = fmt.Sprintf("%dd issue", j.Days)
		}
		description := fmt.Sprintf("Put `Refs %s` in the commit message.\n\nAssess: %s\nCompare the following original issue specification over %d days before and after rollout.\nWindow start: %s\nOriginal issue: %s\nMerged PR: %s\n\nReport observations and name confounders. Finish without commits with a single Verdict: accepted, Verdict: regressed or Verdict: inconclusive line. The recorded verdict is final.\n\n%s\n\n%s", child.Identifier, trailer, j.Days, j.Anchor.Format(time.RFC3339Nano), mdLink(j.Key, j.IssueURL), j.PRURL, spec, marker)

		if err = before("UpdateIssue", child.ID); err != nil {
			return err
		}
		if err = e.its.UpdateIssue(ctx, child.ID, itsaplan.IssueUpdate{Description: &description, StartDate: &j.StartDate, LabelIDs: []int64{label}}); err != nil {
			return err
		}
	}
	return e.st.OutboxDelivered(ctx, e.st.DB, it.ID)
}

// The public trailer names the original issue. Its private description and
// checklists supply the measurement, regardless of their labels or wording.
func assessmentSpecification(original *itsaplan.Issue) string {
	return renderIssueBrief("Original issue specification", original.Identifier, &itsaplan.Issue{
		Title: original.Title, Description: original.Description, Checklists: original.Checklists,
	}, nil)
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

func assessmentSummary(answer string) string {
	if v := assessmentOutcome(answer); v != "" {
		return "Verdict: " + v
	}
	return "No unambiguous verdict line was recorded. The assessment remains open."
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
	body := assessmentSummary(answer) + "\n\nObservations and confounders: " + mdLink(fmt.Sprintf("assessment run %d", r.ID), e.expand(e.cfg.Links.Run, wi, r.ID, ""))
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
			if err = e.st.Enqueue(ctx, tx, store.OutboxComment, key+"/parent", 0, "", commentPayload{IssueID: parent, Body: wi.IssueKey + "\n\n" + body}); err != nil {
				return true, err
			}
		} else {
			state = "completed"
		}
	}
	b := e.binding(wi.BindingName)
	return true, e.st.Enqueue(ctx, tx, "assessment", key+"/move", 0, "", assessmentJob{WorkID: wi.ID, Project: b.ItsaplanProject, ChildID: wi.IssueID, ColumnState: state})
}
