package review

import (
	"context"
	"fmt"

	"github.com/jomcgi/homelab/projects/factory/connector/github"
)

const authorApprovedTitle = "Review skipped: author approves"

// authorApproved passes a new head without a model review when the binding
// enables it and the PR meets the authorship waiver in REVIEW.md "Joe decides":
// the author is in HumanApprovers and every push to the head ref is by a
// HumanApprovers account (authorshipApproval). Joe decided on 2026-11-24 that
// his own PRs get no reviewer tokens, because his sessions review them first.
//
// It never skips a head that already has a verdict, a hold (label, shared
// head, restart recovery) or Semgrep evidence to check, and any read error
// falls back to the full review. It reports whether it posted the pass.
func (e *Engine) authorApproved(ctx context.Context, b *Binding, pr github.PullRequest, run *github.CheckRun) bool {
	if !b.AuthorApproved || e.cfg.Semgrep != nil || pr.Draft || b.skipped(&pr) {
		return false
	}
	// Only a head with no verdict skips; a cancelled run is no verdict.
	if run != nil && !(run.Status == "completed" && run.Conclusion == ConclusionCancelled) {
		return false
	}
	if sharedHeadHeld(run) {
		return false
	}
	l := e.prLog(b, pr)
	now := e.cfg.Now()
	gate := &Gate{Labels: gateLabels(b, pr.Labels), ObservedLabels: gateLabels(b, pr.Labels)}
	if run != nil {
		if prior, ok := LoadState(run.Text); ok && prior.Gate != nil {
			*gate = *prior.Gate
		}
		gate.observeLabels(gateLabels(b, pr.Labels), now)
	}
	gate, _ = e.recoveryGate(b, pr, gate)
	gate, _ = e.retainGate(b, pr, gate, false)
	if gate == nil || gate.reviewRequired() {
		return false
	}
	who, err := e.authorshipApproval(ctx, b, pr)
	if err != nil {
		e.ghError(ctx, l, b, "authorship_approval", err)
		return false
	}
	if who == "" {
		return false
	}
	gate.Authorship = who
	st := State{Gate: gate}
	text := encodeState(st)
	u := &CommentUpdate{Verdict: VerdictApproved, Headline: authorApprovedTitle, Note: who, Gate: gate}
	started := now.UTC()
	created, err := e.createReviewVerdict(ctx, b, github.CheckRunWrite{
		Name: e.cfg.CheckName, HeadSHA: pr.HeadSHA, ExternalID: MessageID(b.repo(), pr.Number, pr.HeadSHA),
		Status: "completed", Conclusion: ConclusionSuccess, StartedAt: &started, CompletedAt: &started,
		Output: &github.CheckRunOutput{
			Title: authorApprovedTitle,
			Summary: who + "\n\nThe binding passes its own authors' PRs without a model review, " +
				"because the author's session reviews them first. " + fmt.Sprintf("No review task ran for %s.", short(pr.HeadSHA)),
			Text: recordComment(text, "", pr.HeadSHA, u),
		},
	})
	if err != nil {
		e.ghError(ctx, l, b, "create_check", err)
		return false
	}
	e.m.ReviewsCompleted.WithLabelValues(b.Name, ConclusionSuccess).Inc()
	l.InfoContext(ctx, "review skipped: author approves", "approver", who)
	e.queueCompletedComment(ctx, b, pr, created, created.Text, u)
	return true
}
