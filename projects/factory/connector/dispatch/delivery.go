package dispatch

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"strconv"
	"strings"

	"github.com/jomcgi/homelab/projects/factory/connector/github"
	"github.com/jomcgi/homelab/projects/factory/connector/itsaplan"
	"github.com/jomcgi/homelab/projects/factory/connector/store"
)

// GitHub is the subset of the GitHub client delivery uses.
type GitHub interface {
	FindPullRequest(ctx context.Context, repo, branch string) (*github.PullRequest, error)
	CreatePullRequest(ctx context.Context, repo string, n github.NewPullRequest) (*github.PullRequest, error)
	UpdatePullRequest(ctx context.Context, repo string, number int, u github.PullRequestUpdate) (*github.PullRequest, error)
	Compare(ctx context.Context, repo, base, head string) (*github.Comparison, error)
	PullRequestDetail(ctx context.Context, repo string, number int) (*github.PullRequestDetail, error)
	CheckRuns(ctx context.Context, repo, sha string) ([]github.CheckRun, error)
	Statuses(ctx context.Context, repo, sha string) ([]github.Status, error)
	EnqueuePullRequest(ctx context.Context, repo, nodeID, headSHA string) (string, error)
}

// Check is a check the binding trusts: a check run named Name from the
// GitHub App AppID, or a commit status with context Name set by the login
// StatusCreator.
type Check struct {
	Name          string
	AppID         int64
	StatusCreator string
}

// ReviewPolicy is a binding's trusted identities and bounds.
type ReviewPolicy struct {
	// TrustedReviewers are GitHub logins whose reviews act.
	TrustedReviewers []string
	// RequiredChecks must succeed on the head before it is enqueued.
	RequiredChecks []Check
	// ReviewerCheck is the independent reviewer's check. Without one, a
	// trusted reviewer's approval of the head stands in for it.
	ReviewerCheck *Check
	// MaxReviewRounds bounds the connector's review rounds per work item.
	MaxReviewRounds int
	// DoneColumn names the itsaplan column a merged issue moves to; empty
	// picks the project's first completed column.
	DoneColumn string
}

// Links are URL templates for the PR body and the Done comment. {issue},
// {issueId}, {run}, {namespace}, {agent} and {session} are replaced; an
// empty template leaves plain text.
type Links struct {
	Issue   string
	Run     string
	Session string
}

func (b *Binding) repo() string { return b.Owner + "/" + b.Repo }

func (p *ReviewPolicy) trustedReviewer(login string) bool {
	for _, l := range p.TrustedReviewers {
		if strings.EqualFold(l, login) {
			return true
		}
	}
	return false
}

// checks lists the required checks and the reviewer check.
func (p *ReviewPolicy) checks() []Check {
	out := append([]Check(nil), p.RequiredChecks...)
	if p.ReviewerCheck != nil {
		out = append(out, *p.ReviewerCheck)
	}
	return out
}

// stepDelivery follows the PRs of work items in review or in the merge
// queue: it opens or updates the one PR after a finished turn, polls each PR
// every ReviewPoll, starts review rounds, enqueues green heads and observes
// the merge.
func (e *Engine) stepDelivery(ctx context.Context) error {
	if e.gh == nil {
		return nil
	}
	items, err := e.st.DeliveryWork(ctx)
	if err != nil {
		return err
	}
	var errs []error
	for _, wi := range items {
		if err := e.deliverWork(ctx, wi); err != nil {
			if fatal(err) {
				return err
			}
			errs = append(errs, fmt.Errorf("work item %d: %w", wi.ID, err))
		}
	}
	return errors.Join(errs...)
}

func (e *Engine) deliverWork(ctx context.Context, wi *store.WorkItem) error {
	b := e.binding(wi.BindingName)
	if b == nil {
		return nil
	}
	pr, err := e.st.PullRequestFor(ctx, wi.ID)
	if err != nil {
		return err
	}
	if wi.State == store.WorkDone {
		return e.followUp(ctx, wi, b, pr)
	}
	if wi.TurnAttemptID != 0 && wi.TurnAttemptID != wi.SyncedAttemptID {
		if pr, err = e.syncPR(ctx, wi, b, pr); err != nil {
			return err
		}
	}
	if pr == nil || e.cfg.Now().Sub(pr.PolledAt) < e.cfg.ReviewPoll {
		return nil
	}
	return e.pollPR(ctx, wi, b, pr)
}

// syncPR opens or updates the work item's one PR for its latest finished
// turn. GitHub keeps one open PR per head branch, and the connector always
// looks the branch's PR up before opening one, so a restart or a lost
// response finds the PR it opened instead of opening a second.
func (e *Engine) syncPR(ctx context.Context, wi *store.WorkItem, b *Binding, pr *store.PullRequest) (*store.PullRequest, error) {
	a, r, err := e.st.LatestTurn(ctx, wi.ID)
	if err != nil || a == nil {
		return pr, err
	}
	repo := b.repo()
	body := e.prBody(ctx, wi, a, r)
	if pr != nil && pr.State == "merged" {
		return pr, e.st.MarkSynced(ctx, e.st.DB, wi.ID, a.ID)
	}
	if pr != nil {
		// Only the body changes. A PR a person closed stays closed: the
		// connector never reopens it against their decision.
		if err := e.guard(ctx); err != nil {
			return nil, err
		}
		got, err := e.gh.UpdatePullRequest(ctx, repo, pr.Number, github.PullRequestUpdate{Body: &body})
		if err != nil {
			return nil, err
		}
		e.m.PullRequests.WithLabelValues("updated").Inc()
		return e.settlePR(ctx, wi, b, a, got)
	}
	if err := e.guard(ctx); err != nil {
		return nil, err
	}
	cmp, err := e.gh.Compare(ctx, repo, b.BaseBranch, wi.Branch)
	if errors.Is(err, github.ErrNotFound) {
		cmp, err = &github.Comparison{}, nil // the branch was never pushed
	}
	if err != nil {
		return nil, err
	}
	if cmp.AheadBy == 0 {
		e.m.PullRequests.WithLabelValues("no_commits").Inc()
		return nil, e.st.Tx(ctx, func(tx *sql.Tx) error {
			if err := e.st.Enqueue(ctx, tx, store.OutboxComment, fmt.Sprintf("comment/no-commits/%d/%d", wi.ID, a.ID), 0, "",
				commentPayload{IssueID: wi.IssueID, Body: fmt.Sprintf(
					"The factory's turn on %s finished without commits on %s ahead of %s, so there is no pull request yet.",
					wi.IssueKey, wi.Branch, b.BaseBranch)}); err != nil {
				return err
			}
			return e.st.MarkSynced(ctx, tx, wi.ID, a.ID)
		})
	}
	got, err := e.findOrOpenPR(ctx, repo, wi, b, cmp, body)
	if err != nil {
		return nil, err
	}
	return e.settlePR(ctx, wi, b, a, got)
}

// settlePR records the PR a turn was synced to. A PR against another base
// branch is not the factory's, and a closed, unmerged one was closed by a
// person; either stops delivery for the work item, with a comment, and it
// is not polled.
func (e *Engine) settlePR(ctx context.Context, wi *store.WorkItem, b *Binding, a *store.Attempt, got *github.PullRequest) (*store.PullRequest, error) {
	if got.BaseRef != "" && got.BaseRef != b.BaseBranch {
		e.m.PullRequests.WithLabelValues("stopped").Inc()
		return nil, e.stopWork(ctx, wi, fmt.Sprintf("PR #%d (%s) targets %s, not %s", got.Number, got.URL, got.BaseRef, b.BaseBranch))
	}
	pr, err := e.recordPR(ctx, wi, a, got)
	if err != nil {
		return nil, err
	}
	if got.State == "closed" && !got.Merged {
		e.m.PullRequests.WithLabelValues("stopped").Inc()
		return nil, e.stopWork(ctx, wi, fmt.Sprintf("PR #%d (%s) was closed; reopen it and start a new run to resume", got.Number, got.URL))
	}
	return pr, nil
}

// stopWork stops delivery for a work item; dispatch then cancels its review
// rounds, each pending until kagent confirms.
func (e *Engine) stopWork(ctx context.Context, wi *store.WorkItem, reason string) error {
	stopped, err := e.st.StopWork(ctx, wi.ID, reason)
	if err == nil && stopped {
		e.log.InfoContext(ctx, "delivery stopped", "work_item", wi.ID, "reason", reason)
	}
	return err
}

// issueStopped reports why the work item's issue no longer wants delivery:
// it left the binding's project, or sits in a completed or canceled column.
// Moving the issue is how a person cancels a review round in itsaplan.
func (e *Engine) issueStopped(ctx context.Context, wi *store.WorkItem, b *Binding) (string, error) {
	if err := e.guard(ctx); err != nil {
		return "", err
	}
	is, err := e.its.GetIssue(ctx, wi.IssueID)
	switch {
	case errors.Is(err, itsaplan.ErrNotFound):
		return "the issue is no longer visible in project " + b.ItsaplanProject, nil
	case err != nil:
		return "", err
	}
	if i := strings.LastIndex(is.Identifier, "-"); i > 0 && is.Identifier[:i] != b.ItsaplanProject {
		return "the issue moved to another project", nil
	}
	if err := e.guard(ctx); err != nil {
		return "", err
	}
	cols, err := e.its.ProjectColumns(ctx, b.ItsaplanProject)
	if err != nil {
		return "", err
	}
	for _, c := range cols {
		if c.ID == is.ColumnID && (c.StateType == "completed" || c.StateType == "canceled") {
			return fmt.Sprintf("the issue is in the %s column %q", c.StateType, c.Name), nil
		}
	}
	return "", nil
}

// checkRound is a review round's heartbeat: its issue must still want
// delivery and its PR must be open, or delivery stops and the round is
// canceled.
func (e *Engine) checkRound(ctx context.Context, r *store.Run) error {
	now := e.cfg.Now()
	wi, err := e.st.GetWorkItem(ctx, r.WorkItemID)
	if err != nil {
		return err
	}
	b := e.binding(wi.BindingName)
	if wi.State == store.WorkStopped || b == nil {
		return e.st.SetHeartbeat(ctx, r.ID, now) // step cancels or fails the round
	}
	reason, err := e.issueStopped(ctx, wi, b)
	if err != nil {
		return err
	}
	if reason == "" && e.gh != nil {
		pr, err := e.st.PullRequestFor(ctx, wi.ID)
		if err != nil {
			return err
		}
		if pr != nil {
			if err := e.guard(ctx); err != nil {
				return err
			}
			d, err := e.gh.PullRequestDetail(ctx, b.repo(), pr.Number)
			if err != nil {
				return err
			}
			if d.State != "open" && !d.Merged {
				reason = fmt.Sprintf("PR #%d (%s) was closed", pr.Number, pr.URL)
			}
		}
	}
	if err := e.st.SetHeartbeat(ctx, r.ID, now); err != nil {
		return err
	}
	if reason == "" {
		return nil
	}
	return e.stopWork(ctx, wi, reason)
}

// followUp handles a turn that finished after the work item's PR merged. Its
// commits are not delivered: the issue says that follow-up work needs a new
// issue, so one issue maps to one merged PR.
func (e *Engine) followUp(ctx context.Context, wi *store.WorkItem, b *Binding, pr *store.PullRequest) error {
	a, _, err := e.st.LatestTurn(ctx, wi.ID)
	if err != nil || a == nil {
		return err
	}
	ahead := 0
	if pr != nil && pr.HeadSHA != "" {
		if err := e.guard(ctx); err != nil {
			return err
		}
		cmp, err := e.gh.Compare(ctx, b.repo(), pr.HeadSHA, wi.Branch)
		switch {
		case errors.Is(err, github.ErrNotFound):
		case err != nil:
			return err
		default:
			ahead = cmp.AheadBy
		}
	}
	return e.st.Tx(ctx, func(tx *sql.Tx) error {
		if ahead > 0 {
			if err := e.st.Enqueue(ctx, tx, store.OutboxComment, fmt.Sprintf("comment/follow-up/%d/%d", wi.ID, a.ID), 0, "",
				commentPayload{IssueID: wi.IssueID, Body: fmt.Sprintf(
					"%s already merged in PR #%d (%s). The %d commits a later turn pushed to %s are not delivered: "+
						"open a new issue for follow-up work.", wi.IssueKey, pr.Number, pr.URL, ahead, wi.Branch)}); err != nil {
				return err
			}
		}
		return e.st.MarkSynced(ctx, tx, wi.ID, a.ID)
	})
}

// findOrOpenPR returns the branch's PR, opening it when GitHub has none. A
// failed open looks again: the PR may exist although its response was lost.
func (e *Engine) findOrOpenPR(ctx context.Context, repo string, wi *store.WorkItem, b *Binding, cmp *github.Comparison, body string) (*github.PullRequest, error) {
	if err := e.guard(ctx); err != nil {
		return nil, err
	}
	found, err := e.gh.FindPullRequest(ctx, repo, wi.Branch)
	if err != nil {
		return nil, err
	}
	if found != nil {
		return e.adoptPR(ctx, repo, b, found, body)
	}
	title := cmp.HeadSubject
	if title == "" {
		title = wi.IssueKey + ": factory delivery"
	}
	if err := e.guard(ctx); err != nil {
		return nil, err
	}
	created, err := e.gh.CreatePullRequest(ctx, repo, github.NewPullRequest{
		Title: title, Head: wi.Branch, Base: b.BaseBranch, Body: body,
	})
	if err == nil {
		e.m.PullRequests.WithLabelValues("created").Inc()
		return created, nil
	}
	e.log.WarnContext(ctx, "opening the PR failed; looking it up", "work_item", wi.ID, "error", err)
	if gerr := e.guard(ctx); gerr != nil {
		return nil, gerr
	}
	found, ferr := e.gh.FindPullRequest(ctx, repo, wi.Branch)
	if ferr != nil || found == nil {
		return nil, err
	}
	return e.adoptPR(ctx, repo, b, found, body)
}

// adoptPR takes a PR the connector did not just open: an open one against
// the binding's base gets the current body. A merged, closed or foreign one
// is returned as found, for settlePR to judge.
func (e *Engine) adoptPR(ctx context.Context, repo string, b *Binding, pr *github.PullRequest, body string) (*github.PullRequest, error) {
	e.m.PullRequests.WithLabelValues("adopted").Inc()
	if pr.Merged || pr.State != "open" || (pr.BaseRef != "" && pr.BaseRef != b.BaseBranch) {
		return pr, nil
	}
	if err := e.guard(ctx); err != nil {
		return nil, err
	}
	return e.gh.UpdatePullRequest(ctx, repo, pr.Number, github.PullRequestUpdate{Body: &body})
}

func (e *Engine) recordPR(ctx context.Context, wi *store.WorkItem, a *store.Attempt, got *github.PullRequest) (*store.PullRequest, error) {
	state := got.State
	if got.Merged {
		state = "merged"
	}
	var pr *store.PullRequest
	err := e.st.Tx(ctx, func(tx *sql.Tx) error {
		var err error
		pr, err = e.st.RecordPullRequest(ctx, tx, store.PullRequest{
			WorkItemID: wi.ID, Number: got.Number, NodeID: got.NodeID, URL: got.URL, HeadSHA: got.HeadSHA, State: state,
		})
		if err != nil {
			return err
		}
		return e.st.MarkSynced(ctx, tx, wi.ID, a.ID)
	})
	return pr, err
}

// expand fills a link template.
func (e *Engine) expand(tmpl string, wi *store.WorkItem, runID int64, session string) string {
	if tmpl == "" {
		return ""
	}
	return strings.NewReplacer(
		"{issue}", wi.IssueKey, "{issueId}", strconv.FormatInt(wi.IssueID, 10),
		"{run}", strconv.FormatInt(runID, 10), "{namespace}", wi.KagentNamespace,
		"{agent}", wi.KagentAgent, "{session}", session,
	).Replace(tmpl)
}

func mdLink(text, url string) string {
	if url == "" {
		return text
	}
	return "[" + text + "](" + url + ")"
}

// prBody links the PR to the issue, the run, the session and the delivery
// attempt of the latest finished turn.
func (e *Engine) prBody(ctx context.Context, wi *store.WorkItem, a *store.Attempt, r *store.Run) string {
	var s strings.Builder
	fmt.Fprintf(&s, "Factory delivery for %s.\n\n", wi.IssueKey)
	fmt.Fprintf(&s, "- Issue: %s\n", mdLink(wi.IssueKey, e.expand(e.cfg.Links.Issue, wi, 0, "")))
	if r.Connector() {
		fmt.Fprintf(&s, "- Run: review round %d", wi.ReviewRounds)
		if id := e.lastItsaplanRun(ctx, wi.ID); id != 0 {
			fmt.Fprintf(&s, ", after %s", mdLink("itsaplan run "+strconv.FormatInt(id, 10), e.expand(e.cfg.Links.Run, wi, id, "")))
		}
		s.WriteString("\n")
	} else {
		fmt.Fprintf(&s, "- Run: %s (itsaplan attempt %d)\n",
			mdLink("itsaplan run "+strconv.FormatInt(r.ID, 10), e.expand(e.cfg.Links.Run, wi, r.ID, "")), r.Attempt)
	}
	fmt.Fprintf(&s, "- Session: %s\n", mdLink(a.ContextID, e.expand(e.cfg.Links.Session, wi, 0, a.ContextID)))
	fmt.Fprintf(&s, "- Attempt: `%s` (delivery attempt %d)\n", a.MessageID, a.Seq)
	fmt.Fprintf(&s, "\nThe issue becomes Done when this pull request merges.\n\n<!-- factory-connector work-item=%d -->\n", wi.ID)
	return s.String()
}

func (e *Engine) lastItsaplanRun(ctx context.Context, workID int64) int64 {
	var id int64
	_ = e.st.DB.QueryRowContext(ctx,
		`SELECT coalesce(max(id), 0) FROM run WHERE work_item_id = $1 AND origin = 'itsaplan'`, workID).Scan(&id)
	return id
}

// pollPR reads the PR and acts on what changed: the merge, a merge queue
// ejection, review events on the head, or a head ready to enqueue.
func (e *Engine) pollPR(ctx context.Context, wi *store.WorkItem, b *Binding, pr *store.PullRequest) error {
	repo := b.repo()
	if err := e.guard(ctx); err != nil {
		return err
	}
	d, err := e.gh.PullRequestDetail(ctx, repo, pr.Number)
	if err != nil {
		return err
	}
	state := d.State
	if d.Merged {
		state = "merged"
	}
	if err := e.st.SetPolled(ctx, pr.ID, d.HeadSHA, state, d.MergeQueueState, e.cfg.Now()); err != nil {
		return err
	}
	pr.HeadSHA, pr.State, pr.MergeQueueState = d.HeadSHA, state, d.MergeQueueState
	switch {
	case d.Merged:
		return e.merged(ctx, wi, b, pr, d)
	case d.State != "open":
		// Closed by a person, queued or not: delivery stops until a new run.
		return e.stopWork(ctx, wi, fmt.Sprintf("PR #%d (%s) was closed without merging", pr.Number, pr.URL))
	case wi.State == store.WorkMerging && d.MergeQueueState != "":
		return nil
	case wi.State == store.WorkMerging:
		// The queue entry can go before the merge shows; look again before
		// calling it an ejection.
		if err := e.guard(ctx); err != nil {
			return err
		}
		again, err := e.gh.PullRequestDetail(ctx, repo, pr.Number)
		switch {
		case err != nil:
			return err
		case again.Merged:
			return e.merged(ctx, wi, b, pr, again)
		case again.MergeQueueState != "":
			return nil
		}
		return e.ejected(ctx, wi, pr)
	case d.MergeQueueState != "":
		// Queued although the enqueue's answer was lost.
		return e.st.MarkEnqueued(ctx, e.st.DB, pr, d.HeadSHA, d.MergeQueueState)
	}
	if err := e.guard(ctx); err != nil {
		return err
	}
	checks, err := e.gh.CheckRuns(ctx, repo, d.HeadSHA)
	if err != nil {
		return err
	}
	if err := e.guard(ctx); err != nil {
		return err
	}
	statuses, err := e.gh.Statuses(ctx, repo, d.HeadSHA)
	if err != nil {
		return err
	}
	started, err := e.reviewEvents(ctx, wi, b, pr, d, checks, statuses)
	if err != nil || started {
		return err
	}
	if !gateOpen(&b.Review, b.BaseBranch, pr, d, checks, statuses) {
		return nil
	}
	if err := e.guard(ctx); err != nil {
		return err
	}
	qs, err := e.gh.EnqueuePullRequest(ctx, repo, pr.NodeID, d.HeadSHA)
	if err != nil {
		e.m.MergeQueue.WithLabelValues("enqueue_error").Inc()
		return fmt.Errorf("enqueue PR %d at %s: %w", pr.Number, d.HeadSHA, err)
	}
	e.m.MergeQueue.WithLabelValues("enqueued").Inc()
	e.log.InfoContext(ctx, "PR enqueued", "work_item", wi.ID, "pr", pr.Number, "head", d.HeadSHA)
	return e.st.MarkEnqueued(ctx, e.st.DB, pr, d.HeadSHA, qs)
}

// failedConclusion reports a check run conclusion that asks for changes.
func failedConclusion(c string) bool {
	switch c {
	case "failure", "timed_out", "action_required":
		return true
	}
	return false
}

type candidate struct {
	ev      store.ReviewEvent
	trusted bool
	text    string
}

// reviewEvents records the PR's events that ask for changes, once each by
// GitHub node ID. An event acts only when its commit is the current head and
// its author is trusted, and once per head, kind and source; acting events
// start one review round in the work item's session, within the binding's
// bound. started reports a new round.
func (e *Engine) reviewEvents(ctx context.Context, wi *store.WorkItem, b *Binding, pr *store.PullRequest,
	d *github.PullRequestDetail, checks []github.CheckRun, statuses []github.Status,
) (started bool, err error) {
	p := &b.Review
	var cands []candidate
	for _, rv := range d.Reviews {
		if rv.State != "CHANGES_REQUESTED" {
			continue
		}
		cands = append(cands, candidate{
			ev: store.ReviewEvent{
				NodeID: rv.NodeID, CommitSHA: rv.CommitSHA, Author: rv.Author,
				Kind: store.EventChangesRequested, Source: strings.ToLower(rv.Author),
			},
			trusted: p.trustedReviewer(rv.Author),
			text:    fmt.Sprintf("%s requested changes:\n\n%s", rv.Author, clip(rv.Body, 4000)),
		})
	}
	for _, c := range checks {
		if c.Status != "completed" || !failedConclusion(c.Conclusion) {
			continue
		}
		named, trusted := false, false
		for _, want := range p.checks() {
			if want.Name == c.Name && want.AppID != 0 {
				named = true
				trusted = trusted || want.AppID == c.AppID
			}
		}
		if !named {
			continue
		}
		cands = append(cands, candidate{
			ev: store.ReviewEvent{
				NodeID: c.NodeID, CommitSHA: c.HeadSHA, Author: "app/" + strconv.FormatInt(c.AppID, 10),
				Kind: store.EventCheckFailed, Source: c.Name,
			},
			trusted: trusted,
			text: fmt.Sprintf("Check %q concluded %s (%s): %s\n\n%s", c.Name, c.Conclusion, c.URL, c.Title,
				clip(c.Summary, 4000)),
		})
	}
	for _, st := range statuses {
		if st.State != "failure" && st.State != "error" {
			continue
		}
		named, trusted := false, false
		for _, want := range p.checks() {
			if want.Name == st.Context && want.StatusCreator != "" {
				named = true
				trusted = trusted || strings.EqualFold(want.StatusCreator, st.Creator)
			}
		}
		if !named {
			continue
		}
		cands = append(cands, candidate{
			ev: store.ReviewEvent{
				NodeID: st.NodeID, CommitSHA: st.SHA, Author: st.Creator, Kind: store.EventStatusFailed, Source: st.Context,
			},
			trusted: trusted,
			text:    fmt.Sprintf("Status %q is %s (%s): %s", st.Context, st.State, st.URL, st.Description),
		})
	}
	ids := make([]string, 0, len(cands))
	for _, c := range cands {
		ids = append(ids, c.ev.NodeID)
	}
	seen, err := e.st.SeenEvents(ctx, ids)
	if err != nil {
		return false, err
	}
	var record, acting []*candidate
	for i := range cands {
		c := &cands[i]
		if seen[c.ev.NodeID] {
			continue // deduplicated on its node ID
		}
		c.ev.PullRequestID = pr.ID
		seen[c.ev.NodeID] = true
		switch {
		case c.ev.CommitSHA != d.HeadSHA:
			c.ev.Disposition = store.EventStaleHead
		case !c.trusted:
			c.ev.Disposition = store.EventUntrusted
		default:
			done, err := e.st.ActedOn(ctx, pr.ID, c.ev.CommitSHA, c.ev.Kind, c.ev.Source)
			if err != nil {
				return false, err
			}
			if done {
				c.ev.Disposition = store.EventDuplicate
			} else {
				c.ev.Disposition = store.EventActed
				acting = append(acting, c)
			}
		}
		record = append(record, c)
	}
	if len(record) == 0 {
		return false, nil
	}
	limited := len(acting) > 0 && wi.ReviewRounds >= p.MaxReviewRounds
	if limited {
		for _, c := range acting {
			c.ev.Disposition = store.EventRoundLimit
		}
	}
	var prompt string
	if len(acting) > 0 && !limited {
		// The issue must still want delivery before a round starts.
		reason, err := e.issueStopped(ctx, wi, b)
		if err != nil {
			return false, err
		}
		if reason != "" {
			return false, e.stopWork(ctx, wi, reason)
		}
		prompt = roundPrompt(p, wi, pr, d, acting)
	}
	var round *store.Run
	err = e.st.Tx(ctx, func(tx *sql.Tx) error {
		if prompt != "" {
			var err error
			if round, err = e.st.StartRound(ctx, tx, wi, prompt); err != nil {
				return err
			}
			for _, c := range acting {
				c.ev.RoundRunID = round.ID
			}
			if err := e.st.Enqueue(ctx, tx, store.OutboxComment, fmt.Sprintf("comment/round/%d/%d", wi.ID, wi.ReviewRounds+1), round.ID, "",
				commentPayload{IssueID: wi.IssueID, Body: fmt.Sprintf(
					"The factory started review round %d of %d for %s on PR #%d (%s) at %s, in kagent session %s.",
					wi.ReviewRounds+1, p.MaxReviewRounds, wi.IssueKey, pr.Number, pr.URL, short(d.HeadSHA), wi.SessionID)}); err != nil {
				return err
			}
		}
		if limited {
			if err := e.st.Enqueue(ctx, tx, store.OutboxComment, fmt.Sprintf("comment/round-limit/%d", wi.ID), 0, "",
				commentPayload{IssueID: wi.IssueID, Body: fmt.Sprintf(
					"%s reached its bound of %d review rounds; PR #%d (%s) waits for a person.",
					wi.IssueKey, p.MaxReviewRounds, pr.Number, pr.URL)}); err != nil {
				return err
			}
		}
		for _, c := range record {
			if _, err := e.st.RecordEvent(ctx, tx, c.ev); err != nil {
				return err
			}
		}
		return nil
	})
	if err != nil {
		return false, err
	}
	for _, c := range record {
		e.m.ReviewEvents.WithLabelValues(c.ev.Kind, c.ev.Disposition).Inc()
	}
	if round == nil {
		return false, nil
	}
	e.m.ReviewRounds.Inc()
	e.log.InfoContext(ctx, "review round started", "work_item", wi.ID, "run", round.ID, "round", wi.ReviewRounds+1)
	return true, e.stepLocked(ctx, round.ID)
}

// roundPrompt asks for a revision: what requested changes, and the PR's
// unresolved review threads from trusted authors.
func roundPrompt(p *ReviewPolicy, wi *store.WorkItem, pr *store.PullRequest, d *github.PullRequestDetail, acting []*candidate) string {
	var s strings.Builder
	fmt.Fprintf(&s, "Review round %d for %s. Pull request #%d (%s) at %s on %s needs changes. "+
		"Address the feedback below, push the fixes to %s, and say what you changed or why you did not.\n",
		wi.ReviewRounds+1, wi.IssueKey, pr.Number, pr.URL, d.HeadSHA, wi.Branch, wi.Branch)
	s.WriteString("\n### Feedback\n")
	for _, c := range acting {
		fmt.Fprintf(&s, "\n%s\n", c.text)
	}
	writeThreads(&s, d.Threads, p)
	return s.String()
}

// writeThreads appends the unresolved review threads, keeping only comments
// by trusted reviewer logins. The repository is public, so anyone can
// comment; the rest never reaches an agent holding a write token, and the
// text says how many comments were left out.
func writeThreads(s *strings.Builder, threads []github.ReviewThread, p *ReviewPolicy) {
	n, omitted := 0, 0
	for _, t := range threads {
		if t.Resolved {
			continue
		}
		var kept []github.ReviewComment
		for _, c := range t.Comments {
			if p.trustedReviewer(c.Author) {
				kept = append(kept, c)
			} else {
				omitted++
			}
		}
		if len(kept) == 0 {
			continue
		}
		if n == 0 {
			s.WriteString("\n### Unresolved review threads\n")
		}
		n++
		where := t.Path
		if t.Line > 0 {
			where += ":" + strconv.Itoa(t.Line)
		}
		if t.Outdated {
			where += " (outdated)"
		}
		fmt.Fprintf(s, "\n- %s\n", where)
		for _, c := range kept {
			fmt.Fprintf(s, "  - %s: %s\n", c.Author, strings.ReplaceAll(clip(c.Body, 2000), "\n", "\n    "))
		}
	}
	if omitted > 0 {
		fmt.Fprintf(s, "\n%d review comments from authors this repository binding does not trust were omitted.\n", omitted)
	}
}

// gateOpen reports whether the head may enter the merge queue: every
// required check and the reviewer succeeded on it from trusted sources, no
// trusted reviewer's latest decision requests changes, and the queue has not
// already sent this head back.
func gateOpen(p *ReviewPolicy, base string, pr *store.PullRequest, d *github.PullRequestDetail, checks []github.CheckRun, statuses []github.Status) bool {
	if d.Draft || d.HeadSHA == "" || d.HeadSHA == pr.EjectedHeadSHA || d.BaseRef != base {
		return false
	}
	if p.ReviewerCheck == nil && len(p.TrustedReviewers) == 0 {
		return false // nothing independent reviews the head
	}
	for _, c := range p.RequiredChecks {
		if !succeeded(c, d.HeadSHA, checks, statuses) {
			return false
		}
	}
	if p.ReviewerCheck != nil && !succeeded(*p.ReviewerCheck, d.HeadSHA, checks, statuses) {
		return false
	}
	latest := map[string]github.Review{}
	for _, rv := range d.Reviews { // oldest first
		if !p.trustedReviewer(rv.Author) {
			continue
		}
		switch rv.State {
		case "APPROVED", "CHANGES_REQUESTED", "DISMISSED":
			latest[strings.ToLower(rv.Author)] = rv
		}
	}
	approved := false
	for _, rv := range latest {
		switch {
		case rv.State == "CHANGES_REQUESTED":
			return false
		case rv.State == "APPROVED" && rv.CommitSHA == d.HeadSHA:
			approved = true
		}
	}
	return p.ReviewerCheck != nil || approved
}

func succeeded(c Check, head string, checks []github.CheckRun, statuses []github.Status) bool {
	if c.AppID != 0 {
		for _, r := range checks {
			if r.Name == c.Name && r.AppID == c.AppID && r.HeadSHA == head && r.Status == "completed" && r.Conclusion == "success" {
				return true
			}
		}
	}
	if c.StatusCreator != "" {
		for _, s := range statuses {
			if s.Context == c.Name && strings.EqualFold(s.Creator, c.StatusCreator) && s.SHA == head && s.State == "success" {
				return true
			}
		}
	}
	return false
}

// ejected returns a work item whose PR left the merge queue unmerged to
// review. The head the queue rejected is not enqueued again.
func (e *Engine) ejected(ctx context.Context, wi *store.WorkItem, pr *store.PullRequest) error {
	e.m.MergeQueue.WithLabelValues("ejected").Inc()
	head := pr.EnqueuedHeadSHA
	return e.st.Tx(ctx, func(tx *sql.Tx) error {
		if err := e.st.MarkEjected(ctx, tx, pr); err != nil {
			return err
		}
		return e.st.Enqueue(ctx, tx, store.OutboxComment, fmt.Sprintf("comment/ejected/%d/%s", pr.ID, head), 0, "",
			commentPayload{IssueID: wi.IssueID, Body: fmt.Sprintf(
				"PR #%d (%s) left the merge queue without merging at %s; %s is back in review. "+
					"The factory does not enqueue that head again: a new push or review moves it on.",
				pr.Number, pr.URL, short(head), wi.IssueKey)})
	})
}

type issueDonePayload struct {
	IssueID int64  `json:"issueId"`
	Project string `json:"project"`
	Column  string `json:"column,omitempty"`
}

// merged records the observed merge. The itsaplan issue becomes Done, with
// a comment linking the issue, run, session, PR and merge, only from here.
func (e *Engine) merged(ctx context.Context, wi *store.WorkItem, b *Binding, pr *store.PullRequest, d *github.PullRequestDetail) error {
	at := d.MergedAt
	if at.IsZero() {
		at = e.cfg.Now()
	}
	runID := e.lastItsaplanRun(ctx, wi.ID)
	merge := d.MergeCommitSHA
	mergeLink := short(merge)
	if merge != "" {
		mergeLink = mdLink(short(merge), "https://github.com/"+b.repo()+"/commit/"+merge)
	}
	body := fmt.Sprintf("Merged: %s is done.\n\n- Issue: %s\n- Run: %s\n- Session: %s\n- Pull request: %s\n- Merge: %s\n",
		wi.IssueKey,
		mdLink(wi.IssueKey, e.expand(e.cfg.Links.Issue, wi, 0, "")),
		mdLink("itsaplan run "+strconv.FormatInt(runID, 10), e.expand(e.cfg.Links.Run, wi, runID, "")),
		mdLink(wi.SessionID, e.expand(e.cfg.Links.Session, wi, 0, wi.SessionID)),
		mdLink("#"+strconv.Itoa(pr.Number), pr.URL), mergeLink)
	err := e.st.Tx(ctx, func(tx *sql.Tx) error {
		if err := e.st.MarkMerged(ctx, tx, pr, merge, at); err != nil {
			return err
		}
		if err := e.st.Enqueue(ctx, tx, store.OutboxComment, fmt.Sprintf("comment/merged/%d", wi.ID), 0, "",
			commentPayload{IssueID: wi.IssueID, Body: body}); err != nil {
			return err
		}
		return e.st.Enqueue(ctx, tx, store.OutboxIssueDone, fmt.Sprintf("issue-done/%d", wi.ID), 0, "",
			issueDonePayload{IssueID: wi.IssueID, Project: b.ItsaplanProject, Column: b.Review.DoneColumn})
	})
	if err == nil {
		e.m.MergeQueue.WithLabelValues("merged").Inc()
		e.log.InfoContext(ctx, "merge observed", "work_item", wi.ID, "pr", pr.Number, "merge", merge)
	}
	return err
}

// deliverIssueDone moves a merged work item's issue to the completed column.
func (e *Engine) deliverIssueDone(ctx context.Context, it *store.OutboxItem) error {
	var p issueDonePayload
	if err := json.Unmarshal(it.Payload, &p); err != nil {
		return e.st.OutboxAbandon(ctx, e.st.DB, it.ID, err.Error())
	}
	if err := e.guard(ctx); err != nil {
		return err
	}
	cols, err := e.its.ProjectColumns(ctx, p.Project)
	switch {
	case terminalHTTP(err):
		return e.st.OutboxAbandon(ctx, e.st.DB, it.ID, clip(err.Error(), maxErrorText))
	case err != nil:
		return err
	}
	var done *itsaplan.Column
	for i := range cols {
		c := &cols[i]
		match := c.StateType == "completed"
		if p.Column != "" {
			match = strings.EqualFold(c.Name, p.Column)
		}
		if match && (done == nil || c.Position < done.Position) {
			done = c
		}
	}
	if done == nil {
		return e.st.OutboxAbandon(ctx, e.st.DB, it.ID, "project "+p.Project+" has no done column")
	}
	if err := e.guard(ctx); err != nil {
		return err
	}
	err = e.its.MoveIssue(ctx, p.IssueID, done.ID)
	switch {
	case err == nil:
		return e.st.OutboxDelivered(ctx, e.st.DB, it.ID)
	case errors.Is(err, itsaplan.ErrConflict):
		// The Done column is at a hard WIP limit. Say so once and keep
		// trying with the outbox's backoff.
		if cerr := e.doneRefused(ctx, p, done.Name, "its WIP limit is reached; the factory keeps trying"); cerr != nil {
			return cerr
		}
		return err
	case terminalHTTP(err):
		if cerr := e.doneRefused(ctx, p, done.Name, "itsaplan refused the move: "+clip(err.Error(), 200)); cerr != nil {
			return cerr
		}
		return e.st.OutboxAbandon(ctx, e.st.DB, it.ID, clip(err.Error(), maxErrorText))
	}
	return err
}

// doneRefused says on the issue, once, that it could not be moved to Done.
func (e *Engine) doneRefused(ctx context.Context, p issueDonePayload, column, why string) error {
	return e.st.Enqueue(ctx, e.st.DB, store.OutboxComment, fmt.Sprintf("comment/done-refused/%d", p.IssueID), 0, "",
		commentPayload{IssueID: p.IssueID, Body: fmt.Sprintf(
			"The pull request merged, but the factory could not move this issue to %q: %s. Move it by hand if it stays.",
			column, why)})
}

// replacementContext is what a replacement session starts from: the issue,
// the PR, the branch head and the open review threads.
func (e *Engine) replacementContext(ctx context.Context, wi *store.WorkItem, b *Binding, old, why string) (string, error) {
	var s strings.Builder
	fmt.Fprintf(&s, "## Replacement session\nThe factory replaced kagent session %s (%s). Nothing from it carries over: "+
		"continue the work from this context and the repository.\n", old, why)
	if err := e.guard(ctx); err != nil {
		return "", err
	}
	issue, err := e.its.GetIssue(ctx, wi.IssueID)
	switch {
	case err == nil:
		fmt.Fprintf(&s, "\n### Issue %s: %s\n\n%s\n", wi.IssueKey, issue.Title, clip(issue.Description, 8000))
	case terminalHTTP(err):
		fmt.Fprintf(&s, "\n### Issue %s\n\n(unavailable: %v)\n", wi.IssueKey, err)
	default:
		return "", err
	}
	if e.gh == nil {
		return s.String(), nil
	}
	repo := b.repo()
	pr, err := e.st.PullRequestFor(ctx, wi.ID)
	if err != nil {
		return "", err
	}
	if pr == nil {
		if err := e.guard(ctx); err != nil {
			return "", err
		}
		cmp, err := e.gh.Compare(ctx, repo, b.BaseBranch, wi.Branch)
		switch {
		case errors.Is(err, github.ErrNotFound) || (err == nil && cmp.AheadBy == 0):
			fmt.Fprintf(&s, "\n### Branch\n\n%s has no commits ahead of %s yet.\n", wi.Branch, b.BaseBranch)
		case err != nil:
			return "", err
		default:
			fmt.Fprintf(&s, "\n### Branch\n\n%s is %d commits ahead of %s; its head is %s.\n", wi.Branch, cmp.AheadBy, b.BaseBranch, cmp.HeadSHA)
		}
		return s.String(), nil
	}
	if err := e.guard(ctx); err != nil {
		return "", err
	}
	d, err := e.gh.PullRequestDetail(ctx, repo, pr.Number)
	if err != nil {
		return "", err
	}
	fmt.Fprintf(&s, "\n### Pull request\n\n#%d %s (%s, %s). Branch %s is at %s.\n",
		d.Number, d.URL, d.Title, d.State, wi.Branch, d.HeadSHA)
	writeThreads(&s, d.Threads, &b.Review)
	return s.String(), nil
}

func short(sha string) string {
	if len(sha) > 12 {
		return sha[:12]
	}
	return sha
}
