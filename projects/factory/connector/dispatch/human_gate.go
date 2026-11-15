package dispatch

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"

	"github.com/jomcgi/homelab/projects/factory/connector/github"
	"github.com/jomcgi/homelab/projects/factory/connector/itsaplan"
	"github.com/jomcgi/homelab/projects/factory/connector/store"
)

// Human-gate issues follow PRs independently of coding turns and delivery stops.
// In particular a new head during a revision must leave Todo immediately.
func (e *Engine) stepHumanGates(ctx context.Context) error {
	if e.gh == nil {
		return nil
	}
	items, err := e.st.HumanGateWork(ctx)
	if err != nil {
		return err
	}
	var errs []error
	for _, item := range items {
		if e.cfg.Now().Sub(item.PolledAt) < e.cfg.ReviewPoll {
			continue
		}
		wi, err := e.st.GetWorkItem(ctx, item.WorkItemID)
		if err != nil {
			return err
		}
		b := e.binding(wi.BindingName)
		if b == nil || b.HumanGateAssignee == "" {
			continue
		}
		pr, err := e.st.PullRequestFor(ctx, wi.ID)
		if err != nil {
			return err
		}
		if err := e.pollHumanGate(ctx, b, pr); err != nil {
			if fatal(err) {
				return err
			}
			errs = append(errs, fmt.Errorf("human gate %s#%d: %w", b.repo(), pr.Number, err))
		}
	}
	return errors.Join(errs...)
}

func (e *Engine) pollHumanGate(ctx context.Context, b *Binding, pr *store.PullRequest) error {
	if err := e.beginHumanGateRead(ctx, b, pr, "pr", fmt.Sprint(pr.Number)); err != nil {
		return err
	}
	d, err := e.gh.PullRequestDetail(ctx, b.repo(), pr.Number)
	if err != nil {
		return err
	}
	h, err := e.st.HumanGateFor(ctx, b.repo(), pr.Number)
	if err != nil {
		return err
	}
	state, summary := "Backlog", "The current head is not ready for Joe's human-gate approval."
	switch {
	case d.Merged:
		state, summary = "Done", "The pull request merged."
	case d.State != "open":
		state, summary = "Canceled", "The pull request closed without merging."
	default:
		if err := e.beginHumanGateRead(ctx, b, pr, "checks", d.HeadSHA); err != nil {
			return err
		}
		checks, err := e.gh.CheckRuns(ctx, b.repo(), d.HeadSHA)
		if err != nil {
			return err
		}
		if err := e.beginHumanGateRead(ctx, b, pr, "statuses", d.HeadSHA); err != nil {
			return err
		}
		statuses, err := e.gh.Statuses(ctx, b.repo(), d.HeadSHA)
		if err != nil {
			return err
		}
		if review := humanGateReady(b, d, checks, statuses); review != nil {
			state = "Todo"
			// Reflected review text cannot impersonate another PR's recovery marker.
			summary = "Gated files and review findings:\n\n" + strings.NewReplacer(
				humanHoldMarker, "", "<!-- factory-human-gate/", "&lt;!-- factory-human-gate/",
			).Replace(review.Summary)
			if err := e.beginHumanGateRead(ctx, b, pr, "confirm-pr", fmt.Sprint(pr.Number)); err != nil {
				return err
			}
			latest, err := e.gh.PullRequestDetail(ctx, b.repo(), pr.Number)
			if err != nil {
				return err
			}
			switch {
			case latest.Merged:
				state, summary = "Done", "The pull request merged."
			case latest.State != "open":
				state, summary = "Canceled", "The pull request closed without merging."
			case latest.HeadSHA != d.HeadSHA || latest.Draft || latest.BaseRef != b.BaseBranch:
				state, summary = "Backlog", "The current head is not ready for Joe's human-gate approval."
			}
			d = latest
		}
	}
	if err := e.st.HumanGatePolled(ctx, pr.ID, e.cfg.Now()); err != nil {
		return err
	}
	if h == nil && state != "Todo" {
		return nil
	}
	key := fmt.Sprintf("factory-human-gate/%s/%d", b.repo(), pr.Number)
	marker := "<!-- " + key + " -->"
	description := fmt.Sprintf("@%s: %s\n\nHead SHA: `%s`.\n\n"+
		"Joe's merge is the approval for PRs authored as `jomcgi`, since GitHub does not allow authors to approve their own PRs.\n\n%s\n\n%s\n",
		b.HumanGateAssignee, mdLink(fmt.Sprintf("Pull request #%d", pr.Number), pr.URL), d.HeadSHA, summary, marker)
	h, err = e.st.ObserveHumanGate(ctx, b.repo(), pr, d.HeadSHA, state, description)
	if err != nil || h.AppliedRevision == h.Revision {
		return err
	}
	return e.deliverHumanGate(ctx, b, pr, h, key, marker)
}

// Require pr-checks explicitly as well as any other configured checks. Trust
// only the configured reviewer App, its completed check on this head and its
// service-owned marker. Pending, stale, foreign and mixed reviews do not qualify.
func humanGateReady(b *Binding, d *github.PullRequestDetail, checks []github.CheckRun, statuses []github.Status) *github.CheckRun {
	if d.Draft || d.HeadSHA == "" || d.BaseRef != b.BaseBranch || !reviewerHumanHold(&b.Review, d.HeadSHA, checks) {
		return nil
	}
	ci := false
	for _, c := range b.Review.RequiredChecks {
		if !succeeded(c, d.HeadSHA, checks, statuses) {
			return nil
		}
		ci = ci || c.Name == "pr-checks"
	}
	if !ci {
		return nil
	}
	r := b.Review.ReviewerCheck
	var review *github.CheckRun
	for i := range checks {
		c := &checks[i]
		if c.Name == r.Name && c.AppID == r.AppID && c.HeadSHA == d.HeadSHA {
			// Ambiguous duplicate checks are not approval evidence.
			if review != nil || c.Status != "completed" || !failedConclusion(c.Conclusion) || !strings.Contains(c.Summary, humanHoldMarker) {
				return nil
			}
			review = c
		}
	}
	return review
}

func (e *Engine) gateIssueBody(ctx context.Context, b *Binding, pr *store.PullRequest, h *store.HumanGateIssue) (itsaplan.GateIssueWrite, error) {
	var body itsaplan.GateIssueWrite
	if err := e.beginHumanGateRead(ctx, b, pr, "project", b.ItsaplanProject); err != nil {
		return body, err
	}
	project, err := e.its.GetProject(ctx, b.ItsaplanProject)
	e.countRefusal(roleCoordinator, lookupIssue, err)
	if err != nil {
		return body, err
	}
	for _, a := range project.Assignees {
		if a.Kind == "member" && strings.EqualFold(a.Username, b.HumanGateAssignee) {
			if body.AssigneeUserID != "" {
				return body, fmt.Errorf("multiple members match @%s", b.HumanGateAssignee)
			}
			body.AssigneeUserID = a.UserID
		}
	}
	for _, c := range project.Columns {
		if strings.EqualFold(c.Name, h.State) {
			body.ColumnID = c.ID
			break
		}
	}
	if body.ColumnID == 0 || body.AssigneeUserID == "" {
		return body, fmt.Errorf("project %s needs column %s and human member @%s", b.ItsaplanProject, h.State, b.HumanGateAssignee)
	}
	body.Title = fmt.Sprintf("Merge #%d past the human gate", pr.Number)
	body.Description = h.Description
	return body, nil
}

func (e *Engine) deliverHumanGate(ctx context.Context, b *Binding, pr *store.PullRequest, h *store.HumanGateIssue, key, marker string) error {
	body, err := e.gateIssueBody(ctx, b, pr, h)
	if err != nil {
		return err
	}
	if h.IssueID == 0 {
		messageID := key + "/create"
		body.Description += "\n<!-- message-id: " + messageID + " -->\n"
		a, err := e.st.HumanGateWrite(ctx, h, e.Epoch(), messageID, "create", body)
		if err != nil {
			return err
		}
		found, err := e.findHumanGateIssue(ctx, b, pr, marker)
		if err != nil {
			return err
		}
		if found == nil {
			if a.SendStarted || a.Finished {
				return errors.New("create outcome unknown; reconciling the issue marker without repeating the POST")
			}
			// Once evidence changed before the first POST, create only when
			// it is ready again. A sent create is still reconciled in any state.
			if h.State != "Todo" {
				return nil
			}
			if err := json.Unmarshal(a.Payload, &body); err != nil {
				return err
			}
			if err := e.st.StartHumanGateWrite(ctx, messageID, e.Epoch()); err != nil {
				return err
			}
			if err := e.guard(ctx); err != nil {
				return err
			}
			found, err = e.its.CreateGateIssue(ctx, b.ItsaplanProject, body)
			if err != nil {
				if notCreated(err) {
					if clearErr := e.st.ClearHumanGateSend(ctx, messageID); clearErr != nil {
						return clearErr
					}
				}
				return err
			}
		}
		if found.ID == 0 {
			return errors.New("itsaplan returned a gate issue without an ID")
		}
		if err := e.st.FinishHumanGateWrite(ctx, h, messageID, found.ID, false); err != nil {
			return err
		}
		h.IssueID = found.ID
		// A recovered create may describe an older head. Apply this poll's
		// current state in the same pass, including a merge or close.
		body, err = e.gateIssueBody(ctx, b, pr, h)
		if err != nil {
			return err
		}
	}
	messageID := fmt.Sprintf("%s/update/%d", key, h.Revision)
	body.Description += "\n<!-- message-id: " + messageID + " -->\n"
	a, err := e.st.HumanGateWrite(ctx, h, e.Epoch(), messageID, "update", body)
	if err != nil {
		return err
	}
	if err := json.Unmarshal(a.Payload, &body); err != nil {
		return err
	}
	if err := e.st.StartHumanGateWrite(ctx, messageID, e.Epoch()); err != nil {
		return err
	}
	if err := e.guard(ctx); err != nil {
		return err
	}
	if err := e.its.UpdateGateIssue(ctx, h.IssueID, body); err != nil {
		return err
	}
	return e.st.FinishHumanGateWrite(ctx, h, messageID, h.IssueID, true)
}

// Reads have stable message IDs too, but always fetch current evidence.
func (e *Engine) beginHumanGateRead(ctx context.Context, b *Binding, pr *store.PullRequest, operation, resource string) error {
	if err := e.guard(ctx); err != nil {
		return err
	}
	messageID := fmt.Sprintf("factory-human-gate/%s/%d/read/%s/%s", b.repo(), pr.Number, operation, resource)
	return e.st.RecordDeliveryRead(ctx, pr.ID, e.Epoch(), messageID, "human-gate/"+operation, resource)
}

func (e *Engine) findHumanGateIssue(ctx context.Context, b *Binding, pr *store.PullRequest, marker string) (*itsaplan.Issue, error) {
	if err := e.beginHumanGateRead(ctx, b, pr, "search", marker); err != nil {
		return nil, err
	}
	hits, err := e.its.SearchGateIssues(ctx, b.ItsaplanProject, marker)
	e.countRefusal(roleCoordinator, lookupIssue, err)
	if err != nil {
		return nil, err
	}
	var found *itsaplan.Issue
	for _, hit := range hits {
		if err := e.beginHumanGateRead(ctx, b, pr, "issue", fmt.Sprint(hit.ID)); err != nil {
			return nil, err
		}
		is, err := e.its.GetIssue(ctx, hit.ID)
		e.countRefusal(roleCoordinator, lookupIssue, err)
		if err != nil {
			return nil, err
		}
		if !strings.Contains(is.Description, marker) {
			continue
		}
		if found != nil {
			return nil, errors.New("multiple issues carry the human-gate marker")
		}
		found = is
	}
	return found, nil
}
