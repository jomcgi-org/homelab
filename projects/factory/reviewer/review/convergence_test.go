package review

import (
	"strings"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/github"
	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
)

// These tests replay the late-finding shapes found in the review history of
// PRs #8040 to #8205 (HOME-107 follow-up): unchanged code inside an edited
// file, a changed caller, a rebase, missing evidence, and a goal verdict that
// flipped on the same issue evidence.

// TestScopeLowersUnchangedCodeInsideAnEditedFile places new medium findings
// at hunk level. A finding on an untouched function of an edited file is an
// earlier review's miss unless its cause names the change that exposed it.
func TestScopeLowersUnchangedCodeInsideAnEditedFile(t *testing.T) {
	d := &github.CommitComparison{Status: "ahead", Files: []github.PullRequestFile{
		// Lines 40-46 of edit.go changed; nothing else in it did.
		{Path: "edit.go", Status: "modified", Patch: "@@ -40,6 +40,7 @@ func fix() {\n a\n b\n c\n+d\n e\n f\n g"},
		{Path: "blob.bin", Status: "modified"}, // no patch: changed throughout
	}}
	s := LaterScope("prev", d)
	at := func(id, severity, path string, line int, cause string, earlier bool) Finding {
		return Finding{ID: id, Severity: severity, Path: path, Line: line, Title: id, Cause: cause, Earlier: earlier}
	}
	r := &Report{Summary: "s", Findings: []Finding{
		at("edit.go:correctness:other-func", SeverityMedium, "edit.go", 200, "", false),
		at("edit.go:correctness:in-hunk", SeverityMedium, "edit.go", 43, "", false),
		at("edit.go:correctness:caller", SeverityMedium, "edit.go", 210, "fix() in edit.go now returns early, so this caller reads a nil value.", false),
		at("edit.go:correctness:unrelated-cause", SeverityMedium, "edit.go", 220, "other.go changed.", false),
		at("edit.go:correctness:marked", SeverityMedium, "edit.go", 230, "edit.go changed.", true),
		at("edit.go:correctness:no-line", SeverityMedium, "edit.go", 0, "", false),
		at("blob.bin:correctness", SeverityMedium, "blob.bin", 5, "", false),
		at("other.go:correctness:caller", SeverityMedium, "pkg/other.go", 9, "The change to `edit.go` makes this call fail.", false),
		at("other.go:security", SeverityCritical, "other.go", 9, "", false),
	}}
	got := s.Lower(r, nil, nil)
	want := map[string]string{
		"edit.go:correctness:other-func":      SeverityLow,
		"edit.go:correctness:in-hunk":         SeverityMedium,
		"edit.go:correctness:caller":          SeverityMedium,
		"edit.go:correctness:unrelated-cause": SeverityLow,
		"edit.go:correctness:marked":          SeverityLow,
		"edit.go:correctness:no-line":         SeverityMedium,
		"blob.bin:correctness":                SeverityMedium,
		"other.go:correctness:caller":         SeverityMedium,
		"other.go:security":                   SeverityCritical,
	}
	for _, f := range got.Findings {
		if f.Severity != want[f.ID] {
			t.Errorf("%s = %s, want %s", f.ID, f.Severity, want[f.ID])
		}
		if f.Severity == SeverityLow && (f.Lowered != SeverityMedium || !f.Defer) {
			t.Errorf("%s lowered without a record: %+v", f.ID, f)
		}
	}
	// A pure deletion's hunk covers the lines around it.
	if h := patchHunks("@@ -10,3 +9,0 @@\n-x\n-y\n-z"); len(h) != 1 || h[0] != (lineRange{9, 9}) {
		t.Errorf("deletion hunks = %+v", h)
	}
}

// TestRebasedScopeComparesThePRsOwnDiff keeps a rebase from reopening the
// whole PR: a file whose own diff makes the same change, at moved line
// numbers, is unchanged; a changed, added or dropped file is changed
// throughout; incomplete lists give no scope.
func TestRebasedScopeComparesThePRsOwnDiff(t *testing.T) {
	before := []github.PullRequestFile{
		{Path: "same.go", Status: "modified", Patch: "@@ -1,2 +1,3 @@\n a\n+b\n c"},
		{Path: "edited.go", Status: "modified", Patch: "@@ -1,2 +1,3 @@\n a\n+b\n c"},
		{Path: "dropped.go", Status: "modified", Patch: "@@ -1 +1 @@\n-a\n+b"},
	}
	after := []github.PullRequestFile{
		{Path: "same.go", Status: "modified", Patch: "@@ -21,2 +21,3 @@\n a\n+b\n c"},
		{Path: "edited.go", Status: "modified", Patch: "@@ -1,2 +1,3 @@\n a\n+B\n c"},
		{Path: "added.go", Status: "added", Patch: "@@ -0,0 +1 @@\n+x"},
	}
	s := RebasedScope("prev", before, after, true)
	if s == nil || !s.Rebased {
		t.Fatal("no rebased scope")
	}
	if got := strings.Join(s.Paths(), ","); got != "added.go,dropped.go,edited.go" {
		t.Fatalf("changed = %s", got)
	}
	if s.touched("same.go", 22) || !s.touched("edited.go", 500) {
		t.Fatal("rebased scope placed findings wrongly")
	}
	// The base moved too, so any stated cause counts, and none lowers nothing.
	r := s.Lower(&Report{Findings: []Finding{
		{ID: "same.go:correctness", Severity: SeverityMedium, Path: "same.go", Line: 22, Title: "t"},
		{ID: "same.go:correctness:base", Severity: SeverityMedium, Path: "same.go", Line: 22, Title: "t", Cause: "main renamed Load, which this code calls."},
	}}, nil, nil)
	if r.Findings[0].Severity != SeverityLow || r.Findings[1].Severity != SeverityMedium {
		t.Fatalf("rebased lowering = %+v", r.Findings)
	}
	for name, s := range map[string]*Scope{
		"first review": RebasedScope("", before, after, true),
		"incomplete":   RebasedScope("prev", before, after, false),
		"no list":      RebasedScope("prev", nil, after, true),
	} {
		if s != nil {
			t.Errorf("%s: scope %+v", name, s)
		}
	}
}

// TestBlockingEntriesSayKindCauseAndReopening tells the author what each
// blocking finding asks for, why a new one appeared on this head, and why
// a cleared one reopened.
func TestBlockingEntriesSayKindCauseAndReopening(t *testing.T) {
	a := Assessment{
		Conclusion: ConclusionFailure, Later: true,
		Earlier: []Cleared{{ID: "a.go:correctness", Status: StatusFixed, Title: "Old"}},
		Blocking: []Blocked{
			{Finding: Finding{ID: "a.go:correctness", Severity: SeverityMedium, Title: "Old again", Cause: "a.go now drops the lock again."}},
			{Finding: Finding{ID: "b.go:correctness", Severity: SeverityHigh, Title: "Crash", Cause: "b.go now indexes an empty slice."}},
			{Finding: Finding{ID: "c.go:security", Severity: SeverityCritical, Title: "Token logged"}},
			{Finding: Finding{ID: "c.go:unverified", Severity: SeverityMedium, Title: "No test result"}},
			{Finding: Finding{ID: semgrepUnverified, Severity: SeverityMedium, Kind: KindHold, Title: "Scan pending"}},
			{Finding: Finding{ID: "HOME-1:goal:criterion-1", Severity: SeverityMedium, Kind: KindEvidence, Verdict: goalUnclear, Title: "unverified"}},
		},
	}
	o := Render(a, &Report{Summary: "s"}, nil, "footer")
	for _, want := range []string{
		"Reopened: an earlier head's review recorded it fixed. Why: a.go now drops the lock again.",
		"Cause on this head: b.go now indexes an empty slice.",
		"Token logged (`c.go:security`)\n  New on this head, and the reviewer named no change that caused it: an earlier review missed it.",
		"No test result (`c.go:unverified`)\n  Kind: missing evidence.",
		"Scan pending (`semgrep:unverified`)\n  Kind: hold.",
		"**Defect**: change the code", "**Missing evidence**: this is not a confirmed defect", "**Hold**: no code change clears it",
	} {
		if !strings.Contains(o.Output.Summary, want) {
			t.Errorf("summary lacks %q:\n%s", want, o.Output.Summary)
		}
	}
	if strings.Contains(o.Output.Summary, "unverified (`HOME-1:goal:criterion-1`)\n  New on this head") {
		t.Error("a service goal finding was called an earlier miss")
	}
	// The agent cannot claim a kind for its own finding.
	r, err := ParseReport("```json\n" + `{"summary":"s","findings":[{"id":"x.go:correctness","severity":"high","title":"t","kind":"hold","cause":" x.go changed "}]}` + "\n```")
	if err != nil || r.Findings[0].Kind != "" || r.Findings[0].Cause != "x.go changed" || findingKind(r.Findings[0]) != KindDefect {
		t.Fatalf("parsed = %+v, %v", r, err)
	}
	// The state keeps the cause and the service's kind for the next head.
	st, _ := LoadState(o.Output.Text)
	if len(st.Blocking) == 0 || st.Blocking[0].Cause == "" {
		t.Fatalf("state = %+v", st.Blocking)
	}
}

// TestGoalVerdictsCarryAcrossHeads keeps a settled verdict from flipping to
// unclear on the same issue evidence, and lets the confirmation call only
// re-check the initial not_met verdicts.
func TestGoalVerdictsCarryAcrossHeads(t *testing.T) {
	prev := &GoalCheck{Head: "a", Digest: "d", Overall: goalMet, Criteria: []GoalVerdict{{1, goalMet}, {2, goalLive}, {3, goalMet}, {4, goalUnclear}}}
	got := keepEarlier(&GoalCheck{
		Head: "b", Digest: "d", Overall: goalUnclear,
		Criteria: []GoalVerdict{{1, goalUnclear}, {2, goalUnclear}, {3, goalNotMet}, {4, goalUnclear}, {5, goalUnclear}},
	}, prev)
	want := []string{goalMet, goalLive, goalNotMet, goalUnclear, goalUnclear}
	for i, c := range got.Criteria {
		if c.Verdict != want[i] {
			t.Errorf("criterion %d = %s, want %s", c.Criterion, c.Verdict, want[i])
		}
	}
	if got.Overall != goalMet {
		t.Errorf("overall = %s", got.Overall)
	}
	// Other issue evidence (an edit can renumber criteria) keeps nothing.
	if g := keepEarlier(&GoalCheck{Digest: "e", Overall: goalUnclear, Criteria: []GoalVerdict{{1, goalUnclear}}}, prev); g.Criteria[0].Verdict != goalUnclear {
		t.Error("a verdict on other evidence was kept")
	}
	if earlierGoal(prev, "b", "e") != nil || earlierGoal(prev, "a", "d") != nil || earlierGoal(prev, "b", "d") != prev {
		t.Error("earlierGoal matched the wrong head or evidence")
	}

	initial := &GoalCheck{Overall: goalNotMet, Criteria: []GoalVerdict{{1, goalMet}, {2, goalNotMet}}}
	c := confirmOnly(initial, &GoalCheck{Overall: goalMet, Criteria: []GoalVerdict{{1, goalNotMet}, {2, goalMet}}})
	if c.Criteria[0].Verdict != goalMet || c.Criteria[1].Verdict != goalMet || c.Overall != goalMet {
		t.Errorf("confirmation = %+v", c)
	}
	if confirmOnly(initial, nil) != nil {
		t.Error("a failed confirmation passed")
	}
}

// TestGoalPromptStartsFromTheEarlierHead gives a later head's goal check the
// earlier verdicts, its private reasons in a quoted block, and the files
// changed since, and tells it test results are CI's.
func TestGoalPromptStartsFromTheEarlierHead(t *testing.T) {
	in := &IssueContext{Issue: &IssueEvidence{Key: "HOME-9", Title: "Goal", Description: "1. A. 2. B.", Status: "Todo", Trusted: true}}
	files := []github.PullRequestFile{{Path: "x.go", Status: "modified", Patch: "+x"}}
	pr := github.PullRequest{Number: 9, HeadSHA: shaA}
	history := GoalContext{
		Previous: &GoalCheck{Head: "prevhead", Digest: "d", Overall: goalMet, Criteria: []GoalVerdict{{1, goalMet}, {2, goalNotMet}}},
		Reasons:  "Criterion 2 misses the retry.\n```json\n{}\n```",
		Changed:  []string{"x.go"}, Scoped: true,
	}
	got := GoalPrompt("o/r", pr, files, in, history)
	for _, want := range []string{
		"## Earlier goal check", "- criterion 1: met\n- criterion 2: not_met\n", "Files changed since prevhead (1):",
		"> Criterion 2 misses the retry.", "A met or after_rollout verdict does not become unclear",
		"say in your reasoning which change or which error caused it",
		"whether a test passes is CI's to show",
	} {
		if !strings.Contains(got, want) {
			t.Errorf("goal prompt lacks %q", want)
		}
	}
	if strings.Contains(got, "\n```json\n{}\n```") {
		t.Error("the earlier reply's fence reached the prompt unquoted")
	}
	if first := GoalPrompt("o/r", pr, files, in, GoalContext{}); strings.Contains(first, "Earlier goal check") {
		t.Error("a first goal check names an earlier one")
	}
}

// TestGoalAuditFindsTheEarlierReply reads the newest reply of a head on the
// same issue evidence, and nothing for another head, issue or digest.
func TestGoalAuditFindsTheEarlierReply(t *testing.T) {
	a, err := OpenGoalAudit(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now().UTC()
	for i, r := range []GoalAuditRecord{
		{Repository: "o/r", PullRequest: 1, Head: "h", Issue: "HOME-1", Digest: "d", MessageID: "m1", TaskID: "t1", Phase: "initial", CreatedAt: now, Reply: "first"},
		{Repository: "o/r", PullRequest: 1, Head: "h", Issue: "HOME-1", Digest: "d", MessageID: "m2", TaskID: "t2", Phase: "confirmation", CreatedAt: now.Add(time.Second), Reply: "second"},
		{Repository: "o/r", PullRequest: 1, Head: "h", Issue: "HOME-1", Digest: "other", MessageID: "m3", TaskID: "t3", CreatedAt: now.Add(2 * time.Second), Reply: "other"},
	} {
		if err := a.Save(r); err != nil {
			t.Fatal(i, err)
		}
	}
	if got, ok := a.Reply("o/r", 1, "h", "HOME-1", "d"); !ok || got != "second" {
		t.Fatalf("reply = %q, %v", got, ok)
	}
	for _, miss := range [][]string{{"h2", "HOME-1", "d"}, {"h", "HOME-2", "d"}, {"h", "HOME-1", "x"}} {
		if _, ok := a.Reply("o/r", 1, miss[0], miss[1], miss[2]); ok {
			t.Errorf("found a reply for %v", miss)
		}
	}
}

// TestGoalCheckOfALaterHeadKeepsSettledVerdicts runs two heads: the second
// goal check reads the first one's reasons privately, an unclear answer
// keeps the first head's met, a not_met stands, and no private text is
// published.
func TestGoalCheckOfALaterHeadKeepsSettledVerdicts(t *testing.T) {
	h, _ := issueHarness(t)
	withComments(h)
	h.k.goalReplies = []string{
		"PRIVATE-A: both criteria are met by x.go.\n\n```json\n" + `{"criteria":[{"criterion":1,"verdict":"met"},{"criterion":2,"verdict":"met"}],"overall":"met"}` + "\n```",
		goalReply(`{"criteria":[{"criterion":1,"verdict":"unclear"},{"criterion":2,"verdict":"not_met"}],"overall":"met"}`),
		goalReply(`{"criteria":[{"criterion":1,"verdict":"not_met"},{"criterion":2,"verdict":"not_met"}],"overall":"met"}`),
	}
	e, ctx, _ := h.start()
	h.k.finish(runReview(t, h, e, ctx, 7, shaA), kagent.StateCompleted, report(t, passing))
	e.Wait()
	shaB := strings.Repeat("b", 40)
	h.gh.push(7, shaB)
	h.k.finish(runReview(t, h, e, ctx, 7, shaB), kagent.StateCompleted, report(t, passing))
	e.Wait()
	texts := goalTexts(h)
	if len(texts) != 3 {
		t.Fatalf("goal calls = %d", len(texts))
	}
	if !strings.Contains(texts[1], "## Earlier goal check") || !strings.Contains(texts[1], "> PRIVATE-A: both criteria are met") ||
		!strings.Contains(texts[1], "interdiff-bbbb.go") {
		t.Fatalf("the second goal prompt lacks the earlier head's verdicts, reasons or changes:\n%s", texts[1])
	}
	st, _ := LoadState(h.gh.check(shaB).Text)
	if st.Goal == nil || st.Goal.Criteria[0].Verdict != goalMet || st.Goal.Criteria[1].Verdict != goalNotMet {
		t.Fatalf("goal = %+v", st.Goal)
	}
	var ids []string
	for _, f := range st.Blocking {
		ids = append(ids, f.ID)
	}
	if strings.Join(ids, ",") != "HOME-58:goal:criterion-2" {
		t.Fatalf("blocking = %v", ids)
	}
	if strings.Contains(published(t, h)+h.log.String(), "PRIVATE") {
		t.Fatal("private rationale was published")
	}
}

// TestRebasedHeadReviewsOnlyFilesWhoseDiffChanged replays a rebase: the
// prompt lists only the file whose own diff changed, and a new medium on
// the other file, with no cause, is an earlier review's miss.
func TestRebasedHeadReviewsOnlyFilesWhoseDiffChanged(t *testing.T) {
	h := newHarness(t)
	addPR7(h)
	same := "@@ -1,2 +1,3 @@\n a\n+b\n c"
	h.gh.withLock(func() {
		h.gh.prs[7].Files = []github.PullRequestFile{
			{Path: "projects/factory/x.go", Status: "modified", Additions: 1, Patch: same},
			{Path: "projects/factory/z.go", Status: "modified", Additions: 1, Patch: "@@ -1,2 +1,3 @@\n a\n+old\n c"},
		}
	})
	e, ctx, _ := h.start()
	h.k.finish(runReview(t, h, e, ctx, 7, shaA), kagent.StateCompleted, report(t, passing))
	eventually(t, "the first check", func() bool { return h.gh.check(shaA).Status == "completed" })
	e.Wait()

	shaB := strings.Repeat("b", 40)
	h.gh.withLock(func() {
		h.gh.compares = map[string]map[string]any{
			shaA + "..." + shaB: {"status": "diverged", "files": []github.PullRequestFile{{Path: "projects/factory/x.go"}, {Path: "main-only.go"}}},
			"main..." + shaA: {"status": "diverged", "files": []github.PullRequestFile{
				{Path: "projects/factory/x.go", Status: "modified", Patch: same},
				{Path: "projects/factory/z.go", Status: "modified", Patch: "@@ -1,2 +1,3 @@\n a\n+old\n c"},
			}},
		}
		h.gh.prs[7].Files = []github.PullRequestFile{
			{Path: "projects/factory/x.go", Status: "modified", Additions: 1, Patch: "@@ -30,2 +30,3 @@\n a\n+b\n c"},
			{Path: "projects/factory/z.go", Status: "modified", Additions: 1, Patch: "@@ -1,2 +1,3 @@\n a\n+new\n c"},
		}
	})
	h.gh.push(7, shaB)
	h.k.finish(runReview(t, h, e, ctx, 7, shaB), kagent.StateCompleted, report(t, map[string]any{"summary": "s", "findings": []any{
		map[string]any{"id": "projects/factory/x.go:correctness", "severity": "medium", "path": "projects/factory/x.go", "line": 31, "title": "Old miss"},
		map[string]any{"id": "projects/factory/z.go:correctness", "severity": "medium", "path": "projects/factory/z.go", "line": 2, "title": "New bug"},
	}}))
	eventually(t, "the second check", func() bool { return h.gh.check(shaB).Status == "completed" })
	prompt := h.k.sent()[len(h.k.sent())-1].Parts[0].Text
	if !strings.Contains(prompt, "compared the PR's own diff before and after") ||
		!strings.Contains(prompt, "Files whose diff changed since "+shaA+" (1):\n> - \"projects/factory/z.go\"") {
		t.Fatalf("the rebased prompt lacks its scope:\n%s", prompt)
	}
	c := h.gh.check(shaB)
	if c.Title != "1 blocking finding (1 medium)" || !strings.Contains(c.Summary, "New bug (`projects/factory/z.go:correctness`)") {
		t.Fatalf("check = %s\n%s", c.Title, c.Summary)
	}
	if _, deferred, _ := strings.Cut(c.Summary, "### Deferred"); !strings.Contains(deferred, "Old miss") {
		t.Fatalf("the unchanged file's late finding was not deferred:\n%s", c.Summary)
	}
}
