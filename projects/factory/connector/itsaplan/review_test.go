package itsaplan

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestReviewReadsUseProjectSequenceAndAccountIDs(t *testing.T) {
	var got seen
	c := server(t, 200, `{"id":95,"projectId":3,"identifier":"HOME-58","columnId":2,"title":"Goal","description":"Criteria"}`, &got)
	i, err := c.GetIssueByNumber(context.Background(), "HOME", 58)
	if err != nil || i.ID != 95 || got.path != "/projects/HOME/issues/58" || got.key != "k-coord" || got.method != "GET" {
		t.Fatalf("issue = %+v, %v; request %+v", i, err, got)
	}
	c = server(t, 200, `{"project":{"id":3,"key":"HOME"},"columns":[{"id":2,"name":"Ready"}],"assignees":[{"userId":"u","username":"joe","kind":"member"}]}`, &got)
	p, err := c.GetProject(context.Background(), "HOME")
	if err != nil || p.Assignees[0].UserID != "u" || p.Assignees[0].Kind != "member" || p.Columns[0].Name != "Ready" {
		t.Fatalf("project = %+v, %v", p, err)
	}
	c = server(t, 200, `{"items":[{"id":8,"issueId":95,"kind":"activity","action":"created","actorUserId":"u","actorName":"not an identity"}],"nextCursor":null}`, &got)
	uid, err := c.IssueAuthor(context.Background(), 95)
	if err != nil || uid != "u" || got.path != "/issues/95/feed" || got.query != "filter=history&order=asc&limit=1" {
		t.Fatalf("author = %q, %v; request %+v", uid, err, got)
	}
	c = server(t, 200, `[{"id":94,"projectId":3,"identifier":"HOME-57","title":"Decision","description":"must not import"}]`, &got)
	i, err = c.FindIssue(context.Background(), "HOME-57")
	if err != nil || i.ID != 94 || i.Description != "" || got.path != "/issues/search" {
		t.Fatalf("metadata = %+v, %v", i, err)
	}
}

func TestLinkedCommentPagesAndChecksIssueAndKind(t *testing.T) {
	calls := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		if r.Method != "GET" || r.URL.Path != "/issues/95/feed" || r.Header.Get("x-api-key") != "read-key" {
			t.Errorf("unexpected request")
		}
		if r.URL.Query().Get("filter") != "comments" || r.URL.Query().Get("limit") != "100" {
			t.Error("unbounded feed")
		}
		if calls == 1 {
			_, _ = w.Write([]byte(`{"items":[{"id":12,"issueId":94,"kind":"comment","body":"wrong issue"},{"id":12,"issueId":95,"kind":"activity","body":"not a comment"}],"nextCursor":{"ts":"2026-11-15 00:00:00+00","id":10}}`))
			return
		}
		var cursor feedCursor
		if json.Unmarshal([]byte(r.URL.Query().Get("cursor")), &cursor) != nil || cursor.ID != 10 {
			t.Error("cursor lost")
		}
		_, _ = w.Write([]byte(`{"items":[{"id":12,"issueId":95,"kind":"comment","replyToId":10,"actorUserId":"sessions","body":"Approved scope"}],"nextCursor":null}`))
	}))
	defer srv.Close()
	c := &Client{BaseURL: srv.URL, Keys: map[string]string{"review": "read-key"}, Coordinator: "review"}
	comment, err := c.LinkedComment(context.Background(), 95, 12)
	if err != nil || comment.ActorUserID != "sessions" || calls != 2 {
		t.Fatalf("comment = %+v, %v, calls %d", comment, err, calls)
	}
}

func TestReviewReadFailuresReturnNoEvidence(t *testing.T) {
	for _, status := range []int{401, 403, 404, 500} {
		var got seen
		c := server(t, status, `{"message":"failure"}`, &got)
		if i, err := c.GetIssueByNumber(context.Background(), "HOME", 58); i != nil || err == nil {
			t.Fatalf("HTTP %d supplied issue", status)
		}
		if d, err := c.LinkedComment(context.Background(), 95, 12); d != nil || err == nil {
			t.Fatalf("HTTP %d supplied comment", status)
		}
		if uid, err := c.IssueAuthor(context.Background(), 95); uid != "" || err == nil {
			t.Fatalf("HTTP %d supplied author", status)
		}
	}
	for _, reply := range []string{"{}", `{"id":95,"identifier":"OTHER-58"}`, "invalid"} {
		var got seen
		c := server(t, 200, reply, &got)
		if i, err := c.GetIssueByNumber(context.Background(), "HOME", 58); i != nil || err == nil {
			t.Fatal("malformed issue supplied evidence")
		}
	}
	var got seen
	c := server(t, 200, `{"items":[],"nextCursor":null}`, &got)
	if uid, err := c.IssueAuthor(context.Background(), 95); uid != "" || err == nil {
		t.Fatal("missing creation event trusted")
	}
	if d, err := c.LinkedComment(context.Background(), 95, 12); d != nil || !errors.Is(err, ErrNotFound) {
		t.Fatal("missing comment trusted")
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if d, err := c.LinkedComment(ctx, 95, 12); d != nil || err == nil {
		t.Fatal("cancelled read supplied evidence")
	}
}

func TestCommentFeedBound(t *testing.T) {
	calls := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		_, _ = w.Write([]byte(`{"items":[],"nextCursor":{"ts":"2026-11-15","id":1}}`))
	}))
	defer srv.Close()
	c := &Client{BaseURL: srv.URL, Keys: map[string]string{"review": "read-key"}, Coordinator: "review"}
	d, err := c.LinkedComment(context.Background(), 95, 12)
	if d != nil || err == nil || !strings.Contains(err.Error(), "bound") || calls != 10 {
		t.Fatalf("unbounded read: %+v, %v, %d", d, err, calls)
	}
}

// invariant: FACT-13
func TestIssueTextAuthorsRequireMatchingImmutableSnapshots(t *testing.T) {
	for _, tc := range []struct {
		name, items, wantTitle, wantDescription string
	}{
		{"both fields original", `{"kind":"activity","action":"created","actorUserId":"joe"}`, "joe", "joe"},
		{"original title by agent", `{"kind":"activity","action":"description","actorUserId":"sessions","payload":{"to":{"value":"Criteria"}}},{"kind":"activity","action":"created","actorUserId":"agent"}`, "agent", "sessions"},
		{"original title by joe", `{"kind":"activity","action":"description","actorUserId":"sessions","payload":{"to":{"value":"Criteria"}}},{"kind":"activity","action":"created","actorUserId":"joe"}`, "joe", "sessions"},
		{"trusted edits", `{"kind":"activity","action":"title","actorUserId":"joe","payload":{"to":{"value":"Goal"}}},{"kind":"activity","action":"description","actorUserId":"sessions","payload":{"to":{"value":"Criteria"}}}`, "joe", "sessions"},
		{"later edit wins", `{"kind":"activity","action":"description","actorUserId":"agent","payload":{"to":{"value":"Criteria"}}},{"kind":"activity","action":"description","actorUserId":"joe","payload":{"to":{"value":"Criteria"}}}`, "", "agent"},
		{"snapshot mismatch", `{"kind":"activity","action":"title","actorUserId":"joe","payload":{"to":{"value":"Old goal"}}}`, "", ""},
		{"missing value", `{"kind":"activity","action":"title","actorUserId":"joe","payload":{"to":{}}}`, "", ""},
		{"comment cannot forge history", `{"kind":"comment","action":"title","actorUserId":"joe","payload":{"to":{"value":"Goal"}}}`, "", ""},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var items []map[string]any
			if err := json.Unmarshal([]byte("["+tc.items+"]"), &items); err != nil {
				t.Fatal(err)
			}
			for _, item := range items {
				item["issueId"] = 95
			}
			body, err := json.Marshal(map[string]any{"items": items, "nextCursor": nil})
			if err != nil {
				t.Fatal(err)
			}
			var got seen
			c := server(t, 200, string(body), &got)
			authors, err := c.IssueTextAuthors(context.Background(), &Issue{ID: 95, Title: "Goal", Description: "Criteria"})
			if err != nil || len(authors) != 2 || authors[0] != tc.wantTitle || authors[1] != tc.wantDescription {
				t.Fatalf("authors = %v, %v", authors, err)
			}
			if got.path != "/issues/95/feed" || got.query != "filter=history&limit=100&order=desc" {
				t.Fatalf("request = %+v", got)
			}
		})
	}
}

func TestIssueHistoryBoundAndReadFailure(t *testing.T) {
	calls := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		if calls > 1 && r.URL.Query().Get("cursor") == "" {
			t.Error("missing cursor")
		}
		_, _ = w.Write([]byte(`{"items":[],"nextCursor":{"ts":"2026-11-15","id":1}}`))
	}))
	defer srv.Close()
	c := &Client{BaseURL: srv.URL, Keys: map[string]string{"review": "read-key"}, Coordinator: "review"}
	authors, err := c.IssueTextAuthors(context.Background(), &Issue{ID: 95})
	if err != nil || len(authors) != 2 || authors[0] != "" || authors[1] != "" || calls != 10 {
		t.Fatalf("history bound: %v, %v, %d", authors, err, calls)
	}
	var got seen
	c = server(t, 503, `{"message":"private failure"}`, &got)
	if authors, err := c.IssueTextAuthors(context.Background(), &Issue{ID: 95}); err == nil || authors != nil {
		t.Fatal("failed history supplied authors")
	}
}

func TestCommentEditsDoNotRetainOriginalAuthority(t *testing.T) {
	for _, tc := range []struct {
		json     string
		original bool
	}{
		{`{"editedAt":null}`, true}, {`{"editedAt":"2026-11-15T00:00:00Z"}`, false}, {`{}`, false},
	} {
		var item FeedItem
		if err := json.Unmarshal([]byte(tc.json), &item); err != nil {
			t.Fatal(err)
		}
		if item.OriginalComment() != tc.original {
			t.Fatalf("original = %v for %s", item.OriginalComment(), tc.json)
		}
	}
}
