package github

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestBrokerCommentsUsesTheBrokerProxy(t *testing.T) {
	var calls []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != DefaultCommentsPath || r.Header.Get("Authorization") != "Bearer sa-token" {
			t.Errorf("call %s %s with %q", r.Method, r.URL.Path, r.Header.Get("Authorization"))
		}
		body, _ := io.ReadAll(r.Body)
		calls = append(calls, r.Method+" "+r.URL.RawQuery+" "+string(body))
		switch r.Method {
		case http.MethodGet:
			page := r.URL.Query().Get("page")
			_ = json.NewEncoder(w).Encode(map[string]any{
				"comments": []map[string]any{{"id": map[string]int{"1": 1, "2": 2}[page], "author": "a", "body": "b" + page}},
				"next":     page == "1",
			})
		case http.MethodPost:
			w.WriteHeader(http.StatusCreated)
			_, _ = w.Write([]byte(`{"id":9,"author":"bot[bot]","body":"new"}`))
		case http.MethodPatch:
			if strings.Contains(string(body), `"id":404`) {
				w.WriteHeader(http.StatusNotFound)
				_, _ = w.Write([]byte(`{"factory_error":"not_found"}`))
				return
			}
			if strings.Contains(string(body), `"id":403`) {
				w.WriteHeader(http.StatusForbidden)
				_, _ = w.Write([]byte(`{"factory_error":"not_the_reviewers_comment"}`))
				return
			}
			_, _ = w.Write([]byte(`{"id":9,"author":"bot[bot]","body":"edited"}`))
		}
	}))
	defer srv.Close()
	b := &BrokerComments{URL: srv.URL, Bearer: staticBearer("sa-token"), HTTP: srv.Client()}
	ctx := context.Background()

	all, complete, err := b.IssueComments(ctx, "o/r", 7)
	if err != nil || !complete || len(all) != 2 || all[0].ID != 1 || all[1].Body != "b2" {
		t.Fatalf("list = %+v, %v, %v", all, complete, err)
	}
	c, err := b.CreateIssueComment(ctx, "o/r", 7, "new")
	if err != nil || c.ID != 9 || c.Author != "bot[bot]" {
		t.Fatalf("create = %+v, %v", c, err)
	}
	if c, err := b.UpdateIssueComment(ctx, "o/r", 9, "edited"); err != nil || c.Body != "edited" {
		t.Fatalf("update = %+v, %v", c, err)
	}
	if _, err := b.UpdateIssueComment(ctx, "o/r", 404, "x"); !errors.Is(err, ErrNotFound) {
		t.Fatalf("deleted comment = %v, want ErrNotFound", err)
	}
	if _, err := b.UpdateIssueComment(ctx, "o/r", 403, "x"); err == nil || errors.Is(err, ErrNotFound) {
		t.Fatalf("refused edit = %v", err)
	}
	want := []string{
		"GET number=7&page=1&repository=o%2Fr ",
		"GET number=7&page=2&repository=o%2Fr ",
		`POST  {"body":"new","number":7,"repository":"o/r"}`,
		`PATCH  {"body":"edited","id":9,"repository":"o/r"}`,
	}
	for i, w := range want {
		if calls[i] != w {
			t.Fatalf("call %d = %q, want %q", i, calls[i], w)
		}
	}
}

// A listing stops at MaxCommentPages and reports itself incomplete only
// when the broker still names a next page.
func TestBrokerCommentsListingBound(t *testing.T) {
	pages := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		pages++
		_ = json.NewEncoder(w).Encode(map[string]any{"comments": []any{}, "next": true})
	}))
	defer srv.Close()
	b := &BrokerComments{URL: srv.URL, Bearer: staticBearer("t"), HTTP: srv.Client()}
	if _, complete, err := b.IssueComments(context.Background(), "o/r", 1); err != nil || complete || pages != MaxCommentPages {
		t.Fatalf("bounded list: complete %v, pages %d, %v", complete, pages, err)
	}
}
