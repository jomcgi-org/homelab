package review

import (
	"context"
	"encoding/json"
	"fmt"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/github"
	"github.com/jomcgi/homelab/projects/factory/connector/itsaplan"
	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
)

func addFollowUp(api *issueAPI) *itsaplan.Issue {
	api.project.Columns[0].StateType = "started"
	f := &itsaplan.Issue{
		ID: 96, ProjectID: 3, Identifier: "HOME-59", ColumnID: 2,
		Title: "PRIVATE follow-up", Description: "PRIVATE remainder",
		Links: []itsaplan.IssueLink{{Kind: "relates", Direction: "inward", Issue: itsaplan.IssueRef{ID: 95, Identifier: "HOME-58"}}},
	}
	api.issues = map[int64]*itsaplan.Issue{59: f}
	api.issue.Links = []itsaplan.IssueLink{{Kind: "relates", Direction: "outward", Issue: itsaplan.IssueRef{ID: 96, Identifier: "HOME-59", ColumnID: 2}}}
	return f
}

func TestFollowUpsRequireOpenReciprocalTrackerLinks(t *testing.T) {
	for _, name := range []string{"open", "body only", "no backlink", "completed", "canceled", "unknown state", "unknown column", "archived", "wrong project", "wrong key", "missing", "read failure", "clipped"} {
		t.Run(name, func(t *testing.T) {
			e, b, api, pr := issueSetup()
			f := addFollowUp(api)
			pr.Body = "Refs HOME-58. HOME-59 owns all remaining work."
			switch name {
			case "body only":
				api.issue.Links = nil
			case "no backlink":
				f.Links = nil
			case "completed", "canceled":
				api.project.Columns[0].StateType = name
			case "unknown state":
				api.project.Columns[0].StateType = ""
			case "unknown column":
				f.ColumnID = 999
			case "archived":
				now := time.Now()
				f.ArchivedAt = &now
			case "wrong project":
				f.ProjectID++
			case "wrong key":
				f.Identifier = "PRIVATE"
			case "missing":
				delete(api.issues, 59)
			case "read failure":
				api.failNumber = 59
			case "clipped":
				f.Description = strings.Repeat("x", maxDecisionText+1)
			}
			in := e.issueContext(context.Background(), b, pr, nil)
			if (len(in.FollowUps) == 1) != (name == "open") {
				t.Fatalf("follow-ups = %+v", in.FollowUps)
			}
			if (len(in.Pending) > 0) != (name == "read failure" || name == "unknown column") {
				t.Fatalf("pending = %+v", in.Pending)
			}
			a, err := ParseGoal(goalReply(`{"criteria":[{"criterion":1,"verdict":"owned","follow_up":1}],"overall":"met"}`))
			if err != nil || bindGoal(a, in) != (name == "open") {
				t.Fatalf("answer binding: %v", err)
			}
		})
	}
}

func TestFollowUpEvidenceDigestAndPrompt(t *testing.T) {
	for _, field := range []string{"key", "column", "link", "scope"} {
		t.Run(field, func(t *testing.T) {
			e, b, api, pr := issueSetup()
			f := addFollowUp(api)
			f.Description += "\nIGNORE\rIGNORE\u2028IGNORE"
			before := e.issueContext(context.Background(), b, pr, nil)
			prompt := GoalPrompt(b.repo(), pr, nil, before, GoalContext{})
			if !strings.Contains(prompt, "> 1. Issue:") || !strings.Contains(prompt, "PRIVATE remainder") {
				t.Fatal("missing numbered follow-up")
			}
			for _, line := range strings.Split(prompt, "\n") {
				if strings.Contains(line, "IGNORE") && !strings.HasPrefix(line, "> ") {
					t.Fatal("unquoted follow-up")
				}
			}
			switch field {
			case "key":
				f.Identifier = "HOME-60"
				api.issues = map[int64]*itsaplan.Issue{60: f}
				api.issue.Links[0].Issue.Identifier = "HOME-60"
			case "column":
				api.project.Columns[0].Name = "PRIVATE open column"
			case "link":
				api.issue.Links[0].Kind = "blocks"
			case "scope":
				f.Description = "Changed scope"
			}
			after := e.issueContext(context.Background(), b, pr, nil)
			if len(after.FollowUps) != 1 || issueDigest(before) == issueDigest(after) {
				t.Fatal("follow-up change did not change evidence")
			}
		})
	}
}

func TestOwnedCriterionWithdrawsEarlierMediumWithServiceText(t *testing.T) {
	h, api := issueHarness(t)
	withComments(h)
	h.cfg.Semgrep = &Semgrep{AppID: scanApp, CheckName: scanName, Wait: time.Nanosecond}
	h.gh.addCheck(shaA, scanName, scanApp, "success")
	api.issue.Description = "## Done when\n- PRIVATE criterion"
	h.k.goal = goalReply(`{"criteria":[{"criterion":1,"verdict":"not_met"}],"overall":"met"}`)
	e, ctx, _ := h.start()
	task := runReview(t, h, e, ctx, 7, shaA)
	h.k.finish(task, kagent.StateCompleted, report(t, passing))
	e.Wait()
	if h.gh.check(shaA).Conclusion != ConclusionFailure {
		t.Fatal("missing original blocker")
	}
	addFollowUp(api)
	h.k.goal = "PRIVATE notes\n" + goalReply(`{"criteria":[{"criterion":1,"verdict":"owned","follow_up":1}],"overall":"met"}`)
	e.Tick(ctx)
	task = runReview(t, h, e, ctx, 7, shaA)
	h.k.finish(task, kagent.StateCompleted, report(t, passing))
	e.Wait()
	out := published(t, h)
	if h.gh.check(shaA).Conclusion != ConclusionSuccess || strings.Contains(out, "PRIVATE") || !strings.Contains(out, "owned by HOME-59") {
		t.Fatalf("owned criterion: %s", out)
	}
	st, _ := LoadState(h.gh.check(shaA).Text)
	if st.Semgrep == nil || !st.Semgrep.Verified || len(st.GoalHistory) != 1 ||
		len(st.GoalClosed) != 1 || st.GoalClosed[0].ID != "HOME-58:goal:criterion-1" {
		t.Fatal("completed review lost Semgrep evidence or the conditional goal resolution")
	}
	// Closing the owner revokes a pass on the same head, without a push.
	api.project.Columns[0].StateType = "completed"
	e.Tick(ctx)
	if h.gh.check(shaA).Conclusion == ConclusionSuccess {
		t.Fatal("closed owner retained the pass")
	}
}

func TestServiceCriteriaPreserveIdentityAcrossRemovalAndRestart(t *testing.T) {
	e, b, api, pr := issueSetup()
	api.issue.Description = "## Done when\n- PRIVATE first\n- PRIVATE removed\n- PRIVATE last"
	before := e.issueContext(context.Background(), b, pr, nil)
	st, ok := LoadState(encodeState(State{GoalHistory: before.History}))
	if !ok {
		t.Fatal("history was not persisted")
	}
	encoded, _ := json.Marshal(st)
	if strings.Contains(string(encoded), "PRIVATE") {
		t.Fatal("private criterion in public history")
	}
	api.issue.Description = "## Done when\n- PRIVATE last\n- PRIVATE first"
	after := e.issueContext(context.Background(), b, pr, nil, st.GoalHistory)
	if !after.CriteriaKnown || len(after.Criteria) != 2 || after.Criteria[0].Number != 3 || after.Criteria[1].Number != 1 {
		t.Fatalf("renumbered criteria: %+v", after.Criteria)
	}
	a, err := ParseGoal(goalReply(`{"criteria":[{"criterion":1,"verdict":"unclear"},{"criterion":2,"verdict":"met"}],"overall":"met"}`))
	if err != nil || !bindGoal(a, after) {
		t.Fatal("valid service list rejected")
	}
	prior := []Finding{{ID: "HOME-58:goal:criterion-2", Severity: SeverityMedium}, {ID: "HOME-58:goal:criterion-3", Severity: SeverityMedium}}
	r := &Report{}
	applyGoal(r, prior, "HOME", "HOME-58", &GoalCheck{Overall: a.Overall, Criteria: a.Criteria}, after)
	assessment := Assess(r, prior)
	if len(assessment.Fixed) != 1 || assessment.Fixed[0].ID != prior[0].ID || len(assessment.Blocking) != 1 || assessment.Blocking[0].ID != prior[1].ID {
		t.Fatalf("removal: %+v", assessment)
	}
	// An omitted tail is invalid even though the remaining model numbers
	// are contiguous. Missing model output is never removal evidence.
	a, _ = ParseGoal(goalReply(`{"criteria":[{"criterion":1,"verdict":"met"}],"overall":"met"}`))
	if bindGoal(a, after) {
		t.Fatal("model omission accepted")
	}
	api.issue.Description = "## Done when\n"
	empty := e.issueContext(context.Background(), b, pr, nil, after.History)
	valid := &GoalCheck{Overall: goalMet}
	if !empty.CriteriaKnown || goalResolved(prior[1].ID, "HOME-58", valid, empty) == "" {
		t.Fatal("empty acceptance section did not prove removal")
	}
	if goalResolved(prior[1].ID, "HOME-58", nil, empty) != "" {
		t.Fatal("removal resolved a finding without a valid current answer")
	}
	api.issue.Description = "Criteria in an unsupported format"
	unknown := e.issueContext(context.Background(), b, pr, nil, after.History)
	if goalResolved(prior[1].ID, "HOME-58", valid, unknown) != "" {
		t.Fatal("unknown criteria proved removal")
	}
	a, _ = ParseGoal(goalReply(`{"criteria":[],"overall":"met"}`))
	if bindGoal(a, unknown) {
		t.Fatal("lost inventory accepted a model omission")
	}
	// Reinstating text restores its original ID, including duplicate text.
	api.issue.Description = "## Done when\n- PRIVATE removed\n- PRIVATE removed"
	restored := e.issueContext(context.Background(), b, pr, nil, after.History)
	if restored.Criteria[0].Number != 2 || restored.Criteria[1].Number != 4 {
		t.Fatalf("restored IDs: %+v", restored.Criteria)
	}
}

func TestConditionalGoalResolutionsRequireCurrentAnswer(t *testing.T) {
	for _, tc := range []struct {
		name, id string
		in       *IssueContext
	}{
		{"removal", "HOME-58:goal:criterion-1", &IssueContext{
			CriteriaKnown: true, History: []GoalCriterionHistory{{Key: "HOME-58", Criteria: []string{"fingerprint"}}},
		}},
		{"switch", "HOME-57:goal", &IssueContext{
			Switches: []SwitchEvidence{{Key: "HOME-57", Retired: true}},
		}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			prior := []Finding{{ID: tc.id, Severity: SeverityMedium}}
			for _, valid := range []bool{false, true} {
				var g *GoalCheck
				if valid {
					g = &GoalCheck{Overall: goalMet}
				}
				r := &Report{Previous: []Previous{{ID: tc.id, Status: StatusFixed, Note: "PRIVATE model claim"}}}
				applyGoal(r, prior, "HOME", "HOME-58", g, tc.in)
				a := Assess(r, prior)
				if (len(a.Fixed) == 1) != valid || (a.Conclusion == ConclusionSuccess) != valid {
					t.Fatalf("valid answer %t: %+v", valid, a)
				}
				if !valid && !slices.ContainsFunc(a.Blocking, func(b Blocked) bool { return b.ID == tc.id }) {
					t.Fatal("missing answer discarded the earlier blocker")
				}
				for _, p := range r.Previous {
					if strings.Contains(p.Note, "PRIVATE") {
						t.Fatal("model text reached the resolution")
					}
				}
			}
		})
	}
}

func TestRemovedCriterionRequiresCurrentAnswerAndReopensAfterRestart(t *testing.T) {
	h, api := issueHarness(t)
	withComments(h)
	original := "## Done when\n- PRIVATE first\n- PRIVATE removed\n- PRIVATE last"
	api.issue.Description = original
	h.k.goal = goalReply(`{"criteria":[{"criterion":1,"verdict":"met"},{"criterion":2,"verdict":"not_met"},{"criterion":3,"verdict":"met"}],"overall":"met"}`)
	e, ctx, stop := h.start()
	task := runReview(t, h, e, ctx, 7, shaA)
	h.k.finish(task, kagent.StateCompleted, report(t, passing))
	e.Wait()
	id := "HOME-58:goal:criterion-2"
	st, _ := LoadState(h.gh.check(shaA).Text)
	if len(st.Blocking) != 1 || st.Blocking[0].ID != id {
		t.Fatalf("initial goal findings: %+v", st.Blocking)
	}
	stop()

	// Reconstruct the service from stored state. Reordering the remaining
	// items must not turn criterion 2 into a different, passing criterion.
	api.issue.Description = "## Done when\n- PRIVATE last\n- PRIVATE first"
	h.k.goal = "PRIVATE invalid answer"
	e, ctx, stop = h.start()
	e.Tick(ctx)
	task = runReview(t, h, e, ctx, 7, shaA)
	h.k.finish(task, kagent.StateCompleted, report(t, passing))
	e.Wait()
	st, _ = LoadState(h.gh.check(shaA).Text)
	if h.gh.check(shaA).Conclusion != ConclusionFailure ||
		!slices.ContainsFunc(st.Blocking, func(f Finding) bool { return f.ID == id }) || len(st.GoalClosed) != 0 {
		t.Fatal("removal cleared the blocker without a valid current answer")
	}

	// A new evidence snapshot permits another goal answer on the same head.
	api.issue.Title += " PRIVATE clarification"
	h.k.goal = goalReply(`{"criteria":[{"criterion":1,"verdict":"met"},{"criterion":2,"verdict":"met"}],"overall":"met"}`)
	e.Tick(ctx)
	task = runReview(t, h, e, ctx, 7, shaA)
	h.k.finish(task, kagent.StateCompleted, report(t, passing))
	e.Wait()
	st, _ = LoadState(h.gh.check(shaA).Text)
	if h.gh.check(shaA).Conclusion != ConclusionSuccess || st.Goal == nil ||
		len(st.Goal.Criteria) != 2 || st.Goal.Criteria[0].Criterion != 3 || st.Goal.Criteria[1].Criterion != 1 ||
		len(st.GoalClosed) != 1 || st.GoalClosed[0] != (GoalClosure{ID: id, Severity: SeverityMedium}) {
		t.Fatalf("removed criterion was not conditionally resolved: %+v", st)
	}
	if out := published(t, h); strings.Contains(out, "PRIVATE") || !strings.Contains(out, "no longer contains criterion 2") {
		t.Fatalf("removal published private text or lost the evidence note: %s", out)
	}
	stop()

	// Reintroducing the criterion after another restart restores the original
	// blocker. Neither the earlier pass nor a model omission can hide it.
	api.issue.Description = original
	e, ctx, _ = h.start()
	e.Tick(ctx)
	if h.gh.check(shaA).Conclusion == ConclusionSuccess {
		t.Fatal("restored criterion retained the old pass")
	}
	task = runReview(t, h, e, ctx, 7, shaA)
	h.k.finish(task, kagent.StateCompleted, report(t, passing))
	e.Wait()
	st, _ = LoadState(h.gh.check(shaA).Text)
	if h.gh.check(shaA).Conclusion != ConclusionFailure ||
		!slices.ContainsFunc(st.Blocking, func(f Finding) bool { return f.ID == id && f.Severity == SeverityMedium }) {
		t.Fatal("model omission lost the restored criterion's original blocker")
	}
}

func TestIssueCriteriaParser(t *testing.T) {
	for _, tc := range []struct {
		text  string
		known bool
		count int
	}{
		{"Problem\n\n**Done when:**\n- [ ] first\n  continuation\n  - nested\n- [x] second\n\n## Links\n- other", true, 2},
		{"1. first\n2. second", true, 2},
		{"## Acceptance criteria\n1) first", true, 1},
		{"## Done when\n\n## Links\n- not a criterion", true, 0},
		{"## Done when\nprose", false, 0},
		{"## Done when\n- first\n## Acceptance criteria\n- second", false, 0},
		{"## Done when\n```\n- example\n```", false, 0},
		{"prose only", false, 0},
	} {
		items, known := issueCriteria(tc.text)
		if known != tc.known || len(items) != tc.count {
			t.Errorf("%q: %v, %t", tc.text, items, known)
		}
	}
}

func TestCriteriaBoundsAndKeyRotationCannotProveRemoval(t *testing.T) {
	e, b, api, pr := issueSetup()
	api.issue.Description = "## Done when\n- first"
	before := e.issueContext(context.Background(), b, pr, nil)
	for _, change := range []string{"key", "count", "history"} {
		t.Run(change, func(t *testing.T) {
			e.cfg.MACSecret = testMACSecret
			api.issue.Description = "## Done when\n- changed"
			history := slices.Clone(before.History)
			switch change {
			case "key":
				e.cfg.MACSecret = func() (string, error) { return "rotated-test-key-0123456789", nil }
			case "count":
				api.issue.Description = "## Done when\n" + strings.Repeat("- criterion\n", maxCriteria+1)
			case "history":
				history[0].Criteria = make([]string, maxGoalHistory)
			}
			after := e.issueContext(context.Background(), b, pr, nil, history)
			if after.CriteriaKnown || len(after.Findings) == 0 || goalResolved("HOME-58:goal:criterion-1", "HOME-58", &GoalCheck{Overall: goalMet}, after) != "" {
				t.Fatal("unverifiable inventory proved removal")
			}
		})
	}
}

func TestIssueSwitchRequiresRetirementOrTrustedDecision(t *testing.T) {
	for _, name := range []string{"open", "canceled", "completed", "duplicate", "inward duplicate", "unrelated duplicate", "wrong duplicate id", "trusted decision", "untrusted decision", "edited decision", "missing", "read failure"} {
		t.Run(name, func(t *testing.T) {
			e, b, api, pr := issueSetup()
			old := &itsaplan.Issue{ID: 96, ProjectID: 3, Identifier: "HOME-57", ColumnID: 2}
			api.issues = map[int64]*itsaplan.Issue{57: old}
			files := linkedFiles("https://factory-plan.jomcgi.dev/factory/issue/HOME-58#comment-1")
			api.comments[1] = &itsaplan.FeedItem{ID: 1, IssueID: 95, Kind: "comment", ActorUserID: "human", Body: "PRIVATE correction: replace HOME-57 with HOME-58", EditedAt: []byte("null")}
			switch name {
			case "canceled", "completed":
				api.project.Columns[0].StateType = name
			case "duplicate", "inward duplicate", "unrelated duplicate", "wrong duplicate id":
				old.Links = []itsaplan.IssueLink{{Kind: "duplicates", Direction: "outward", Issue: itsaplan.IssueRef{ID: 95, Identifier: "HOME-58"}}}
				if name == "inward duplicate" {
					old.Links[0].Direction = "inward"
				}
				if name == "unrelated duplicate" {
					old.Links[0].Issue.Identifier = "HOME-99"
				}
				if name == "wrong duplicate id" {
					old.Links[0].Issue.ID = 999
				}
			case "untrusted decision":
				api.comments[1].ActorUserID = "bot"
			case "edited decision":
				api.comments[1].EditedAt = []byte(`"2026-11-17"`)
			case "missing":
				delete(api.issues, 57)
			case "read failure":
				api.failNumber = 57
			}
			in := e.issueContext(context.Background(), b, pr, files, []GoalCriterionHistory{{Key: "HOME-57"}})
			g := &GoalCheck{Overall: goalMet}
			if strings.Contains(name, "decision") {
				a, err := ParseGoal(goalReply(`{"criteria":[],"overall":"met","switches":[{"issue":1,"decision":1}]}`))
				if err != nil {
					t.Fatal(err)
				}
				valid := bindGoal(a, in)
				if valid != (name == "trusted decision") {
					t.Fatalf("decision accepted = %t", valid)
				}
				if valid {
					g.Switches = a.Switches
				}
			}
			want := name == "canceled" || name == "duplicate" || name == "trusted decision"
			for _, id := range []string{"HOME-57:goal", "HOME-57:goal:criterion-1", "HOME-57:goal:unavailable"} {
				note := goalResolved(id, "HOME-58", g, in)
				if (note != "") != want || strings.Contains(note, "PRIVATE") {
					t.Fatalf("resolution %s: %q", id, note)
				}
				if goalResolved(id, "HOME-58", nil, in) != "" {
					t.Fatal("switch resolved a finding without a valid current answer")
				}
			}
			if (len(in.Pending) > 0) != (name == "read failure") {
				t.Fatalf("pending = %+v", in.Pending)
			}
		})
	}
}

func TestCorrectedReferenceClosesCanceledGoalOnSameHead(t *testing.T) {
	h, api := issueHarness(t)
	h.k.goal = goalReply(`{"criteria":[],"overall":"not_met"}`)
	e, ctx, _ := h.start()
	task := runReview(t, h, e, ctx, 7, shaA)
	h.k.finish(task, kagent.StateCompleted, report(t, passing))
	e.Wait()
	if h.gh.check(shaA).Conclusion != ConclusionFailure {
		t.Fatal("missing earlier goal blocker")
	}
	api.project.Columns = append(api.project.Columns, itsaplan.Column{ID: 3, Name: "Canceled", StateType: "canceled"})
	api.issue.ColumnID = 3
	api.issues = map[int64]*itsaplan.Issue{59: {ID: 96, ProjectID: 3, Identifier: "HOME-59", ColumnID: 2, Title: "Corrected goal"}}
	h.gh.withLock(func() { h.gh.prs[7].Body = "Refs HOME-59" })
	h.k.goal = goalReply(`{"criteria":[],"overall":"met"}`)
	e.Tick(ctx)
	task = runReview(t, h, e, ctx, 7, shaA)
	h.k.finish(task, kagent.StateCompleted, report(t, passing))
	e.Wait()
	if h.gh.check(shaA).Conclusion != ConclusionSuccess {
		t.Fatalf("corrected issue: %s", h.gh.check(shaA).Summary)
	}
	st, _ := LoadState(h.gh.check(shaA).Text)
	if !slices.ContainsFunc(st.GoalHistory, func(h GoalCriterionHistory) bool { return h.Key == "HOME-58" }) {
		t.Fatal("old issue evidence lost")
	}
	// Reopening the old issue changes the evidence even after a restart.
	api.issue.ColumnID = 2
	e.Tick(ctx)
	if h.gh.check(shaA).Conclusion == ConclusionSuccess {
		t.Fatal("reopened issue retained the pass")
	}
	task = runReview(t, h, e, ctx, 7, shaA)
	h.k.finish(task, kagent.StateCompleted, report(t, passing))
	e.Wait()
	st, _ = LoadState(h.gh.check(shaA).Text)
	if !slices.ContainsFunc(st.Blocking, func(f Finding) bool { return f.ID == "HOME-58:goal" && f.Severity == SeverityHigh }) {
		t.Fatal("fresh review forgot the reopened issue's original blocker")
	}
}

func TestGoalReferenceFieldsRejectFreeTextAndInvalidNumbers(t *testing.T) {
	for _, fields := range []string{
		`"verdict":"owned"`, `"verdict":"owned","follow_up":0`, `"verdict":"owned","follow_up":11`,
		`"verdict":"owned","follow_up":"PRIVATE"`, `"verdict":"owned","follow_up":1,"key":"PRIVATE"`,
		`"verdict":"met","follow_up":1`,
	} {
		if _, err := ParseGoal(goalReply(`{"criteria":[{"criterion":1,` + fields + `}],"overall":"met"}`)); err == nil {
			t.Errorf("accepted %s", fields)
		}
	}
	for _, fields := range []string{`"issue":1,"decision":"PRIVATE"`, `"issue":0,"decision":1`, `"issue":1,"decision":0`, `"issue":1,"decision":1,"why":"PRIVATE"`} {
		if _, err := ParseGoal(goalReply(fmt.Sprintf(`{"criteria":[],"overall":"met","switches":[{%s}]}`, fields))); err == nil {
			t.Errorf("accepted %s", fields)
		}
	}
	if _, err := ParseGoal(goalReply(`{"criteria":[],"overall":"owned"}`)); err == nil {
		t.Fatal("ownership resolved the stated problem")
	}
}

func TestGoalConfirmationBindsFollowUpEvidence(t *testing.T) {
	for _, followUp := range []int{1, 2} {
		t.Run(fmt.Sprint(followUp), func(t *testing.T) {
			h, api := issueHarness(t)
			withComments(h)
			api.issue.Description = "## Done when\n- PRIVATE criterion"
			addFollowUp(api)
			initial := goalReply(`{"criteria":[{"criterion":1,"verdict":"not_met"}],"overall":"met"}`)
			confirmation := "PRIVATE rationale\n" + goalReply(fmt.Sprintf(`{"criteria":[{"criterion":1,"verdict":"owned","follow_up":%d}],"overall":"met"}`, followUp))
			h.k.goalReplies = []string{initial, confirmation}
			e, ctx, _ := h.start()
			h.k.finish(runReview(t, h, e, ctx, 7, shaA), kagent.StateCompleted, report(t, passing))
			e.Wait()
			c := h.gh.check(shaA)
			st, _ := LoadState(c.Text)
			if len(goalTexts(h)) != 2 {
				t.Fatal("negative answer must receive one independent confirmation")
			}
			if followUp == 1 {
				if c.Conclusion != ConclusionSuccess || st.Goal == nil || len(st.Goal.Criteria) != 1 ||
					st.Goal.Criteria[0].Verdict != goalOwned || st.Goal.Criteria[0].FollowUp != 1 ||
					!strings.Contains(published(t, h), "owned by HOME-59") {
					t.Fatal("confirmation lost the verified follow-up")
				}
			} else if c.Conclusion != ConclusionFailure || st.Goal != nil || len(st.Blocking) != 1 ||
				st.Blocking[0].ID != "HOME-58:goal:unavailable" {
				t.Fatal("confirmation accepted an unverified follow-up")
			}
			records := auditRecords(t, h.cfg.GoalAudit.(*FileGoalAudit))
			if len(records) != 2 || records[0].SessionID == records[1].SessionID {
				t.Fatal("both independent replies must remain in the private audit")
			}
			replies := map[string]string{}
			for _, r := range records {
				replies[r.Phase] = r.Reply
			}
			if replies["initial"] != initial || replies["confirmation"] != confirmation ||
				strings.Contains(published(t, h)+h.log.String(), "PRIVATE") {
				t.Fatal("raw replies must remain private, including invalid confirmations")
			}
		})
	}
}

func TestGoalConfirmationPreservesFollowUpReferences(t *testing.T) {
	in := &IssueContext{
		CriteriaKnown: true,
		Criteria:      []GoalCriterion{{Number: 3}, {Number: 1}, {Number: 7}},
		FollowUps:     []FollowUpEvidence{{Key: "HOME-59"}, {Key: "HOME-60"}},
	}
	bound := func(reply string) *GoalCheck {
		t.Helper()
		a, err := ParseGoal(goalReply(reply))
		if err != nil || !bindGoal(a, in) {
			t.Fatalf("answer binding failed: %v", err)
		}
		return &GoalCheck{Overall: a.Overall, Criteria: a.Criteria}
	}
	initial := bound(`{"criteria":[{"criterion":1,"verdict":"owned","follow_up":1},{"criterion":2,"verdict":"met"},{"criterion":3,"verdict":"not_met"}],"overall":"met"}`)
	confirmed := bound(`{"criteria":[{"criterion":1,"verdict":"owned","follow_up":2},{"criterion":2,"verdict":"owned","follow_up":1},{"criterion":3,"verdict":"owned","follow_up":2}],"overall":"not_met"}`)
	beforeInitial, beforeConfirmed := slices.Clone(initial.Criteria), slices.Clone(confirmed.Criteria)
	got := confirmOnly(initial, confirmed)
	want := []GoalVerdict{
		{Criterion: 3, Verdict: goalOwned, FollowUp: 1},
		{Criterion: 1, Verdict: goalMet},
		{Criterion: 7, Verdict: goalOwned, FollowUp: 2},
	}
	if got.Overall != goalMet || !slices.Equal(got.Criteria, want) {
		t.Fatalf("confirmation changed a settled criterion or lost its owner: %+v", got)
	}
	if !slices.Equal(initial.Criteria, beforeInitial) || !slices.Equal(confirmed.Criteria, beforeConfirmed) {
		t.Fatal("confirmation mutated a source answer")
	}
}

func TestGoalHistoryUsesCurrentDisplayNumbers(t *testing.T) {
	in := &IssueContext{
		Issue:         &IssueEvidence{Key: "HOME-58"},
		CriteriaKnown: true,
		Criteria:      []GoalCriterion{{Number: 3, Text: "Remaining work"}, {Number: 1, Text: "Implemented work"}},
		FollowUps:     []FollowUpEvidence{{Key: "HOME-59"}},
	}
	previous := &GoalCheck{Head: "earlier", Digest: "same", Overall: goalMet, Criteria: []GoalVerdict{
		{Criterion: 3, Verdict: goalOwned, FollowUp: 1},
		{Criterion: 1, Verdict: goalMet},
	}}
	got := GoalPrompt("o/r", github.PullRequest{HeadSHA: shaA}, nil, in, GoalContext{Previous: previous})
	if !strings.Contains(got, "- criterion 1: owned; follow_up: 1\n- criterion 2: met\n") || strings.Contains(got, "- criterion 3:") {
		t.Fatalf("earlier verdicts do not use the displayed criterion numbers:\n%s", got)
	}
	// Answering those display numbers must return to the same stored IDs.
	a, err := ParseGoal(goalReply(`{"criteria":[{"criterion":1,"verdict":"owned","follow_up":1},{"criterion":2,"verdict":"unclear"}],"overall":"met"}`))
	if err != nil || !bindGoal(a, in) {
		t.Fatalf("history answer binding failed: %v", err)
	}
	current := keepEarlier(&GoalCheck{Digest: "same", Overall: a.Overall, Criteria: a.Criteria}, previous)
	if !slices.Equal(current.Criteria, previous.Criteria) {
		t.Fatalf("earlier verdicts were applied to different criteria: %+v", current.Criteria)
	}
}
