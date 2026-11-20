package review

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"

	"github.com/jomcgi/homelab/projects/factory/connector/kagent"
)

// invariant: FACT-13
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
