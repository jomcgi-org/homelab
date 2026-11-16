package github

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strconv"
	"testing"
	"time"
)

func TestPullRequestsUpdatedSinceStopsAtTheBoundAndRereadsTheFirstPage(t *testing.T) {
	since := time.Date(2026, 11, 15, 0, 0, 0, 0, time.UTC)
	for _, tc := range []struct {
		name     string
		count    int // PRs updated since, one minute apart, before older ones
		older    int
		broken   bool
		complete bool
		err      bool
	}{
		{name: "one short page", count: 3, complete: true},
		{name: "stops at the first older page", count: 150, older: 500, complete: true},
		{name: "past the page bound", count: MaxRecentPullPages*MaxListed + 1, complete: false},
		{name: "missing update time", count: 3, broken: true, err: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var pages []int
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				q := r.URL.Query()
				if r.URL.Path != "/repos/example-org/homelab/pulls" || q.Get("state") != "all" || q.Get("sort") != "updated" ||
					q.Get("direction") != "desc" || q.Get("per_page") != "100" {
					t.Errorf("request = %s", r.URL)
				}
				page, _ := strconv.Atoi(q.Get("page"))
				pages = append(pages, page)
				out := []map[string]any{}
				for i := (page - 1) * 100; i < min(page*100, tc.count+tc.older); i++ {
					updated := since.Add(time.Duration(tc.count-i) * time.Minute)
					p := map[string]any{"number": i + 1, "state": "closed", "closed_at": updated, "head": map[string]any{"sha": "sha-" + strconv.Itoa(i)}, "base": map[string]any{"ref": "main"}}
					if !tc.broken {
						p["updated_at"] = updated
					}
					out = append(out, p)
				}
				_ = json.NewEncoder(w).Encode(out)
			}))
			defer srv.Close()
			c := &Client{BaseURL: srv.URL, Tokens: fixedTokens{}}
			prs, complete, err := c.PullRequestsUpdatedSince(context.Background(), "example-org/homelab", since)
			if (err != nil) != tc.err || complete != tc.complete {
				t.Fatalf("complete = %v, error = %v", complete, err)
			}
			if tc.err {
				return
			}
			if tc.complete && pages[len(pages)-1] != 1 {
				t.Fatalf("pages = %v, want page 1 read again last", pages)
			}
			if tc.complete && (len(prs) < tc.count || prs[0].HeadSHA != "sha-0" || prs[0].State != "closed" || !prs[0].ClosedAt.Equal(since.Add(time.Duration(tc.count)*time.Minute))) {
				t.Fatalf("listed %d PRs, first %+v", len(prs), prs[0])
			}
			if !tc.complete && len(pages) != MaxRecentPullPages {
				t.Fatalf("read %d pages past the bound", len(pages))
			}
		})
	}
}

func TestCommitPullRequestsFailClosed(t *testing.T) {
	for _, tc := range []struct {
		name     string
		count    int
		body     string
		complete bool
		err      bool
	}{
		{name: "one page", count: 2, complete: true},
		{name: "full pages", count: 150, complete: true},
		{name: "past the bound", count: MaxCommitPullPages * MaxListed, complete: false},
		{name: "missing listing", body: "null", err: true},
		{name: "missing base", body: `[{"number":7,"head":{"sha":"abc"},"base":{"ref":""}}]`, err: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path != "/repos/example-org/homelab/commits/abc/pulls" || r.URL.Query().Get("per_page") != "100" {
					t.Errorf("request = %s", r.URL)
				}
				if tc.body != "" {
					_, _ = w.Write([]byte(tc.body))
					return
				}
				page, _ := strconv.Atoi(r.URL.Query().Get("page"))
				out := []map[string]any{}
				for i := (page - 1) * 100; i < min(page*100, tc.count); i++ {
					out = append(out, map[string]any{"number": i + 1, "state": "open", "head": map[string]any{"sha": "abc"}, "base": map[string]any{"ref": "main"}})
				}
				_ = json.NewEncoder(w).Encode(out)
			}))
			defer srv.Close()
			c := &Client{BaseURL: srv.URL, Tokens: fixedTokens{}}
			prs, complete, err := c.CommitPullRequests(context.Background(), "example-org/homelab", "abc")
			if (err != nil) != tc.err || complete != tc.complete {
				t.Fatalf("complete = %v, error = %v", complete, err)
			}
			if tc.complete && (len(prs) != tc.count || prs[0].BaseRef != "main" || prs[0].HeadSHA != "abc") {
				t.Fatalf("PRs = %+v", prs)
			}
		})
	}
}
