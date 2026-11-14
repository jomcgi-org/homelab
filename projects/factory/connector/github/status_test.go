package github

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestStatusUsesOneBoundedSummaryQuery(t *testing.T) {
	reads := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		reads++
		if r.URL.Path != "/graphql" { t.Errorf("path = %s", r.URL.Path) }
		var request struct { Query string; Variables map[string]any }
		if err := json.NewDecoder(r.Body).Decode(&request); err != nil { t.Error(err) }
		for _, forbidden := range []string{"body", "summary", "text", "reviews(", "reviewThreads"} {
			if strings.Contains(request.Query, forbidden) { t.Errorf("query reads %s", forbidden) }
		}
		if request.Variables["number"] != float64(8000) || request.Variables["appId"] != float64(123) || request.Variables["hasReviewer"] != true {
			t.Errorf("variables = %v", request.Variables)
		}
		_, _ = w.Write([]byte(`{"data":{"repository":{"pullRequest":{"number":8000,"url":"https://github.com/org/repo/pull/8000","reviewDecision":"APPROVED","mergeQueueEntry":{"state":"QUEUED"},"commits":{"nodes":[{"commit":{"statusCheckRollup":{"state":"SUCCESS"},"checkSuites":{"nodes":[{"checkRuns":{"nodes":[{"status":"COMPLETED","conclusion":"FAILURE"}]}}]}}}]}}}}}`))
	}))
	defer srv.Close()
	c := &Client{BaseURL: srv.URL, Tokens: fixedTokens{}}
	s, err := c.Status(context.Background(), "org/repo", 8000, CheckRunFilter{AppID: 123, Name: "factory review"})
	if err != nil { t.Fatal(err) }
	if reads != 1 || s.CI != "success" || s.Review != "failure" || s.Queue != "queued" { t.Fatalf("status=%+v, reads=%d", s, reads) }
}

func TestStatusMissingChecksAndPullRequest(t *testing.T) {
	for _, exists := range []bool{true, false} {
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
			if exists { _, _ = w.Write([]byte(`{"data":{"repository":{"pullRequest":{"number":1,"reviewDecision":"APPROVED"}}}}`))
			} else { _, _ = w.Write([]byte(`{"data":{"repository":{"pullRequest":null}}}`)) }
		}))
		c := &Client{BaseURL: srv.URL, Tokens: fixedTokens{}}
		s, err := c.Status(context.Background(), "org/repo", 1, CheckRunFilter{})
		srv.Close()
		if !exists {
			if err != ErrNotFound { t.Fatalf("err=%v", err) }
		} else if err != nil || s.CI != "unknown" || s.Review != "approved" || s.Queue != "not_queued" { t.Fatalf("status=%+v err=%v", s, err) }
	}
}
