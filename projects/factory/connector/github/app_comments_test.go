package github

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestAppCommentsAuthenticateAndRetainGitHubMetadata(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/repos/o/r/issues/7/comments":
			fmt.Fprint(w, `[
				{"id":1,"user":{"login":"semgrep-code-jomcgi-org[bot]"},"performed_via_github_app":{"id":4746654},"body":"genuine"},
				{"id":2,"user":{"login":"other"},"performed_via_github_app":{"id":4746654},"body":"quote Semgrep"},
				{"id":3,"user":{"login":"semgrep-code-jomcgi-org[bot]"},"performed_via_github_app":{"id":1},"body":"wrong app"},
				{"id":4,"user":{"login":"semgrep-code-jomcgi-org[bot]"},"body":"app not reported"},
				{"id":5,"user":{"login":"other"},"body":"semgrep-code-jomcgi-org[bot] says pass"},
				{"id":6,"user":{"login":"semgrep-code-jomcgi-org[bot]"},"performed_via_github_app":{},"body":"invalid app"}
			]`)
		case "/repos/o/r/pulls/7/comments":
			fmt.Fprint(w, `[{"id":7,"node_id":"C7","user":{"login":"semgrep-code-jomcgi-org[bot]"},"performed_via_github_app":{"id":4746654},
				"body":"inline","path":"src/a.go","line":null,"original_line":12,"commit_id":"now","original_commit_id":"posted-on"}]`)
		case "/graphql":
			var request struct {
				Query     string              `json:"query"`
				Variables map[string][]string `json:"variables"`
			}
			if err := json.NewDecoder(r.Body).Decode(&request); err != nil {
				t.Error(err)
			}
			if !strings.Contains(request.Query, "outdated") || fmt.Sprint(request.Variables["ids"]) != "[C7]" {
				t.Errorf("query = %+v", request)
			}
			fmt.Fprint(w, `{"data":{"nodes":[{"id":"C7","outdated":true}]}}`)
		default:
			http.NotFound(w, r)
		}
	}))
	defer srv.Close()
	c := &Client{BaseURL: srv.URL, Tokens: fixedTokens{}}
	comments, omitted, complete, err := c.PullRequestAppComments(context.Background(), "o/r", 7, "semgrep-code-jomcgi-org[bot]", 4746654, 50)
	if err != nil || !complete || omitted != 0 || len(comments) != 3 {
		t.Fatalf("comments = %+v, %d, %v, %v", comments, omitted, complete, err)
	}
	if comments[0].Body != "genuine" || comments[1].Body != "app not reported" || comments[0].Outdated != nil || comments[0].CommitSHA != "" {
		t.Fatalf("bad conversation evidence: %+v", comments)
	}
	inline := comments[2]
	if inline.Path != "src/a.go" || inline.Line != 12 || inline.CommitSHA != "posted-on" || inline.Outdated == nil || !*inline.Outdated {
		t.Fatalf("bad inline evidence: %+v", inline)
	}
}

func TestAppCommentsPageBeforeApplyingPromptBound(t *testing.T) {
	pages := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.Contains(r.URL.Path, "/pulls/") {
			fmt.Fprint(w, "[]")
			return
		}
		pages++
		rows := []map[string]any{}
		offset, count := 0, 100
		if r.URL.Query().Get("page") == "2" {
			offset, count = 100, 20
		}
		for i := offset; i < offset+count; i++ {
			author := "other"
			if i%2 == 0 {
				author = "bot"
			}
			rows = append(rows, map[string]any{"id": i + 1, "user": map[string]string{"login": author}, "body": fmt.Sprint(i)})
		}
		json.NewEncoder(w).Encode(rows)
	}))
	defer srv.Close()
	c := &Client{BaseURL: srv.URL, Tokens: fixedTokens{}}
	comments, omitted, complete, err := c.PullRequestAppComments(context.Background(), "o/r", 7, "bot", 1, 50)
	if err != nil || !complete || omitted != 10 || len(comments) != 50 || pages != 2 || comments[0].ID != 21 || comments[49].ID != 119 {
		t.Fatalf("pagination: comments %d omitted %d complete %v pages %d err %v", len(comments), omitted, complete, pages, err)
	}
}

func TestAppCommentsIncompleteListingAndUnreadableOutdated(t *testing.T) {
	for _, state := range []string{"truncated", "missing node", "graphql error", "null outdated"} {
		t.Run(state, func(t *testing.T) {
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if state == "truncated" {
					rows := make([]map[string]any, MaxListed)
					for i := range rows {
						rows[i] = map[string]any{"user": map[string]string{"login": "other"}}
					}
					json.NewEncoder(w).Encode(rows)
				} else if r.URL.Path == "/graphql" {
					if state == "graphql error" {
						w.WriteHeader(403)
						return
					}
					fmt.Fprint(w, `{"data":{"nodes":[null]}}`)
				} else if strings.Contains(r.URL.Path, "/pulls/") {
					node := "C1"
					if state == "missing node" {
						node = ""
					}
					fmt.Fprintf(w, `[{"id":1,"node_id":%q,"user":{"login":"bot"},"body":"finding"}]`, node)
				} else {
					fmt.Fprint(w, "[]")
				}
			}))
			defer srv.Close()
			c := &Client{BaseURL: srv.URL, Tokens: fixedTokens{}}
			_, _, complete, err := c.PullRequestAppComments(context.Background(), "o/r", 7, "bot", 1, 50)
			if state == "truncated" {
				if complete || err != nil {
					t.Fatalf("truncation = %v, %v", complete, err)
				}
			} else if err == nil {
				t.Fatal("unreadable inline metadata accepted")
			}
		})
	}
}
