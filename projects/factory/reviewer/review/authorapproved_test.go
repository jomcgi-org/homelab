package review

import (
	"strings"
	"testing"
)

func TestAuthorApprovedSkipsTheModelReview(t *testing.T) {
	for _, tc := range []struct {
		name    string
		enabled bool
		change  func(*harness, *fakePR)
		skip    bool
	}{
		{name: "Joe's own PR", enabled: true, skip: true},
		{name: "disabled binding reviews as before"},
		{name: "factory author", enabled: true, change: func(_ *harness, pr *fakePR) { pr.Author = "jomcgi-factory[bot]" }},
		{name: "other pusher", enabled: true, change: func(_ *harness, pr *fakePR) { pr.Activity[0].Actor.Login = "someone" }},
		{name: "fork", enabled: true, change: func(_ *harness, pr *fakePR) { pr.HeadRepo = "jomcgi/fork" }},
		{name: "Renovate branch", enabled: true, change: func(_ *harness, pr *fakePR) { pr.HeadRef = "renovate/dependency" }},
		{name: "activity read error", enabled: true, change: func(h *harness, _ *fakePR) { h.gh.failWith("GET /repos/"+testRepo+"/activity", 502) }},
		{name: "human label", enabled: true, change: func(_ *harness, pr *fakePR) { pr.Labels = []string{"human"} }},
		{name: "needs-human label", enabled: true, change: func(_ *harness, pr *fakePR) { pr.Labels = []string{"needs-human"} }},
		{name: "skip label", enabled: true, change: func(_ *harness, pr *fakePR) { pr.Labels = []string{"no-review"} }},
	} {
		t.Run(tc.name, func(t *testing.T) {
			h := newHarness(t)
			h.cfg.Bindings[0].AuthorApproved = tc.enabled
			pr := authoredPR()
			if tc.change != nil {
				tc.change(h, pr)
			}
			h.gh.addPR(pr)
			h.gh.setStatus(shaA, "pr-checks", "success", ciCreator)
			e, ctx, _ := h.start()
			e.Tick(ctx)
			if !tc.skip {
				// A review that is not skipped starts a model task, or none for a skip label.
				if c := h.gh.check(shaA); c != nil && strings.Contains(c.Title, authorApprovedTitle) {
					t.Fatalf("skipped a review that needs one: %+v", c)
				}
				return
			}
			eventually(t, "the author pass", func() bool {
				c := h.gh.check(shaA)
				return c != nil && c.Conclusion == ConclusionSuccess
			})
			c := h.gh.check(shaA)
			if !strings.Contains(c.Summary, authorshipPrefix) {
				t.Fatalf("pass does not name the authorship evidence: %s", c.Summary)
			}
			if got := h.taskOn(7, shaA, 1); got != "" { // no model task
				t.Fatalf("a review task ran for a skipped head: %s", got)
			}
			if len(h.k.sent()) != 0 {
				t.Fatalf("the model received a message for a skipped head: %d", len(h.k.sent()))
			}
		})
	}
}
