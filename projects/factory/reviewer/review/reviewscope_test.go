package review

import (
	"strings"
	"testing"
)

var shaOther = strings.Repeat("e", 40)

// Only the named authors get a review; another author's PR gets no check.
func TestReviewAuthorsLimitsWhoIsReviewed(t *testing.T) {
	h := newHarness(t)
	h.cfg.Bindings[0].ReviewAuthors = []string{"jomcgi", "jomcgi-factory[bot]"}
	h.gh.addPR(&fakePR{Number: 1, Author: "stranger", Head: shaA})
	h.gh.addPR(&fakePR{Number: 2, Author: "JomcGi-Factory[bot]", Head: shaOther})
	h.gh.setStatus(shaA, "pr-checks", "success", ciCreator)
	h.gh.setStatus(shaOther, "pr-checks", "success", ciCreator)
	e, ctx, _ := h.start()
	e.Tick(ctx)
	eventually(t, "the named author's review task", func() bool { return h.taskOn(2, shaOther, 1) != "" })
	if c := h.gh.check(shaA); c != nil {
		t.Fatalf("a PR by an unnamed author got a check: %+v", c)
	}
	if got := h.taskOn(1, shaA, 1); got != "" {
		t.Fatalf("a review ran for an unnamed author: %s", got)
	}
}

// A head that has not passed its required checks starts no model review. It
// starts once they pass, and a head that fails them never does.
func TestReviewAfterChecksHoldsTheReviewUntilCIIsGreen(t *testing.T) {
	h := newHarness(t)
	h.cfg.Bindings[0].ReviewAfterChecks = true
	addPR7(h)
	e, ctx, _ := h.start()
	e.Tick(ctx) // No status yet: CI is pending.
	if c := h.gh.check(shaA); c != nil || h.taskOn(7, shaA, 1) != "" {
		t.Fatalf("review started before CI: check %+v", c)
	}
	h.gh.setStatus(shaA, "pr-checks", "failure", ciCreator)
	e.Tick(ctx)
	if c := h.gh.check(shaA); c != nil || h.taskOn(7, shaA, 1) != "" {
		t.Fatalf("review started on a failed head: check %+v", c)
	}
	h.gh.setStatus(shaA, "pr-checks", "success", ciCreator)
	runReview(t, h, e, ctx, 7, shaA)
}
