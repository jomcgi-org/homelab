package dispatch

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"

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

// invariant: FACT-14
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
	description, err := humanGateDescription(b, pr, d, summary, key)
	if err != nil {
		return err
	}
	h, err = e.st.ObserveHumanGate(ctx, b.repo(), pr, d.HeadSHA, state, description)
	if err != nil {
		return err
	}
	var marked []*itsaplan.Issue
	needsSearch := h.IssueID != 0 && (h.State == "Done" || h.CreateSends != 1)
	if h.IssueID == 0 {
		if marked, err = e.adoptHumanGateIssue(ctx, b, pr, h, key, marker); err != nil || h.IssueID == 0 {
			return err
		}
	}
	if h.AppliedRevision != h.Revision {
		if err := e.updateHumanGate(ctx, b, pr, h, key); err != nil {
			return err
		}
	}
	if needsSearch {
		// One create POST makes at most one issue. After a retry, an earlier
		// POST can still commit a marked copy (ARCHITECTURE.md "Human gate"),
		// so search on every poll. An adoption without a POST searches too.
		if marked, err = e.markedHumanGateIssues(ctx, b, pr, marker); err != nil {
			return err
		}
	}
	return e.cancelHumanGateDuplicates(ctx, b, pr, h, key, marked)
}

// itsaplan limits descriptions to 50,000 characters. A UTF-8 byte budget is
// conservative for both Unicode code points and UTF-16 units. Reserve space
// for the longest message ID before clipping only the reflected review text.
func humanGateDescription(b *Binding, pr *store.PullRequest, d *github.PullRequestDetail, summary, key string) (string, error) {
	const limit = 50000
	prefix := fmt.Sprintf("@%s: %s\n\nHead SHA: `%s`.\n\n%s\n\n", b.HumanGateAssignee,
		mdLink(fmt.Sprintf("Pull request #%d", pr.Number), pr.URL), d.HeadSHA, humanGateApproval(b, d))
	suffix := "\n\n<!-- " + key + " -->\n"
	budget := limit - len(prefix) - len(suffix) - len(humanGateMessageMarker(key+"/update/9223372036854775807"))
	if len(summary) > budget {
		note := "\n\n" + mdLink("Review summary truncated; see the full review in the pull request's checks", pr.URL+"/checks")
		if budget < len(note) {
			return "", errors.New("human-gate metadata exceeds the issue description limit")
		}
		summary = clip(summary, budget-len(note)) + note
	}
	return prefix + summary + suffix, nil
}

// joeLogin holds Joe's human gate in REVIEW.md "Joe decides".
const joeLogin = "jomcgi"

// humanGateApproval names the approval that clears the gate for this PR and
// links the rule or decision that sets it. Only fixed text and configured
// labels reach the issue; the author login is compared, never copied.
func humanGateApproval(b *Binding, d *github.PullRequestDetail) string {
	const (
		rule   = "https://github.com/jomcgi-org/homelab/blob/main/REVIEW.md#joe-decides"
		waiver = "https://github.com/jomcgi-org/homelab/issues/7817#issuecomment-6891640540"
		scope  = "https://github.com/jomcgi-org/homelab/issues/8049"
	)
	review := fmt.Sprintf("an APPROVED review of this head by `%s`", joeLogin)
	switch {
	case b.Review.held(d.Labels) != "":
		return fmt.Sprintf("To approve: the PR has an explicit hold (`%s`), so only %s clears the gate (%s).",
			strings.Join(b.Review.HoldLabels, "` or `"), review, mdLink("rule", rule))
	case strings.EqualFold(d.Author, joeLogin):
		check := "reviewer"
		if b.Review.ReviewerCheck != nil {
			check = "`" + b.Review.ReviewerCheck.Name + "`"
		}
		return fmt.Sprintf("To approve: the PR's %s check says which approval it needs. For a PR authored by `%s`, "+
			"the reviewer's verified authorship waiver may apply (%s, %s). Holds such as a shared head or reviewer "+
			"recovery always need %s (%s).",
			check, joeLogin, mdLink("decision", waiver), mdLink("#8049 scope", scope), review, mdLink("rule", rule))
	}
	return fmt.Sprintf("To approve: %s clears the gate (%s).", review, mdLink("rule", rule))
}

func humanGateMessageMarker(messageID string) string {
	return "\n<!-- message-id: " + messageID + " -->\n"
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

func (e *Engine) gateProject(ctx context.Context, b *Binding, pr *store.PullRequest) (*itsaplan.Project, error) {
	if err := e.beginHumanGateRead(ctx, b, pr, "project", b.ItsaplanProject); err != nil {
		return nil, err
	}
	project, err := e.its.GetProject(ctx, b.ItsaplanProject)
	e.countRefusal(roleCoordinator, lookupIssue, err)
	return project, err
}

func columnNamed(project *itsaplan.Project, name string) int64 {
	for _, c := range project.Columns {
		if strings.EqualFold(c.Name, name) {
			return c.ID
		}
	}
	return 0
}

func (e *Engine) gateIssueBody(ctx context.Context, b *Binding, pr *store.PullRequest, h *store.HumanGateIssue) (itsaplan.GateIssueWrite, error) {
	var body itsaplan.GateIssueWrite
	project, err := e.gateProject(ctx, b, pr)
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
	body.ColumnID = columnNamed(project, h.State)
	if body.ColumnID == 0 || body.AssigneeUserID == "" {
		return body, fmt.Errorf("project %s needs column %s and human member @%s", b.ItsaplanProject, h.State, b.HumanGateAssignee)
	}
	body.Title = fmt.Sprintf("Merge #%d past the human gate", pr.Number)
	body.Description = h.Description
	return body, nil
}

// adoptHumanGateIssue adopts the earliest marked issue, or creates one when
// no issue carries the marker. It returns every marked issue it found.
func (e *Engine) adoptHumanGateIssue(ctx context.Context, b *Binding, pr *store.PullRequest, h *store.HumanGateIssue, key, marker string) ([]*itsaplan.Issue, error) {
	body, err := e.gateIssueBody(ctx, b, pr, h)
	if err != nil {
		return nil, err
	}
	messageID := key + "/create"
	body.Description += humanGateMessageMarker(messageID)
	a, err := e.st.HumanGateWrite(ctx, h, e.Epoch(), messageID, "create", body)
	if err != nil {
		return nil, err
	}
	marked, err := e.markedHumanGateIssues(ctx, b, pr, marker)
	if err != nil {
		return nil, err
	}
	found, err := earliestHumanGateIssue(marked)
	if err != nil {
		return nil, err
	}
	if found == nil {
		// An issue from a started send carries the marker. Until that
		// send settles, its POST can still land: never send another.
		if a.SendStarted && a.SendAge < humanGateSettle {
			return nil, fmt.Errorf("create outcome unknown; searching for the marker until %s after the send", humanGateSettle)
		}
		// Once evidence changed before the first POST, create only when
		// it is ready again. A started create is still reconciled in any state.
		if h.State != "Todo" {
			return nil, nil
		}
		if err := json.Unmarshal(a.Payload, &body); err != nil {
			return nil, err
		}
		if found, err = e.createHumanGateIssue(ctx, b, messageID, a.Sends, body); err != nil {
			return nil, err
		}
	}
	if found.ID == 0 {
		return nil, errors.New("itsaplan returned a gate issue without an ID")
	}
	if err := e.st.FinishHumanGateWrite(ctx, h, messageID, found.ID, false); err != nil {
		return nil, err
	}
	h.IssueID = found.ID
	return marked, nil
}

// earliestHumanGateIssue picks the same issue on every poll: the earliest
// createdAt, then the lowest ID. An unreadable createdAt picks none.
func earliestHumanGateIssue(marked []*itsaplan.Issue) (*itsaplan.Issue, error) {
	switch len(marked) {
	case 0:
		return nil, nil
	case 1:
		return marked[0], nil
	}
	var first *itsaplan.Issue
	var firstAt time.Time
	for _, is := range marked {
		at, err := time.Parse(time.RFC3339Nano, is.CreatedAt)
		if err != nil {
			return nil, fmt.Errorf("gate issue %d has no readable createdAt: %w", is.ID, err)
		}
		if first == nil || at.Before(firstAt) || at.Equal(firstAt) && is.ID < first.ID {
			first, firstAt = is, at
		}
	}
	return first, nil
}

// updateHumanGate applies this poll's state to the adopted issue. A recovered
// create may describe an older head, so it runs after adoption in the same
// pass, including for a merge or close.
func (e *Engine) updateHumanGate(ctx context.Context, b *Binding, pr *store.PullRequest, h *store.HumanGateIssue, key string) error {
	body, err := e.gateIssueBody(ctx, b, pr, h)
	if err != nil {
		return err
	}
	messageID := fmt.Sprintf("%s/update/%d", key, h.Revision)
	body.Description += humanGateMessageMarker(messageID)
	a, err := e.st.HumanGateWrite(ctx, h, e.Epoch(), messageID, "update", body)
	if err != nil {
		return err
	}
	if err := json.Unmarshal(a.Payload, &body); err != nil {
		return err
	}
	if _, err := e.st.StartHumanGateWrite(ctx, e.w, messageID, a.Sends); err != nil {
		return err
	}
	if err := e.its.UpdateGateIssue(ctx, h.IssueID, body); err != nil {
		return err
	}
	return e.st.FinishHumanGateWrite(ctx, h, messageID, h.IssueID, true)
}

// gateDuplicate is the frozen write that cancels one duplicate gate issue.
type gateDuplicate struct {
	IssueID int64  `json:"issueId"`
	Column  string `json:"column"`
	Comment string `json:"comment"`
}

// cancelHumanGateDuplicates moves each marked issue other than the adopted
// one to Canceled once. Its attempt is recorded before the move, under a
// message ID for that issue; the comment that names the kept issue is queued
// when the move is acknowledged. A person's later move of the copy stands.
func (e *Engine) cancelHumanGateDuplicates(ctx context.Context, b *Binding, pr *store.PullRequest, h *store.HumanGateIssue, key string, marked []*itsaplan.Issue) error {
	var kept *itsaplan.Issue
	copies := 0
	for _, is := range marked {
		if is.ID == h.IssueID {
			kept = is
		} else {
			copies++
		}
	}
	if copies == 0 {
		return nil
	}
	if kept == nil {
		return fmt.Errorf("gate issue %d lost its marker while other issues carry it", h.IssueID)
	}
	var canceled int64
	for _, dup := range marked {
		if dup.ID == kept.ID {
			continue
		}
		messageID := fmt.Sprintf("%s/duplicate/%d", key, dup.ID)
		a, err := e.st.HumanGateWrite(ctx, h, e.Epoch(), messageID, "cancel", gateDuplicate{
			IssueID: dup.ID, Column: "Canceled",
			Comment: fmt.Sprintf("Duplicate of %s, the human-gate issue for %s. A delayed create made this copy, "+
				"so the factory moved it to Canceled.", kept.Identifier, mdLink(fmt.Sprintf("pull request #%d", pr.Number), pr.URL)),
		})
		if err != nil {
			return err
		}
		if a.Finished {
			continue
		}
		var p gateDuplicate
		if err := json.Unmarshal(a.Payload, &p); err != nil {
			return err
		}
		if canceled == 0 {
			project, err := e.gateProject(ctx, b, pr)
			if err != nil {
				return err
			}
			if canceled = columnNamed(project, p.Column); canceled == 0 {
				return fmt.Errorf("project %s needs column %s", b.ItsaplanProject, p.Column)
			}
		}
		if _, err := e.st.StartHumanGateWrite(ctx, e.w, messageID, a.Sends); err != nil {
			return err
		}
		if err := e.its.MoveIssue(ctx, p.IssueID, canceled); err != nil {
			return err
		}
		if err := e.st.FinishHumanGateCancel(ctx, messageID, p.IssueID, p.Comment); err != nil {
			return err
		}
	}
	return nil
}

// itsaplan 1.10 has no create idempotency key, and its search matches the
// description with SQL ILIKE in the database that stores the issue
// (ARCHITECTURE.md "Human gate"). The POST deadline starts before the send
// marker commits. When the marker is humanGateSettle old, the connector holds
// no POST under it open. itsaplan can still commit one late; the next poll then
// cancels the copy (cancelHumanGateDuplicates).
const (
	humanGateSendTimeout = 30 * time.Second
	humanGateSettle      = 10 * time.Minute
)

// createHumanGateIssue marks the send and POSTs at once. A crash or an
// ambiguous answer leaves the send started; the poll after it settles
// searches again and retries only if no issue carries the marker.
func (e *Engine) createHumanGateIssue(ctx context.Context, b *Binding, messageID string, sends int, body itsaplan.GateIssueWrite) (*itsaplan.Issue, error) {
	send, cancel := context.WithTimeout(ctx, humanGateSendTimeout)
	defer cancel()
	n, err := e.st.StartHumanGateWrite(send, e.w, messageID, sends)
	if err != nil {
		return nil, err
	}
	found, err := e.its.CreateGateIssue(send, b.ItsaplanProject, body)
	if err != nil && notCreated(err) {
		// Retry on the next poll with the current payload. If this write
		// fails, the send settles and the retry waits for humanGateSettle.
		err = errors.Join(err, e.st.ClearHumanGateSend(ctx, messageID, e.Epoch(), n))
	}
	return found, err
}

// Reads have stable message IDs too, but always fetch current evidence.
func (e *Engine) beginHumanGateRead(ctx context.Context, b *Binding, pr *store.PullRequest, operation, resource string) error {
	if err := e.guard(ctx); err != nil {
		return err
	}
	messageID := fmt.Sprintf("factory-human-gate/%s/%d/read/%s/%s", b.repo(), pr.Number, operation, resource)
	return e.st.RecordDeliveryRead(ctx, pr.ID, e.Epoch(), messageID, "human-gate/"+operation, resource)
}

// markedHumanGateIssues returns every issue whose description carries the
// exact marker, in search order.
func (e *Engine) markedHumanGateIssues(ctx context.Context, b *Binding, pr *store.PullRequest, marker string) ([]*itsaplan.Issue, error) {
	if err := e.beginHumanGateRead(ctx, b, pr, "search", marker); err != nil {
		return nil, err
	}
	hits, err := e.its.SearchGateIssues(ctx, b.ItsaplanProject, marker)
	e.countRefusal(roleCoordinator, lookupIssue, err)
	if err != nil {
		return nil, err
	}
	var marked []*itsaplan.Issue
	for _, hit := range hits {
		if err := e.beginHumanGateRead(ctx, b, pr, "issue", fmt.Sprint(hit.ID)); err != nil {
			return nil, err
		}
		is, err := e.its.GetIssue(ctx, hit.ID)
		e.countRefusal(roleCoordinator, lookupIssue, err)
		if err != nil {
			return nil, err
		}
		if strings.Contains(is.Description, marker) {
			marked = append(marked, is)
		}
	}
	return marked, nil
}
