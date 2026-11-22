package review

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
)

// invariant: FACT-302
// TestGoalReasonsAPIServesTheSelectedReplyForAHead returns a head's selected
// goal reply without its JSON answer, and nothing for another head, a bad
// query or another method.
func TestGoalReasonsAPIServesTheSelectedReplyForAHead(t *testing.T) {
	h, api := issueHarness(t)
	head, other := shaA, strings.Repeat("b", 40)
	h.k.goalReplies = []string{
		goalReply(`{"criteria":[{"criterion":1,"verdict":"met"},{"criterion":2,"verdict":"not_met"}],"overall":"not_met"}`),
		"Overall: retries fail.\nCriterion 1: new failure.\nCriterion 2: missing retry.\n" + goalReply(`{"criteria":[{"criterion":1,"verdict":"not_met"},{"criterion":2,"verdict":"not_met"}],"overall":"not_met"}`),
	}
	e, ctx, _ := h.start()
	h.k.finish(runReview(t, h, e, ctx, 7, head), kagent.StateCompleted, report(t, passing))
	e.Wait()
	srv := httptest.NewServer(GoalReasonsHandler(e))
	defer srv.Close()
	get := func(method, query string) (*http.Response, GoalReasons) {
		t.Helper()
		req, _ := http.NewRequest(method, srv.URL+GoalReasonsPath+"?"+query, nil)
		resp, err := srv.Client().Do(req)
		if err != nil {
			t.Fatal(err)
		}
		defer resp.Body.Close()
		var g GoalReasons
		_ = json.NewDecoder(resp.Body).Decode(&g)
		return resp, g
	}
	resp, g := get(http.MethodGet, "repository="+testRepo+"&pullRequest=7&head="+head)
	if resp.StatusCode != http.StatusOK || len(g.Findings) != 3 || g.Findings[0] != "Overall: retries fail." || g.Findings[2] != "Criterion 2: missing retry." || g.Phase != "confirmation" ||
		g.Issue != "HOME-58" || g.Digest == "" || g.Head != head || resp.Header.Get("Cache-Control") != "no-store" {
		t.Fatalf("answer = %d %+v", resp.StatusCode, g)
	}
	dir := h.cfg.GoalAudit.(*FileGoalAudit).dir
	if err := os.Rename(dir, dir+".offline"); err != nil {
		t.Fatal(err)
	}
	if resp, _ := get(http.MethodGet, "repository="+testRepo+"&pullRequest=7&head="+head); resp.StatusCode != http.StatusServiceUnavailable {
		t.Fatalf("unreadable audit = %d", resp.StatusCode)
	}
	if err := os.Rename(dir+".offline", dir); err != nil {
		t.Fatal(err)
	}
	api.issue.Description += " Changed evidence."
	if resp, _ := get(http.MethodGet, "repository="+testRepo+"&pullRequest=7&head="+head); resp.StatusCode != http.StatusNotFound {
		t.Fatal("served superseded evidence before the next review poll")
	}
	for query, want := range map[string]int{
		"repository=" + testRepo + "&pullRequest=7&head=" + other:        http.StatusNotFound, // only an answer block
		"repository=" + testRepo + "&pullRequest=8&head=" + head:         http.StatusNotFound,
		"repository=" + testRepo + "&pullRequest=7&head=abc":             http.StatusBadRequest,
		"repository=o/r/x&pullRequest=7&head=" + head:                    http.StatusBadRequest,
		"repository=" + testRepo + "&pullRequest=-1&head=" + head:        http.StatusBadRequest,
		"repository=o%2Fr%0A&pullRequest=7&head=" + head:                 http.StatusBadRequest,
		"repository=" + testRepo + "&pullRequest=7&head=" + head + "%0A": http.StatusBadRequest,
	} {
		if resp, _ := get(http.MethodGet, query); resp.StatusCode != want {
			t.Errorf("%s = %d, want %d", query, resp.StatusCode, want)
		}
	}
	if resp, _ := get(http.MethodPost, "repository="+testRepo+"&pullRequest=7&head="+head); resp.StatusCode != http.StatusNotFound {
		t.Errorf("POST = %d", resp.StatusCode)
	}
}

// TestJSONOnlyGoalAssessmentReturnsTicketFeedback summarizes the selected
// check's verdicts when its reply has no reasons, and keeps the ticket reply
// revision it judged.
func TestJSONOnlyGoalAssessmentReturnsTicketFeedback(t *testing.T) {
	a, err := OpenGoalAudit(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	head := strings.Repeat("a", 40)
	if err := a.Save(GoalAuditRecord{
		Repository: "o/r", PullRequest: 7, Head: head, Issue: "HOME-1", Digest: "d1",
		MessageID: "m", TaskID: "t", ReplyRevision: 17, CreatedAt: time.Now(),
		Reply: "```json\n{\"criteria\":[{\"criterion\":1,\"verdict\":\"not_met\"}],\"overall\":\"not_met\"}\n```",
	}); err != nil {
		t.Fatal(err)
	}
	selected := &GoalCheck{AuditID: auditID("m", "t"), Head: head, Digest: "d1", Overall: goalNotMet, Criteria: []GoalVerdict{{1, goalNotMet}}}
	g, ok, err := a.reasons("o/r", 7, head, "HOME-1", selected)
	if err != nil || !ok || g.ReplyRevision != 17 || !strings.Contains(g.Reasons, "Criterion 1: not met.") {
		t.Fatalf("ticket assessment = %+v, %v, %v", g, ok, err)
	}
	// Another digest, issue or audit ID selects nothing.
	for _, s := range []*GoalCheck{
		{AuditID: selected.AuditID, Digest: "d2"},
		{AuditID: auditID("m", "other"), Digest: "d1"},
	} {
		if _, ok, _ := a.reasons("o/r", 7, head, "HOME-1", s); ok {
			t.Errorf("selected %+v answered", s)
		}
	}
	if _, ok, _ := a.reasons("o/r", 7, head, "HOME-2", selected); ok {
		t.Error("another issue answered")
	}
}

// invariant: FACT-302
// TestUnauditedGoalCheckRefreshesBeforeServingReasons holds a completed check
// whose goal verdict names no audit record, as one published before
// GoalCheck.AuditID. The API answers 404 until a fresh goal call on the same
// head records an AuditID; the stored verdict is not reused for it.
func TestUnauditedGoalCheckRefreshesBeforeServingReasons(t *testing.T) {
	h, _ := issueHarness(t)
	h.k.goal = "Overall: retries fail.\nCriterion 1: missing retry.\n" + goalReply(`{"criteria":[{"criterion":1,"verdict":"not_met"},{"criterion":2,"verdict":"met"}],"overall":"not_met"}`)
	e, ctx, _ := h.start()
	h.k.finish(runReview(t, h, e, ctx, 7, shaA), kagent.StateCompleted, report(t, passing))
	e.Wait()
	goalCalls := func() (n int) {
		for _, m := range h.k.sent() {
			if strings.Contains(m.MessageID, "/goal/") {
				n++
			}
		}
		return n
	}
	before := goalCalls()
	if before == 0 {
		t.Fatal("no goal call")
	}
	// Make the check a legacy one: same verdict and digest, no AuditID.
	h.gh.withLock(func() {
		for _, c := range h.gh.checks {
			if c.SHA != shaA || c.Name != "factory review" {
				continue
			}
			st, ok := LoadState(c.Text)
			if !ok || st.Goal == nil || st.Goal.AuditID == "" {
				t.Fatalf("published goal = %+v", st.Goal)
			}
			st.Goal.AuditID = ""
			c.Text = encodeState(st)
		}
	})
	srv := httptest.NewServer(GoalReasonsHandler(e))
	defer srv.Close()
	status := func() int {
		t.Helper()
		resp, err := srv.Client().Get(srv.URL + GoalReasonsPath + "?repository=" + testRepo + "&pullRequest=7&head=" + shaA)
		if err != nil {
			t.Fatal(err)
		}
		resp.Body.Close()
		return resp.StatusCode
	}
	if got := status(); got != http.StatusNotFound {
		t.Fatalf("unaudited check answered %d", got)
	}
	reviews := func() int { return len(h.k.sent()) - goalCalls() }
	started := reviews()
	e.Tick(ctx)
	eventually(t, "a fresh review on the same head", func() bool { return reviews() > started })
	if got := status(); got != http.StatusNotFound {
		t.Fatalf("held check answered %d", got)
	}
	h.k.finishOpen(kagent.StateCompleted, report(t, passing))
	e.Wait()
	if goalCalls() == before {
		t.Fatal("the refresh reused the unaudited verdict")
	}
	st, _ := LoadState(h.gh.check(shaA).Text)
	if st.IssuesPending || st.Goal == nil || !auditHash.MatchString(st.Goal.AuditID) {
		t.Fatalf("refreshed goal = %+v", st.Goal)
	}
	if got := status(); got != http.StatusOK {
		t.Fatalf("refreshed check answered %d", got)
	}
}
