package review

import (
	"strings"
	"testing"

	"github.com/jomcgi/homelab/projects/factory/connector/github"
	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
)

var (
	shaB = strings.Repeat("b", 40)
	shaC = strings.Repeat("c", 40)
)

// reuseHarness is the issue harness with reuse on and one tree and merge base
// for the first head, so a test says which later head differs.
func reuseHarness(t *testing.T) (*harness, *issueAPI) {
	t.Helper()
	h, api := issueHarness(t)
	t.Cleanup(func() {
		if t.Failed() {
			t.Log(h.log.String())
		}
	})
	h.cfg.ReuseUnchangedTree = true
	h.gh.withLock(func() {
		h.gh.trees = map[string]string{shaA: "tree-1"}
		h.gh.mergeBases = map[string]string{shaA: "base-1"}
	})
	return h, api
}

// pushHead makes sha the PR's head with tree and merge base, and a commit.
func pushHead(h *harness, sha, tree, mergeBase, message string) {
	h.gh.push(7, sha)
	h.gh.withLock(func() {
		h.gh.trees[sha], h.gh.mergeBases[sha] = tree, mergeBase
		var c github.PullRequestCommit
		c.SHA, c.Commit.Author.Email, c.Commit.Message = sha, "joe@example.com", message
		h.gh.prs[7].Commits = append(h.gh.prs[7].Commits, c)
	})
}

// reviewTasks counts the code review messages kagent took; goal checks are
// separate tasks.
func reviewTasks(h *harness) int {
	n := 0
	for _, m := range h.k.sent() {
		if !strings.Contains(m.MessageID, "/goal/") {
			n++
		}
	}
	return n
}

func reviewOne(t *testing.T, h *harness, e *Engine, ctxHead string, reply string) {
	t.Helper()
	ctx := t.Context()
	h.k.finish(runReview(t, h, e, ctx, 7, ctxHead), kagent.StateCompleted, reply)
	eventually(t, "the check of "+short(ctxHead), func() bool { return h.gh.check(ctxHead).Status == "completed" })
	e.Wait()
}

var oneBlocking = map[string]any{"summary": "s", "findings": []any{
	map[string]any{"id": "projects/factory/x.go:correctness", "severity": "high", "path": "projects/factory/x.go", "line": 2, "title": "Wrong x", "detail": "x is wrong."},
	map[string]any{"id": "x.go#naming", "severity": "low", "path": "x.go", "line": 3, "title": "Rename x"},
}}

// An empty commit holds the tree of the head already reviewed: the earlier
// findings stand, no review task is sent, and the goal check and the rest of
// the verdict run for the new head.
func TestUnchangedTreeKeepsTheEarlierReview(t *testing.T) {
	h, _ := reuseHarness(t)
	e, ctx, _ := h.start()
	reviewOne(t, h, e, shaA, report(t, oneBlocking))
	first, _ := LoadState(h.gh.check(shaA).Text)
	if len(first.Blocking) != 1 || first.Basis == "" {
		t.Fatalf("first state = %+v", first)
	}
	reviews, goals := reviewTasks(h), len(goalTexts(h))
	if reviews != 1 || goals == 0 {
		t.Fatalf("first head ran %d review tasks and %d goal checks", reviews, goals)
	}

	pushHead(h, shaB, "tree-1", "base-1", "chore: empty commit")
	e.Tick(ctx)
	eventually(t, "the second check", func() bool { c := h.gh.check(shaB); return c != nil && c.Status == "completed" })
	e.Wait()

	if got := reviewTasks(h); got != reviews {
		t.Fatalf("the unchanged tree started %d more review tasks", got-reviews)
	}
	if got := len(goalTexts(h)); got != goals+1 {
		t.Fatalf("goal checks = %d, want %d: the goal check must run again for the new head", got, goals+1)
	}
	c := h.gh.check(shaB)
	st, _ := LoadState(c.Text)
	if c.Conclusion != ConclusionFailure || len(st.Blocking) != 1 || st.Blocking[0].ID != "projects/factory/x.go:correctness" {
		t.Fatalf("reused check = %s %s %+v", c.Conclusion, c.Title, st.Blocking)
	}
	if st.TaskID != first.TaskID || st.Basis != first.Basis || st.Goal == nil || st.Goal.Head != shaB {
		t.Fatalf("reused state = task %q basis %q goal %+v", st.TaskID, st.Basis, st.Goal)
	}
	if !strings.Contains(c.Summary, "no new review task ran") || !strings.Contains(c.Summary, "Wrong x") {
		t.Fatalf("reused summary:\n%s", c.Summary)
	}
	// The session carries to the head after, without a second review in it.
	if st.Session == nil || st.Session.Head != shaB || st.Session.Reviews != first.Session.Reviews || len(st.Session.Tasks) != len(first.Session.Tasks) {
		t.Fatalf("reused session = %+v, first %+v", st.Session, first.Session)
	}

	// A third head of the same tree reuses the reused review in turn.
	pushHead(h, shaC, "tree-1", "base-1", "chore: another empty commit")
	e.Tick(ctx)
	eventually(t, "the third check", func() bool { c := h.gh.check(shaC); return c != nil && c.Status == "completed" })
	e.Wait()
	if got := reviewTasks(h); got != reviews {
		t.Fatalf("the third head started %d more review tasks", got-reviews)
	}
}

// A decision clears a finding because the agent read Joe's comment, so a head
// with a new comment is reviewed in full. The pass it earns then carries to
// an empty commit, which keeps both the finding cleared and its decision.
func TestDecisionClearsThroughAFullReviewThenCarriesToReusedHeads(t *testing.T) {
	h, _ := reuseHarness(t)
	e, ctx, _ := h.start()
	reviewOne(t, h, e, shaA, report(t, map[string]any{"summary": "s", "findings": []any{
		map[string]any{"id": "a.yaml:live", "severity": "medium", "path": "a.yaml", "title": "Rollout unverified"},
	}}))
	if c := h.gh.check(shaA); c.Conclusion != ConclusionFailure {
		t.Fatalf("first check = %s", c.Conclusion)
	}
	h.gh.withLock(func() {
		h.gh.prs[7].Comments = append(h.gh.prs[7].Comments, github.Comment{ID: 42, Author: "jomcgi", Body: "Decision: accept `a.yaml:live` on the rollout log; check delivery after rollout."})
	})
	// The empty commit after the decision is reviewed in full: the comment
	// is new, and only the agent's note can link it to the finding.
	pushHead(h, shaB, "tree-1", "base-1", "chore: empty commit")
	link := "https://github.com/" + testRepo + "/pull/7#issuecomment-42"
	reviewOne(t, h, e, shaB, report(t, map[string]any{"summary": "s", "findings": []any{}, "previous": []any{
		map[string]any{"id": "a.yaml:live", "status": "justified", "note": "decision: " + link + " accepts the rollout log."},
	}}))
	if got := reviewTasks(h); got != 2 {
		t.Fatalf("the decision head ran %d review tasks, want a full review", got-1)
	}
	if c := h.gh.check(shaB); c.Conclusion != ConclusionSuccess {
		t.Fatalf("the decision did not clear the finding: %s %s", c.Conclusion, c.Title)
	}

	pushHead(h, shaC, "tree-1", "base-1", "chore: another empty commit")
	e.Tick(ctx)
	eventually(t, "the reused check", func() bool { c := h.gh.check(shaC); return c != nil && c.Status == "completed" })
	e.Wait()
	if got := reviewTasks(h); got != 2 {
		t.Fatalf("the reused head started %d review tasks", got-2)
	}
	c := h.gh.check(shaC)
	st, _ := LoadState(c.Text)
	if c.Conclusion != ConclusionSuccess || len(st.Blocking) != 0 || len(st.Decisions) != 1 || st.Decisions[0].ID != 42 {
		t.Fatalf("reused check = %s %s, blocking %+v, decisions %+v", c.Conclusion, c.Title, st.Blocking, st.Decisions)
	}
	if len(st.Cleared) != 1 || st.Cleared[0].ID != "a.yaml:live" || st.Cleared[0].Decision == nil {
		t.Fatalf("the finding's clearing did not carry: %+v", st.Cleared)
	}
}

// Anything that could change the code review, or cannot be shown not to, gives
// the head a full review.
func TestReuseFailsClosed(t *testing.T) {
	for _, tc := range []struct {
		name   string
		setup  func(h *harness)
		commit string
		tree   string
		base   string
	}{
		{name: "a tree change", tree: "tree-2", base: "base-1"},
		{name: "another merge base", tree: "tree-1", base: "base-2"},
		{name: "an unreadable tree", tree: "", base: "base-1"},
		{name: "an unreadable merge base", tree: "tree-1", base: ""},
		{
			name: "an edited description", tree: "tree-1", base: "base-1",
			setup: func(h *harness) { h.gh.prs[7].Body += "\n\nThe author's evidence: it is fine." },
		},
		{
			name: "a new trusted comment", tree: "tree-1", base: "base-1",
			setup: func(h *harness) {
				h.gh.prs[7].Comments = append(h.gh.prs[7].Comments, github.Comment{ID: 9, Author: "jomcgi", Body: "Decision: accept projects/factory/x.go:correctness."})
			},
		},
		{
			name: "a changed rule", tree: "tree-1", base: "base-1",
			// Rules are read at the base commit, so a changed rule is a new base commit.
			setup: func(h *harness) {
				h.gh.baseFiles["AGENTS.md"] = "Stricter agent rules."
				h.gh.baseHead = "base-2"
			},
		},
		{
			name: "a review response in the commit", tree: "tree-1", base: "base-1",
			commit: "chore: re-review\n\nReview response: projects/factory/x.go:correctness is a false positive.",
		},
		{
			name: "a commit that names a finding", tree: "tree-1", base: "base-1",
			commit: "fix: projects/factory/x.go:correctness",
		},
		{
			name: "an activation to assess", tree: "tree-1", base: "base-1",
			commit: "chore: re-review\n\nActivation: after rollout, compare the rate",
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			h, _ := reuseHarness(t)
			e, ctx, _ := h.start()
			reviewOne(t, h, e, shaA, report(t, oneBlocking))
			if tc.setup != nil {
				h.gh.withLock(func() { tc.setup(h) })
			}
			msg := tc.commit
			if msg == "" {
				msg = "chore: empty commit"
			}
			pushHead(h, shaB, tc.tree, tc.base, msg)
			h.gh.withLock(func() {
				if tc.tree == "" {
					delete(h.gh.trees, shaB)
				}
				if tc.base == "" {
					delete(h.gh.mergeBases, shaB)
				}
			})
			task := runReview(t, h, e, ctx, 7, shaB)
			if task == "" || reviewTasks(h) != 2 {
				t.Fatalf("review tasks = %d, want the head reviewed in full", reviewTasks(h))
			}
			h.k.finish(task, kagent.StateCompleted, report(t, oneBlocking))
			eventually(t, "the full review", func() bool { return h.gh.check(shaB).Status == "completed" })
			if st, _ := LoadState(h.gh.check(shaB).Text); st.Basis == "" || strings.Contains(h.gh.check(shaB).Summary, "no new review task ran") {
				t.Fatalf("the head's state = %+v\n%s", st, h.gh.check(shaB).Summary)
			}
		})
	}
}

// Reuse is off unless configured, and an earlier review kept no inputs to
// compare (as before this change) cannot be reused.
func TestReuseNeedsTheSettingAndARecordedBasis(t *testing.T) {
	for _, tc := range []struct {
		name string
		on   bool
		prep func(st *State)
	}{
		{name: "setting off"},
		{name: "no recorded basis", on: true, prep: func(st *State) { st.Basis = "" }},
		{name: "incomplete earlier state", on: true, prep: func(st *State) { st.Incomplete = true }},
		{name: "an earlier evidence finding", on: true, prep: func(st *State) {
			st.Blocking[0].Kind = KindEvidence
		}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			h, _ := reuseHarness(t)
			h.cfg.ReuseUnchangedTree = tc.on
			e, ctx, _ := h.start()
			reviewOne(t, h, e, shaA, report(t, oneBlocking))
			if tc.prep != nil {
				st, _ := LoadState(h.gh.check(shaA).Text)
				tc.prep(&st)
				id := h.gh.check(shaA).ID
				h.gh.withLock(func() {
					for _, c := range h.gh.checks {
						if c.ID == id {
							c.Text = encodeState(st)
						}
					}
				})
			}
			pushHead(h, shaB, "tree-1", "base-1", "chore: empty commit")
			runReview(t, h, e, ctx, 7, shaB)
			if reviewTasks(h) != 2 {
				t.Fatalf("review tasks = %d, want the head reviewed in full", reviewTasks(h))
			}
		})
	}
}

// Joe's approval belongs to the head he approved. A reused head of a gated PR
// is held again until its own approval, though no review task ran for it.
func TestReusedHeadStillNeedsItsOwnApproval(t *testing.T) {
	h := newHarness(t)
	h.gh.addPR(&fakePR{Number: 7, Author: "jomcgi-factory[bot]", Title: "fix(reviewer): x", Head: shaA, Files: []github.PullRequestFile{
		{Path: "projects/factory/reviewer/review/engine.go", Status: "modified", Additions: 2},
	}})
	h.cfg.ReuseUnchangedTree = true
	h.gh.baseFiles["AGENTS.md"] = "Agent rules from main."
	h.gh.trees, h.gh.mergeBases = map[string]string{shaA: "tree-1"}, map[string]string{shaA: "base-1"}
	e, ctx, _ := h.start()
	clean := map[string]any{"summary": "s", "findings": []any{}}
	h.k.finish(runReview(t, h, e, ctx, 7, shaA), kagent.StateCompleted, report(t, clean))
	eventually(t, "the held check", func() bool { return h.gh.check(shaA).Status == "completed" })
	if c := h.gh.check(shaA); c.Conclusion != ConclusionFailure {
		t.Fatalf("the gated head passed unapproved: %s %s", c.Conclusion, c.Title)
	}
	h.gh.review(7, "jomcgi", "APPROVED", shaA, "")
	e.Tick(ctx)
	eventually(t, "the approved pass", func() bool { return h.gh.check(shaA).Conclusion == ConclusionSuccess })
	e.Wait()

	pushHead(h, shaB, "tree-1", "base-1", "chore: empty commit")
	e.Tick(ctx)
	eventually(t, "the reused check", func() bool { c := h.gh.check(shaB); return c != nil && c.Status == "completed" })
	e.Wait()
	if got := reviewTasks(h); got != 1 {
		t.Fatalf("the reused head started %d review tasks", got-1)
	}
	if c := h.gh.check(shaB); c.Conclusion != ConclusionFailure {
		t.Fatalf("the reused head passed on the earlier head's approval: %s %s", c.Conclusion, c.Title)
	}
	h.gh.review(7, "jomcgi", "APPROVED", shaB, "")
	e.Tick(ctx)
	eventually(t, "the second approval", func() bool { return h.gh.check(shaB).Conclusion == ConclusionSuccess })
}
