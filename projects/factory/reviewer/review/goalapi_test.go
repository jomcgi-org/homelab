package review

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
	"time"
)

// TestGoalReasonsAPIServesTheSelectedReplyForAHead returns a head's selected
// goal reply without its JSON answer, and nothing for another head, a bad
// query or another method.
func TestGoalReasonsAPIServesTheSelectedReplyForAHead(t *testing.T) {
	a, err := OpenGoalAudit(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	head, other := strings.Repeat("a", 40), strings.Repeat("b", 40)
	now := time.Now().UTC()
	for _, r := range []GoalAuditRecord{
		{
			Repository: "o/r", PullRequest: 7, Head: head, Issue: "HOME-1", Digest: "d1", MessageID: "m1", TaskID: "t1", Phase: "initial", CreatedAt: now,
			Reply: "Old reasons.\n\n```json\n{\"criteria\":[],\"overall\":\"not_met\"}\n```",
		},
		{
			Repository: "o/r", PullRequest: 7, Head: head, Issue: "HOME-1", Digest: "d1", MessageID: "m2", TaskID: "t2", Phase: "confirmation", CreatedAt: now.Add(time.Second),
			Initial: &GoalCheck{Overall: goalNotMet, Criteria: []GoalVerdict{{1, goalMet}, {2, goalNotMet}}},
			Reply:   "Overall: retries fail.\nCriterion 1: ignored.\nCriterion 2: missing retry.\n\n```json\n{\"criteria\":[{\"criterion\":1,\"verdict\":\"not_met\"},{\"criterion\":2,\"verdict\":\"not_met\"}],\"overall\":\"not_met\"}\n```",
		},
		{Repository: "o/r", PullRequest: 7, Head: other, Issue: "HOME-1", Digest: "d1", MessageID: "m3", TaskID: "t3", CreatedAt: now, Reply: "```json\n{}\n```"},
	} {
		if err := a.Save(r); err != nil {
			t.Fatal(err)
		}
	}
	if _, ok := a.Reasons("o/r", 7, head); ok {
		t.Fatal("unselected reply served")
	}
	if err := a.selectGoal("o/r", 7, head, "d2", auditID("m2", "t2"), now); err != nil {
		t.Fatal(err)
	}
	if _, ok := a.Reasons("o/r", 7, head); ok {
		t.Fatal("stale evidence served")
	}
	if err := a.selectGoal("o/r", 7, head, "d1", auditID("m2", "t2"), now); err != nil {
		t.Fatal(err)
	}
	srv := httptest.NewServer(GoalReasonsHandler(a))
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
	resp, g := get(http.MethodGet, "repository=o/r&pullRequest=7&head="+head)
	if resp.StatusCode != http.StatusOK || len(g.Findings) != 3 || g.Findings[0] != "Overall: retries fail." || g.Findings[2] != "Criterion 2: missing retry." || g.Phase != "confirmation" ||
		g.Issue != "HOME-1" || g.Digest != "d1" || g.Head != head || resp.Header.Get("Cache-Control") != "no-store" {
		t.Fatalf("answer = %d %+v", resp.StatusCode, g)
	}
	for query, want := range map[string]int{
		"repository=o/r&pullRequest=7&head=" + other:        http.StatusNotFound, // only an answer block
		"repository=o/r&pullRequest=8&head=" + head:         http.StatusNotFound,
		"repository=o/r&pullRequest=7&head=abc":             http.StatusBadRequest,
		"repository=o/r/x&pullRequest=7&head=" + head:       http.StatusBadRequest,
		"repository=o/r&pullRequest=-1&head=" + head:        http.StatusBadRequest,
		"repository=o%2Fr%0A&pullRequest=7&head=" + head:    http.StatusBadRequest,
		"repository=o/r&pullRequest=7&head=" + head + "%0A": http.StatusBadRequest,
	} {
		if resp, _ := get(http.MethodGet, query); resp.StatusCode != want {
			t.Errorf("%s = %d, want %d", query, resp.StatusCode, want)
		}
	}
	if resp, _ := get(http.MethodPost, "repository=o/r&pullRequest=7&head="+head); resp.StatusCode != http.StatusNotFound {
		t.Errorf("POST = %d", resp.StatusCode)
	}
}

// TestGoalReasonsAPIUnreadableAuditIsUnavailable answers 503, not 404, when
// the audit directory cannot be read, so the connector retries the read.
func TestGoalReasonsAPIUnreadableAuditIsUnavailable(t *testing.T) {
	dir := t.TempDir()
	a, err := OpenGoalAudit(dir)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.RemoveAll(dir); err != nil {
		t.Fatal(err)
	}
	srv := httptest.NewServer(GoalReasonsHandler(a))
	defer srv.Close()
	resp, err := srv.Client().Get(srv.URL + GoalReasonsPath + "?repository=o/r&pullRequest=7&head=" + strings.Repeat("a", 40))
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusServiceUnavailable {
		t.Fatalf("unreadable audit = %d, want 503", resp.StatusCode)
	}
}
