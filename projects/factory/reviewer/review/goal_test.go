package review

import (
	"encoding/json"
	"strings"
	"testing"

	"github.com/jomcgi/homelab/projects/factory/connector/itsaplan"
	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
)

func TestParseGoalAcceptsOnlyNumbersAndVerdicts(t *testing.T) {
	a, err := ParseGoal("PRIVATE notes outside the block.\n\n```json\n" +
		`{"criteria": [{"criterion": 2, "verdict": "not_met"}, {"criterion": 1, "verdict": "met"}], "overall": "unclear"}` + "\n```")
	if err != nil || len(a.Criteria) != 2 || a.Criteria[0] != (GoalVerdict{2, goalNotMet}) || a.Overall != goalUnclear {
		t.Fatalf("answer = %+v, %v", a, err)
	}
	if a, err := ParseGoal("```\n{\"criteria\": []}\n```"); err != nil || len(a.Criteria) != 0 || a.Overall != "" {
		t.Fatalf("empty answer = %+v, %v", a, err)
	}
	many := strings.Repeat(`{"criterion": 1, "verdict": "met"},`, maxCriteria+1)
	for name, block := range map[string]string{
		"extra field":         `{"criteria": [], "overall": "met", "note": "PRIVATE goal"}`,
		"extra verdict field": `{"criteria": [{"criterion": 1, "verdict": "met", "why": "PRIVATE goal"}]}`,
		"text verdict":        `{"criteria": [{"criterion": 1, "verdict": "met: PRIVATE goal"}]}`,
		"text overall":        `{"criteria": [], "overall": "PRIVATE goal"}`,
		"cased verdict":       `{"criteria": [{"criterion": 1, "verdict": "Met"}]}`,
		"text criterion":      `{"criteria": [{"criterion": "PRIVATE", "verdict": "met"}]}`,
		"fraction":            `{"criteria": [{"criterion": 1.5, "verdict": "met"}]}`,
		"zero":                `{"criteria": [{"criterion": 0, "verdict": "met"}]}`,
		"too high":            `{"criteria": [{"criterion": 51, "verdict": "met"}]}`,
		"repeated":            `{"criteria": [{"criterion": 1, "verdict": "met"}, {"criterion": 1, "verdict": "not_met"}]}`,
		"too many":            `{"criteria": [` + strings.TrimSuffix(many, ",") + `]}`,
		"no verdict":          `{"criteria": [{"criterion": 1}]}`,
		"no criteria":         `{"overall": "met"}`,
		"null criteria":       `{"criteria": null}`,
		"two values":          `{"criteria": []} {"criteria": []}`,
		"not json":            `PRIVATE goal`,
	} {
		if a, err := ParseGoal("```json\n" + block + "\n```"); err == nil || strings.Contains(err.Error(), "PRIVATE") {
			t.Errorf("%s: answer %+v, error %v", name, a, err)
		}
	}
	for _, reply := range []string{"PRIVATE: no block", "```PRIVATE\n{\"criteria\": []}\n```"} {
		if a, err := ParseGoal(reply); err == nil || strings.Contains(err.Error(), "PRIVATE") {
			t.Errorf("%q: answer %+v, error %v", reply, a, err)
		}
	}
}

// Every public word comes from the issue key, a criterion number or the
// service's fixed text.
func TestGoalFindingsUseOnlyKeyNumberAndFixedText(t *testing.T) {
	got := goalFindings("HOME-58", &GoalCheck{Overall: goalNotMet, Criteria: []GoalVerdict{{1, goalMet}, {2, goalNotMet}, {3, goalUnclear}}})
	want := []Finding{
		{
			ID: "HOME-58:goal", Severity: SeverityHigh, Title: "HOME-58: stated problem not addressed",
			Detail: "The goal check found that the PR does not address the stated problem of HOME-58.",
		},
		{
			ID: "HOME-58:goal:criterion-2", Severity: SeverityMedium, Title: "HOME-58 criterion 2: not met",
			Detail: "The goal check found that the diff does not meet criterion 2 of HOME-58, and the PR names no owner for the rest. Meet it, or name the HOME issue that owns the rest in the PR description.",
		},
		{
			ID: "HOME-58:goal:criterion-3", Severity: SeverityLow, Title: "HOME-58 criterion 3: unclear",
			Detail: "The goal check cannot tell from the diff if criterion 3 of HOME-58 is met, for example when only a live rollout can show it. The delivery owner checks it.",
		},
	}
	if len(got) != len(want) {
		t.Fatalf("findings = %+v", got)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Errorf("finding %d = %+v, want %+v", i, got[i], want[i])
		}
	}
	if u := goalFindings("HOME-58", nil); len(u) != 1 || u[0] != (Finding{
		ID: "HOME-58:goal:unavailable", Severity: SeverityMedium, Title: "HOME-58: goal check unavailable",
		Detail: "The goal check gave no valid answer, so this head is not checked against HOME-58. A new head or an issue change runs it again.",
	}) {
		t.Fatalf("unavailable = %+v", u)
	}
}

// The code review cannot report, justify or fix a goal finding: only the
// goal check decides them.
func TestApplyGoalOwnsGoalFindings(t *testing.T) {
	prior := []Finding{
		{ID: "HOME-58:goal:criterion-2", Severity: SeverityMedium},
		{ID: "HOME-58:goal:criterion-3", Severity: SeverityMedium},
		{ID: "HOME-57:goal", Severity: SeverityHigh},
		{ID: "x.go:goal", Severity: SeverityMedium},
	}
	r := &Report{
		Findings: []Finding{{ID: "HOME-58:goal:criterion-9", Severity: SeverityHigh, Title: "Model goal finding"}},
		Previous: []Previous{
			{ID: "HOME-58:goal:criterion-2", Status: StatusJustified, Note: "Joe agreed"},
			{ID: "x.go:goal", Status: StatusJustified, Note: "Owned by HOME-60"},
		},
	}
	applyGoal(r, prior, "HOME", goalFindings("HOME-58", &GoalCheck{Criteria: []GoalVerdict{{2, goalNotMet}, {3, goalUnclear}}}))
	a := Assess(r, prior)
	blocking := map[string]string{}
	for _, b := range a.Blocking {
		blocking[b.ID] = b.Severity
	}
	if len(blocking) != 1 || blocking["HOME-58:goal:criterion-2"] != SeverityMedium {
		t.Fatalf("blocking = %v", blocking)
	}
	if len(a.Accepted) != 1 || a.Accepted[0].ID != "x.go:goal" || len(a.Fixed) != 1 || a.Fixed[0].ID != "HOME-57:goal" ||
		len(a.Optional) != 1 || a.Optional[0].ID != "HOME-58:goal:criterion-3" {
		t.Fatalf("assessment = %+v", a)
	}
}

// goalTexts returns the text of each goal check message sent so far.
func goalTexts(h *harness) []string {
	var out []string
	for _, m := range h.k.sent() {
		if strings.Contains(m.MessageID, "/goal/") {
			out = append(out, m.Parts[0].Text)
		}
	}
	return out
}

// published is everything the head's check and the PR comment show.
func published(t *testing.T, h *harness) string {
	t.Helper()
	c := h.gh.check(shaA)
	st, _ := LoadState(c.Text)
	state, _ := json.Marshal(st)
	return c.Title + c.Summary + c.Text + string(state) + onlyComment(t, h, 7).Body
}

func TestGoalCheckPublishesOnlyServiceWording(t *testing.T) {
	h, api := issueHarness(t)
	withComments(h)
	cleaner := &fakeCleaner{}
	h.cfg.Cleaner = cleaner
	api.issue.Description = "1. PRIVATE-ONE\n2. PRIVATE-TWO\n3. PRIVATE-THREE"
	h.k.goal = "PRIVATE-TWO is missing.\n\n```json\n" +
		`{"criteria": [{"criterion": 1, "verdict": "met"}, {"criterion": 2, "verdict": "not_met"}, {"criterion": 3, "verdict": "unclear"}], "overall": "met"}` + "\n```"
	e, ctx, _ := h.start()
	task := runReview(t, h, e, ctx, 7, shaA)
	h.k.finish(task, kagent.StateCompleted, report(t, passing))
	e.Wait()
	goals := goalTexts(h)
	if len(goals) != 1 || !strings.Contains(goals[0], "PRIVATE-TWO") || strings.Contains(h.k.sent()[0].Parts[0].Text, "PRIVATE") {
		t.Fatal("only the goal check may receive the issue")
	}
	out := published(t, h)
	if strings.Contains(out, "PRIVATE") || !strings.Contains(out, "HOME-58 criterion 2: not met") || !strings.Contains(out, "HOME-58 criterion 3: unclear") {
		t.Fatalf("published = %q", out)
	}
	c := h.gh.check(shaA)
	st, _ := LoadState(c.Text)
	if c.Conclusion != ConclusionFailure || st.Goal == nil || len(st.Goal.Criteria) != 3 || st.Goal.Head != shaA {
		t.Fatalf("check = %s, goal %+v", c.Conclusion, st.Goal)
	}
	if d := cleaner.deletions(); len(d) != 1 || d[0] == "" {
		t.Fatalf("goal session deletions = %v", d)
	}
}

func TestInvalidGoalAnswerPublishesOnlyTheFixedMessage(t *testing.T) {
	for name, reply := range map[string]string{
		"extra field":  goalReply(`{"criteria": [{"criterion": 1, "verdict": "met", "why": "PRIVATE-GOAL"}]}`),
		"text verdict": goalReply(`{"criteria": [{"criterion": 1, "verdict": "PRIVATE-GOAL"}]}`),
		"malformed":    "PRIVATE-GOAL is not met.",
	} {
		t.Run(name, func(t *testing.T) {
			h, api := issueHarness(t)
			withComments(h)
			api.issue.Description = "1. PRIVATE-GOAL"
			h.k.goal = reply
			e, ctx, _ := h.start()
			task := runReview(t, h, e, ctx, 7, shaA)
			h.k.finish(task, kagent.StateCompleted, report(t, passing))
			e.Wait()
			out := published(t, h)
			st, _ := LoadState(h.gh.check(shaA).Text)
			if strings.Contains(out, "PRIVATE") || !strings.Contains(out, "HOME-58: goal check unavailable") || st.Goal != nil ||
				h.gh.check(shaA).Conclusion != ConclusionFailure {
				t.Fatalf("published = %q", out)
			}
		})
	}
}

// Only a trusted author's PR whose issue resolves costs a goal check.
func TestGoalCheckOnlyForTrustedAuthorsWithAnIssue(t *testing.T) {
	for name, pr := range map[string]struct{ author, body string }{
		"outsider": {"outsider", "Refs HOME-58"}, "no issue": {"jomcgi", "Update the pin"},
	} {
		t.Run(name, func(t *testing.T) {
			h, _ := issueHarness(t)
			h.gh.withLock(func() { h.gh.prs[7].Author, h.gh.prs[7].Body = pr.author, pr.body })
			e, ctx, _ := h.start()
			task := runReview(t, h, e, ctx, 7, shaA)
			h.k.finish(task, kagent.StateCompleted, report(t, passing))
			e.Wait()
			if len(goalTexts(h)) != 0 || h.gh.check(shaA).Conclusion != ConclusionSuccess {
				t.Fatal("goal check ran")
			}
		})
	}
}

// The State caches the answer per head and evidence digest: a recovered read
// reuses it, and only changed evidence asks again.
func TestGoalCheckRunsOncePerHeadAndEvidence(t *testing.T) {
	h, api := issueHarness(t)
	api.comments[1] = &itsaplan.FeedItem{ID: 1, IssueID: 95, Kind: "comment", ActorUserID: "human", Body: "Decision", EditedAt: []byte("null")}
	h.gh.withLock(func() {
		h.gh.prs[7].Files = linkedFiles("https://factory-plan.jomcgi.dev/factory/issue/HOME-58#comment-1")
	})
	e, ctx, _ := h.start()
	review := func(want int) {
		t.Helper()
		var task string
		eventually(t, "a review task", func() bool {
			e.Tick(ctx)
			task = h.taskOn(7, shaA, 1)
			return task != "" && h.gh.check(shaA).Status != "completed"
		})
		h.k.finish(task, kagent.StateCompleted, report(t, passing))
		e.Wait()
		if c := h.gh.check(shaA); c.Conclusion != ConclusionSuccess || len(goalTexts(h)) != want {
			t.Fatalf("check %s after %d goal checks, want %d", c.Conclusion, len(goalTexts(h)), want)
		}
	}
	review(1)
	api.fail = "project"
	e.Tick(ctx)
	api.fail = ""
	review(1)
	if !strings.Contains(goalTexts(h)[0], "Decision") {
		t.Fatal("goal prompt lacks the linked decision")
	}
	api.issue.Description = "Changed criteria"
	e.Tick(ctx)
	review(2)
}
