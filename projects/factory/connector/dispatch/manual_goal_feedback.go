package dispatch

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"strconv"
	"strings"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/github"
	"github.com/jomcgi/homelab/projects/factory/connector/itsaplan"
	"github.com/jomcgi/homelab/projects/factory/connector/store"
)

// ManualGoalRequest identifies an original operator comment and the exact
// maintainer PR head it requests feedback for.
type ManualGoalRequest struct {
	Repository  string `json:"repository"`
	Issue       string `json:"issue"`
	PullRequest int    `json:"pullRequest"`
	Head        string `json:"head"`
	CommentID   int64  `json:"commentId"`
}

var ErrManualGoalUnavailable = errors.New("manual goal feedback unavailable")

const (
	manualGoalOutboxKind = "manual_goal_feedback"
	manualGoalStale      = "manual feedback is no longer current"
)

// A request can use a fresh assessment after its previous one was withdrawn
// before sending. A rollback can also reject the kind without sending it.
const manualGoalRenewable = `outbox.delivered_at IS NULL AND outbox.send_started_at IS NULL
	AND outbox.abandoned_at IS NOT NULL AND coalesce(outbox.last_error, '') IN ($2, $3)`

type manualGoalPlan interface {
	GetProject(context.Context, string) (*itsaplan.Project, error)
	GetIssueByNumber(context.Context, string, int64) (*itsaplan.Issue, error)
	LinkedComment(context.Context, int64, int64) (*itsaplan.FeedItem, error)
	RecentComments(context.Context, int64) ([]itsaplan.FeedItem, error)
}

type manualGoalGitHub interface {
	GetPullRequest(context.Context, string, int) (*github.PullRequest, error)
	ListPullRequests(context.Context, string, string) ([]github.PullRequest, bool, error)
	ListCheckRuns(context.Context, string, string, github.CheckRunFilter) ([]github.CheckRun, error)
}

type ManualGoalFeedback struct {
	Bindings []Binding
	Plan     manualGoalPlan
	GitHub   manualGoalGitHub
	Reasons  GoalReasonsSource
	Seen     func(context.Context, string) (bool, error)
	Queue    func(context.Context, int64, string, string, manualGoalProof) error
}

type manualGoalProof struct {
	Request       ManualGoalRequest `json:"request"`
	CheckID       int64             `json:"checkId"`
	Digest        string            `json:"digest"`
	ReplyRevision int64             `json:"replyRevision"`
}

type manualGoalAssessment struct {
	github.GoalAssessment
	checkID int64
}

// This kind and payload are separate from generic comments. An older binary
// does not know how to revalidate their proof and must never post them.
type manualGoalDelivery struct {
	IssueID    int64           `json:"issueId"`
	Body       string          `json:"body"`
	Proof      manualGoalProof `json:"proof"`
	Assessment string          `json:"assessment"`
}

func (p manualGoalProof) assessmentKey(issueID int64) string {
	return fmt.Sprintf("%d/%s/%d/%s/%d/%s/%d", issueID, p.Request.Repository, p.Request.PullRequest,
		p.Request.Head, p.CheckID, p.Digest, p.ReplyRevision)
}

func (h *ManualGoalFeedback) Request(ctx context.Context, in ManualGoalRequest) error {
	if in.CommentID < 1 || in.PullRequest < 1 || !baseCommit.MatchString(in.Head) ||
		h.Reasons == nil || h.Plan == nil || h.GitHub == nil || h.Queue == nil {
		return ErrManualGoalUnavailable
	}
	b := h.binding(in)
	if b == nil {
		return ErrManualGoalUnavailable
	}
	issue, err := h.authorize(ctx, b, in)
	if err != nil {
		return err
	}
	return h.deliver(ctx, b, in, issue)
}

func (h *ManualGoalFeedback) binding(in ManualGoalRequest) *Binding {
	for i := range h.Bindings {
		b := &h.Bindings[i]
		if b.repo() == in.Repository && manualIssueNumber(b.ItsaplanProject, in.Issue) > 0 {
			return b
		}
	}
	return nil
}

func manualIssueNumber(project, key string) int64 {
	number, ok := strings.CutPrefix(key, project+"-")
	n, err := strconv.ParseInt(number, 10, 64)
	if !ok || project == "" || n < 1 || err != nil || strconv.FormatInt(n, 10) != number {
		return 0
	}
	return n
}

func (in ManualGoalRequest) comment() string {
	return fmt.Sprintf("Request goal feedback for %s#%d@%s", in.Repository, in.PullRequest, in.Head)
}

func (h *ManualGoalFeedback) authorize(ctx context.Context, b *Binding, in ManualGoalRequest) (int64, error) {
	project, err := h.Plan.GetProject(ctx, b.ItsaplanProject)
	if err != nil {
		return 0, err
	}
	issue, err := h.ticket(ctx, b, in)
	if err != nil {
		return 0, err
	}
	if project == nil || project.Project.Key != b.ItsaplanProject || project.Project.ID < 1 || issue.ProjectID != project.Project.ID {
		return 0, ErrManualGoalUnavailable
	}
	comment, err := h.Plan.LinkedComment(ctx, issue.ID, in.CommentID)
	if err != nil {
		return 0, err
	}
	if comment == nil || comment.ID != in.CommentID || comment.IssueID != issue.ID || comment.Kind != "comment" ||
		!comment.OriginalComment() || strings.TrimSpace(comment.Body) != in.comment() {
		return 0, ErrManualGoalUnavailable
	}
	operator := false
	for _, a := range project.Assignees {
		if a.Kind == "member" && a.UserID != "" && a.UserID == comment.ActorUserID &&
			(a.Username == "joe" || a.Username == "joesessions") {
			operator = true
			break
		}
	}
	if !operator {
		return 0, ErrManualGoalUnavailable
	}
	return issue.ID, nil
}

func (h *ManualGoalFeedback) ticket(ctx context.Context, b *Binding, in ManualGoalRequest) (*itsaplan.Issue, error) {
	number := manualIssueNumber(b.ItsaplanProject, in.Issue)
	if number == 0 {
		return nil, ErrManualGoalUnavailable
	}
	issue, err := h.Plan.GetIssueByNumber(ctx, b.ItsaplanProject, number)
	if err != nil {
		return nil, err
	}
	if issue == nil || issue.ID < 1 || issue.Identifier != in.Issue || issue.ArchivedAt != nil {
		return nil, ErrManualGoalUnavailable
	}
	return issue, nil
}

func (h *ManualGoalFeedback) deliver(ctx context.Context, b *Binding, in ManualGoalRequest, issueID int64) error {
	assessment, err := h.currentAssessment(ctx, b, in)
	if err != nil {
		return err
	}
	key := fmt.Sprintf("manual-goal-feedback/%d/%d", issueID, in.CommentID)
	if h.Seen != nil {
		seen, err := h.Seen(ctx, key)
		if seen || err != nil {
			return err
		}
	}
	answer, err := h.Reasons.GoalReasons(ctx, in.Repository, in.PullRequest, in.Head)
	if errors.Is(err, ErrNoGoalReasons) {
		return ErrManualGoalUnavailable
	}
	if err != nil {
		return err
	}
	if answer == nil || answer.Repository != in.Repository || answer.PullRequest != in.PullRequest ||
		answer.Head != in.Head || answer.Issue != in.Issue || answer.Digest != assessment.Digest ||
		answer.ReplyRevision != assessment.ReplyRevision || strings.TrimSpace(answer.Reasons) == "" {
		return ErrManualGoalUnavailable
	}
	current, err := h.currentAssessment(ctx, b, in)
	if err != nil {
		return err
	}
	if *current != *assessment {
		return ErrManualGoalUnavailable
	}
	pr := &store.PullRequest{Number: in.PullRequest, URL: fmt.Sprintf("https://github.com/%s/pull/%d", in.Repository, in.PullRequest)}
	proof := manualGoalProof{Request: in, CheckID: assessment.checkID, Digest: answer.Digest, ReplyRevision: answer.ReplyRevision}
	return h.Queue(ctx, issueID, key, goalReasonsComment(pr, answer), proof)
}

func (h *ManualGoalFeedback) currentAssessment(ctx context.Context, b *Binding, in ManualGoalRequest) (*manualGoalAssessment, error) {
	pr, err := h.GitHub.GetPullRequest(ctx, in.Repository, in.PullRequest)
	if err != nil {
		return nil, err
	}
	if pr == nil || pr.Number != in.PullRequest || pr.HeadSHA != in.Head || !manualGoalPR(b, pr) {
		return nil, ErrManualGoalUnavailable
	}
	return h.reviewedAssessment(ctx, b, in)
}

func manualGoalPR(b *Binding, pr *github.PullRequest) bool {
	return pr.Number > 0 && baseCommit.MatchString(pr.HeadSHA) && pr.State == "open" && !pr.Draft &&
		pr.HeadRepo == b.repo() && pr.BaseRef == b.BaseBranch && !strings.HasSuffix(pr.Author, "[bot]") &&
		b.Review.trustedReviewer(pr.Author)
}

func (h *ManualGoalFeedback) reviewedAssessment(ctx context.Context, b *Binding, in ManualGoalRequest) (*manualGoalAssessment, error) {
	reviewer := b.Review.ReviewerCheck
	if reviewer == nil || reviewer.AppID == 0 || reviewer.Name == "" {
		return nil, ErrManualGoalUnavailable
	}
	checks, err := h.GitHub.ListCheckRuns(ctx, in.Repository, in.Head, github.CheckRunFilter{
		Name: reviewer.Name, AppID: reviewer.AppID, All: true,
	})
	if err != nil {
		return nil, err
	}
	expectedID := fmt.Sprintf("factory-review/%s/%d/%s", in.Repository, in.PullRequest, in.Head)
	var newest github.CheckRun
	for _, check := range checks {
		if check.HeadSHA == in.Head && check.AppID == reviewer.AppID && check.Name == reviewer.Name &&
			check.ExternalID == expectedID && check.ID > newest.ID {
			newest = check
		}
	}
	if newest.Status != "completed" || (newest.Conclusion != "success" && newest.Conclusion != "failure") {
		return nil, ErrManualGoalUnavailable
	}
	assessment := github.ReviewedGoal(newest.Text, in.Head, in.Issue)
	if assessment == nil || len(assessment.Digest) != 64 || !digestText.MatchString(assessment.Digest) {
		return nil, ErrManualGoalUnavailable
	}
	return &manualGoalAssessment{GoalAssessment: *assessment, checkID: newest.ID}, nil
}

func (h *ManualGoalFeedback) findRequests(ctx context.Context, b *Binding, pr github.PullRequest) error {
	in := ManualGoalRequest{Repository: b.repo(), PullRequest: pr.Number, Head: pr.HeadSHA}
	assessment, err := h.reviewedAssessment(ctx, b, in)
	if err != nil {
		return err
	}
	in.Issue = assessment.Issue
	issue, err := h.ticket(ctx, b, in)
	if err != nil {
		return err
	}
	comments, err := h.Plan.RecentComments(ctx, issue.ID)
	if err != nil {
		return err
	}
	for _, comment := range comments {
		if comment.Kind != "comment" || !comment.OriginalComment() || strings.TrimSpace(comment.Body) != in.comment() {
			continue
		}
		in.CommentID = comment.ID
		if err := h.Request(ctx, in); !errors.Is(err, ErrManualGoalUnavailable) {
			return err
		}
	}
	return nil
}

// EnableManualGoalFeedback supplies the writer-checked read clients before
// Tick starts. Their transports journal all wire attempts, including retries.
func (e *Engine) EnableManualGoalFeedback(plan manualGoalPlan, gh manualGoalGitHub, reasons GoalReasonsSource) {
	e.manualGoal = &ManualGoalFeedback{
		Bindings: e.cfg.Bindings, Plan: plan, GitHub: gh, Reasons: reasons,
		Seen: func(ctx context.Context, key string) (bool, error) {
			var seen bool
			err := e.st.DB.QueryRowContext(ctx, `
				SELECT EXISTS (SELECT 1 FROM outbox WHERE dedupe_key=$1 AND NOT (`+manualGoalRenewable+`))`,
				key, manualGoalStale, "unknown kind "+manualGoalOutboxKind).Scan(&seen)
			return seen, err
		},
		Queue: e.queueManualGoalFeedback,
	}
}

func (e *Engine) queueManualGoalFeedback(ctx context.Context, issueID int64, key, body string, proof manualGoalProof) error {
	data, err := json.Marshal(manualGoalDelivery{
		IssueID: issueID, Body: body, Proof: proof, Assessment: proof.assessmentKey(issueID),
	})
	if err != nil {
		return err
	}
	return e.w.Tx(ctx, func(tx *sql.Tx) error {
		_, err := tx.ExecContext(ctx, `
			INSERT INTO outbox (kind, dedupe_key, payload, next_attempt_at)
			VALUES ($4, $1, $5, $6)
			ON CONFLICT (dedupe_key) DO UPDATE SET payload=excluded.payload,
				next_attempt_at=excluded.next_attempt_at, abandoned_at=NULL, last_error=NULL,
				tries=0, unavailable_since=NULL
			WHERE outbox.kind=excluded.kind AND `+manualGoalRenewable,
			key, manualGoalStale, "unknown kind "+manualGoalOutboxKind, manualGoalOutboxKind, data, e.cfg.Now())
		return err
	})
}

func (e *Engine) deliverManualGoalFeedback(ctx context.Context, it *store.OutboxItem) error {
	var p manualGoalDelivery
	if err := json.Unmarshal(it.Payload, &p); err != nil {
		return e.st.OutboxAbandon(ctx, e.st.DB, it.ID, err.Error())
	}
	if err := e.guard(ctx); err != nil {
		return err
	}
	if err := e.verifyManualFeedback(ctx, p.IssueID, p.Proof); err != nil {
		if errors.Is(err, ErrManualGoalUnavailable) || errors.Is(err, itsaplan.ErrNotFound) {
			return e.st.OutboxAbandon(ctx, e.st.DB, it.ID, manualGoalStale)
		}
		return err
	}
	// A second request for the same assessment must not repeat a send.
	// Preserve attempts with an unknown outcome as well as confirmed sends.
	var attempted bool
	err := e.st.DB.QueryRowContext(ctx, `
		SELECT EXISTS (SELECT 1 FROM outbox WHERE kind=$1 AND id<>$2
			AND payload->>'assessment'=$3 AND (delivered_at IS NOT NULL OR send_started_at IS NOT NULL))`,
		manualGoalOutboxKind, it.ID, p.Assessment).Scan(&attempted)
	if err != nil {
		return err
	}
	if attempted {
		return e.st.OutboxAbandon(ctx, e.st.DB, it.ID, "this assessment has already been sent or attempted")
	}
	return e.sendComment(ctx, it, commentPayload{IssueID: p.IssueID, Body: p.Body, Once: true})
}

func (e *Engine) stepManualGoalFeedback(ctx context.Context) error {
	h := e.manualGoal
	if h == nil || e.cfg.Now().Sub(e.manualGoalPoll) < e.cfg.ReviewPoll {
		return nil
	}
	e.manualGoalPoll = e.cfg.Now()
	ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	for i := range h.Bindings {
		b := &h.Bindings[i]
		if b.Review.ReviewerCheck == nil || len(b.Review.TrustedReviewers) == 0 {
			continue
		}
		prs, complete, err := h.GitHub.ListPullRequests(ctx, b.repo(), b.BaseBranch)
		if err != nil || !complete {
			return err
		}
		var eligible []github.PullRequest
		for _, pr := range prs {
			if manualGoalPR(b, &pr) {
				eligible = append(eligible, pr)
			}
		}
		if e.manualGoalCursor == nil {
			e.manualGoalCursor = make(map[string]int)
		}
		for range min(4, len(eligible)) {
			current := e.manualGoalCursor[b.Name] % len(eligible)
			e.manualGoalCursor[b.Name] = current + 1
			if err := h.findRequests(ctx, b, eligible[current]); err != nil && !errors.Is(err, ErrManualGoalUnavailable) {
				return err
			}
		}
	}
	return nil
}

func (e *Engine) verifyManualFeedback(ctx context.Context, issueID int64, proof manualGoalProof) error {
	h := e.manualGoal
	if h == nil {
		return ErrManualGoalUnavailable
	}
	b := h.binding(proof.Request)
	if b == nil {
		return ErrManualGoalUnavailable
	}
	issue, err := h.authorize(ctx, b, proof.Request)
	if err != nil {
		return err
	}
	current, err := h.currentAssessment(ctx, b, proof.Request)
	if err != nil {
		return err
	}
	if issue != issueID || current.checkID != proof.CheckID || current.Digest != proof.Digest ||
		current.ReplyRevision != proof.ReplyRevision {
		return ErrManualGoalUnavailable
	}
	return nil
}
